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
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DOCS = os.path.join(HERE, "docs")
ARCHIVE_KEEP = 30


def _nav(active):
    """三页共用的导航条。"""
    items = [("index.html", "资金流动大盘", "flow"),
             ("asym.html", "信息不对称", "asym"),
             ("explain.html", "背离解释器", "explain")]
    links = []
    for href, label, key in items:
        cls = ' class="on"' if key == active else ""
        links.append(f'<a href="{href}"{cls}>{label}</a>')
    return ('<nav class="sitebar">'
            '<span class="brand">A股研究工具</span>'
            + "".join(links) +
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
body{padding-top:0 !important}
"""


def _inject(html, active):
    """把导航条与样式注入页面（页面本身是自包含的，只加导航）。"""
    nav = _nav(active)
    inject = f"<style>{NAV_CSS}</style>{nav}"
    # 插到 <body> 之后
    for marker in ("<body>", "<body >"):
        if marker in html:
            return html.replace(marker, marker + inject, 1)
    return inject + html


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
            f.write(_inject(html, "flow"))
        built.append(("index.html", len(html)))
    else:
        missing.append("out/latest.html（先跑 python3 main.py）")

    # ② 信息不对称
    src = os.path.join(HERE, "asym", "out.html")
    if os.path.exists(src):
        html = open(src, encoding="utf-8").read()
        with open(os.path.join(DOCS, "asym.html"), "w", encoding="utf-8") as f:
            f.write(_inject(html, "asym"))
        built.append(("asym.html", len(html)))
    else:
        missing.append("asym/out.html（先跑 python3 asym/asym_render.py）")

    # ③ 背离解释器
    src = os.path.join(HERE, "explain", "out.html")
    if os.path.exists(src):
        html = open(src, encoding="utf-8").read()
        with open(os.path.join(DOCS, "explain.html"), "w", encoding="utf-8") as f:
            f.write(_inject(html, "explain"))
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
    for d in snaps[-ARCHIVE_KEEP:]:
        s = os.path.join(HERE, "out", f"flow-{d}.html")
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(arch_dir, f"{d}.html"))
            copied += 1

    # ⑤ 归档索引
    if copied:
        rows = "".join(
            f'<li><a href="archive/{d}.html">{d}</a></li>'
            for d in reversed(snaps[-ARCHIVE_KEEP:])
            if os.path.exists(os.path.join(arch_dir, f"{d}.html")))
        idx = (
            '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>历史归档</title>'
            '<style>body{background:#080b11;color:#c9d3e0;font:14px/1.7 '
            '-apple-system,"PingFang SC",sans-serif;padding:24px;max-width:760px;margin:0 auto}'
            'a{color:#7ea8f0;text-decoration:none}a:hover{text-decoration:underline}'
            'h1{font-size:18px;margin-bottom:16px;color:#e8eef7}'
            'li{margin:5px 0}</style></head><body>'
            f'<h1>历史归档（最近 {copied} 个交易日）</h1>'
            f'<ul>{rows}</ul>'
            '<p style="color:#6b7688;font-size:12px;margin-top:24px">'
            '每个文件是当日收盘后的完整快照，可离线打开。</p>'
            '</body></html>')
        with open(os.path.join(DOCS, "archive", "index.html"), "w", encoding="utf-8") as f:
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
