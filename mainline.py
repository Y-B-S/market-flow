"""资金主线识别：自动发现「今天钱集中砸向哪条产业链」。

## 为什么需要

工具原来只给一堆行业榜单。人要自己看出「前12名有8个是科技链」，
才能意识到这是产业链级别的资金流入，而非零散行情。这一步不该由人做。

## 核心方法：资金流同步性聚类

**关键痛点**：产业链图谱接口拿不到（实测 5+ 种 reportName 全失败）。
退而求其次用「价格相关性」也不行 —— 实测大盘普涨时
贵金属 vs 电子 r=+0.72、有色 vs 电子 r=+0.89，
全是伪相关（共同趋势导致）。

**解法**：用**资金流的同步性**（而非价格）。实测区分度：
    同链 电子-半导体 r=+0.956、半导体-数字芯片 +0.914、电子-通信 +0.898
    异链 电子-黄金  r=+0.519、电子-铜 +0.453、电子-银行 -0.137
同链普遍 >0.85，异链普遍 <0.55，分界清晰。

算法不需要人工维护产业链名单：从当日最大流入板块出发，
用历史资金流相关性找出所有 r>=阈值 的板块，即为该主线成员。

## 诚实的边界

这不是真实的产业链关系，而是「资金同步性聚类」。
它能识别「谁和谁一起被买」，但不能证明因果。
板块名称按东财分类，与申万分类不同。
"""
from __future__ import annotations

import json
import math
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# 聚类阈值：实测同链 >0.85、异链 <0.55
R_THRESHOLD = 0.85
# 最小样本：少于这个天数的相关性不可信
MIN_SAMPLES = 8
# 主线至少要这么多成员，否则不算「产业链级别」
MIN_MEMBERS = 3
# 主线合计净流入至少要占全市场这个比例
MIN_SHARE = 0.30


def _build_series(data_dir=None):
    """从历史快照构建 {板块名: {日期: 主力净额}}。"""
    d = data_dir or DATA
    series, dates = {}, set()
    if not os.path.isdir(d):
        return {}, []
    for fn in sorted(os.listdir(d)):
        if not (fn.endswith(".json") and fn[:4].isdigit()):
            continue
        try:
            snap = json.load(open(os.path.join(d, fn), encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        dt = snap.get("as_of")
        if not dt:
            continue
        dates.add(dt)
        for x in (snap.get("industries") or []):
            nm = x.get("name")
            v = x.get("main_net")
            if nm and v is not None:
                series.setdefault(nm, {})[dt] = v
    return series, sorted(dates)


def _corr(series, a, b):
    """两条资金流序列的皮尔逊相关系数。"""
    if a not in series or b not in series:
        return None
    ds = sorted(set(series[a]) & set(series[b]))
    if len(ds) < MIN_SAMPLES:
        return None
    x = [series[a][d] for d in ds]
    y = [series[b][d] for d in ds]
    mx, my = statistics.mean(x), statistics.mean(y)
    num = sum((i - mx) * (j - my) for i, j in zip(x, y))
    dx = math.sqrt(sum((i - mx) ** 2 for i in x))
    dy = math.sqrt(sum((j - my) ** 2 for j in y))
    if not (dx and dy):
        return None
    return num / (dx * dy)


def _nm(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def detect(analysis, snapshot=None, data_dir=None):
    """识别当日资金主线。

    返回：
      { "available": bool,
        "lines": [ {name, lead, members:[...], net_yi, share, avg_r,
                    divergence:[...], note} ],
        "limitations": [...] }
    """
    lims = []
    rows = analysis.get("industries_ranked") or []
    if not rows:
        return {"available": False, "lines": [],
                "limitations": ["无行业数据，无法识别资金主线"]}

    b = analysis.get("breadth_block") or {}
    total_net = _nm(b.get("main_net_total"))
    if total_net is None:
        total_net = sum((_nm(x.get("main_net")) or 0) for x in rows)
    # 只算净流入部分作分母更合理（钱只可能来自流入的方向）
    inflow_total = sum(max(0, _nm(x.get("main_net")) or 0) for x in rows)

    series, dates = _build_series(data_dir)
    if not series:
        lims.append("无历史资金流序列，主线识别退化为「按当日净额排序」")
        # 退化：仅按当日净额，不聚类
        top = [x for x in rows if (_nm(x.get("main_net")) or 0) > 0][:10]
        if not top:
            return {"available": False, "lines": [], "limitations": lims}
        share = sum(_nm(x.get("main_net")) or 0 for x in top) / inflow_total if inflow_total else 0
        return {
            "available": True,
            "lines": [{
                "name": "（无历史数据，仅按当日流入排序）",
                "lead": top[0].get("name"),
                "members": [{"name": x.get("name"),
                             "net_yi": round((_nm(x.get("main_net")) or 0) / 1e8, 1),
                             "r": None} for x in top],
                "net_yi": round(sum(_nm(x.get("main_net")) or 0 for x in top) / 1e8, 1),
                "share": round(share * 100, 1),
                "avg_r": None,
                "divergence": [],
                "note": "缺少历史序列，未做资金同步性聚类",
            }],
            "limitations": lims,
        }

    # ── 找种子：当日净流入最大、且有足够历史的板块 ──
    cand = [x for x in rows
            if (_nm(x.get("main_net")) or 0) > 0
            and (x.get("name") in series)
            and len(series.get(x.get("name"), {})) >= MIN_SAMPLES]
    cand.sort(key=lambda x: -(_nm(x.get("main_net")) or 0))
    if not cand:
        return {"available": False, "lines": [],
                "limitations": lims + ["无板块同时满足「今日净流入」与「历史样本充足」"]}

    # 已归属的板块，避免同一批被反复识别成多条主线
    claimed = set()
    lines = []
    div_all = {(x.get("name")): x for x in (analysis.get("divergence_gated") or [])}

    for seed in cand[:24]:
        sname = seed.get("name")
        if sname in claimed:
            continue
        seed_net = _nm(seed.get("main_net")) or 0
        if seed_net <= 0:
            continue

        members = []
        for x in rows:
            nm = x.get("name")
            if nm in claimed or nm not in series:
                continue
            net = _nm(x.get("main_net")) or 0
            if nm == sname:
                members.append({"name": nm, "net_yi": round(net / 1e8, 1), "r": 1.0,
                                "seed": True})
                continue
            r = _corr(series, sname, nm)
            if r is not None and r >= R_THRESHOLD:
                members.append({"name": nm, "net_yi": round(net / 1e8, 1),
                                "r": round(r, 3), "seed": False})

        if len(members) < MIN_MEMBERS:
            continue
        grp_net = sum(_nm(x.get("main_net")) or 0 for x in rows
                      if x.get("name") in {m["name"] for m in members})
        share = (grp_net / inflow_total * 100) if inflow_total else 0
        if share < MIN_SHARE * 100:
            continue

        rs = [m["r"] for m in members if not m.get("seed") and m["r"] is not None]
        avg_r = round(statistics.mean(rs), 3) if rs else None

        # ── 主线性质判定：是"真流入"还是"逆势回补"？──
        #
        # 这一步是整套逻辑的结论所在。只看「今日合计净流入」会得出
        # "钱在砸向这条链"的乐观结论；但若成员的 5日/10日累计几乎全为负，
        # 真实含义是：**这条链已连续失血多日，今天是单日回补**。
        # 两种情形对投资判断完全相反，必须区分。
        row_by_name = {x.get("name"): x for x in rows}
        d5_pos = d10_pos = 0
        divs = []
        for m in members:
            r0 = row_by_name.get(m["name"]) or {}
            d5 = _nm(r0.get("main_net_5d"))
            d10 = _nm(r0.get("main_net_10d"))
            if d5 is not None and d5 > 0:
                d5_pos += 1
            if d10 is not None and d10 > 0:
                d10_pos += 1
            if d10 is not None and d10 < 0:
                divs.append({
                    "name": m["name"],
                    "today_yi": m["net_yi"],
                    "d5_yi": round((d5 or 0) / 1e8, 1) if d5 is not None else None,
                    "d10_yi": round(d10 / 1e8, 1),
                    "severity": _nm((div_all.get(m["name"]) or {}).get("severity")),
                })
        n_mem = len(members)
        # 过半成员中期为负 -> 定性为逆势回补
        if n_mem and d10_pos <= n_mem * 0.34:
            nature = "rebound"
            nature_txt = "逆势回补"
        elif n_mem and d10_pos >= n_mem * 0.66:
            nature = "genuine"
            nature_txt = "持续流入"
        else:
            nature = "mixed"
            nature_txt = "多空混杂"
        divs.sort(key=lambda x: -(x.get("severity") or 0))

        members.sort(key=lambda m: -m["net_yi"])
        lines.append({
            "name": f"{sname}链",
            "lead": sname,
            "members": members,
            "net_yi": round(grp_net / 1e8, 1),
            "share": round(share, 1),
            "avg_r": avg_r,
            "member_count": len(members),
            "divergence": divs,
            "nature": nature,
            "nature_txt": nature_txt,
            "d10_pos_count": d10_pos,
            "note": None,
        })
        for m in members:
            claimed.add(m["name"])

    lines.sort(key=lambda x: -x["net_yi"])

    # 主线命名：用成员里出现最多的「大类」词，否则用领头板块
    for ln in lines:
        lead = ln["lead"]
        ln["name"] = f"以「{lead}」为首的资金主线"

    if not lines:
        lims.append(
            f"未发现满足条件的资金主线（需 ≥{MIN_MEMBERS} 个同步板块"
            f"且合计流入占比 ≥{int(MIN_SHARE*100)}%）")

    lims.append(
        "主线由「资金流同步性」聚类得出（阈值 r≥0.85），"
        "不是真实产业链图谱——它识别「谁和谁一起被买」，不证明因果")
    if len(dates) < 20:
        lims.append(f"仅 {len(dates)} 个交易日样本，聚类稳定性有限")

    return {
        "available": bool(lines),
        "lines": lines,
        "sample_days": len(dates),
        "threshold": R_THRESHOLD,
        "limitations": lims,
    }


def plain_summary(result):
    """给非专业读者的总结。

    关键：不能只说「钱砸向某条链」，必须说清是**持续买入**还是**单日回补**。
    这两种情形对判断的意义完全相反：
      genuine  → 资金确实在建仓，趋势可能延续
      rebound  → 这条链已经失血多日，今天只是回补，追高风险大
    """
    if not result.get("available"):
        return None
    lines = result.get("lines") or []
    if not lines:
        return None
    top = lines[0]
    members = top.get("members") or []
    n = len(members)
    leads = "、".join(m["name"] for m in members[:3])
    nature = top.get("nature")
    share = top.get("share") or 0
    net = top.get("net_yi") or 0

    if nature == "rebound":
        d10_pos = top.get("d10_pos_count") or 0
        txt = (f"今天钱看起来在猛砸一条产业链——以「{top['lead']}」为核心，"
               f"{n} 个相关板块同步流入 {net:+.0f} 亿，占全市场流入的 {share:.0f}%，"
               f"包括 {leads} 等。")
        txt += (f"\n\n但别高兴太早：这 {n} 个板块里只有 {d10_pos} 个"
                f"近 10 日是净流入的，其余都在失血。"
                f"也就是说，这条链已经连续流出多日，今天更像是「单日回补」，"
                f"不是资金在建仓。")
        dv = top.get("divergence") or []
        if dv:
            d0 = dv[0]
            txt += (f"\n\n最典型的是「{d0['name']}」：今日 {d0['today_yi']:+.0f} 亿，"
                    f"10 日累计 {d0['d10_yi']:+.0f} 亿。"
                    f"这种「今日进、中期出」的形态，追高风险很大。")
    elif nature == "genuine":
        d10_pos = top.get("d10_pos_count") or 0
        txt = (f"今天钱在持续买一条产业链——以「{top['lead']}」为核心，"
               f"{n} 个相关板块同步流入 {net:+.0f} 亿，占全市场流入的 {share:.0f}%，"
               f"包括 {leads} 等。")
        txt += (f"\n\n这 {n} 个板块里有 {d10_pos} 个近 10 日也是净流入，"
                f"说明不是一次性动作，资金在持续建仓。")
    else:
        txt = (f"今天资金集中在「{top['lead']}」周边：{n} 个板块同步流入 "
               f"{net:+.0f} 亿，占全市场流入的 {share:.0f}%。")
        txt += "\n\n但这些板块近 10 日的表现参差不齐，方向还不够一致，需要再观察几天。"
    return txt


if __name__ == "__main__":
    import sys
    sys.path.insert(0, HERE)
    import metrics
    snap = None
    for fn in sorted(os.listdir(DATA))[::-1]:
        if fn.endswith(".json") and fn[:4].isdigit():
            try:
                cand = json.load(open(os.path.join(DATA, fn), encoding="utf-8"))
                if (cand.get("breadth") or {}).get("amount_total"):
                    snap = cand
                    break
            except Exception:  # noqa: BLE001
                continue
    if not snap:
        raise SystemExit("无可用快照")
    a = metrics.compute(snap)
    r = detect(a, snap)
    print(f"交易日: {snap['as_of']}   样本 {r.get('sample_days')} 天")
    print(f"发现主线: {len(r['lines'])} 条\n")
    for ln in r["lines"]:
        print(f"■ {ln['name']}  合计 {ln['net_yi']:+.0f}亿  占 {ln['share']:.0f}%  "
              f"({ln['member_count']} 个板块, 平均r={ln['avg_r']})")
        print(f"  性质: {ln.get('nature_txt')}  "
              f"（{ln.get('d10_pos_count')}/{ln['member_count']} 个板块 10日为正）")
        for m in ln["members"]:
            mark = "★" if m.get("seed") else " "
            rt = f"r={m['r']:+.3f}" if m.get("r") is not None else "      "
            print(f"  {mark} {m['name']:<18s} {m['net_yi']:+8.1f}亿  {rt}")
        if ln["divergence"]:
            print(f"    ⚠ 组内背离: {len(ln['divergence'])} 个")
            for dv in ln["divergence"][:3]:
                print(f"      {dv['name']}: 今 {dv['today_yi']:+.1f}亿 / 10日 {dv['d10_yi']:+.1f}亿")
        print()
    if r["limitations"]:
        print("限制:")
        for l in r["limitations"]:
            print(f"  · {l}")
    print()
    ps = plain_summary(r)
    if ps:
        print("=== 大白话 ===")
        print(ps)
