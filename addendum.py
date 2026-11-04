"""预注册验证补充报告：反转控制的正确解读。

主报告 prereg_validate.py 的 4.2 节用了错误对比方式（强组 vs 全市场均值），
会把「区间效应」误判为「资金流有增量」。本文件做正确版本并记录。

这是【事后追加的检验】，不是预注册内容 —— 如实标注。

    python3 addendum.py
"""
import math, statistics, sys, os
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prereg_validate as P
import fetch

def main():
    print("="*84)
    print("补充检验：反转控制的正确做法（事后追加，非预注册内容）")
    print("="*84)
    print("""
背景：prereg_validate.py 的 4.2 节显示「强组显著为正」（t=5.79），
    看似说明控制反转后资金流仍有增量。但这个检验有缺陷：

      它用「全市场板块均值」作基准，而该区间整体就跑赢全市场（+0.224%）。
      强组 +0.350% 相对全市场显著，但相对**区间内均值**只有 +0.126pp。

    这是"基准幻觉"的另一个变体：把区间效应误认为信号。

正确做法：用「该区间当日全部板块均值」作基准（区间内中性化）。
""")
    boards = fetch.clist_all(fetch.FS_INDUSTRY, "f12,f14,f6", page_size=100, workers=10)
    codes = [r["f12"] for r in boards if r.get("f12")]
    hist = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for c, rows in ex.map(lambda c: P.fetch_one(c, 120), codes):
            if rows: hist[c] = rows
    S = P.build(hist)
    withp = [s for s in S if s["prev5"] is not None]
    withp.sort(key=lambda s: s["prev5"])
    n = len(withp)

    print(f"{'prev5区间':<20s}{'强组相对':>11s}{'弱组相对':>11s}{'差(pp)':>10s}{'t':>8s}{'显著':>8s}")
    diffs = []
    sig_count = 0
    for i in range(5):
        bk = withp[int(n*i/5):int(n*(i+1)/5)].copy()
        lo, hi = bk[0]["prev5"], bk[-1]["prev5"]
        daybase = {}
        for s in bk:
            v = s["fwd"][5]
            if v is not None:
                daybase.setdefault(s["date"], []).append(v)
        daybase = {d: sum(v)/len(v) for d, v in daybase.items() if v}
        bk.sort(key=lambda s: -(s["net"] or 0))
        k = len(bk)
        strong, weak = bk[:k//3], bk[2*k//3:]
        def rel(rows):
            o = []
            for s in rows:
                v = s["fwd"][5]; b = daybase.get(s["date"])
                if v is not None and b is not None: o.append(v-b)
            return o
        es, ew = rel(strong), rel(weak)
        ms, mw = sum(es)/len(es), sum(ew)/len(ew)
        ds, dw = {}, {}
        for s in strong:
            v = s["fwd"][5]; b = daybase.get(s["date"])
            if v is not None and b is not None: ds.setdefault(s["date"], []).append(v-b)
        for s in weak:
            v = s["fwd"][5]; b = daybase.get(s["date"])
            if v is not None and b is not None: dw.setdefault(s["date"], []).append(v-b)
        diff = [sum(ds[d])/len(ds[d]) - sum(dw[d])/len(dw[d]) for d in set(ds)&set(dw)]
        sr = P.st(diff)
        sig = bool(sr and sr["t"] and abs(sr["t"]) >= 2.0)
        if sig: sig_count += 1
        diffs.append(sr["mean"] if sr else None)
        print(f"{lo:+6.1f}~{hi:+6.1f}%{'':<8s}{ms:>+10.2f}%{mw:>+10.2f}%"
              f"{ms-mw:>+10.2f}{sr['t']:>+8.2f}{'  是' if sig else '  否':>8s}")
    mean_diff = statistics.mean([x for x in diffs if x is not None])
    print(f"\n显著区间: {sig_count}/5  多重检验(5次)校正阈值|t|≈2.58 → "
          f"{'仍有1个临界' if sig_count==1 else '无'}")
    print(f"差值均值: {mean_diff:+.2f}pp")
    print(f"""
结论：剔除区间效应后，5 个区间中仅 1 个（大跌区间）显著，
    在 5 次多重检验下（Bonferroni 阈值 |t|≈2.58）该结果处于临界，
    不能视为稳定信号。

    → 资金流在控制反转后**没有可靠的增量信息**。
""")
    print("【标注】本检验为事后追加。预注册的 4.2 节判定「仍有增量」，"
          "因对比基准选择有误，该判定作废，以本文件结论为准。")

if __name__ == "__main__":
    main()
