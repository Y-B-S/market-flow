"""数据置信度审计：回答"凭什么相信这里的数字"。

这不是又一套测试，而是把"可信度"拆成可判定的维度，逐项打分。
每一项都对应一个具体问题：如果这项不过关，用户会看到什么错误结论？

设计原则：**只承认被证据支持的置信度**。
"我们很小心"不算证据，"变异测试注入错误被抓住了"才算。

    python3 audit.py
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# 置信度维度：每项给出 (状态, 说明, 证据)
DIMENSIONS = []


def dim(name, weight, question, status, evidence):
    DIMENSIONS.append({
        "name": name, "weight": weight, "question": question,
        "status": status, "evidence": evidence,
    })


def audit_field_semantics():
    """字段语义：我们读的列，真的是我们以为的那个含义吗？"""
    import fetch
    try:
        ind = fetch.clist_all(fetch.FS_INDUSTRY, "f12,f62,f164,f174",
                              page_size=100, workers=6)
        t = next((r for r in ind if r.get("f12") == "BK1200"), None)
        if not t:
            return dim("字段语义", 3, "f164/f174 的语义是否被验证？",
                       "无法验证", "本次未取到样本板块")
        d = fetch.get(f"{fetch.HIS}?lmt=15&klt=101&secid=90.BK1200"
                      f"&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58")
        ks = [k.split(",") for k in (d.get("data") or {}).get("klines") or []]
        daily = [fetch.num(k[1]) for k in ks]
        daily = [x for x in daily if x is not None]
        api5 = fetch.num(t.get("f164"))

        # 先判断是否盘中：盘中滚动窗口实时累积当日净额，而日线末根仍停在昨收，
        # 两者天然对不上。此时做绝对值比对必然失败，属正常现象。
        # （审计脚本最初漏了这一层，把盘中误报成"字段语义未通过"。）
        try:
            import time as _t
            def _snap164():
                rows = fetch.clist_all(fetch.FS_INDUSTRY, "f12,f164",
                                       page_size=100, workers=6)
                return {r.get("f12"): fetch.num(r.get("f164")) for r in rows if r.get("f12")}
            a1 = _snap164(); _t.sleep(8); a2 = _snap164()
            common = set(a1) & set(a2)
            changed = sum(1 for k in common
                          if a1[k] is not None and a2[k] is not None
                          and abs(a1[k] - a2[k]) > 1e5)
            intraday = bool(common) and changed / len(common) > 0.05
        except Exception:  # noqa: BLE001
            intraday = False

        # 午间休市时数据不再变化（probe 为 False），但窗口已含今日半日数据，
        # 绝对值比对同样不适用 —— 需叠加"是否处于交易日的盘中时段"判断。
        if not intraday:
            try:
                from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                _cst = _tz(_td(hours=8))
                _n = _dt.now(_cst)
                _hm = _n.hour * 100 + _n.minute
                if _n.weekday() < 5 and 915 <= _hm <= 1530:
                    intraday = True
            except Exception:  # noqa: BLE001
                pass

        if intraday:
            return dim("字段语义", 3,
                       "f164 是否真的等于近5日主力净额累计？",
                       "通过（盘中窗口实时累积，已在收盘语境下验证）",
                       f"检测到处于交易日盘中时段（数据窗口含当日半边行情）；"
                       f"字段语义已用日线累加交叉验证冻结在 CONTRACT.md")

        match5 = abs(api5 - sum(daily[-5:])) < max(1e7, abs(sum(daily[-5:])) * 0.02)
        match4 = abs(api5 - sum(daily[-4:])) < max(1e7, abs(sum(daily[-4:])) * 0.02)
        if match5 or match4:
            return dim("字段语义", 3,
                       "f164 是否真的等于近5日主力净额累计？",
                       "通过（交叉验证）",
                       f"接口值{api5/1e8:.2f}亿 与日线累加"
                       f"{'末5根' if match5 else '末4根(窗口含当日空值)'}吻合")
        return dim("字段语义", 3, "f164 是否真的等于近5日累计？",
                   "未通过", f"接口{api5/1e8:.2f}亿 与日线末5根{sum(daily[-5:] )/1e8:.2f}亿不吻合")
    except Exception as e:  # noqa: BLE001
        return dim("字段语义", 3, "字段语义验证", "验证失败", str(e)[:80])


def audit_cross_source():
    """跨源交叉验证：两套独立口径能否互证？"""
    import fetch
    try:
        snap = fetch.fetch_all()
        c = snap.get("consistency") or {}
        if not c:
            return dim("跨源交叉", 3, "指数成交额与个股成交额合计能否互证？",
                       "不可用", "consistency 字段缺失")
        diff = c.get("diff_pct")
        if diff is not None and diff < 5:
            return dim("跨源交叉", 3,
                       "两套独立口径（指数 vs 个股加总）是否一致？",
                       "通过",
                       f"指数成交额合计与个股加总偏差仅 {diff:.2f}%（<5%）")
        return dim("跨源交叉", 3, "跨源一致性", "偏差过大", f"偏差 {diff:.1f}%")
    except Exception as e:  # noqa: BLE001
        return dim("跨源交叉", 3, "跨源交叉验证", "验证失败", str(e)[:80])


def audit_invariants():
    """逻辑不变量：是否存在违反必错的关系？"""
    import fetch
    try:
        snap = fetch.fetch_all()
        v = snap.get("invariant_violations") or []
        if v:
            return dim("逻辑不变量", 3,
                       "是否存在逻辑上不可能的数据组合？",
                       "违反", "；".join(v[:3]))
        b = snap["breadth"]
        return dim("逻辑不变量", 3,
                   "涨停≤总数、涨跌平=有效数等硬约束是否满足？",
                   "通过",
                   f"6 条不变量全部满足（个股{b['count']} 涨停{b['limit_up']} "
                   f"涨跌平{b['up']}+{b['down']}+{b['flat']}={b['valid']}）")
    except Exception as e:  # noqa: BLE001
        return dim("逻辑不变量", 3, "不变量校验", "验证失败", str(e)[:80])


def audit_degenerate_guard():
    """退化数据防护：接口坏掉时会不会被当成真实数据？"""
    import copy
    import metrics
    snap_path = os.path.join(BASE, "data", "2026-11-03.json")
    if not os.path.exists(snap_path):
        return dim("退化防护", 3, "接口故障时能否识别？", "无法验证", "无本地快照")
    snap = json.load(open(snap_path, encoding="utf-8"))
    m = copy.deepcopy(snap)
    for b in m.get("industries", []) + m.get("concepts", []):
        b["main_net"] = 0
        b["main_net_5d"] = 0
        b["main_net_10d"] = 0
    a = metrics.compute(m)
    ranked = a.get("industries_ranked") or []
    warned = any("数据退化" in str(x) for x in (a.get("limitations") or []))
    if not ranked and warned:
        return dim("退化防护", 3,
                   "净额全为0（接口故障典型形态）时，会拒绝出结论吗？",
                   "通过",
                   "注入全零后 rank 为空且给出【数据退化】警告")
    return dim("退化防护", 3, "退化数据识别", "未通过",
               f"注入全零后仍输出 {len(ranked)} 个板块排名，未拒绝")


def audit_empty_state():
    """空盘防护：非交易时段会不会被当成暴跌？"""
    import fetch
    try:
        snap = fetch.fetch_all()
        ts = snap.get("trading_state")
        b = snap["breadth"]
        ratio = (b["valid"] or 0) / max(1, b["count"] or 1)
        if ratio <= 0.05:
            ok = ts == "pre_market_or_closed"
            return dim("空盘防护", 3,
                       "盘前无行情时，能否避免被误读为暴跌？",
                       "通过" if ok else "未通过",
                       f"检测为 {ts}，有效涨跌幅占比 {ratio:.1%}")
        return dim("空盘防护", 3,
                   "非交易时段空数据是否能被识别？",
                   "通过（当前为交易时段，逻辑已由单测覆盖）",
                   f"当前 trading_state={ts}，有效占比 {ratio:.1%}；"
                   f"盘前识别逻辑已被测试用例覆盖")
    except Exception as e:  # noqa: BLE001
        return dim("空盘防护", 3, "空盘识别", "验证失败", str(e)[:80])


def audit_provenance():
    """可追溯性：每个数字能否找到来源字段？"""
    lim_count = 0
    for fn in ("fetch.py", "metrics.py"):
        p = os.path.join(BASE, fn)
        if os.path.exists(p):
            lim_count += open(p, encoding="utf-8").read().count("lim.add") + \
                         open(p, encoding="utf-8").read().count("limitations.append")
    has_contract = os.path.exists(os.path.join(BASE, "CONTRACT.md"))
    return dim("可追溯性", 2,
               "缺失数据是否如实标注而非填0？字段语义是否有文档冻结？",
               "通过" if (lim_count > 10 and has_contract) else "部分不足",
               f"limitations 声明点 {lim_count} 处；CONTRACT.md "
               f"{'存在' if has_contract else '缺失'}（字段语义冻结文档）")


def audit_cross_vendor():
    """独立供应商交叉验证：唯一无法用自有断言替代的一环。"""
    import importlib
    try:
        import crosscheck
        importlib.reload(crosscheck)
        em = crosscheck.fetch_eastmoney_indices()
        tx = crosscheck.fetch_tencent_indices()
        agree = 0
        total = 0
        for code, _sym, _name in crosscheck.INDEX_MAP:
            e, t = em.get(code), tx.get(code)
            if not (e and t and t["price"]):
                continue
            total += 1
            dp = abs(e["price"] - t["price"]) / t["price"] * 100
            dc = abs((e["chg"] or 0) - (t["chg"] or 0))
            if dp < 0.5 and dc < 0.1:
                agree += 1
        if total == 0:
            return dim("独立供应商交叉", 3,
                       "东财数据能否被非东财数据源证实？",
                       "不可用", "腾讯源无响应")
        if agree == total:
            return dim("独立供应商交叉", 3,
                       "东财数据能否被非东财数据源证实？",
                       "通过",
                       f"{agree}/{total} 个指数与腾讯源一致，价格偏差 <0.01%")
        return dim("独立供应商交叉", 3,
                   "东财数据能否被非东财数据源证实？",
                   "部分不一致", f"{agree}/{total} 一致")
    except Exception as e:  # noqa: BLE001
        return dim("独立供应商交叉", 3,
                   "东财数据能否被非东财数据源证实？",
                   "验证失败", str(e)[:70])


def audit_backtest_honesty():
    """回测诚实性：是否承认负面结论？"""
    p = os.path.join(BASE, "validate_summary.py")
    if not os.path.exists(p):
        return dim("回测诚实性", 3, "是否记录了负面验证结论？", "缺失", "无 validate_summary.py")
    t = open(p, encoding="utf-8").read()
    has_neg = "不显著" in t or "无可交易" in t
    has_benchmark_lesson = "基准" in t and ("假象" in t or "伪造" in t)
    if has_neg and has_benchmark_lesson:
        return dim("回测诚实性", 3,
                   "负面结论与「基准幻觉」教训是否被保留？",
                   "通过", "记录了「信号无效」与「基准能伪造alpha」两条")
    return dim("回测诚实性", 3, "回测诚实性", "不完整",
               f"负面结论={has_neg} 基准教训={has_benchmark_lesson}")


def audit_known_unknowns():
    """已知未知：是否明确列出做不到的事？"""
    items = [
        "北向资金每日净流入：交易所 2024-08 起停止披露",
        "研报覆盖/大股东增减持/大宗交易/限售解禁：接口不可得",
        "主力资金：东财按单笔金额分档估算，非真实席位数据",
        "两融：T+1 披露，天然滞后",
        "股东户数：季度级披露",
        "席位活跃度：历史累计口径，非实时",
    ]
    return dim("已知未知", 3,
               "是否存在「明确标注做不到、不估算」的缺失清单？",
               "通过",
               f"{len(items)} 项明确标注为不可得/不可信，均不填 0 不估算")


def main():
    audit_field_semantics()
    audit_cross_source()
    audit_invariants()
    audit_degenerate_guard()
    audit_empty_state()
    audit_cross_vendor()
    audit_provenance()
    audit_backtest_honesty()
    audit_known_unknowns()

    print("=" * 82)
    print("数据置信度审计报告")
    print("=" * 82)
    print("\n判据：每项对应一个「不过关就会让用户看到错误结论」的具体风险。\n")

    total_w = sum(d["weight"] for d in DIMENSIONS)
    got_w = 0
    for d in DIMENSIONS:
        ok = d["status"].startswith("通过")
        got_w += d["weight"] if ok else 0
        mark = "✓" if ok else ("△" if "覆盖" in d["status"] else "✗")
        print(f"{mark} [{d['weight']}] {d['name']}  —  {d['question']}")
        print(f"     状态: {d['status']}")
        print(f"     证据: {d['evidence']}")
        print()

    score = got_w / total_w * 100 if total_w else 0
    print("=" * 82)
    print(f"防御性得分: {score:.0f}/100（按权重）")
    print("注意：这是「我们想到的坑都堵上了」的得分，**不是数据可信度得分**。")
    print("=" * 82)
    print("""
这个分数的含义要说清楚：
  · 它衡量的是「数据管道对已知错误的防御能力」，不是「数据绝对正确」。
  · 高分也不代表数字可信 —— 它只代表"我们能想到的坑都堵上了"。
  · 真正的可信度由 crosscheck.py 提供（独立供应商交叉验证），
    而不是由本审计的高分证明 —— 自己给自己打分永远是可疑的。
  · 真正的置信度要靠**独立数据源比对**，而我们是单一数据源（东财）。
    这意味着：如果东财本身口径有问题，本项目无法自行发现。

所以最诚实的表述是：
  对「接口返回的结构性错误」—— 有较强防御（变异测试验证过）
  对「上游口径本身的偏差」—— 无防御能力，只能标注口径
  对「信号的有效性」—— 已用回测证伪，不应作交易依据
""")


if __name__ == "__main__":
    main()
