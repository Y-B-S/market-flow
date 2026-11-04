"""历史回补：漏跑了怎么办？

实测结论：
  · **板块级**可以补 —— 资金流日线接口可回溯 120 个交易日，
    每个板块的"日期/主力净额/5日/10日/收盘价/涨跌幅"都在里面。
    496 个板块并发取完约 19 秒。
  · **个股级截面补不了** —— 接口只给"单只股票的序列"，
    不给"某一天全市场 5900 只股票的截面"。
    所以涨跌家数、个股净流入榜、涨停池这些**当日宽度指标永久丢失**。

本脚本把可补的部分补进 store，并把不可补的部分明确标注出来，
避免以后回测时误以为历史是完整的。

    python3 backfill.py            # 回补最近 60 个交易日
    python3 backfill.py --days 120
    python3 backfill.py --dry-run  # 只看能补什么，不写盘
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import fetch   # noqa: E402
import store   # noqa: E402

FLOW_API = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
            "?lmt={lmt}&klt=101&secid=90.{code}"
            "&fields1=f1,f2,f3,f7"
            "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63")


def board_daily(code, lmt):
    """取单板块日线：日期/主力/收盘/涨跌幅。"""
    for attempt in range(3):
        try:
            d = fetch.get(FLOW_API.format(lmt=lmt, code=code), retries=1, timeout=25)
            ks = (d.get("data") or {}).get("klines") or []
            out = []
            for k in ks:
                p = k.split(",")
                if len(p) <= 12:
                    continue
                out.append({
                    "date": p[0],
                    "main_net": fetch.num(p[1]),
                    "close": fetch.num(p[11]),
                    "chg": fetch.num(p[12]),
                })
            return code, out
        except Exception:  # noqa: BLE001
            if attempt < 2:
                time.sleep(1.0)
    return code, []


def main(argv=None):
    ap = argparse.ArgumentParser(description="历史回补（仅板块级）")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    print("=" * 74)
    print("历史回补：能补什么、不能补什么")
    print("=" * 74)
    print("""
  ✓ 可补：板块级资金流（日期 / 主力净额 / 收盘价 / 涨跌幅）
  ✗ 不可补：当日全市场截面 —— 涨跌家数、个股净流入榜、涨停池、两融当日快照
     原因：接口只提供"单只标的的序列"，不提供"某日全市场截面快照"。
     这些指标一旦当天没跑，就**永久缺失**。
""")

    print(f"· 取板块清单…")
    boards = fetch.clist_all(fetch.FS_INDUSTRY, "f12,f14,f6",
                            page_size=100, workers=args.workers)
    codes = [r["f12"] for r in boards if r.get("f12")]
    names = {r["f12"]: r.get("f14") for r in boards}
    print(f"  {len(codes)} 个行业板块")

    t0 = time.time()
    series = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {c: ex.submit(board_daily, c, args.days) for c in codes}
        for c, f in futs.items():
            code, rows = f.result()
            if rows:
                series[code] = rows
    print(f"· 取回 {len(series)}/{len(codes)} 个板块，耗时 {time.time()-t0:.1f}s")

    if not series:
        print("✗ 无数据可补")
        return 1

    # 汇总出所有交易日
    dates = sorted({r["date"] for rows in series.values() for r in rows})
    print(f"· 覆盖 {len(dates)} 个交易日：{dates[0]} ~ {dates[-1]}")

    existing = set(store.available_dates())
    fillable = [d for d in dates if d not in existing]
    print(f"· 已有快照 {len(existing)} 天，可补 {len(fillable)} 天")

    if args.dry_run:
        print("\n--dry-run：不写盘。可补日期：")
        print("  " + "、".join(fillable[:20]) + (" …" if len(fillable) > 20 else ""))
        return 0

    # 按日期组装"板块级部分快照"
    by_date = {}
    for code, rows in series.items():
        for r in rows:
            by_date.setdefault(r["date"], []).append({
                "code": code,
                "name": names.get(code),
                "kind": "industry",
                "chg": r["chg"],
                "main_net": r["main_net"],
                # 5日/10日需要自己滚算（接口的 f164/f174 只反映"当下"窗口，不适用历史）
                "close": r["close"],
            })

    # 为每个日期补算滚动 5/10 日净额
    for code, rows in series.items():
        for i, r in enumerate(rows):
            d5 = sum(x["main_net"] for x in rows[max(0, i - 4):i + 1]
                     if x["main_net"] is not None)
            d10 = sum(x["main_net"] for x in rows[max(0, i - 9):i + 1]
                      if x["main_net"] is not None)
            for rec in by_date.get(r["date"], []):
                if rec["code"] == code:
                    rec["main_net_5d"] = d5
                    rec["main_net_10d"] = d10
                    break

    written = 0
    for d in fillable:
        path = os.path.join(store.DATA, f"{d}.json")
        if os.path.exists(path):
            continue
        os.makedirs(store.DATA, exist_ok=True)
        snap = {
            "as_of": d,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": "backfill:eastmoney-board-daily",
            "backfilled": True,
            "trading_state": "settled",
            "indices": [],
            "breadth": {},
            "industries": by_date.get(d, []),
            "concepts": [],
            "stocks": [],
            "limit_up_pool": [],
            "limit_down_pool": [],
            "margin": None,
            "lhb": None,
            "index_flow": {},
            "global": [],
            "limitations": [
                "【历史回补】本快照由板块资金流日线重建，仅含板块级数据；",
                "【历史回补】当日全市场宽度指标（涨跌家数/个股榜单/涨停池/两融）"
                "在接口中不可回溯，**永久缺失**，不可用于回测该类指标。",
                "【历史回补】5日/10日净额由日线自行滚算，与当日盘中接口值可能有细微差异。",
            ],
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        written += 1

    print(f"\n· 已回补 {written} 天到 data/")
    print(f"· 现有快照共 {len(store.available_dates())} 天")
    print("""
注意：回补出的快照 marked backfilled=True，且 limitations 明确写了缺什么。
用它们做板块级回测是**可以的**；做含涨跌家数的回测是**不可以的**。
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
