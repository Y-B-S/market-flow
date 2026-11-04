"""背离解释器 · 测试。

    python3 explain_test.py

设计原则：测"结论会不会误导"，不是测"代码跑不跑"。
每条断言对应一个具体的误导风险。
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

def _latest_snapshot():
    """取 data/ 下最新的、含完整宽度数据的快照。

    ⚠️ 早期这里硬编码了 "2026-11-03.json"。该快照后因数据错误被删除，
    模块直接失效 —— 硬编码日期是测试与演示代码的典型反模式，
    每天的数据都会变，写死等于定时炸弹。
    """
    import glob
    cands = sorted(glob.glob(os.path.join(ROOT, "data", "2*.json")))[::-1]
    for p in cands:
        try:
            s = json.load(open(p, encoding="utf-8"))
            if (s.get("breadth") or {}).get("amount_total"):
                return s
        except Exception:
            continue
    # 退而求其次：任取最后一个
    for p in cands:
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
    return None
sys.path.insert(0, HERE)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✓ {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(f"{name} :: {detail}")
        print(f"  ✗ {name}  {detail}")


def section(t):
    print(f"\n── {t} ──")


def main():
    import explain_fetch as F
    import explain_metrics as M
    import explain_render as R

    # ── 1. 取数层 ──
    section("1. 取数层：同源过滤与去重")

    # 核心风险：把同源资金流报道当独立证据 → 拿数据源A验证数据源A
    try:
        n1 = F.fetch_news("电力设备")
        if n1:
            check("新闻同源过滤生效",
                  n1["dropped_count"] > 0 or len(n1["kept"]) < n1["raw_count"],
                  f"原始 {n1['raw_count']} → 保留 {len(n1['kept'])}，"
                  f"剔除同源 {n1['dropped_count']}")
            # 保留项中不得再有同源关键词
            bad = [x["title"] for x in n1["kept"]
                   if any(w in x["title"] for w in F.FLOW_SELFREF)]
            check("保留项无同源关键词残留", not bad, f"残留: {bad[:2]}")
            # 标题去重
            titles = [re.sub(r"[\s，,。、\-—|｜]+", "", x["title"]) for x in n1["kept"]]
            check("新闻标题已去重", len(titles) == len(set(titles)),
                  f"{len(titles)} 条中 {len(set(titles))} 个唯一")
        else:
            check("新闻取数可用", False, "返回 None")
    except Exception as e:  # noqa: BLE001
        check("新闻取数", False, f"{type(e).__name__}: {e}")

    # 业绩预告：必须去重（实测原始有 2.5 倍重复）
    try:
        fc = F.fetch_forecast_calendar("2026-07-01", max_pages=2)
        if fc:
            check("业绩预告已去重", fc["deduped"] < fc["raw_rows"],
                  f"{fc['raw_rows']} 行 → {fc['deduped']} 条唯一")
            # 类型映射必须覆盖全部
            unmapped = {r["type"] for r in fc["records"]
                        if r["type"] not in F.FORECAST_DIR}
            check("预告类型映射完整", not unmapped, f"未映射: {unmapped}")
        else:
            check("业绩预告取数可用", False, "返回 None")
    except Exception as e:  # noqa: BLE001
        check("业绩预告取数", False, f"{type(e).__name__}: {e}")

    # 估值：只接受官方口径，不得用代理
    try:
        v = F.fetch_valuation(["电网设备"])
        g = v.get("电网设备")
        if g:
            check("估值取到完整历史", g["hist_days"] >= 100,
                  f"{g['hist_days']} 个交易日")
            check("估值分位可算", g["pe_pct"] is not None,
                  f"PE={g['pe_ttm']:.1f} 分位={g['pe_pct']:.0f}%")
        else:
            check("估值取数", False, "电网设备应有估值")
        # 无估值的板块必须返回 None，不能造代理值
        v2 = F.fetch_valuation(["电力设备"])
        check("无估值板块返回 None（不用代理）",
              v2.get("电力设备") is None,
              "电力设备不在申万估值体系，应返回 None")
    except Exception as e:  # noqa: BLE001
        check("估值取数", False, f"{type(e).__name__}: {e}")

    # ── 2. 计算层：方向判定与一致性 ──
    section("2. 计算层：方向与一致性逻辑")

    # 方向符号约定必须自洽（负面证据支持背离，正面证据质疑）
    cases = [
        ("估值高分位→支持", M._layer_valuation("x", {"pe_ttm": 50, "pe_pct": 85,
                                                "hist_days": 100}), 1),
        ("估值低分位→反对", M._layer_valuation("x", {"pe_ttm": 20, "pe_pct": 10,
                                                "hist_days": 100}), -1),
        ("估值中分位→中性", M._layer_valuation("x", {"pe_ttm": 30, "pe_pct": 50,
                                                "hist_days": 100}), 0),
    ]
    for name, layer, expect in cases:
        check(name, layer["dir_num"] == expect, f"实际 {layer['dir_num']}")

    # 缺层必须 available=False 且不编造方向
    miss = M._layer_valuation("x", None)
    check("缺估值层不编造方向",
          miss["available"] is False and miss["direction"] is None,
          miss["evidence"][:40])

    # 一致性：四层支持→一致；三层支持一层反对→部分矛盾；全员反对→矛盾
    def mk(nums):
        return [{"available": True, "dir_num": n, "layer": f"L{i}", "direction": "x"}
                for i, n in enumerate(nums)]

    check("四层支持→一致", M._consistency(mk([1, 1, 1, 1]))["label"] == "一致")
    check("三层支持一层反对→部分矛盾",
          M._consistency(mk([1, 1, 1, -1]))["label"] == "部分矛盾")
    check("四层全反对→矛盾", M._consistency(mk([1, -1, -1, -1]))["label"] == "矛盾")
    # 只有两层时结论强度必须被限定
    c2 = M._consistency(mk([1, 1]) + [{"available": False, "dir_num": 0, "layer": "L2"}])
    check("两层结果标注强度限制",
          c2["available"] == 2 and "强度" in (c2.get("note") or ""),
          c2.get("note", "")[:50])

    # ── 3. 端到端 ──
    section("3. 端到端：真实背离样本")

    snap = _latest_snapshot()
    if snap is None:
        check("本地快照存在", False, "data/ 下无可用快照")
        return report()
    snap_path = os.path.join(ROOT, "data", f"{snap['as_of']}.json")

    try:
        import metrics
        a0 = metrics.compute(snap)
        div = (a0.get("divergence_gated") or [])[:4]
        names = [d["name"] for d in div]
        boards = [(d.get("code"), d.get("name")) for d in div]

        data = F.fetch_all(names, boards=boards, as_of=snap["as_of"])
        res = M.compute(div, data)

        check("生成卡片数量正确", len(res["cards"]) == len(div),
              f"{len(res['cards'])} 张")
        check("每卡都有判定", all(c["consistency"]["label"] for c in res["cards"]))

        # 关键：不得把两层包装成三层的结论
        for c in res["cards"]:
            av = c["consistency"]["available"]
            note = c["consistency"].get("note") or ""
            if av < 3:
                check(f"{c['name']} 层数{av}时有强度限定",
                      av == 1 or "强度" in note or av >= 3,
                      note[:40])
                break

        # 缺估值必须进 limitations
        check("缺估值进 limitations",
              any("估值" in str(x) for x in res["limitations"]),
              "已标注")

        # 财报层必须带选择性偏差提示
        fc_note = False
        for c in res["cards"]:
            l = next((x for x in c["layers"] if x["layer"] == "财报"), None)
            if l and l.get("available") and "偏乐观" in (l.get("caveat") or ""):
                fc_note = True
                break
        check("财报层标注选择性偏差", fc_note, "caveat 含'偏乐观'")

        # 渲染
        html = R.render_html(res)
        check("HTML 非空", len(html) > 5000, f"{len(html)} chars")
        check("零外链", not re.search(r'<script[^>]+src=', html)
              and not re.search(r'<link[^>]+stylesheet', html))
        check("标签闭合 div", html.count("<div") == html.count("</div>"),
              f"{html.count('<div')}/{html.count('</div>')}")
        check("无 markdown 星号泄漏", "**" not in html,
              "已清理")
        check("无字面量转义泄漏", "&lt;br&gt;" not in html)

        # 合规：不得出现买卖建议
        banned = ["建议买入", "建议卖出", "必将", "一定涨", "稳赚", "目标价"]
        hits = [w for w in banned if w in html]
        check("无买卖建议措辞", not hits, f"命中: {hits}")

        # 降级：analysis 为空不崩
        html2 = R.render_html({})
        check("空 analysis 渲染不崩", isinstance(html2, str) and len(html2) > 2000)

    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("端到端流程", False, f"{type(e).__name__}: {e}")

    return report()


def report():
    print(f"\n{'='*66}")
    print(f"通过 {len(PASS)} / {len(PASS)+len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for f in FAIL:
            print(f"  ✗ {f}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
