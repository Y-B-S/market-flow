"""背离解释器 · 渲染层。

纯函数：render_html(analysis, meta) -> str
每张卡对应一个背离板块，按可用层数给出不同强度的结论。
"""
from __future__ import annotations

from html import escape

DASH = "—"

# A 股配色
C_UP = "#f6465d"
C_DOWN = "#0ecb81"
C_MUTED = "#8b96a8"
C_WARN = "#f0a020"
C_TEXT = "#e8eef7"

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080b11;color:#c9d3e0;
 font:14px/1.62 -apple-system,BlinkMacSystemFont,"PingFang SC",
 "Hiragino Sans GB","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased;
 padding:22px 16px 44px}
.wrap{max-width:1080px;margin:0 auto}
.hero{background:linear-gradient(135deg,rgba(59,130,246,.10),rgba(16,185,129,.05));
 border:1px solid #1e2635;border-radius:14px;padding:20px 24px;margin-bottom:18px}
.hero h1{font-size:20px;font-weight:700;color:#e8eef7;letter-spacing:.4px}
.hero .sub{margin-top:9px;font-size:14px;color:#b9c4d4;line-height:1.75}
.hero .sub b{color:#f0a020}
.tags{margin-top:13px;display:flex;flex-wrap:wrap;gap:8px}
.tag{font-size:11.5px;color:#8b96a8;background:rgba(255,255,255,.03);
 border:1px solid #1b2331;border-radius:7px;padding:3px 9px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0}
@media(max-width:760px){.stats{grid-template-columns:repeat(2,1fr)}}
.stat{background:rgba(255,255,255,.024);border:1px solid #1b2331;
 border-radius:10px;padding:11px 13px}
.stat .k{font-size:11.5px;color:#8b96a8}
.stat .v{font-size:19px;font-weight:700;font-variant-numeric:tabular-nums;
 color:#e8eef7;margin-top:3px}
.card{background:#0d1119;border:1px solid #1b2331;border-radius:13px;
 padding:18px 20px;margin-bottom:15px}
.chd{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
 padding-bottom:11px;border-bottom:1px solid #1b2331}
.chd .nm{font-size:17px;font-weight:700;color:#e8eef7}
.chd .cd{font-size:11.5px;color:#5c6678}
.badge{font-size:11.5px;padding:3px 10px;border-radius:7px;font-weight:650}
.b-pass{background:rgba(240,160,32,.14);color:#f0a020;border:1px solid rgba(240,160,32,.35)}
.b-mid{background:rgba(139,150,168,.12);color:#a8b3c4;border:1px solid #2a3346}
.b-low{background:rgba(139,150,168,.08);color:#76819a;border:1px solid #232b3b}
.flowrow{display:flex;flex-wrap:wrap;gap:16px;padding:11px 0 4px;font-size:13px}
.flowrow .fi .k{color:#76819a;font-size:11.5px}
.flowrow .fi .v{font-variant-numeric:tabular-nums;font-weight:650}
.layers{margin-top:8px}
.ly{display:flex;gap:11px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.035);
 align-items:flex-start}
.ly:last-child{border-bottom:none}
.ly .ic{flex:0 0 18px;font-size:14px;line-height:1.5}
.ly .body{flex:1 1 auto;min-width:0}
.ly .hdr{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
.ly .lname{font-size:13px;font-weight:700;color:#dfe6f0;flex:0 0 auto}
.ly .ldir{font-size:11.5px;padding:2px 7px;border-radius:6px;font-weight:650}
.d-support{background:rgba(246,70,93,.13);color:#ff8095}
.d-oppose{background:rgba(14,203,129,.13);color:#3ddb9e}
.d-neutral{background:rgba(139,150,168,.12);color:#9aa5b8}
.ly .ev{font-size:12px;color:#8792a4;line-height:1.7;margin-top:4px;
 word-break:break-word}
.ly .cv{font-size:11px;color:#6b7688;margin-top:3px;font-style:italic}
.na{color:#5c6678;font-size:12px}
.verdict{margin-top:12px;padding:12px 14px;border-radius:9px;font-size:13px;line-height:1.85}
.v-ok{background:rgba(246,70,93,.07);border-left:3px solid #f6465d}
.v-bad{background:rgba(240,160,32,.08);border-left:3px solid #f0a020}
.v-na{background:rgba(255,255,255,.022);border-left:3px solid #2a3346}
.narr{margin-top:12px;padding-top:11px;border-top:1px dashed #1b2331}
.narr li{list-style:none;font-size:12.5px;color:#b9c4d4;line-height:1.85;
 padding-left:13px;position:relative;margin-bottom:5px}
.narr li:before{content:"·";position:absolute;left:0;color:#5c6678}
.narr li.strong{color:#e8eef7;font-weight:650}
.foot{margin-top:20px;padding:16px 20px;background:#0a0e15;border:1px solid #1b2331;
 border-radius:12px;font-size:11.5px;color:#6b7688;line-height:1.9}
.foot b{color:#8792a4}
.warnbox{margin-top:14px;border-left:3px solid #f0a020;background:rgba(240,160,32,.07);
 border-radius:0 9px 9px 0;padding:11px 14px;font-size:12px;color:#d9c9a8;line-height:1.8}
.warnbox b{color:#f0a020}
.empty{text-align:center;padding:26px 0;color:#6b7688;font-size:12.5px}
"""


def esc(v):
    return DASH if v is None else escape(str(v), quote=True)


def fmt_yi(v):
    if v is None:
        return DASH
    try:
        n = float(v)
    except (TypeError, ValueError):
        return DASH
    if abs(n) >= 1e12:
        return f"{n/1e12:+.2f}万亿"
    if abs(n) >= 1e8:
        return f"{n/1e8:+.1f}亿"
    return f"{n/1e4:+.0f}万"


def _dir_class(dir_num):
    if dir_num == 1:
        return "d-support"
    if dir_num == -1:
        return "d-oppose"
    return "d-neutral"


def _verdict_class(label):
    if label in ("一致", "两层一致"):
        return "v-ok"
    if label in ("矛盾", "部分矛盾", "两层矛盾"):
        return "v-bad"
    return "v-na"


def _badge_class(avail):
    if avail >= 3:
        return "b-mid"
    if avail == 2:
        return "b-mid"
    return "b-low"


def _layer_row(l):
    ok = l.get("available")
    ic = "✓" if ok else "✗"
    ic_color = C_DOWN if ok else "#5c6678"
    dnum = l.get("dir_num")
    dclass = _dir_class(dnum)
    dtext = l.get("direction") or "不可得"
    ev = l.get("evidence") or ""
    cv = l.get("caveat") or ""
    return (
        '<div class="ly">'
        f'<div class="ic" style="color:{ic_color}">{ic}</div>'
        '<div class="body">'
        '<div class="hdr">'
        f'<span class="lname">{esc(l.get("layer"))}</span>'
        f'<span class="ldir {dclass}">{esc(dtext)}</span>'
        f'<span class="na">{esc(l.get("lag") or "")}</span>'
        '</div>'
        f'<div class="ev">{esc(ev)}</div>'
        f'{f"<div class=cv>{esc(cv)}</div>" if cv else ""}'
        '</div></div>'
    )


def _card(c):
    cons = c.get("consistency") or {}
    avail = cons.get("available") or 0
    label = cons.get("label") or DASH
    flow = c.get("flow") or {}

    head = (
        '<div class="chd">'
        f'<span class="nm">{esc(c.get("name"))}</span>'
        f'<span class="cd">{esc(c.get("code"))}</span>'
        f'<span class="badge {_badge_class(avail)}">{esc(label)} · {avail}层</span>'
        + (f'<span class="badge b-mid">象限 {esc(flow.get("quadrant"))}</span>'
           if flow.get("quadrant") else "")
        + '</div>'
    )

    flowrow = (
        '<div class="flowrow">'
        f'<div class="fi"><span class="k">今日主力</span> '
        f'<span class="v" style="color:{C_UP}">{esc(fmt_yi(flow.get("today")))}</span></div>'
        f'<div class="fi"><span class="k">5日</span> '
        f'<span class="v" style="color:{C_DOWN}">{esc(fmt_yi(flow.get("d5")))}</span></div>'
        f'<div class="fi"><span class="k">10日</span> '
        f'<span class="v" style="color:{C_DOWN}">{esc(fmt_yi(flow.get("d10")))}</span></div>'
        '</div>'
    )

    layers = "".join(_layer_row(l) for l in (c.get("layers") or []))

    # 判定
    vtext = ""
    if avail >= 3:
        if label == "一致":
            vtext = ("多维度一致：估值、新闻、财报均指向「基本面或情绪转弱」，"
                     "这个背离相对可信，中期流出有解释。")
        elif label in ("矛盾", "部分矛盾"):
            vtext = ("维度矛盾：部分证据支持「资金撤离合理」，部分证据相反。"
                     "这个背离的成因存疑——可能只是估值消化或短期情绪，"
                     "需要结合基本面人工判断。")
        else:
            vtext = f"判定：{label}。"
    elif avail == 2:
        vtext = (f"只有两层证据（{label}），结论强度中等。"
                 "缺失的层无法用代理数据填补（代理 PE 与官方口径平均差 29.6%，已验证不可用）。")
    else:
        vtext = "证据不足：可用层数太少，只陈列事实，不下结论。"

    # narrative：最后一条是免责，其余正常渲染
    narr_items = []
    for x in (c.get("narrative") or []):
        cls = ' class="strong"' if "综合判定" in x else ""
        narr_items.append(f"<li{cls}>{esc(x.replace('**', ''))}</li>")

    verdict = f'<div class="verdict {_verdict_class(label)}">{esc(vtext)}</div>'
    narr = f'<ul class="narr">{"".join(narr_items)}</ul>'

    return f'<div class="card">{head}{flowrow}<div class="layers">{layers}</div>{verdict}{narr}</div>'


def render_html(analysis, meta=None):
    a = analysis if isinstance(analysis, dict) else {}
    cards = a.get("cards") or []
    dist = a.get("layer_distribution") or {}
    lims = a.get("limitations") or []

    hero = (
        '<div class="hero">'
        '<h1>背离解释器 · 资金背离的多维交叉检验</h1>'
        '<div class="sub">对一个资金背离信号（今日流入但中期流出），'
        '同时陈列<b>估值 / 新闻 / 财报</b>三层证据，'
        '看它们是互相印证还是互相矛盾。</div>'
        '<div class="tags">'
        f'<span class="tag">交易日 {esc(a.get("as_of"))}</span>'
        f'<span class="tag">生成 {esc((a.get("generated_at") or "")[:19])}</span>'
        f'<span class="tag">覆盖 {len(cards)} 个背离板块</span>'
        '</div>'
        '</div>'
    )

    total = len(cards) or 1
    n3 = dist.get(3, 0) + dist.get(4, 0)
    n2 = dist.get(2, 0)
    n1 = dist.get(1, 0)
    # 兜底：分布里出现未预料的层数时，也要计入
    for k, v in dist.items():
        if k > 4:
            n3 += v
    stats = (
        '<div class="stats">'
        f'<div class="stat"><div class="k">三层及以上</div>'
        f'<div class="v" style="color:{C_DOWN}">{n3}</div>'
        f'<div class="k">{n3/total*100:.0f}% 可做完整检验</div></div>'
        f'<div class="stat"><div class="k">仅两层</div>'
        f'<div class="v" style="color:{C_WARN}">{n2}</div>'
        f'<div class="k">{n2/total*100:.0f}% 结论强度中等</div></div>'
        f'<div class="stat"><div class="k">仅一层</div>'
        f'<div class="v" style="color:#76819a">{n1}</div>'
        f'<div class="k">{n1/total*100:.0f}% 证据不足</div></div>'
        f'<div class="stat"><div class="k">背离板块</div>'
        f'<div class="v">{total}</div>'
        f'<div class="k">本次分析对象</div></div>'
        '</div>'
    )

    body = "".join(_card(c) for c in cards) if cards else \
        '<div class="card"><div class="empty">无背离板块可分析</div></div>'

    # 缺失说明
    warn = ""
    if lims:
        items = "".join(f"<div>· {esc(x)}</div>" for x in lims)
        warn = ('<div class="warnbox"><b>数据边界（必须知道）</b>'
                f'{items}</div>')

    foot = (
        '<div class="foot">'
        '<b>这是什么</b>：把资金背离信号与估值、新闻、财报三个维度交叉陈列，'
        '目的是<b>暴露矛盾</b>，而不是给出结论。<br>'
        '<b>不是什么</b>：不是交易信号。四象限资金流信号已用 120 个交易日、'
        '5.4 万个样本做过预注册检验，<b>未通过</b>（见项目根目录 PREREGISTRATION.md）。<br>'
        '<b>数据来源</b>：东方财富公开接口。估值仅覆盖 127 个申万板块（东财 496 板块体系的 27%）；'
        '新闻已剔除同源资金流报道与 ETF 软文；财报覆盖率约 4%，且存在系统性偏乐观。<br>'
        '<b>免责</b>：内容为公开数据的交叉陈列与规则判定，不构成任何投资建议。'
        '</div>'
    )

    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>背离解释器 · {esc(a.get("as_of"))}</title>'
        f'<style>{CSS}</style></head><body>'
        f'<div class="wrap">{hero}{stats}{body}{warn}{foot}</div>'
        '</body></html>'
    )


render = render_html


if __name__ == "__main__":
    import json
    import os
    import sys
    HERE = os.path.dirname(os.path.abspath(__file__))
    ROOT = os.path.dirname(HERE)
    sys.path.insert(0, ROOT)
    sys.path.insert(0, HERE)
    import explain_fetch as F
    import explain_metrics as M
    import metrics

    snap = json.load(open(os.path.join(ROOT, "data", "2026-11-03.json"), encoding="utf-8"))
    a0 = metrics.compute(snap)
    div = (a0.get("divergence_gated") or [])[:8]
    boards_meta = [(d.get("code"), d.get("name")) for d in div]
    names = [d["name"] for d in div]
    data = F.fetch_all(names, boards=boards_meta, as_of="2026-11-03")
    res = M.compute(div, data)
    html = render_html(res)
    out = os.path.join(HERE, "out.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"已生成 {out} ({len(html)} chars, {len(res['cards'])} 卡片)")
    print(f"层数分布: {res['layer_distribution']}")
