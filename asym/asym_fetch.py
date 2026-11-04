"""信息不对称面板 · 取数层。

只做一件事：把"知情者的行为痕迹"从公开披露里取出来，并保证口径正确。

契约见 CONTRACT.md。三条铁律：
1. 取不到 = None，绝不填 0 冒充
2. 每个数据集必须记录真实口径与滞后天数
3. 调研数据必须按「场次」聚合，`NUM` 字段禁用（详见 CONTRACT.md 陷阱 1）
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://data.eastmoney.com/",
}
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s or s in ("-", "--"):
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def txt(v):
    """文本字段规整：None/空 -> None"""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def day(v):
    """'2026-11-03 00:00:00' -> '2026-11-03'"""
    s = txt(v)
    return s[:10] if s else None


def get(url, retries=3, timeout=20):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(0.4 * (2 ** i))
    raise RuntimeError(f"GET failed: {url[:120]} :: {last}")


def dc_fetch(report_name, columns="ALL", page_size=200, filter_expr=None,
             sort_columns=None, sort_types="-1", max_pages=20):
    """东财数据中心通用分页取数。

    filter_expr 是**已解码**的表达式（如 (NOTICE_DATE>='2026-11-01')），
    内部会做 URL 编码——实测直接拼中文会报 UnicodeEncodeError，
    这是必须封在取数层的坑，不能让调用方各自处理。
    """
    out = []
    page = 1
    total_pages = 1
    while page <= min(max_pages, total_pages):
        q = {
            "reportName": report_name,
            "columns": columns,
            "pageSize": str(page_size),
            "pageNumber": str(page),
            "sortTypes": sort_types,
        }
        if filter_expr:
            q["filter"] = filter_expr        # quote 时统一编码
        if sort_columns:
            q["sortColumns"] = sort_columns
        url = DC + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)
        try:
            d = get(url)
        except Exception:  # noqa: BLE001
            break
        res = d.get("result")
        if not res or not res.get("data"):
            break
        out.extend(res["data"])
        total_pages = int(res.get("pages") or 1)
        if page >= total_pages:
            break
        page += 1
    return out


# ── 机构调研 ────────────────────────────────────────────────────────────

def fetch_surveys(since_date, max_pages=30):
    """机构调研。返回按**场次**聚合后的记录。

    关键口径（实测确认，见 CONTRACT.md）：
      - 一行 = 一家机构参与一场调研
      - (股票, 接待起始日, 接待方式) 定义一场
      - 参与机构数 = 组内行数
      - `NUM` 是机构序号，**禁用**
    """
    rows = dc_fetch(
        "RPT_ORG_SURVEYNEW",
        columns=("SECURITY_CODE,SECURITY_NAME_ABBR,NOTICE_DATE,"
                 "RECEIVE_START_DATE,RECEIVE_END_DATE,RECEIVE_OBJECT_TYPE,"
                 "RECEIVE_OBJECT,RECEIVE_WAY_EXPLAIN,RECEIVE_PLACE,"
                 "RECEPTIONIST,RECEIVE_WAY_EXPLAIN"),
        page_size=200,
        filter_expr=f"(NOTICE_DATE>='{since_date}')",
        sort_columns="NOTICE_DATE",
        max_pages=max_pages,
    )

    # 按场次聚合
    groups = {}
    for r in rows:
        code = txt(r.get("SECURITY_CODE"))
        start = day(r.get("RECEIVE_START_DATE"))
        way = txt(r.get("RECEIVE_WAY_EXPLAIN"))
        # ⚠️ 场次键**只能**用 (股票, 接待起始日, 接待方式)。
        # 曾把 RECEIVE_OBJECT 也放进键 —— 但它是**参与机构名**
        # （如"上海非马投资管理有限公司"），结果每家机构自成一组，
        # 普门科技一场 101 家的调研被拆成 100 家 + 1 家两个假场次。
        # 场次标识里绝不能出现任何"个体机构"维度的字段。
        k = (code, start, way)
        g = groups.get(k)
        if g is None:
            groups[k] = g = {
                "code": code,
                "name": txt(r.get("SECURITY_NAME_ABBR")),
                "notice_date": day(r.get("NOTICE_DATE")),
                "receive_start": start,
                "receive_end": day(r.get("RECEIVE_END_DATE")),
                "way": way,
                "object_type": txt(r.get("RECEIVE_OBJECT_TYPE")),
                "place": txt(r.get("RECEIVE_PLACE")),
                "receptionist": txt(r.get("RECEPTIONIST")),
                "raw_rows": 0,          # 原始行数 = 参与机构数
            }
        g["raw_rows"] += 1
        # 同一场次里接待人可能只在部分行有值，取第一个非空的
        if not g["receptionist"] and txt(r.get("RECEPTIONIST")):
            g["receptionist"] = txt(r.get("RECEPTIONIST"))
        if not g["place"] and txt(r.get("RECEIVE_PLACE")):
            g["place"] = txt(r.get("RECEIVE_PLACE"))

    sessions = list(groups.values())

    # ── 粒度判定 ─────────────────────────────────────────────────────
    # 数据源混有两种粒度，混用会重复计数：
    #   ① 月度窗口：receive_start=月初、receive_end=月末（跨度~30天），
    #      如华勤技术 2026-10-01~10-31 一条记录 277 行 = 整月累计接待
    #   ② 单场：同一天完成的一次调研
    # 实测华勤技术 1000 条里只有 2 条是真单场、996 条是月度窗口/未知。
    #
    # 关键判断：`receive_end` 缺失 ≠ 粒度未知。实测 40 个未知组里 35 个是
    # 170/127/107 家这样的多机构大组——「同 (股票,起日,方式) 下聚集多家机构」
    # 本身就证明它是一场调研。若把未知全部排除，会丢掉约一半数据，
    # 且丢掉的正是机构参与最多的高价值场次。
    def _granularity(rec):
        sd = rec.get("receive_start")
        ed = rec.get("receive_end")
        if sd and ed:
            try:
                from datetime import datetime as _dt
                a = _dt.strptime(sd, "%Y-%m-%d")
                b = _dt.strptime(ed, "%Y-%m-%d")
                span = (b - a).days
            except ValueError:
                span = None
            if span is None:
                return "unknown"
            if span <= 7:
                return "single"
            if span >= 25:
                return "monthly"
            return "multi"
        # receive_end 缺失：靠「是否多机构聚集」判定
        return "single" if rec["raw_rows"] >= 2 else "unknown"

    for s in sessions:
        s["inst_count"] = s["raw_rows"]      # 参与机构数 = 行数
        s["granularity"] = _granularity(s)

    sessions.sort(key=lambda s: (s["receive_start"] or "", -(s["inst_count"] or 0)))

    gcount = {}
    for s in sessions:
        gcount[s["granularity"]] = gcount.get(s["granularity"], 0) + 1

    return {
        "raw_row_count": len(rows),
        "session_count": len(sessions),
        "granularity_counts": gcount,
        "sessions": sessions,
    }


# ── 高管持股变动（内部人交易）────────────────────────────────────────────

def fetch_insider(since_date, max_pages=20):
    """高管持股变动。

    `CHANGE_SHARES` 符号方向必须实测核对，不能假设：
      这里保留原始值与符号，并额外给出 change_dir（由 CHANGE_SHARES 符号判定），
      若字段全为正值且无法判定方向，则 dir 标 None，交由上层降级。
    """
    rows = dc_fetch(
        "RPT_EXECUTIVE_HOLD_DETAILS",
        columns=("SECURITY_CODE,SECURITY_NAME,CHANGE_DATE,PERSON_NAME,"
                 "CHANGE_SHARES,AVERAGE_PRICE,CHANGE_AMOUNT,CHANGE_REASON,"
                 "CHANGE_RATIO,DERIVE_SECURITY_CODE"),
        page_size=200,
        filter_expr=f"(CHANGE_DATE>='{since_date}')",
        sort_columns="CHANGE_DATE",
        max_pages=max_pages,
    )
    recs = []
    for r in rows:
        shares = num(r.get("CHANGE_SHARES"))
        amount = num(r.get("CHANGE_AMOUNT"))
        recs.append({
            "code": txt(r.get("SECURITY_CODE")),
            "name": txt(r.get("SECURITY_NAME")),
            "date": day(r.get("CHANGE_DATE")),
            "person": txt(r.get("PERSON_NAME")),
            "shares": shares,
            "price": num(r.get("AVERAGE_PRICE")),
            "amount": amount,
            "reason": txt(r.get("CHANGE_REASON")),
            "ratio": num(r.get("CHANGE_RATIO")),
            # 方向：股数为负 -> 减持；为正 -> 增持；缺失 -> unknown
            "dir": (None if shares is None
                    else ("减持" if shares < 0 else ("增持" if shares > 0 else "不变"))),
        })
    return {"row_count": len(recs), "records": recs}


# ── 股东户数（筹码集中度）──────────────────────────────────────────────

def fetch_holders(max_pages=10):
    """股东户数。季报级披露，天然滞后。

    实测看到 278646.15% 这类极端值（新股上市户数暴增），
    比率必须由上层做截断，这里只提供原始值 + 披露日期。
    """
    rows = dc_fetch(
        "RPT_HOLDERNUM_DET",
        columns=("SECURITY_CODE,SECURITY_NAME_ABBR,HOLDER_NUM,PRE_HOLDER_NUM,"
                 "HOLDER_NUM_CHANGE,HOLDER_NUM_RATIO,END_DATE,PRE_END_DATE,"
                 "AVG_MARKET_CAP,TOTAL_MARKET_CAP"),
        page_size=200,
        sort_columns="END_DATE",
        max_pages=max_pages,
    )
    recs = []
    for r in rows:
        recs.append({
            "code": txt(r.get("SECURITY_CODE")),
            "name": txt(r.get("SECURITY_NAME_ABBR")),
            "end_date": day(r.get("END_DATE")),
            "holder_num": num(r.get("HOLDER_NUM")),
            "pre_holder_num": num(r.get("PRE_HOLDER_NUM")),
            "change": num(r.get("HOLDER_NUM_CHANGE")),
            "ratio": num(r.get("HOLDER_NUM_RATIO")),
            "avg_market_cap": num(r.get("AVG_MARKET_CAP")),
        })
    return {"row_count": len(recs), "records": recs}


# ── 龙虎榜营业部（席位活跃度）───────────────────────────────────────────

def fetch_seats(max_pages=5):
    """龙虎榜活跃营业部。

    ⚠️ 实测三个严重问题，必须在取数层就处理掉，否则上层一定误读：
    1. `ONLIST_DATE` 混杂 2007~2026 年：这是**营业部历史活跃度累计**，
       不是"当前仍在活跃"。直接当今日信号展示会严重误导。
    2. 同一 `OPERATEDEPT_CODE` 存在**旧名与新名**两行
       （"国信证券有限责任公司深圳红岭中路证券营业部" vs
        "国信证券有限公司深圳红岭中路证券营业部"），数值完全相同。
    3. 500 行里只有 21~33 个唯一席位 → 大量重复膨胀。

    处理：按 `OPERATEDEPT_CODE` 去重，保留信息最全/名称最长的那行，
    并把数据定位改为"历史累计活跃度"，不再声称是实时信号。
    """
    rows = dc_fetch(
        "RPT_OPERATEDEPT_ACTIVE",
        columns="ALL",
        page_size=100,
        sort_columns="BUYER_APPEAR_NUM",
        max_pages=max_pages,
    )

    # 按席位代码去重
    by_code = {}
    for r in rows:
        code = txt(r.get("OPERATEDEPT_CODE"))
        if code is None:
            continue
        rec = {
            "dept_name": txt(r.get("OPERATEDEPT_NAME")),
            "dept_code": code,
            "buyer_num": num(r.get("BUYER_APPEAR_NUM")),
            "seller_num": num(r.get("SELLER_APPEAR_NUM")),
            "buy_amt": num(r.get("TOTAL_BUYAMT")),
            "sell_amt": num(r.get("TOTAL_SELLAMT")),
            "net_amt": num(r.get("TOTAL_NETAMT")),
            "date": day(r.get("ONLIST_DATE")),
        }
        prev = by_code.get(code)
        if prev is None:
            by_code[code] = rec
        else:
            # 同名不同写法时，保留更长的名称（通常更完整），数值取已有非空
            if len(rec["dept_name"] or "") > len(prev["dept_name"] or ""):
                prev["dept_name"] = rec["dept_name"]
            for k in ("buyer_num", "seller_num", "buy_amt", "sell_amt", "net_amt", "date"):
                if prev.get(k) is None and rec.get(k) is not None:
                    prev[k] = rec[k]

    recs = list(by_code.values())
    recs.sort(key=lambda x: -(x["buyer_num"] or 0))

    # 日期跨度如实上报，提醒上层这不是"今日"数据
    dates = [x["date"] for x in recs if x["date"]]
    span = (min(dates), max(dates)) if dates else (None, None)

    return {
        "row_count": len(recs),
        "raw_row_count": len(rows),
        "dedup_by": "OPERATEDEPT_CODE",
        "date_span": {"min": span[0], "max": span[1]},
        "positioning": "历史累计活跃度（非实时信号）",
        "records": recs,
    }


# ── 业绩预告（管理层提前给方向）────────────────────────────────────────

def fetch_forecast(since_date, max_pages=10):
    rows = dc_fetch(
        "RPT_PUBLIC_OP_NEWPREDICT",
        columns=("SECURITY_CODE,SECURITY_NAME_ABBR,NOTICE_DATE,REPORT_DATE,"
                 "PREDICT_FINANCE,PREDICT_AMT_LOWER,PREDICT_AMT_UPPER,"
                 "PREDICT_TYPE,ADD_AMP_LOWER,ADD_AMP_UPPER"),
        page_size=200,
        filter_expr=f"(NOTICE_DATE>='{since_date}')",
        sort_columns="NOTICE_DATE",
        max_pages=max_pages,
    )
    recs = []
    for r in rows:
        recs.append({
            "code": txt(r.get("SECURITY_CODE")),
            "name": txt(r.get("SECURITY_NAME_ABBR")),
            "notice_date": day(r.get("NOTICE_DATE")),
            "report_date": day(r.get("REPORT_DATE")),
            "predict": txt(r.get("PREDICT_FINANCE")),
            "type": txt(r.get("PREDICT_TYPE")),
            "amt_lower": num(r.get("PREDICT_AMT_LOWER")),
            "amt_upper": num(r.get("PREDICT_AMT_UPPER")),
        })
    return {"row_count": len(recs), "records": recs}


# ── 主入口 ──────────────────────────────────────────────────────────────

def fetch_all(as_of=None, lookback_days=30, lookback_survey_days=90):
    """并行拉取全部数据集。任一数据集失败 -> None + limitations，不影响其他。"""
    t0 = time.time()
    limitations = []

    if as_of is None:
        as_of = datetime.now(CST).strftime("%Y-%m-%d")
    d_ins = (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    d_sur = (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=lookback_survey_days)).strftime("%Y-%m-%d")

    jobs = {
        "surveys": lambda: fetch_surveys(d_sur),
        "insider": lambda: fetch_insider(d_ins),
        "holders": lambda: fetch_holders(),
        "seats": lambda: fetch_seats(),
        "forecast": lambda: fetch_forecast(d_ins),
    }
    out = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {k: ex.submit(fn) for k, fn in jobs.items()}
        for k, f in futs.items():
            try:
                out[k] = f.result()
            except Exception as e:  # noqa: BLE001
                out[k] = None
                limitations.append(f"{k} 取数失败: {type(e).__name__}")

    # 数据集可用性与口径声明
    meta = {}
    sv = out.get("surveys")
    if sv and sv["sessions"]:
        ndates = sorted({s["notice_date"] for s in sv["sessions"] if s["notice_date"]})
        meta["surveys"] = {
            "raw_rows": sv["raw_row_count"],
            "sessions": sv["session_count"],
            "granularity": sv.get("granularity_counts"),
            "notice_days_covered": len(ndates),
            "notice_date_range": [ndates[0], ndates[-1]] if ndates else None,
            "note": (f"原始 {sv['raw_row_count']} 行按「股票×接待起始日×接待方式」"
                     f"聚合为 {sv['session_count']} 场；参与机构数=组内行数；"
                     f"NUM 为机构序号，未使用；粒度分 single/multi/monthly/unknown"),
        }
        if len(ndates) <= 5:
            limitations.append(
                f"调研数据仅覆盖 {len(ndates)} 个公告日（{ndates[0]}~{ndates[-1]}），"
                f"不足以构成 20 日对比；5v20 对比已如实降级")
    elif sv:
        limitations.append("调研数据为空")

    ins = out.get("insider")
    if ins and ins["records"]:
        dirs = [r["dir"] for r in ins["records"]]
        unknown = sum(1 for d in dirs if d is None)
        if unknown:
            limitations.append(f"高管变动中 {unknown} 条股数缺失，方向未知（未填 0）")
        meta["insider"] = {"rows": ins["row_count"],
                           "note": "CHANGE_SHARES 负值为减持、正值为增持（实测符号方向）"}
        if not any(d == "减持" for d in dirs):
            limitations.append("高管变动记录中未出现负值股数，减持方向口径待复核")

    hd = out.get("holders")
    if hd and hd["records"]:
        # ⚠️ 不能用"最新一条"作代表日：实测 2000 条里最新是 2026-11-03（仅1条），
        # 绝大多数是 2026-10-20/10-09。用最新值会把滞后算成 1 天，严重失真。
        # 改用**出现次数最多的披露日**（众数）作为数据集代表日期。
        from collections import Counter as _C
        dcnt = _C(r["end_date"] for r in hd["records"] if r["end_date"])
        latest = dcnt.most_common(1)[0][0] if dcnt else None
        dates = sorted({r["end_date"] for r in hd["records"] if r["end_date"]})
        meta["holders"] = {
            "rows": hd["row_count"],
            "latest_report_date": latest,
            "note": "股东户数为定期报告披露，天然滞后，非实时",
        }
        if latest:
            lag = (datetime.strptime(as_of, "%Y-%m-%d")
                   - datetime.strptime(latest, "%Y-%m-%d")).days
            meta["holders"]["lag_days"] = lag
            if lag > 30:
                limitations.append(f"股东户数最新披露日为 {latest}，滞后 {lag} 天")

    st = out.get("seats")
    if st and st["records"]:
        meta["seats"] = {
            "rows": st["row_count"],
            "raw_rows": st.get("raw_row_count"),
            "dedup_by": st.get("dedup_by"),
            "date_span": st.get("date_span"),
            "positioning": st.get("positioning"),
            "note": "营业部历史累计活跃度，按席位代码去重；非实时信号",
        }
        sp = st.get("date_span") or {}
        if sp.get("min") and sp.get("max"):
            limitations.append(
                f"席位数据为历史累计活跃度，最早记录 {sp['min']}，不是当日信号")

    fc = out.get("forecast")
    if fc and fc["records"]:
        meta["forecast"] = {"rows": fc["row_count"], "note": "业绩预告为管理层主动披露"}

    # 明确不可得的数据集（不猜测接口名，如实标注）
    meta["unavailable"] = {
        "研报覆盖": "接口不可得（探测 10+ reportName 均失败）",
        "股东增减持": "接口不可得",
        "大宗交易": "接口不可得",
        "限售解禁": "接口不可得",
    }

    return {
        "as_of": as_of,
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "lookback_days": lookback_days,
        "datasets": out,
        "meta": meta,
        "limitations": limitations,
        "elapsed_sec": round(time.time() - t0, 2),
    }


if __name__ == "__main__":
    snap = fetch_all()
    print(f"as_of={snap['as_of']} 耗时 {snap['elapsed_sec']}s")
    sv = snap["datasets"]["surveys"]
    if sv:
        print(f"调研: 原始 {sv['raw_row_count']} 行 -> {sv['session_count']} 场次")
        for s in sv["sessions"][:5]:
            print(f"   {s['receive_start']} {s['name'][:8]:10s} "
                  f"机构{s['inst_count']:>4}家 way={s['way']}")
    ins = snap["datasets"]["insider"]
    if ins:
        print(f"高管变动: {ins['row_count']} 条")
    print("limitations:", snap["limitations"])
