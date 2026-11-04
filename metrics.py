"""metrics.py — 资金流动大盘：指标计算层（纯函数）。

依赖：仅 Python 标准库（本文件实际只用到内置语法，未 import 任何第三方）。
约束：**零 IO / 零网络 / 零文件读写**。唯一例外是 __main__ 自测里读
      sample_snapshot.json，用于本地验证。

对外契约（CONTRACT.md）：
    compute(snapshot: dict) -> analysis: dict

设计原则
---------
1. 一切数值必须来自 snapshot。任何缺失一律降级为 None，并把原因写入
   analysis['limitations']。**绝不填 0 冒充真实值** —— 资金净额为 0 与
   "没有数据"是两件事，混在一起会让下游渲染说谎。
2. 打分用"全板块截面分位"（mid-rank percentile），不依赖分布假设，
   天然抗极端值。这一点很重要：板块净额的量级差异达 3 个数量级
   （本样本行业净额从 -236 亿到 +34 亿），直接线性归一化会被个别
   权重板块绑架。
3. 所有结论必须可解释：regime 返回命中规则原文 + 全部候选规则的
   评估记录；flow_score 返回三个分项分位与一致性调整量。不允许黑箱。
4. 禁止出现任何投资建议措辞（买入/卖出/建议/目标价/推荐）。
   本模块只做**描述性统计**与**形态归类**。

字段语义以 CONTRACT.md 为准（已用日线累加交叉验证冻结）：
    f62 -> main_net      今日主力净额（元）
    f164 -> main_net_5d  近 5 日累计（元）
    f174 -> main_net_10d 近 10 日累计（元）
    f184 -> main_ratio   今日主力净额 / 成交额（%）
    f21  -> mktcap_float 流通市值（元）

主力资金口径：东财按单笔成交金额分档估算，非真实席位数据。
"""

from __future__ import annotations

import math

__all__ = ["compute"]

# --------------------------------------------------------------------------
# 常量：全部为"可解释的阈值"，集中放置便于复核
# --------------------------------------------------------------------------

#: flow_score 三维加权。今日权重最高，中期用于确认。
FLOW_WEIGHTS = {"today": 0.5, "d5": 0.3, "d10": 0.2}

#: 一致性调整量（分）。正=同向确认，负=背离扣分。
CONSISTENCY = {
    "full_inflow": 8.0,      # 今日+5日+10日 三档全流入
    "inflow_confirmed": 4.0,  # 今日+5日 同向，10日未确认
    "inflow_divergence": -10.0,  # 今日流入但 5日/10日 双流出 —— 核心背离
    "inflow_weak": -6.0,     # 今日流入但 5日流出
    "full_outflow": -8.0,    # 三档全流出
    "outflow_confirmed": -4.0,  # 今日+5日 流出
    "outflow_pullback": 6.0,  # 今日流出但中期流入 —— 回吐，非失血
}

#: 四象限标签（顺序即渲染顺序）
QUADRANTS = ("流入加速", "抄底试探", "持续失血", "拉高出货")

#: 情绪综合分的分项权重
SENTIMENT_WEIGHTS = {
    "breadth": 0.30,   # 赚钱效应
    "limit": 0.20,     # 涨跌停比
    "seal": 0.15,      # 封板率
    "netflow": 0.20,   # 主力净额占成交比
    "ladder": 0.15,    # 连板高度
}

#: 主力净额占成交比的映射区间（%）。超出即饱和到 0/100。
NETFLOW_PCT_RANGE = 6.0

#: 两融单日变动映射区间（亿元）
MARGIN_CHANGE_RANGE = 500.0

#: z-score 至少需要的样本数（去掉当日后的历史根数）
MIN_Z_SAMPLES = 5

#: ── 背离分层门槛（决定产品卖点是否可信）────────────────────────────
#: "今日流入但中期流出"的原始命中在本样本行业里有 68~71 个，但绝大多数
#: 只是分位裂口极小的噪声：尾部裂口低到 1.2，对应板块今日只流入 200 万，
#: 其中期分位甚至高于今日分位——把这种报成"背离"是在骗用户。
#:
#: 因此做三层，而不是简单截断：
#:   divergence          全量命中，供渲染层展开/计数，不做取舍
#:   divergence_strong   过双门槛（裂口≥50 且今日≥3000万），按裂口降序
#:   divergence_top      最多 5 条，按"裂口 × 流入规模"综合排序 —— 给人看的那栏
#:
#: 为什么要第三层：裂口大但只流入 200 万的板块，和裂口稍小但流入 30 亿的
#: 板块，后者才值得占用用户注意力。只按裂口排序会让小票噪声霸榜。
DIVERGENCE_MIN_SEVERITY = 50.0
DIVERGENCE_MIN_AMOUNT = 3e7  # 3000 万元
DIVERGENCE_TOP_N = 5


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def _num(v):
    """安全数值转换。

    东财在停牌股上返回字符串 "-"；JSON 里也可能是 None / bool。
    契约 v1 明确要求所有取数走安全转换，禁止直接比较。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(v, str):
        s = v.strip()
        if not s or s in ("-", "--", "—", ""):
            return None
        try:
            f = float(s)
        except (TypeError, ValueError):
            return None
        return None if (math.isnan(f) or math.isinf(f)) else f
    return None


def _sign(v):
    """-1 / 0 / +1；None 透传为 None。"""
    if v is None:
        return None
    if v > 0:
        return 1
    if v < 0:
        return -1
    return 0


def _clamp(v, lo=0.0, hi=100.0):
    if v is None:
        return None
    return max(lo, min(hi, v))


def _mean(vals):
    """忽略 None 的均值；空集返回 None。"""
    xs = [v for v in vals if v is not None]
    return sum(xs) / len(xs) if xs else None


def _pstdev(vals):
    """总体标准差；样本不足返回 None（不用 0 冒充"无波动"）。"""
    xs = [v for v in vals if v is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _pct_rank(population, v):
    """mid-rank 分位（0-100）。

    并列值取平均秩，避免"全部相等"时人为制造 0/100 的假区分。
    空总体或 v 为 None 时返回 None。
    """
    if v is None:
        return None
    xs = [x for x in population if x is not None]
    if not xs:
        return None
    below = sum(1 for x in xs if x < v)
    equal = sum(1 for x in xs if x == v)
    return (below + 0.5 * equal) / len(xs) * 100.0


def _fmt_yi(v, signed=True):
    """金额格式化：自动 亿 / 万亿。取不到返回破折号。"""
    if v is None:
        return "—"
    sign = "+" if signed else ""
    if abs(v) >= 1e12:
        return f"{v / 1e12:{sign}.2f}万亿"
    if abs(v) >= 1e8:
        return f"{v / 1e8:{sign}.1f}亿"
    return f"{v / 1e4:{sign}.0f}万"


def _hhmmss(v):
    """东财封板时间 92500 -> '09:25:00'。"""
    n = _num(v)
    if n is None:
        return None
    try:
        i = int(n)
    except (TypeError, ValueError, OverflowError):
        return None
    h, m, s = i // 10000, (i // 100) % 100, i % 100
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        return None
    return f"{h:02d}:{m:02d}:{s:02d}"


class _Lim:
    """limitations 收集器：同一原因只记一次，保持顺序。"""

    def __init__(self):
        self._items = []
        self._seen = set()

    def add(self, msg):
        if msg and msg not in self._seen:
            self._seen.add(msg)
            self._items.append(msg)

    def extend(self, msgs):
        for m in msgs or []:
            self.add(m)

    def out(self):
        return list(self._items)


# --------------------------------------------------------------------------
# 板块流强度打分
# --------------------------------------------------------------------------

def _consistency_note(t, d5, d10):
    """一致性判定 -> (调整分, 说明文字)。

    这是产品的核心差异点：纯分位打分会把"今日流入但中期大幅流出"的
    板块误判为强势（因为它今日分位确实高）。必须用中期维度去修正。
    """
    st, s5 = _sign(t), _sign(d5)
    s10 = _sign(d10) if d10 is not None else None

    if st is None or s5 is None:
        return 0.0, "今日或5日净额缺失，未做一致性调整"

    if st > 0:
        if s5 > 0 and s10 == 1:
            return CONSISTENCY["full_inflow"], "今日/5日/10日三档同向流入，中期趋势确认"
        if s5 > 0:
            return CONSISTENCY["inflow_confirmed"], "今日与5日同向流入，10日尚未确认"
        if s5 < 0 and s10 == -1:
            return CONSISTENCY["inflow_divergence"], \
                "今日流入但5日/10日双双流出，中期资金仍在撤离（背离）"
        if s5 < 0:
            return CONSISTENCY["inflow_weak"], "今日流入但5日为流出，中期未确认"
        return 2.0, "今日流入而5日基本持平"

    if st < 0:
        if s5 < 0 and s10 == -1:
            return CONSISTENCY["full_outflow"], "今日/5日/10日三档同向流出，失血趋势确认"
        if s5 < 0:
            return CONSISTENCY["outflow_confirmed"], "今日与5日同向流出"
        if s5 > 0:
            return CONSISTENCY["outflow_pullback"], \
                "今日流出但5日为流入，属前期流入后的回吐而非趋势性失血"
        return -2.0, "今日流出而5日基本持平"

    # 今日恰好为 0（极少见）
    return 0.0, "今日主力净额恰为 0，方向不可判"


def _score_board(row, pop):
    """对单个板块计算 flow_score 与其构成。返回 dict（不改动入参）。"""
    t = _num(row.get("main_net"))
    d5 = _num(row.get("main_net_5d"))
    d10 = _num(row.get("main_net_10d"))
    cap = _num(row.get("mktcap_float"))
    amount = _num(row.get("amount"))
    main_ratio = _num(row.get("main_ratio"))

    p_t = _pct_rank(pop["today"], t)
    p_5 = _pct_rank(pop["d5"], d5)
    p_10 = _pct_rank(pop["d10"], d10)

    # 加权分位：只用可用的维度，权重按可用项重新归一，避免缺失维度被当 0
    parts, wsum = [], 0.0
    for p, w in ((p_t, FLOW_WEIGHTS["today"]),
                 (p_5, FLOW_WEIGHTS["d5"]),
                 (p_10, FLOW_WEIGHTS["d10"])):
        if p is not None:
            parts.append(p * w)
            wsum += w
    base = (sum(parts) / wsum) if wsum > 0 else None

    adj, note = _consistency_note(t, d5, d10)
    # raw_score 保留未经截断的复合分（base∈[0,100]，adj∈[-10,+8]），
    # 由 _rank_boards 在截面内做单调重标定后再产出最终 flow_score。
    raw = (base + adj) if base is not None else None
    score = _clamp(raw) if raw is not None else None

    # 归一化强度：主力净额 / 流通市值（%）。这是不受板块体量影响的量，
    # 用来发现"小板块大异动"，与绝对金额的排序互补。
    intensity = None
    if t is not None and cap is not None and cap > 0:
        intensity = t / cap * 100.0

    quadrant, quad_note = _classify_quadrant(t, d5, d10)

    return {
        "code": row.get("code"),
        "name": row.get("name"),
        "kind": row.get("kind"),
        "chg": _num(row.get("chg")),
        "amount": amount,
        "turnover": _num(row.get("turnover")),
        "mktcap_float": cap,
        "main_net": t,
        "main_net_5d": d5,
        "main_net_10d": d10,
        "main_ratio": main_ratio,
        "intensity": intensity,
        "intensity_pct": _pct_rank(pop["intensity"], intensity),
        "p_today": p_t,
        "p_5d": p_5,
        "p_10d": p_10,
        "base_score": base,
        "raw_score": raw,
        "consistency_adj": adj,
        "consistency_note": note,
        "flow_score": score,
        "quadrant": quadrant,
        "quadrant_note": quad_note,
    }


def _classify_quadrant(t, d5, d10):
    """今日 × 5日 符号定象限，10日做确认标注。

    象限语义（从"钱在往哪流"的角度）：
      流入加速 = 短期中期同向流入，趋势确立
      抄底试探 = 短期流入、中期流出，资金在逆势试探（本产品核心信号）
      持续失血 = 短期中期同向流出，趋势性撤离
      拉高出货 = 短期流出、中期流入，前期流入盘开始兑现
    """
    st, s5 = _sign(t), _sign(d5)
    if st is None or s5 is None:
        return None, "今日或5日净额缺失，无法归入象限"
    if st == 0 or s5 == 0:
        return None, "今日或5日净额恰为 0，方向无意义"

    s10 = _sign(d10) if d10 is not None else None

    if st > 0 and s5 > 0:
        if s10 == 1:
            return "流入加速", "今日与5日同向流入，10日亦为流入，趋势确认"
        if s10 == -1:
            return "流入加速", "今日与5日同向流入，但10日累计仍为流出，属恢复初期"
        return "流入加速", "今日与5日同向流入（10日数据缺失，未做确认）"

    if st > 0 and s5 < 0:
        if s10 == -1:
            return "抄底试探", "今日流入但5日与10日均流出，中期资金仍在撤离"
        if s10 == 1:
            return "抄底试探", "今日流入、5日流出，10日为流入，中期方向分歧"
        return "抄底试探", "今日流入但5日流出（10日数据缺失）"

    if st < 0 and s5 < 0:
        if s10 == 1:
            return "持续失血", "今日与5日均流出，10日累计仍为流入，属流入趋势中的回撤"
        return "持续失血", "今日与5日同向流出，中期一致性失血"

    # st < 0 and s5 > 0
    if s10 == 1:
        return "拉高出货", "今日流出但5日/10日均流入，前期流入盘出现兑现"
    if s10 == -1:
        return "拉高出货", "今日与5日方向相反，10日为流出，中期已转弱"
    return "拉高出货", "今日流出而5日流入（10日数据缺失）"


def _rank_boards(boards, lim, label):
    """给一组板块（行业或概念）打分并排序。"""
    if not boards:
        lim.add(f"{label}板块数据为空，相关打分与排名不可用")
        return [], {"today": [], "d5": [], "d10": [], "intensity": []}

    # ── 退化数据检测 ──────────────────────────────────────────────
    # 实测（变异测试）：把 457 个板块的净额全置 0 后，程序仍会"正常"输出
    # 仅 1 个板块（镍）的排名，且不报警——这是接口故障时最危险的形态：
    # 数字都合法，结论完全无意义。
    # 统计"非零净额的占比"，过低则拒绝排名。
    _nets = [_num(b.get("main_net")) for b in boards]
    _valid = [x for x in _nets if x is not None]
    if _valid:
        _nonzero = sum(1 for x in _valid if x != 0)
        _ratio = _nonzero / len(_valid)
        if _ratio < 0.10:
            lim.add(f"【数据退化】{label}板块中仅 {_nonzero}/{len(_valid)} "
                    f"({_ratio*100:.1f}%) 有非零主力净额，疑似接口故障或全市场无成交，"
                    f"已拒绝产出板块排名（避免把无效数据当真实结论）")
            return [], {"today": [], "d5": [], "d10": [], "intensity": []}

    pop = {
        "today": [_num(b.get("main_net")) for b in boards],
        "d5": [_num(b.get("main_net_5d")) for b in boards],
        "d10": [_num(b.get("main_net_10d")) for b in boards],
    }
    # intensity 总体要先算出来（依赖流通市值），用两遍扫描
    caps = [_num(b.get("mktcap_float")) for b in boards]
    intens = []
    for b, cap in zip(boards, caps):
        t = _num(b.get("main_net"))
        intens.append(t / cap * 100.0 if (t is not None and cap is not None and cap > 0) else None)
    pop["intensity"] = intens

    for key, field in (("today", "main_net"), ("d5", "main_net_5d"), ("d10", "main_net_10d")):
        if all(v is None for v in pop[key]):
            lim.add(f"{label}板块的 {field} 全为缺失，该维度分位不可用")
    if all(v is None for v in pop["intensity"]):
        lim.add(f"{label}板块缺流通市值，无法计算占市值比强度")

    # ── 同义层级去重 ──────────────────────────────────────────────
    # 东财行业板块同时混有申万二级与三级分类：保险Ⅱ/保险Ⅲ、白酒Ⅱ/白酒Ⅲ
    # 等 39+43 个板块其实是同一批成分股在两级分类下的重复条目，
    # 三项资金流数值完全相同。若不去重，TOP20 会被这些孪生条目挤占。
    # 判据：今日净额、5日、10日 三者完全相同即视为同一条目；
    # 保留名称更短的那个（通常是不带罗马数字的上级分类）。
    orig_n = len(boards)
    seen, uniq = {}, []

    def _prefer(name_a, name_b):
        """同族条目里该保留哪个名字。返回 True 表示 name_a 更该保留。

        两条判据，按优先级：
        1) 名称更短者优先（不带层级后缀的上级分类，信息密度更高）。
        2) 长度相同时，按申万层级排序取更**高级**的那级：
           Ⅱ < Ⅲ < Ⅳ。原实现只比长度，保险Ⅱ/保险Ⅲ 同为 3 字时
           退化成"先到先得"，实际保留的是 Ⅲ 级——层级越低成分越窄，
           对外展示时应优先用更宽的上级分类。
        """
        a, b = (name_a or ""), (name_b or "")
        if len(a) != len(b):
            return len(a) < len(b)
        order = {"Ⅱ": 2, "Ⅲ": 3, "Ⅳ": 4, "Ⅰ": 1}
        ta = order.get(a[-1:], 0)
        tb = order.get(b[-1:], 0)
        return ta < tb

    for b in boards:
        key = (_num(b.get("main_net")),
               _num(b.get("main_net_5d")),
               _num(b.get("main_net_10d")))
        if all(v is None for v in key):
            uniq.append(b)
            continue
        if key in seen:
            keep = seen[key]
            if _prefer(b.get("name"), keep.get("name")):
                uniq[uniq.index(keep)] = b
                seen[key] = b
            continue
        seen[key] = b
        uniq.append(b)
    dropped = orig_n - len(uniq)
    if dropped > 0:
        lim.add(f"{label}板块中 {dropped} 个为同义层级重复条目（二级/三级分类指向同一批成分股），已按申万层级择优保留")
    boards = uniq

    scored = [_score_board(b, pop) for b in boards]

    # 缺关键维度的板块单独记录，不参与主排名（但不丢弃）
    bad = [s for s in scored if s["flow_score"] is None]
    if bad:
        lim.add(f"{label}板块中 {len(bad)} 个因关键字段缺失无法打分，已排除在排名之外")
    good = [s for s in scored if s["flow_score"] is not None]

    # ── 单调重标定 ────────────────────────────────────────────────
    # 原始复合分 = 加权分位(0~100) + 一致性调整(-10~+8)，直接截断会让
    # 头部十几个板块一起卡在 100.0、尾部一起卡在 0.0，排名失去区分度
    # （实测 496 个行业里 12 个并列 100 分、36 个并列 0 分）。
    # 这里在全截面做 min-max 线性映射：严格单调，相对次序完全不变，
    # 只是把被截断的堆积拉开。全部同分时退回 50 分。
    raws = [s["raw_score"] for s in good if s.get("raw_score") is not None]
    if raws:
        lo, hi = min(raws), max(raws)
        span = hi - lo
        for s in good:
            r = s.get("raw_score")
            if r is None:
                continue
            s["flow_score"] = 50.0 if span <= 1e-9 else (r - lo) / span * 100.0

    good.sort(key=lambda s: (-s["flow_score"], s["name"] or ""))
    # 名次在排序后写入，保证与展示顺序一致
    for i, s in enumerate(good, 1):
        s["rank"] = i
    return good, pop


# --------------------------------------------------------------------------
# 背离检测（核心卖点）
# --------------------------------------------------------------------------

def _divergence_scan(scored, lim, label, topn=12):
    """扫描"今日流入但中期流出"的板块，并做三层产出。

    判定：
      strong  = 今日 > 0 且 5日 < 0 且 10日 < 0   完整背离（10日确认）
      partial = 今日 > 0 且 5日 < 0 且 10日 >= 0  短期逆中期，尚未被 10日确认

    严重度（分位裂口）= 今日分位 - (0.6*5日分位 + 0.4*10日分位)
    即"今日有多强" 与 "中期有多弱" 的错位幅度。

    返回四元组：
      (full, strong_recs, strong_thresholded, top)
        full    : 全量命中（strong 在前），供渲染层展开/计数
        strong  : 全部完整背离记录（未过滤）
        gated   : 过双门槛（裂口≥50 且今日≥3000万），按裂口降序
        top     : ≤5 条，按"裂口 × 今日流入规模"综合排序，给人看的那栏
    """
    strong, partial = [], []

    for s in scored:
        t, d5, d10 = s["main_net"], s["main_net_5d"], s["main_net_10d"]
        st, s5 = _sign(t), _sign(d5)
        if st != 1 or s5 != -1:
            continue
        s10 = _sign(d10) if d10 is not None else None
        if s10 is None:
            continue

        p_t, p_5, p_10 = s["p_today"], s["p_5d"], s["p_10d"]
        if None in (p_t, p_5, p_10):
            continue
        mid = 0.6 * p_5 + 0.4 * p_10
        severity = p_t - mid

        # 第三层排序用的复合权重：裂口 × 流入规模。
        # 用 sqrt 压缩金额量级，否则 34 亿会直接碾压所有其它板块，
        # 让"裂口大但规模中等"的板块彻底不可见。裂口本身已归一到 0-100。
        scale = math.sqrt(max(t, 0.0) / 1e8) if t > 0 else 0.0
        composite = severity * scale

        rec = {
            "code": s["code"],
            "name": s["name"],
            "kind": s["kind"],
            "chg": s["chg"],
            "main_net": t,
            "main_net_5d": d5,
            "main_net_10d": d10,
            "intensity": s.get("intensity"),
            "p_today": p_t,
            "p_5d": p_5,
            "p_10d": p_10,
            "severity": severity,
            "composite": composite,
            "level": "strong" if s10 == -1 else "partial",
            "flow_score": s["flow_score"],
        }
        (strong if s10 == -1 else partial).append(rec)

    for lst in (strong, partial):
        lst.sort(key=lambda r: (-r["severity"], r["name"] or ""))

    if not strong and not partial:
        lim.add(f"{label}板块中未检测到『今日流入但中期流出』的背离形态")

    # 第二层：双门槛。既要裂口够大，也要今日流入的金额够实。
    gated = [
        r for r in strong
        if r["severity"] >= DIVERGENCE_MIN_SEVERITY
        and (r["main_net"] or 0) >= DIVERGENCE_MIN_AMOUNT
    ]
    if strong and not gated:
        lim.add(
            f"{label}板块有 {len(strong)} 个背离命中，但均未达到报送门槛"
            f"（裂口≥{DIVERGENCE_MIN_SEVERITY:.0f} 且今日净流入≥{DIVERGENCE_MIN_AMOUNT / 1e8:.2f}亿）"
        )
    dropped = len(strong) - len(gated)
    if dropped > 0:
        lim.add(
            f"{label}背离命中 {len(strong)} 个，剔除 {dropped} 个低裂口/小额噪声后"
            f"报送 {len(gated)} 个（门槛：裂口≥{DIVERGENCE_MIN_SEVERITY:.0f} 且今日净流入≥{DIVERGENCE_MIN_AMOUNT / 1e8:.2f}亿）"
        )

    # 第三层：给人看的 ≤5 条，按"裂口 × 规模"排序
    top = sorted(gated, key=lambda r: (-(r["composite"] or 0), r["name"] or ""))[:DIVERGENCE_TOP_N]

    # 全量层必须是"真全量"：不能截断到 topn。
    # 否则门槛层会出现全量层里没有的条目（本样本门槛 16 条 > topn 12），
    # 渲染层拿 divergence 展开时会漏条目，计数也对不上。
    full = strong + partial
    return full, strong, gated, top


def _divergence_text(rec):
    """把一条背离记录转成人类可读的一句话（纯描述，无建议）。"""
    return (
        f"{rec['name']}：今日主力{_fmt_yi(rec['main_net'])}，"
        f"但5日{_fmt_yi(rec['main_net_5d'])}、10日{_fmt_yi(rec['main_net_10d'])}，"
        f"今日分位{rec['p_today']:.0f} vs 中期分位{(0.6 * rec['p_5d'] + 0.4 * rec['p_10d']):.0f}"
    )


# --------------------------------------------------------------------------
# 情绪指标
# --------------------------------------------------------------------------

def _map_linear(v, lo, hi):
    """把 [lo, hi] 线性映射到 [0, 100]，超界饱和。None 透传。"""
    if v is None:
        return None
    if hi == lo:
        return None
    return _clamp((v - lo) / (hi - lo) * 100.0)


def _sentiment(snap, lim):
    """情绪面板：赚钱效应 / 涨跌停 / 封板率 / 连板高度 / 两融 / 主力占比。

    每个分项独立降级；综合分只对可用分项加权平均，并把权重重新归一。
    这样"跌停池明细为空"不会把综合分整体拉 0。
    """
    b = snap.get("breadth") or {}
    pool = snap.get("limit_up_pool") or []
    down_pool = snap.get("limit_down_pool") or []

    count = _num(b.get("count"))
    valid = _num(b.get("valid"))
    suspended = _num(b.get("suspended"))
    up = _num(b.get("up"))
    down = _num(b.get("down"))
    flat = _num(b.get("flat"))

    # --- 赚钱效应：上涨家数 / 有效交易家数 ---
    up_ratio = down_ratio = flat_ratio = None
    denom = valid if valid else None
    if denom is None:
        # 退回自己求和（限定只加非 None 项，缺失不补 0）
        parts = [v for v in (up, down, flat) if v is not None]
        denom = sum(parts) if parts else None
        if denom:
            lim.add("breadth.valid 缺失，赚钱效应分母改用 up+down+flat 求和")
    if denom and up is not None:
        up_ratio = up / denom * 100.0
    if denom and down is not None:
        down_ratio = down / denom * 100.0
    if denom and flat is not None:
        flat_ratio = flat / denom * 100.0
    if up_ratio is None:
        lim.add("涨跌家数缺失，赚钱效应不可计算")

    if suspended is None and count is not None and valid is not None:
        suspended = count - valid
        lim.add("breadth.suspended 缺失，已用 count-valid 推算")
    if suspended and count and suspended / count > 0.05:
        lim.add(f"停牌股 {int(suspended)} 家（占 {suspended / count * 100:.1f}%），涨跌家数已剔除")

    # --- 涨跌停 ---
    lu = _num(b.get("limit_up"))
    ld = _num(b.get("limit_down"))
    lu_src = "breadth.limit_up"
    ld_src = "breadth.limit_down"
    if lu is None:
        lu = _num(snap.get("limit_up_count"))
        lu_src = "snapshot.limit_up_count"
    if ld is None:
        ld = _num(snap.get("limit_down_count"))
        ld_src = "snapshot.limit_down_count"
    if lu is None or ld is None:
        lim.add("涨停/跌停家数缺失，涨跌停比不可用")

    if lu is not None and ld is not None:
        if ld > 0:
            limit_ratio = lu / ld
        elif lu > 0:
            limit_ratio = None
            lim.add(f"跌停家数为 0，涨停/跌停比无定义（涨停 {int(lu)} 家）")
        else:
            limit_ratio = None
    else:
        limit_ratio = None

    # --- 封板率：以涨停池内 open_times==0（全天未开板）计 ---
    seal_rate = None
    sealed = opened = None
    if not pool:
        lim.add("涨停池明细为空，封板率与连板梯队不可用")
    else:
        ot = [_num(r.get("open_times")) for r in pool]
        known = [x for x in ot if x is not None]
        if known:
            sealed = sum(1 for x in known if x == 0)
            opened = sum(1 for x in known if x > 0)
            seal_rate = sealed / len(known) * 100.0
        if len(known) < len(ot):
            lim.add(f"涨停池中 {len(ot) - len(known)} 家 open_times 缺失，封板率按已知样本计算")
        if lu is not None and lu != len(pool):
            lim.add(f"涨停计数 {int(lu)} 家与涨停池明细 {len(pool)} 家不一致，封板率以明细为准")

    # --- 连板梯队 ---
    ladder, max_height, multi_board = {}, None, None
    if pool:
        days = [_num(r.get("limit_days")) for r in pool]
        known_days = [d for d in days if d is not None]
        if known_days:
            max_height = int(max(known_days))
            multi_board = sum(1 for d in known_days if d >= 2)
            for d in known_days:
                ladder[int(d)] = ladder.get(int(d), 0) + 1
            ladder = dict(sorted(ladder.items()))
        if len(known_days) < len(days):
            lim.add(f"涨停池中 {len(days) - len(known_days)} 家连板数缺失，梯队按已知样本计算")

    # --- 跌停池明细 ---
    if ld is not None and ld > 0 and not down_pool:
        lim.add(f"跌停计数 {int(ld)} 家但跌停池明细为空，跌停结构无法拆解")

    # --- 成交额与主力净额占成交比 ---
    amount_total = _num(b.get("amount_total"))
    main_net_total = _num(b.get("main_net_total"))
    netflow_pct = None
    if amount_total and main_net_total is not None:
        netflow_pct = main_net_total / amount_total * 100.0
    if amount_total is None:
        lim.add("全市场成交额缺失，主力净额占成交比不可用")
    if main_net_total is None:
        lim.add("全市场主力净额缺失，主力净额占成交比不可用")

    # --- 成交额环比：snapshot 未提供前一交易日成交额 ---
    prev_amount = _num(snap.get("prev_amount_total"))
    amount_chg = None
    if prev_amount and amount_total:
        amount_chg = (amount_total - prev_amount) / prev_amount * 100.0
    else:
        lim.add("snapshot 未提供前一交易日成交额，成交额环比不可用（不用 0 或历史猜测填充）")

    # --- 两融 ---
    margin = snap.get("margin") or {}
    margin_balance = _num(margin.get("margin_balance"))
    margin_change = _num(margin.get("margin_change"))
    if margin_balance is None:
        lim.add("两融余额缺失")
    if margin_change is None and margin_balance is not None:
        prev = _num(margin.get("margin_balance_prev"))
        if prev is not None:
            margin_change = margin_balance - prev
        else:
            lim.add("两融前一日余额缺失，变动不可计算")
    margin_date = margin.get("date")
    if margin_date and snap.get("as_of") and margin_date < snap["as_of"]:
        lim.add(f"两融为 T+1 数据：最新披露日 {margin_date}，滞后于交易日 {snap['as_of']}")

    # --- 分项打分（0-100，越高越偏多） ---
    sub = {
        "breadth": up_ratio,
        "limit": (lu / (lu + ld) * 100.0) if (lu is not None and ld is not None and (lu + ld) > 0) else None,
        "seal": seal_rate,
        "netflow": _map_linear(netflow_pct, -NETFLOW_PCT_RANGE, NETFLOW_PCT_RANGE),
        "ladder": (_clamp(max_height / 6.0 * 100.0) if max_height is not None else None),
    }
    parts, wsum = [], 0.0
    for k, w in SENTIMENT_WEIGHTS.items():
        if sub.get(k) is not None:
            parts.append(sub[k] * w)
            wsum += w
    score = _clamp(sum(parts) / wsum) if wsum > 0 else None

    for k, w in SENTIMENT_WEIGHTS.items():
        if sub.get(k) is None:
            lim.add(f"情绪分项 {k} 不可用，综合分按剩余分项重新归一")

    # 家数类字段统一转 int：它们在语义上就是计数，浮点会污染下游
    # （render 里 `{:d}` 遇到 2108.0 直接抛 ValueError）。
    def _int(v):
        return None if v is None else int(round(v))

    return {
        "score": score,
        "score_components": sub,
        "count": _int(count),
        "valid": _int(valid),
        "suspended": _int(suspended),
        "up": _int(up),
        "down": _int(down),
        "flat": _int(flat),
        "up_ratio": up_ratio,
        "down_ratio": down_ratio,
        "flat_ratio": flat_ratio,
        "up5pct": _int(_num(b.get("up5pct"))),
        "down5pct": _int(_num(b.get("down5pct"))),
        "limit_up": _int(lu),
        "limit_down": _int(ld),
        "limit_up_source": lu_src,
        "limit_down_source": ld_src,
        "limit_ratio": limit_ratio,
        "sealed": _int(sealed),
        "opened": _int(opened),
        "seal_rate": seal_rate,
        "ladder": ladder,
        "max_height": _int(max_height),
        "multi_board": _int(multi_board),
        "amount_total": amount_total,
        "amount_chg_pct": amount_chg,
        "main_net_total": main_net_total,
        "netflow_pct": netflow_pct,
        "margin_balance": margin_balance,
        "margin_change": margin_change,
        "margin_date": margin_date,
        "fin_net_buy": _num(margin.get("fin_net_buy")),
        "short_balance": _num(margin.get("short_balance")),
    }


# --------------------------------------------------------------------------
# 市场状态（regime）：规则级联，命中即停，全程留痕
# --------------------------------------------------------------------------

def _index_series(snap, secid="1.000001"):
    """取指数主力净额日线序列。契约：klines[1] = 主力净额。"""
    flow = (snap.get("index_flow") or {}).get(secid) or {}
    series = flow.get("series") or []
    vals = [_num(r.get("main_net")) for r in series]
    dates = [r.get("date") for r in series]
    pairs = [(d, v) for d, v in zip(dates, vals) if v is not None]
    return flow.get("name"), pairs


def _regime_z(snap, lim):
    """用指数主力净额的 15 日序列算当日 z-score。

    这是"今天的主力流出，相对它自己的历史，算不算极端"的度量。
    比用绝对金额好，因为绝对金额会被市场规模漂移污染。
    """
    name, pairs = _index_series(snap)
    if not pairs:
        lim.add("index_flow 无上证指数主力净额序列，资金 z 分数不可用")
        return None, None, None, None

    today_date, today_val = pairs[-1]
    hist = [v for _, v in pairs[:-1]]
    if len(hist) < MIN_Z_SAMPLES:
        lim.add(
            f"上证主力净额历史仅 {len(hist)} 个样本（<{MIN_Z_SAMPLES}），"
            f"z 分数不可用；历史不足 1 天时该指标无意义"
        )
        return None, today_val, None, today_date

    mu = sum(hist) / len(hist)
    sd = _pstdev(hist)
    if not sd or sd == 0:
        lim.add("上证主力净额历史标准差为 0，z 分数无定义")
        return None, today_val, None, today_date
    z = (today_val - mu) / sd
    return z, today_val, mu, today_date


def _regime(sent, z, lim):
    """四态分类：进攻 / 防守 / 震荡 / 风险。

    规则按严重度从高到低级联；每条规则都记录是否命中与判定依据，
    保证结论可复核，不是黑箱打分。
    """
    up_ratio = sent.get("up_ratio")
    limit_ratio = sent.get("limit_ratio")
    netflow_pct = sent.get("netflow_pct")
    lu, ld = sent.get("limit_up"), sent.get("limit_down")

    def r(rule, cond):
        return {"rule": rule, "hit": bool(cond)}

    evaluated = []

    # 1) 风险：资金极端撤离 或 跌多涨少且跌停占优 或 主力净流出占比极端
    risk_cond = (
        (z is not None and z <= -2.0)
        or (up_ratio is not None and up_ratio < 30 and lu is not None and ld is not None and ld > lu)
        or (netflow_pct is not None and netflow_pct <= -5.0)
    )
    evaluated.append(r(
        "风险：上证主力净额 z ≤ -2.0，或下跌家数占比 >70% 且跌停多于涨停，或主力净流出占成交比 ≤ -5%",
        risk_cond,
    ))

    # 2) 进攻：赚钱效应强 + 资金净流入 + 涨停显著多于跌停
    attack_cond = (
        up_ratio is not None and up_ratio >= 60
        and z is not None and z >= 0.5
        and limit_ratio is not None and limit_ratio >= 2.0
    )
    evaluated.append(r(
        "进攻：上涨家数占比 ≥ 60% 且主力资金 z ≥ +0.5 且涨停/跌停 ≥ 2",
        attack_cond,
    ))

    # 3) 防守：资金净流出明显，或跌多涨少
    defense_cond = (
        (z is not None and z <= -1.0 and up_ratio is not None and up_ratio < 45)
        or (up_ratio is not None and up_ratio < 45 and limit_ratio is not None and limit_ratio < 1.0)
    )
    evaluated.append(r(
        "防守：主力资金 z ≤ -1.0 且上涨家数占比 < 45%，或上涨占比 < 45% 且涨停/跌停 < 1",
        defense_cond,
    ))

    # 震荡是兜底态：只有前三态都没命中时它才算"命中"。
    # 若恒记为 True，看板会出现"防守+震荡同时命中"的误导。
    fallback_hit = not (risk_cond or attack_cond or defense_cond)
    evaluated.append(r("震荡：以上规则均未命中，资金与情绪无一致方向", fallback_hit))

    label = "震荡"
    hit_rule = evaluated[-1]["rule"]
    if risk_cond:
        label, hit_rule = "风险", evaluated[0]["rule"]
    elif attack_cond:
        label, hit_rule = "进攻", evaluated[1]["rule"]
    elif defense_cond:
        label, hit_rule = "防守", evaluated[2]["rule"]
    else:
        label, hit_rule = "震荡", evaluated[-1]["rule"]

    # 记录判定所用到的具体数值，便于人工复核
    evidence = []
    if z is not None:
        evidence.append(f"上证主力净额 z={z:+.2f}")
    else:
        evidence.append("上证主力净额 z=不可用")
    if up_ratio is not None:
        evidence.append(f"上涨家数占比 {up_ratio:.1f}%")
    if limit_ratio is not None:
        evidence.append(f"涨停/跌停 {limit_ratio:.2f}")
    if netflow_pct is not None:
        evidence.append(f"主力净额占成交 {netflow_pct:+.2f}%")

    return {
        "label": label,
        "hit_rule": hit_rule,
        "evaluated": evaluated,
        "evidence": evidence,
        "z": z,
    }


# --------------------------------------------------------------------------
# 规则化判读
# --------------------------------------------------------------------------

def _rules(snap, sent, regime, div_strong, div_partial, ind_ranked, con_ranked, lim):
    """生成 3-5 条规则化判读文本。

    纯模板拼装，只陈述"数据呈现什么形态"，不含任何操作建议。
    数据不足的条目直接不生成（宁缺毋滥），而不是写一句空话。
    """
    out = []

    # 1) 市场状态 + 资金面
    z = regime.get("z")
    net = sent.get("main_net_total")
    pct = sent.get("netflow_pct")
    if net is not None:
        seg = f"全市场主力资金净额 {_fmt_yi(net)}"
        if pct is not None:
            seg += f"，占成交额 {pct:+.2f}%"
        if z is not None:
            seg += f"；上证主力净额 z 分数 {z:+.2f}"
        seg += f"，市场状态判定为「{regime['label']}」。{regime['hit_rule']}"
        out.append(seg)

    # 2) 赚钱效应与涨跌停结构
    up_ratio = sent.get("up_ratio")
    lu, ld = sent.get("limit_up"), sent.get("limit_down")
    if up_ratio is not None:
        seg = f"赚钱效应：上涨家数占比 {up_ratio:.1f}%"
        if sent.get("down"):
            seg += f"（上涨 {int(sent['up'])} / 下跌 {int(sent['down'])}"
            if sent.get("flat") is not None:
                seg += f" / 平盘 {int(sent['flat'])}"
            seg += "）"
        if lu is not None and ld is not None:
            seg += f"；涨停 {int(lu)} 家、跌停 {int(ld)} 家"
            if sent.get("limit_ratio") is not None:
                seg += f"，比值 {sent['limit_ratio']:.2f}"
        if sent.get("seal_rate") is not None:
            seg += f"；封板率 {sent['seal_rate']:.1f}%"
        if sent.get("max_height") is not None:
            seg += f"，最高连板 {sent['max_height']} 板"
        out.append(seg)

    # 3) 背离（核心卖点）—— 必须出现
    #    传入的 div_strong 已是"过双门槛"的集合，所以这里报的数字是
    #    经过裂口与规模筛选后的，不会出现"68 个行业背离"这种噪声表述。
    if div_strong:
        top = div_strong[0]
        seg = (
            f"背离警报：{len(div_strong)} 个行业出现「今日流入但中期流出」"
            f"（已按裂口与流入规模筛选）。"
            f"最典型为 {top['name']}，今日 {_fmt_yi(top['main_net'])}、"
            f"5日 {_fmt_yi(top['main_net_5d'])}、10日 {_fmt_yi(top['main_net_10d'])}，"
            f"今日分位 {top['p_today']:.0f} 而中期分位仅 "
            f"{(0.6 * top['p_5d'] + 0.4 * top['p_10d']):.0f}，"
            f"属资金在中期撤离趋势中的逆势试探形态。"
        )
        out.append(seg)
    elif div_partial:
        top = div_partial[0]
        out.append(
            f"背离警报：{len(div_partial)} 个行业「今日流入但5日流出」（10日尚未转负）。"
            f"最典型为 {top['name']}，今日 {_fmt_yi(top['main_net'])}、"
            f"5日 {_fmt_yi(top['main_net_5d'])}，中期方向未确认。"
        )
    else:
        out.append("背离警报：今日未检测到「今日流入但中期流出」的行业级背离形态。")

    # 4) 资金集中度 / 主线
    if ind_ranked:
        pos = [r for r in ind_ranked if r["main_net"] is not None and r["main_net"] > 0]
        neg = [r for r in ind_ranked if r["main_net"] is not None and r["main_net"] < 0]
        total_pos = sum(r["main_net"] for r in pos)
        total_neg = sum(r["main_net"] for r in neg)
        seg = f"资金分布：{len(pos)} 个行业净流入（合计 {_fmt_yi(total_pos)}），"
        seg += f"{len(neg)} 个行业净流出（合计 {_fmt_yi(total_neg)}）"
        top3 = [r for r in pos][:3]
        if top3:
            seg += "；流入居前为 " + "、".join(
                f"{r['name']}({_fmt_yi(r['main_net'])})" for r in top3
            )
        out.append(seg)

    # 5) 两融 / 情绪面补充
    mb, mc = sent.get("margin_balance"), sent.get("margin_change")
    if mb is not None:
        seg = f"两融余额 {_fmt_yi(mb, signed=False)}"
        if mc is not None:
            seg += f"，较前值 {_fmt_yi(mc)}"
        if sent.get("margin_date"):
            seg += f"（披露日 {sent['margin_date']}，T+1 滞后）"
        out.append(seg)

    # 夹到 3-5 条
    return out[:5]


# --------------------------------------------------------------------------
# 指数 / 外部环境
# --------------------------------------------------------------------------

def _majors(snap, lim):
    """指数块。

    snapshot.indices 是权威来源；但若为空（本样本即如此），
    退回到 index_flow 里的指数名称 + 主力净额，价格与涨跌幅标记为 None。
    **不允许**用指数资金流反推点位。
    """
    rows = []
    for r in snap.get("indices") or []:
        rows.append({
            "code": r.get("code"),
            "name": r.get("name"),
            "price": _num(r.get("price")),
            "chg": _num(r.get("chg")),
            "amount": _num(r.get("amount")),
            "net_5d": None,
            "net_10d": None,
            "source": "snapshot.indices",
        })

    flow_map = snap.get("index_flow") or {}

    if not rows:
        lim.add(
            "snapshot.indices 为空：指数点位与涨跌幅不可用。"
            "已退回 index_flow 提供指数名称与主力净额，绝不反推点位"
        )
        for sid, fl in flow_map.items():
            if not fl:
                continue
            rows.append({
                "code": sid,
                "name": fl.get("name") or sid,
                "price": None,
                "chg": None,
                "amount": None,
                "net_5d": _num(fl.get("net_5d")),
                "net_10d": _num(fl.get("net_10d")),
                "source": "snapshot.index_flow",
            })
    else:
        # indices 存在时，把指数资金流按"裸码"或"组合码"两种形式挂上去
        by_naked = {}
        for sid, fl in flow_map.items():
            if not fl:
                continue
            by_naked[str(sid).split(".")[-1]] = fl
        for r in rows:
            naked = str(r["code"] or "").split(".")[-1]
            fl = by_naked.get(naked)
            if fl:
                r["net_5d"] = _num(fl.get("net_5d"))
                r["net_10d"] = _num(fl.get("net_10d"))

    if not rows:
        lim.add("指数数据完全不可用（indices 与 index_flow 均为空）")
    return rows


def _global_block(snap, lim):
    """外部环境：把 global 列表转成对 A 股的风向判读。"""
    rows = snap.get("global") or []
    if not rows:
        lim.add("外部市场数据为空，外部环境判读不可用")
        return {"rows": [], "read": None, "risk_appetite": None}

    cats = {
        "NDX": "美股", "DJIA": "美股", "SPX": "美股",
        "HSI": "港股", "N225": "日经",
        "USDCNH": "汇率", "UDI": "汇率",
        "GC00Y": "商品", "CL00Y": "商品",
    }
    out_rows, groups = [], {}
    for r in rows:
        code = r.get("code")
        chg = _num(r.get("chg"))
        price = _num(r.get("price"))
        cat = cats.get(code, "其他")
        out_rows.append({
            "code": code, "name": r.get("name"), "price": price,
            "chg": chg, "category": cat,
        })
        if chg is not None:
            groups.setdefault(cat, []).append((r.get("name"), chg))

    us = groups.get("美股", [])
    us_avg = _mean([c for _, c in us])
    hk = groups.get("港股", [])
    hk_chg = hk[0][1] if hk else None

    # 美元兑离岸人民币上涨 = 人民币走弱，对 A 股偏逆风
    cnh = next((c for n, c in groups.get("汇率", []) if n and "人民币" in n), None)
    usd = next((c for n, c in groups.get("汇率", []) if n and "美元指数" in n), None)

    # 外部风险偏好：美股 + 港股是主要外溢来源
    parts = [v for v in (us_avg, hk_chg) if v is not None]
    risk_appetite = _mean(parts)

    segs = []
    if us_avg is not None:
        segs.append(f"隔夜美股三大指数均值 {us_avg:+.2f}%")
    if hk_chg is not None:
        segs.append(f"港股 {hk_chg:+.2f}%")
    if cnh is not None:
        segs.append(f"美元兑离岸人民币 {cnh:+.2f}%（{'人民币走弱' if cnh > 0 else '人民币走强' if cnh < 0 else '持平'}）")
    if usd is not None:
        segs.append(f"美元指数 {usd:+.2f}%")
    read = "；".join(segs) + "。" if segs else None

    if risk_appetite is not None:
        if risk_appetite > 0.5:
            read = (read or "") + "外部风险偏好偏暖，对 A 股构成正向外部背景。"
        elif risk_appetite < -0.5:
            read = (read or "") + "外部风险偏好偏冷，对 A 股构成负向外部背景。"
        else:
            read = (read or "") + "外部环境中性。"

    return {
        "rows": out_rows,
        "us_avg": us_avg,
        "hk_chg": hk_chg,
        "cnh_chg": cnh,
        "usd_chg": usd,
        "risk_appetite": risk_appetite,
        "read": read,
    }


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def compute(snapshot):
    """snapshot -> analysis。

    纯函数：不修改入参，不做任何 IO。snapshot 为 None 或非 dict 时
    返回结构完整但全部降级的 analysis（不抛异常，便于上层容错）。
    """
    lim = _Lim()

    if not isinstance(snapshot, dict):
        lim.add("snapshot 不是 dict，全部分析降级")
        snapshot = {}

    as_of = snapshot.get("as_of")
    if not as_of:
        lim.add("snapshot.as_of 缺失，交易日未知")

    for m in snapshot.get("limitations") or []:
        lim.add(f"取数层：{m}")

    # ── 空盘状态：拒绝给出任何当日结论 ─────────────────────────────
    # 盘前/非交易时段，全市场当日涨跌幅与主力净额为空。此时若照常打分，
    # 会产出"震荡""0 个行业净流入"这类看似合法的假结论（实测 2026-11-04 盘前）。
    # 正确做法：把所有当日类结论置空，只保留历史累计类指标，并置顶说明。
    # ⚠️ 只有**真正无行情**的状态才短路。
    # 早期写成 `tstate != "trading"`，把 settled（收盘已定盘）、
    # closed_after_hours（收盘后）这些完全正常的状态也当空盘处理，
    # 导致归档快照被误判"数据无效"而拒绝出结论（实测 6 条断言同时失败）。
    EMPTY_STATES = {"pre_market_or_closed"}   # 只有这个状态算"无有效行情"
    tstate = snapshot.get("trading_state")
    if tstate in EMPTY_STATES:
        lim.add(f"【空盘状态】{tstate}：当日涨跌与资金流数据无效，"
                f"已拒绝产出市场状态、板块排名、个股榜单等当日结论；"
                f"历史累计类指标（5日/10日净额、两融、股东户数）仍可用")
        empty_note = ("非交易时段采集，当日指标无效。"
                      "本页仅保留历史累计类数据，不构成任何当日行情判断。")
        return {
            "as_of": as_of,
            "generated_at": snapshot.get("generated_at"),
            "trading_state": tstate,
            "empty_state": True,
            "empty_note": empty_note,
            "majors": [],
            "breadth_block": {},
            "sentiment": {},
            "regime": {"label": "—", "hit_rule": "空盘状态，不判定",
                       "evaluated": [], "evidence": [], "z": None},
            "industries_ranked": [],
            "concepts_ranked": [],
            "quadrants": {},
            "stocks_top_in": [],
            "stocks_top_out": [],
            "divergence": [],
            "divergence_gated": [],
            "divergence_top": [],
            "divergence_partial": [],
            "margin_block": (
                lambda m: {
                    "date": m.get("date"),
                    "margin_balance": _num(m.get("margin_balance")),
                    "margin_change": _num(m.get("margin_change")),
                    "fin_net_buy": _num(m.get("fin_net_buy")),
                    "short_balance": _num(m.get("short_balance")),
                }
            )(snapshot.get("margin") or {}),
            "lhb_block": dict(snapshot.get("lhb") or {}),
            "global_block": _global_block(snapshot, lim),
            "rules": [empty_note],
            "limitations": lim.out(),
        }

    # ---- 板块 ----
    ind_ranked, _ = _rank_boards(snapshot.get("industries") or [], lim, "行业")
    con_ranked, _ = _rank_boards(snapshot.get("concepts") or [], lim, "概念")

    # ---- 四象限（行业为主，概念做补充）----
    quadrants = {}
    for q in QUADRANTS:
        rows = [r for r in ind_ranked if r["quadrant"] == q]
        if q == "流入加速":
            rows.sort(key=lambda r: (-(r["flow_score"] or 0), r["name"] or ""))
        elif q == "持续失血":
            rows.sort(key=lambda r: ((r["flow_score"] if r["flow_score"] is not None else 999), r["name"] or ""))
        elif q == "抄底试探":
            rows.sort(key=lambda r: (-(r["main_net"] or 0), r["name"] or ""))
        else:  # 拉高出货：流出最多的排前面
            rows.sort(key=lambda r: ((r["main_net"] if r["main_net"] is not None else 0), r["name"] or ""))
        # 同一个列表用多个常见键名同时暴露，保持向后兼容。
        # 背景：render_term.py 的 _as_boards() 一度只认 boards/items/list，
        # 不认契约规定的 rows，于是静默取到空 → 误判"analysis 没有象限数据"
        # → 退回用原始 snapshot 重新分桶，把上游的申万Ⅱ/Ⅲ去重和排序完全绕过
        # （表现为"保险Ⅱ/保险Ⅲ"又并列出现）。
        # 这里把 4 个键都放上，任何一版渲染层都能正确读到；成本仅为 4 个引用。
        top8 = rows[:8]
        quadrants[q] = {
            "count": len(rows),
            "rows": top8,
            "boards": top8,
            "items": top8,
            "list": top8,
        }
    unknown_q = [r for r in ind_ranked if r["quadrant"] is None]
    if unknown_q:
        u8 = unknown_q[:8]
        quadrants["未分类"] = {
            "count": len(unknown_q),
            "rows": u8, "boards": u8, "items": u8, "list": u8,
        }

    # ---- 背离 ----
    # 行业做三层产出；概念只做全量+门槛（概念板块重叠严重，不适合再出 top 榜）
    div, div_strong, div_gated, div_top = _divergence_scan(ind_ranked, lim, "行业")
    div_con, div_strong_con, _, div_con_top = _divergence_scan(con_ranked, lim, "概念", topn=8)
    for rec in div + div_top + div_con_top:
        rec["text"] = _divergence_text(rec)

    # ---- 个股 ----
    stocks = snapshot.get("stocks") or []
    stock_rows, missing_net = [], 0
    for r in stocks:
        net = _num(r.get("main_net"))
        if net is None:
            missing_net += 1
            continue
        stock_rows.append({
            "code": r.get("code"),
            "name": r.get("name"),
            "chg": _num(r.get("chg")),
            "price": _num(r.get("price")),
            "amount": _num(r.get("amount")),
            "turnover": _num(r.get("turnover")),
            "main_net": net,
            "mktcap_float": _num(r.get("mktcap_float")),
            "industry": r.get("industry"),
        })
    if missing_net:
        lim.add(f"{missing_net} 只个股主力净额缺失（含停牌），已从个股榜单剔除")
    if not stocks:
        lim.add("个股数据为空，个股资金榜不可用")
    stock_rows.sort(key=lambda r: (-r["main_net"], r["name"] or ""))
    stocks_top_in = stock_rows[:15]
    stocks_top_out = sorted(stock_rows, key=lambda r: (r["main_net"], r["name"] or ""))[:15]

    # 停牌股提示：chg 缺失但是正常交易口径
    susp = sum(1 for r in stocks if _num(r.get("chg")) is None)
    if susp:
        lim.add(f"{susp} 只个股涨跌幅缺失（停牌或当日无成交），已从涨跌统计中剔除")

    # ---- 情绪 / 状态 ----
    sent = _sentiment(snapshot, lim)
    z, today_net, hist_mu, series_date = _regime_z(snapshot, lim)
    regime = _regime(sent, z, lim)

    # ---- 指数 / 外部 ----
    majors = _majors(snapshot, lim)
    gblock = _global_block(snapshot, lim)

    # ---- 龙虎榜 ----
    lhb_raw = snapshot.get("lhb") or {}
    lhb_block = {
        "trade_date": lhb_raw.get("trade_date"),
        "count": _num(lhb_raw.get("count")),
        "net_buy": _num(lhb_raw.get("net_buy")),
        "buy": _num(lhb_raw.get("buy")),
        "sell": _num(lhb_raw.get("sell")),
    }
    if lhb_block["net_buy"] is None:
        lim.add("龙虎榜数据缺失")

    margin_block = {
        "date": sent.get("margin_date"),
        "margin_balance": sent.get("margin_balance"),
        "margin_change": sent.get("margin_change"),
        "fin_net_buy": sent.get("fin_net_buy"),
        "short_balance": sent.get("short_balance"),
    }

    breadth_block = {
        "count": sent.get("count"),
        "valid": sent.get("valid"),
        "suspended": sent.get("suspended"),
        "up": sent.get("up"),
        "down": sent.get("down"),
        "flat": sent.get("flat"),
        "up_ratio": sent.get("up_ratio"),
        "up5pct": sent.get("up5pct"),
        "down5pct": sent.get("down5pct"),
        "limit_up": sent.get("limit_up"),
        "limit_down": sent.get("limit_down"),
        "amount_total": sent.get("amount_total"),
        "main_net_total": sent.get("main_net_total"),
        "netflow_pct": sent.get("netflow_pct"),
    }

    # ---- 规则 ----
    # 规则里的"背离警报"一条用 div_gated（过门槛的），不再用全量，
    # 避免正文写"68 个行业背离"这种既噪声又吓人的表述。
    rules = _rules(snapshot, sent, regime, div_gated,
                   [r for r in div if r.get("level") == "partial"],
                   ind_ranked, con_ranked, lim)

    # ---- 数据自洽性 ----
    cons = snapshot.get("consistency") or {}
    if _num(cons.get("diff_pct")) is not None:
        d = _num(cons.get("diff_pct"))
        if d > 5:
            lim.add(f"成交额交叉校验偏差 {d:.1f}%，两市口径可能不一致")

    analysis = {
        "as_of": as_of,
        "generated_at": snapshot.get("generated_at"),
        "source": snapshot.get("source"),
        "breadth_block": breadth_block,
        "majors": majors,
        "industries_ranked": ind_ranked,
        "concepts_ranked": con_ranked,
        "quadrants": quadrants,
        "stocks_top_in": stocks_top_in,
        "stocks_top_out": stocks_top_out,
        "sentiment": sent,
        "regime": regime,
        "rules": rules,
        # ---- 背离三层 ----
        # divergence        : 全量命中（strong 在前 + partial），供渲染层展开/计数
        # divergence_strong : 全部完整背离（未过滤），按裂口降序
        # divergence_gated  : 过双门槛的（裂口≥50 且今日≥3000万），按裂口降序
        # divergence_top    : ≤5 条，按"裂口×规模"排序 —— 给人看的那栏
        # divergence_partial: 短期逆中期（10日未确认），保留向后兼容
        "divergence": div,
        "divergence_strong": div_strong,
        "divergence_gated": div_gated,
        "divergence_top": div_top,
        "divergence_partial": [r for r in div if r.get("level") == "partial"],
        "divergence_concepts": div_con,
        "divergence_concepts_top": div_con_top,
        "margin_block": margin_block,
        "lhb_block": lhb_block,
        "global_block": gblock,
        "index_flow_block": {
            "code": "1.000001",
            "name": _index_series(snapshot)[0],
            "series_date": series_date,
            "today_net": today_net,
            "hist_mean": hist_mu,
            "z": z,
        },
        "limitations": lim.out(),
        "meta": {
            "industry_count": len(snapshot.get("industries") or []),
            "concept_count": len(snapshot.get("concepts") or []),
            "stock_count": len(stocks),
            "stock_with_net": len(stock_rows),
            "quadrant_counts": {k: v["count"] for k, v in quadrants.items()},
            "divergence_count": len(div_strong),
            "divergence_gated_count": len(div_gated),
        },
    }
    return analysis


# --------------------------------------------------------------------------
# 自测（本模块唯一的 IO）
# --------------------------------------------------------------------------

def _self_test():
    import json
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "sample_snapshot.json")
    with open(path, "r", encoding="utf-8") as f:
        snap = json.load(f)

    a = compute(snap)

    def yi(v, s=True):
        return _fmt_yi(v, s)

    print("=" * 74)
    print(f"交易日 {a['as_of']}   生成于 {a['generated_at']}")
    print(f"样本：行业 {a['meta']['industry_count']} / 概念 {a['meta']['concept_count']}"
          f" / 个股 {a['meta']['stock_count']}（有主力净额 {a['meta']['stock_with_net']}）")
    print("=" * 74)

    b = a["breadth_block"]
    print(f"\n[大盘] 成交 {yi(b['amount_total'], False)}  主力净额 {yi(b['main_net_total'])}"
          f"  占成交 {b['netflow_pct']:+.2f}%" if b["netflow_pct"] is not None
          else f"\n[大盘] 成交 {yi(b['amount_total'], False)}  主力净额 {yi(b['main_net_total'])}")
    print(f"       上涨 {b['up']} / 下跌 {b['down']} / 平 {b['flat']}"
          f"  赚钱效应 {b['up_ratio']:.1f}%" if b["up_ratio"] is not None
          else f"       上涨 {b['up']} / 下跌 {b['down']}")
    print(f"       涨停 {b['limit_up']} / 跌停 {b['limit_down']}"
          f"   停牌 {b['suspended']} 家")

    r = a["regime"]
    print(f"\n[市场状态] {r['label']}")
    print(f"   命中规则：{r['hit_rule']}")
    print(f"   判定依据：{'；'.join(r['evidence'])}")
    for e in r["evaluated"]:
        print(f"     {'✅' if e['hit'] else '  '} {e['rule']}")

    s = a["sentiment"]
    print(f"\n[情绪] 综合分 {s['score']:.1f}/100" if s["score"] is not None else "\n[情绪] 综合分不可用")
    comp = ", ".join(f"{k}={v:.0f}" for k, v in s["score_components"].items() if v is not None)
    print(f"   分项：{comp}")
    print(f"   封板率 {s['seal_rate']:.1f}%（{s['sealed']}封/{s['opened']}开）"
          if s["seal_rate"] is not None else "   封板率 不可用")
    print(f"   连板梯队 {s['ladder']}  最高 {s['max_height']} 板  2板以上 {s['multi_board']} 家"
          if s["ladder"] else "   连板梯队 不可用")
    if s["margin_balance"] is not None:
        print(f"   两融 {yi(s['margin_balance'], False)}  变动 {yi(s['margin_change'])}"
              f"（披露日 {s['margin_date']}）")

    print(f"\n[四象限] 计数 {a['meta']['quadrant_counts']}")
    for q in QUADRANTS:
        rows = a["quadrants"][q]["rows"][:4]
        names = "、".join(f"{x['name']}({yi(x['main_net'])})" for x in rows)
        print(f"   {q:<5}({a['quadrants'][q]['count']:>3}个) {names}")

    print(f"\n[资金强度 TOP8 行业]")
    for i, x in enumerate(a["industries_ranked"][:8], 1):
        print(f"   {i:>2}. {x['name']:<12} score={x['flow_score']:5.1f} "
              f"今日={yi(x['main_net']):>10} 5日={yi(x['main_net_5d']):>10} "
              f"10日={yi(x['main_net_10d']):>11} {x['quadrant']}")
        print(f"       分位 今日{x['p_today']:5.1f}/5日{x['p_5d']:5.1f}/10日{x['p_10d']:5.1f}"
              f"  一致性调整{x['consistency_adj']:+.1f}  {x['consistency_note']}")

    print(f"\n[★ 背离检测] 今日流入但中期流出 —— 三层产出")
    print(f"   第一层 全量命中       : {len(a['divergence_strong'])} 个完整背离"
          f" + {len(a['divergence_partial'])} 个部分背离（10日未确认）")
    print(f"   第二层 过双门槛       : {len(a['divergence_gated'])} 个"
          f"（裂口≥{DIVERGENCE_MIN_SEVERITY:.0f} 且今日净流入≥{DIVERGENCE_MIN_AMOUNT / 1e8:.2f}亿）")
    print(f"   第三层 给人看的 TOP{len(a['divergence_top'])} : 按『裂口×流入规模』排序")
    for i, x in enumerate(a["divergence_top"], 1):
        print(f"   {i}. {x['name']:<12} 今日={yi(x['main_net']):>10}"
              f" 5日={yi(x['main_net_5d']):>10} 10日={yi(x['main_net_10d']):>11}")
        print(f"       裂口={x['severity']:5.1f} 综合={x['composite']:7.1f}"
              f" 今日分位={x['p_today']:5.1f} 涨跌={x['chg']}%")
    print(f"   —— 第二层完整清单（按裂口降序）——")
    for i, x in enumerate(a["divergence_gated"], 1):
        print(f"   {i:>2}. {x['name']:<12} 裂口={x['severity']:5.1f}"
              f" 今日={yi(x['main_net']):>10}")

    print(f"\n[个股流入 TOP5]")
    for x in a["stocks_top_in"][:5]:
        print(f"   {x['name']:<10} 净额={yi(x['main_net']):>10} 涨跌={x['chg']}%")
    print(f"[个股流出 TOP5]")
    for x in a["stocks_top_out"][:5]:
        print(f"   {x['name']:<10} 净额={yi(x['main_net']):>10} 涨跌={x['chg']}%")

    print(f"\n[指数]")
    for m in a["majors"]:
        net = f"5日{yi(m['net_5d'])} 10日{yi(m['net_10d'])}" if m["net_5d"] is not None else ""
        print(f"   {m['name']:<8} 点位={m['price']} 涨跌={m['chg']}  {net}")

    print(f"\n[外部环境] {a['global_block']['read']}")

    print(f"\n[规则判读]")
    for i, t in enumerate(a["rules"], 1):
        print(f"   {i}. {t}")

    print(f"\n[limitations] 共 {len(a['limitations'])} 条")
    for t in a["limitations"]:
        print(f"   - {t}")

    # ---- 边界用例 ----
    print("\n" + "=" * 74)
    print("边界用例")
    print("=" * 74)

    # 1) 空 snapshot
    e = compute({})
    assert isinstance(e["limitations"], list) and e["limitations"]
    assert e["industries_ranked"] == [] and e["regime"]["label"] == "震荡"
    print(f"1) 空 dict        -> 不崩，{len(e['limitations'])} 条 limitations，regime={e['regime']['label']}")

    # 2) 停牌股 chg = "-" 字符串
    e = compute({
        "as_of": "2026-11-03",
        "stocks": [
            {"code": "1", "name": "停牌股", "chg": "-", "main_net": "-", "amount": "-"},
            {"code": "2", "name": "正常股", "chg": 3.2, "main_net": 1e8, "amount": 2e8},
        ],
        "breadth": {"count": 2, "valid": 1, "up": 1, "down": 0, "flat": 0},
    })
    assert len(e["stocks_top_in"]) == 1 and e["stocks_top_in"][0]["name"] == "正常股"
    print(f"2) 停牌股 '-'     -> 已剔除，榜单仅 {len(e['stocks_top_in'])} 只，未填 0")

    # 3) 板块为空
    e = compute({"as_of": "2026-11-03", "industries": [], "concepts": [], "stocks": []})
    assert e["industries_ranked"] == [] and e["quadrants"]["流入加速"]["count"] == 0
    print(f"3) 板块全空       -> 不崩，四象限计数归零，{len(e['limitations'])} 条 limitations")

    # 4) 只有一天历史
    e = compute({
        "as_of": "2026-11-03",
        "index_flow": {"1.000001": {"name": "上证指数", "net_5d": -1e10, "net_10d": None,
                                    "series": [{"date": "2026-11-03", "main_net": -1e10}]}},
    })
    assert e["index_flow_block"]["z"] is None
    assert any("样本" in t for t in e["limitations"])
    print(f"4) 仅1天历史      -> z=None（未假装 0），regime={e['regime']['label']}")

    # 5) 单板块截面（分位退化）
    e = compute({"as_of": "2026-11-03", "industries": [
        {"code": "BK1", "name": "唯一板块", "main_net": 1e8, "main_net_5d": -1e8, "main_net_10d": -5e8,
         "mktcap_float": 1e10, "amount": 5e9, "chg": 1.0}
    ]})
    only = e["industries_ranked"][0]
    assert 0 <= only["flow_score"] <= 100
    print(f"5) 单板块         -> score={only['flow_score']:.1f} 象限={only['quadrant']}（分位退化到50仍可用）")

    # 6) 关键断言：电力设备必须被识别为背离
    elec = next((x for x in a["industries_ranked"] if x["name"] == "电力设备"), None)
    assert elec is not None, "电力设备缺失"
    assert elec["quadrant"] == "抄底试探", f"电力设备象限错误: {elec['quadrant']}"
    assert elec["consistency_adj"] < 0, "电力设备未触发背离扣分"
    div_names = [x["name"] for x in a["divergence_strong"]]
    assert "电力设备" in div_names, "电力设备未进入背离榜"
    print(f"6) 核心断言       -> 电力设备 象限={elec['quadrant']} "
          f"score={elec['flow_score']:.1f} 调整={elec['consistency_adj']:+.1f} 已在背离榜")

    # 7) 无建议措辞
    banned = ("建议", "买入", "卖出", "加仓", "减仓", "推荐", "目标价", "看多", "看空")
    blob = " ".join(a["rules"])
    assert not any(w in blob for w in banned), "规则文本含投资建议措辞"
    print(f"7) 措辞检查       -> {len(a['rules'])} 条规则无非建议措辞")

    # 8) 结论自洽：亏损日不该判进攻
    assert a["regime"]["label"] != "进攻"
    print(f"8) 自洽性         -> 净流出日 regime={a['regime']['label']}（非进攻）")

    # 9) 背离三层：全量 ⊇ 门槛层 ⊇ TOP，且 TOP 至多 5 条
    full_names = {r["name"] for r in a["divergence"]}
    gated_names = {r["name"] for r in a["divergence_gated"]}
    top_names = [r["name"] for r in a["divergence_top"]]
    assert gated_names <= full_names, "门槛层出现全量层没有的板块"
    assert set(top_names) <= gated_names, "TOP 层出现门槛层没有的板块"
    assert len(a["divergence_top"]) <= DIVERGENCE_TOP_N, "TOP 超过 5 条"
    for r in a["divergence_gated"]:
        assert r["severity"] >= DIVERGENCE_MIN_SEVERITY, f"{r['name']} 裂口未过门槛"
        assert r["main_net"] >= DIVERGENCE_MIN_AMOUNT, f"{r['name']} 流入额未过门槛"
    assert a["divergence_top"][0]["name"] == "电力设备", "TOP 首位应是电力设备"
    print(f"9) 背离分层       -> 全量{len(a['divergence'])} ⊇ 门槛{len(a['divergence_gated'])}"
          f" ⊇ TOP{len(a['divergence_top'])}，门槛与排序均自洽")

    # 10) 四象限多键兼容（Lead 报的 bug：render_term 只认 boards/items/list）
    for q in QUADRANTS:
        cell = a["quadrants"][q]
        for key in ("rows", "boards", "items", "list"):
            assert key in cell, f"象限 {q} 缺键 {key}"
            assert cell[key] == cell["rows"], f"象限 {q} 的 {key} 与 rows 不一致"
    print(f"10) 象限兼容      -> 4 个键 (rows/boards/items/list) 齐备且同源")

    # 11) 家数类字段必须是 int（下游 {:d} 会崩在 float 上）
    s0 = a["sentiment"]
    for f in ("count", "valid", "suspended", "up", "down", "flat",
              "up5pct", "down5pct", "limit_up", "limit_down",
              "sealed", "opened", "max_height", "multi_board"):
        v = s0.get(f)
        assert v is None or isinstance(v, int), f"{f} 不是 int: {type(v).__name__}={v!r}"
    print(f"11) 计数类型      -> 家数字段全部为 int 或 None，无 float 污染")

    # 12) 纯函数：不修改入参
    import copy
    snap2 = copy.deepcopy(snap)
    compute(snap2)
    assert snap2 == snap, "compute() 修改了入参 snapshot"
    print(f"12) 纯函数性      -> compute() 未修改入参 snapshot")

    print("\n全部自测通过 ✅")


if __name__ == "__main__":
    _self_test()
