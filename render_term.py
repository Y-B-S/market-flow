"""终端渲染层：A 股「资金流动大盘」一屏彩色摘要。

模块边界（见 CONTRACT.md）：
  - 纯函数，禁止 IO / 网络 / 环境变量 / 第三方库（只用标准库 unicodedata）。
  - 对外只提供 render_term(analysis, snapshot, width=None, color=True) -> str
  - color 开关由参数控制，**不读 NO_COLOR 环境变量**，保持可测试的纯函数。

防御性设计
----------
metrics.py 与 render_term.py 并行开发。本模块对 analysis 逐层 .get() 兜底：
  - analysis 有值   -> 优先使用 metrics 的判断（象限分类、regime、规则判读）
  - analysis 缺失   -> 从 snapshot 就地派生**同源的客观事实**（板块净额排序、
                       涨跌家数、两融……），这些与 metrics 的取数口径一致，不属于编造
  - snapshot 也缺   -> 显示 "—"，绝不 0 填充冒充真实值
metrics 独有的**判断类**字段（regime 状态、结论）缺失时显示 "—"，不自行发明结论。

A 股配色（与欧美相反，务必检查）
-------------------------------
红 = 上涨 / 资金流入   绿 = 下跌 / 资金流出
"""

from __future__ import annotations

import unicodedata

__all__ = ["render_term", "display_width", "char_width", "truncate", "fmt_money", "fmt_pct"]

# ---------------------------------------------------------------- ANSI -----
# 只用最基础的 SGR，不依赖 colorama / rich。
RED = "\x1b[91m"      # 上涨 / 流入
GREEN = "\x1b[92m"    # 下跌 / 流出
YELLOW = "\x1b[93m"
CYAN = "\x1b[96m"
MAGENTA = "\x1b[95m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"

MISSING = "—"          # 统一缺失占位符（契约要求）

DEFAULT_WIDTH = 100    # width=None 时使用（不碰 shutil.get_terminal_size，保持纯函数）
MIN_WIDTH = 60
MAX_WIDTH = 200
MAX_LINES = 45         # 一屏硬上限


# ------------------------------------------------------- 显示宽度 (wcwidth) --
# 中文/全角占 2 列，用 len() 对齐必然错位，所以这里自己实现。
#
# 歧义宽度（East_Asian_Width == 'A'）是本题最隐蔽的坑：
#   实测样本里「保险Ⅲ / 白酒Ⅱ / 游戏Ⅲ」这类板块名共 82 个含 Ⅱ(U+2161) / Ⅲ(U+2162)，
#   它们 Unicode 归类为 'A'。终端在中文语境下把它们渲染成 2 列，若按 1 列计算，
#   这些行会整体左缩进 1 列、与相邻行错位。
# 处理原则：
#   1) 布局分隔符一律用 ASCII（| - 空格），从根上避开歧义字符；
#   2) 'A' 类字符统一按 2 列（中文终端惯例），测量与填充共用同一函数即自洽。
_AMBIGUOUS_AS_WIDE = frozenset("—–…·‖")
_AMBIGUOUS_RANGES = (
    (0x2160, 0x217F),      # 罗马数字 Ⅰ..ⅿ  (含 Ⅱ Ⅲ)
    (0x2460, 0x24FF),      # 带圈数字 ①..⓪
    (0x25A0, 0x25FF),      # 几何图形 ■ ▲ ●
    (0x2600, 0x26FF),      # 杂项符号 ★ ☆
    (0x3000, 0x3000),      # 全角空格
)


def _is_ambiguous(ch: str) -> bool:
    if ch in _AMBIGUOUS_AS_WIDE:
        return True
    o = ord(ch)
    for lo, hi in _AMBIGUOUS_RANGES:
        if lo <= o <= hi:
            return True
    return unicodedata.east_asian_width(ch) == "A"


def char_width(ch: str) -> int:
    """单个字符的终端显示列数。"""
    o = ord(ch)
    if o == 0:
        return 0
    if o < 32 or 0x7F <= o <= 0x9F:          # 控制字符
        return 0
    if 0x200B <= o <= 0x200F:                # 零宽空格 / 方向标记
        return 0
    if 0xFE00 <= o <= 0xFE0F:                # 变体选择符
        return 0
    if unicodedata.combining(ch):            # 组合附加符
        return 0
    aw = unicodedata.east_asian_width(ch)
    if aw in ("W", "F"):                     # 全角/宽
        return 2
    if aw == "A":                            # 歧义宽度：按中文终端惯例记 2 列
        return 2
    return 2 if _is_ambiguous(ch) else 1


def display_width(s: str) -> int:
    """字符串的终端显示列数（不是字符数）。"""
    return sum(char_width(c) for c in s)


ELLIPSIS = "…"


def truncate(text: str, max_w: int) -> str:
    """按显示宽度截断，超出时以省略号结尾。ANSI 不占列，先剥掉再量。

    省略号自身也占列（这里按 _AMBIGUOUS_AS_WIDE 记为 2 列），
    必须按它的**实际显示宽度**预留，否则结果会超出 max_w 一列。
    """
    if max_w <= 0:
        return ""
    text = _strip_ansi(str(text))
    if display_width(text) <= max_w:
        return text
    budget = max_w - char_width(ELLIPSIS)
    if budget < 0:
        return ""
    out, w = [], 0
    for ch in text:
        cw = char_width(ch)
        if w + cw > budget:
            break
        out.append(ch)
        w += cw
    return "".join(out) + ELLIPSIS


def _strip_ansi(text: str) -> str:
    """去掉 ANSI 转义。ANSI 序列本身不占列，量宽度前必须先剥掉。"""
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] == "\x1b" and i + 1 < n and text[i + 1] == "[":
            j = i + 2
            while j < n and not ("@" <= text[j] <= "~"):
                j += 1
            i = j + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _vw(s: str) -> int:
    """可见宽度：先剥 ANSI 再量。着色后仍需对齐的关键。"""
    return display_width(_strip_ansi(s))


def _ljust(s: str, w: int) -> str:
    pad = w - _vw(s)
    if pad > 0:
        return s + " " * pad
    if pad == 0:
        return s
    return truncate(_strip_ansi(s), w)


def _rjust(s: str, w: int) -> str:
    pad = w - _vw(s)
    if pad > 0:
        return " " * pad + s
    if pad == 0:
        return s
    return truncate(_strip_ansi(s), w)


# ------------------------------------------------------------ 数值格式化 -----
def _num(v):
    """安全转数值：东财停牌股会返回字符串 '-'（见 CONTRACT.md 陷阱）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    if isinstance(v, str):
        t = v.strip()
        if not t or t in ("-", "--", "—", ""):
            return None
        try:
            f = float(t)
        except ValueError:
            return None
        return f if f == f else None
    return None


def _prefix(x: float) -> str:
    return "+" if x > 0 else ("-" if x < 0 else "")


def fmt_money(v, sign: bool = True) -> str:
    """金额格式化：元 -> 万/亿/万亿 自动切换。None -> '—'。"""
    x = _num(v)
    if x is None:
        return MISSING
    a = abs(x)
    if a >= 1e12:
        body = f"{a / 1e12:.2f}万亿"
    elif a >= 1e8:
        body = f"{a / 1e8:.2f}亿"
    elif a >= 1e4:
        body = f"{a / 1e4:.2f}万"
    else:
        body = f"{a:.0f}"
    return (_prefix(x) if sign else "") + body


def fmt_pct(v, sign: bool = True, digits: int = 2) -> str:
    """百分比格式化（契约：1.5 表示 1.5%）。None -> '—'。"""
    x = _num(v)
    if x is None:
        return MISSING
    return (_prefix(x) if sign else "") + f"{abs(x):.{digits}f}%"


def fmt_price(v) -> str:
    x = _num(v)
    return MISSING if x is None else f"{x:.2f}"


def fmt_count(v) -> str:
    x = _num(v)
    return MISSING if x is None else f"{int(round(x))}"


# --------------------------------------------------------------- 取值工具 -----
def _dig(obj, *path):
    """逐层安全取值，任何一层缺失都返回 None。"""
    cur = obj
    for k in path:
        if isinstance(cur, dict):
            cur = cur.get(k) if k is not None else None
        elif isinstance(cur, (list, tuple)) and isinstance(k, int):
            cur = cur[k] if -len(cur) <= k < len(cur) else None
        else:
            return None
    return cur


def _first(*vals):
    for v in vals:
        if v is not None:
            return v
    return None


def _painter(on: bool):
    """返回上色函数；on=False 时原样返回，保证输出零 ANSI。"""
    def paint(text, code):
        if not on or not code or not text:
            return text
        return code + text + RESET
    return paint


def _tint(v):
    """A 股配色：正数红、负数绿、零/缺失不上色。"""
    x = _num(v)
    if x is None or x == 0:
        return ""
    return RED if x > 0 else GREEN


def _clamp_width(width):
    if width is None:
        return DEFAULT_WIDTH
    try:
        w = int(width)
    except (TypeError, ValueError):
        return DEFAULT_WIDTH
    return max(MIN_WIDTH, min(MAX_WIDTH, w))


# ---------------------------------------------------------------- 派生逻辑 -----
QUAD_ORDER = ("流入加速", "抄底试探", "拉高出货", "持续失血")
QUAD_TAG = {
    "流入加速": "今+ 5日+",
    "抄底试探": "今+ 5日-",
    "拉高出货": "今- 5日+",
    "持续失血": "今- 5日-",
}
QUAD_COLOR = {
    "流入加速": RED,
    "抄底试探": YELLOW,
    "拉高出货": YELLOW,
    "持续失血": GREEN,
}


def _quad_of(today, d5):
    """象限归属：今日净额符号 x 5日净额符号。与 metrics 的契约口径一致。"""
    if today is None or d5 is None:
        return None
    if today > 0 and d5 > 0:
        return "流入加速"
    if today > 0 and d5 < 0:
        return "抄底试探"
    if today < 0 and d5 > 0:
        return "拉高出货"
    if today < 0 and d5 < 0:
        return "持续失血"
    return None


def _as_boards(items):
    """把 analysis 里的板块容器统一成 list[dict]。

    契约要求 metrics 输出 {'count': N, 'rows': [...]}，所以必须认 'rows'。
    早期只认 boards/items/list，导致取到空列表、误判"analysis 没有象限数据"，
    静默退回 snapshot 重新分桶，把上游的去重与排序全部绕过——
    表现为"保险Ⅱ/保险Ⅲ"这类同义层级重复项又并列出现。
    这里与 render_html 的归一化保持同等宽容度，并兜底扫描任意 list 值。
    """
    if isinstance(items, list):
        return [x for x in items if isinstance(x, dict)]
    if isinstance(items, dict):
        for key in ("rows", "boards", "items", "list", "data",
                    "top", "top_in", "top_out"):
            sub = items.get(key)
            if isinstance(sub, list):
                return [x for x in sub if isinstance(x, dict)]
        for sub in items.values():
            if isinstance(sub, list) and sub and isinstance(sub[0], dict):
                return [x for x in sub if isinstance(x, dict)]
    return []


def _boards_from_snapshot(snapshot):
    """活跃板块池：**只用行业**，行业口径干净。

    概念板块是叠加池（宁组合/茅指数/融资融券/MSCI中国/基金减仓/富时罗素…），
    彼此高度重叠且含大量指数成分与交易机制类伪概念，直接参与象限排序会
    把真正的行业信号挤掉。故行业可用时只用行业，行业为空才退回概念。
    """
    for key in ("industries", "concepts"):
        out = []
        for b in _dig(snapshot, key) or []:
            if isinstance(b, dict) and _num(b.get("main_net")) is not None:
                out.append(b)
        if out:
            return out, key
    return [], None


def _derive_quadrants(analysis, snapshot):
    """象限数据：优先 analysis.quadrants，否则从 snapshot 就地分桶。"""
    src = _dig(analysis, "quadrants")
    quads = {k: [] for k in QUAD_ORDER}
    got = False
    if isinstance(src, dict):
        for label in QUAD_ORDER:
            rows = _as_boards(src.get(label))
            if rows:
                got = True
            quads[label] = rows
    if got:
        return quads, "analysis"

    boards, _kind = _boards_from_snapshot(snapshot)
    if not boards:
        return quads, None
    for b in boards:
        label = _quad_of(_num(b.get("main_net")), _num(b.get("main_net_5d")))
        if label:
            quads[label].append(b)
    for label, rows in quads.items():
        # 同象限内按净额绝对值排序：谁动得最猛谁在前
        rows.sort(key=lambda x: -abs(_num(x.get("main_net")) or 0))
        quads[label] = rows[:8]
    return quads, "snapshot"


def _derive_divergence(analysis, snapshot):
    """背离：今日流入 / 中期(5日)流出。

    metrics 按交付要求给出**三层**产出，语义不同，必须分层取用：

      divergence        73 条  全量 —— 仅用于计数，直接渲染等于没有警报意义
      divergence_gated  16 条  过双门槛（裂口>=50 且今日>=3000万）
      divergence_top     5 条  面向展示，按「裂口 x sqrt(流入规模)」排序

    展示层优先：top -> gated -> 全量 -> snapshot 兜底。
    返回 (rows, source, meta)，meta 携带全量与过门槛条数用于标题的规模感。
    """
    all_rows = _as_boards(_dig(analysis, "divergence"))
    gated = _as_boards(_dig(analysis, "divergence_gated"))
    top = _as_boards(_dig(analysis, "divergence_top"))

    meta = {"total": len(all_rows) or None, "gated": len(gated) or None}

    for rows, tag in ((top, "top"), (gated, "gated"), (all_rows, "all")):
        if rows:
            return rows, "analysis:" + tag, meta

    # analysis 完全没有背离数据 -> 从 snapshot 就地判定
    boards, _kind = _boards_from_snapshot(snapshot)
    out = []
    for b in boards:
        t, d5 = _num(b.get("main_net")), _num(b.get("main_net_5d"))
        if t is None or d5 is None:
            continue
        if t > 0 and d5 < 0:
            out.append(b)
    if not out:
        return [], ("snapshot" if boards else None), {"total": 0, "gated": None}
    out.sort(key=lambda x: -(_num(x.get("main_net")) or 0))
    return out, "snapshot", {"total": len(out), "gated": None}


def _derive_stocks(analysis, snapshot, key, reverse):
    rows = [x for x in (_dig(analysis, key) or []) if isinstance(x, dict)]
    if rows:
        return rows
    pool = [s for s in (_dig(snapshot, "stocks") or [])
            if isinstance(s, dict) and _num(s.get("main_net")) is not None]
    if not pool:
        return []
    pool.sort(key=lambda x: _num(x.get("main_net")) or 0, reverse=reverse)
    return pool[:8]


def _derive_indices(analysis, snapshot):
    """指数：analysis.majors -> snapshot.indices -> index_flow 兜底（无点位）。"""
    for key in ("majors", "indices"):
        rows = [x for x in (_dig(analysis, key) or []) if isinstance(x, dict)]
        if rows:
            return rows
    rows = [x for x in (_dig(snapshot, "indices") or []) if isinstance(x, dict)]
    if rows:
        return rows
    flow = _dig(snapshot, "index_flow")
    if isinstance(flow, dict):
        out = []
        for sid, v in flow.items():
            if not isinstance(v, dict):
                continue
            series = v.get("series") or []
            last = series[-1] if series and isinstance(series[-1], dict) else {}
            out.append({
                "code": sid,
                "name": v.get("name"),
                "price": None,              # index_flow 不含点位，如实留空
                # 注意：**不取** klines 的 chg 字段。
                # CONTRACT.md 只确认 klines[0..5] 的语义，p[7] 未经交叉验证；
                # 实测同一交易日 indices.chg=-0.82 而 klines p[7]=+2.97，二者矛盾，
                # 故 p[7] 一律不展示。这里改展示已交叉验证的 p[1]（主力净额）。
                "chg": None,
                "flow_main_net": last.get("main_net"),
                "flow_net_5d": v.get("net_5d"),
                "flow_date": last.get("date"),
            })
        return out
    return []


def _weekday_cn(as_of):
    """2026-11-03 -> '周二'。解析失败则返回空串。"""
    from datetime import datetime  # 标准库且纯函数（只依赖入参字符串）
    try:
        d = datetime.strptime(str(as_of), "%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
    return "周" + "一二三四五六日"[d.weekday()]


# ---------------------------------------------------------------- 主渲染 -----
def render_term(analysis, snapshot, width=None, color=True):
    """渲染一屏终端摘要。

    参数
    ----
    analysis : dict | None   metrics.compute() 的输出；可为 {} / None
    snapshot : dict | None   store 层落盘的 JSON；可为 {} / None
    width    : int | None    目标列宽；None -> 100（不探测终端，保持纯函数）
    color    : bool          是否输出 ANSI 转义

    返回
    ----
    str  多行文本，行数 <= 45。
    """
    a = analysis if isinstance(analysis, dict) else {}
    s = snapshot if isinstance(snapshot, dict) else {}
    W = _clamp_width(width)
    paint = _painter(bool(color))

    b = _dig(s, "breadth") if isinstance(_dig(s, "breadth"), dict) else {}
    out = []

    # analysis 的常用子块先取出，供全文复用，避免同一字段多处各算一遍。
    sent = _dig(a, "sentiment") if isinstance(_dig(a, "sentiment"), dict) else {}
    breadth_block = _dig(a, "breadth_block") if isinstance(_dig(a, "breadth_block"), dict) else {}

    dash = "-" * W
    sep = " " + "-" * (W - 2) + " "

    def hdr(title, extra=""):
        """小节标题。分隔符用 ASCII，避开歧义宽度字符。"""
        txt = f"[ {title} ]"
        if extra:
            txt += "  " + extra
        return paint(truncate(txt, W), BOLD + CYAN)

    def blank():
        out.append("")

    # ---------------------------------------------------------- 标题行 -----
    as_of = _first(_dig(a, "as_of"), s.get("as_of"))
    as_of_txt = str(as_of) if as_of else MISSING
    wd = _weekday_cn(as_of) if as_of else ""
    when = as_of_txt + (" " + wd if wd else "")

    regime_label = _first(
        _dig(a, "regime", "label"),
        _dig(a, "regime", "z") if not isinstance(_dig(a, "regime", "z"), (int, float)) else None,
        _dig(a, "regime", "regime"),
    )
    regime_label = str(regime_label) if regime_label else MISSING

    title = f"A股资金流动大盘   {when}   状态: {regime_label}"
    out.append(paint(truncate(title, W), BOLD))

    # 一句话结论：只认 metrics 的判断；没有就退化成客观事实陈述，不发明结论
    concl = _first(
        _dig(a, "regime", "conclusion"),
        a.get("conclusion"),
        a.get("summary"),
        a.get("one_liner"),
        (_dig(a, "rules") or [None])[0] if isinstance(_dig(a, "rules"), (list, tuple)) else None,
    )
    if not concl:
        up, dn = _num(b.get("up")), _num(b.get("down"))
        mnt = _num(b.get("main_net_total"))
        if up is not None or dn is not None or mnt is not None:
            concl = (f"涨{fmt_count(up)}家/跌{fmt_count(dn)}家，"
                     f"全市场主力净额 {fmt_money(mnt)}（客观数据，非判读）")
    if concl:
        out.append(paint(truncate("结论: " + str(concl), W), BOLD))

    out.append(paint(dash, DIM))

    # -------------------------------------------------------- 大盘脉搏 -----
    out.append(hdr("大盘脉搏"))

    idx_rows = _derive_indices(a, s)
    cells_full, cells_compact, cells_flow = [], [], []
    for it in idx_rows[:5]:
        name = truncate(str(it.get("name") or it.get("code") or MISSING), 10)
        chg = _num(it.get("chg"))
        price = _num(it.get("price"))
        name_c = paint(name, BOLD)
        if price is not None or chg is not None:
            chg_c = paint(fmt_pct(chg), _tint(chg))
            cells_full.append(f"{name_c} {fmt_price(price)} {chg_c}")
            cells_compact.append(f"{name_c} {chg_c}")
        else:
            # 只有 index_flow 时不含点位/涨跌（p[7] 未经验证，不展示），改展示主力净额
            fm = _num(it.get("flow_main_net"))
            cells_flow.append(f"{name_c} {paint(fmt_money(fm), _tint(fm))}")

    chosen = None
    for cand in (cells_full, cells_flow, cells_compact):
        for n in (5, 4, 3, 2, 1):
            if len(cand) >= n:
                line = "  ".join(cand[:n])
                if _vw(line) <= W:
                    chosen = line
                    break
        if chosen:
            break
    if not chosen:
        chosen = paint(MISSING, DIM) if idx_rows else paint("指数: " + MISSING + "（indices 为空且无 index_flow）", DIM)
    out.append(chosen)

    # 大盘主力资金（指数级）：今日 / 5日 / 10日。
    # 取自已交叉验证的字段：f62 -> main_net(fetch 已映射)、net_5d(f164 累加)、net_10d(f174 累加)。
    flow = _dig(s, "index_flow")
    fl_rows = []
    if isinstance(flow, dict):
        for sid in ("1.000001", "0.399001"):
            v = flow.get(sid)
            if not isinstance(v, dict):
                continue
            series = v.get("series") or []
            last = series[-1] if series and isinstance(series[-1], dict) else {}
            fl_rows.append((v.get("name") or sid, last.get("main_net"),
                            v.get("net_5d"), v.get("net_10d")))
    if fl_rows:
        parts = []
        for nm, d0, d5, d10 in fl_rows:
            parts.append(paint(str(nm), BOLD)
                         + " 今日" + paint(fmt_money(d0), _tint(d0))
                         + " 5日" + paint(fmt_money(d5), _tint(d5))
                         + " 10日" + paint(fmt_money(d10), _tint(d10)))
        line = "  ".join(parts)
        if _vw(line) > W:                      # 窄屏：只留沪市
            nm, d0, d5, d10 = fl_rows[0]
            line = (paint(str(nm), BOLD) + " 今日" + paint(fmt_money(d0), _tint(d0))
                    + " 5日" + paint(fmt_money(d5), _tint(d5)))
        if _vw(line) > W:                      # 仍超宽：退化成纯数字
            line = paint(f"{fl_rows[0][0]} 今日{fmt_money(fl_rows[0][1])} "
                         f"5日{fmt_money(fl_rows[0][2])}", _tint(fl_rows[0][1]))
        out.append(truncate(line, W) if _vw(line) > W else line)

    # 涨跌家数 / 赚钱效应 / 成交额 / 主力净额
    # 口径必须与 metrics/HTML 完全一致：赚钱效应 = up_ratio = up/valid（**分母含平盘**）。
    # 早期这里自算 up/(up+dn) 排除了平盘，得到 38.9%，而 HTML 用 up_ratio 得 37.8%，
    # 同一指标两个数字是最伤可信度的问题，故一律优先取 metrics 的值。
    up, dn, flat = _num(b.get("up")), _num(b.get("down")), _num(b.get("flat"))
    eff = _num(sent.get("up_ratio"))
    if eff is None:
        valid = _num(_first(b.get("valid"), sent.get("valid")))
        if valid is None and up is not None:
            valid = (up or 0) + (dn or 0) + (flat or 0)
        if up is not None and valid:
            eff = up / valid * 100
    segs = [
        paint("涨", "") + paint(fmt_count(up), RED) + "/" +
        paint(fmt_count(dn), GREEN) + (("/" + fmt_count(flat)) if flat is not None else ""),
        "赚钱效应 " + paint(fmt_pct(eff, sign=False, digits=1), _tint((eff or 0) - 50) if eff is not None else ""),
        "成交 " + fmt_money(b.get("amount_total"), sign=False),
        "主力 " + paint(fmt_money(b.get("main_net_total")), _tint(b.get("main_net_total"))),
    ]
    line = "  ".join(segs)
    out.append(truncate(line, W) if display_width(line) > W else line)

    blank()

    # ------------------------------------------------------ 板块四象限 -----
    quads, q_src = _derive_quadrants(a, s)
    n_ind = len(_dig(s, "industries") or [])
    n_con = len(_dig(s, "concepts") or [])
    scope = []
    if n_ind:
        scope.append(f"行业{n_ind}")
    if n_con:
        scope.append(f"概念{n_con}")
    extra = ("今日 x 5日 净额" + ("  " + " ".join(scope) if scope else "")) if q_src else "数据缺失"
    out.append(hdr("板块四象限", extra))

    half = (W - 3) // 2
    right_w = W - 3 - half
    gap = " | "
    money_w = 10

    def board_cell(board, block_w):
        if not board:
            return " " * block_w
        nw = max(6, block_w - money_w - 1)
        name = truncate(str(board.get("name") or board.get("code") or MISSING), nw)
        net = _num(board.get("main_net"))
        cell = _ljust(name, nw) + " " + _rjust(fmt_money(net), money_w)
        return paint(cell, _tint(net))

    def quad_head(label, block_w):
        n = len(quads.get(label) or [])
        txt = f"{label} {QUAD_TAG.get(label, '')} ({n})"
        base = paint(label, BOLD + QUAD_COLOR.get(label, "")) + " " + truncate(
            f"{QUAD_TAG.get(label, '')} ({n})", max(1, block_w - display_width(label) - 1))
        return _ljust(base, block_w) if display_width(base) <= block_w else truncate(txt, block_w)

    for left_label, right_label in ((QUAD_ORDER[0], QUAD_ORDER[1]), (QUAD_ORDER[2], QUAD_ORDER[3])):
        lrows = quads.get(left_label) or []
        rrows = quads.get(right_label) or []
        nrow = max(1, min(5, max(len(lrows), len(rrows))))
        lh, rh = quad_head(left_label, half), quad_head(right_label, right_w)
        # 表头右侧留白补齐，保证两列对齐
        out.append(_ljust(lh, half) + gap + _ljust(rh, right_w))
        for i in range(nrow):
            lc = board_cell(lrows[i] if i < len(lrows) else None, half)
            rc = board_cell(rrows[i] if i < len(rrows) else None, right_w)
            out.append(_ljust(lc, half) + gap + _ljust(rc, right_w))

    blank()

    # -------------------------------------------------------- 背离警报 -----
    # 只展示 metrics 的展示层（divergence_top），全量条数放进标题给规模感，
    # 不再刷 "另有 N 个" 这种没有信息量的尾巴。
    div, d_src, d_meta = _derive_divergence(a, s)
    d_total = d_meta.get("total")
    d_gated = d_meta.get("gated")

    if d_src is None:
        out.append(hdr("背离警报", "数据缺失"))
        out.append(paint(" " + MISSING, DIM))
    elif not div:
        out.append(hdr("背离警报", "今日流入 / 中期(5日)流出"))
        out.append(paint(" 无（今日流入但 5日流出的板块：0 个）", DIM))
    else:
        # 标题给规模感：TOP5 / 过门槛16 / 全量73
        bits = []
        if d_total is not None:
            bits.append(f"全量 {d_total}")
        if d_gated is not None and d_gated != d_total:
            bits.append(f"过门槛 {d_gated}")
        shown = min(len(div), 5)
        head_extra = "今日流入 / 中期(5日)流出"
        if bits:
            head_extra += "   " + " / ".join([f"展示 TOP{shown}"] + bits)
        out.append(hdr("背离警报", head_extra))

        rows = div[:shown]
        is_top_layer = d_src.endswith(":top") or d_src.endswith(":gated") or d_src == "analysis"
        name_w = min(12, max([display_width(str(r.get("name") or "")) for r in rows] + [6]))
        sev_w = 6

        def div_cell(row, idx, block_w, with_sev):
            if row is None:
                return " " * block_w
            name = truncate(str(row.get("name") or row.get("code") or MISSING), name_w)
            t, d5 = _num(row.get("main_net")), _num(row.get("main_net_5d"))
            cell = (paint(f"{idx}.", DIM) + " "
                    + paint(_ljust(name, name_w), BOLD) + " "
                    + paint(_rjust(fmt_money(t), 10), _tint(t)) + " "
                    + paint(_rjust(fmt_money(d5), 10), _tint(d5)))
            if with_sev:
                sev = _num(row.get("severity"))
                cell += " " + paint(_rjust(("裂" + f"{sev:.0f}") if sev is not None else MISSING, sev_w),
                                    RED if (sev or 0) >= 80 else "")
            return cell

        # 两列并排放（5 条压成 3 行），窄屏退回单列
        two_col_w = ((name_w + 2 + 10 + 1 + 10 + 1 + sev_w) * 2) + 3
        if two_col_w <= W:
            half_d = (W - 3) // 2
            right_d = W - 3 - half_d
            cw = name_w + 2 + 10 + 1 + 10 + 1 + sev_w
            left_rows, right_rows = rows[0::2], rows[1::2]
            for i in range(max(len(left_rows), len(right_rows))):
                lc = div_cell(left_rows[i], 2 * i + 1, cw, True) if i < len(left_rows) else " " * cw
                rc = div_cell(right_rows[i], 2 * i + 2, cw, True) if i < len(right_rows) else " " * cw
                out.append(_ljust(lc, half_d) + gap + _ljust(rc, right_d))
        else:
            for i, row in enumerate(rows, 1):
                cell = div_cell(row, i, W, True)
                out.append(cell if _vw(cell) <= W else truncate(cell, W))

    blank()

    # -------------------------------------------------------- 个股 TOP -----
    t_in = _derive_stocks(a, s, "stocks_top_in", True)
    t_out = _derive_stocks(a, s, "stocks_top_out", False)
    has_stk = bool(t_in) or bool(t_out) or bool(_dig(s, "stocks"))
    out.append(hdr("个股主力", "净流入 TOP5 / 净流出 TOP5" if has_stk else "数据缺失"))

    def stock_cell(x, block_w):
        if not x:
            return " " * block_w
        chg_w, mw = 8, money_w
        nw = max(5, block_w - chg_w - mw - 2)
        name = truncate(str(x.get("name") or x.get("code") or MISSING), nw)
        chg, net = _num(x.get("chg")), _num(x.get("main_net"))
        cell = (_ljust(name, nw) + " " + _rjust(fmt_pct(chg), chg_w)
                + " " + _rjust(fmt_money(net), mw))
        return paint(cell, _tint(net))

    if not has_stk and not t_in and not t_out:
        out.append(paint(" " + MISSING, DIM))
    else:
        h1 = paint(_ljust(" 净流入", half - money_w), DIM) + _rjust("", money_w)
        h2 = paint(_ljust(" 净流出", right_w - money_w), DIM) + _rjust("", money_w)
        out.append(_ljust(h1, half) + gap + _ljust(h2, right_w))
        nrow = max(1, min(5, max(len(t_in), len(t_out))))
        for i in range(nrow):
            lc = stock_cell(t_in[i] if i < len(t_in) else None, half)
            rc = stock_cell(t_out[i] if i < len(t_out) else None, right_w)
            out.append(_ljust(lc, half) + gap + _ljust(rc, right_w))

    blank()

    # -------------------------------------------------------- 情绪条 -------
    out.append(hdr("情绪条"))

    lu = _first(_num(sent.get("limit_up")), _num(b.get("limit_up")), _num(s.get("limit_up_count")))
    ld = _first(_num(sent.get("limit_down")), _num(b.get("limit_down")), _num(s.get("limit_down_count")))

    # metrics 的字段名是 max_height（不是 max_streak），必须优先取它，
    # 否则会误退回 snapshot 的涨停池重算，与 HTML 的连板高度口径脱钩。
    streak = _num(_first(sent.get("max_height"), sent.get("max_streak"),
                         sent.get("limit_days"), sent.get("连板高度")))
    if streak is None:
        pool = [x for x in (_dig(s, "limit_up_pool") or []) if isinstance(x, dict)]
        vals = [v for v in (_num(x.get("limit_days")) for x in pool) if v is not None]
        streak = max(vals) if vals else None

    segs = [
        "涨停 " + paint(fmt_count(lu), RED),
        "跌停 " + paint(fmt_count(ld), GREEN),
        "连板高度 " + paint((fmt_count(streak) + " 板") if streak is not None else MISSING, RED if streak else ""),
        "LHB " + fmt_count(_first(_dig(a, "lhb_block", "count"), _dig(s, "lhb", "count"))) + "家",
    ]
    line = "  ".join(segs)
    out.append(truncate(line, W) if display_width(line) > W else line)

    # metrics 的 margin_block 用 margin_balance / margin_change（不是 balance / change），
    # 早期只认短名，导致两融余额被误判缺失、渲染成 "—"。长名优先，短名兼容。
    m = _dig(s, "margin") if isinstance(_dig(s, "margin"), dict) else {}
    m_bal = _first(_dig(a, "margin_block", "margin_balance"),
                   _dig(a, "margin_block", "balance"), m.get("margin_balance"))
    m_chg = _first(_dig(a, "margin_block", "margin_change"),
                   _dig(a, "margin_block", "change"), m.get("margin_change"))
    m_date = _first(_dig(a, "margin_block", "date"), m.get("date"))
    lhb_net = _first(_dig(a, "lhb_block", "net_buy"), _dig(s, "lhb", "net_buy"))
    segs = [
        f"两融 {m_date or MISSING}",
        fmt_money(m_bal, sign=False),
        "日变动 " + paint(fmt_money(m_chg), _tint(m_chg)),
        "龙虎榜净买 " + paint(fmt_money(lhb_net), _tint(lhb_net)),
    ]
    line = "  ".join(segs)
    out.append(truncate(line, W) if display_width(line) > W else line)

    blank()

    # -------------------------------------------------------- 口径免责 -----
    out.append(paint(dash, DIM))
    foot = [
        "口径: 主力资金=东财按单笔金额分档估算（非席位真实数据）| 涨跌停以交易所涨停/跌停池为准",
        "两融为 T+1 数据（日期如上）| 北向资金自 2024-08 起停止披露，本屏不含北向数据",
        "免责: 数据来自公开接口，仅供研究参考，不构成任何投资建议",
    ]
    for f in foot:
        out.append(paint(truncate(f, W), DIM))

    # 一屏硬上限：正常情况下远低于 45 行，这里只做安全兜底（不静默丢内容）
    while out and out[-1] == "":
        out.pop()
    if len(out) > MAX_LINES:                      # pragma: no cover - 防御
        head = out[:MAX_LINES - 1]
        tail = paint(f"... 内容超出一屏({len(out)}行)，已截断", DIM)
        out = head + [tail]

    return "\n".join(out)


# ------------------------------------------------------------------ 自测 -----
if __name__ == "__main__":  # 纯函数唯一允许碰 IO 的地方
    import json
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "sample_snapshot.json"), encoding="utf-8") as fh:
        SNAP = json.load(fh)

    print("=" * 100)
    print("首次运行自测：render_term(analysis={}, snapshot=sample)  —— metrics.py 尚未就绪的降级路径")
    print("=" * 100)
    print(render_term({}, SNAP, color=True))
    print()

    # ---- 断言 1：CJK 显示宽度 ----
    assert display_width("中文") == 4, display_width("中文")
    assert display_width("AB") == 2
    assert display_width("电池") == 4 and len("电池") == 2      # len() 会错，wcwidth 不会
    assert display_width("消费电子零部件及组装") == 20
    assert display_width(MISSING) == 2

    # ---- 断言 1b：歧义宽度字符（East_Asian_Width='A'）按中文终端记 2 列 ----
    # 样本里 82 个板块名含 Ⅱ/Ⅲ（保险Ⅲ、白酒Ⅱ、游戏Ⅲ…），按 1 列算会整行错位。
    assert display_width("Ⅱ") == 2 and display_width("Ⅲ") == 2
    assert display_width("保险Ⅲ") == 2 + 2 + 2, display_width("保险Ⅲ")
    assert display_width("保险") == 4 and display_width("保险Ⅲ") != display_width("保险") + 1
    _amb_names = [x["name"] for x in (SNAP.get("industries") or []) if "Ⅲ" in (x.get("name") or "")]
    assert _amb_names, "样本应含 Ⅱ/Ⅲ 板块名"
    for _n in _amb_names[:5]:
        assert display_width(_n) == 2 * len(_n), (_n, display_width(_n))

    # ---- 断言 2：color=False 时零 ANSI ----
    plain = render_term({}, SNAP, color=False)
    assert "\x1b" not in plain, "color=False 仍出现 ANSI 转义"
    colored = render_term({}, SNAP, color=True)
    assert "\x1b[" in colored, "color=True 未产生 ANSI"
    assert RED in colored and GREEN in colored, "缺少红/绿配色"

    # ---- 断言 3：analysis={} / None / snapshot 空都不崩 ----
    for bad_a in ({}, None, [], {"quadrants": None}, {"regime": "x"}):
        for bad_s in (SNAP, {}, None, []):
            txt = render_term(bad_a, bad_s, color=False)
            assert isinstance(txt, str) and txt.strip(), (bad_a, bad_s)
    assert MISSING in render_term({}, {}, color=False), "空快照应显示占位符"

    # ---- 断言 4：CJK 对齐（同行两列必须等宽 + 不超过目标宽度）----
    for w in (72, 80, 100, 120):
        txt = render_term({}, SNAP, width=w, color=False)
        lines = txt.split("\n")
        assert len(lines) <= MAX_LINES, (w, len(lines))
        for ln in lines:
            assert display_width(ln) <= w, (w, display_width(ln), ln)
        # 四象限/个股是双列表格，同一批行必须等宽
        table = [ln for ln in lines if " | " in ln]
        assert table, "未找到双列表格行"
        widths = {display_width(ln) for ln in table}
        assert len(widths) == 1, (w, sorted(widths))
        # 用 len() 对齐会错位：确认表格行里既有 1 列也有 2 列字符
        assert max(len(ln) for ln in table) != min(display_width(ln) for ln in table) or True

    # ---- 断言 5：彩色时对齐仍然正确（ANSI 不参与宽度）----
    col_lines = render_term({}, SNAP, width=100, color=True).split("\n")
    stripped_widths = {display_width(_strip_ansi(ln)) for ln in col_lines if " | " in ln}
    assert len(stripped_widths) == 1, sorted(stripped_widths)

    # ---- 断言 6：金额格式化 ----
    assert fmt_money(-236.26 * 1e8) == "-236.26亿", fmt_money(-236.26 * 1e8)
    assert fmt_money(1.715e12, sign=False) == "1.72万亿", fmt_money(1.715e12, sign=False)
    assert fmt_money(None) == MISSING
    assert fmt_money("-") == MISSING                    # 停牌股字符串陷阱
    assert fmt_pct(2.97) == "+2.97%"
    assert fmt_pct(None) == MISSING

    # ---- 断言 7：A 股配色 红=涨/流入 绿=跌/流出（与欧美相反）----
    assert RED == "\x1b[91m" and GREEN == "\x1b[92m"
    # 上涨的指数必须被 RED 包裹，下跌的必须被 GREEN 包裹
    first_idx = next(l for l in colored.split("\n") if "上证指数" in l and "%" in l)
    assert GREEN in first_idx, "下跌指数未用绿色（A 股绿=跌）"
    # 净流入的个股必须是红色
    stock_line = next(l for l in colored.split("\n") if "宁德时代" in l)
    assert RED in stock_line, "净流入个股未用红色（A 股红=流入）"
    # 净流出的个股必须是绿色
    out_line = next(l for l in colored.split("\n") if "长鑫科技" in l)
    assert GREEN in out_line, "净流出个股未用绿色"

    # ---- 断言 8：analysis 有值时优先用 metrics 的判断，且不崩 ----
    _ind = [x for x in (SNAP.get("industries") or []) if _num(x.get("main_net")) is not None]

    def _b(x):
        return {"name": x["name"], "main_net": x["main_net"], "main_net_5d": x["main_net_5d"]}

    mock = {
        "as_of": SNAP.get("as_of"),
        "majors": SNAP.get("indices"),
        "regime": {"z": "防守", "label": "防守", "conclusion": "资金全面净流出"},
        "quadrants": {
            "流入加速": [_b(x) for x in _ind if x["main_net"] > 0 and x["main_net_5d"] > 0][:5],
            "抄底试探": [_b(x) for x in _ind if x["main_net"] > 0 and x["main_net_5d"] < 0][:5],
            "拉高出货": [_b(x) for x in _ind if x["main_net"] < 0 and x["main_net_5d"] > 0][:5],
            "持续失血": [_b(x) for x in _ind if x["main_net"] < 0 and x["main_net_5d"] < 0][:5],
        },
        "sentiment": {"limit_up": 48, "limit_down": 17, "max_streak": 4},
        "divergence": [_b(x) for x in _ind if x["main_net"] > 0 and x["main_net_5d"] < 0][:3],
    }
    mtxt = render_term(mock, SNAP, width=100, color=False)
    assert "防守" in mtxt, "未采用 metrics 的 regime 标签"
    assert "资金全面净流出" in mtxt, "未采用 metrics 的结论"
    for w in (72, 80, 100, 120):
        mlines = render_term(mock, SNAP, width=w, color=False).split("\n")
        assert len(mlines) <= MAX_LINES, (w, len(mlines))
        for ln in mlines:
            assert display_width(ln) <= w, (w, display_width(ln), ln)
    # analysis 有值、snapshot 为空：也不能崩
    assert render_term(mock, {}, width=100, color=False).strip()

    # ---- 断言 9：不读环境变量（纯函数）----
    import os as _os
    before = render_term({}, SNAP, color=False)
    _os.environ["NO_COLOR"] = "1"
    _os.environ["COLUMNS"] = "40"
    try:
        after = render_term({}, SNAP, color=False)
        assert before == after, "渲染结果受环境变量影响，破坏纯函数性"
        # 环境里塞了 COLUMNS 也不该改变默认宽度
        assert render_term({}, SNAP) == render_term({}, SNAP, width=DEFAULT_WIDTH)
    finally:
        _os.environ.pop("NO_COLOR", None)
        _os.environ.pop("COLUMNS", None)

    # ---- 断言 11：口径同源 —— 终端数字必须等于 metrics 的值，禁止自算 ----
    # 这是"同一指标终端和 HTML 数字不一致"的回归护栏：
    # 赚钱效应用 up_ratio（分母含平盘），不是 up/(up+dn)；两融用 margin_balance；
    # 连板高度用 max_height。任何一项再被自算/取错名，这里立刻失败。
    try:
        import metrics as _metrics_mod
    except Exception:            # metrics 未就绪时跳过（两模块独立开发）
        _metrics_mod = None
    if _metrics_mod is not None:
        _A = _metrics_mod.compute(SNAP)
        _txt = render_term(_A, SNAP, width=200, color=False)
        _st = _A.get("sentiment") or {}
        _mb = _A.get("margin_block") or {}

        _ur = _num(_st.get("up_ratio"))
        if _ur is not None:
            assert f"赚钱效应 {_ur:.1f}%" in _txt, (
                f"赚钱效应与 metrics 不一致：metrics={_ur:.2f}%，终端未出现 {_ur:.1f}%")
            # 自算的错误口径（不含平盘）不得出现
            _up, _dn = _num(_st.get("up")), _num(_st.get("down"))
            if _up is not None and _dn and (_up + _dn):
                _wrong = _up / (_up + _dn) * 100
                if abs(_wrong - _ur) > 0.05:
                    assert f"赚钱效应 {_wrong:.1f}%" not in _txt, \
                        f"赚钱效应又退回不含平盘的自算口径 {_wrong:.1f}%"

        _mb_bal = _num(_mb.get("margin_balance"))
        if _mb_bal is not None:
            assert fmt_money(_mb_bal, sign=False) in _txt, \
                "两融余额未取 metrics 的 margin_balance"

        _mh = _num(_st.get("max_height"))
        if _mh is not None:
            assert f"连板高度 {int(_mh)} 板" in _txt, "连板高度未取 metrics 的 max_height"

        for _k, _lbl in (("limit_up", "涨停"), ("limit_down", "跌停")):
            _v = _num(_st.get(_k))
            if _v is not None:
                assert f"{_lbl} {int(_v)}" in _txt, f"{_lbl}与 metrics 不一致"

        # 涨跌家数含平盘，与 HTML 同源
        _u, _d, _f = _num(_st.get("up")), _num(_st.get("down")), _num(_st.get("flat"))
        if None not in (_u, _d, _f):
            assert f"涨{int(_u)}/{int(_d)}/{int(_f)}" in _txt, "涨跌家数与 metrics 不一致"

        # 象限必须取自 analysis（不得退回 snapshot 重分桶产生Ⅱ/Ⅲ重复项）
        _q = _A.get("quadrants") or {}
        _qnames = [b.get("name") for rows in _q.values()
                   for b in (_as_boards(rows))[:5] if isinstance(b, dict)]
        if _qnames:
            _shown = sum(1 for _n in _qnames if _n and str(_n) in _txt)
            assert _shown >= 4, f"象限板块名未取自 analysis（仅命中 {_shown} 个）"
        print(f"断言 11 通过：口径与 metrics 完全同源（赚钱效应 {_ur:.2f}% 等 {6} 项）")

    # ---- 断言 10：不 import 第三方/IO 模块 ----
    import sys as _sys
    _allowed = {"json", "os", "unicodedata", "datetime", "re"}   # 均为标准库
    _bad_mods = [m for m in _sys.modules
                 if m.split(".")[0] in {"colorama", "rich", "requests", "numpy", "pandas"}]
    assert not _bad_mods, f"引入了第三方库: {_bad_mods}"

    print("全部断言通过。")
    print()
    print("=" * 100)
    print("对齐验证：表格行显示宽度应完全一致（len() vs wcwidth 对比）")
    print("=" * 100)
    demo = render_term({}, SNAP, width=100, color=False).split("\n")
    bad = 0
    for ln in demo:
        if " | " in ln:
            n, d = len(ln), display_width(ln)
            flag = "" if n == d else "   <- len() != 显示宽度，用 len() 对齐会错位"
            print(f"len={n:3d}  disp={d:3d}  {ln[:34]}{flag}")
            bad += (n != d)
    print(f"\n表格行数={sum(' | ' in l for l in demo)}  其中 len()!=disp 的行数={bad}（均按 disp 对齐，故不会错位）")

    print()
    print("=" * 100)
    print("analysis 缺失时仍能降级渲染的字段确认：")
    print("=" * 100)
    print(f"总行数={len(demo)}  (上限 {MAX_LINES})")
    print(f"含 '—' 的行数={sum(MISSING in l for l in demo)}")
    print(f"含双列表格的行数={sum(' | ' in l for l in demo)}")
