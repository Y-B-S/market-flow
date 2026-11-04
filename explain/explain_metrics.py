"""背离解释器 · 计算层。

纯函数：无 IO、无网络。输入 = 主项目的背离信号 + 三层取数结果。

核心原则：
  1. **有多少证据就说多少话** —— 两层不能包装成三层
  2. 每层方向必须附具体数值，禁止黑箱打分
  3. 缺层不推算，明确标"证据不足"
"""
from __future__ import annotations

import math
import statistics

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
    n = _n(v)
    if n is None:
        return DASH
    if abs(n) >= 1e12:
        return f"{n/1e12:+.2f}万亿"
    if abs(n) >= 1e8:
        return f"{n/1e8:+.1f}亿"
    return f"{n/1e4:+.0f}万"


# ── 单层方向判定 ────────────────────────────────────────────────────────

def _layer_flow(div):
    """资金流层：背离信号的固有方向（今日流入 + 中期流出）。"""
    today = _n(div.get("main_net"))
    d5 = _n(div.get("main_net_5d"))
    d10 = _n(div.get("main_net_10d"))
    if today is None or d5 is None:
        return None
    return {
        "layer": "资金流",
        "available": True,
        "direction": "中期流出",
        "dir_num": -1,
        "evidence": (f"今日 {_fmt_yi(today)} / 5日 {_fmt_yi(d5)}"
                     + (f" / 10日 {_fmt_yi(d10)}" if d10 is not None else "")),
        "detail": {
            "today": today, "d5": d5, "d10": d10,
            "severity": _n(div.get("severity")),
        },
        "lag": "收盘后定盘",
    }


def _layer_valuation(name, val):
    """估值层：PE 历史分位 → 偏高/中性/偏低。"""
    if not val:
        return {
            "layer": "估值", "available": False, "direction": None, "dir_num": 0,
            "evidence": "该板块不在申万行业估值体系内（127/496 可覆盖）",
            "detail": {}, "lag": None, "reason": "无官方估值数据",
        }
    pe = _n(val.get("pe_ttm"))
    pct = _n(val.get("pe_pct"))
    days = _n(val.get("hist_days"))
    if pe is None or pct is None:
        return {
            "layer": "估值", "available": False, "direction": None, "dir_num": 0,
            "evidence": "有估值记录但 PE 或分位不可用", "detail": {}, "lag": None,
            "reason": "数据不完整",
        }
    # 分位 → 方向：高分位=贵=对背离提供"利空"解释支持方向+1（贵→跌合理）
    if pct >= 70:
        direction, dir_num = "估值偏高", +1
    elif pct <= 30:
        direction, dir_num = "估值偏低", -1
    else:
        direction, dir_num = "估值中性", 0
    rng = ""
    if val.get("pe_min") and val.get("pe_max"):
        rng = f"，历史区间 {val['pe_min']:.1f}~{val['pe_max']:.1f}"
    return {
        "layer": "估值",
        "available": True,
        "direction": direction,
        "dir_num": dir_num,
        "evidence": f"PE {pe:.1f}，历史分位 {pct:.0f}%{rng}（{int(days)}个交易日）",
        "detail": {"pe": pe, "pe_pct": pct, "hist_days": days,
                   "pe_min": val.get("pe_min"), "pe_max": val.get("pe_max"),
                   "date": val.get("date")},
        "lag": "当日 TTM",
    }


def _layer_news(name, news):
    """新闻层：基于可核对的事实标签净倾向（不做情感分析）。"""
    if not news:
        return {
            "layer": "新闻", "available": False, "direction": None, "dir_num": 0,
            "evidence": "新闻检索失败", "detail": {}, "lag": None,
            "reason": "取数失败",
        }
    kept = news.get("kept") or []
    pos = news.get("pos_hits") or 0
    neg = news.get("neg_hits") or 0
    if not kept:
        return {
            "layer": "新闻", "available": False, "direction": None, "dir_num": 0,
            "evidence": (f"原始 {news.get('raw_count')} 条，"
                         f"剔除同源/广告后无可用的独立报道"),
            "detail": {"raw": news.get("raw_count"),
                       "dropped": news.get("dropped_count")},
            "lag": None,
            "reason": "无独立信息量的报道",
        }
    # 方向：正标签多于负 → 偏正面（对背离是反向证据，dir=-1）
    if pos > neg:
        direction, dir_num = "报道偏正面", -1
    elif neg > pos:
        direction, dir_num = "报道偏负面", +1
    else:
        direction, dir_num = "报道中性", 0
    return {
        "layer": "新闻",
        "available": True,
        "direction": direction,
        "dir_num": dir_num,
        "evidence": (f"{len(kept)} 条独立报道（原始 {news.get('raw_count')} 条，"
                     f"剔除同源 {news.get('dropped_count')} 条）"
                     f"；正面标签 {pos} / 负面标签 {neg}"),
        "detail": {"kept": len(kept), "pos": pos, "neg": neg,
                   "raw": news.get("raw_count"),
                   "dropped": news.get("dropped_count"),
                   "samples": kept[:5]},
        "lag": "公告当日",
        "caveat": "标签为关键词命中，非情感分析；原文链接可自行核对",
    }


def _layer_forecast(name, forecast_hits):
    """财报层（降级）：只报是否有业绩预告，不给板块级财务数字。"""
    if not forecast_hits:
        return {
            "layer": "财报", "available": False, "direction": None, "dir_num": 0,
            "evidence": "该板块成分股近期无业绩预告披露（覆盖率仅约 4%）",
            "detail": {}, "lag": None, "reason": "数据覆盖率过低",
        }
    pos = sum(1 for r in forecast_hits if r.get("dir") == 1)
    neg = sum(1 for r in forecast_hits if r.get("dir") == -1)
    if pos > neg:
        direction, dir_num = "预告偏正面", -1
    elif neg > pos:
        direction, dir_num = "预告偏负面", +1
    else:
        direction, dir_num = "预告中性", 0
    uniq = {}
    for r in forecast_hits:
        uniq[(r.get("code"), r.get("report_date"))] = r
    hits = list(uniq.values())
    pos = sum(1 for r in hits if r.get("dir") == 1)
    neg = sum(1 for r in hits if r.get("dir") == -1)
    und = sum(1 for r in hits if r.get("dir") == 0)
    if pos > neg:
        direction, dir_num = "预告偏正面", -1
    elif neg > pos:
        direction, dir_num = "预告偏负面", +1
    else:
        direction, dir_num = "预告中性", 0
    # 名称唯一化：同一公司可能有多个报告期的预告
    _seen = set()
    names = []
    for r in hits:
        nm = r.get("name")
        if nm and nm not in _seen:
            _seen.add(nm)
            names.append(nm)
        if len(names) >= 5:
            break

    # ⚠️ 选择性偏差：业绩预告是自愿披露，全样本实测 正向436 / 负向166 ≈ 2.6:1。
    # 公司倾向于预告好消息。因此"预告偏正面"本身含有系统性偏乐观成分，
    # 不能简单当作独立证据。
    total_dir = pos + neg
    bias_note = ""
    if total_dir and pos / total_dir > 0.6:
        bias_note = "（注意：预告本身有选择性偏差，全样本正向占比约 72%）"

    return {
        "layer": "财报",
        "available": True,
        "direction": direction,
        "dir_num": dir_num,
        "evidence": (f"成分股中 {len(hits)} 家有业绩预告"
                     f"（预增类 {pos} / 预减类 {neg}"
                     + (f" / 不确定 {und}" if und else "")
                     + f"）：{'、'.join(names[:4])}{bias_note}"),
        "detail": {"hits": len(hits), "pos": pos, "neg": neg, "und": und,
                   "raw_hits": len(forecast_hits)},
        "lag": "季度级",
        "caveat": ("仅提示有预告，不构成板块级财务结论；"
                   "且预告为自愿披露，存在系统性偏乐观"),
    }


# ── 跨层一致性 ──────────────────────────────────────────────────────────

def _consistency(layers):
    """三/两层交叉检验。返回一致性判定。

    方向符号约定：+1 = 与"资金流出"同向（支持背离），-1 = 反向（质疑背离），0 = 中性
    """
    avail = [l for l in layers if l.get("available")]
    if not avail:
        return {"available": 0, "score": None, "label": "证据不足",
                "support": 0, "oppose": 0, "neutral": 0,
                "conflicts": [], "note": "无任何可用层"}
    # 资金流层是锚，恒为 +1（背离本身意味着中期流出）
    support = sum(1 for l in avail if l.get("dir_num") == 1)
    oppose = sum(1 for l in avail if l.get("dir_num") == -1)
    neutral = sum(1 for l in avail if l.get("dir_num") == 0)
    n_avail = len(avail)

    if n_avail >= 3:
        if support >= 2 and oppose == 0:
            label, score = "一致", 90
        elif oppose >= 2 and support <= 1:
            label, score = "矛盾", 30
        elif support >= 1 and oppose >= 1:
            label, score = "部分矛盾", 55
        else:
            label, score = "中性", 50
    elif n_avail == 2:
        if support == 2:
            label, score = "两层一致", 70
        elif oppose == 2:
            label, score = "两层矛盾", 35
        else:
            label, score = "两层中性", 50
    else:
        label, score = "仅一层，证据不足", None

    conflicts = []
    if oppose and support:
        dirs = [f"{l.get('layer', '?')}={l.get('direction') or '?'}"
                for l in avail if l.get("dir_num") in (1, -1)]
        conflicts.append(" / ".join(dirs))

    # 强度限定说明
    note = ""
    if n_avail == 2:
        note = "只有两层可用，结论强度中，不可当三层结论使用"
    elif n_avail == 1:
        note = "仅一层可用，不下结论，只陈列事实"

    return {
        "available": n_avail,
        "score": score,
        "label": label,
        "support": support,
        "oppose": oppose,
        "neutral": neutral,
        "conflicts": conflicts,
        "note": note,
        "max_possible": 4,
    }


# ── 判读文本 ────────────────────────────────────────────────────────────

def _narrative(name, div, layers, cons, limitations):
    out = []
    flow = layers[0]
    out.append(
        f"{name}：今日主力 {_fmt_yi(div.get('main_net'))}，"
        f"但 5 日 {_fmt_yi(div.get('main_net_5d'))}、10 日 {_fmt_yi(div.get('main_net_10d'))}，"
        f"典型「短期回流、中期失血」形态。"
    )

    val = next((l for l in layers if l["layer"] == "估值"), None)
    if val and val.get("available"):
        out.append(f"估值维度：{val['evidence']} → {val['direction']}。")
        if val["dir_num"] == 1:
            out.append("估值不便宜，为「资金撤离」提供了合理性支撑。")
        elif val["dir_num"] == -1:
            out.append("估值处于历史低位，资金撤离更可能是错杀而非基本面恶化。")
    elif val:
        out.append(f"估值维度：{val['evidence']} → 无法判断。")

    nw = next((l for l in layers if l["layer"] == "新闻"), None)
    if nw and nw.get("available"):
        out.append(f"新闻维度：{nw['evidence']} → {nw['direction']}。")
        if nw["dir_num"] == -1:
            out.append("报道口径偏正面，与资金撤离矛盾——这值得警惕。")
        elif nw["dir_num"] == 1:
            out.append("报道口径偏负面，与资金撤离互相印证。")
    elif nw:
        out.append(f"新闻维度：{nw['evidence']}。")

    fc = next((l for l in layers if l["layer"] == "财报"), None)
    if fc and fc.get("available"):
        out.append(f"财报维度：{fc['evidence']} → {fc['direction']}。")
    elif fc:
        out.append(f"财报维度：{fc['evidence']}。")

    # 结论
    if cons["available"] >= 2:
        if cons["label"] == "一致":
            out.append(f"综合判定：{cons['label']} —— 多维度指向同一方向，"
                       f"这个背离相对可信。")
        elif cons["label"] in ("矛盾", "部分矛盾", "两层矛盾"):
            out.append(f"综合判定：{cons['label']} —— 各维度方向不一致，"
                       f"这个背离的成因存疑，需要人工判断（可能只是估值消化或情绪回落）。")
        else:
            out.append(f"综合判定：{cons['label']}。")
    else:
        out.append("证据不足：可用层数太少，不构成判断。")

    out.append("以上为公开数据的交叉陈列与规则判定，不构成投资建议。")
    return out


# ── 主入口 ──────────────────────────────────────────────────────────────

def compute(divergences, fetched):
    """divergences: 主项目的背离记录列表
    fetched: explain_fetch.fetch_all 的返回
    """
    limitations = list(fetched.get("limitations") or [])
    val_all = fetched.get("valuation") or {}
    news_all = fetched.get("news") or {}
    fc_all = fetched.get("forecast_by_board") or {}

    cards = []
    for div in divergences:
        name = div.get("name")
        layers = [
            _layer_flow(div),
            _layer_valuation(name, val_all.get(name)),
            _layer_news(name, news_all.get(name)),
            _layer_forecast(name, fc_all.get(name)),
        ]
        cons = _consistency(layers)
        narr = _narrative(name, div, layers, cons, limitations)
        cards.append({
            "name": name,
            "code": div.get("code"),
            "flow": {
                "today": _n(div.get("main_net")),
                "d5": _n(div.get("main_net_5d")),
                "d10": _n(div.get("main_net_10d")),
                "severity": _n(div.get("severity")),
                "quadrant": div.get("quadrant"),
            },
            "layers": layers,
            "consistency": cons,
            "narrative": narr,
        })

    # 汇总统计：多少板块有几层
    dist = {}
    for c in cards:
        k = c["consistency"]["available"]
        dist[k] = dist.get(k, 0) + 1

    # 缺估值是常态，必须进 limitations
    no_val = sum(1 for c in cards
                 if not next((l for l in c["layers"] if l["layer"] == "估值"
                              and l.get("available")), None))
    if no_val:
        limitations.append(
            f"{no_val}/{len(cards)} 个板块无官方估值数据"
            f"（申万估值体系仅 127 板块，与东财 496 板块体系不同）")

    return {
        "as_of": fetched.get("as_of"),
        "generated_at": fetched.get("generated_at"),
        "cards": cards,
        "layer_distribution": dist,
        "limitations": limitations,
    }


if __name__ == "__main__":
    # 用真实数据自测
    import json
    import os
    import sys
    HERE = os.path.dirname(os.path.abspath(__file__))
    ROOT = os.path.dirname(HERE)
    sys.path.insert(0, ROOT)
    sys.path.insert(0, HERE)
    import explain_fetch as F
    import metrics

    snap = json.load(open(os.path.join(ROOT, "data", "2026-11-03.json"), encoding="utf-8"))
    a = metrics.compute(snap)
    div = (a.get("divergence_gated") or a.get("divergence_top") or [])[:6]
    names = [d["name"] for d in div]
    print(f"样本: {names}\n")
    boards_meta = [(d.get("code"), d.get("name")) for d in div]
    data = F.fetch_all(names, boards=boards_meta, as_of="2026-11-03")
    res = compute(div, data)
    print(f"\n层数分布: {res['layer_distribution']}")
    print(f"限制: {res['limitations'][-1] if res['limitations'] else '无'}\n")
    for c in res["cards"][:4]:
        print("=" * 70)
        print(f"{c['name']}  [{c['consistency']['label']}]  "
              f"可用层数={c['consistency']['available']}")
        for l in c["layers"]:
            mark = "✓" if l.get("available") else "✗"
            print(f"  {mark} {l['layer']:<4s} {l.get('direction') or '不可得':<10s} "
                  f"{l['evidence'][:60]}")
        print(f"  判读: {c['narrative'][-2]}")
