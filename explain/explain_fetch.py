"""背离解释器 · 取数层。

三层数据（实测确认可用）：
  ① 估值 —— 官方行业估值 RPT_VALUEINDUSTRY_DET（127 板块，权威口径）
  ② 新闻 —— 东财搜索 API（需剔除同源资金流报道）
  ③ 财报 —— 业绩预告（覆盖率仅 4%，仅作日历提示）

已否决的方案（见 STAGE1B）：
  ✗ 用成分股自算 PE 作代理 —— 与官方口径平均差 29.6%，不可用

铁律：
  1. 取不到 = None，绝不填 0 或估算
  2. 每层必须记录口径与滞后
  3. 新闻必须剔除同源资金流报道（否则等于拿数据源 A 验证数据源 A）
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import fetch as base_fetch   # noqa: E402

DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SEARCH = "https://search-api-web.eastmoney.com/search/jsonp"
UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
}

# ── 新闻同源过滤规则（实测迭代两版确定，见 DESIGN.md 5.2）────────────
# 这些标题的原始数据来自东财资金流本身；若当作独立证据，
# 四层交叉检验会退化为"拿数据源A验证数据源A"。
FLOW_SELFREF = [
    "资金流出榜", "资金流入榜", "资金流向日报", "半日主力资金",
    "主力资金净", "净流入资金", "净流出资金", "净流入规模",
    "资金净流入", "资金净流出", "加仓", "抛售", "主力资金",
    "资金流向", "个股异动", "板块异动", "局部异动",
    # 盘面播报类：只是把当日涨跌换个说法，无独立信息量
    "板块走高", "板块走弱", "震荡走强", "震荡走弱", "表现活跃",
    "涨停潮", "触及跌停", "直线涨停", "涨幅居前", "跌幅居前",
    # ETF 营销软文：实为基金推介，不是产业信息（实测电网设备 8 条保留中有 6 条是此类）
    "ETF", "配置性价比", "逢跌布局", "标的指数", "布局机遇",
    "关注", "基金",
]

# 事实标签：只做可核对的短语命中，**不做情感分析**
POS_FACTS = ["净流入居首", "中标", "订单", "涨价", "扩产", "供需改善",
             "净利增长", "同比增长", "创新高", "突破", "放量", "扭亏"]
NEG_FACTS = ["净流出", "回调", "降价", "减产", "亏损", "预减", "下滑",
             "库存", "疲软", "下跌", "承压", "首亏"]

# 财报预告类型 -> 方向
# 业绩预告类型 → 方向（实测共 11 种类型，必须全部覆盖）
FORECAST_DIR = {
    # 正面
    "预增": +1, "略增": +1, "扭亏": +1, "续盈": +1, "减亏": +1,
    # 负面
    "预减": -1, "略减": -1, "首亏": -1, "续亏": -1, "增亏": -1,
    # 无法判定
    "不确定": 0,
}


def _get_json(url, referer=None, retries=3, timeout=25):
    last = None
    for i in range(retries):
        try:
            h = dict(UA)
            if referer:
                h["Referer"] = referer
            req = urllib.request.Request(url, headers=h)
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
            # 东财搜索返回 jsonp 包装，剥掉
            s = raw.strip()
            if not s.startswith("{"):
                a, b = s.find("{"), s.rfind("}")
                if a >= 0 and b > a:
                    s = s[a:b + 1]
            return json.loads(s)
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(0.5 * (2 ** i))
    raise RuntimeError(f"GET failed: {url[:100]} :: {last}")


# ── ① 估值 ────────────────────────────────────────────────────────────

def fetch_valuation(names=None, max_pages=6):
    """官方行业估值（按板块逐个取，可取到完整历史）。

    ⚠️ 关键效率/深度陷阱（实测）：
    按 TRADE_DATE 倒序全表翻页，40 页×500 条只覆盖 **158 个交易日**，
    因为每页返回的是"同一批日期 × 127 个板块"，翻页等于在日期上爬。
    正确做法是**按 BOARD_NAME 过滤单板块**，一次可取到 **2387 个交易日（2017 至今）**，
    且只需 1.8 秒/板块。

    覆盖约束：只有 127 个申万板块有官方估值，东财 496 板块体系仅约 27% 可匹配。
    无数据的板块**返回 None**，不做代理估算（代理误差 29.6%，见 STAGE1B）。
    """
    out = {}
    page = 1
    if names:
        out = {}
        import urllib.parse as _up
        for nm in names:
            f = _up.quote(f'(BOARD_NAME="{nm}")')
            hist = []
            for p in range(1, max_pages + 1):
                u = (f"{DC}?reportName=RPT_VALUEINDUSTRY_DET"
                     f"&columns=BOARD_NAME,BOARD_CODE,TRADE_DATE,PE_TTM,PB_MRQ,TOTAL_MARKET_CAP"
                     f"&pageSize=500&pageNumber={p}&filter={f}"
                     f"&sortColumns=TRADE_DATE&sortTypes=-1")
                try:
                    d = _get_json(u, referer="https://data.eastmoney.com/")
                except Exception:  # noqa: BLE001
                    break
                res = d.get("result") or {}
                rows = res.get("data") or []
                if not rows:
                    break
                for r in rows:
                    hist.append({
                        "date": (r.get("TRADE_DATE") or "")[:10],
                        "pe": base_fetch.num(r.get("PE_TTM")),
                        "pb": base_fetch.num(r.get("PB_MRQ")),
                    })
                if p >= (res.get("pages") or 1):
                    break
            if not hist:
                out[nm] = None
                continue
            hist.sort(key=lambda x: x["date"])
            cur = hist[-1]
            pes = [h["pe"] for h in hist if h["pe"]]
            g = {
                "date": cur["date"],
                "pe_ttm": cur["pe"],
                "pb": cur["pb"],
                "hist": hist,
                "hist_days": len(hist),
            }
            if cur["pe"] and pes:
                g["pe_pct"] = sum(1 for x in pes if x < cur["pe"]) / len(pes) * 100.0
                g["pe_min"] = min(pes)
                g["pe_max"] = max(pes)
                g["pe_median"] = pes[len(pes) // 2]
            else:
                g["pe_pct"] = None
            out[nm] = g
        return out

    # 未指定板块名时，只取最新截面（用于判断哪些板块有估值）
    import urllib.parse as _up
    while page <= 3:
        u = (f"{DC}?reportName=RPT_VALUEINDUSTRY_DET"
             f"&columns=BOARD_NAME,BOARD_CODE,TRADE_DATE,PE_TTM,PB_MRQ,TOTAL_MARKET_CAP"
             f"&pageSize=500&pageNumber={page}"
             f"&sortColumns=TRADE_DATE&sortTypes=-1")
        try:
            d = _get_json(u, referer="https://data.eastmoney.com/")
        except Exception:  # noqa: BLE001
            break
        res = d.get("result") or {}
        rows = res.get("data") or []
        if not rows:
            break
        for r in rows:
            name = (r.get("BOARD_NAME") or "").strip()
            if name and name not in out:
                out[name] = {
                    "date": (r.get("TRADE_DATE") or "")[:10],
                    "pe_ttm": base_fetch.num(r.get("PE_TTM")),
                    "pb": base_fetch.num(r.get("PB_MRQ")),
                    "hist": [], "hist_days": 0, "pe_pct": None,
                }
        if page >= (res.get("pages") or 1):
            break
        page += 1
    return out


# ── ② 新闻 ────────────────────────────────────────────────────────────

def _clean(s):
    return re.sub(r"<[^>]+>", "", s or "")


def _facts(title):
    t = _clean(title)
    pos = [w for w in POS_FACTS if w in t]
    neg = [w for w in NEG_FACTS if w in t]
    return pos, neg


def fetch_news(name, page_size=20, retries=2):
    """按板块名检索新闻，并剔除同源资金流报道。"""
    param = {
        "uid": "", "keyword": name, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "default",
            "pageIndex": 1, "pageSize": page_size}},
    }
    u = SEARCH + "?cb=&param=" + urllib.parse.quote(json.dumps(param, ensure_ascii=False))
    try:
        d = _get_json(u, referer="https://so.eastmoney.com/", retries=retries, timeout=25)
    except Exception:  # noqa: BLE001
        return None

    arts = ((d.get("result") or {}).get("cmsArticleWebOld")) or []
    raw = len(arts)
    kept, dropped = [], []
    seen = set()          # 标题去重：同一事件常被多家媒体重复报道
    for a in arts:
        title = _clean(a.get("title"))
        if any(w in title for w in FLOW_SELFREF):
            dropped.append(title)
            continue
        # 归一化标题做去重（去掉空白与标点差异）
        norm = re.sub(r"[\s，,。、\-—|｜]+", "", title)
        if norm in seen:
            continue
        seen.add(norm)
        pos, neg = _facts(title)
        kept.append({
            "date": (a.get("date") or "")[:16],
            "title": title,
            "media": a.get("mediaName"),
            "url": a.get("url"),
            "pos": pos, "neg": neg,
        })
    return {
        "name": name,
        "raw_count": raw,
        "kept": kept,
        "dropped_count": len(dropped),
        "dup_removed": raw - len(kept) - len(dropped),
        "dropped_sample": dropped[:3],
        "pos_hits": sum(len(x["pos"]) for x in kept),
        "neg_hits": sum(len(x["neg"]) for x in kept),
    }


def fetch_news_batch(names, workers=8):
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {n: ex.submit(fetch_news, n) for n in names}
        for n, f in futs.items():
            try:
                out[n] = f.result()
            except Exception:  # noqa: BLE001
                out[n] = None
    return out


# ── ③ 财报日历（降级方案：只给预告，不给数字）─────────────────────────

def fetch_forecast_calendar(since_date, max_pages=6):
    """业绩预告。按 (股票代码, 报告期) 去重 —— 实测原始数据有大量重复行。

    覆盖率仅约 4%（实测 226/5767 公司），**只作日历提示，不给板块级财务结论**。
    """
    rows, page = [], 1
    while page <= max_pages:
        f = urllib.parse.quote(f"(NOTICE_DATE>='{since_date}')")
        u = (f"{DC}?reportName=RPT_PUBLIC_OP_NEWPREDICT"
             f"&columns=SECURITY_CODE,SECURITY_NAME_ABBR,NOTICE_DATE,REPORT_DATE,PREDICT_TYPE"
             f"&pageSize=500&pageNumber={page}"
             f"&sortColumns=NOTICE_DATE&sortTypes=-1&filter={f}")
        try:
            d = _get_json(u, referer="https://data.eastmoney.com/")
        except Exception:  # noqa: BLE001
            break
        res = d.get("result") or {}
        batch = res.get("data") or []
        if not batch:
            break
        rows.extend(batch)
        if page >= (res.get("pages") or 1):
            break
        page += 1

    # 去重：同一 (代码, 报告期) 只保留最新公告
    uniq = {}
    for r in rows:
        code = r.get("SECURITY_CODE")
        rep = (r.get("REPORT_DATE") or "")[:10]
        if not code:
            continue
        k = (code, rep)
        notice = (r.get("NOTICE_DATE") or "")[:10]
        if k not in uniq or notice > (uniq[k].get("notice_date") or ""):
            uniq[k] = {
                "code": code,
                "name": r.get("SECURITY_NAME_ABBR"),
                "notice_date": notice,
                "report_date": rep,
                "type": r.get("PREDICT_TYPE"),
                "dir": FORECAST_DIR.get((r.get("PREDICT_TYPE") or "").strip()),
            }
    recs = list(uniq.values())
    return {
        "raw_rows": len(rows),
        "deduped": len(recs),
        "companies": len({r["code"] for r in recs}),
        "records": recs,
        "coverage_note": "业绩预告为自愿披露，实测覆盖约 4% 公司，不可作板块级财务推断",
    }


def fetch_forecast_for_boards(board_names, forecast, constituents_fn=None):
    """把业绩预告关联到板块（需要成分股）。覆盖极低，多数板块会是空的。"""
    if not forecast:
        return {}
    by_code = {}
    for r in forecast.get("records") or []:
        by_code.setdefault(r["code"], []).append(r)
    out = {}
    for name in board_names:
        if constituents_fn is None:
            out[name] = None
            continue
        try:
            members = constituents_fn(name)
        except Exception:  # noqa: BLE001
            members = []
        hits = []
        for code in members:
            if code in by_code:
                hits.extend(by_code[code])
        out[name] = hits
    return out


# ── 板块成分股（业绩预告聚合用）──────────────────────────────────────

_MEMBER_CACHE = {}


def fetch_constituents(board_code, board_name=None):
    """板块成分股代码列表。pz 上限 100，必须分页。"""
    key = board_code or board_name
    if key in _MEMBER_CACHE:
        return _MEMBER_CACHE[key]
    if not board_code:
        return []
    out = []
    total = None
    for p in range(1, 12):
        try:
            u = (f"https://push2.eastmoney.com/api/qt/clist/get"
                 f"?pn={p}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
                 f"&fs=b:{board_code}&fields=f12")
            d = base_fetch.get(u, retries=2, timeout=25)
        except Exception:  # noqa: BLE001
            break
        data = d.get("data") or {}
        total = data.get("total")
        rows = data.get("diff") or []
        if not rows:
            break
        out.extend(r.get("f12") for r in rows if r.get("f12"))
        if total and len(out) >= total:
            break
    _MEMBER_CACHE[key] = out
    return out


def attach_forecast_to_boards(boards, forecast):
    """把业绩预告按成分股关联到板块。boards: [(code, name), ...]"""
    if not forecast or not forecast.get("records"):
        return {}
    by_code = {}
    for r in forecast["records"]:
        by_code.setdefault(r["code"], []).append(r)
    out = {}
    for code, name in boards:
        members = fetch_constituents(code, name)
        hits = []
        for m in members:
            hits.extend(by_code.get(m, []))
        out[name] = hits if hits else None
    return out


# ── 主入口 ────────────────────────────────────────────────────────────

def fetch_all(board_names, boards=None, as_of=None, with_forecast=True):
    t0 = time.time()
    limitations = []
    if as_of is None:
        as_of = time.strftime("%Y-%m-%d")

    print(f"· 取估值（官方行业口径，按板块逐个取完整历史）…")
    try:
        val = fetch_valuation(board_names)
    except Exception as e:  # noqa: BLE001
        val = {}
        limitations.append(f"估值取数失败: {type(e).__name__}")
    hit = sum(1 for n in board_names if val.get(n))
    print(f"  估值匹配 {hit}/{len(board_names)} 个板块")

    print(f"· 取新闻（{len(board_names)} 个板块）…")
    news = fetch_news_batch(board_names)
    ok_news = sum(1 for v in news.values() if v)
    print(f"  新闻成功 {ok_news}/{len(board_names)}")

    fc = None
    if with_forecast:
        print(f"· 取业绩预告…")
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 120 * 86400))
        try:
            fc = fetch_forecast_calendar(since)
            print(f"  预告 {fc['raw_rows']} 行 → 去重 {fc['deduped']} 条 / {fc['companies']} 家公司")
        except Exception as e:  # noqa: BLE001
            limitations.append(f"业绩预告取数失败: {type(e).__name__}")

    fbyb = {}
    if fc and boards:
        print(f"· 把业绩预告关联到板块（需成分股）…")
        fbyb = attach_forecast_to_boards(boards, fc)
        hit = sum(1 for v in fbyb.values() if v)
        print(f"  有关联预告的板块 {hit}/{len(boards)}")

    return {
        "as_of": as_of,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "valuation": val,
        "news": news,
        "forecast": fc,
        "forecast_by_board": fbyb,
        "limitations": limitations,
        "elapsed_sec": round(time.time() - t0, 1),
    }


if __name__ == "__main__":
    import metrics
    snap = json.load(open(os.path.join(ROOT, "data", "2026-11-03.json"), encoding="utf-8"))
    a = metrics.compute(snap)
    div = (a.get("divergence_gated") or a.get("divergence_top") or [])[:8]
    names = [d["name"] for d in div]
    print(f"用真实背离样本 {len(names)} 个: {names}\n")
    data = fetch_all(names, as_of="2026-11-03")
    print(f"\n耗时 {data['elapsed_sec']}s")
    print("\n=== 估值匹配 ===")
    for n in names:
        v = data["valuation"].get(n)
        if v and v.get("pe_ttm"):
            pct = v.get("pe_pct")
            pstr = f" 历史分位{pct:.0f}%" if pct is not None else ""
            print(f"  {n:<12s} PE={v['pe_ttm']:.1f}{pstr}  ({v['hist_days']}日历史)")
        else:
            print(f"  {n:<12s} 不可得（不在申万估值体系内）")
