"""编排层：取数 → 存储 → 计算 → 渲染。CLI 入口。

用法：
    python3 main.py                  # 拉今日数据，出终端摘要 + HTML
    python3 main.py --no-fetch       # 用磁盘上最新快照重算（离线/调样式）
    python3 main.py --date 2026-11-03
    python3 main.py --term-only      # 只出终端
    python3 main.py --open           # 生成后自动打开 HTML
"""
from __future__ import annotations

import argparse
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import fetch as fetch_mod      # noqa: E402
import store                   # noqa: E402


def _load_modules():
    """延迟导入，某个模块缺失时给出清晰提示而不是崩掉整个流程。"""
    mods = {}
    for name in ("metrics", "render_html", "render_term"):
        try:
            mods[name] = __import__(name)
        except ImportError as e:
            mods[name] = None
            mods.setdefault("_errors", []).append(f"{name}: {e}")
    return mods


def build_analysis(snap, mods):
    """计算层失败时退回一个最小可渲染结构，保证渲染层仍能出图。"""
    m = mods.get("metrics")
    if m is None:
        return {"as_of": snap.get("as_of"),
                "limitations": ["metrics 模块不可用，仅渲染原始数据"]}
    try:
        return m.compute(snap)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return {"as_of": snap.get("as_of"),
                "limitations": [f"metrics.compute 失败: {e}"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="A股资金流动大盘")
    ap.add_argument("--no-fetch", action="store_true", help="不联网，用磁盘最新快照")
    ap.add_argument("--date", help="指定交易日 YYYY-MM-DD")
    ap.add_argument("--term-only", action="store_true", help="只输出终端摘要")
    ap.add_argument("--html-only", action="store_true", help="只生成 HTML")
    ap.add_argument("--open", action="store_true", help="生成后自动打开 HTML")
    ap.add_argument("--width", type=int, default=None, help="终端渲染宽度")
    ap.add_argument("--save", action="store_true", default=True,
                    help="保存快照到 data/（默认开启）")
    args = ap.parse_args(argv)

    # 1) 取数
    if args.no_fetch:
        snap = store.load(args.date)
        if snap is None:
            print("✗ 本地没有快照，去掉 --no-fetch 先联网拉一次", file=sys.stderr)
            return 1
        print(f"· 使用本地快照 {snap['as_of']}")
    else:
        print("· 拉取行情数据…")
        try:
            snap = fetch_mod.fetch_all(as_of=args.date)
        except Exception as e:  # noqa: BLE001
            print(f"✗ 取数失败: {e}", file=sys.stderr)
            fallback = store.load(args.date)
            if fallback is None:
                return 1
            print(f"· 退回本地快照 {fallback['as_of']}", file=sys.stderr)
            snap = fallback
        if not args.date:
            store.save(snap)
            print(f"· 已保存 data/{snap['as_of']}.json 并追加时序")

    # 2) 计算
    mods = _load_modules()
    errs = mods.get("_errors") or []
    if errs:
        print("⚠ 模块缺失: " + "; ".join(errs), file=sys.stderr)
    analysis = build_analysis(snap, mods)

    # 2.5) 长期摘要（接口只留 120 交易日，今天的摘要不存就永久消失）
    try:
        import bars
        if not (analysis or {}).get("empty_state"):
            row = bars.summarize(snap, analysis)
            n = bars.append(row)
            print(f"· 已记录长期摘要 data/bars.jsonl（共 {n} 天）")
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 长期摘要写入失败: {type(e).__name__}: {e}", file=sys.stderr)

    # 3) 渲染
    rc = 0
    if not args.html_only:
        rt = mods.get("render_term")
        if rt:
            try:
                out = rt.render_term(analysis, snap, width=args.width)
                print()
                print(out)
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                print(f"✗ 终端渲染失败: {e}", file=sys.stderr)
                rc = 1
        else:
            print("✗ render_term 不可用", file=sys.stderr)
            rc = 1

    if not args.term_only:
        rh = mods.get("render_html")
        if rh:
            try:
                html = rh.render_html(analysis, snap)
                out_dir = os.path.join(BASE, "out")
                os.makedirs(out_dir, exist_ok=True)
                path = os.path.join(out_dir, f"flow-{snap['as_of']}.html")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(html)
                # latest 软链式副本，方便固定路径打开
                latest = os.path.join(out_dir, "latest.html")
                with open(latest, "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"\n· HTML 已生成: {path}")
                print(f"· 固定路径:   {latest}")
                if args.open:
                    import webbrowser
                    webbrowser.open("file://" + latest)
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                print(f"✗ HTML 渲染失败: {e}", file=sys.stderr)
                rc = 1
        else:
            print("✗ render_html 不可用", file=sys.stderr)
            rc = 1

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
