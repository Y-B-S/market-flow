"""长期存储：只保留不可重获核心指标。

## 为什么需要这个模块

东财接口的关键数据**只保留滚动 120 个交易日（约 6 个月）**。
实测：板块/个股资金流日线 `lmt=250` 也只返回 121 条。

这意味着 —— **今天不存下来，6 个月后这段历史就永久消失**。
存储的唯一意义就是让项目活过接口的记忆。

## 存什么、不存什么

存：
  · 市场状态（regime）、涨跌家数、涨停/跌停、成交额、主力净额
  · 四象限分布、背离数量、龙虎榜/两融关键值
  · 板块 TOP 榜（净流入/流出各前 10）

不存：
  · 5819 只个股明细（占完整快照 80% 体积，且分析中从未使用）
  · 531 个概念板块明细
  · 拒绝理由：这些可用 `data/snapshot/` 的完整快照回溯（前提是存了）

体积：约 2KB/天 → 一年 500KB → 十年 5MB。可以放心提交进 git。
"""
from __future__ import annotations

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BARS = os.path.join(HERE, "data", "bars.jsonl")


def _n(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    return None


def _f(v):
    """带单位友好的四舍五入（亿元）。"""
    if v is None:
        return None
    try:
        return round(float(v) / 1e8, 2)
    except (TypeError, ValueError):
        return None


def _top(rows, key, n=10, reverse=True):
    """取 TOPN，返回紧凑的 [名称, 数值] 列表。"""
    if not rows:
        return []
    vals = [(r.get("name"), _n(r.get(key))) for r in rows
            if r.get("name") and _n(r.get(key)) is not None]
    vals.sort(key=lambda x: x[1], reverse=reverse)
    return [{"n": nm, "v": round(val / 1e8, 2)} for nm, val in vals[:n]]


def summarize(snapshot, analysis):
    """把完整快照 + 分析结果压成一行核心指标。"""
    b = snapshot.get("breadth") or {}
    a = analysis or {}
    reg = a.get("regime") or {}
    sent = a.get("sentiment") or {}

    quads = a.get("quadrants") or {}
    qcounts = {}
    if isinstance(quads, dict):
        for name in ("accel", "probe", "bleed", "dist"):
            v = quads.get(name)
            if isinstance(v, dict):
                qcounts[name] = v.get("count")
            elif isinstance(v, list):
                qcounts[name] = len(v)

    row = {
        "date": snapshot.get("as_of"),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        # ── 市场宽度 ──
        "count": b.get("count"),
        "up": b.get("up"),
        "down": b.get("down"),
        "flat": b.get("flat"),
        "suspended": b.get("suspended"),
        "limit_up": b.get("limit_up"),
        "limit_down": b.get("limit_down"),
        "up5pct": b.get("up5pct"),
        "down5pct": b.get("down5pct"),
        # ── 资金 ──
        "amount_yi": _f(b.get("amount_total")),
        "main_net_yi": _f(b.get("main_net_total")),
        # ── 结论 ──
        "regime": reg.get("label"),
        "regime_z": _n(reg.get("z")),
        "up_ratio": _n(sent.get("up_ratio")),
        "sentiment_score": _n(sent.get("score")),
        # ── 结构 ──
        "quadrants": qcounts,
        "divergence": len(a.get("divergence") or []),
        "divergence_strong": len(a.get("divergence_strong") or []),
        # ── 板块 TOP ──
        "boards_in": _top(a.get("industries_ranked"), "main_net"),
        "boards_out": _top(a.get("industries_ranked"), "main_net", reverse=False),
        # ── 其他关键块 ──
        "margin_yi": _f((a.get("margin_block") or {}).get("margin_balance")),
        "lhb_net_yi": _f((a.get("lhb_block") or {}).get("net_buy")),
        # ── 元信息 ──
        "state": snapshot.get("trading_state"),
        "source": ("backfill" if snapshot.get("backfilled")
                   else ("repaired" if snapshot.get("repaired") else "live")),
    }
    # 去掉 None 值，减小体积
    return {k: v for k, v in row.items() if v is not None and v != {} and v != []}


def append(row, path=BARS):
    """追加一行，同日重复则替换（重跑不产生重复记录）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("date") != row.get("date"):
                    existing.append(r)
    existing.append(row)
    existing.sort(key=lambda r: r.get("date") or "")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in existing:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, path)
    return len(existing)


def load(path=BARS):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def stats(path=BARS):
    rows = load(path)
    if not rows:
        return {"days": 0, "bytes": 0, "kb_per_day": 0}
    size = os.path.getsize(path)
    return {
        "days": len(rows),
        "bytes": size,
        "kb_per_day": round(size / len(rows) / 1024, 2),
        "first": rows[0].get("date"),
        "last": rows[-1].get("date"),
        "projection_10y_mb": round(size / len(rows) * 2500 / 1048576, 1),
    }


def summarize_board_only(snapshot):
    """从「只有板块级数据」的回补快照提取能拿到的部分。

    回补快照没有 breadth（全市场宽度指标不可回溯），但板块级数据里有：
      · 各板块主力净额 → 可加总出全市场近似净额
      · 各板块涨跌幅 → 可算出上涨板块数（不等于涨跌家数！）
      · 板块 TOP 榜

    **必须标注 partial=True**，避免以后把它当完整数据用。
    """
    inds = snapshot.get("industries") or []
    if not inds:
        return None
    nets = [x.get("main_net") for x in inds if x.get("main_net") is not None]
    chgs = [x.get("chg") for x in inds if x.get("chg") is not None]
    if not nets:
        return None
    ups = sum(1 for c in chgs if c > 0)
    row = {
        "date": snapshot.get("as_of"),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "partial": True,
        "boards": len(inds),
        "main_net_yi": round(sum(nets) / 1e8, 2),
        "boards_up": ups,
        "boards_down": len(chgs) - ups,
        "boards_in": _top(inds, "main_net"),
        "boards_out": _top(inds, "main_net", reverse=False),
        "source": "backfill",
        "note": "回补快照：无个股宽度指标（涨跌家数/涨停池不可回溯），"
                "此处 main_net_yi 为板块净额加总，非全市场口径",
    }
    return {k: v for k, v in row.items() if v is not None and v != []}


def backfill_from_snapshots(snapshots_dir=None, path=BARS):
    """从已有 data/*.json 快照批量生成摘要（补齐历史）。

    只处理有 breadth 数据（非空）的实采快照 —— 回补快照的 breadth 是空的，
    它们没有任何宽度指标可提取。
    """
    import metrics
    d = snapshots_dir or os.path.join(HERE, "data")
    done = 0
    for fn in sorted(os.listdir(d)):
        if not (fn.endswith(".json") and fn[:4].isdigit()):
            continue
        p = os.path.join(d, fn)
        try:
            snap = json.load(open(p, encoding="utf-8"))
            if (snap.get("breadth") or {}).get("amount_total"):
                ana = metrics.compute(snap)
                if ana.get("empty_state"):
                    continue
                append(summarize(snap, ana), path)
                done += 1
            else:
                # 回补快照：走降级路径，只提取板块级可得部分
                row = summarize_board_only(snap)
                if row:
                    append(row, path)
                    done += 1
        except Exception:  # noqa: BLE001
            continue
    return done


if __name__ == "__main__":
    import sys
    sys.path.insert(0, HERE)
    n = backfill_from_snapshots()
    print(f"· 从快照生成摘要: {n} 天")
    st = stats()
    print(f"· bars.jsonl: {st['days']} 天, {st['bytes']/1024:.1f} KB, "
          f"单日 {st['kb_per_day']} KB")
    print(f"· 范围: {st.get('first')} ~ {st.get('last')}")
    print(f"· 十年推算: {st.get('projection_10y_mb')} MB")
    rows = load()
    if rows:
        print(f"\n· 最后一行样例:")
        print(json.dumps(rows[-1], ensure_ascii=False, indent=1)[:900])
