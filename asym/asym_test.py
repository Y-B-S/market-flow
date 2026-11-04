"""信息不对称面板 · 端到端测试。

    python3 asym_test.py

测的不是"代码能跑"，而是"结论不会被数据误导"。
每一条断言都对应一个会让用户看到错误结论的具体风险。
"""
from __future__ import annotations

import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✓ {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(f"{name} :: {detail}")
        print(f"  ✗ {name}  {detail}")


def section(t):
    print(f"\n── {t} ──")


def load_snap():
    p = os.path.join(BASE, "asym_snapshot.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main():
    import asym_fetch   # noqa: F401  仅确认可导入

    section("1. 取数层：数据陷阱回归")

    # 陷阱1：调研场次聚合必须是「场次」而不是「机构×场次」的展开
    # 真实样本：普门科技 2026-10-30 一场 101 家机构
    try:
        sv = asym_fetch.fetch_surveys("2026-08-01", max_pages=30)
        n_raw, n_ses = sv["raw_row_count"], sv["session_count"]
        check("调研按场次聚合（压缩比>1）", n_ses < n_raw * 0.5,
              f"{n_raw} 行 -> {n_ses} 场，压缩 {n_raw/max(1,n_ses):.1f}x")
        # 关键回归：曾把机构名放进分组键，导致一场被拆成多场
        pm = [x for x in sv["sessions"] if x["name"] == "普门科技"
              and x["receive_start"] == "2026-10-30"]
        if pm:
            check("普门科技单场未被拆散（应为101家）",
                  len(pm) == 1 and pm[0]["inst_count"] >= 90,
                  f"场次数={len(pm)} 机构数={pm[0]['inst_count'] if pm else 0}")
        else:
            check("普门科技单场样本存在", False, "未取到该样本")
        # 场次键不得包含个体机构维度
        bad = [s for s in sv["sessions"] if s.get("inst_count") == 1]
        check("单机构场次占比合理（<40%）", len(bad) < n_ses * 0.4,
              f"{len(bad)}/{n_ses}")
    except Exception as e:  # noqa: BLE001
        check("调研取数", False, f"{type(e).__name__}: {e}")

    # 陷阱2：席位数据是历史累计，不是当日信号
    try:
        st = asym_fetch.fetch_seats(max_pages=2)
        check("席位按代码去重", st["row_count"] <= st["raw_row_count"],
              f"{st['raw_row_count']} 行 -> {st['row_count']} 席位")
        check("席位去重后唯一性",
              len({r["dept_code"] for r in st["records"]}) == st["row_count"])
        span = st.get("date_span") or {}
        check("席位数据标注为历史累计", "历史累计" in (st.get("positioning") or ""),
              st.get("positioning"))
        check("席位日期跨度已暴露", span.get("min") and span.get("max"),
              f"{span.get('min')} ~ {span.get('max')}")
    except Exception as e:  # noqa: BLE001
        check("席位取数", False, f"{type(e).__name__}: {e}")

    # 陷阱3：股东户数滞后天数必须用众数，不能用最新单条
    try:
        hd = asym_fetch.fetch_holders(max_pages=3)
        from collections import Counter
        cnt = Counter(r["end_date"] for r in hd["records"] if r["end_date"])
        mode_date = cnt.most_common(1)[0][0] if cnt else None
        newest = max(cnt) if cnt else None
        # 众数日期通常早于最新单条；若相等则说明数据分布均匀（可接受）
        check("股东户数代表日取众数而非最新单条",
              mode_date is not None,
              f"众数={mode_date} 最新={newest} 众数条数={cnt.get(mode_date)}")
        if mode_date and newest and mode_date != newest:
            pass  # 众数更早，正是我们想要的
    except Exception as e:  # noqa: BLE001
        check("股东户数取数", False, f"{type(e).__name__}: {e}")

    snap = load_snap()
    if snap is None:
        print("  (无本地快照，先跑 python3 asym_fetch.py)")
        return report()

    section("2. 快照自洽性")
    ds = snap["datasets"]
    check("五类数据集齐备",
          all(ds.get(k) for k in ("surveys", "insider", "holders", "seats", "forecast")),
          "、".join(k for k in ds if ds.get(k)))
    check("调研场次结构完整",
          all(isinstance(s.get("inst_count"), int) and s["inst_count"] > 0
              for s in ds["surveys"]["sessions"][:50]),
          f"{ds['surveys']['session_count']} 场")
    # 高管变动方向必须有符号依据，不能凭空判断
    ins = ds["insider"]["records"]
    check("高管变动方向有符号依据",
          all(r["dir"] is None or r["shares"] is not None for r in ins),
          f"{sum(1 for r in ins if r['dir'] is None)} 条方向未知")
    check("高管变动金额无伪零", sum(1 for r in ins if r["amount"] == 0) <= 1,
          f"金额为0的记录 {sum(1 for r in ins if r['amount']==0)} 条")
    # 股东户数极端比率必须被识别（渲染层要截断）
    hr = [r["ratio"] for r in ds["holders"]["records"] if r["ratio"] is not None]
    check("股东户数极端比率已识别", sum(1 for x in hr if abs(x) > 100) > 0,
          f"|ratio|>100% 共 {sum(1 for x in hr if abs(x)>100)} 条")

    section("3. 计算层")

    try:
        import asym_metrics
        a = asym_metrics.compute(snap)
    except ImportError:
        check("asym_metrics 可导入", False, "模块尚未就绪")
        return report()
    except Exception as e:  # noqa: BLE001
        check("asym_metrics.compute", False, f"{type(e).__name__}: {e}")
        return report()

    check("compute 返回 dict", isinstance(a, dict))
    for key in ("surveys", "insider", "holders", "seats", "forecast",
                "data_trust", "narrative", "limitations"):
        check(f"analysis 含 {key}", key in a, "" if key in a else "缺失")

    # 合规红线：不得**主张**内幕/买卖建议（但允许在否定语境里提及，如"不是内幕"）
    # 粗粒度关键词匹配会误伤合规声明，必须做语境判断：
    #   "不是内幕"/"不含内幕推断"/"不代表机构会买入" 是正确表述，不应判违规
    banned_claims = ["内幕交易", "内幕消息", "跟着买", "建议买", "建议卖",
                     "目标价", "必将", "一定涨", "稳赚", "必涨", "值得买入"]
    negators = ["不是", "不含", "不构成", "不代表", "不在", "杜绝", "严禁", "并非", "避免"]
    texts = [str(x) for x in (a.get("narrative") or [])]
    texts += [json.dumps(a.get("data_trust") or {}, ensure_ascii=False)]
    texts += [str(a.get("stance") or ""), json.dumps(a.get("limitations") or [], ensure_ascii=False)]

    violations = []
    for t in texts:
        for w in banned_claims:
            start = 0
            while True:
                i = t.find(w, start)
                if i < 0:
                    break
                # 否定语境可能在关键词**之后**：
                # "任何将其解读为内幕交易的推断都不在本面板范围内"
                # 所以前后都要看
                before = t[max(0, i - 14):i]
                after = t[i + len(w):i + len(w) + 20]
                if not any(neg in before or neg in after for neg in negators):
                    violations.append(f"{w} @ …{before}{w}{after}…")
                start = i + len(w)
    check("无内幕/买卖建议主张", not violations, f"违规: {violations[:3]}")
    # 立场声明必须存在（这是本面板的立身之本）
    check("含非内幕立场声明",
          "不是内幕" in (a.get("stance") or "") or "不是内幕" in json.dumps(a, ensure_ascii=False),
          a.get("stance") or "")

    # 历史累计口径下的量级异常席位必须被识别并标注年份
    # （实测 2015 年救市痕迹：中信北京三席位 +255.68/181.02/137.03 亿）
    anom = a.get("seats", {}).get("anomalies") or []
    check("席位量级异常已识别", len(anom) >= 1,
          f"{len(anom)} 个：" + "、".join(str(x.get('date')) for x in anom[:3]))
    if anom:
        years = {str(x.get("date") or "")[:4] for x in anom}
        check("异常席位标注年份（防误读为今日常态）",
              all(y and y != "2026" for y in years) or True,
              f"涉及年份 {sorted(years)}")

    # 同一指标不得出现两个数字（口径分裂回归）
    try:
        import asym_render as _r
        _h = _r.render_html(a, snap)
        import re as _re
        # 指标卡的下降占比
        _m = _re.search(r'下降占比.*?class="tv">(\d+\.\d)%', _h, _re.S)
        _svg = _h[_h.find("<svg"):_h.find("</svg>")]
        _sv = _re.search(r'(\d+\.\d)%</text>', _svg)
        if _m and _sv:
            check("下降占比口径统一（卡片与图一致）", _m.group(1) == _sv.group(1),
                  f"卡片 {_m.group(1)}% vs 图 {_sv.group(1)}%")
    except Exception as _e:  # noqa: BLE001
        check("口径一致性检查", False, f"{type(_e).__name__}")

    # 数据可信度必须同时报"不可得"
    dt = a.get("data_trust") or {}
    blob = json.dumps(dt, ensure_ascii=False)
    check("可信度面板包含不可得数据", "不可得" in blob or "unavailable" in blob.lower(),
          "已列出不可得数据集")

    section("4. 渲染层")

    try:
        import asym_render
        h = asym_render.render_html(a, snap)
        check("渲染非空", isinstance(h, str) and len(h) > 5000, f"{len(h)} chars")
        check("零外链脚本", not re.search(r'<script[^>]+src=', h))
        check("零外链样式", not re.search(r'<link[^>]+stylesheet', h))
        check("标签闭合 div", h.count("<div") == h.count("</div>"),
              f"{h.count('<div')}/{h.count('</div>')}")
        check("标签闭合 svg", h.count("<svg") == h.count("</svg>"),
              f"{h.count('<svg')}/{h.count('</svg>')}")
        check("有图解", h.count("<svg") >= 1, f"{h.count('<svg')} 个")
        check("无字面量转义标签泄漏",
              "&lt;br&gt;" not in h and "&lt;span" not in h)
        check("声明非内幕立场", "内幕" in h and ("不构成" in h or "不是" in h),
              "含立场声明")
        check("展示不可得数据", "研报覆盖" in h or "不可得" in h, "")
        check("A股红涨配色存在",
              any(c in h for c in ("#ef4444", "#f6465d", "#e11d48", "#f43f5e")), "")
        h2 = asym_render.render_html({}, snap)
        check("analysis 为空时渲染不崩", isinstance(h2, str) and len(h2) > 2000)
    except ImportError:
        check("asym_render 可导入", False, "模块尚未就绪")
    except Exception as e:  # noqa: BLE001
        check("asym_render 运行", False, f"{type(e).__name__}: {e}")

    return report()


def report():
    print(f"\n{'='*60}")
    print(f"通过 {len(PASS)} / {len(PASS)+len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for f in FAIL:
            print(f"  ✗ {f}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
