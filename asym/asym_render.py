"""信息不对称面板 · 渲染层。

纯函数：render_html(analysis, snapshot) -> str，无 IO、无网络、无第三方依赖。
防御性设计：analysis 为空时仍渲染降级但完整的页面。
"""
from __future__ import annotations

from html import escape

DASH = "—"

# A 股配色：红=上涨/增持/流入，绿=下跌/减持/流出（与欧美相反）
RED = "#f6465d"
GREEN = "#0ecb81"
AMBER = "#f0a020"
BLUE = "#3b82f6"
PURPLE = "#a855f7"

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080b11;color:#c9d3e0;font:14px/1.62 -apple-system,BlinkMacSystemFont,
 "PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
 -webkit-font-smoothing:antialiased;padding:20px 16px 40px}
.wrap{max-width:1180px;margin:0 auto}
.hero{background:linear-gradient(135deg,rgba(168,85,247,.10),rgba(59,130,246,.06));
 border:1px solid #1e2635;border-radius:14px;padding:20px 24px;margin-bottom:18px}
.hero h1{font-size:21px;font-weight:700;color:#e8eef7;letter-spacing:.4px}
.hero .stance{margin-top:10px;font-size:15px;color:#b9c4d4;line-height:1.7}
.hero .stance b{color:#f0a020;font-weight:700}
.meta{margin-top:14px;display:flex;flex-wrap:wrap;gap:8px}
.tag{font-size:11.5px;color:#8b96a8;background:rgba(255,255,255,.03);
 border:1px solid #1b2331;border-radius:7px;padding:3px 9px}
.panel{background:#0d1119;border:1px solid #1b2331;border-radius:13px;
 padding:18px 20px;margin-bottom:16px}
.phd{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:4px}
.phd h2{font-size:16px;font-weight:700;color:#e8eef7}
.phd h2::before{content:"";display:inline-block;width:3px;height:14px;
 background:linear-gradient(#a855f7,#3b82f6);border-radius:2px;
 margin-right:9px;vertical-align:-2px}
.psub{font-size:11.5px;color:#7c8799}
.pnote{font-size:12px;color:#7c8799;line-height:1.7;margin-top:10px;
 padding-top:10px;border-top:1px dashed #1b2331}
.pnote b{color:#f0a020}
.grid{display:grid;gap:10px;margin-top:12px}
.g2{grid-template-columns:repeat(2,1fr)}
.g3{grid-template-columns:repeat(3,1fr)}
.g4{grid-template-columns:repeat(4,1fr)}
@media(max-width:820px){.g2,.g3,.g4{grid-template-columns:1fr}}
.tile{background:rgba(255,255,255,.024);border:1px solid #1b2331;
 border-radius:10px;padding:11px 13px;min-width:0}
.tl{font-size:11.5px;color:#8b96a8;margin-bottom:5px}
.tv{font-size:19px;font-weight:700;font-variant-numeric:tabular-nums;color:#e8eef7}
.tv.up{color:#f6465d}.tv.down{color:#0ecb81}.tv.warn{color:#f0a020}
.tn{font-size:11px;color:#6b7688;margin-top:4px}
.list{margin-top:12px}
.row{display:flex;align-items:center;gap:10px;padding:7px 0;
 border-bottom:1px solid rgba(255,255,255,.035)}
.row:last-child{border-bottom:none}
.rk{width:20px;flex:0 0 20px;font-size:11.5px;color:#5c6678;
 font-variant-numeric:tabular-nums;text-align:right}
.nm{flex:1 1 auto;min-width:0;font-size:13.5px;color:#dfe6f0;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub{font-size:11.5px;color:#76819a;margin-left:6px}
.val{flex:0 0 auto;font-size:13.5px;font-weight:650;
 font-variant-numeric:tabular-nums;text-align:right}
.val.up{color:#f6465d}.val.down{color:#0ecb81}.val.dim{color:#76819a}
.bar{height:5px;border-radius:3px;background:rgba(255,255,255,.06);
 overflow:hidden;margin-top:5px}
.bar>i{display:block;height:100%;border-radius:3px}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;
 padding:5px 11px;border-radius:8px;background:rgba(255,255,255,.03);
 border:1px solid #1b2331;margin:4px 6px 0 0}
.chip .k{color:#8b96a8;font-size:11.5px}
.chip .v{font-weight:700;font-variant-numeric:tabular-nums}
.warnbox{margin-top:12px;border-left:3px solid #f0a020;background:rgba(240,160,32,.07);
 border-radius:0 9px 9px 0;padding:11px 14px;font-size:12.5px;color:#d9c9a8;line-height:1.75}
.warnbox b{color:#f0a020}
.miss{margin-top:10px;border-left:3px solid #6b7688;background:rgba(255,255,255,.022);
 border-radius:0 9px 9px 0;padding:10px 13px;font-size:12px;color:#8792a4;line-height:1.7}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:12.5px}
th{text-align:left;font-weight:600;color:#8b96a8;font-size:11.5px;
 padding:7px 9px;border-bottom:1px solid #1b2331;white-space:nowrap}
td{padding:7px 9px;border-bottom:1px solid rgba(255,255,255,.03);color:#c9d3e0}
tr:last-child td{border-bottom:none}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.up{color:#f6465d}td.down{color:#0ecb81}td.dim{color:#76819a}
.ucenter{text-align:center;padding:20px 0;color:#6b7688;font-size:12.5px}
.foot{margin-top:20px;padding:16px 20px;background:#0a0e15;border:1px solid #1b2331;
 border-radius:12px;font-size:11.5px;color:#6b7688;line-height:1.9}
.foot b{color:#8792a4}
.scroll{overflow-x:auto}
"""


def _d(obj, *path):
    """安全逐层取值。"""
    cur = obj
    for k in path:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[k]
            except (IndexError, TypeError):
                return None
        else:
            return None
    return cur


def _rows(v):
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    if isinstance(v, dict):
        for k in ("rows", "boards", "items", "list", "top", "top_in", "top_out"):
            sub = v.get(k)
            if isinstance(sub, list):
                return [x for x in sub if isinstance(x, dict)]
        for sub in v.values():
            if isinstance(sub, list) and sub and isinstance(sub[0], dict):
                return [x for x in sub if isinstance(x, dict)]
    return []


def _n(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def esc(v):
    return DASH if v is None else escape(str(v), quote=True)


def fmt_int(v):
    n = _n(v)
    if n is None:
        return DASH
    return f"{int(n):,}"


def fmt_yi(v, signed=True):
    n = _n(v)
    if n is None:
        return DASH
    s = "+" if signed else ""
    if abs(n) >= 1e12:
        return f"{n/1e12:{s}.2f}万亿"
    if abs(n) >= 1e8:
        return f"{n/1e8:{s}.2f}亿"
    return f"{n/1e4:{s}.0f}万"


def fmt_pct(v, digits=1, signed=True):
    n = _n(v)
    if n is None:
        return DASH
    s = "+" if signed else ""
    return f"{n:{s}.{digits}f}%"


def _cls(n, invert=False):
    """A股配色：正=红、负=绿。invert 用于"户数下降"这类语义反转的场景。"""
    n = _n(n)
    if n is None:
        return "dim"
    if invert:
        return "down" if n > 0 else ("up" if n < 0 else "dim")
    return "up" if n > 0 else ("down" if n < 0 else "dim")


def _panel(pid, title, sub, body, extra=""):
    return (
        f'<section class="panel" id="{pid}">'
        f'<div class="phd"><h2>{esc(title)}</h2>'
        f'{f"<span class=psub>{esc(sub)}</span>" if sub else ""}</div>'
        f"{body}{extra}</section>"
    )


# ── 顶部 ───────────────────────────────────────────────────────────────

def _hero(a, s):
    stance = _d(a, "stance") or "这里展示的不是内幕，是知情者留下的公开痕迹。"
    tags = [f"交易日 {esc(_d(a,'as_of'))}",
            f"生成 {esc(_d(a,'generated_at'))}"]
    nd = _d(a, "surveys", "notice_days_covered")
    if nd:
        tags.append(f"调研窗口 {nd} 个公告日")
    lag = _d(a, "holders", "lag_days")
    if lag is not None:
        tags.append(f"股东户数滞后 {lag} 天")

    return (
        '<div class="hero">'
        '<h1>信息不对称面板 · 知情者行为痕迹</h1>'
        f'<div class="stance"><b>{esc(stance)}</b></div>'
        f'<div class="meta">{"".join(f"<span class=tag>{t}</span>" for t in tags)}</div>'
        '</div>'
    )


# ── P1 调研热度 ─────────────────────────────────────────────────────────

def _p1(a):
    sv = _d(a, "surveys") or {}
    rows = _rows(sv.get("boards") or sv.get("top"))
    counts = sv.get("counts") or {}

    tiles = (
        '<div class="grid g4">'
        f'<div class="tile"><div class="tl">单场调研</div>'
        f'<div class="tv">{fmt_int(counts.get("single"))}</div>'
        f'<div class="tn">场（可入榜）</div></div>'
        f'<div class="tile"><div class="tl">月度汇总</div>'
        f'<div class="tv warn">{fmt_int(counts.get("monthly"))}</div>'
        f'<div class="tn">单列，不可相加</div></div>'
        f'<div class="tile"><div class="tl">原始记录</div>'
        f'<div class="tv">{fmt_int(counts.get("raw_rows"))}</div>'
        f'<div class="tn">行（机构×场次展开）</div></div>'
        f'<div class="tile"><div class="tl">公告日窗口</div>'
        f'<div class="tv warn">{fmt_int(sv.get("notice_days_covered"))}</div>'
        f'<div class="tn">个公告日</div></div>'
        '</div>'
    )

    if rows:
        mx = max((_n(r.get("inst_count")) or 0) for r in rows) or 1
        items = []
        for i, r in enumerate(rows[:20], 1):
            ic = _n(r.get("inst_count")) or 0
            w = ic / mx * 100
            items.append(
                '<div class="row">'
                f'<div class="rk">{i}</div>'
                f'<div class="nm">{esc(r.get("name"))}'
                f'<span class="sub">{esc(r.get("way"))}</span></div>'
                f'<div class="val up">{fmt_int(ic)} 家</div>'
                '</div>'
                f'<div class="bar"><i style="width:{w:.1f}%;'
                f'background:linear-gradient(90deg,{RED},{AMBER})"></i></div>'
            )
        body = tiles + '<div class="list">' + "".join(items) + "</div>"
    else:
        body = tiles + '<div class="ucenter">调研数据不可用</div>'

    # 月度窗口单列，并明确不可相加
    monthly = _rows(sv.get("monthly"))
    if monthly:
        mrows = "".join(
            '<div class="row"><div class="rk">·</div>'
            f'<div class="nm">{esc(r.get("name"))}'
            f'<span class="sub">{esc(r.get("receive_start"))} ~ {esc(r.get("receive_end"))}</span></div>'
            f'<div class="val dim">{fmt_int(r.get("inst_count"))} 家</div></div>'
            for r in monthly[:6]
        )
        body += (
            '<div class="warnbox"><b>月度窗口（整月累计接待，不可与上方单场相加）</b>'
            f'<div class="list">{mrows}</div></div>'
        )

    note = _d(a, "surveys", "note") or ""
    extra = f'<div class="pnote"><b>口径</b>：{esc(note)}</div>'
    return _panel("p1", "调研热度", "谁正在被密集拜访", body, extra)


# ── P2 内部人投票 ───────────────────────────────────────────────────────

def _p2(a):
    s2 = _d(a, "insider") or {}
    sm = s2.get("summary") or {}
    inc, dec = _rows(s2.get("top_in")), _rows(s2.get("top_out"))
    net = _n(sm.get("net_amount"))

    tiles = (
        '<div class="grid g4">'
        f'<div class="tile"><div class="tl">增持人次</div>'
        f'<div class="tv up">{fmt_int(sm.get("inc_count"))}</div>'
        f'<div class="tn">合计 {esc(fmt_yi(sm.get("inc_amount")))}</div></div>'
        f'<div class="tile"><div class="tl">减持人次</div>'
        f'<div class="tv down">{fmt_int(sm.get("dec_count"))}</div>'
        f'<div class="tn">合计 {esc(fmt_yi(sm.get("dec_amount")))}</div></div>'
        f'<div class="tile"><div class="tl">净额</div>'
        f'<div class="tv {_cls(net)}">{esc(fmt_yi(net))}</div>'
        f'<div class="tn">{esc(sm.get("net_sign"))}</div></div>'
        f'<div class="tile"><div class="tl">增持/减持</div>'
        f'<div class="tv">{fmt_int(sm.get("inc_count"))}/{fmt_int(sm.get("dec_count"))}</div>'
        f'<div class="tn">人次比</div></div>'
        '</div>'
    )

    def table(src, title, cls):
        if not src:
            return f'<div class="ucenter">{title}不可用</div>'
        rows = "".join(
            '<tr>'
            f'<td>{esc(r.get("name"))}<span class="sub"> {esc(r.get("person"))}</span></td>'
            f'<td class="dim">{esc(r.get("date"))}</td>'
            f'<td class="dim">{esc(r.get("reason"))}</td>'
            f'<td class="num {cls}">{esc(fmt_yi(r.get("amount")))}</td>'
            '</tr>' for r in src[:10]
        )
        return (
            '<div class="scroll"><table>'
            f'<tr><th>{esc(title)}</th><th>日期</th><th>渠道</th><th>金额</th></tr>'
            f'{rows}</table></div>'
        )

    body = (tiles
            + '<div class="grid g2">'
            + table(inc, "增持 TOP", "up")
            + table(dec, "减持 TOP", "down")
            + '</div>')

    # 渠道分布
    br = s2.get("by_reason") or {}
    if br:
        total = sum(v.get("count") or 0 for v in br.values()) or 1
        chips = "".join(
            f'<span class="chip"><span class="k">{esc(k)}</span>'
            f'<span class="v">{fmt_int(v.get("count"))} 笔</span>'
            f'<span class="k">{(v.get("count") or 0)/total*100:.0f}%</span>'
            f'<span class="v {_cls(v.get("amount"))}">{esc(fmt_yi(v.get("amount")))}</span></span>'
            for k, v in sorted(br.items(), key=lambda kv: -(kv[1].get("count") or 0))
        )
        body += f'<div style="margin-top:12px">{chips}</div>'

    note = s2.get("note") or ""
    extra = f'<div class="pnote"><b>口径</b>：{esc(note)}</div>'
    return _panel("p2", "内部人投票", "谁在用自己的钱表态", body, extra)


def _svg_concentrate(dn, upc, total=None, w=520, h=120):
    """筹码流向双向条：左=户数下降(集中)，右=户数上升(分散)。

    这张图是这个面板的核心视觉——一眼看出筹码在往哪边动。
    """
    dn = _n(dn) or 0
    upc = _n(upc) or 0
    # 分母必须与指标卡一致（total=全部样本），否则同一指标会出现两个数字：
    # 曾用 dn/(dn+up) 得 64.2%，而指标卡用 dn/total 得 62.8%，
    # 同一个"下降占比"两个值，比数字不准更伤可信度。
    tot = _n(total) or (dn + upc)
    if tot <= 0:
        return ""
    cx = w // 2
    bar_h = 34
    dn_w = (dn / tot) * (cx - 20)
    up_w = (upc / tot) * (cx - 20)
    y = 40
    dn_pct = dn / tot * 100
    up_pct = upc / tot * 100
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" '
        f'style="max-width:{w}px;display:block;margin:6px auto 0">'
        # 左半：集中
        f'<rect x="{cx - 20 - dn_w:.1f}" y="{y}" width="{dn_w:.1f}" height="{bar_h}" '
        f'rx="5" fill="{RED}" opacity="0.85"/>'
        # 右半：分散
        f'<rect x="{cx + 20}" y="{y}" width="{up_w:.1f}" height="{bar_h}" '
        f'rx="5" fill="{GREEN}" opacity="0.85"/>'
        # 中轴线
        f'<line x1="{cx}" y1="{y - 10}" x2="{cx}" y2="{y + bar_h + 10}" '
        f'stroke="#2a3346" stroke-width="1.5" stroke-dasharray="3 3"/>'
        # 标签
        f'<text x="{cx - 26}" y="{y - 14}" text-anchor="end" fill="{RED}" '
        f'font-size="12.5" font-weight="700">{dn:,.0f} 家 · {dn_pct:.1f}%</text>'
        f'<text x="{cx + 26}" y="{y - 14}" text-anchor="start" fill="{GREEN}" '
        f'font-size="12.5" font-weight="700">{upc:,.0f} 家 · {up_pct:.1f}%</text>'
        f'<text x="{cx - 26}" y="{y + bar_h + 22}" text-anchor="end" '
        f'fill="#8b96a8" font-size="11.5">← 户数下降（筹码集中）</text>'
        f'<text x="{cx + 26}" y="{y + bar_h + 22}" text-anchor="start" '
        f'fill="#8b96a8" font-size="11.5">户数上升（筹码分散）→</text>'
        '</svg>'
    )


# ── P3 筹码集中度 ───────────────────────────────────────────────────────

def _p3(a):
    s3 = _d(a, "holders") or {}
    sm = s3.get("summary") or {}
    conc = _rows(s3.get("top_concentrate"))
    lag = _d(a, "holders", "lag_days")

    tiles = (
        '<div class="grid g4">'
        f'<div class="tile"><div class="tl">户数下降</div>'
        f'<div class="tv up">{fmt_int(sm.get("down_count"))}</div>'
        f'<div class="tn">筹码趋向集中</div></div>'
        f'<div class="tile"><div class="tl">户数上升</div>'
        f'<div class="tv down">{fmt_int(sm.get("up_count"))}</div>'
        f'<div class="tn">筹码趋向分散</div></div>'
        f'<div class="tile"><div class="tl">下降占比</div>'
        f'<div class="tv">{esc(fmt_pct(sm.get("down_pct"), 1, False))}</div>'
        f'<div class="tn">占全部样本</div></div>'
        f'<div class="tile"><div class="tl">数据滞后</div>'
        f'<div class="tv warn">{fmt_int(lag)} 天</div>'
        f'<div class="tn">定期报告披露</div></div>'
        '</div>'
    )

    # 集中/分散对比：核心 SVG 图
    dn, upc = _n(sm.get("down_count")) or 0, _n(sm.get("up_count")) or 0
    body = tiles + _svg_concentrate(dn, upc, sm.get("total"))

    if conc:
        rows = "".join(
            '<tr>'
            f'<td>{esc(r.get("name"))}</td>'
            f'<td class="dim">{esc(r.get("end_date"))}</td>'
            f'<td class="num">{fmt_int(r.get("pre_holder_num"))}</td>'
            f'<td class="num">{fmt_int(r.get("holder_num"))}</td>'
            f'<td class="num {"up" if (_n(r.get("ratio")) or 0) < 0 else "down"}">'
            f'{esc(fmt_pct(r.get("ratio")))}</td>'
            '</tr>' for r in conc[:12]
        )
        body += (
            '<div class="scroll"><table>'
            '<tr><th>户数降幅最大（筹码集中）</th><th>披露日</th>'
            '<th>上期户数</th><th>本期户数</th><th>环比</th></tr>'
            f'{rows}</table></div>'
        )
    else:
        body += '<div class="ucenter">股东户数明细不可用</div>'

    # 极端值提示：不静默丢弃
    clipped = _n(sm.get("clipped_count"))
    warn = ""
    if clipped:
        warn = (f'<div class="warnbox"><b>极端值未静默丢弃</b>：{fmt_int(clipped)} 条样本的'
                f'户数环比极端（新股上市等原因），已在统计中做 1%/99% 截断，'
                f'但原始值仍保留在快照中供核查。截断只压边界，不删除样本点。</div>')
    note = s3.get("note") or ""
    lag_note = s3.get("lag_note") or ""
    extra = (warn + f'<div class="pnote"><b>口径</b>：{esc(note)}'
             f'<br><b>时效</b>：{esc(lag_note)}</div>')
    return _panel("p3", "筹码集中度", "筹码在向少数人还是多数人集中", body, extra)


# ── P4 席位 ─────────────────────────────────────────────────────────────

def _p4(a):
    s4 = _d(a, "seats") or {}
    rows = _rows(s4.get("top_active") or s4.get("boards"))
    span = s4.get("date_span") or {}
    pos = s4.get("positioning") or ""

    hdr = (
        '<div class="warnbox"><b>这是历史累计活跃度，不是当日游资动向</b><br>'
        f'数据跨度 {esc(span.get("min"))} ~ {esc(span.get("max"))}，'
        f'按席位代码去重（原始 {fmt_int(s4.get("raw_rows"))} 行）。'
        '仅用于识别长期活跃席位，不能据此判断今天谁在买。</div>'
    )

    if rows:
        mx = max((_n(r.get("buyer_num")) or 0) for r in rows) or 1
        items = []
        for i, r in enumerate(rows[:15], 1):
            bn = _n(r.get("buyer_num")) or 0
            net = _n(r.get("net_amt"))
            items.append(
                '<div class="row">'
                f'<div class="rk">{i}</div>'
                f'<div class="nm">{esc(r.get("name"))}</div>'
                f'<div class="val dim">{fmt_int(bn)} 次</div>'
                f'<div class="val {_cls(net)}" style="flex:0 0 92px">'
                f'{esc(fmt_yi(net))}</div></div>'
                f'<div class="bar"><i style="width:{bn/mx*100:.1f}%;background:{PURPLE}"></i></div>'
            )
        body = hdr + '<div class="list">' + "".join(items) + "</div>"
    else:
        body = hdr + '<div class="ucenter">席位数据不可用</div>'

    # 历史事件异常席位单列：避免被误读为今日常态
    anom = _rows(s4.get("anomalies"))
    if anom:
        arows = "".join(
            '<div class="row"><div class="rk">!</div>'
            f'<div class="nm">{esc(r.get("name"))}'
            f'<span class="sub">{esc(r.get("date"))}</span></div>'
            f'<div class="val {_cls(r.get("net_amt"))}">{esc(fmt_yi(r.get("net_amt")))}</div>'
            '</div>' for r in anom[:6]
        )
        body += (
            '<div class="warnbox"><b>量级异常席位（特定历史事件痕迹，非近期常态）</b><br>'
            '下列席位净额远超同业中位数量级，且记录日期集中在特定历史时点'
            '（如 2015 年市场干预期间），<b>不代表这些席位现在仍如此活跃</b>，'
            '更不可据此判断当下资金动向。'
            f'<div class="list">{arows}</div></div>'
        )

    note = s4.get("note") or ""
    extra = f'<div class="pnote"><b>口径</b>：{esc(note)}</div>'
    return _panel("p4", "席位活跃度", "长期活跃的营业部（历史累计口径）", body,
                  extra)


# ── 业绩预告 ───────────────────────────────────────────────────────────

def _p5(a):
    fc = _d(a, "forecast") or {}
    rows = _rows(fc.get("boards") or fc.get("rows"))
    if not rows:
        return _panel("p5", "业绩预告", "管理层提前给出的方向",
                      '<div class="ucenter">数据不可用</div>')
    # ⚠️ predict 字段值很长（如"扣除非经常性损益后的净利润"），
    # 用固定窄列会把文字压成多行、撑坏行高。改为表格布局给足宽度。
    trs = "".join(
        '<tr>'
        f'<td>{esc(r.get("name"))}<span class="sub">{esc(r.get("type"))}</span></td>'
        f'<td class="dim">{esc(r.get("notice_date"))}</td>'
        f'<td class="dim" style="white-space:normal">{esc(r.get("predict"))}</td>'
        '</tr>' for r in rows[:10]
    )
    body = ('<div class="scroll"><table>'
            '<tr><th>公司</th><th>公告日</th><th>预告口径</th></tr>'
            f'{trs}</table></div>')
    extra = f'<div class="pnote"><b>口径</b>：{esc(fc.get("note"))}</div>'
    return _panel("p5", "业绩预告", "管理层主动披露的前瞻方向", f'<div class="list">{body}</div>', extra)


# ── 数据可信度 ─────────────────────────────────────────────────────────

def _trust(a):
    tr = _d(a, "data_trust") or {}
    if not tr:
        return _panel("p6", "数据可信度", "每个数据集的口径、覆盖与局限",
                      '<div class="ucenter">不可用</div>')

    rows = []
    for k, v in tr.items():
        if k.startswith("_"):
            continue
        if not isinstance(v, dict):
            continue
        rows.append(
            '<tr>'
            f'<td><b>{esc(k)}</b></td>'
            f'<td class="num">{esc(v.get("rows"))}</td>'
            f'<td class="dim">{esc(v.get("覆盖"))}</td>'
            f'<td class="dim">{esc(v.get("滞后"))}</td>'
            f'<td class="dim">{esc(v.get("口径"))}</td>'
            '</tr>'
        )
    table = ('<div class="scroll"><table>'
             '<tr><th>数据集</th><th>行数</th><th>覆盖</th><th>滞后</th><th>口径</th></tr>'
             + "".join(rows) + '</table></div>')

    # 不可得数据集必须展示
    unm = tr.get("_不可得") or {}
    if unm:
        items = "".join(f'<div class="row"><div class="rk">—</div>'
                        f'<div class="nm">{esc(k)}</div>'
                        f'<div class="val dim" style="flex:0 0 auto;font-size:11.5px">'
                        f'{esc(v)}</div></div>'
                        for k, v in unm.items())
        table += (
            '<div class="warnbox"><b>明确不可得的数据集</b>（不展示、不估算、不冒充）'
            f'<div class="list">{items}</div>'
            '<div style="margin-top:8px;color:#8792a4;font-size:11.5px">'
            '只展示能拿到的部分，会让使用者以为信息完整——那等于制造新的信息不对称。'
            '所以这里如实列出缺失项。</div></div>'
        )
    return _panel("p6", "数据可信度", "口径 / 覆盖 / 滞后 / 不可得项", table)


# ── 判读 ───────────────────────────────────────────────────────────────

def _narr(a):
    ns = [str(x) for x in (a.get("narrative") or []) if x]
    lim = [str(x) for x in (a.get("limitations") or []) if x]
    if not ns:
        return _panel("p7", "规则化判读", "仅陈述痕迹形态", '<div class="ucenter">无判读</div>')
    body = "".join(f'<div class="row"><div class="rk">·</div>'
                   f'<div class="nm" style="white-space:normal;line-height:1.75">'
                   f'{esc(x)}</div></div>' for x in ns)
    extra = ""
    if lim:
        lrows = "".join(f'<div class="row"><div class="rk">!</div>'
                        f'<div class="nm" style="white-space:normal;color:#8792a4;'
                        f'line-height:1.7">{esc(x)}</div></div>' for x in lim)
        extra = f'<div class="warnbox"><b>如实标注的局限</b>{lrows}</div>'
    return _panel("p7", "规则化判读", "描述痕迹，不含因果推断", f'<div class="list">{body}</div>', extra)


# ── 底部 ───────────────────────────────────────────────────────────────

def _foot(a):
    return (
        '<div class="foot">'
        '<b>合规声明</b>：本面板展示的调研、高管增减持、股东户数、业绩预告、龙虎榜'
        '均为<b>合法公开披露</b>行为。任何将其解读为内幕交易的推断都不在本面板范围内。'
        '面板不做买卖建议、不做目标价预测、不推荐个股。<br>'
        '<b>数据来源</b>：东方财富公开接口（免 Key）。不保证准确性与完整性。<br>'
        '<b>已知局限</b>：调研/股东户数/席位数据均有明显滞后；股东户数为季度级；'
        '席位为历史累计口径；研报覆盖、大股东增减持、大宗交易、限售解禁四项接口不可得，'
        '已如实标注。<br>'
        '<b>免责</b>：本面板为个人研究工具，内容为数据呈现与规则化统计描述，'
        '不构成任何投资建议。据此操作，风险自负。'
        '</div>'
    )


# ── 主入口 ─────────────────────────────────────────────────────────────

def render_html(analysis, snapshot=None):
    """渲染完整 HTML 单文件。analysis 为空时必须仍产出结构完整的降级页面。"""
    a = analysis if isinstance(analysis, dict) else {}
    s = snapshot if isinstance(snapshot, dict) else {}

    # analysis 缺失时用 snapshot 兜底 as_of
    if not a.get("as_of"):
        a = dict(a)
        a["as_of"] = s.get("as_of")
    if not a.get("generated_at"):
        a = dict(a)
        a["generated_at"] = s.get("generated_at")

    parts = [
        _hero(a, s),
        _p1(a),
        _p2(a),
        _p3(a),
        _p4(a),
        _p5(a),
        _trust(a),
        _narr(a),
        _foot(a),
    ]
    body = "".join(parts)
    title = f"信息不对称面板 · 知情者行为痕迹 · {a.get('as_of') or DASH}"
    return (
        '<!DOCTYPE html>'
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(title)}</title><style>{CSS}</style></head>'
        f'<body><div class="wrap">{body}</div></body></html>'
    )


if __name__ == "__main__":
    import json, os
    here = os.path.dirname(os.path.abspath(__file__))
    sp = os.path.join(here, "asym_snapshot.json")
    snap = json.load(open(sp, encoding="utf-8")) if os.path.exists(sp) else {}
    try:
        import asym_metrics
        a = asym_metrics.compute(snap)
    except ImportError:
        a = {}
    html = render_html(a, snap)
    out = os.path.join(here, "out.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"已生成 {out}  ({len(html)} chars, "
          f"{html.count('<svg')} svg, {html.count('<section')} panel)")
    print("降级测试:", len(render_html({}, snap)), "chars")
