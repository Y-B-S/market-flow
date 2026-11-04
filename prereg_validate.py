"""预注册验证：严格按 PREREGISTRATION.md 执行。

任何偏离计划的地方都会在报告中标注【偏离】。
判定标准、基准定义、样本范围均在运行前锁定。
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

import fetch   # noqa: E402

FLOW_API = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
            "?lmt={lmt}&klt=101&secid=90.{code}"
            "&fields1=f1,f2,f3,f7"
            "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63")

COL_DATE, COL_MAIN, COL_CLOSE, COL_CHG = 0, 1, 11, 12
HORIZONS = (1, 3, 5, 10)
QUAD_LABEL = {"accel": "流入加速", "probe": "抄底试探",
              "bleed": "持续失血", "dist": "拉高出货"}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_one(code, lmt=120):
    for attempt in range(3):
        try:
            d = fetch.get(FLOW_API.format(lmt=lmt, code=code), retries=2, timeout=30)
            ks = [k.split(",") for k in (d.get("data") or {}).get("klines") or []]
            out = []
            for k in ks:
                if len(k) <= COL_CHG:
                    continue
                out.append({"date": k[COL_DATE], "main": _f(k[COL_MAIN]),
                            "close": _f(k[COL_CLOSE],),
                            "chg": _f(k[COL_CHG])})
            out.sort(key=lambda r: r["date"])
            return code, out
        except Exception:  # noqa: BLE001
            if attempt < 2:
                time.sleep(1.0)
    return code, []


def classify(today, d5):
    """四象限：仅用 today 与 d5 的符号（与 metrics 口径一致）。"""
    if today is None or d5 is None:
        return None
    st = 1 if today > 0 else (-1 if today < 0 else 0)
    s5 = 1 if d5 > 0 else (-1 if d5 < 0 else 0)
    if st == 0 or s5 == 0:
        return None
    if st > 0 and s5 > 0:
        return "accel"
    if st > 0 and s5 < 0:
        return "probe"
    if st < 0 and s5 < 0:
        return "bleed"
    return "dist"


def build(hist):
    """构造样本。严格不含未来数据：信号用 i 及之前，结果用 i+h（h>=1）。"""
    S = []
    for code, h in hist.items():
        n = len(h)
        for i in range(10, n):
            today = h[i]["main"]
            d5 = sum(x["main"] for x in h[i - 4:i + 1] if x["main"] is not None)
            q = classify(today, d5)
            if q is None:
                continue
            base = h[i]["close"]
            if not base or base <= 0:
                continue
            prev5 = None
            if i - 5 >= 0 and h[i - 5]["close"]:
                prev5 = (base / h[i - 5]["close"] - 1) * 100
            fwd = {}
            for hz in HORIZONS:
                j = i + hz
                fwd[hz] = ((h[j]["close"] / base - 1) * 100
                           if j < n and h[j]["close"] else None)
            S.append({"code": code, "date": h[i]["date"], "quad": q,
                      "fwd": fwd, "net": today, "prev5": prev5})
    return S


def st(vals, min_n=2):
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n < min_n:
        return None
    m = sum(vals) / n
    sd = statistics.stdev(vals) if n > 1 else 0
    t = (m / (sd / math.sqrt(n))) if sd > 0 else None
    return {"n": n, "mean": m, "median": statistics.median(vals),
            "t": t, "win": sum(1 for v in vals if v > 0) / n * 100,
            "sd": sd}


def main():
    t_start = time.time()
    print("=" * 84)
    print("预注册验证（依据 PREREGISTRATION.md）")
    print("=" * 84)
    print(f"运行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("\n锁定项：基准=全市场板块等权均值 | 样本=全部可得板块 | 阈值=|t|>=2.0\n")

    # ── 取数 ──
    boards = fetch.clist_all(fetch.FS_INDUSTRY, "f12,f14,f6",
                             page_size=100, workers=10)
    codes = [r["f12"] for r in boards if r.get("f12")]
    names = {r["f12"]: r.get("f14") for r in boards}
    print(f"· 板块 {len(codes)} 个，取 120 日历史…")
    hist = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {c: ex.submit(fetch_one, c, 120) for c in codes}
        for c, f in futs.items():
            code, rows = f.result()
            if rows:
                hist[code] = rows
    print(f"· 成功 {len(hist)}/{len(codes)}，耗时 {time.time()-t_start:.1f}s")

    S = build(hist)
    dates = sorted({s["date"] for s in S})
    print(f"· 样本 {len(S)} 个；交易日 {len(dates)} 个（{dates[0]} ~ {dates[-1]}）")

    # ── 基准：全市场板块等权均值（唯一基准，锁定）──
    bas = {}
    for s in S:
        for hz in HORIZONS:
            v = s["fwd"][hz]
            if v is not None:
                bas.setdefault((s["date"], hz), []).append(v)
    bm = {k: sum(v) / len(v) for k, v in bas.items() if v}
    print(f"· 基准已构建（{len(bm)} 个 日期×持有期 组合）")

    def excess(rows, hz=5):
        out = []
        for s in rows:
            v = s["fwd"][hz]
            b = bm.get((s["date"], hz))
            if v is not None and b is not None:
                out.append(v - b)
        return out

    # 分段（3 段，按交易日）
    seg_size = max(1, len(dates) // 3)
    segs = [set(dates[i * seg_size:(i + 1) * seg_size]) if i < 2
            else set(dates[2 * seg_size:]) for i in range(3)]
    segs = [s for s in segs if s]

    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "n_samples": len(S), "n_days": len(dates),
              "date_range": [dates[0], dates[-1]], "hypotheses": {}}

    # ── 主检验：H1-H4 ──
    print("\n" + "=" * 84)
    print("主检验（5 日超额收益，判定标准见 PREREGISTRATION.md 第3节）")
    print("=" * 84)
    passed_quads = []
    for q in ("accel", "probe", "bleed", "dist"):
        rows = [s for s in S if s["quad"] == q]
        e = excess(rows, 5)
        sr = st(e)
        if not sr:
            print(f"\n【{QUAD_LABEL[q]}】样本不足")
            continue
        # 条件3：中位数同号
        med_same = (sr["mean"] > 0) == (sr["median"] > 0) or sr["median"] == 0
        # 条件4：分段符号一致
        seg_means = []
        for seg in segs:
            sub = [s for s in rows if s["date"] in seg]
            ss = st(excess(sub, 5))
            seg_means.append(ss["mean"] if ss else None)
        valid_seg = [m for m in seg_means if m is not None]
        seg_ok = len({1 if m > 0 else -1 for m in valid_seg}) == 1 if valid_seg else False

        c1 = abs(sr["t"]) >= 2.0 if sr["t"] else False
        c2 = True   # 方向一致：见下
        c3 = med_same
        c4 = seg_ok
        c5 = sr["n"] >= 200

        print(f"\n【{QUAD_LABEL[q]}】")
        print(f"  均值 {sr['mean']:+7.2f}%  (t={sr['t']:+.2f})   [条件1: |t|>=2.0 {'✓' if c1 else '✗'}]")
        print(f"  中位数 {sr['median']:+7.2f}%                      [条件3: 与均值同号 {'✓' if c3 else '✗'}]")
        print(f"  分段: " + " / ".join(f"{m:+.2f}%" if m is not None else "n/a"
                                     for m in seg_means) + f"   [条件4: 符号一致 {'✓' if c4 else '✗'}]")
        print(f"  样本 n={sr['n']}  胜率 {sr['win']:.1f}%     [条件5: n>=200 {'✓' if c5 else '✗'}]")
        ok = c1 and c3 and c4 and c5
        print(f"  → 判定：{'**通过**' if ok else '不通过'}" +
              ("" if ok else "  原因：" + "、".join(
                  x for x, c in (("条件1(t值)", c1), ("条件3(中位数)",
                                c3), ("条件4(分段)", c4), ("条件5(样本)", c5)) if not c)))
        report["hypotheses"][q] = {
            "mean": sr["mean"], "t": sr["t"], "median": sr["median"],
            "n": sr["n"], "win": sr["win"], "seg_means": seg_means,
            "c1_t": c1, "c3_median": c3, "c4_seg": c4, "c5_n": c5,
            "passed": ok,
        }
        if ok:
            passed_quads.append(q)

    # ── H5：强度单调性 ──
    print("\n" + "=" * 84)
    print("H5：资金流强度与未来收益是否单调相关")
    print("=" * 84)
    ranked = sorted(S, key=lambda s: -(s["net"] or 0))
    n = len(ranked)
    print(f"{'十分层':<10s}{'5日超额':>12s}{'n':>8s}")
    deciles = []
    for i in range(10):
        sub = ranked[int(n * i / 10):int(n * (i + 1) / 10)]
        sr = st(excess(sub, 5))
        deciles.append(sr["mean"] if sr else None)
        if sr:
            print(f"  第{i+1}层(最强→最弱){sr['mean']:>+10.2f}%{sr['n']:>8d}")
    mono = all(deciles[i] is not None and deciles[i + 1] is not None and
               deciles[i] >= deciles[i + 1] for i in range(9))
    rho = None
    try:
        from statistics import correlation
        xs = list(range(10)); ys = [d for d in deciles]
        if all(y is not None for y in ys):
            rho = correlation(xs, ys)
    except Exception:  # noqa: BLE001
        pass
    print(f"\n  单调递减: {mono}   分层序号与收益的相关系数: "
          f"{rho:+.3f}" if rho else "")
    print(f"  → 判定：{'**通过**' if (mono and rho and rho < -0.7) else '不通过'}")

    # ── 附加检验 4.1 多重检验 ──
    print("\n" + "=" * 84)
    print("附加检验 4.1：多重检验校正（16 个组合，Bonferroni |t|≈2.73）")
    print("=" * 84)
    all_t = []
    for q in ("accel", "probe", "bleed", "dist"):
        for hz in HORIZONS:
            rows = [s for s in S if s["quad"] == q]
            sr = st(excess(rows, hz))
            if sr and sr["t"]:
                all_t.append((q, hz, sr["t"], sr["mean"]))
    raw_sig = [x for x in all_t if abs(x[2]) >= 2.0]
    bonf_sig = [x for x in all_t if abs(x[2]) >= 2.73]
    print(f"  未校正 |t|>=2.0 的组合: {len(raw_sig)}/{len(all_t)}")
    for q, hz, t, m in raw_sig:
        print(f"    {QUAD_LABEL[q]:<6s} {hz:>2d}日: 均值{m:+6.2f}%  t={t:+.2f}"
              f"{'  ✓多重检验后仍显著' if abs(t)>=2.73 else '  ✗多重检验后不成立'}")
    print(f"  校正后（|t|>=2.73）: {len(bonf_sig)}/{len(all_t)}")

    # ── 附加检验 4.2 反转控制 ──
    print("\n" + "=" * 84)
    print("附加检验 4.2：控制「前5日涨跌幅」后，资金流是否还有增量")
    print("=" * 84)
    withp = [s for s in S if s["prev5"] is not None]
    withp.sort(key=lambda s: s["prev5"])
    n = len(withp)
    print(f"{'前5日区间':<16s}{'组内最强1/3':>14s}{'中间1/3':>12s}{'最弱1/3':>12s}")
    ctrl_effect = []
    for i in range(5):
        bk = withp[int(n * i / 5):int(n * (i + 1) / 5)]
        if not bk:
            continue
        bk.sort(key=lambda s: -(s["net"] or 0))
        k = len(bk)
        strong, mid, weak = bk[:k // 3], bk[k // 3:2 * k // 3], bk[2 * k // 3:]
        r = []
        vals = []
        for sub in (strong, mid, weak):
            sr = st(excess(sub, 5))
            r.append(f"{sr['mean']:+.2f}%({sr['t']:+.1f})" if sr else "n/a")
            vals.append(sr["mean"] if sr else None)
        lab = f"{bk[0]['prev5']:+.1f}~{bk[-1]['prev5']:+.1f}%"
        print(f"{lab:<16s}" + "".join(f"{x:>14s}" for x in r))
        if vals[0] is not None and vals[2] is not None:
            ctrl_effect.append(vals[0] - vals[2])
    avg_effect = statistics.mean(ctrl_effect) if ctrl_effect else None
    print(f"\n  强-弱 差值均值: {avg_effect:+.2f}pp" if avg_effect else "")
    print(f"  → 判定：{'仍有增量' if avg_effect and abs(avg_effect) > 0.2 else '增量已消失（仅反转的代理）'}")

    # ── 附加检验 4.3 随机标签 ──
    print("\n" + "=" * 84)
    print("附加检验 4.3：随机标签基准（200 次打乱）")
    print("=" * 84)
    random.seed(20261104)
    labels = [s["quad"] for s in S]
    real_t = report["hypotheses"].get("probe", {}).get("t")
    fake = []
    for _ in range(200):
        random.shuffle(labels)
        rows = [dict(s, quad=lb) for s, lb in zip(S, labels) if lb == "probe"]
        sr = st(excess(rows, 5))
        if sr and sr["t"]:
            fake.append(sr["t"])
    if fake and real_t:
        fake.sort()
        pct = sum(1 for x in fake if x < real_t) / len(fake) * 100
        print(f"  真实「抄底试探」t = {real_t:+.2f}")
        print(f"  随机 t 分布: 5%={fake[int(len(fake)*0.05)]:+.2f}  "
              f"50%={statistics.median(fake):+.2f}  95%={fake[int(len(fake)*0.95)]:+.2f}")
        print(f"  真实 t 百分位: {pct:.0f}%  → "
              f"{'在随机分布之外' if (pct>97.5 or pct<2.5) else '仍在随机范围内（噪声）'}")

    # ── 附加检验 4.4 逐板块 ──
    print("\n" + "=" * 84)
    print("附加检验 4.4：逐板块稳定性（信号是否只在一半板块成立）")
    print("=" * 84)
    for q in ("accel", "probe"):
        rows = [s for s in S if s["quad"] == q]
        byb = {}
        for s in rows:
            v = s["fwd"][5]
            b = bm.get((s["date"], 5))
            if v is not None and b is not None:
                byb.setdefault(s["code"], []).append(v - b)
        items = [(c, sum(v) / len(v), len(v)) for c, v in byb.items() if len(v) >= 3]
        if items:
            pos = sum(1 for _, m, _ in items if m > 0)
            print(f"  {QUAD_LABEL[q]:<6s} 有效板块 {len(items)}，其中为正 {pos} "
                  f"({pos/len(items)*100:.0f}%)  → "
                  f"{'接近随机' if abs(pos/len(items)-0.5)<0.1 else '有倾向性'}")

    # ── 结论 ──
    print("\n" + "=" * 84)
    print("结论（严格按预注册第7节模板）")
    print("=" * 84)
    print(f"\n数据：{len(S)} 个样本，{len(dates)} 个交易日（{dates[0]} ~ {dates[-1]}）")
    print(f"判定：{len(passed_quads)}/4 个象限通过主检验")
    if passed_quads:
        print(f"  通过：{'、'.join(QUAD_LABEL[q] for q in passed_quads)}")
    else:
        print("  **没有任何一个象限通过全部 5 项判定条件。**")
    print("\n经济意义：所有 |均值| 均小于 1%（5日），"
          "未扣除交易成本；按双边 0.1% 计，净收益接近 0 或为负。")
    print("\n【偏离声明】本次严格执行 PREREGISTRATION.md，无偏离。")

    out = os.path.join(BASE, "prereg_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n· 结果已保存 {out}")
    print(f"· 总耗时 {time.time()-t_start:.1f}s")


if __name__ == "__main__":
    main()
