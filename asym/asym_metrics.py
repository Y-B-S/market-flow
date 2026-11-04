"""信息不对称面板 · 计算层。

核心命题：展示的不是内幕，是知情者留下的公开痕迹。
每个面板都必须能回答"这是谁的痕迹、为什么可观测"。

纯函数：无 IO、无网络、无第三方依赖。
"""
from __future__ import annotations

import math
from datetime import datetime

# 变动原因 -> 三档归类（实测 reason 有 9 种写法，逐字映射）
REASON_MAP = {
    "集中竞价": "集中竞价", "竞价交易": "集中竞价", "集中竞价交易": "集中竞价",
    "二级市场买卖": "集中竞价",      # 实质也是二级市场竞价成交
    "大宗交易": "大宗",
    "盘后定价": "其他", "询价转让": "其他",
    "协议转让": "其他", "协议转让过户": "其他",
}

DASH = "—"


def _n(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _fmt_yi(v):
    if v is None:
        return DASH
    if abs(v) >= 1e12:
        return f"{v/1e12:+.2f}万亿"
    if abs(v) >= 1e8:
        return f"{v/1e8:+.2f}亿"
    return f"{v/1e4:+.0f}万"


def _pct_rank(pop, v):
    if v is None or not pop:
        return None
    xs = [x for x in pop if x is not None]
    if not xs:
        return None
    below = sum(1 for x in xs if x < v)
    equal = sum(1 for x in xs if x == v)
    return (below + 0.5 * equal) / len(xs) * 100.0


def _winsorize(vals, lo_pct=0.01, hi_pct=0.99):
    """分位截断（winsorize）：超界值**替换为边界值**，不是删除。

    股东户数比率实测 max=777480.85%（新股上市户数暴增），
    直接求均值/分位会被单点毁掉。截断后保留所有样本点，只压边界。
    返回 (截断后列表, 被截断的原始值列表)。
    """
    xs = [v for v in vals if v is not None]
    if len(xs) < 5:
        return list(xs), []
    s = sorted(xs)
    n = len(s)
    lo = s[max(0, min(n - 1, int(math.floor(lo_pct * (n - 1)))))]
    hi = s[max(0, min(n - 1, int(math.ceil(hi_pct * (n - 1)))))]
    clipped = []
    out = []
    for v in xs:
        if v < lo:
            clipped.append(v); out.append(lo)
        elif v > hi:
            clipped.append(v); out.append(hi)
        else:
            out.append(v)
    return out, clipped


# ── P1 调研热度 ─────────────────────────────────────────────────────────

def _p1_surveys(snap, lim):
    sv = (snap.get("datasets") or {}).get("surveys")
    if not sv or not sv.get("sessions"):
        lim.append("调研数据不可用或为空")
        return {"boards": [], "note": DASH}

    sessions = sv["sessions"]
    single = [s for s in sessions if s.get("granularity") in ("single", "multi")]
    monthly = [s for s in sessions if s.get("granularity") == "monthly"]
    unknown = [s for s in sessions if s.get("granularity") == "unknown"]

    if not single:
        lim.append("无单场粒度调研记录，调研热度不可用")

    single.sort(key=lambda s: -(s.get("inst_count") or 0))
    top = [{
        "name": s.get("name"),
        "code": s.get("code"),
        "notice_date": s.get("notice_date"),
        "receive_start": s.get("receive_start"),
        "inst_count": s.get("inst_count"),
        "way": s.get("way"),
        "receptionist": s.get("receptionist"),
        "place": s.get("place"),
        "granularity": s.get("granularity"),
    } for s in single[:20]]

    # 按公告日的日序列（数据只覆盖少数公告日，如实给出）
    by_day = {}
    for s in single:
        d = s.get("notice_date")
        if not d:
            continue
        g = by_day.setdefault(d, {"date": d, "sessions": 0, "inst_total": 0})
        g["sessions"] += 1
        g["inst_total"] += (s.get("inst_count") or 0)
    day_series = [by_day[d] for d in sorted(by_day, reverse=True)]

    # 月度窗口单列：整月累计，绝不能与单场相加
    monthly_rows = [{
        "name": s.get("name"),
        "receive_start": s.get("receive_start"),
        "receive_end": s.get("receive_end"),
        "inst_count": s.get("inst_count"),
    } for s in sorted(monthly, key=lambda s: -(s.get("inst_count") or 0))[:10]]

    counts = sv.get("granularity_counts") or {}
    notice_window = ((snap.get("meta") or {}).get("surveys") or {}).get("notice_days_covered")

    note = ("调研 ≠ 利好。它只证明「有机构获得了与公司直接沟通的机会」，"
            "不证明公司基本面变化，也不代表机构会买入。")
    if notice_window and notice_window <= 5:
        lim.append(f"调研数据仅覆盖 {notice_window} 个公告日，不足以做中长期对比")

    return {
        "boards": top,
        "top": top,
        "rows": top,
        "day_series": day_series,
        "monthly": monthly_rows,
        "counts": {
            "total_sessions": sv.get("session_count"),
            "single": counts.get("single", len(single)),
            "monthly": counts.get("monthly", len(monthly)),
            "unknown": counts.get("unknown", len(unknown)),
            "raw_rows": sv.get("raw_row_count"),
        },
        "notice_days_covered": notice_window,
        "note": note,
    }


# ── P2 内部人投票 ──────────────────────────────────────────────────────

def _p2_insider(snap, lim):
    ins = (snap.get("datasets") or {}).get("insider")
    if not ins or not ins.get("records"):
        lim.append("高管持股变动数据不可用")
        return {"boards": [], "note": DASH}

    recs = ins["records"]
    inc, dec, unknown_dir = [], [], 0
    amt_missing = 0
    by_reason = {}
    for r in recs:
        d = r.get("dir")
        amt = _n(r.get("amount"))
        if amt is None:
            amt_missing += 1
        if d == "增持":
            inc.append(r)
        elif d == "减持":
            dec.append(r)
        else:
            unknown_dir += 1
        cat = REASON_MAP.get((r.get("reason") or "").strip(), "其他")
        g = by_reason.setdefault(cat, {"count": 0, "amount": 0.0, "has_amount": 0})
        g["count"] += 1
        if amt is not None:
            g["amount"] += amt
            g["has_amount"] += 1

    if amt_missing:
        lim.append(f"高管变动中 {amt_missing} 条金额缺失，未按 0 计入")

    # ⚠️ 符号一致：增持为正、减持为负（实测确认）。
    # 净额是**直接求和**，不是"增持额-减持额"——后者会把负值再减一次得到荒谬结果。
    net_amount = sum(_n(r.get("amount")) or 0 for r in recs if _n(r.get("amount")) is not None)

    top_inc = sorted([r for r in inc if _n(r.get("amount")) is not None],
                     key=lambda r: -(_n(r.get("amount")) or 0))[:15]
    top_dec = sorted([r for r in dec if _n(r.get("amount")) is not None],
                     key=lambda r: (_n(r.get("amount")) or 0))[:15]

    def _rows(src):
        return [{
            "name": r.get("name"), "code": r.get("code"), "person": r.get("person"),
            "date": r.get("date"), "amount": _n(r.get("amount")),
            "shares": _n(r.get("shares")), "reason": r.get("reason"),
            "dir": r.get("dir"),
        } for r in src]

    return {
        "boards": _rows(top_inc) or _rows(top_dec),
        "top_in": _rows(top_inc),
        "top_out": _rows(top_dec),
        "summary": {
            "inc_count": len(inc), "dec_count": len(dec),
            "unknown_dir": unknown_dir,
            "inc_amount": sum(_n(r.get("amount")) or 0 for r in inc if _n(r.get("amount")) is not None),
            "dec_amount": sum(_n(r.get("amount")) or 0 for r in dec if _n(r.get("amount")) is not None),
            "net_amount": net_amount,
            "net_sign": ("净增持" if net_amount > 0 else ("净减持" if net_amount < 0 else "持平")),
        },
        "by_reason": by_reason,
        "note": ("内部人用自有资金投票。但减持可能是个人资金需求、税务安排或激励行权，"
                 "不等于看空；增持也可能有市值管理动机。这是**行为痕迹**，不是方向判断。"),
    }


# ── P3 筹码集中度 ──────────────────────────────────────────────────────

def _p3_holders(snap, lim):
    hd = (snap.get("datasets") or {}).get("holders")
    if not hd or not hd.get("records"):
        lim.append("股东户数数据不可用")
        return {"boards": [], "note": DASH}

    recs = hd["records"]
    raw_ratio = [_n(r.get("ratio")) for r in recs if _n(r.get("ratio")) is not None]
    if not raw_ratio:
        lim.append("股东户数比率全缺失")
        return {"boards": [], "note": DASH}

    win, clipped = _winsorize(raw_ratio, 0.01, 0.99)
    if clipped:
        lim.append(f"股东户数比率有 {len(clipped)} 条极端值（|比率|>99分位）已截断，"
                   f"未静默丢弃；最大原始值 {max(clipped):.0f}%")

    down = [r for r in recs if (_n(r.get("ratio")) or 0) < 0]
    up = [r for r in recs if (_n(r.get("ratio")) or 0) > 0]

    # 按截断后数据给分位
    med = sorted(win)[len(win) // 2] if win else None
    conc_row = sorted([{
        "name": r.get("name"), "code": r.get("code"),
        "ratio": _n(r.get("ratio")),
        "ratio_clamped": None,
        "holder_num": _n(r.get("holder_num")),
        "pre_holder_num": _n(r.get("pre_holder_num")),
        "end_date": r.get("end_date"),
        "avg_market_cap": _n(r.get("avg_market_cap")),
    } for r in recs], key=lambda x: (x["ratio"] if x["ratio"] is not None else 0))

    meta_h = ((snap.get("meta") or {}).get("holders") or {})
    return {
        "boards": conc_row[:15],
        "rows": conc_row[:15],
        "top_concentrate": conc_row[:15],            # 户数降幅最大 = 筹码集中
        "top_disperse": conc_row[-15:][::-1],        # 户数增幅最大 = 筹码分散
        "summary": {
            "total": len(recs),
            "down_count": len(down), "up_count": len(up),
            "down_pct": len(down) / len(recs) * 100 if recs else None,
            "median_ratio": med,
            "clipped_count": len(clipped),
        },
        "latest_report_date": meta_h.get("latest_report_date"),
        "lag_days": meta_h.get("lag_days"),
        "note": ("股东户数是**定期报告披露**，季度级滞后。户数下降 = 筹码向少数人集中，"
                 "但可能是股价下跌导致被动集中，不必然上涨。"),
        "lag_note": f"最新披露日 {meta_h.get('latest_report_date')}，滞后 {meta_h.get('lag_days')} 天",
    }


# ── P4 席位活跃度 ──────────────────────────────────────────────────────

def _p4_seats(snap, lim):
    st = (snap.get("datasets") or {}).get("seats")
    if not st or not st.get("records"):
        lim.append("龙虎榜营业部数据不可用")
        return {"boards": [], "note": DASH}

    meta_s = ((snap.get("meta") or {}).get("seats") or {})
    recs = st["records"]
    ranked = sorted(recs, key=lambda r: -(_n(r.get("buyer_num")) or 0))

    def _rows(src):
        return [{
            "name": r.get("dept_name"), "dept_code": r.get("dept_code"),
            "buyer_num": _n(r.get("buyer_num")), "seller_num": _n(r.get("seller_num")),
            "net_amt": _n(r.get("net_amt")), "date": r.get("date"),
        } for r in src]

    top_active = _rows(ranked[:15])
    top_net_buy = _rows(sorted([r for r in recs if (_n(r.get("net_amt")) or 0) > 0],
                               key=lambda r: -(_n(r.get("net_amt")) or 0))[:10])
    top_net_sell = _rows(sorted([r for r in recs if (_n(r.get("net_amt")) or 0) < 0],
                                key=lambda r: (_n(r.get("net_amt")) or 0))[:10])

    span = meta_s.get("date_span") or {}

    # ⚠️ 历史累计口径下，某些席位是**特定历史事件**的产物，不能当常态读。
    # 实测：中信北京望京(+255.68亿)/金融大街(+181.02亿)/总部(+137.03亿)
    # 三席位的记录日期均为 2015-07-06~08，量级远超同类（其余多在亿级），
    # 这是 2015 年救市期间的资金痕迹，与今日游资无关。
    # 必须在数据里显式标出，否则会被误读为"这些席位今天很活跃"。
    big = []
    for r in recs:
        amt = _n(r.get("net_amt"))
        if amt is None:
            continue
        # 超过同业中位数 20 倍视为量级异常
        nets = [abs(_n(x.get("net_amt")) or 0) for x in recs
                if _n(x.get("net_amt")) is not None]
        if not nets:
            continue
        import statistics as _st
        med = _st.median([x for x in nets if x > 0]) or 1
        if abs(amt) > med * 20:
            big.append({"name": r.get("dept_name"), "net_amt": amt,
                        "date": r.get("date"), "buyer_num": _n(r.get("buyer_num"))})
    big.sort(key=lambda x: -abs(x["net_amt"]))
    if big:
        years = sorted({(b["date"] or "")[:4] for b in big if b["date"]})
        lim.append(
            f"席位净额存在 {len(big)} 个量级异常席位（最高 {big[0]['name'][:16]} "
            f"{_fmt_yi(big[0]['net_amt'])}，日期 {big[0]['date']}），"
            f"涉及年份 {','.join(years)}，属特定历史事件痕迹，非近期常态，已在面板单列说明")

    return {
        "boards": top_active,
        "anomalies": big[:8],
        "rows": top_active,
        "top_active": top_active,
        "top_net_buy": top_net_buy,
        "top_net_sell": top_net_sell,
        "positioning": meta_s.get("positioning") or "历史累计活跃度（非实时信号）",
        "date_span": span,
        "dedup_by": meta_s.get("dedup_by"),
        "raw_rows": meta_s.get("raw_rows"),
        "note": ("这是营业部**历史累计**活跃度，不是当日游资动向。"
                 f"数据跨度 {span.get('min', DASH)} ~ {span.get('max', DASH)}，"
                 "按席位代码去重。"),
    }


# ── 业绩预告 ──────────────────────────────────────────────────────────

def _forecast(snap, lim):
    fc = (snap.get("datasets") or {}).get("forecast")
    if not fc or not fc.get("records"):
        lim.append("业绩预告数据不可用")
        return {"rows": [], "note": DASH}
    recs = fc["records"]
    from collections import Counter
    types = Counter((r.get("type") or "unknown") for r in recs)
    rows = sorted(recs, key=lambda r: (r.get("notice_date") or ""), reverse=True)[:15]
    return {
        "boards": [{
            "name": r.get("name"), "code": r.get("code"),
            "notice_date": r.get("notice_date"), "report_date": r.get("report_date"),
            "type": r.get("type"), "predict": r.get("predict"),
        } for r in rows],
        "rows": rows,
        "type_counts": dict(types),
        "count": len(recs),
        "note": "业绩预告是管理层主动披露的前瞻方向，法定窗口内必须公开。",
    }


# ── 数据可信度 ─────────────────────────────────────────────────────────

def _data_trust(snap):
    meta = snap.get("meta") or {}
    ds = snap.get("datasets") or {}
    out = {}

    sv = meta.get("surveys")
    if sv:
        out["调研"] = {
            "rows": sv.get("raw_rows"), "sessions": sv.get("sessions"),
            "口径": "原始行按「股票×接待起始日×接待方式」聚合为场次；参与者数=行数；NUM 为序号未使用",
            "覆盖": f"{sv.get('notice_days_covered')} 个公告日 "
                    f"{(sv.get('notice_date_range') or ['',''])[0]}~{(sv.get('notice_date_range') or ['',''])[1]}",
            "局限": "数据窗口过短，不能做中长期趋势对比" if (sv.get("notice_days_covered") or 9) <= 5 else "",
            "滞后": "公告日通常滞后实际接待 1-3 天",
        }
    ins = meta.get("insider")
    if ins:
        out["高管变动"] = {
            "rows": ins.get("rows"),
            "口径": "CHANGE_SHARES 负=减持、正=增持；金额随官方字段，缺失不填 0",
            "覆盖": "近 30 天",
            "滞后": "T+1 ~ T+3 披露",
        }
    hd = meta.get("holders")
    if hd:
        out["股东户数"] = {
            "rows": hd.get("rows"),
            "口径": "定期报告披露口径；比率做 1%/99% winsorize",
            "覆盖": f"最新披露日 {hd.get('latest_report_date')}",
            "局限": "季度级数据，严重滞后于实时行情",
            "滞后": f"{hd.get('lag_days')} 天",
        }
    st = meta.get("seats")
    if st:
        out["龙虎榜席位"] = {
            "rows": f"{st.get('rows')}（原始 {st.get('raw_rows')} 行，按代码去重）",
            "口径": st.get("dedup_by") or "",
            "覆盖": f"{(st.get('date_span') or {}).get('min','')} ~ "
                    f"{(st.get('date_span') or {}).get('max','')}",
            "局限": "历史累计活跃度，非当日信号；按活跃度降序取头部样本，非全量",
            "滞后": "无固定口径",
        }
    fc = meta.get("forecast")
    if fc:
        out["业绩预告"] = {"rows": fc.get("rows"), "口径": "管理层主动披露", "滞后": "法定披露窗口"}

    # 明确不可得的数据集：必须展示，只报好消息的看板等于制造新的信息不对称
    unm = meta.get("unavailable") or {}
    out["_不可得"] = {k: v for k, v in unm.items()}
    return out


# ── 规则化判读 ─────────────────────────────────────────────────────────

def _narrative(p1, p2, p3, p4, lim):
    out = []

    boards = p1.get("boards") or []
    if boards:
        b = boards[0]
        out.append(
            f"调研聚集：{b.get('name')} 于 {b.get('notice_date')} 公告一场调研，"
            f"参与机构 {b.get('inst_count')} 家"
            + (f"（{b.get('way')}）" if b.get("way") else "")
            + f"。当日另有 {len(boards)-1} 家公司进入调研榜前列，"
              f"说明机构注意力在集中而非分散。"
        )
    else:
        out.append("调研数据不足以形成聚集判断。")

    s2 = p2.get("summary") or {}
    if s2.get("inc_count") or s2.get("dec_count"):
        out.append(
            f"内部人方向：近 30 天高管增持 {s2.get('inc_count')} 人、"
            f"减持 {s2.get('dec_count')} 人；"
            f"增持金额 {_fmt_yi(s2.get('inc_amount'))}、减持金额 {_fmt_yi(s2.get('dec_amount'))}，"
            # 净额显示绝对值 + 方向词：写"净减持 +14.24亿"会自相矛盾
            f"合计{s2.get('net_sign')} {_fmt_yi(abs(s2.get('net_amount') or 0)).lstrip('+')}。"
            + _reason_lead(p2.get("by_reason"))
        )

    s3 = p3.get("summary") or {}
    if s3.get("total"):
        out.append(
            f"筹码流向：{s3.get('down_count')} 家公司股东户数下降、{s3.get('up_count')} 家上升"
            f"（下降占 {_fmt_pct(s3.get('down_pct'))}）。"
            f"户数下降意味着筹码向少数人集中，但需注意这可能是股价下跌导致的被动集中。"
            f"数据滞后 {p3.get('lag_days')} 天。"
        )

    span = (p4.get("date_span") or {})
    if p4.get("top_active"):
        out.append(
            f"席位口径提示：龙虎榜活跃营业部为**历史累计**数据"
            f"（{span.get('min')} ~ {span.get('max')}），"
            f"不代表当前游资动向，仅用于识别长期活跃席位。"
        )

    # 交叉矛盾：调研密集但高管减持 = 值得警惕的矛盾
    lead_names = {b.get("name") for b in boards[:8] if b.get("name")}
    dec_names = {r.get("name") for r in (p2.get("top_out") or []) if r.get("name")}
    cross = lead_names & dec_names
    if cross:
        out.append(
            f"矛盾信号：{'、'.join(list(cross)[:3])} 同时出现在「调研聚集」与「高管净减持」榜单，"
            f"即机构在密集了解情况的同时，内部人在卖出。这类矛盾值得单独跟踪。"
        )

    out.append(
        "以上均为**公开披露行为的痕迹描述**，不含因果推断。"
        "调研、增减持、股东户数都是合法披露信息，"
        "任何将其解读为内幕交易的推断都不在本面板范围内。"
    )
    return out[:6]


def _fmt_pct(v):
    return DASH if v is None else f"{v:.1f}%"


def _reason_lead(by_reason):
    """描述减持主要通过什么渠道。

    ⚠️ 曾写成「大宗存在就用大宗为主」——实测大宗只有 19 笔、
    集中竞价 557 笔，这样写会把绝对主导的竞价交易说成大宗，结论完全反了。
    必须按**笔数**比较，且只在占比显著时才说"为主"。
    """
    if not by_reason:
        return ""
    total = sum(v.get("count") or 0 for v in by_reason.values())
    if not total:
        return ""
    shares = []
    for k, v in by_reason.items():
        c = v.get("count") or 0
        shares.append((c / total, k, c))
    shares.sort(reverse=True)
    top_ratio, top_name, top_cnt = shares[0]
    if top_ratio >= 0.6:
        return f"减持渠道以 {top_name} 为主（{top_cnt} 笔，占 {top_ratio*100:.0f}%）。"
    return ("减持渠道分散：" +
            "、".join(f"{k} {c} 笔" for _, k, c in shares) + "。")


# ── 主入口 ─────────────────────────────────────────────────────────────

def compute(snapshot):
    lim = []

    p1 = _p1_surveys(snapshot, lim)
    p2 = _p2_insider(snapshot, lim)
    p3 = _p3_holders(snapshot, lim)
    p4 = _p4_seats(snapshot, lim)
    fc = _forecast(snapshot, lim)

    trust = _data_trust(snapshot)
    narrative = _narrative(p1, p2, p3, p4, lim)

    # 取数层带来的限制原样透传
    for m in (snapshot.get("limitations") or []):
        if m not in lim:
            lim.append(m)

    return {
        "as_of": snapshot.get("as_of"),
        "generated_at": snapshot.get("generated_at"),
        "stance": "这里展示的不是内幕，是知情者留下的公开痕迹。",
        "surveys": p1,
        "insider": p2,
        "holders": p3,
        "seats": p4,
        "forecast": fc,
        "data_trust": trust,
        "narrative": narrative,
        "limitations": lim,
    }


# ── 自测 ───────────────────────────────────────────────────────────────

def _self_test():
    import json, os
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "asym_snapshot.json")
    if not os.path.exists(p):
        print("无快照，先跑 asym_fetch.py")
        return
    snap = json.load(open(p, encoding="utf-8"))
    a = compute(snap)

    print("市场立场:", a["stance"])
    print("\n[P1 调研] 单场 Top5:")
    for b in a["surveys"]["boards"][:5]:
        print(f"  {b['notice_date']} {b['name'][:10]:12s} {b['inst_count']:>4}家  {str(b['way'])[:22]}")
    print("  粒度:", a["surveys"]["counts"])

    print("\n[P2 内部人]", a["insider"]["summary"])
    print("  原因分布:")
    for k, v in (a["insider"]["by_reason"] or {}).items():
        print(f"    {k}: {v['count']} 笔  {_fmt_yi(v['amount'])}")

    print("\n[P3 筹码]", a["holders"]["summary"])
    print("  ", a["holders"]["lag_note"])
    print("  集中 Top3:", [(r["name"], round(r["ratio"], 2)) for r in a["holders"]["top_concentrate"][:3]])

    print("\n[P4 席位]", a["seats"]["positioning"], a["seats"]["date_span"])
    print("  活跃 Top3:", [(r["name"][:18], r["buyer_num"]) for r in a["seats"]["top_active"][:3]])

    print("\n[判读]")
    for n in a["narrative"]:
        print("  •", n)

    print("\n[限制]")
    for l in a["limitations"]:
        print("  -", l)


if __name__ == "__main__":
    _self_test()
