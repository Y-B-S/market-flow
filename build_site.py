"""构建静态网站：把三个面板输出到 docs/，供 GitHub Pages 托管。

产出：
  docs/index.html      资金流动大盘（主页）
  docs/asym.html       信息不对称面板
  docs/explain.html    背离解释器
  docs/archive/        历史归档（最近 N 天）

设计原则：
  - docs/ 是**发布产物**，可由本脚本随时重建
  - 每个页面都是自包含单文件（零外链），可直接静态托管
  - 加一个索引条，三个页面互相可跳转
"""
from __future__ import annotations

import json
import os
from datetime import date as _date, datetime as _dt
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DOCS = os.path.join(HERE, "docs")
# 历史网页保留策略：**永久保留**所有完整图表页。
#
# 成本：约 267KB/天 → 一年 65MB、十年 652MB（未压缩）。
# 收益：任何一天都能点开当时完整的页面（treemap / 板块排名 / 个股榜单）。
#
# 注意：历史页面写入后不再改动（只增不减），所以每次构建只复制**新增**的，
# 不重复复制旧的 —— 否则每次构建都要重写所有历史文件，天数一多会很慢。


def _nav(active, as_of=None, days=None):
    """三页共用的导航条。

    ⚠️ 可用性教训：原导航条只有三个页面链接，没有日期、没有历史入口。
    用户打开后第一反应是「这是哪天的？之前的数据怎么看？」——
    这两件事必须一眼可见，所以日期做成醒目的徽章，历史做成常驻入口。
    """
    items = [("index.html", "资金流动大盘", "flow"),
             ("asym.html", "信息不对称", "asym"),
             ("explain.html", "背离解释器", "explain")]
    links = []
    for href, label, key in items:
        cls = ' class="on"' if key == active else ""
        links.append(f'<a href="{href}"{cls}>{label}</a>')

    # 日期徽章（大、彩色、永远在导航条右侧）
    date_badge = ""
    if as_of:
        date_badge = (f'<span class="datebadge">{as_of}</span>')

    # 历史入口
    hist = ""
    if days:
        hist = (f'<a href="archive/" class="hist">'
                f'历史归档 <span class="cnt">{len(days)}</span></a>')
    else:
        hist = '<a href="archive/" class="hist">历史归档</a>'

    return ('<nav class="sitebar">'
            '<span class="brand">A股研究工具</span>'
            + "".join(links) +
            '<span class="spacer"></span>'
            + hist + date_badge +
            '</nav>')


NAV_CSS = """
.sitebar{position:sticky;top:0;z-index:999;display:flex;align-items:center;gap:6px;
 background:rgba(8,11,17,.94);backdrop-filter:blur(10px);border-bottom:1px solid #1b2331;
 padding:9px 18px;font-size:13px;flex-wrap:wrap}
.sitebar .brand{color:#5c6678;font-size:11.5px;letter-spacing:.6px;
 text-transform:uppercase;margin-right:8px}
.sitebar a{color:#8b96a8;text-decoration:none;padding:5px 12px;border-radius:7px;
 transition:.15s}
.sitebar a:hover{color:#e8eef7;background:rgba(255,255,255,.05)}
.sitebar a.on{color:#fff;background:rgba(59,130,246,.22);font-weight:650}
.sitebar .spacer{flex:1 1 auto}
.sitebar .hist{font-size:12.5px;color:#a8b3c4}
.sitebar .hist .cnt{color:#6b7688;font-size:11px}
.datebadge{font-size:13.5px;font-weight:700;color:#fff;
 background:rgba(59,130,246,.30);border:1px solid rgba(59,130,246,.55);
 border-radius:7px;padding:4px 12px;letter-spacing:.5px;
 font-variant-numeric:tabular-nums}
body{padding-top:0 !important}
"""


def _freshness(as_of):
    """数据新鲜度提示。

    可用性教训：页面上的日期只是灰色小字，用户看不出「这已经是昨天的数据了」。
    股票数据差一天，结论可能完全相反，所以必须显式提示。
    """
    if not as_of:
        return ""
    try:
        d = _dt.strptime(as_of, "%Y-%m-%d").date()
    except ValueError:
        return ""
    today = _date.today()
    gap = (today - d).days

    if gap <= 0:
        return ""
    if gap == 1:
        txt = "这是<b>上一个交易日</b>的数据（今天收盘后会自动更新）"
        bg, bd, fg = "rgba(240,160,32,.10)", "rgba(240,160,32,.4)", "#e8d3a8"
    else:
        txt = (f"这是 <b>{gap} 天前</b> 的数据，"
               f"最近可能没有运行采集或未推送")
        bg, bd, fg = "rgba(200,60,50,.12)", "rgba(200,60,50,.45)", "#f0b8b0"

    return (f'<div style="margin:0 0 14px;padding:10px 16px;border-radius:8px;'
            f'background:{bg};border:1px solid {bd};color:{fg};font-size:12.5px;'
            f'line-height:1.7">数据新鲜度：{txt}</div>')


def _inject(html, active, as_of=None, days=None):
    """把导航条与样式注入页面（页面本身是自包含的，只加导航）。"""
    nav = _nav(active, as_of=as_of, days=days)
    inject = f"<style>{NAV_CSS}</style>{nav}{_freshness(as_of)}"
    # 插到 <body> 之后
    for marker in ("<body>", "<body >"):
        if marker in html:
            return html.replace(marker, marker + inject, 1)
    return inject + html


def _snap_dates():
    """data/ 下所有有完整宽度数据的快照日期（用于导航条计数）。"""
    d = os.path.join(HERE, "data")
    out = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if not (fn.endswith(".json") and fn[:4].isdigit()):
                continue
            try:
                s0 = json.load(open(os.path.join(d, fn), encoding="utf-8"))
                if (s0.get("breadth") or {}).get("amount_total"):
                    out.append(s0.get("as_of") or fn[:-5])
            except Exception:  # noqa: BLE001
                continue
    return out


def _extract_date(html):
    """从页面内容提取交易日（形如 2026-11-04）。"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", html)
    return m.group(1) if m else None


def build():
    t0 = time.time()
    os.makedirs(DOCS, exist_ok=True)
    os.makedirs(os.path.join(DOCS, "archive"), exist_ok=True)
    built = []
    missing = []

    # ① 主页：资金流动大盘
    src = os.path.join(HERE, "out", "latest.html")
    if os.path.exists(src):
        html = open(src, encoding="utf-8").read()
        with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
            f.write(_inject(html, "flow", as_of=_extract_date(html), days=_snap_dates()))
        built.append(("index.html", len(html)))
    else:
        missing.append("out/latest.html（先跑 python3 main.py）")

    # ② 信息不对称
    src = os.path.join(HERE, "asym", "out.html")
    if os.path.exists(src):
        html = open(src, encoding="utf-8").read()
        with open(os.path.join(DOCS, "asym.html"), "w", encoding="utf-8") as f:
            f.write(_inject(html, "asym", as_of=_extract_date(html), days=_snap_dates()))
        built.append(("asym.html", len(html)))
    else:
        missing.append("asym/out.html（先跑 python3 asym/asym_render.py）")

    # ③ 背离解释器
    src = os.path.join(HERE, "explain", "out.html")
    if os.path.exists(src):
        html = open(src, encoding="utf-8").read()
        with open(os.path.join(DOCS, "explain.html"), "w", encoding="utf-8") as f:
            f.write(_inject(html, "explain", as_of=_extract_date(html), days=_snap_dates()))
        built.append(("explain.html", len(html)))
    else:
        missing.append("explain/out.html（先跑 python3 explain/explain_render.py）")

    # ④ 历史归档
    data_dir = os.path.join(HERE, "data")
    snaps = []
    if os.path.isdir(data_dir):
        snaps = sorted(x[:-5] for x in os.listdir(data_dir)
                       if x.endswith(".json") and x[:4].isdigit())
    arch_dir = os.path.join(DOCS, "archive")
    copied = 0
    kept = 0
    # 永久保留：遍历所有有完整数据的日期，但只复制**尚不存在**的页面
    # （历史页面内容不随新数据变化，重复复制只会浪费时间与 IO）
    _full_dates = []
    for d in snaps:
        src_p = os.path.join(HERE, "out", f"flow-{d}.html")
        if not os.path.exists(src_p):
            continue
        _full_dates.append(d)
        dst = os.path.join(arch_dir, f"{d}.html")
        if os.path.exists(dst):
            kept += 1
            continue
        shutil.copy2(src_p, dst)
        copied += 1

    # ⑤ 归档索引（用 bars.jsonl 做有信息量的列表，而非裸链接）
    try:
        import bars as _bars
        _rows = _bars.load()
    except Exception:  # noqa: BLE001
        _rows = []
    _rows = [r for r in _rows if r.get("date")]

    # 兜底：归档目录里实际存在的页面，若 bars 里没有对应记录，
    # 也要补一行 —— 否则历史页还在但索引里找不到它。
    _by_date = {r["date"]: r for r in _rows}
    for fn in sorted(os.listdir(arch_dir)):
        if not (fn.endswith(".html") and fn[:4].isdigit()):
            continue
        d = fn[:-5]
        if d not in _by_date:
            _rows.append({"date": d, "note": "仅页面，无摘要记录"})
            _by_date[d] = _rows[-1]
    _rows.sort(key=lambda r: r.get("date") or "", reverse=True)

    # 统计趋势所需
    _full = [r for r in _rows if not r.get("partial")]
    _part = [r for r in _rows if r.get("partial")]

    def _regime_color(lbl):
        return {"进攻": "#f6465d", "防守": "#0ecb81",
                "震荡": "#f0a020", "风险": "#c0392b"}.get(lbl or "", "#8b96a8")

    trs = []
    for r in _rows:
        d = r.get("date")
        has_page = os.path.exists(os.path.join(arch_dir, f"{d}.html"))
        partial = bool(r.get("partial"))
        reg = r.get("regime") or ("—" if partial else "—")
        rc = _regime_color(reg)
        if partial:
            # 板块级：只能给板块涨跌数与净额
            cells = (f'<td class="num">{r.get("boards_up", "—")}</td>'
                     f'<td class="num">{r.get("boards_down", "—")}</td>'
                     f'<td class="num">—</td>'
                     f'<td class="num">—</td>'
                     f'<td class="num">{r.get("main_net_yi", 0):+.0f}</td>')
            name_cell = (f'<td><span class="pdot"></span>{d}</td>'
                         if not has_page else f'<td>{d}</td>')
            tag = '<span class="tagp">板块级</span>'
        else:
            up = r.get("up"); dn = r.get("down")
            amt = r.get("amount_yi")
            net = r.get("main_net_yi")
            cells = (f'<td class="num up">{up:,}</td>' if up else '<td class="num">—</td>')
            cells += (f'<td class="num down">{dn:,}</td>' if dn else '<td class="num">—</td>')
            cells += (f'<td class="num">{amt/10000:.2f}万亿</td>' if amt else '<td class="num">—</td>')
            cells += (f'<td class="num">{r.get("limit_up", "—")}/{r.get("limit_down", "—")}</td>')
            cells += (f'<td class="num" style="color:{"#f6465d" if (net or 0) > 0 else "#0ecb81"}">'
                      f'{net:+.0f}</td>' if net is not None else '<td class="num">—</td>')
            name_cell = (f'<td><a href="archive/{d}.html">{d}</a></td>'
                         if has_page else f'<td>{d}</td>')
            tag = ""

        trs.append(
            '<tr>' + name_cell +
            f'<td style="color:{rc};font-weight:650">{reg}</td>' +
            cells +
            f'<td class="tg">{tag}</td></tr>')

    rows_html = "".join(trs)
    idx = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>历史归档</title><style>'
        'body{background:#080b11;color:#c9d3e0;font:13px/1.7 -apple-system,'
        '"PingFang SC",sans-serif;padding:22px 16px 40px}'
        '.wrap{max-width:960px;margin:0 auto}'
        'h1{font-size:19px;color:#e8eef7;margin-bottom:6px}'
        '.sub{font-size:12.5px;color:#6b7688;margin-bottom:18px;line-height:1.8}'
        'table{width:100%;border-collapse:collapse;font-size:12.5px}'
        'th{text-align:left;color:#6b7688;font-weight:500;font-size:11.5px;'
        'padding:8px 8px;border-bottom:1px solid #222b3a;letter-spacing:.3px}'
        'td{padding:8px;border-bottom:1px solid rgba(255,255,255,.045)}'
        'td.num{text-align:right;font-variant-numeric:tabular-nums}'
        '.up{color:#f6465d}.down{color:#0ecb81}'
        'a{color:#7ea8f0;text-decoration:none}a:hover{text-decoration:underline}'
        '.tagp{font-size:10.5px;color:#6b7688;background:rgba(255,255,255,.04);'
        'border:1px solid #222b3a;border-radius:5px;padding:1px 6px}'
        '.pdot{display:inline-block;width:5px;height:5px;border-radius:50%;'
        'background:#3a4557;margin-right:6px;vertical-align:middle}'
        '.note{margin-top:18px;padding:13px 16px;border-left:3px solid #2a3346;'
        'background:rgba(255,255,255,.02);border-radius:0 8px 8px 0;'
        'font-size:12px;color:#8792a4;line-height:1.85}'
        '.note b{color:#a8b3c4}'
        '</style></head><body><div class="wrap">'
        f'<h1>历史归档</h1>'
        f'<div class="sub">共 {len(_rows)} 个交易日 · '
        f'{len(_full)} 天有完整市场数据，{len(_part)} 天仅板块级</div>'
        '<table><thead><tr>'
        '<th>交易日</th><th>状态</th>'
        '<th>涨(个)</th><th>跌(个)</th>'
        '<th>成交额</th><th>涨停/跌停</th><th>主力净额(亿)</th><th></th>'
        '</tr></thead><tbody>' + rows_html + '</tbody></table>'
        '<div class="note">'
        '<b>怎么看这张表</b><br>'
        '· 点击日期可打开当天的完整页面（带图表的归档）。<br>'
        '· 标「板块级」的日期没有个股宽度数据——那些天的全市场涨跌家数、'
        '涨停池无法从接口回溯，只能保留板块净额与板块涨跌数。<br>'
        '· 注意列含义随行变化：<b>有完整数据的日子，"涨/跌"是个股家数</b>；'
        '<b>标「板块级」的日子，"涨/跌"是板块个数</b>（约 496 个板块中多少个涨）。'
        '两者不能直接比较。<br>'
        '<b>为什么日期不全</b>：数据从 2026-09-15 才开始留存。'
        '接口本身只保留滚动 120 个交易日，更早的数据已无法取回。<br>'
        '<b>关于 11-02 / 11-03</b>：这两天因宽度数据错误已被移除'
        '（11-02 实际是上涨日却被记录成跌多涨少），宁缺不留错。'
        '</div></div></body></html>')

    with open(os.path.join(arch_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(idx)
    built.append(("archive/index.html", len(idx)))

    # ⑥ 禁用 Jekyll（避免下划线文件被忽略）
    with open(os.path.join(DOCS, ".nojekyll"), "w") as f:
        f.write("")

    print(f"· 构建完成，耗时 {time.time()-t0:.1f}s")
    for name, size in built:
        print(f"  ✓ docs/{name}  ({size/1024:.0f} KB)")
    if missing:
        print("\n⚠ 缺失（先运行对应脚本）：")
        for m in missing:
            print(f"  - {m}")
    total = sum(os.path.getsize(os.path.join(dp, fn))
                for dp, _, fns in os.walk(DOCS) for fn in fns)
    print(f"\n· docs/ 总计 {total/1048576:.2f} MB")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(build())
