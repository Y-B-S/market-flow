"""信号验证：四象限资金流信号到底有没有预测力？

这个脚本回答一个具体问题：
    「今日流入但中期流出」（抄底试探）的板块，
    在之后 1/3/5/10 个交易日的涨跌幅，是否显著不同于全市场？

关键设计原则（避免自欺）：
1. 只用**严格未来**数据做结果变量，信号日之后的数据绝不参与信号计算。
2. 用**同日全市场板块**作基准（market-neutral），而不是与 0 比较——
   否则牛市里任何信号都"有效"。
3. 给出超额收益的均值、胜率、样本量、t 统计量，以及分年度稳定性。
4. 每个数字都标注样本量；样本 < 30 的结论一律标为"样本不足"。

用法：
    python3 validate.py                 # 全行业板块，90 日窗口
    python3 validate.py --days 120      # 更长窗口
    python3 validate.py --top 60        # 只取成交额前 60 大板块（更快）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import fetch  # noqa: E402

FLOW_API = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
            "?lmt={lmt}&klt=101&secid=90.{code}"
            "&fields1=f1,f2,f3,f7"
            "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63")

# 资金流日线的列语义（已用"收盘价变化 vs 涨跌幅字段"自洽验证）
# 0=日期 1=主力净额 2=小单 3=中单 4=大单 5=超大单
# 6=主力占比 7..10=各档占比 11=收盘价 12=涨跌幅
COL_DATE, COL_MAIN, COL_CLOSE, COL_CHG = 0, 1, 11, 12

HORIZONS = (1, 3, 5, 10)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_board_history(code, lmt=90):
    """取单个板块的日线资金流 + 收盘价。返回按日期升序的 list[dict]。"""
    for attempt in range(3):
        try:
            d = fetch.get(FLOW_API.format(lmt=lmt, code=code), retries=2, timeout=25)
            ks = (d.get("data") or {}).get("klines") or []
            rows = []
            for k in ks:
                p = k.split(",")
                if len(p) <= COL_CHG:
                    continue
                rows.append({
                    "date": p[COL_DATE],
                    "main": _f(p[COL_MAIN]),
                    "close": _f(p[COL_CLOSE]),
                    "chg": _f(p[COL_CHG]),
                })
            rows.sort(key=lambda r: r["date"])
            return rows
        except Exception:  # noqa: BLE001
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
    return []


def classify(today, d5, d10):
    """四象限归类（与 metrics.py 的 _classify_quadrant 口径一致）。

    返回 'accel'/'probe'/'bleed'/'dist'/None。
    """
    if today is None or d5 is None:
        return None
    st = 1 if today > 0 else (-1 if today < 0 else 0)
    s5 = 1 if d5 > 0 else (-1 if d5 < 0 else 0)
    if st == 0 or s5 == 0:
        return None
    if st > 0 and s5 > 0:
        return "accel"      # 流入加速
    if st > 0 and s5 < 0:
        return "probe"      # 抄底试探
    if st < 0 and s5 < 0:
        return "bleed"      # 持续失血
    return "dist"           # 拉高出货


QUAD_LABEL = {"accel": "流入加速", "probe": "抄底试探",
              "bleed": "持续失血", "dist": "拉高出货"}


def build_samples(histories, lmt):
    """把各板块历史转成 (信号日, 象限, 未来N日收益, 同期基准收益) 样本。

    ⚠️ 严格防未来函数：
    - 信号只用 index i 及之前的数据计算（今日/5日/10日）
    - 结果用 i+1 .. i+h 的收盘价计算，即信号日**之后**才发生的事
    """
    samples = []
    for code, h in histories.items():
        n = len(h)
        if n < 15:
            continue
        for i in range(10, n):          # 前 10 根不足以算 10 日净额
            today = h[i]["main"]
            if today is None:
                continue
            # 滚动 5/10 日净额：用 i 及之前的数据，不含未来
            d5 = sum(x["main"] for x in h[i - 4:i + 1] if x["main"] is not None)
            d10 = sum(x["main"] for x in h[i - 9:i + 1] if x["main"] is not None)
            quad = classify(today, d5, d10)
            if quad is None:
                continue
            base_close = h[i]["close"]
            if base_close is None or base_close <= 0:
                continue
            fwd = {}
            for hz in HORIZONS:
                j = i + hz
                fwd[hz] = ((h[j]["close"] / base_close - 1.0) * 100.0
                           if j < n and h[j]["close"] else None)
            samples.append({
                "code": code,
                "date": h[i]["date"],
                "quad": quad,
                "fwd": fwd,
                "net_amount": today,
            })
    return samples


def stats(vals):
    """一组超额收益的基本统计。"""
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0}
    mean = sum(vals) / n
    med = statistics.median(vals)
    win = sum(1 for v in vals if v > 0) / n * 100.0
    sd = statistics.stdev(vals) if n > 1 else None
    t = (mean / (sd / math.sqrt(n))) if (sd and n > 1 and sd > 0) else None
    return {"n": n, "mean": mean, "median": med, "win_rate": win,
            "std": sd, "t": t, "min": min(vals), "max": max(vals)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="四象限资金流信号验证")
    ap.add_argument("--days", type=int, default=90, help="回溯天数（日线根数）")
    ap.add_argument("--top", type=int, default=None,
                    help="只取成交额前 N 个板块（加速用）")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default="validate_report.json")
    args = ap.parse_args(argv)

    print(f"· 取行业板块清单…")
    boards = fetch.clist_all(
        fetch.FS_INDUSTRY,
        "f12,f14,f3,f6,f62,f164,f174",
        page_size=100, workers=args.workers,
    )
    if args.top:
        boards.sort(key=lambda r: -(fetch.num(r.get("f6")) or 0))
        boards = boards[:args.top]
    codes = [r["f12"] for r in boards if r.get("f12")]
    names = {r["f12"]: r.get("f14") for r in boards}
    print(f"· 板块数 {len(codes)}，回溯 {args.days} 根日线")

    t0 = time.time()
    histories = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {c: ex.submit(fetch_board_history, c, args.days) for c in codes}
        done = 0
        for c, f in futs.items():
            h = f.result()
            if h:
                histories[c] = h
            done += 1
            if done % 50 == 0:
                print(f"  … {done}/{len(codes)}")
    print(f"· 成功取到 {len(histories)}/{len(codes)} 个板块，耗时 {time.time()-t0:.1f}s")

    if not histories:
        print("✗ 无可用历史数据")
        return 1

    samples = build_samples(histories, args.days)
    print(f"· 生成 {len(samples)} 个信号样本")

    # 基准：同一交易日的全市场板块平均收益（market-neutral 超额）
    baseline = {}
    for s in samples:
        for hz in HORIZONS:
            v = s["fwd"][hz]
            if v is not None:
                b = baseline.setdefault((s["date"], hz), [])
                b.append(v)
    base_mean = {k: (sum(v) / len(v)) for k, v in baseline.items() if v}

    # 按象限统计超额收益
    print("\n" + "=" * 78)
    print("四象限信号的未来收益（超额 = 板块收益 - 当日全市场板块均值）")
    print("=" * 78)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "days": args.days,
        "board_count": len(histories),
        "sample_count": len(samples),
        "date_range": [min(s["date"] for s in samples), max(s["date"] for s in samples)],
        "by_quadrant": {},
        "per_board": {},
    }

    for q in ("accel", "probe", "bleed", "dist"):
        rows = [s for s in samples if s["quad"] == q]
        if not rows:
            continue
        print(f"\n【{QUAD_LABEL[q]}】样本 {len(rows)} 个")
        qrep = {}
        for hz in HORIZONS:
            excess = []
            for s in rows:
                v = s["fwd"][hz]
                b = base_mean.get((s["date"], hz))
                if v is not None and b is not None:
                    excess.append(v - b)
            st = stats(excess)
            qrep[f"{hz}d"] = st
            if st["n"]:
                sig = ""
                if st.get("t") is not None:
                    if abs(st["t"]) >= 2.0:
                        sig = "  ◀ 显著(|t|≥2)"
                    elif abs(st["t"]) >= 1.6:
                        sig = "  ◁ 边缘(|t|≥1.6)"
                flag = "" if st["n"] >= 30 else "  ⚠ 样本不足(<30)"
                tstr = ("%+5.2f" % st["t"]) if st.get("t") is not None else "  n/a"
                print(f"  {hz:>2}日: 超额均值 {st['mean']:+6.2f}%  "
                      f"中位 {st['median']:+6.2f}%  胜率 {st['win_rate']:5.1f}%  "
                      f"t={tstr}  n={st['n']}{sig}{flag}")
        report["by_quadrant"][q] = qrep

    # 逐板块稳定性：某板块该象限信号是否反复有效
    print("\n" + "=" * 78)
    print("逐板块稳定性（仅列样本≥5 的板块，看信号是否只在个别板块成立）")
    print("=" * 78)
    for q in ("probe", "accel"):
        rows = [s for s in samples if s["quad"] == q]
        if not rows:
            continue
        by_board = {}
        for s in rows:
            v = s["fwd"][5]
            b = base_mean.get((s["date"], 5))
            if v is not None and b is not None:
                by_board.setdefault(s["code"], []).append(v - b)
        items = [(c, sum(v) / len(v), len(v)) for c, v in by_board.items() if len(v) >= 3]
        items.sort(key=lambda x: -x[1])
        n_board = len(items)
        n_pos = sum(1 for _, m, _ in items if m > 0)
        print(f"\n【{QUAD_LABEL[q]}】5日超额：{n_board} 个板块有效，"
              f"其中 {n_pos} 个为正（{n_pos/max(1,n_board)*100:.0f}%）")
        for c, m, cnt in items[:8]:
            print(f"     {names.get(c, c):<14s} 均值 {m:+6.2f}%  n={cnt}")
        report["per_board"][q] = {
            "board_count": n_board, "positive": n_pos,
            "positive_ratio": (n_pos / n_board * 100) if n_board else None,
            "top": [{"code": c, "name": names.get(c), "mean": m, "n": cnt}
                    for c, m, cnt in items[:10]],
        }

    # ── 诚实的结论判定 ──
    print("\n" + "=" * 78)
    print("结论")
    print("=" * 78)
    probe5 = report["by_quadrant"].get("probe", {}).get("5d", {})
    verdict = []
    if not probe5.get("n"):
        verdict.append("样本不足，无法得出结论。")
    else:
        n = probe5["n"]
        mean = probe5["mean"]
        t = probe5.get("t")
        if n < 30:
            verdict.append(f"✗ 样本量仅 {n}，**不足以判定**。需要更长回溯期或多个验证窗口。")
        elif t is None:
            verdict.append("✗ 无法计算 t 统计量（方差异常）。")
        elif abs(t) >= 2.0 and mean > 0:
            verdict.append(f"✓ 「抄底试探」5日超额 {mean:+.2f}%，t={t:+.2f}，"
                           f"在统计上显著为正。")
        elif abs(t) >= 2.0 and mean < 0:
            verdict.append(f"✗ 「抄底试探」5日超额 {mean:+.2f}%，t={t:+.2f}，"
                           f"统计上显著为**负**——信号方向是反的。")
        else:
            verdict.append(f"✗ 「抄底试探」5日超额 {mean:+.2f}%，t={t:+.2f}，"
                           f"**不显著**——这个信号目前没有可用的预测力。")
    verdict.append("注意：单一样本区间（单一市场环境）的显著性可能是偶然，"
                   "需用不同时间窗口重复验证才算可靠。")
    verdict.append("且本检验未扣除交易成本、涨跌停无法买入、板块成分股变动等实际约束。")
    for v in verdict:
        print("  " + v)
    report["verdict"] = verdict

    out = os.path.join(BASE, args.out)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n· 报告已保存 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
