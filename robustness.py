"""稳健性检验：单窗口的统计显著性可能是偶然。

validate.py 的结果显示「流入加速」在 90 日窗口里 3/5 日超额显著为正（t≈2.1），
但这类结论最容易自欺。本脚本做四项独立检验：

1. **多窗口重复**：把 90 日切成多个不重叠子区间，看符号是否一致
2. **随机基准检验**：随机打乱象限标签，看"显著"是否也能从噪声里冒出来
3. **中位数 vs 均值背离**：均值显著但中位数为负，说明是被极少数样本拉动的
4. **样本外衰减**：前半段 vs 后半段

用法：python3 robustness.py
"""
from __future__ import annotations

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

import validate as V


def _stats(vals):
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "t": None, "median": None, "win_rate": None}
    mean = sum(vals) / n
    med = statistics.median(vals)
    sd = statistics.stdev(vals) if n > 1 else None
    t = (mean / (sd / math.sqrt(n))) if (sd and n > 1 and sd > 0) else None
    return {"n": n, "mean": mean, "t": t, "median": med,
            "win_rate": sum(1 for v in vals if v > 0) / n * 100.0}


def main():
    print("· 加载历史数据…")
    boards = V.fetch.clist_all(V.fetch.FS_INDUSTRY,
                               "f12,f14,f6", page_size=100, workers=10)
    codes = [r["f12"] for r in boards if r.get("f12")]
    histories = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {c: ex.submit(V.fetch_board_history, c, 90) for c in codes}
        for c, f in futs.items():
            h = f.result()
            if h:
                histories[c] = h
    print(f"  {len(histories)}/{len(codes)} 板块，耗时 {time.time()-t0:.1f}s")

    samples = V.build_samples(histories, 90)
    dates = sorted({s["date"] for s in samples})
    print(f"  样本 {len(samples)}，日期 {dates[0]} ~ {dates[-1]}（{len(dates)} 个交易日）")

    # 当日全市场基准
    base = {}
    for s in samples:
        for hz in V.HORIZONS:
            v = s["fwd"][hz]
            if v is not None:
                base.setdefault((s["date"], hz), []).append(v)
    base_mean = {k: sum(v) / len(v) for k, v in base.items() if v}

    def excess(rows, hz):
        out = []
        for s in rows:
            v = s["fwd"][hz]
            b = base_mean.get((s["date"], hz))
            if v is not None and b is not None:
                out.append(v - b)
        return out

    print("\n" + "=" * 80)
    print("检验 1：多窗口重复（把样本按时间切成 3 段，看符号是否一致）")
    print("=" * 80)

    seg_size = max(1, len(dates) // 3)
    segments = [dates[i * seg_size:(i + 1) * seg_size] if i < 2 else dates[2 * seg_size:]
                for i in range(3)]
    segments = [set(s) for s in segments if s]

    result1 = {}
    for q in ("accel", "probe"):
        print(f"\n【{V.QUAD_LABEL[q]}】5日超额，分段：")
        seg_means = []
        for si, seg in enumerate(segments):
            rows = [s for s in samples if s["quad"] == q and s["date"] in seg]
            st = _stats(excess(rows, 5))
            seg_means.append(st["mean"])
            if st["n"]:
                tstr = "%+.2f" % st["t"] if st["t"] is not None else "n/a"
                print(f"  段{si+1} ({min(seg)}~{max(seg)}): 均值 {st['mean']:+6.2f}%  "
                      f"t={tstr}  中位 {st['median']:+6.2f}%  n={st['n']}")
            else:
                print(f"  段{si+1}: 无样本")
        valid = [m for m in seg_means if m is not None]
        same_sign = len({1 if m > 0 else -1 for m in valid}) == 1 if valid else False
        print(f"  → 符号一致: {same_sign}（{'稳定' if same_sign else '不稳定，信号在不同时段方向会变'}）")
        result1[q] = {"segment_means": seg_means, "sign_consistent": same_sign}

    print("\n" + "=" * 80)
    print("检验 2：随机基准——打乱象限标签，看能否也从噪声里得到「显著」")
    print("=" * 80)
    random.seed(42)
    labels = [s["quad"] for s in samples]
    random.shuffle(labels)
    shuffled = [dict(s, quad=lb) for s, lb in zip(samples, labels)]
    n_sig = 0
    trials = 200
    real_t = _stats(excess([s for s in samples if s["quad"] == "accel"], 5))["t"]
    fake_ts = []
    for _ in range(trials):
        random.shuffle(labels)
        rows = [dict(s, quad=lb) for s, lb in zip(samples, labels) if lb == "accel"]
        st = _stats(excess(rows, 5))
        if st["t"] is not None:
            fake_ts.append(st["t"])
            if abs(st["t"]) >= 2.0:
                n_sig += 1
    fake_ts.sort()
    print(f"  真实「流入加速」5日 t = {real_t:+.2f}" if real_t else "")
    print(f"  {trials} 次随机标签中，|t|≥2.0 的比例: {n_sig/trials*100:.1f}%"
          f"（即误报率；若接近 5%，说明真实 t≈2 只是噪声边缘）")
    if fake_ts:
        print(f"  随机 t 值分布: 5%分位 {fake_ts[int(0.05*len(fake_ts))]:+.2f}  "
              f"中位 {statistics.median(fake_ts):+.2f}  "
              f"95%分位 {fake_ts[int(0.95*len(fake_ts))]:+.2f}")
        pct = sum(1 for x in fake_ts if x < real_t) / len(fake_ts) * 100 if real_t else 0
        print(f"  真实 t 在随机分布中的百分位: {pct:.0f}%（>97.5% 才算显著）")

    print("\n" + "=" * 80)
    print("检验 3：均值 vs 中位数背离（均值显著但中位数为负 = 被极少数拉动）")
    print("=" * 80)
    for q in ("accel", "probe", "bleed", "dist"):
        rows = [s for s in samples if s["quad"] == q]
        st = _stats(excess(rows, 5))
        if st["n"]:
            gap = st["mean"] - st["median"]
            flag = ""
            if st["mean"] > 0 and st["median"] < 0:
                flag = "  ⚠ 均值正/中位负 → 结论靠少数极端样本支撑"
            print(f"  {V.QUAD_LABEL[q]:<6s} 均值 {st['mean']:+6.2f}%  "
                  f"中位 {st['median']:+6.2f}%  差 {gap:+5.2f}pp  n={st['n']}{flag}")

    print("\n" + "=" * 80)
    print("检验 4：样本外衰减（前 45 日 vs 后 45 日）")
    print("=" * 80)
    half = len(dates) // 2
    first, second = set(dates[:half]), set(dates[half:])
    for q in ("accel", "probe"):
        f_st = _stats(excess([s for s in samples if s["quad"] == q and s["date"] in first], 5))
        s_st = _stats(excess([s for s in samples if s["quad"] == q and s["date"] in second], 5))
        if f_st["n"] and s_st["n"]:
            print(f"  {V.QUAD_LABEL[q]:<6s} 前半 {f_st['mean']:+6.2f}% (n={f_st['n']})  "
                  f"后半 {s_st['mean']:+6.2f}% (n={s_st['n']})  "
                  f"衰减 {s_st['mean']-f_st['mean']:+6.2f}pp")

    print("\n" + "=" * 80)
    print("总判定")
    print("=" * 80)
    accel_t = _stats(excess([s for s in samples if s["quad"] == "accel"], 5))["t"]
    accel_med = _stats(excess([s for s in samples if s["quad"] == "accel"], 5))["median"]
    accel_seg = result1.get("accel", {}).get("sign_consistent")
    verdicts = []
    if accel_t is None:
        verdicts.append("无法计算 t。")
    else:
        if abs(accel_t) < 2.0:
            verdicts.append("✗ t 未达 2.0，不显著。")
        elif accel_med is not None and accel_med < 0:
            verdicts.append("✗ 均值显著但中位数为负——收益被少数极端样本拉动，"
                            "典型板块拿不到这个收益，实盘不可用。")
        elif not accel_seg:
            verdicts.append("✗ 分段符号不一致，信号在不同时段方向会翻转，不稳定。")
        else:
            verdicts.append("△ 统计上边缘显著且方向稳定，但幅度极小（约 "
                            f"{_stats(excess([s for s in samples if s['quad']=='accel'],5))['mean']:.2f}%/5日），"
                            "扣除交易成本后很可能归零。")
    verdicts.append("结论：不应把四象限信号当作可交易的 alpha。它的价值在于**描述当前资金分布**，"
                    "而不是预测未来收益。")
    for v in verdicts:
        print("  " + v)

    out = os.path.join(BASE, "robustness_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"verdicts": verdicts, "accel_t": accel_t,
                   "accel_median": accel_med, "segment_consistent": accel_seg,
                   "random_false_positive_rate": n_sig / trials},
                  f, ensure_ascii=False, indent=1)
    print(f"\n· 报告已保存 {out}")


if __name__ == "__main__":
    main()
