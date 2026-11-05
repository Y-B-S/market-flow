"""取数层：东方财富公开接口。只用标准库，8 线程并发。

契约见 CONTRACT.md。所有金额单位=元，比率单位=%。
严禁编造：任何取不到的字段一律 None，不做 0 填充。
"""
from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}

PUSH2 = "https://push2.eastmoney.com/api/qt/clist/get"
ULIST = "https://push2.eastmoney.com/api/qt/ulist.np/get"
HIS = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
EX = "https://push2ex.eastmoney.com"
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 沪深京 A 股全市场
FS_ALL = "m:0+t:6,m:0+t:13,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
FS_INDUSTRY = "m:90+t:2"
FS_CONCEPT = "m:90+t:3"

INDICES = [
    ("1.000001", "上证指数"),
    ("0.399001", "深证成指"),
    ("0.399006", "创业板指"),
    ("1.000688", "科创50"),
    ("1.000300", "沪深300"),
]

GLOBAL_SECIDS = "100.HSI,100.NDX,100.DJIA,100.SPX,100.N225,101.GC00Y,102.CL00Y,133.USDCNH,100.UDI"


def num(v):
    """东财在停牌股上会返回字符串 '-'，必须安全转换。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s or s in ("-", "--", ""):
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def get(url, retries=3, timeout=15):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(0.4 * (2**i))
    raise RuntimeError(f"GET failed after {retries}: {url} :: {last}")


def clist_all(fs, fields, page_size=100, workers=8, max_pages=80):
    """东财 pz 硬上限 100，必须分页。先探总数再并发拉全。"""
    probe = get(f"{PUSH2}?pn=1&pz={page_size}&po=1&np=1&fltt=2&invt=2&fid=f3&fs={fs}&fields={fields}")
    data = probe.get("data") or {}
    total = data.get("total") or 0
    if total == 0:
        return []
    pages = min(max_pages, (total + page_size - 1) // page_size)

    def one(p):
        u = (f"{PUSH2}?pn={p}&pz={page_size}&po=1&np=1&fltt=2&invt=2"
             f"&fid=f3&fs={fs}&fields={fields}")
        d = (get(u).get("data") or {}).get("diff") or []
        return list(d.values()) if isinstance(d, dict) else list(d)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        chunks = list(ex.map(one, range(1, pages + 1)))
    out = [row for c in chunks for row in c]
    seen, uniq = set(), []
    for r in out:
        c = r.get("f12")
        if c and c not in seen:
            seen.add(c)
            uniq.append(r)
    return uniq


def _parse_klines(klines):
    """日线资金流：[日期, 主力净额, 小单, 中单, 大单, 超大单, 主力占比, 涨跌幅]"""
    rows = []
    for k in klines or []:
        p = k.split(",")
        if len(p) < 2:
            continue
        rows.append({
            "date": p[0],
            "main_net": num(p[1]),
            "small": num(p[2]) if len(p) > 2 else None,
            "medium": num(p[3]) if len(p) > 3 else None,
            "large": num(p[4]) if len(p) > 4 else None,
            "xlarge": num(p[5]) if len(p) > 5 else None,
            "chg": num(p[7]) if len(p) > 7 else None,
        })
    return rows


def fetch_index_flow(secid, lmt=15):
    u = (f"{HIS}?lmt={lmt}&klt=101&secid={secid}"
         f"&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58")
    try:
        d = get(u).get("data") or {}
        series = _parse_klines(d.get("klines"))
        if not series:
            return None
        last5 = [r["main_net"] for r in series[-5:] if r["main_net"] is not None]
        last10 = [r["main_net"] for r in series[-10:] if r["main_net"] is not None]
        return {
            "name": d.get("name"),
            "net_5d": sum(last5) if last5 else None,
            "net_10d": sum(last10) if last10 else None,
            "series": series,
        }
    except Exception:  # noqa: BLE001
        return None


def fetch_zt_pool(date_str):
    """涨停/跌停池。p 是价格*1000，需除以 1000。

    坑：sort 键对两个池不通用。涨停池用 fbt（首次封板时间）排序正常，
    跌停池用 fbt 会返回 tc=17 但 pool=[]（有总数、没明细），
    看起来像"跌停结构无法拆解"。跌停池必须改用 fund 或 zdp 排序。
    """
    res = {}
    # (kind, api, sort 候选列表) —— 依次尝试，取第一个真拿到明细的
    specs = (
        ("limit_up", "getTopicZTPool", ("fbt%3Aasc", "fund%3Adesc", "zdp%3Adesc")),
        ("limit_down", "getTopicDTPool", ("fund%3Aasc", "zdp%3Aasc", "fbt%3Aasc")),
    )
    for kind, api, sorts in specs:
        res[kind] = None
        for sort in sorts:
            u = (f"{EX}/{api}?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
                 f"&Pageindex=0&pagesize=200&sort={sort}&date={date_str}")
            try:
                d = (get(u).get("data") or {})
            except Exception:  # noqa: BLE001
                continue
            tc = d.get("tc")
            pool = d.get("pool") or []
            # 有总数却拿不到明细，说明这个 sort 键对该池无效，换下一个
            if tc and not pool:
                continue
            rows = []
            for r in pool:
                zttj = r.get("zttj") or {}
                rows.append({
                    "code": r.get("c"),
                    "name": r.get("n"),
                    "price": (num(r.get("p")) or 0) / 1000 or None,
                    "chg": num(r.get("zdp")),
                    "amount": num(r.get("amount")),
                    "limit_days": r.get("lbc"),
                    "industry": r.get("hybk"),
                    "seal_time": r.get("fbt"),
                    "stat_days": zttj.get("days"),
                    "stat_count": zttj.get("ct"),
                    "open_times": r.get("zbc"),
                })
            res[kind] = {"count": tc, "rows": rows,
                         "date": d.get("qdate"), "sort": sort}
            break
    return res


def fetch_margin():
    """两融是 T+1：今天拿到的最新一条通常是上一个交易日。"""
    u = (f"{DC}?reportName=RPTA_RZRQ_LSHJ&columns=ALL&pageSize=5"
         f"&sortColumns=dim_date&sortTypes=-1")
    try:
        d = (get(u).get("result") or {}).get("data") or []
        if not d:
            return None
        cur = d[0]
        prev = d[1] if len(d) > 1 else None
        cur_bal = num(cur.get("RZRQYE"))
        prev_bal = num(prev.get("RZRQYE")) if prev else None
        return {
            "date": (cur.get("DIM_DATE") or "")[:10],
            "margin_balance": cur_bal,
            "margin_balance_prev": prev_bal,
            "margin_change": (cur_bal - prev_bal) if (cur_bal is not None and prev_bal is not None) else None,
            "fin_balance": num(cur.get("RZYE")),
            "short_balance": num(cur.get("RQYE")),
            "fin_net_buy": num(cur.get("RZJME")),
        }
    except Exception:  # noqa: BLE001
        return None


def fetch_lhb(as_of):
    u = (f"{DC}?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL&pageSize=500"
         f"&sortColumns=TRADE_DATE&sortTypes=-1")
    try:
        d = (get(u).get("result") or {}).get("data") or []
        today = [r for r in d if str(r.get("TRADE_DATE") or "").startswith(as_of)]
        if not today:
            return None
        net = sum(num(r.get("BILLBOARD_NET_AMT")) or 0 for r in today)
        buy = sum(num(r.get("BILLBOARD_BUY_AMT")) or 0 for r in today)
        sell = sum(num(r.get("BILLBOARD_SELL_AMT")) or 0 for r in today)
        return {"trade_date": as_of, "count": len(today), "net_buy": net,
                "buy": buy, "sell": sell}
    except Exception:  # noqa: BLE001
        return None


def fetch_global():
    u = f"{ULIST}?fltt=2&secids={GLOBAL_SECIDS}&fields=f2,f3,f12,f14"
    try:
        d = (get(u).get("data") or {}).get("diff") or []
        rows = list(d.values()) if isinstance(d, dict) else list(d)
        return [{"code": r.get("f12"), "name": r.get("f14"),
                 "price": num(r.get("f2")), "chg": num(r.get("f3"))} for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _board(row, kind):
    return {
        "code": row.get("f12"),
        "name": row.get("f14"),
        "kind": kind,
        "chg": num(row.get("f3")),
        "amount": num(row.get("f6")),
        "turnover": num(row.get("f8")),
        "mktcap_total": num(row.get("f20")),
        "mktcap_float": num(row.get("f21")),
        "main_net": num(row.get("f62")),
        "main_net_5d": num(row.get("f164")),   # 已交叉验证
        "main_net_10d": num(row.get("f174")),  # 已交叉验证
        "main_ratio": num(row.get("f184")),
    }


def _ordered_indices(idx_map):
    """按 INDICES 的既定顺序输出指数，secid 的数字部分即代码。"""
    out = []
    for secid, _label in INDICES:
        code = secid.split(".")[-1]
        row = idx_map.get(code)
        if row:
            row = dict(row)
            row["secid"] = secid
            out.append(row)
    # 兜底：接口若返回了 INDICES 之外的指数，也一并带上（顺序在后）
    known = {r["code"] for r in out}
    for code, row in idx_map.items():
        if code not in known:
            out.append(dict(row))
    return out


def fetch_all(as_of=None, workers=8):
    """拉取全量快照。返回 CONTRACT.md 定义的 snapshot dict。"""
    t0 = time.time()
    limitations = []

    # 1) 指数 + 指数资金流（顺带确定交易日）
    idx_map = {}
    try:
        u = f"{ULIST}?fltt=2&secids={','.join(s for s, _ in INDICES)}&fields=f2,f3,f6,f12,f14"
        d = (get(u).get("data") or {}).get("diff") or []
        rows = list(d.values()) if isinstance(d, dict) else list(d)
        for r in rows:
            # 接口返回的 f12 是纯数字代码（"000001"），而 INDICES 里用的是
            # secid（"1.000001"）。必须以纯代码为键，否则永远匹配不上——
            # 这曾导致 indices 整个为空却只报一句"不可用"。
            code = r.get("f12")
            idx_map[code] = {
                "code": code, "name": r.get("f14"),
                "price": num(r.get("f2")), "chg": num(r.get("f3")),
                "amount": num(r.get("f6")),
            }
    except Exception as e:  # noqa: BLE001
        limitations.append(f"指数行情获取失败: {e}")

    index_flow = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(fetch_index_flow, sid): sid for sid, _ in INDICES}
        for f in futs:
            sid = futs[f]
            try:
                r = f.result()
                if r:
                    index_flow[sid] = r
            except Exception:  # noqa: BLE001
                pass

    # 交易日判定（关键：盘中不能让 as_of 落后，否则落盘会污染历史）
    #
    # 教训：早期直接用"指数资金流最后一根日线"的日期作 as_of。
    # 但盘中运行时，日线接口的最后一根**仍是昨日收盘**，而实际采集的
    # breadth/成交额是**今天的半日数据**。于是 as_of=11-03 却装进 11-04 的行情，
    # 保存后把已归档的 11-03 收盘数据覆盖成了半日值（实测成交额 17151亿→8709亿）。
    #
    # 正确做法：以**本地日期**为主，再看今日是否有有效行情来判断状态。
    now_local = datetime.now(CST)
    today_local = now_local.strftime("%Y-%m-%d")
    inferred = None
    for sid in ("1.000001", "0.399001"):
        fl = index_flow.get(sid)
        if fl and fl.get("series"):
            inferred = fl["series"][-1]["date"]
            break
    if as_of is None:
        # 本地日期与日线推断日相同 → 收盘后/盘后，用该日；
        # 本地日期更晚 且 今日有有效行情 → 盘中，as_of 也应用**今天**，
        #   否则下游无法区分"今天半日数据"与"昨天全天数据"。
        as_of = today_local
        if inferred and inferred < today_local:
            limitations.append(
                f"日线接口最新交易日为 {inferred}，本地日期为 {today_local}；"
                f"若当前处于交易时段，本快照为**当日盘中数据**，非完整交易日")

    # 2) 全市场个股 + 行业 + 概念（并发）
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_stocks = ex.submit(clist_all, FS_ALL,
                             "f12,f14,f2,f3,f6,f8,f20,f21,f62,f100", 100, 8)
        f_ind = ex.submit(clist_all, FS_INDUSTRY,
                          "f12,f14,f3,f6,f8,f20,f21,f62,f164,f174,f184,f267", 100, 6)
        f_con = ex.submit(clist_all, FS_CONCEPT,
                          "f12,f14,f3,f6,f8,f20,f21,f62,f164,f174,f184", 100, 6)
        raw_stocks = f_stocks.result()
        raw_ind = f_ind.result()
        raw_con = f_con.result()

    stocks = []
    for r in raw_stocks:
        stocks.append({
            "code": r.get("f12"), "name": r.get("f14"),
            "price": num(r.get("f2")), "chg": num(r.get("f3")),
            "amount": num(r.get("f6")), "turnover": num(r.get("f8")),
            "mktcap_total": num(r.get("f20")), "mktcap_float": num(r.get("f21")),
            "main_net": num(r.get("f62")), "industry": r.get("f100"),
        })

    industries = [_board(r, "industry") for r in raw_ind]
    concepts = [_board(r, "concept") for r in raw_con]

    # 3) 涨停/跌停池、两融、龙虎榜、外部市场（先取池子，涨跌停数以交易所池为准）
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_zt = ex.submit(fetch_zt_pool, as_of.replace("-", ""))
        f_mg = ex.submit(fetch_margin)
        f_lhb = ex.submit(fetch_lhb, as_of)
        f_gl = ex.submit(fetch_global)
        zt = f_zt.result()
        margin = f_mg.result()
        lhb = f_lhb.result()
        glob = f_gl.result()

    limit_up_pool = (zt.get("limit_up") or {}).get("rows") or []
    limit_down_pool = (zt.get("limit_down") or {}).get("rows") or []
    pool_up = (zt.get("limit_up") or {}).get("count")
    pool_dn = (zt.get("limit_down") or {}).get("count")

    # 4) 涨跌家数（全部自己算，不用别人给的现成指标）
    #    注意：涨跌停数一律以交易所池子为准。简单用 ±9.8% 阈值会严重高估，
    #    因为创业板/科创板/北交所涨跌幅限制是 20%/30%，不是 10%。
    chgs = [s["chg"] for s in stocks if s["chg"] is not None]
    amount_total = sum(s["amount"] for s in stocks if s["amount"] is not None)
    main_net_total = sum(s["main_net"] for s in stocks if s["main_net"] is not None)
    naive_up = sum(1 for c in chgs if c >= 9.8)
    naive_dn = sum(1 for c in chgs if c <= -9.8)
    breadth = {
        "count": len(stocks),
        "valid": len(chgs),
        "suspended": len(stocks) - len(chgs),
        "up": sum(1 for c in chgs if c > 0),
        "down": sum(1 for c in chgs if c < 0),
        "flat": sum(1 for c in chgs if c == 0),
        "up5pct": sum(1 for c in chgs if c > 5),
        "down5pct": sum(1 for c in chgs if c < -5),
        # 权威值来自交易所涨停板/跌停板池；池子失败时才退回粗略阈值
        "limit_up": pool_up if pool_up is not None else naive_up,
        "limit_down": pool_dn if pool_dn is not None else naive_dn,
        "limit_up_naive": naive_up,
        "limit_down_naive": naive_dn,
        "amount_total": amount_total or None,
        "main_net_total": main_net_total if chgs else None,
    }
    if pool_up is not None and abs(pool_up - naive_up) > 10:
        limitations.append(
            f"涨跌停以交易所池为准（涨停{pool_up}家/跌停{pool_dn}家）；"
            f"±9.8%阈值粗略统计为{naive_up}/{naive_dn}家，差异源于创业板科创板20%限制")

    # 5) 自洽性检查：个股成交额合计 vs 指数成交额合计
    idx_amount = sum(v["amount"] for v in idx_map.values()
                     if v["amount"] is not None and v["code"] in ("000001", "399001"))
    consistency = None
    if idx_amount and breadth["amount_total"]:
        consistency = {
            "index_amount_sum": idx_amount,
            "stock_amount_sum": breadth["amount_total"],
            "diff_pct": abs(idx_amount - breadth["amount_total"]) / idx_amount * 100,
        }
        if consistency["diff_pct"] > 15:
            limitations.append(
                f"成交额交叉校验偏差 {consistency['diff_pct']:.1f}%，两市口径可能不一致")

    if not industries:
        limitations.append("行业板块数据为空")
    if not concepts:
        limitations.append("概念板块数据为空")

    # ── 盘前/非交易时段空盘检测 ──────────────────────────────────────────
    # 实测：11-04 08:48（盘前）全市场 5927 只个股 f62 全部返回 "-"、f3 全为 0，
    # 成交额与主力净额为 None。若不拦截，下游会把它当成"真实数据"算出
    # 涨0跌0、上证0.00%，看起来像暴跌，实际是还没开盘。
    # 这是最危险的一类静默错误：数字都"合法"，但结论完全错。
    # 交易状态判定（初值按当前时钟判断，随后按数据完整性修正）
    #
    # 教训：早期只区分"有数据(trading)"与"无数据(pre_market_or_closed)"。
    # 实测 17:46 收盘后跑，数据完整却仍标 trading —— 状态名不准确会误导下游
    # （落盘保护要靠它判断是否盘中，名字错了保护就失效）。
    _now_hm = datetime.now(CST).hour * 100 + datetime.now(CST).minute
    # ⚠️ 判定"是否已定盘"必须覆盖**午间休市**：11:30-13:00 上午行情已固定
    # 但下午还有交易，全天数据未完成，绝不能标成 closed_settled。
    # 实测 bug：11:31 落在 11:30 之后被判为 closed_settled，
    # 导致落盘保护失效，把上午半日数据（14766亿）当成收盘值写进了历史。
    _in_session = ((930 <= _now_hm <= 1130) or (1300 <= _now_hm <= 1500))
    _noon_break = 1130 < _now_hm < 1300
    _before_open = _now_hm < 930
    _after_close_settled = _now_hm >= 1500
    if _in_session or _noon_break or _before_open:
        # 盘中 / 午休 / 盘前：当日未定盘
        trading_state = "in_session" if (_in_session or _noon_break) else "pre_market_or_closed"
    else:
        # 15:00 之后（含 15:00-15:15 未完全定盘、以及之后）
        trading_state = "closed_settled"
    chg_valid = sum(1 for c in chgs if c != 0)
    # 判据要用"个股总数"而非 valid：盘前 f3 为 "-"，num() 转成 None 后
    # valid 会变成 0，光看 valid 判不出来。真正信号是"有效涨跌幅几乎为零"。
    total_stocks = breadth["count"] or 0
    if total_stocks >= 100 and breadth["valid"] <= total_stocks * 0.05:
        trading_state = "pre_market_or_closed"
        limitations.append(
            f"【空盘状态】{as_of} 的 {total_stocks} 只个股中仅 {breadth['valid']} 只"
            f"有有效涨跌幅数据，当日成交额与主力净额不可用——这是盘前或非交易时段，"
            f"本快照不代表任何真实交易日行情。请勿据此得出任何结论。")
    if breadth["amount_total"] is None and total_stocks > 0:
        trading_state = "pre_market_or_closed"
        if not any("空盘状态" in x for x in limitations):
            limitations.append("【空盘状态】当日成交额缺失，可能是盘前或非交易时段")

    snap_trading_state = trading_state

    # 6) 前一交易日成交额：从本地时序里取（fetch 不依赖 store，这里只读时序文件）
    #    用它是为了算成交额环比。取不到就 None，绝不用均值或 0 伪造。
    prev_amount_total = None
    try:
        import json as _json
        import os as _os
        tl = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                           "data", "flows.jsonl")
        if _os.path.exists(tl):
            prevs = []
            with open(tl, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        r = _json.loads(ln)
                    except _json.JSONDecodeError:
                        continue
                    d = r.get("as_of")
                    if d and d < as_of:
                        prevs.append((d, (r.get("breadth") or {}).get("amount_total")))
            prevs = [p for p in prevs if p[1]]
            if prevs:
                prevs.sort()
                prev_amount_total = prevs[-1][1]
    except Exception:  # noqa: BLE001
        prev_amount_total = None
    if prev_amount_total is None:
        limitations.append("本地无前一交易日快照，成交额环比首次运行不可用（累积历史后自动可用）")

    # ── 数据不变量校验 ────────────────────────────────────────────
    # 这些是"违反必然导致结论错误"的硬逻辑约束，不是"看起来奇怪"的软判断。
    # 变异测试发现：此前注入"涨停数=个股总数"这类不可能值，系统毫无反应。
    inv_violations = []
    _cnt = breadth["count"] or 0
    _lu = breadth["limit_up"] or 0
    _ld = breadth["limit_down"] or 0
    if _lu > _cnt:
        inv_violations.append(f"涨停数({_lu}) 超过个股总数({_cnt})")
    if _ld > _cnt:
        inv_violations.append(f"跌停数({_ld}) 超过个股总数({_cnt})")
    _sum3 = (breadth["up"] or 0) + (breadth["down"] or 0) + (breadth["flat"] or 0)
    if _sum3 != (breadth["valid"] or 0):
        inv_violations.append(
            f"涨+跌+平({_sum3}) 不等于有效个股数({breadth['valid']})")
    if _cnt and not (1000 <= _cnt <= 8000):
        inv_violations.append(f"个股总数({_cnt}) 不在合理区间[1000,8000]")
    # 同一事实的重复字段必须一致，否则下游取哪个都可能拿到陈旧值
    if (zt.get("limit_up") or {}).get("count") != breadth["limit_up"]:
        inv_violations.append(
            f"涨停数在 breadth({breadth['limit_up']}) 与涨停池"
            f"({(zt.get('limit_up') or {}).get('count')}) 不一致")
    for v in inv_violations:
        limitations.append(f"【不变量违反】{v}")

    snap = {
        "as_of": as_of,
        "invariant_violations": inv_violations,
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "source": "eastmoney-public-api",
        "indices": _ordered_indices(idx_map),
        "breadth": breadth,
        "trading_state": snap_trading_state,
        "prev_amount_total": prev_amount_total,
        "industries": industries,
        "concepts": concepts,
        "stocks": stocks,
        "limit_up_pool": limit_up_pool,
        "limit_down_pool": limit_down_pool,
        "limit_up_count": (zt.get("limit_up") or {}).get("count"),
        "limit_down_count": (zt.get("limit_down") or {}).get("count"),
        "margin": margin,
        "lhb": lhb,
        "index_flow": index_flow,
        "global": glob,
        "consistency": consistency,
        "limitations": limitations,
        "elapsed_sec": round(time.time() - t0, 2),
    }
    return snap


if __name__ == "__main__":
    s = fetch_all()
    print(f"as_of={s['as_of']} 耗时{s['elapsed_sec']}s")
    b = s["breadth"]
    print(f"个股{b['count']} 涨{b['up']} 跌{b['down']} 涨停{b['limit_up']} 跌停{b['limit_down']}")
    print(f"成交额{b['amount_total']/1e8:.0f}亿 主力净额{b['main_net_total']/1e8:.0f}亿")
    print(f"行业{len(s['industries'])} 概念{len(s['concepts'])} 涨停池{len(s['limit_up_pool'])}")
    print("limitations:", s["limitations"])
