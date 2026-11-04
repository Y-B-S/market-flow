"""存储层：把每次快照落盘，积攒历史。只用标准库。

设计要点：
- data/YYYY-MM-DD.json  完整快照（含 limitations）
- data/flows.jsonl      轻量时序：每天一行，只存板块与大盘的资金流事实
  → 这样以后要做回测/看趋势，不用重爬历史
"""
from __future__ import annotations

import json
import os
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")

# 时序文件里保留的板块字段（轻量，避免 jsonl 膨胀）
BOARD_KEYS = ("code", "name", "chg", "amount", "mktcap_float",
              "main_net", "main_net_5d", "main_net_10d", "main_ratio")
STOCK_KEYS = ("code", "name", "chg", "amount", "main_net", "industry")
STOCK_TOP_N = 300


def _ensure_dir():
    os.makedirs(DATA, exist_ok=True)


def save(snap, allow_intraday=False):
    """保存完整快照 + 追加时序行。返回快照文件路径，或 None（拒绝保存）。

    ⚠️ 盘中禁止落盘（实测教训）：
    盘中运行时 fetch 返回的 as_of 仍是**上一交易日**（因为当日行情尚未定盘），
    但它携带的 breadth/成交额/净额是**当日半日数据**。
    直接保存会用 "11-03" 这个日期写入 11-04 的半日数据，
    把已归档的收盘数据覆盖成不完整值 —— 历史一旦被污染不可逆。

    实测：11-04 11:21 盘中保存，11-03 的成交额从 17151 亿被覆盖成 8709 亿。

    因此默认拒绝盘中保存；确需保存（如做盘中快照）须显式 allow_intraday=True，
    且会标注 intraday=True 以便与收盘数据区分。
    """
    ts = snap.get("trading_state")
    if ts == "pre_market_or_closed":
        # 盘前/非交易时段根本没有有效当日数据，无保存价值
        return None
    if not allow_intraday:
        # 判定盘中：用**当前时刻**判断，而不是靠 as_of 与今天的关系。
        # （as_of 已修正为本地日期，"as_of<今天"这条旧判据在盘中不再成立，
        #   会导致盘中数据被当成收盘数据落盘。）
        from datetime import datetime, timedelta, timezone
        cst = timezone(timedelta(hours=8))
        now = datetime.now(cst)
        hhmm = now.hour * 100 + now.minute
        # A 股连续竞价 09:30-11:30、13:00-15:00（含集合竞价与收盘集合竞价）
        in_session = (930 <= hhmm <= 1130) or (1300 <= hhmm <= 1500)
        amt = (snap.get("breadth") or {}).get("amount_total")
        # 盘中状态既看时钟，也看数据自报的状态（兼容新旧命名）
        _live_states = {"in_session", "trading"}
        if in_session and amt and (ts in _live_states or ts is None):
            return None

    _ensure_dir()
    as_of = snap["as_of"]
    path = os.path.join(DATA, f"{as_of}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)

    # 时序追加（按日期去重：同日重跑则覆盖该行）
    line = _timeline_row(snap)
    tl = os.path.join(DATA, "flows.jsonl")
    existing = []
    if os.path.exists(tl):
        with open(tl, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if r.get("as_of") != as_of:
                    existing.append(r)
    existing.append(line)
    existing.sort(key=lambda r: r.get("as_of") or "")
    tmp_tl = tl + ".tmp"
    with open(tmp_tl, "w", encoding="utf-8") as f:
        for r in existing:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp_tl, tl)
    return path


def _timeline_row(snap):
    """时序行要轻：板块全留（回测需要完整横截面），个股只留主力净额前 300。

    5927 只个股全存会让每天一行 ~900KB，一年 200MB+，没有意义：
    回测关注的是"钱流去哪了"，而不是每只小票。
    """
    b = snap.get("breadth") or {}

    def slim(rows):
        return [{k: r.get(k) for k in BOARD_KEYS} for r in (rows or [])]

    stocks = snap.get("stocks") or []
    ranked = sorted(stocks, key=lambda s: abs(s.get("main_net") or 0), reverse=True)
    top_stocks = [{k: s.get(k) for k in STOCK_KEYS} for s in ranked[:STOCK_TOP_N]]

    return {
        "as_of": snap.get("as_of"),
        "breadth": b,
        "margin": snap.get("margin"),
        "lhb": snap.get("lhb"),
        "limit_up_count": snap.get("limit_up_count"),
        "limit_down_count": snap.get("limit_down_count"),
        "indices": [{k: i.get(k) for k in ("code", "name", "price", "chg", "amount")}
                    for i in (snap.get("indices") or [])],
        "industries": slim(snap.get("industries")),
        "concepts": slim(snap.get("concepts")),
        "stocks_top": top_stocks,
        "stocks_total": len(stocks),
    }


def load(as_of=None):
    """读取某日快照；as_of=None 时取最新。"""
    if as_of:
        p = os.path.join(DATA, f"{as_of}.json")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    files = sorted(x for x in os.listdir(DATA)
                   if x.endswith(".json") and x[:4].isdigit()) if os.path.isdir(DATA) else []
    if not files:
        return None
    with open(os.path.join(DATA, files[-1]), encoding="utf-8") as f:
        return json.load(f)


def history(n=5, fields=None):
    """读取最近 n 天的时序行。fields 可选裁剪顶层键。"""
    tl = os.path.join(DATA, "flows.jsonl")
    if not os.path.exists(tl):
        return []
    rows = []
    with open(tl, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    rows.sort(key=lambda r: r.get("as_of") or "")
    rows = rows[-n:] if n else rows
    if fields:
        rows = [{k: r.get(k) for k in fields} for r in rows]
    return rows


def board_history(board_name, n=10):
    """某板块的跨日资金流序列。返回 [{as_of, main_net, main_net_5d, main_net_10d}]"""
    out = []
    for r in history(n):
        for b in (r.get("industries") or []) + (r.get("concepts") or []):
            if b.get("name") == board_name:
                out.append({"as_of": r.get("as_of"),
                            "main_net": b.get("main_net"),
                            "main_net_5d": b.get("main_net_5d"),
                            "main_net_10d": b.get("main_net_10d")})
                break
    return out


def available_dates():
    if not os.path.isdir(DATA):
        return []
    return sorted(x[:-5] for x in os.listdir(DATA)
                  if x.endswith(".json") and x[:4].isdigit())


if __name__ == "__main__":
    s = load()
    if s:
        p = save(s)
        print("saved ->", p)
        print("dates:", available_dates())
        print("history rows:", len(history(10)))
    else:
        print("no data yet, run: python3 fetch.py")
