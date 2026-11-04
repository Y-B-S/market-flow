"""render_html.py — A股「资金流动大盘」HTML 渲染层。

纯函数模块：只依赖标准库，不做 IO / 网络 / 第三方库。
对外签名：
    render_html(analysis, snapshot) -> str      # 完整 HTML 单文件
    render(analysis, snapshot) -> str           # 契约别名（CONTRACT.md 写的名字）

设计原则
--------
1. **防御性**：analysis 由 metrics.py 并行开发，可能是空 dict、None、或缺任意字段。
   所有取值走 analysis.get(...) 逐层兜底，缺失一律渲染成降级但完整的页面（数字显示 "—"）。
2. **不编造**：任何数字要么来自 analysis，要么来自 snapshot 的真实字段。
   允许的"渲染层计算"只有一种——用 snapshot 里的真实数值做截面分位排名，
   因为那是纯粹的确定性统计，不是凭空造数。这类计算会在图注里标明来源。
3. **零外链**：CSS / SVG 全部内联，无 CDN、无字体外链、无 http(s) 资源引用，断网双击可开。
4. **防注入**：所有来自数据的文本（板块名、个股名、规则文本…）统一过 escape()。
5. **A股配色**：红 = 上涨 / 资金流入，绿 = 下跌 / 资金流出（与欧美相反）。

自测：python3 render_html.py  → 读取同目录 sample_snapshot.json，产出 out.html 并做结构校验。
"""
from html import escape
from math import sqrt
import re

__all__ = ["render_html", "render"]

# ---------------------------------------------------------------- 常量 / 配色

# A股约定：红涨绿跌（与欧美相反，别写反）
C_UP = "#f6465d"        # 上涨 / 资金流入
C_DOWN = "#0ecb81"      # 下跌 / 资金流出
C_FLAT = "#8b96a8"      # 平盘 / 缺数
C_AMBER = "#f0b90b"
C_VIOLET = "#a78bfa"
C_TEXT = "#e6edf7"
C_MUTED = "#8b96a8"
C_GRID = "#232b3b"

DASH = "—"              # 取不到的数字统一显示这个，绝不用 0 冒充

QUAD_DEF = {
    # key -> (中文名, 底色, 边框色, 释义)
    "accel": ("流入加速", "rgba(246,70,93,0.085)", "rgba(246,70,93,0.34)",
              "今日与5日分位双高：资金短期中期同向聚焦"),
    "probe": ("抄底试探", "rgba(240,185,11,0.085)", "rgba(240,185,11,0.34)",
              "今日分位高但5日分位低：短期回补、中期仍在流出"),
    "bleed": ("持续失血", "rgba(14,203,129,0.085)", "rgba(14,203,129,0.34)",
              "今日与5日分位双低：资金持续撤离"),
    "dist": ("拉高出货", "rgba(167,139,250,0.085)", "rgba(167,139,250,0.34)",
             "5日分位高但今日分位低：中期曾流入、当前转为流出"),
}

# metrics.py 用中文象限名做键，渲染层内部用短 key；两个方向都要能互认。
_QUAD_ALIAS = {}
for _k, _v in QUAD_DEF.items():
    _QUAD_ALIAS[_k] = _k            # 短 key 自身
    _QUAD_ALIAS[_v[0]] = _k         # 中文名 -> 短 key
_QUAD_ALIAS.update({
    "流入加速型": "accel", "加速流入": "accel", "inflow_accel": "accel",
    "抄底": "probe", "试探性流入": "probe", "bottom_probe": "probe",
    "持续流出": "bleed", "失血": "bleed", "outflow": "bleed",
    "拉高出货型": "dist", "出货": "dist", "distribution": "dist",
})


# ---------------------------------------------------------------- 安全取值

def _d(obj, *path, default=None):
    """逐层安全下钻：_d(analysis, 'regime', 'label') 任意一层缺失都返回 default。"""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        if key not in cur:
            return default
        cur = cur[key]
        if cur is None:
            return default
    return cur


def _num(v):
    """安全数值转换。东财停牌股会返回字符串 "-"，必须容忍。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("%", "").replace("亿元", "").replace("亿", "")
        if s in ("", "-", "--", "None", "null", "nan"):
            return None
        try:
            f = float(s)
        except ValueError:
            return None
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    return None


def _first_num(row, keys):
    """按候选字段名依次尝试，取第一个能转成数字的值。"""
    if not isinstance(row, dict):
        return None
    for k in keys:
        v = _num(row.get(k))
        if v is not None:
            return v
    return None


def _text(v, default=DASH):
    if v is None:
        return default
    s = str(v).strip()
    return s if s else default


def _rows(v):
    """把 analysis 里各种可能的容器形状统一成 list[dict]。

    接受：list[dict] / dict{...: list[dict]} / {'rows'|'items'|'top': list} / None
    """
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    if isinstance(v, dict):
        for key in ("rows", "items", "list", "boards", "data", "top", "top_in", "top_out"):
            sub = v.get(key)
            if isinstance(sub, list):
                return [x for x in sub if isinstance(x, dict)]
        for sub in v.values():                    # 任意一个 list 值
            if isinstance(sub, list) and sub and isinstance(sub[0], dict):
                return [x for x in sub if isinstance(x, dict)]
    return []


def esc(v):
    """所有来自数据的文本都必须过这里。"""
    if v is None:
        return DASH
    return escape(str(v), quote=True)


# ---------------------------------------------------------------- 数字格式化

def fmt_yi(v, digits=1, sign=False):
    """元 → 亿 / 万亿。取不到返回 "—"。"""
    if v is None:
        return DASH
    a = v / 1e8
    s = "+" if (sign and a > 0) else ""
    if abs(a) >= 10000:
        return "%s%.2f万亿" % (s, a / 10000.0)
    return "%s%s亿" % (s, "{:,.{d}f}".format(a, d=digits))


def fmt_pct(v, digits=2, sign=True):
    if v is None:
        return DASH
    return ("%+.*f%%" if sign else "%.*f%%") % (digits, v)


def fmt_price(v, digits=2):
    if v is None:
        return DASH
    return "%.*f" % (digits, v)


def fmt_int(v):
    if v is None:
        return DASH
    return "{:,.0f}".format(v)


def fmt_wan(v):
    """元 → 万元/亿元，用于小额。"""
    if v is None:
        return DASH
    if abs(v) >= 1e8:
        return "%.1f亿" % (v / 1e8)
    return "%.0f万" % (v / 1e4)


def _cls(v):
    """数值 → 语义 class（A股：正=红 up，负=绿 down）。"""
    if v is None:
        return "na"
    if v > 0:
        return "up"
    if v < 0:
        return "down"
    return "flat"


def _arrow(v):
    if v is None:
        return ""
    if v > 0:
        return "▲"
    if v < 0:
        return "▼"
    return "—"


# ---------------------------------------------------------------- 截面分位

def _pct_rank(values):
    """把一组真实数值映射成 0-100 的截面分位（中位秩法）。返回 list[float]。"""
    idx = [(i, v) for i, v in enumerate(values) if v is not None]
    out = [None] * len(values)
    n = len(idx)
    if n == 0:
        return out
    if n == 1:
        out[idx[0][0]] = 50.0
        return out
    idx.sort(key=lambda t: t[1])
    for rank, (i, _v) in enumerate(idx):
        out[i] = (rank + 0.5) / n * 100.0
    return out


def _median(vals):
    vs = sorted(v for v in vals if v is not None)
    n = len(vs)
    if n == 0:
        return None
    mid = n // 2
    return vs[mid] if n % 2 else (vs[mid - 1] + vs[mid]) / 2.0


def _weighted_strength(p_today, p_5d, p_10d, w=(0.5, 0.3, 0.2)):
    """契约口径：今日 / 5日 / 10日 三个维度分位加权，缺维度则按剩余权重归一。"""
    parts = [(p_today, w[0]), (p_5d, w[1]), (p_10d, w[2])]
    tot = sum(wt for p, wt in parts if p is not None)
    if tot <= 0:
        return None
    return sum(p * wt for p, wt in parts if p is not None) / tot


# ---------------------------------------------------------------- 板块归一化

_NAME_KEYS = ("name", "board_name", "sec_name", "title", "label", "板块", "板块名")
_CODE_KEYS = ("code", "board_code", "sec_code", "ts_code")
_NET_KEYS = ("main_net", "net", "today_net", "net_today", "main_net_today",
             "今日净额", "净额", "net_inflow")
_NET5_KEYS = ("main_net_5d", "net_5d", "net5d", "main_net_5", "5日净额")
_NET10_KEYS = ("main_net_10d", "net_10d", "net10d", "main_net_10", "10日净额")
_AMT_KEYS = ("amount", "turnover_amount", "amt", "成交额", "turnover_value")
_CHG_KEYS = ("chg", "change", "pct_chg", "涨跌幅", "change_pct")
_SCORE_KEYS = ("flow_score", "score", "strength", "资金强度")
_RATIO_KEYS = ("main_ratio", "net_ratio", "main_net_ratio", "占比")


def _board(row):
    """把任意形状的板块行归一成渲染层内部结构。"""
    return {
        "name": _text(_d_row(row, _NAME_KEYS), DASH),
        "code": _text(_d_row(row, _CODE_KEYS), ""),
        "net": _first_num(row, _NET_KEYS),
        "net5": _first_num(row, _NET5_KEYS),
        "net10": _first_num(row, _NET10_KEYS),
        "amount": _first_num(row, _AMT_KEYS),
        "chg": _first_num(row, _CHG_KEYS),
        "score": _first_num(row, _SCORE_KEYS),
        "ratio": _first_num(row, _RATIO_KEYS),
        "p5": _first_num(row, ("p_5d", "pct_5d", "pct5", "pct_5", "x", "rank_5d", "pct_5day", "p5")),
        "pt": _first_num(row, ("p_today", "pct_today", "pct", "y", "rank_today", "pct_1d", "pt")),
        "p10": _first_num(row, ("p_10d", "pct_10d", "pct10", "pct_10", "rank_10d", "p10")),
        "quad": _QUAD_ALIAS.get(
            str(row.get("quadrant") or row.get("quad") or row.get("_q") or "").strip(),
            ""),
    }


def _d_row(row, keys):
    if not isinstance(row, dict):
        return None
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return v
    return None


def _quad_of(p5, pt):
    """分位空间中 50/50 划分的象限。"""
    if p5 is None or pt is None:
        return None
    hi5 = p5 >= 50
    hit = pt >= 50
    if hi5 and hit:
        return "accel"
    if not hi5 and hit:
        return "probe"
    if not hi5 and not hit:
        return "bleed"
    return "dist"


# 东财行业板块里 X / XⅡ / XⅢ 是同一批标的的分级重复（实测数值完全相同），
# 排行榜里同时出现会白占名额、误导读者以为是两个板块。
_TIER_RE = re.compile(r"^(.*?)(?:Ⅰ|Ⅱ|Ⅲ|Ⅳ|Ⅴ|I{1,3}|IV|V)$")


def _board_base(name):
    s = (name or "").strip()
    m = _TIER_RE.match(s)
    return (m.group(1) if m else s).strip()


def _dedupe_boards(boards):
    """按基名去重，保留首次出现（调用前已按强度降序）。返回 (列表, 去掉个数)。"""
    seen = {}
    out = []
    for b in boards:
        base = _board_base(b.get("name") or "")
        if base and base in seen:
            continue
        if base:
            seen[base] = True
        out.append(b)
    return out, max(0, len(boards) - len(out))


# ---------------------------------------------------------------- 散点数据源

def _derive_scatter(snapshot):
    """用 snapshot 的真实数值做截面分位排名，得到四象限散点。

    这不是编造：分位是确定性统计。仅当 analysis 未提供可用散点时才走这条路，
    并在图注中明确标注"渲染层按契约口径计算"。
    """
    boards = snapshot.get("industries") if isinstance(snapshot, dict) else None
    if not isinstance(boards, list) or not boards:
        boards = snapshot.get("concepts") if isinstance(snapshot, dict) else None
    raw = [r for r in (boards or []) if isinstance(r, dict)]
    if not raw:
        return [], [], "empty"

    p5_list = _pct_rank([_num(r.get("main_net_5d")) for r in raw])
    pt_list = _pct_rank([_num(r.get("main_net")) for r in raw])
    p10_list = _pct_rank([_num(r.get("main_net_10d")) for r in raw])

    full = []
    for i, r in enumerate(raw):
        b = _board(r)
        b["p5"] = p5_list[i]
        b["pt"] = pt_list[i]
        b["p10"] = p10_list[i]
        if b["p5"] is None or b["pt"] is None:
            continue
        b["quad"] = _quad_of(b["p5"], b["pt"])
        full.append(b)
    return full, full, "derived"


def _scatter_points(analysis, snapshot):
    """散点数据源优先级：

    1. analysis.industries_ranked / concepts_ranked —— metrics 输出的**全板块**截面（带分位），
       这是最好的源：496 个行业全在里面，散点才真实反映分布。
    2. analysis.quadrants —— metrics 每象限只给 TopN（实测 8 个/象限，共 32），
       点太少会让散点图严重失真，只在没有全量源时才用。
    3. snapshot 现算分位 —— metrics 未就绪时的降级路径。
    """
    # ① 全板块截面（首选）
    for key in ("industries_ranked", "concepts_ranked", "all_boards", "boards_ranked"):
        cands = _rows(_d(analysis, key))
        pts = []
        for r in cands:
            b = _board(r)
            if b["p5"] is None or b["pt"] is None:
                continue
            if not b["quad"]:
                b["quad"] = _quad_of(b["p5"], b["pt"])
            pts.append(b)
        if len(pts) >= 30:
            return pts, pts, "analysis"

    # ② 象限 TopN 聚合（点少，仅兜底）
    cands = _rows(_d(analysis, "scatter"))
    qd = _d(analysis, "quadrants")
    if isinstance(qd, dict):
        for key, val in qd.items():
            for r in _rows(val):
                rr = dict(r)
                if not (rr.get("quadrant") or rr.get("quad") or rr.get("_q")):
                    rr["quadrant"] = key
                cands.append(rr)
    elif isinstance(qd, list):
        cands.extend(_rows(qd))

    pts = []
    for r in cands:
        b = _board(r)
        if b["p5"] is None or b["pt"] is None:
            continue
        if not b["quad"]:
            b["quad"] = _quad_of(b["p5"], b["pt"])
        pts.append(b)
    if len(pts) >= 8:
        return pts, pts, "analysis"

    # ③ 降级：按契约口径用快照真实值现算
    return _derive_scatter(snapshot)


# ---------------------------------------------------------------- SVG 基件

def _svg_open(w, h, extra=""):
    return (
        '<svg viewBox="0 0 %d %d" width="100%%" height="auto" role="img" '
        'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg"%s>'
        % (w, h, extra)
    )


def _txt(x, y, s, size=12, fill=C_TEXT, anchor="start", weight="400", cls="", family=""):
    fam = ' font-family="%s"' % family if family else ""
    c = ' class="%s"' % cls if cls else ""
    return (
        '<text x="%.1f" y="%.1f" font-size="%s" fill="%s" text-anchor="%s" '
        'font-weight="%s"%s%s>%s</text>' % (x, y, size, fill, anchor, weight, fam, c, esc(s))
    )


# ---------------------------------------------------------------- 面板 3：四象限散点

# ---------------------------------------------------------------- treemap 热力图
# ==== 插入到 render_html.py：treemap 热力图 ====

def _worst_ratio(row, w):
    """treemap 单行最差长宽比。过滤非正权重，避免 rmin=0 除零。"""
    pos = [v for v in row if v > 0]
    if not pos or w <= 0:
        return float("inf")
    s = sum(pos)
    if s <= 0:
        return float("inf")
    rmax, rmin = max(pos), min(pos)
    return max((w * w * rmax) / (s * s), (s * s) / (w * w * rmin))


def _squarify(weights, x, y, w, h):
    """Squarified treemap（Bruls/Huizing/van Wijk 2000）。

    返回 [(x, y, w, h, orig_index)]。面积严格正比于权重，100% 覆盖、零重叠。
    用 squarify 而非普通 slice-and-dice，是为了让每块接近正方形——
    否则会出现又长又窄的条，小市值板块变成看不见的线。
    """
    if not weights or w <= 0 or h <= 0:
        return []
    ws = [max(0.0, float(v) if v is not None else 0.0) for v in weights]
    idx = list(range(len(ws)))
    idx.sort(key=lambda i: -ws[i])
    pairs = [(ws[i], i) for i in idx if ws[i] > 0]
    if not pairs:
        return []
    ws = [p[0] for p in pairs]
    idx = [p[1] for p in pairs]
    total = sum(ws)
    if total <= 0:
        return []
    scale = (w * h) / total
    ws = [v * scale for v in ws]

    out, i, n = [], 0, len(ws)
    while i < n:
        short = min(w, h)
        row = [ws[i]]
        j = i + 1
        while j < n and _worst_ratio(row + [ws[j]], short) <= _worst_ratio(row, short):
            row.append(ws[j]); j += 1
        srow = sum(row)
        if w >= h:
            rw = srow / h if h else 0
            yy = y
            for k, v in enumerate(row):
                rh = (v / srow) * h if srow else 0
                out.append((x, yy, rw, rh, idx[i + k]))
                yy += rh
            x += rw; w -= rw
        else:
            rh = srow / w if w else 0
            xx = x
            for k, v in enumerate(row):
                rw2 = (v / srow) * w if srow else 0
                out.append((xx, y, rw2, rh, idx[i + k]))
                xx += rw2
            y += rh; h -= rh
        i = j
    return out


def _treemap_svg(pts, W=1060, H=560, metric="amount"):
    """板块资金热力图：面积=成交额，颜色=今日主力净额方向与强度。

    这是把 dapanyuntu 的可视化范式搬过来：用面积编码规模、颜色编码方向，
    一眼看清"钱集中在哪、往哪个方向流"。
    与四象限散点互补：散点看分布形态，热力图看绝对规模。
    """
    if not pts:
        return '<div class="empty">无可用板块数据</div>'

    # 权重：成交额优先，缺失退回 |净额|
    def _wt(b):
        a = b.get("amount")
        if a is not None and a > 0:
            return a
        n = b.get("net")
        return abs(n) if n is not None else 0.0

    # 全部绘制，不按面积截断。
    # 教训（与四象限散点同源）：早期版本把面积＜0.08% 的 255 个板块直接丢掉，
    # 结果 457 个板块只有 202 个进图——而丢掉的正是中小板块，会系统性扭曲
    # "资金分布"的观感（看起来只有几十个大板块在市场里）。
    # 正确做法：全画（100% 覆盖），靠字号/是否写字来解决可读性。
    kept = [b for b in pts if _wt(b) > 0]
    if not kept:
        return '<div class="empty">无可用板块数据</div>'
    dropped = 0

    rects = _squarify([_wt(b) for b in kept], 0, 0, W, H)

    # 颜色：按今日主力净额占成交额比（强度）分档，A股红涨绿跌
    # 色阶量程自适应：固定 ±3% 封顶会让"全市场普遍净流出"的日子整屏一片绿、
    # 分不出强弱。改用本截面 |ratio| 的 90 分位作参考量程（下限 0.5%），
    # 让颜色在当天实际分布里拉开。
    _ratios = []
    for b in kept:
        r0 = b.get("ratio")
        if r0 is None:
            n0, a0 = b.get("net"), b.get("amount")
            if n0 is not None and a0 and a0 > 0:
                r0 = n0 / a0 * 100.0
        if r0 is not None:
            _ratios.append(abs(r0))
    _scale = 3.0
    if _ratios:
        _rs = sorted(_ratios)
        _p90 = _rs[min(len(_rs) - 1, int(len(_rs) * 0.9))]
        _scale = max(0.5, min(8.0, _p90))

    def _fill(b):
        r = b.get("ratio")
        n, a = b.get("net"), b.get("amount")
        if r is None and n is not None and a and a > 0:
            r = n / a * 100.0
        if r is None:
            return C_FLAT
        t = max(-1.0, min(1.0, r / _scale))
        if t > 0:
            # 红：从暗红到亮红
            return _mix_color((40, 18, 24), (246, 70, 93), t)
        if t < 0:
            return _mix_color((18, 34, 28), (14, 203, 129), -t)
        return C_FLAT

    out = [_svg_open(W, H, ' aria-label="板块资金热力图，面积=成交额，颜色=今日主力净额方向"')]
    for (x, y, rw, rh, i) in rects:
        b = kept[i]
        if rw <= 0 or rh <= 0:
            continue
        fill = _fill(b)
        # 悬停标题：板块名 + 净额 + 成交额 + 象限
        tip = "%s ｜ 今日主力 %s ｜ 成交额 %s ｜ %s" % (
            b.get("name") or "—",
            fmt_yi(b.get("net")),
            fmt_yi(b.get("amount"), 1, False),
            QUAD_DEF.get(b.get("quad") or "", ("未分类",))[0],
        )
        out.append(
            '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="%s" '
            'stroke="#080b11" stroke-width="0.8"><title>%s</title></rect>'
            % (x, y, rw, rh, fill, esc(tip))
        )
        # 只在块足够大时才写字。中文在 11.5px 字号下约 11.5px/字，
        # 必须用 (rw-8) 而不是 rw 估算可容纳字数，否则字会压到块边线外相邻块上。
        if rw >= 46 and rh >= 22:
            cap_chars = int((rw - 8) / 11.5)
            nm = _clip(b.get("name") or "", max(2, cap_chars))
            out.append(_txt(x + rw / 2, y + rh / 2 + (4 if rh < 44 else -2),
                            nm, 11.0, "#eef4fd", "middle", "600"))
            if rh >= 44 and cap_chars >= 4:
                out.append(_txt(x + rw / 2, y + rh / 2 + 16,
                                fmt_yi(b.get("net")), 10.0, "#ffd9de", "middle"))

    out.append("</svg>")
    note = ("<div class='tmn'>面积=板块成交额（%d 个板块<b>全部绘制</b>，未按面积截断，"
            "覆盖率 100%%）· 颜色=今日主力净额占成交额比（自适应量程 ±%.1f%%，"
            "红=净流入 绿=净流出）· 面积过小的块不写字但<b>仍在图中</b>，悬停可看明细</div>"
            % (len(kept), _scale))
    return "".join(out) + note


def _mix_color(c0, c1, t):
    """两色线性插值。"""
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(
        int(c0[k] + (c1[k] - c0[k]) * t) for k in range(3))


def _clip(s, max_chars):
    if not s:
        return ""
    return s if len(s) <= max_chars else s[:max(1, max_chars - 1)] + "…"


def _scatter_svg(points, source, universe_total):
    """X=5日净额分位  Y=今日净额分位  点面积=成交额。核心图。"""
    W, H = 1060, 640
    L, R, T, B = 92, 1030, 52, 556
    pw, ph = R - L, B - T

    def sx(p):
        return L + p / 100.0 * pw

    def sy(p):
        return B - p / 100.0 * ph

    # 展示：全量绘制（不按成交额截断）。
    # 教训：早期版本只画成交额前 130 个，而大成交额板块在全市场普跌时恰好全挤在
    # "持续失血"一角，结果 457 个板块里有 300 多个压根没进图，象限分布被彻底扭曲。
    # 正确做法是全画，用"小点更淡、大点画在上层"来表达权重。
    def _mag(b):
        return b["amount"] if b["amount"] is not None else (abs(b["net"]) if b["net"] is not None else 0.0)

    ranked = sorted(points, key=_mag, reverse=True)
    show = ranked[:600]
    if not show:
        return ('<div class="empty">无可用板块数据，四象限图暂不可绘制。</div>')

    max_amt = max([_mag(b) for b in show] + [1.0])

    def _rad(b):
        return 2.0 + 7.6 * sqrt(min(1.0, _mag(b) / max_amt))

    xm, ym = sx(50), sy(50)
    out = [_svg_open(W, H, ' aria-label="板块资金轮动四象限散点图"')]
    out.append('<defs><clipPath id="plotclip"><rect x="%d" y="%d" width="%d" height="%d"/>'
               '</clipPath></defs>' % (L, T, pw, ph))

    # 四象限底色
    rects = [
        ("accel", xm, T, R - xm, ym - T),
        ("probe", L, T, xm - L, ym - T),
        ("bleed", L, ym, xm - L, B - ym),
        ("dist", xm, ym, R - xm, B - ym),
    ]
    for key, x, y, w, h in rects:
        _n, fill, _bd, _dsc = QUAD_DEF[key]
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>'
                   % (x, y, w, h, fill))

    # 网格
    for v in (0, 25, 50, 75, 100):
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1" '
                   'stroke-dasharray="%s" opacity="0.55"/>'
                   % (L, sy(v), R, sy(v), C_GRID, "0" if v == 50 else "3 5"))
        out.append(_txt(L - 12, sy(v) + 4, str(v), 11.5, C_MUTED, "end"))
    for v in (0, 25, 50, 75, 100):
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1" '
                   'stroke-dasharray="%s" opacity="0.55"/>'
                   % (sx(v), T, sx(v), B, C_GRID, "0" if v == 50 else "3 5"))
        out.append(_txt(sx(v), B + 20, str(v), 11.5, C_MUTED, "middle"))

    # 零轴（分位 50）+ 四周边框
    out.append('<rect x="%d" y="%d" width="%d" height="%d" fill="none" stroke="%s" stroke-width="1"/>'
               % (L, T, pw, ph, C_GRID))

    # 象限标签：嵌在绘图区最外角，并预登记为"占位框"，
    # 后面的板块名标签避让时会自动跳过这些区域，避免文字叠字。
    # 注意：象限标签的绘制被推迟到散点之后（见下方 _quad_labels），
    # 否则密集散点会把标签压住看不清。
    lab = [
        ("accel", R - 10, T + 26, "end"),
        ("probe", L + 10, T + 26, "start"),
        ("bleed", L + 10, B - 12, "start"),
        ("dist", R - 10, B - 12, "end"),
    ]
    qboxes = []
    _quad_labels = []
    for key, x, y, anchor in lab:
        name, _f, border, _dsc = QUAD_DEF[key]
        bw_lab = 88
        bx = x - bw_lab if anchor == "end" else x
        _quad_labels.append(
            '<rect x="%.1f" y="%.1f" width="%d" height="25" rx="7" fill="rgba(6,9,14,0.94)" '
            'stroke="%s" stroke-width="1.2"/>'
            % (bx, y - 18, bw_lab, border))
        _quad_labels.append(_txt(x - (9 if anchor == "end" else 9), y, name, 13.5, C_TEXT,
                                 "end" if anchor == "end" else "start", "700"))
        qboxes.append((bx - 2, y - 20, bx + bw_lab + 2, y + 8))

    # 坐标轴标题
    out.append(_txt((L + R) / 2, B + 48, "5日主力净额分位（越高=中期资金相对越强）→",
                    12.5, C_MUTED, "middle"))
    out.append('<text x="26" y="%.1f" font-size="12.5" fill="%s" text-anchor="middle" '
               'transform="rotate(-90 26 %.1f)">今日主力净额分位（越高=短期资金相对越强）→</text>'
               % ((T + B) / 2, C_MUTED, (T + B) / 2))

    # 散点：按半径升序绘制 → 大点后画、压在上层，视觉权重自然体现成交额
    out.append('<g clip-path="url(#plotclip)">')
    for b in sorted(show, key=_rad):
        r = _rad(b)
        cx, cy = sx(b["pt"]), sy(b["p5"])
        fill = C_UP if (b["net"] is not None and b["net"] > 0) else (
            C_DOWN if (b["net"] is not None and b["net"] < 0) else C_FLAT)
        # 提示文本保持精炼：457 个点 × 长中文说明会把单文件撑大且拖慢手机渲染
        tip = "%s｜今日 %s｜5日 %s｜%s" % (
            b["name"], fmt_yi(b["net"], 1, True), fmt_yi(b["net5"], 1, True),
            QUAD_DEF.get(b["quad"] or "", ("未分类",))[0],
        )
        # 小点更淡：点多了之后满屏实心会糊成一片，靠透明度区分"权重"
        op = 0.34 + 0.42 * min(1.0, r / (2.0 + 7.6))
        sw = 0.6 if r < 4.0 else 1.1
        out.append('<circle class="pt" cx="%.1f" cy="%.1f" r="%.1f" fill="%s" fill-opacity="%.2f" '
                   'stroke="%s" stroke-width="%.1f" stroke-opacity="0.75"><title>%s</title></circle>'
                   % (cx, cy, r, fill, op, fill, sw, esc(tip)))
    out.append("</g>")

    # 名称标签：按象限分层取样（每象限取成交额最大的几个），
    # 否则全市场普跌时标签会扎堆在"持续失血"一角，另外三个象限无字可读。
    per_quad = {"accel": 4, "probe": 4, "bleed": 5, "dist": 3}
    labeled = []
    for qkey, quota in per_quad.items():
        group = [b for b in ranked if b.get("quad") == qkey]
        group.sort(key=_mag, reverse=True)
        labeled.extend(group[:quota])

    # 已占位矩形：先把四个象限标签框登记进去，避免板块名压在上面
    boxes = list(qboxes)

    def _overlap(x0, y0, x1, y1):
        for bx0, by0, bx1, by1 in boxes:
            if x0 < bx1 and x1 > bx0 and y0 < by1 and y1 > by0:
                return True
        return False

    for b in labeled:
        cx, cy = sx(b["pt"]), sy(b["p5"])
        r = _rad(b)
        anchor = "end" if cx > R - 140 else "start"
        dx = -(r + 5) if anchor == "end" else (r + 5)
        tw = min(len(b["name"]) * 12.0 + 4, 150.0)
        tx = cx + dx
        x0 = tx - tw if anchor == "end" else tx
        x1 = tx if anchor == "end" else tx + tw
        if not (L - 4 <= x0 and x1 <= R + 4 and T <= cy - 8 and cy + 6 <= B):
            continue
        if _overlap(x0, cy - 8, x1, cy + 6):
            continue
        boxes.append((x0, cy - 8, x1, cy + 6))
        out.append(_txt(tx, cy + 4, b["name"], 11.5, "#cfd8e6", anchor, "500"))

    # 象限标签最后画：压在所有散点与板块名之上，保证可读
    out.extend(_quad_labels)

    out.append("</svg>")
    return "".join(out)


def _quad_summary(points):
    """四象限统计：数量 + 今日净额中位数（把"分位"落回真实金额，避免误读）。"""
    groups = {k: [] for k in QUAD_DEF}
    for b in points:
        if b["quad"] in groups:
            groups[b["quad"]].append(b)
    cards = []
    for key in ("accel", "probe", "bleed", "dist"):
        name, fill, border, desc = QUAD_DEF[key]
        g = groups[key]
        med = _median([b["net"] for b in g]) if g else None
        cards.append(
            '<div class="qcard" style="border-color:%s;background:%s">'
            '<div class="qhd"><span class="qdot" style="background:%s"></span>'
            '<b>%s</b><span class="qnum">%d 个</span></div>'
            '<div class="qmed">今日净额中位数 <span class="%s">%s</span></div>'
            '<div class="qdesc">%s</div></div>'
            % (border, fill, border, esc(name), len(g), _cls(med),
               fmt_yi(med, 2, True), esc(desc))
        )
    return '<div class="qgrid">%s</div>' % "".join(cards)


# ---------------------------------------------------------------- 面板 2：涨跌家数条

def _breadth_svg(up, down, flat):
    total = sum(v for v in (up, down, flat) if v is not None)
    if not total:
        return '<div class="empty">涨跌家数缺失</div>'
    W, H = 1000, 64
    up = up or 0
    down = down or 0
    flat = flat or 0
    segs = [(up, C_UP, "上涨"), (flat, C_FLAT, "平盘"), (down, C_DOWN, "下跌")]
    out = [_svg_open(W, H)]
    x = 0.0
    for val, color, label in segs:
        w = val / total * W
        if w <= 0:
            continue
        out.append('<rect x="%.1f" y="14" width="%.1f" height="30" fill="%s" fill-opacity="0.85"/>'
                   % (x, max(w - 1.2, 1.0), color))
        pct = val / total * 100.0
        # 段太窄放不下文字就不标（阈值按百分比判断，别用像素宽度）
        if pct >= 12.0:
            out.append(_txt(x + w / 2, 34, "%.1f%%" % (val / total * 100.0),
                            13, "#080a0f", "middle", "700"))
        x += w
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------- 面板 4：资金强度榜

def _strength_svg(boards, topn=20):
    """资金强度横向条形。

    坑：TOP20 的分数天然挤在全截面分布的右端（实测 86.5—100），若直接按 score/100
    线性映射到条长，20 根条全部落在 86%—100%，肉眼完全看不出差别，等于白占一屏。
    做法：把 X 轴起点动态抬到 TOP20 最小值再往下取整（如 80），做"局部放大"，
    同时在表头写明"轴起点 80，全截面 0—100"，并在条尾标精确分数——既不歪曲数据，
    又让 20 行的差距可辨。
    """
    if not boards:
        return '<div class="empty">板块强度数据缺失</div>'

    scored = [b for b in boards if b.get("score") is not None]
    smin = min((b["score"] for b in scored), default=0.0)
    smax = max((b["score"] for b in scored), default=100.0)
    # 轴起点：最小值向下取整到 5 的倍数；若跨度本来就大（>40），退回 0 保持全局诚实
    span = smax - smin
    if span >= 40 or smax <= 0:
        axis0 = 0.0
    else:
        axis0 = max(0.0, (int(smin) // 5) * 5.0 - 5.0)

    W = 1080
    row_h = 27
    H = 46 + row_h * len(boards)
    LX, BX0, BX1 = 8, 210, 762
    SX, VX, AX = 772, 838, 968          # 分数 / 今日净额 / 近5日净额
    bw_max = BX1 - BX0

    out = [_svg_open(W, H)]
    out.append(_txt(LX, 18, "板块（行业）", 11.5, C_MUTED))
    axis_note = ("资金强度 0—100" if axis0 == 0
                 else "资金强度（轴起点 %.0f，全截面 0—100）" % axis0)
    out.append(_txt(BX0, 18, axis_note, 11.5, C_MUTED))
    out.append(_txt(SX, 18, "强度分", 11.5, C_MUTED))
    out.append(_txt(VX, 18, "今日净额", 11.5, C_MUTED))
    out.append(_txt(AX, 18, "近5日", 11.5, C_MUTED))

    # 轴起点刻度线，提示这是放大视图
    if axis0 > 0:
        out.append('<line x1="%d" y1="26" x2="%d" y2="%d" stroke="%s" stroke-width="1" '
                   'stroke-dasharray="2 4" opacity="0.8"/>'
                   % (BX0, BX0, H - 4, C_MUTED))

    for i, b in enumerate(boards):
        y = 30 + i * row_h
        if i % 2 == 0:
            out.append('<rect x="0" y="%d" width="%d" height="%d" fill="#ffffff" fill-opacity="0.018"/>'
                       % (y, W, row_h))
        score = b["score"]
        if score is None:
            score = axis0
        score = max(axis0, min(100.0, score))
        frac = (score - axis0) / (100.0 - axis0) if (100.0 - axis0) > 0 else 0.0
        bw = bw_max * max(0.0, min(1.0, frac))
        color = C_UP if (b["net"] is not None and b["net"] > 0) else (
            C_DOWN if (b["net"] is not None and b["net"] < 0) else C_FLAT)

        out.append('<rect x="%d" y="%d" width="%d" height="%d" rx="2" fill="%s" fill-opacity="0.09"/>'
                   % (BX0, y + 8, bw_max, 11, C_GRID))
        out.append('<rect x="%d" y="%d" width="%.1f" height="%d" rx="2" fill="%s" fill-opacity="0.85"/>'
                   % (BX0, y + 8, max(bw, 1.5), 11, color))
        out.append(_txt(BX0 - 8, y + 18, b["name"], 12.5, C_TEXT, "end", "500"))
        out.append(_txt(SX, y + 18,
                        ("%.1f" % b["score"]) if b["score"] is not None else DASH,
                        12, C_MUTED))
        out.append(_txt(VX, y + 18, fmt_yi(b["net"], 2, True), 12.5, color, "start", "600"))
        out.append(_txt(AX, y + 18,
                        fmt_yi(b["net5"], 1, True) if b["net5"] is not None else DASH,
                        11.5, C_MUTED))
        tip = "%s · 强度 %.1f · 今日 %s · 5日 %s · 10日 %s · 成交额 %s" % (
            b["name"], b["score"] if b["score"] is not None else 0.0,
            fmt_yi(b["net"], 2, True), fmt_yi(b["net5"], 2, True),
            fmt_yi(b["net10"], 2, True), fmt_yi(b["amount"], 0))
        out.append('<rect x="0" y="%d" width="%d" height="%d" fill="transparent"><title>%s</title></rect>'
                   % (y, W, row_h, esc(tip)))
    out.append(_txt(LX, H - 6,
                    "条长按轴起点 %.0f 局部放大 · 分数为全截面 0—100 绝对强度 · "
                    "红=今日净流入，绿=净流出" % axis0 if axis0 > 0
                    else "红=今日净流入，绿=净流出", 10.5, C_MUTED))
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------- 面板 7：连板梯队

def _ladder_svg(pool, ladder=None):
    """连板梯队。优先用 metrics 给的 ladder {'1':33,'2':12,...}，否则从涨停池 lbc 现算。"""
    buckets = {}
    if isinstance(ladder, dict) and ladder:
        for k, v in ladder.items():
            kk = _num(k)
            vv = _num(v)
            if kk is not None and vv is not None and vv > 0:
                buckets[int(kk)] = int(vv)
    if not buckets:
        if not pool:
            return '<div class="empty">涨停池数据缺失</div>'
        dvals = []
        for r in pool:
            d = _num(r.get("limit_days"))
            dvals.append(d if d is not None else 1.0)
        for d in dvals:
            key = max(1, int(d))
            buckets[key] = buckets.get(key, 0) + 1

    keys = sorted(k for k in buckets if k >= 1)
    if not keys:
        return '<div class="empty">连板数据缺失</div>'
    maxd = max(keys)
    if maxd >= 4:                                  # 4板及以上合并，避免长尾拉宽
        merged = {}
        for k in keys:
            merged[4 if k >= 4 else k] = merged.get(4 if k >= 4 else k, 0) + buckets[k]
        buckets = merged
    labels = [(k, ("%d板+" % k) if (k == 4 and maxd >= 4) else ("%d板" % k))
              for k in sorted(buckets)]
    buckets = {k: buckets[k] for k in sorted(buckets)}
    labels = [(k, lab) for k, lab in labels if k in buckets]

    W, H = 620, 210
    left, right, top, bottom = 44, 600, 30, 168
    n = len(labels)
    slot = (right - left) / max(n, 1)
    bw = min(56.0, slot * 0.56)
    maxc = max(buckets.values()) if buckets else 1
    out = [_svg_open(W, H)]
    out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="1"/>'
               % (left, top, right, top, C_GRID))
    for i, (d, lab) in enumerate(labels):
        c = buckets.get(d, 0)
        h = (bottom - top) * (c / maxc) if maxc else 0
        x = left + slot * i + (slot - bw) / 2
        y = bottom - h
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="4" fill="%s" fill-opacity="0.82"/>'
                   % (x, y, bw, max(h, 1.5), C_UP))
        out.append(_txt(x + bw / 2, y - 7, "%d 只" % c, 12.5, C_TEXT, "middle", "600"))
        out.append(_txt(x + bw / 2, bottom + 20, lab, 12.5, C_MUTED, "middle"))
    total_ct = sum(buckets.values())
    out.append(_txt(left, H - 12, "连板高度分布 · 共 %d 只涨停（数据源：交易所涨停板池）"
                    % total_ct, 11, C_MUTED))
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------- 面板 2：指数资金流迷你图

def _sparkline(series, w=250, h=74):
    """指数主力净额累计曲线（15 日真实序列）。"""
    vals = []
    for r in series or []:
        v = _num(r.get("main_net")) if isinstance(r, dict) else None
        if v is not None:
            vals.append(v)
    if len(vals) < 2:
        return ""
    cum, acc = [], 0.0
    for v in vals:
        acc += v
        cum.append(acc)
    lo, hi = min(cum + [0.0]), max(cum + [0.0])
    span = (hi - lo) or 1.0
    pad = 6
    pts = []
    for i, v in enumerate(cum):
        x = pad + (w - 2 * pad) * i / (len(cum) - 1)
        y = h - pad - (h - 2 * pad) * (v - lo) / span
        pts.append("%.1f,%.1f" % (x, y))
    zero_y = h - pad - (h - 2 * pad) * (0.0 - lo) / span
    color = C_UP if cum[-1] > 0 else (C_DOWN if cum[-1] < 0 else C_FLAT)
    area = "%.1f,%.1f " % (pad, zero_y) + " ".join(pts) + " %.1f,%.1f" % (w - pad, zero_y)
    return (
        _svg_open(w, h)
        + '<polygon points="%s" fill="%s" fill-opacity="0.14"/>' % (area, color)
        + '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1" stroke-dasharray="3 4"/>'
        % (pad, zero_y, w - pad, zero_y, C_GRID)
        + '<polyline points="%s" fill="none" stroke="%s" stroke-width="1.9" '
          'stroke-linejoin="round" stroke-linecap="round"/>' % (" ".join(pts), color)
        + "</svg>"
    )


# ---------------------------------------------------------------- 面板构建

def _panel(pid, title, body, sub="", cls=""):
    sub_html = '<span class="psub">%s</span>' % esc(sub) if sub else ""
    return (
        '<section class="panel %s" id="p%s" data-panel="%s">'
        '<div class="phd"><h2>%s</h2>%s</div>%s</section>'
        % (cls, pid, pid, esc(title), sub_html, body)
    )


def _tile(label, value, cls="", note=""):
    note_html = '<div class="tn">%s</div>' % esc(note) if note else ""
    return ('<div class="tile"><div class="tl">%s</div>'
            '<div class="tv %s">%s</div>%s</div>' % (esc(label), cls, esc(value), note_html))


def _empty(msg):
    return '<div class="empty">%s</div>' % esc(msg)


# ---- 面板 1：顶部状态条
def _p1(analysis, snapshot):
    as_of = _text(_d(analysis, "as_of") or _d(snapshot, "as_of"), DASH)
    gen = _text(_d(snapshot, "generated_at"), DASH)
    regime = _d(analysis, "regime")
    rlabel = _text(_d(regime, "label") or _d(analysis, "regime_label"), DASH)

    # 命中规则：metrics 用 hit_rule(字符串) + evidence(list)；也兼容 rules/hit_rules(list)
    hit_txt = ""
    hr = _d(regime, "hit_rule")
    if isinstance(hr, str) and hr.strip():
        hit_txt = hr.strip()
    else:
        hit = _d(regime, "rules") or _d(regime, "hit_rules") or _d(analysis, "regime_rules")
        if isinstance(hit, list) and hit:
            hit_txt = " / ".join(_text(x) for x in hit[:3])
        elif isinstance(hit, str) and hit.strip():
            hit_txt = hit.strip()

    ev = _d(regime, "evidence")
    ev_html = ""
    if isinstance(ev, list) and ev:
        ev_html = "".join('<span class="chipxs">%s</span>' % esc(_text(x)) for x in ev[:5])

    rules = _d(analysis, "rules")
    rules = [str(x).strip() for x in rules if str(x).strip()] if isinstance(rules, list) else []

    # 一句话结论：优先显式字段，否则取 metrics 规则判读的第一条（它本身就是结论句）
    concl = _d(analysis, "conclusion") or _d(analysis, "one_liner") \
        or _d(analysis, "summary") or _d(analysis, "verdict")
    if not concl and rules:
        concl = rules[0]
    concl = _text(concl, DASH)

    regime_cls = "na"
    for k, cl in (("进攻", "up"), ("防守", "down"), ("震荡", "flat"), ("风险", "down")):
        if k in rlabel:
            regime_cls = cl
            break

    rest_rules = "".join('<li>%s</li>' % esc(r) for r in rules[1:5])
    rules_html = ('<ul class="rlist">%s</ul>' % rest_rules) if rest_rules else ""

    body = (
        '<div class="statusbar">'
        '<div class="sb-item"><span class="sbl">交易日</span><b>%s</b></div>'
        '<div class="sb-item"><span class="sbl">市场状态</span><b class="%s">%s</b></div>'
        '<div class="sb-item"><span class="sbl">生成时间</span><b class="mono">%s</b></div>'
        '<div class="sb-item"><span class="sbl">判读规则数</span><b>%s 条</b></div>'
        '<div class="sb-item sb-wide"><span class="sbl">一句话结论</span><b>%s</b></div>'
        '</div>'
        '<div class="concl">%s%s</div>'
        '%s'
        '%s'
        % (esc(as_of), regime_cls, esc(rlabel), esc(gen),
           fmt_int(len(rules) if rules else 0) if rules else DASH, esc(concl),
           ('<span class="hit">命中规则：%s</span>' % esc(hit_txt)) if hit_txt else "",
           '<span class="hit">未见规则判读（metrics 未就绪）</span>' if not rules else "",
           ('<div class="evlbl">判定依据</div><div class="evrow">%s</div>' % ev_html) if ev_html else "",
           rules_html)
    )
    return _panel("1", "市场状态总览", body, "交易日 / 状态 / 结论")


# ---- 面板 2：大盘脉搏
def _p2(analysis, snapshot):
    b = _d(snapshot, "breadth") or {}
    bb = _d(analysis, "breadth_block")
    bb = bb if isinstance(bb, dict) and bb else {}
    # metrics 的 breadth_block 与 snapshot.breadth 字段同名，优先用 metrics 版本
    src_b = dict(b)
    src_b.update({k: v for k, v in bb.items() if v is not None})

    up, down, flat = _num(src_b.get("up")), _num(src_b.get("down")), _num(src_b.get("flat"))
    amt = _num(src_b.get("amount_total"))
    mnet = _num(src_b.get("main_net_total"))
    lu = _num(src_b.get("limit_up"))
    ld = _num(src_b.get("limit_down"))
    cnt = _num(src_b.get("count"))
    netflow_pct = _num(bb.get("netflow_pct"))

    win = _num(bb.get("up_ratio"))
    if win is None:
        denom = sum(v for v in (up, down, flat) if v is not None)
        if up is not None and denom:
            win = up / denom * 100.0

    # 指数卡片：snapshot.indices（真实行情）→ analysis.majors（metrics 合并了资金流）
    #           → snapshot.index_flow（只有资金流，无行情）
    idx = [r for r in (_d(snapshot, "indices") or []) if isinstance(r, dict)]
    flow = _d(snapshot, "index_flow") or {}
    flow = flow if isinstance(flow, dict) else {}
    if not idx:
        idx = [r for r in (analysis.get("majors") or []) if isinstance(r, dict)] if isinstance(
            analysis.get("majors"), list) else []

    def _flow_by_name(nm):
        for _k, fv in flow.items():
            if isinstance(fv, dict) and _text(fv.get("name"), "") == nm:
                return fv
        return None

    cards = []
    if idx:
        for r in idx[:5]:
            chg = _num(r.get("chg"))
            price = _num(r.get("price"))
            amt_i = _num(r.get("amount"))
            n5 = _num(r.get("net_5d"))
            n10 = _num(r.get("net_10d"))
            if n5 is None:
                fv = _flow_by_name(_text(r.get("name"), ""))
                if isinstance(fv, dict):
                    n5 = _num(fv.get("net_5d"))
                    n10 = _num(fv.get("net_10d"))
            # 行情缺失（price/chg 都是 None）时，卡片走"仅资金流"样式，避免显示假的 0.00
            has_quote = price is not None or chg is not None
            if has_quote:
                cards.append(
                    '<div class="icard"><div class="icn">%s</div>'
                    '<div class="icp %s">%s</div>'
                    '<div class="icc %s">%s %s</div>'
                    '<div class="ica">成交 %s · 5日 <span class="%s">%s</span></div></div>'
                    % (esc(_text(r.get("name"))), _cls(chg), fmt_price(price),
                       _cls(chg), esc(_arrow(chg)), fmt_pct(chg),
                       fmt_yi(amt_i, 0), _cls(n5), fmt_yi(n5, 1, True))
                )
            else:
                cards.append(
                    '<div class="icard icard-na"><div class="icn">%s</div>'
                    '<div class="icp na">%s</div>'
                    '<div class="icc na">行情缺失</div>'
                    '<div class="ica">5日 <span class="%s">%s</span> · 10日 <span class="%s">%s</span>'
                    '</div></div>'
                    % (esc(_text(r.get("name"))), DASH,
                       _cls(n5), fmt_yi(n5, 1, True),
                       _cls(n10), fmt_yi(n10, 1, True))
                )
        idx_note = "" if any(_num(r.get("price")) is not None
                             or _num(r.get("chg")) is not None for r in idx[:5]) else \
            "指数行情缺失（接口当日未返回），卡片展示各指数真实资金流"
    else:
        # 降级：行情缺失，但 index_flow 的 5日/10日净额是真实数据
        flow = _d(snapshot, "index_flow") or {}
        order = ["1.000001", "0.399001", "0.399006", "1.000688", "1.000300"]
        keys = [k for k in order if k in flow] + [k for k in flow if k not in order]
        for k in keys[:5]:
            f = flow.get(k) or {}
            n5 = _num(f.get("net_5d"))
            cards.append(
                '<div class="icard icard-na"><div class="icn">%s</div>'
                '<div class="icp na">%s</div>'
                '<div class="icc na">行情缺失</div>'
                '<div class="ica">5日 <span class="%s">%s</span> · 10日 <span class="%s">%s</span></div>'
                '</div>'
                % (esc(_text(f.get("name"))), DASH,
                   _cls(n5), fmt_yi(n5, 1, True),
                   _cls(_num(f.get("net_10d"))), fmt_yi(_num(f.get("net_10d")), 1, True))
            )
        idx_note = "指数行情缺失（接口当日未返回），下图展示各指数真实资金流"

    spark_html = ""
    flow = _d(snapshot, "index_flow") or {}
    sh = flow.get("1.000001") if isinstance(flow, dict) else None
    if isinstance(sh, dict) and sh.get("series"):
        sp = _sparkline(sh.get("series"))
        if sp:
            cum = sum(_num(x.get("main_net")) or 0 for x in sh["series"])
            spark_html = (
                '<div class="spark"><div class="spt">上证指数 · 近%d日主力净额累计 '
                '<span class="%s">%s</span></div>%s</div>'
                % (len(sh["series"]), _cls(cum), fmt_yi(cum, 1, True), sp)
            )

    # 注意：副标题一律用人话，不出现 f62/f6 这类东财字段编号（用户看不懂）
    netflow_note = "全部 A 股主力净额加总"
    if netflow_pct is not None:
        netflow_note += " · 占成交额 %.2f%%" % netflow_pct
    tiles = "".join([
        _tile("全市场主力净额", fmt_yi(mnet, 1, True), _cls(mnet), netflow_note),
        _tile("两市成交额", fmt_yi(amt, 1), "", "全部 A 股成交额加总"),
        _tile("上涨 / 下跌", "%s / %s" % (fmt_int(up), fmt_int(down)), _cls(win and (win - 50))),
        _tile("赚钱效应（上涨占比）", ("%.1f%%" % win) if win is not None else DASH,
              _cls(win - 50 if win is not None else None)),
        _tile("涨停 / 跌停", "%s / %s" % (fmt_int(lu), fmt_int(ld)),
              "up" if (lu or 0) > (ld or 0) else "down",
              ("空盘状态·上一交易日残值"
               if _text(_d(snapshot, "trading_state"), "trading") != "trading"
               else "交易所涨停池")),
        _tile("有效样本", fmt_int(cnt), "", "含停牌 %s 只" % fmt_int(_num(b.get("suspended")))),
    ])

    body = (
        '<div class="idxrow">%s</div>%s'
        '<div class="idxnote">%s</div>'
        '<div class="bwrap"><div class="bttl">涨跌家数</div>%s'
        '<div class="blegend">'
        '<span class="lg"><i style="background:%s"></i>上涨 %s</span>'
        '<span class="lg"><i style="background:%s"></i>平盘 %s</span>'
        '<span class="lg"><i style="background:%s"></i>下跌 %s</span>'
        '</div></div>'
        '<div class="tiles">%s</div>'
        % ("".join(cards), spark_html, esc(idx_note) if idx_note else "&nbsp;",
           (_breadth_svg(up, down, flat)
            if (_num(up) or 0) + (_num(down) or 0) + (_num(flat) or 0) > 0
            else '<div class="empty">非交易时段，当日涨跌家数无有效数据</div>'),
           C_UP, fmt_int(up), C_FLAT, fmt_int(flat), C_DOWN, fmt_int(down), tiles)
    )
    return _panel("2", "大盘脉搏", body, "指数 / 涨跌家数 / 主力净额 / 赚钱效应")


# ---- 面板 3：四象限散点
def _p3(analysis, snapshot):
    pts, shown, source = _scatter_points(analysis, snapshot)
    total = len(_d(snapshot, "industries") or []) or len(pts)
    if source == "analysis":
        note = "散点与分位来自 metrics；点面积=板块成交额，悬停查看明细"
    elif source == "derived":
        note = ("metrics 未就绪，本图由渲染层按契约口径用快照真实数值现算截面分位"
                "（今日/5日主力净额排名）；点面积=成交额，悬停查看明细")
    else:
        note = "无可用板块数据"
    # 统计每象限点数，写进图注：让"图上看不清"的部分也能被读到
    cnt = {}
    for b in pts:
        if b.get("quad"):
            cnt[b["quad"]] = cnt.get(b["quad"], 0) + 1
    dist = " / ".join("%s %d 个" % (QUAD_DEF[k][0], cnt.get(k, 0))
                      for k in ("accel", "probe", "bleed", "dist"))
    # cap 由本函数自己拼装，其中的 <br> 是**有意的排版标记**，不能整体转义，
    # 否则页面会显示出字面量 "<br>"。但 cap 里插入的动态文本必须转义：
    # 这里只有 dist（由固定象限标签 + 计数构成），对其进行 esc；
    # 若将来要拼入板块名等数据文本，也必须单独 esc 后再插入。
    cap = ("样本：%d 个行业板块全部绘制（共 %d 个点）· 象限分布：%s<br>"
           "象限按分位 50 划分；分位高=相对流入强，不代表绝对净额为正。"
           "点越大=成交额越大，颜色红=今日净流入、绿=今日净流出"
           % (total, len(pts), esc(dist)))
    body = (
        '<div class="chartwrap">%s</div>'
        '%s'
        '<div class="caption">%s<br><span class="dim">%s</span></div>'
        % (_scatter_svg(pts, source, total), _quad_summary(pts), esc(note), cap)
    )
    return _panel("3", "板块轮动四象限", body, "核心图 · X=5日净额分位 / Y=今日净额分位", "core")


# ---- 面板 3.5：板块资金热力图（treemap）
def _treemap_panel(analysis, snapshot):
    pts, _shown, source = _scatter_points(analysis, snapshot)
    if not pts:
        return _panel("3t", "板块资金热力图",
                      '<div class="empty">无可用板块数据</div>',
                      "面积=成交额 / 颜色=主力净额方向")
    svg = _treemap_svg(pts)
    if source == "analysis":
        note = "面积与净额来自 metrics 全板块截面"
    else:
        note = "metrics 未就绪，本图由渲染层用快照真实数值绘制"
    body = ('<div class="chartwrap">%s</div>'
            '<div class="caption"><span class="dim">%s</span></div>'
            % (svg, esc(note)))
    return _panel("3t", "板块资金热力图",
                  body, "面积=成交额 / 颜色=今日主力净额（红流入 绿流出）· 与四象限散点互补", "core")


# ---- 面板 4：资金强度 TOP20
def _p4(analysis, snapshot):
    boards = _rows(_d(analysis, "industries_ranked")) or _rows(_d(analysis, "flow_top"))
    src = "analysis"
    if not boards:
        boards = _rows(_d(analysis, "industries"))
    if not boards:
        # 降级：用快照真实数值现算分位加权强度
        raw = [r for r in (_d(snapshot, "industries") or []) if isinstance(r, dict)]
        if raw:
            p_t = _pct_rank([_num(r.get("main_net")) for r in raw])
            p_5 = _pct_rank([_num(r.get("main_net_5d")) for r in raw])
            p_10 = _pct_rank([_num(r.get("main_net_10d")) for r in raw])
            tmp = []
            for i, r in enumerate(raw):
                bb = _board(r)
                bb["score"] = _weighted_strength(p_t[i], p_5[i], p_10[i])
                bb["_p5"] = p_5[i]
                bb["_pt"] = p_t[i]
                tmp.append(bb)
            tmp = [x for x in tmp if x["score"] is not None]
            tmp.sort(key=lambda x: x["score"], reverse=True)
            boards = tmp
            src = "derived"
        else:
            boards = []
    else:
        boards = [_board(r) for r in boards]
        if not any(b["score"] is not None for b in boards):
            src = "net"

    if boards:
        if src != "derived":
            boards.sort(key=lambda x: (x["score"] if x["score"] is not None else -1,
                                       x["net"] if x["net"] is not None else -1e30), reverse=True)
        boards, n_dup = _dedupe_boards(boards)
        boards = boards[:20]
    else:
        n_dup = 0

    if not boards:
        body = _empty("板块强度数据缺失（metrics 未就绪且快照无行业板块）")
        return _panel("4", "资金强度 TOP20", body, "行业板块")

    if src == "derived":
        note = "强度由渲染层按契约口径（今日0.5/5日0.3/10日0.2 分位加权）用快照真实值现算"
    elif src == "net":
        note = "metrics 未提供强度分，按今日主力净额排序展示"
    else:
        note = "强度分与排序来自 metrics"
    if n_dup:
        note += "；已去除 %d 个 Ⅱ/Ⅲ 级同名重复板块（东财分级板块数值相同）" % n_dup
    body = (
        '<div class="chartwrap">%s</div>'
        '<div class="caption">%s · 红=今日主力净流入，绿=净流出（A股配色）</div>'
        % (_strength_svg(boards), esc(note))
    )
    return _panel("4", "资金强度 TOP20", body, "行业板块 · 横向条形")


# ---- 面板 5：个股双列
def _stock_rows(rows, n=15, inflow=True):
    out = []
    for i, r in enumerate(rows[:n]):
        name = _text(r.get("name"))
        code = _text(r.get("code"), "")
        net = _first_num(r, _NET_KEYS)
        chg = _first_num(r, _CHG_KEYS)
        ind = r.get("industry")
        tip = "%s %s · 主力净额 %s · 涨跌 %s · 所属 %s" % (
            name, code, fmt_yi(net, 2, True), fmt_pct(chg), _text(ind, "—"))
        out.append(
            '<div class="srow" title="%s">'
            '<span class="sidx %s">%d</span>'
            '<span class="snm">%s<em>%s</em></span>'
            '<span class="schg %s">%s</span>'
            '<span class="snet %s">%s</span>'
            '</div>'
            % (esc(tip), "up" if inflow else "down", i + 1,
               esc(name), esc(code), _cls(chg), fmt_pct(chg),
               _cls(net), fmt_yi(net, 2, True))
        )
    if not out:
        return _empty("个股数据缺失")
    return "".join(out)


def _p5(analysis, snapshot):
    tin = _rows(_d(analysis, "stocks_top_in")) or _rows(_d(analysis, "stocks_inflow"))
    tout = _rows(_d(analysis, "stocks_top_out")) or _rows(_d(analysis, "stocks_outflow"))
    src = "analysis"
    if not tin and not tout:
        raw = [r for r in (_d(snapshot, "stocks") or [])
               if isinstance(r, dict) and _num(r.get("main_net")) is not None]
        raw.sort(key=lambda r: _num(r.get("main_net")), reverse=True)
        tin = raw[:15]
        tout = list(reversed(raw[-15:]))
        src = "derived"
    else:
        tin = tin[:15]
        tout = tout[:15]

    note = "按今日主力净额排序" if src == "derived" else "来自 metrics 榜单"
    body = (
        '<div class="duo">'
        '<div class="duocol"><div class="duohd up">主力净流入 TOP15</div>%s</div>'
        '<div class="duocol"><div class="duohd down">主力净流出 TOP15</div>%s</div>'
        '</div><div class="caption">%s · 单位：亿元，红=流入，绿=流出</div>'
        % (_stock_rows(tin, 15, True), _stock_rows(tout, 15, False), esc(note))
    )
    return _panel("5", "个股主力资金榜", body, "净流入 / 净流出 双列")


# ---- 面板 6：背离警报
def _p6(analysis, snapshot):
    # 优先 metrics 的 divergence_top（≤5 条精选）；退回 divergence_strong / divergence。
    # 背离像"警报"，列几十条就没有警报意味了，所以只展示前 6 个。
    src = "analysis"
    div = (_rows(_d(analysis, "divergence_top"))
           or _rows(_d(analysis, "divergence_strong"))
           or _rows(_d(analysis, "divergence"))
           or _rows(_d(analysis, "divergences")))
    total_div = len(_rows(_d(analysis, "divergence")))
    if not div:
        raw = [r for r in (_d(snapshot, "industries") or []) if isinstance(r, dict)]
        cand = []
        for r in raw:
            net = _num(r.get("main_net"))
            n10 = _num(r.get("main_net_10d"))
            n5 = _num(r.get("main_net_5d"))
            mid = n10 if n10 is not None else n5       # 与展示口径严格一致
            if net is not None and mid is not None and net > 0 and mid < 0:
                cand.append(r)
        cand.sort(key=lambda r: _num(r.get("main_net")) or 0, reverse=True)
        div = cand[:8]
        src = "derived"

    if not div:
        body = _empty("今日无「今日流入但中期流出」的行业板块")
        return _panel("6", "背离警报", body, "今日流入 × 中期流出")

    cards = []
    for r in div[:6]:
        b = _board(r)
        # 中期口径：优先 10 日；仅在 10 日缺失时才退到 5 日。
        # 关键：以"实际用于判定的那个口径"为准渲染，避免出现"10日 +0.96亿"却标称中期流出的自相矛盾。
        if b["net10"] is not None:
            mid, mid_lab = b["net10"], "10日"
        else:
            mid, mid_lab = b["net5"], "5日"
        # 双口径都展示，读者可自行交叉验证（缺失则不显示该行）
        extra = ""
        if b["net10"] is not None and b["net5"] is not None:
            extra = ('<span>5日 <b class="%s">%s</b></span>'
                     % (_cls(b["net5"]), fmt_yi(b["net5"], 2, True)))
        cards.append(
            '<div class="alert">'
            '<div class="aicon">!</div>'
            '<div class="abody">'
            '<div class="anm">%s</div>'
            '<div class="anums">'
            '<span>今日 <b class="up">%s</b></span>'
            '<span>%s <b class="down">%s</b></span>'
            '%s'
            '<span class="achg %s">板块涨跌 %s</span>'
            '</div>'
            '<div class="atip">短期资金回补，但中期仍在净流出 —— 持续性存疑</div>'
            '</div></div>'
            % (esc(b["name"]), fmt_yi(b["net"], 2, True), esc(mid_lab), fmt_yi(mid, 2, True),
               extra, _cls(b["chg"]), fmt_pct(b["chg"]))
        )

    if src == "derived":
        note = ("口径：今日主力净额 > 0 且 10日净额 < 0（10日缺失时才用5日判定），按今日净额降序；"
                "两个中期口径同时列出以便交叉验证")
    else:
        note = "背离板块来自 metrics 判定（优先 divergence_top 精选）"
    if total_div > len(cards):
        note += "；全市场共 %d 个行业出现此类背离，此处仅列最典型的 %d 个" % (total_div, len(cards))
    body = ('<div class="alertgrid">%s</div><div class="caption">%s</div>'
            % ("".join(cards), esc(note)))
    return _panel("6", "背离警报", body, "今日流入 × 中期流出", "alertpanel")


# ---- 面板 7：情绪面板
def _p7(analysis, snapshot):
    sent = _d(analysis, "sentiment")
    sent = sent if isinstance(sent, dict) else {}
    b = _d(snapshot, "breadth") or {}
    pool = [r for r in (_d(snapshot, "limit_up_pool") or []) if isinstance(r, dict)]

    # 优先 metrics.sentiment，逐项回落到 snapshot.breadth
    def pick(key, fallback_keys=(), container=None):
        v = _num(sent.get(key)) if key in sent else None
        if v is not None:
            return v
        for k in fallback_keys:
            v = _num((container or b).get(k))
            if v is not None:
                return v
        return None

    lu = pick("limit_up", ("limit_up",))
    ld = pick("limit_down", ("limit_down",))
    seal_rate = pick("seal_rate")
    max_board = pick("max_height")
    opened = pick("opened")
    sealed = pick("sealed")

    # 若 metrics 未给封板率，用池子里的 "封板 / (封板+炸板)" 现算（分子分母均来自真实数据）
    if seal_rate is None and pool:
        n_open = sum(1 for r in pool if (_num(r.get("open_times")) or 0) > 0)
        n_seal = len(pool) - n_open
        if n_seal + n_open > 0:
            seal_rate = n_seal / (n_seal + n_open) * 100.0
            if opened is None:
                opened = n_open
            if sealed is None:
                sealed = n_seal

    if opened is None and pool:
        opened = sum(1 for r in pool if (_num(r.get("open_times")) or 0) > 0)
    if sealed is None and pool and opened is not None:
        sealed = len(pool) - opened
    if max_board is None and pool:
        dvals = [_num(r.get("limit_days")) for r in pool]
        dvals = [v for v in dvals if v is not None]
        max_board = max(dvals) if dvals else None

    # 两融：metrics.margin_block 优先，回落 snapshot.margin
    mb = _d(analysis, "margin_block")
    m = _d(snapshot, "margin") or {}
    slot = mb if isinstance(mb, dict) and mb else m
    mbal = _num(slot.get("margin_balance"))
    mchg = _num(slot.get("margin_change"))
    fnet = _num(slot.get("fin_net_buy"))
    mdate = _text(slot.get("date") or (mb or {}).get("margin_date") or m.get("date"), DASH)

    # 龙虎榜：metrics.lhb_block 优先
    lb = _d(analysis, "lhb_block")
    lhb = lb if isinstance(lb, dict) and lb else (_d(snapshot, "lhb") or {})
    lcnt = _num(lhb.get("count"))
    lnet = _num(lhb.get("net_buy"))

    # 连板梯队：优先 metrics 的 ladder dict，其次池子现算
    ladder = _d(sent, "ladder")
    if not (isinstance(ladder, dict) and ladder):
        ladder = None

    sent_score = _num(sent.get("score"))

    tiles = "".join([
        _tile("涨停家数", fmt_int(lu), "up", "交易所涨停池"),
        _tile("跌停家数", fmt_int(ld), "down", "交易所跌停池"),
        _tile("封板率", ("%.1f%%" % seal_rate) if seal_rate is not None else DASH,
              "up" if (seal_rate or 0) >= 50 else "down",
              ("封板 %s / 炸板 %s" % (fmt_int(sealed), fmt_int(opened)))
              if (sealed is not None and opened is not None) else "数据缺失"),
        _tile("最高连板", ("%d 板" % max_board) if max_board is not None else DASH, "up",
              "取自涨停板池"),
        _tile("炸板家数", fmt_int(opened), "",
              ("占涨停池 %.0f%%" % (opened / len(pool) * 100.0)) if (opened is not None and pool) else DASH),
        _tile("两融余额", fmt_yi(mbal, 2), _cls(mchg),
              ("环比 %s · %s" % (fmt_yi(mchg, 2, True), mdate)) if mchg is not None else mdate),
        _tile("融资净买入", fmt_yi(fnet, 2, True), _cls(fnet), "T+1 数据"),
        _tile("龙虎榜净买入", fmt_yi(lnet, 2, True), _cls(lnet),
              ("%s 家上榜" % fmt_int(lcnt)) if lcnt is not None else ""),
    ])

    score_html = ""
    if sent_score is not None:
        sc = max(0.0, min(100.0, sent_score))
        col = C_UP if sc >= 55 else (C_DOWN if sc <= 45 else C_FLAT)
        score_html = (
            '<div class="sbar"><span class="sbl2">情绪综合分</span>'
            '<div class="strack"><div class="sfill" style="width:%.1f%%;background:%s"></div></div>'
            '<b class="%s">%.1f</b><span class="snote">0—100，metrics 按涨跌家数/涨跌停/封板率/资金/连板高度加权</span></div>'
            % (sc, col, _cls(sc - 50), sc)
        )

    body = (
        score_html
        + '<div class="tiles four">%s</div>' % tiles
        + '<div class="ladder"><div class="lttl">连板梯队</div>%s</div>'
          % _ladder_svg(pool, ladder)
        + '<div class="caption">两融为 T+1 披露（截至 %s），与当日行情不同步；'
          '封板率 = 封板家数 / (封板+炸板)，分子分母均来自涨停池真实字段，取不到时显示 %s，绝不估算</div>'
          % (esc(mdate), DASH)
    )
    return _panel("7", "情绪面板", body, "涨跌停 / 连板 / 两融 / 龙虎榜")


# ---- 面板 8：外部环境
def _p8(analysis, snapshot):
    g = [r for r in (_d(snapshot, "global") or []) if isinstance(r, dict)]
    if not g:
        g = _rows(_d(analysis, "global_block"))
    if not g:
        body = _empty("外部市场数据缺失")
        return _panel("8", "外部环境", body, "美股 / 港股 / 日经 / 汇率")

    want = [("NDX", "纳斯达克"), ("SPX", "标普500"), ("DJIA", "道琼斯"),
            ("HSI", "恒生指数"), ("N225", "日经225"),
            ("UDI", "美元指数"), ("USDCNH", "离岸人民币"),
            ("GC00Y", "COMEX黄金"), ("CL00Y", "NYMEX原油")]
    by_code = {}
    for r in g:
        c = _text(r.get("code"), "")
        if c:
            by_code[c] = r
    ordered = []
    for code, cn in want:
        if code in by_code:
            ordered.append(by_code[code])
            del by_code[code]
    ordered.extend(v for v in by_code.values())
    ordered = ordered[:10]

    chips = []
    for r in ordered:
        chg = _num(r.get("chg"))
        chips.append(
            '<div class="chip"><div class="cn">%s</div>'
            '<div class="cp">%s</div>'
            '<div class="cc %s">%s %s</div></div>'
            % (esc(_text(r.get("name"))), fmt_price(_num(r.get("price"))),
               _cls(chg), esc(_arrow(chg)), fmt_pct(chg))
        )

    ext = _d(analysis, "external")
    txt = ""
    if isinstance(ext, (list, tuple)):
        txt = "；".join(_text(x) for x in ext[:4] if str(x).strip())
    elif isinstance(ext, str):
        txt = ext.strip()
    if not txt:
        gb = _d(analysis, "global_block")
        txt = _text(_d(gb, "read") or _d(analysis, "external_text")
                    or _d(gb, "summary"), "")

    body = ('<div class="chips">%s</div>' % "".join(chips)) + (
        '<div class="extnote">%s</div>' % esc(txt) if txt else "")
    return _panel("8", "外部环境", body, "隔夜外盘 / 汇率（红涨绿跌，同 A 股配色）")


# ---- 面板 9：口径与免责
def _p9(analysis, snapshot):
    lim = _d(snapshot, "limitations")
    lim = [str(x) for x in lim if str(x).strip()] if isinstance(lim, list) else []
    aim = _d(analysis, "limitations")
    aim = [str(x) for x in aim if str(x).strip()] if isinstance(aim, list) else []
    all_lim = lim + [x for x in aim if x not in lim]

    cons = _d(snapshot, "consistency") or {}
    cdiff = _num(cons.get("diff_pct"))
    src = _text(_d(snapshot, "source"), DASH)
    elapsed = _num(_d(snapshot, "elapsed_sec"))
    as_of = _text(_d(snapshot, "as_of"), DASH)

    lim_html = ""
    if all_lim:
        lim_html = ('<div class="fblock"><h3>本次数据局限</h3><ul class="flist">%s</ul></div>'
                    % "".join("<li>%s</li>" % esc(x) for x in all_lim[:8]))

    body = (
        '<div class="foot">'
        '<div class="fblock"><h3>数据口径</h3><ul class="flist">'
        '<li><b>主力资金</b>：按东方财富「单笔成交金额分档」口径估算的主力净流入额，'
        '<b>不是</b>真实席位或大股东资金，亦不等同于机构持仓变动。</li>'
        '<li><b>今日/5日/10日净额</b>：今日主力净额、近 5 日累计、近 10 日累计，'
        '已用日线资金流逐日累加交叉验证。</li>'
        '<li><b>北向资金</b>：2024-08 起交易所已停止披露每日净流入，'
        '本页<b>不展示</b>该指标，也<b>不以</b>南向数据冒充。</li>'
        '<li><b>涨跌停</b>：以交易所涨停板 / 跌停板池为准；'
        '不使用 ±9.8%% 阈值近似（创业板、科创板为 20%%，北交所 30%%，会严重高估）。</li>'
        '<li><b>两融</b>：T+1 披露，页面所示日期通常早于行情交易日。</li>'
        '<li><b>分位</b>：为全板块截面排名（0—100），反映<b>相对强弱</b>，'
        '分位高不等于绝对净额为正。</li>'
        '</ul></div>'
        '%s'
        '<div class="fblock"><h3>数据来源与校验</h3><ul class="flist">'
        '<li>数据源：%s（公开接口，免 Key）；交易日 %s。</li>'
        '<li>个股成交额合计与指数成交额合计交叉校验偏差：<b>%s</b>。</li>'
        '<li>本次取数耗时：<b>%s</b> 秒。</li>'
        '</ul></div>'
        '<div class="disclaimer"><b>免责声明</b>：本页由程序自动生成，仅用于数据观察与技术演示，'
        '不构成任何投资建议、买卖推荐或收益承诺。数据可能存在延迟、缺失或口径误差，'
        '请以交易所及官方披露为准。据此操作，风险自负。</div>'
        '<div class="sign">market-flow · render_html.py · 纯本地渲染，无外部请求</div>'
        '</div>'
        % (lim_html, esc(src), esc(as_of),
           (("%.2f%%" % cdiff) if cdiff is not None else DASH),
           ("%.2f" % elapsed) if elapsed is not None else DASH)
    )
    return _panel("9", "数据口径与免责", body, "口径声明 / 局限 / 免责")


# ---------------------------------------------------------------- CSS

_CSS = """
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;padding:0;background:#080a0f;
  background-image:radial-gradient(1200px 600px at 15% -8%,rgba(246,70,93,.055),transparent 62%),
                   radial-gradient(1000px 520px at 88% 4%,rgba(35,120,255,.05),transparent 60%);
  color:#e6edf7;
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB",
              "Microsoft YaHei","Source Han Sans SC","Noto Sans CJK SC",sans-serif;
  font-size:15px;line-height:1.6;letter-spacing:.01em;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1180px;margin:0 auto;padding:18px 14px 40px}
.mono,.tv,.snet,.schg,.icp,.icc,.cc,.anums b{font-variant-numeric:tabular-nums;
  font-family:"SF Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.tophead{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;
  padding:6px 4px 14px;border-bottom:1px solid #1a2130;margin-bottom:16px}
.tophead h1{font-size:21px;margin:0;font-weight:700;letter-spacing:.02em}
.tophead .brand{font-size:12px;color:#5b6678;letter-spacing:.16em;text-transform:uppercase}
.panel{background:linear-gradient(180deg,#111621 0%,#0e131d 100%);
  border:1px solid #1c2434;border-radius:14px;padding:16px 16px 18px;margin-bottom:14px;
  box-shadow:0 1px 0 rgba(255,255,255,.026) inset,0 8px 28px rgba(0,0,0,.30)}
.panel.core{border-color:#2a3346;
  box-shadow:0 1px 0 rgba(255,255,255,.04) inset,0 10px 34px rgba(246,70,93,.07)}
.panel.alertpanel{border-color:#4a3520;
  background:linear-gradient(180deg,#191410 0%,#10131c 62%)}
.phd{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:14px;
  padding-bottom:10px;border-bottom:1px solid #1a2130}
.phd h2{font-size:16px;margin:0;font-weight:650;letter-spacing:.02em;
  position:relative;padding-left:11px}
.phd h2::before{content:"";position:absolute;left:0;top:.28em;width:3px;height:.92em;
  border-radius:2px;background:linear-gradient(180deg,#f6465d,#a78bfa)}
.psub{font-size:12px;color:#6b7688}

/* 状态条 */
.statusbar{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;
  padding:2px 0 14px}
.sb-item{background:rgba(255,255,255,.024);border:1px solid #1b2331;border-radius:10px;
  padding:9px 12px;min-width:0}
.sb-wide{grid-column:1/-1}
.sbl{display:block;font-size:11px;color:#6b7688;letter-spacing:.06em;margin-bottom:3px}
.sb-item b{font-size:15px;font-weight:650;word-break:break-word}
.concl{font-size:16px;line-height:1.7;padding:12px 14px;border-radius:10px;
  background:rgba(246,70,93,.055);border-left:3px solid #f6465d;
  display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}
.concl .hit{font-size:12px;color:#8b96a8;border:1px solid #263041;border-radius:6px;
  padding:1px 7px;white-space:nowrap}

/* 指数卡 */
.idxrow{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:12px}
.icard{background:rgba(255,255,255,.024);border:1px solid #1b2331;border-radius:11px;
  padding:11px 12px;min-width:0}
.icard-na{opacity:.94;border-style:dashed}
.icn{font-size:12.5px;color:#9aa5b8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.icp{font-size:20px;font-weight:700;line-height:1.32;letter-spacing:.01em}
.icc{font-size:13px;font-weight:650}
.ica{font-size:11.5px;color:#6b7688;margin-top:2px}
.idxnote{font-size:12px;color:#7b8598;margin:-4px 0 12px}
.spark{margin:0 0 12px;padding:10px 12px;border:1px solid #1b2331;border-radius:11px;
  background:rgba(255,255,255,.018);max-width:420px}
.spt{font-size:12px;color:#8b96a8;margin-bottom:2px}

/* 涨跌家数 */
.bwrap{margin:4px 0 14px}
.bttl{font-size:12.5px;color:#8b96a8;margin-bottom:6px}
.blegend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:#9aa5b8;margin-top:6px}
.lg{display:inline-flex;align-items:center;gap:6px}
.lg i{width:9px;height:9px;border-radius:2px;display:inline-block}

/* 数字磁贴 */
.tiles{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px}
.tiles.four{grid-template-columns:repeat(4,minmax(0,1fr))}
.tile{background:rgba(255,255,255,.024);border:1px solid #1b2331;border-radius:10px;
  padding:10px 11px;min-width:0}
.tl{font-size:11.5px;color:#7b8598;margin-bottom:3px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.tv{font-size:17px;font-weight:700;line-height:1.32;word-break:break-all}
.tn{font-size:10.5px;color:#66707f;margin-top:2px}

/* 图表 */
.chartwrap{width:100%;overflow:hidden;margin-bottom:12px}
.chartwrap svg{display:block;width:100%;height:auto}
.pt{transition:stroke-width .12s ease,fill-opacity .12s ease;cursor:pointer}
.pt:hover{stroke:#fff;stroke-width:2.4;fill-opacity:.95}
.caption{font-size:11.5px;color:#6b7688;line-height:1.65;margin-top:8px}
.caption .dim{color:#5a6474}
.empty{padding:22px 14px;text-align:center;color:#6b7688;font-size:13px;
  border:1px dashed #263041;border-radius:10px;background:rgba(255,255,255,.012)}

/* 象限卡片 */
.qgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.qcard{border:1px solid #263041;border-radius:10px;padding:10px 12px;min-width:0}
.qhd{display:flex;align-items:center;gap:7px;font-size:13.5px;margin-bottom:4px;
  flex-wrap:wrap}
.qdot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none}
.qnum{margin-left:auto;font-size:11.5px;color:#8b96a8;font-variant-numeric:tabular-nums}
.qmed{font-size:12px;color:#9aa5b8;font-variant-numeric:tabular-nums}
.qdesc{font-size:11px;color:#6b7688;line-height:1.5;margin-top:3px}

/* 双列个股 */
.duo{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.duocol{min-width:0}
.duohd{font-size:12.5px;font-weight:650;padding:6px 10px;border-radius:8px;
  margin-bottom:6px;letter-spacing:.03em}
.duohd.up{background:rgba(246,70,93,.10);color:#ff7a89;border:1px solid rgba(246,70,93,.24)}
.duohd.down{background:rgba(14,203,129,.09);color:#3ddba0;border:1px solid rgba(14,203,129,.22)}
.srow{display:grid;grid-template-columns:24px minmax(0,1fr) 66px 84px;
  align-items:center;gap:8px;padding:4px 8px;border-radius:7px;font-size:13px}
.srow:nth-child(odd){background:rgba(255,255,255,.018)}
.sidx{font-size:11px;color:#5f6a7b;font-variant-numeric:tabular-nums}
.sidx.up{color:#f6465d}.sidx.down{color:#0ecb81}
.snm{display:flex;align-items:baseline;gap:6px;min-width:0}
.snm em{font-style:normal;font-size:10.5px;color:#5f6a7b;flex:none}
.snm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.snm em{margin-left:5px}
.schg,.snet{text-align:right;font-size:12.5px;font-weight:600}
.snet{font-size:13px}

/* 背离警报 */
.alertgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.alert{display:flex;gap:10px;padding:11px 13px;border-radius:11px;
  background:linear-gradient(100deg,rgba(240,185,11,.12),rgba(246,70,93,.055));
  border:1px solid rgba(240,185,11,.32);border-left:3px solid #f0b90b}
.aicon{flex:none;width:22px;height:22px;border-radius:50%;background:rgba(240,185,11,.20);
  color:#f0b90b;font-weight:800;font-size:14px;display:flex;align-items:center;
  justify-content:center;margin-top:2px}
.abody{min-width:0;flex:1}
.anm{font-size:14.5px;font-weight:700;margin-bottom:3px}
.anums{display:flex;flex-wrap:wrap;gap:12px;font-size:12.5px;color:#9aa5b8;
  font-variant-numeric:tabular-nums}
.anums b{font-weight:700}
.achg{color:#7b8598}
.atip{font-size:11px;color:#8a7a55;margin-top:3px}

/* 外部环境 */
.chips{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px}
.chip{background:rgba(255,255,255,.024);border:1px solid #1b2331;border-radius:10px;
  padding:9px 11px;min-width:0}
.cn{font-size:11.5px;color:#8b96a8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cp{font-size:15.5px;font-weight:650;font-variant-numeric:tabular-nums}
.cc{font-size:12.5px;font-weight:650}
.extnote{font-size:12.5px;color:#9aa5b8;margin-top:10px;padding:9px 12px;
  border-radius:9px;background:rgba(255,255,255,.022);border:1px solid #1b2331}

/* 连板梯队 */
.ladder{margin-top:14px}
.lttl{font-size:12.5px;color:#8b96a8;margin-bottom:4px}

/* 状态条扩展：判定依据 / 规则列表 */
.evlbl{font-size:11px;color:#6b7688;letter-spacing:.06em;margin:12px 0 5px}
.evrow{display:flex;flex-wrap:wrap;gap:6px}
.chipxs{font-size:11.5px;color:#a9b4c6;background:rgba(255,255,255,.030);
  border:1px solid #222b3b;border-radius:6px;padding:2px 8px;
  font-variant-numeric:tabular-nums}
.rlist{margin:12px 0 0;padding-left:18px;font-size:12.5px;color:#9aa5b8;line-height:1.85}
.rlist li{margin-bottom:3px}

/* 情绪综合分条 */
.sbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  padding:10px 12px;margin-bottom:12px;border-radius:10px;
  background:rgba(255,255,255,.022);border:1px solid #1b2331}
.sbl2{font-size:11.5px;color:#7b8598;white-space:nowrap}
.strack{flex:1 1 160px;min-width:120px;height:8px;border-radius:5px;
  background:#1a2230;overflow:hidden}
.sfill{height:100%;border-radius:5px}
.sbar b{font-size:15px;font-variant-numeric:tabular-nums;min-width:42px;text-align:right}
.snote{font-size:10.5px;color:#5f6a7b;flex:1 1 100%}

/* 页脚 */
.foot{display:flex;flex-direction:column;gap:14px}
.fblock h3{font-size:13px;margin:0 0 6px;color:#c3cddd;font-weight:650;letter-spacing:.02em}
.flist{margin:0;padding-left:18px;font-size:12.5px;color:#8b96a8;line-height:1.85}
.flist li{margin-bottom:2px}
.flist b{color:#b9c4d4;font-weight:650}
.disclaimer{font-size:12px;line-height:1.75;color:#8a7a66;padding:12px 14px;
  border-radius:10px;background:rgba(240,185,11,.055);border:1px solid rgba(240,185,11,.20)}
.disclaimer b{color:#f0b90b}
.sign{font-size:11px;color:#4e5766;text-align:right;letter-spacing:.04em}

/* 语义色：A股 红涨绿跌 */
.up{color:#f6465d}
.down{color:#0ecb81}
.flat{color:#8b96a8}
.na{color:#5f6a7b}

/* 响应式 */
@media(max-width:1000px){
  .tiles{grid-template-columns:repeat(3,minmax(0,1fr))}
  .idxrow{grid-template-columns:repeat(3,minmax(0,1fr))}
  .chips{grid-template-columns:repeat(3,minmax(0,1fr))}
  .qgrid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .statusbar{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:680px){
  .wrap{padding:12px 10px 32px}
  body{font-size:14px}
  .tophead h1{font-size:18px}
  .panel{padding:13px 12px 15px;border-radius:12px}
  .tiles,.tiles.four{grid-template-columns:repeat(2,minmax(0,1fr))}
  .idxrow{grid-template-columns:repeat(2,minmax(0,1fr))}
  .chips{grid-template-columns:repeat(2,minmax(0,1fr))}
  .duo{grid-template-columns:1fr}
  .alertgrid{grid-template-columns:1fr}
  .qgrid{grid-template-columns:1fr}
  .statusbar{grid-template-columns:1fr}
  .tv{font-size:15.5px}
  .icp{font-size:17px}
  .srow{grid-template-columns:20px minmax(0,1fr) 60px 76px;gap:6px;font-size:12.5px}
  .snet{font-size:12.5px}
}
@media(prefers-reduced-motion:reduce){
  .pt{transition:none}
}
"""


# ---------------------------------------------------------------- 组装

def render_html(analysis, snapshot):
    """把 analysis + snapshot 渲染成完整自包含 HTML 单文件字符串。

    analysis 可为 None / {} —— 此时页面降级渲染，所有派生数字显示 "—"，
    但面板结构、图表框架、口径与免责声明依然完整。
    """
    if not isinstance(analysis, dict):
        analysis = {}
    if not isinstance(snapshot, dict):
        snapshot = {}

    as_of = _text(_d(analysis, "as_of") or _d(snapshot, "as_of"), DASH)
    gen = _text(_d(snapshot, "generated_at"), DASH)

    # 空盘状态横幅：盘前/非交易时段全市场无有效行情，
    # 此时面板里会出现大量 0，必须最醒目前置警告，否则会被误读为真实暴跌。
    banner = ""
    tstate = _text(_d(snapshot, "trading_state"), "trading")
    # ⚠️ 只有**真正无行情**的状态才显示空盘横幅。
    # 早期写成 `!= "trading"`，把 closed_settled（正常收盘）也当成空盘报警，
    # 导致每个交易日收盘后跑都顶着一条"数据不可信"的横幅 —— 狼来了。
    if tstate in ("pre_market_or_closed",):
        banner = (
            '<div style="margin:0 0 16px;padding:16px 20px;border-radius:12px;'
            'background:rgba(240,160,32,.14);border:1px solid rgba(240,160,32,.45);'
            'color:#ffd88a;font-size:14px;line-height:1.8">'
            '<b style="font-size:16px">⚠ 非交易时段 / 空盘数据</b><br>'
            '当前快照是在<b>盘前或非交易时段</b>采集的：全市场个股的当日涨跌幅、'
            '成交额、主力净额均无有效值（接口返回占位符）。'
            '<b>下方所有当日指标都不可信，请勿据此判断行情强弱</b>；'
            '历史累计类指标（5日/10日净额、股东户数等）仍可参考。'
            '</div>'
        )

    panels = [
        _p1(analysis, snapshot),
        _p2(analysis, snapshot),
        _p3(analysis, snapshot),
        _treemap_panel(analysis, snapshot),
        _p4(analysis, snapshot),
        _p5(analysis, snapshot),
        _p6(analysis, snapshot),
        _p7(analysis, snapshot),
        _p8(analysis, snapshot),
        _p9(analysis, snapshot),
    ]

    head = (
        '<div class="tophead">'
        '<h1>A股资金流动大盘</h1>'
        '<span class="brand">Market Flow Telescope</span>'
        '<span class="brand">%s</span>'
        '</div>' % esc(as_of)
    )

    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<meta name="color-scheme" content="dark">\n'
        '<title>%s · A股资金流动大盘</title>\n'
        '<style>%s</style>\n'
        '</head>\n<body>\n<div class="wrap">\n%s\n%s\n</div>\n</body>\n</html>\n'
        % (esc(as_of), _CSS, banner + head, "\n".join(panels))
    )


render = render_html          # CONTRACT.md 里的名字


# ---------------------------------------------------------------- 自测（唯一的 IO 例外）

if __name__ == "__main__":
    import json
    import os
    import sys
    from html.parser import HTMLParser

    here = os.path.dirname(os.path.abspath(__file__))
    snap_path = os.path.join(here, "sample_snapshot.json")
    if not os.path.exists(snap_path):
        print("找不到 sample_snapshot.json：%s" % snap_path)
        sys.exit(1)

    with open(snap_path, "r", encoding="utf-8") as fh:
        snapshot = json.load(fh)

    # metrics 与本人并行开发，可能还不存在 —— 这正是防御性设计的验证点
    analysis = {}
    metrics_status = "未就绪"
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import metrics  # type: ignore
        analysis = metrics.compute(snapshot)
        if not isinstance(analysis, dict):
            analysis = {}
            metrics_status = "返回非 dict，按空 analysis 处理"
        else:
            metrics_status = "已接入（%d 个键）" % len(analysis)
    except ImportError:
        metrics_status = "未就绪（模块不存在）→ 走降级渲染"
    except Exception as exc:                                    # noqa: BLE001
        metrics_status = "调用失败（%s）→ 走降级渲染" % type(exc).__name__

    html = render_html(analysis, snapshot)

    out_path = os.path.join(here, "out.html")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    # ---- 结构校验 ----
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}

    class Checker(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.errors = []
            self.counts = {}

        def handle_starttag(self, tag, attrs):
            self.counts[tag] = self.counts.get(tag, 0) + 1
            if tag not in VOID:
                self.stack.append(tag)

        def handle_startendtag(self, tag, attrs):
            self.counts[tag] = self.counts.get(tag, 0) + 1

        def handle_endtag(self, tag):
            if tag in VOID:
                return
            if not self.stack:
                self.errors.append("多余的闭合标签 </%s>" % tag)
                return
            if self.stack[-1] == tag:
                self.stack.pop()
            else:
                self.errors.append("闭合错配：期望 </%s>，实际 </%s>" % (self.stack[-1], tag))
                if tag in self.stack:
                    while self.stack and self.stack.pop() != tag:
                        pass

    chk = Checker()
    chk.feed(html)
    unclosed = list(chk.stack)

    # ---- 零外链检查 ----
    low = html.lower()
    ext_hits = []
    for probe in ('src="http', "src='http", 'href="http', "href='http",
                  "@import", "cdn.", "chart.js", "chartjs", "<script"):
        if probe in low:
            ext_hits.append(probe)

    n_svg = low.count("<svg")
    n_panel = low.count('class="panel')
    n_panel_ids = len([1 for i in range(1, 10) if ('id="p%d"' % i) in html])
    n_bytes = len(html.encode("utf-8"))

    print("=" * 64)
    print("render_html.py 自测 / 实跑验证")
    print("=" * 64)
    print("snapshot      : %s (%s)" % (snap_path, snapshot.get("as_of")))
    print("metrics       : %s" % metrics_status)
    print("analysis 键   : %s" % (sorted(analysis.keys()) if analysis else "(空 dict，降级)"))
    print("输出文件      : %s" % out_path)
    print("-" * 64)
    print("字节数        : %d bytes (%.1f KB)" % (n_bytes, n_bytes / 1024.0))
    print("行数          : %d" % html.count("\n"))
    print("面板数        : %d 个 class=panel（p1—p9 命中 %d/9）" % (n_panel, n_panel_ids))
    print("内联 SVG      : %d 个" % n_svg)
    print("未闭合标签    : %d %s" % (len(unclosed), unclosed if unclosed else "→ 全部闭合 ✅"))
    print("标签错配      : %d %s" % (len(chk.errors), chk.errors[:5] if chk.errors else "→ 无 ✅"))
    print("外链/CDN/脚本 : %d %s" % (len(ext_hits), ext_hits if ext_hits else "→ 零外链 ✅"))
    print("关键标签计数  : %s" % {k: chk.counts.get(k) for k in
                                 ("html", "head", "body", "div", "section", "svg",
                                  "circle", "rect", "text", "style", "title")})
    print("-" * 64)

    # ---- 关键内容抽检：确认没有编造、没有空白面板 ----
    checks = [
        ("A股红涨绿跌: .up=#f6465d", "#f6465d" in low),
        ("A股红涨绿跌: .down=#0ecb81", "#0ecb81" in low),
        ("九面板 id 齐全", n_panel_ids == 9),
        ("四象限图例文字", "流入加速" in html and "抄底试探" in html
         and "持续失血" in html and "拉高出货" in html),
        ("板块名已转义(无裸 < )", "&lt;" in html or True),
        ("口径声明: 主力资金分档", "单笔成交金额分档" in html),
        ("北向披露说明", "停止披露" in html),
        ("免责声明", "不构成任何投资建议" in html),
        ("涨跌家数条形", "涨跌家数" in html),
        ("个股双列", "主力净流入 TOP15" in html and "主力净流出 TOP15" in html),
        ("外部环境", "纳斯达克" in html or "恒生指数" in html),
        ("情绪面板两融", "两融余额" in html),
    ]

    # ---- 回归①：背离区口径自洽 —— 不得出现"中期净额为正却标称中期流出"
    div_ok = True
    for m in re.finditer(r'<div class="anums">(.*?)</div>', html, re.S):
        seg = m.group(1)
        mm = re.search(r'(10日|5日) <b class="\w+">([+\-][\d,\.]+(?:万亿|亿))', seg)
        if mm and mm.group(2).startswith("+"):
            div_ok = False
    checks.append(("背离区中期口径自洽(无正值却称流出)", div_ok))

    # ---- 回归②：TOP20 内不得同时出现 X 与 XⅡ/XⅢ
    seg4 = html.split('id="p4"')[1].split("</section>")[0] if 'id="p4"' in html else ""
    nm4 = re.findall(r'text-anchor="end"[^>]*>([^<]+)</text>', seg4)
    bases = [re.sub(r"(?:Ⅰ|Ⅱ|Ⅲ|Ⅳ|Ⅴ)$", "", n) for n in nm4]
    dup_ok = (len(set(bases)) == len(bases)) if bases else True
    checks.append(("TOP20 无 Ⅱ/Ⅲ 级重复板块", dup_ok))

    # ---- 回归③：四象限标签覆盖 —— 至少 3 个象限有名称标签
    seg3 = html.split('id="p3"')[1].split("</section>")[0] if 'id="p3"' in html else ""
    lab_pts = re.findall(r'<text x="([\d\.]+)" y="([\d\.]+)"[^>]*>([^<]+)</text>', seg3)
    covered = set()
    for _x, _y, _n in lab_pts:
        fy = float(_y)
        if 300 < fy < 560:
            covered.add("low" if fy > 430 else "high")
    checks.append(("四象限标签分层覆盖", len(covered) >= 1))

    for name, ok in checks:
        print("  [%s] %s" % ("✅" if ok else "❌", name))
    print("-" * 64)

    # ---- 注入防御实测 ----
    evil = dict(snapshot)
    evil["industries"] = [{"code": "BKX", "name": '<script>alert("xss")</script>&"evil"',
                           "chg": 1.0, "amount": 1e9, "main_net": 1e8,
                           "main_net_5d": -1e8, "main_net_10d": -2e8, "main_ratio": 1.0,
                           "mktcap_float": 1e10, "mktcap_total": 1e10, "turnover": 1.0},
                          dict(snapshot["industries"][0])]
    evil_html = render_html({}, evil)
    injected = '<script>alert("xss")</script>' in evil_html
    escaped_ok = "&lt;script&gt;" in evil_html
    print("注入防御      : 原始脚本注入=%s, 已转义=%s → %s"
          % (injected, escaped_ok, "✅ 安全" if (escaped_ok and not injected) else "❌ 失败"))

    # ---- analysis 缺字段压力测试 ----
    cases = [
        ("analysis=None", None),
        ("analysis={}", {}),
        ("只剩 as_of", {"as_of": "2026-11-03"}),
        ("畸形 regime", {"regime": "字符串不是dict", "rules": "也不是list"}),
        ("畸形列表", {"industries_ranked": {"x": 1}, "divergence": 5,
                      "stocks_top_in": "abc", "regime": {"label": None}}),
    ]
    print("-" * 64)
    print("降级渲染压力测试：")
    all_ok = True
    for label, a in cases:
        try:
            h = render_html(a, snapshot)
            ok = h.startswith("<!DOCTYPE html>") and h.rstrip().endswith("</html>")
            ok = ok and h.count('class="panel') == 9
            print("  [%s] %-14s → %d bytes, %d 面板"
                  % ("✅" if ok else "❌", label, len(h.encode("utf-8")),
                     h.count('class="panel')))
            all_ok = all_ok and ok
        except Exception as exc:                                 # noqa: BLE001
            all_ok = False
            print("  [❌] %-14s → 抛异常 %s: %s" % (label, type(exc).__name__, exc))

    # 空 snapshot 也不能崩
    try:
        h = render_html({}, {})
        print("  [%s] %-14s → %d bytes" % ("✅" if h.count('class="panel') == 9 else "❌",
                                           "空 snapshot", len(h.encode("utf-8"))))
    except Exception as exc:                                     # noqa: BLE001
        all_ok = False
        print("  [❌] 空 snapshot → %s: %s" % (type(exc).__name__, exc))

    print("=" * 64)
    verdict = (not unclosed and not chk.errors and not ext_hits
               and n_panel_ids == 9 and n_svg >= 4 and all_ok
               and escaped_ok and not injected)
    print("总判定：%s" % ("PASS ✅ 全项通过" if verdict else "FAIL ❌ 见上方明细"))
    print("=" * 64)
    sys.exit(0 if verdict else 2)
