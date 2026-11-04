# 数据契约 v1（冻结，禁止各模块私自改字段名）

所有金额统一 **元（float）**，比率统一 **百分数（float，1.5 表示 1.5%）**。
数据源：东方财富公开接口（免 Key）。所有时间以 `Asia/Shanghai` 为准。

## 已验证的接口字段语义（用日线累加交叉验证过，勿凭记忆改）

| 字段 | 语义 | 验证方式 |
|---|---|---|
| `f62` | 今日主力净流入额 | 与日线 `fflow/daykline` 末行一致 |
| `f164` | **近 5 日**主力净流入额累计 | BK1200: -34.34亿 == 日线近5日累加 -34.34亿 ✅ |
| `f174` | **近 10 日**主力净流入额累计 | BK1200: -201.70亿 == 日线近10日累加 -201.70亿 ✅ |
| `f267` | 非 5/10 日累计，**不要使用** | 语义未确认 |
| `f184` | 今日主力净额占成交额比（%） | — |
| `f21` | 流通市值（元） | 用于归一化 |
| `f20` | 总市值（元） | — |
| `f6` | 成交额（元） | — |
| `f8` | 换手率（%） | — |
| `f3` | 涨跌幅（%） | — |
| `f12` / `f14` | 代码 / 名称 | — |

**陷阱**：`f3` 等字段在停牌股上可能返回字符串 `"-"`，所有取数必须走 `num()` 安全转换，禁止直接比较。

## 接口清单（全部实测可用）

```
# 全市场个股：必须分页，pz 上限 100（传更大值也只返回 100）
GET https://push2.eastmoney.com/api/qt/clist/get
  ?pn=<1..60>&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3
  &fs=m:0+t:6,m:0+t:13,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048
  &fields=f12,f14,f2,f3,f6,f8,f20,f21,f62,f100
  → data.total=5927, data.diff=[...]

# 行业板块 496 个 / 概念板块 531 个（pz 上限同样 100，需分页）
  fs=m:90+t:2  (行业)    fs=m:90+t:3  (概念)
  &fields=f12,f14,f3,f6,f8,f20,f21,f62,f164,f174,f184

# 指数行情
GET https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2
  &secids=1.000001,0.399001,0.399006,1.000688,1.000300&fields=f2,f3,f6,f12,f14

# 指数/个股 日线资金流（用于历史序列）
GET https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get
  ?lmt=<n>&klt=101&secid=1.000001&fields1=f1,f2,f3,f7
  &fields2=f51,f52,f53,f54,f55,f56,f57,f58
  → data.klines=["2026-11-03,主力净额,小单,中单,大单,超大单,主力占比,...", ...]
  注意：klines[0]=日期, [1]=主力净额, [2]=小单, [3]=中单, [4]=大单, [5]=超大单

# 指数分钟资金流（盘中）
  同上，klt=1，返回 240 条 "YYYY-MM-DD HH:MM,..."

# 涨停池 / 跌停池
GET https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989
  &dpt=wz.ztzt&Pageindex=0&pagesize=200&sort=fbt:asc&date=YYYYMMDD
  同上 getTopicDTPool 为跌停池
  → data.tc=总数, data.pool=[{c:代码,n:名称,p:价格*1000,zdp:涨跌幅,lbc:连板数,zttj:{days,ct},hybk:行业}]

# 两融（T+1，今天取到的是昨天）
GET https://datacenter-web.eastmoney.com/api/data/v1/get
  ?reportName=RPTA_RZRQ_LSHJ&columns=ALL&pageSize=5&sortColumns=dim_date&sortTypes=-1
  → result.data[0] = {DIM_DATE, RZYE:融资余额, RQYE:融券余额, RZRQYE:两融余额, RZJME:融资净买入}

# 龙虎榜（当日，需按 TRADE_DATE 过滤）
  ?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL&pageSize=500
  &sortColumns=TRADE_DATE&sortTypes=-1

# 全球指数 / 汇率
GET https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2
  &secids=100.HSI,100.NDX,100.DJIA,100.SPX,100.N225,101.GC00Y,102.CL00Y,133.USDCNH,100.UDI
  &fields=f2,f3,f12,f14
```

## 明确不做的字段（禁止编造）

- **北向资金每日净流入**：2024-08 起交易所停止披露。接口 `kamt` 返回 `status:3, dayNetAmtIn:0`。
  任何模块**不得**把该值当真实数据展示，也不得用南向数据冒充。最多展示"港股通南向"并明确标注。
- 主力资金 = 东财按单笔金额分档的口径估算，非真实席位数据，展示时须注明口径。

## 统一数据结构（store 层落盘 JSON 的顶层键）

```json
{
  "as_of": "2026-11-03",            // 交易日
  "generated_at": "2026-11-04T00:30:00+08:00",
  "indices": [ {code,name,price,chg,amount} ],
  "breadth": {up,down,flat,limit_up,limit_down,up5pct,amount_total,main_net_total,count},
  "industries": [ {code,name,chg,amount,turnover,mktcap_float,main_net,main_net_5d,main_net_10d,main_ratio} ],
  "concepts":   [ 同上 ],
  "stocks": [ {code,name,price,chg,amount,turnover,mktcap_float,main_net,industry} ],
  "limit_up_pool": [ {code,name,chg,limit_days,industry,seal_time} ],
  "limit_down_count": 17,
  "margin": {date,margin_balance,margin_net_buy,short_balance},
  "lhb": {count,net_buy,trade_date},
  "index_flow": { "1.000001": {net_5d, net_10d, series:[{date,main_net}]} },
  "global": [ {code,name,price,chg} ]
}
```

## 模块边界

| 文件 | 只依赖 | 对外只提供 |
|---|---|---|
| `fetch.py` | 标准库 | `fetch_all() -> dict`（原始、未打分） |
| `store.py` | 标准库 | `save(snapshot)` / `load(date)` / `history(n)` |
| `metrics.py` | 纯函数，**不碰 IO** | `compute(snapshot) -> analysis dict` |
| `render_html.py` | 纯函数，**不碰网络** | `render(analysis, snapshot) -> html str` |
| `render_term.py` | 纯函数，**不碰网络** | `render(analysis, snapshot) -> str` |
| `main.py` | 以上全部 | 编排 + CLI |

`metrics.py` / `render_*.py` 必须能在无网络、只有一份固定 JSON 的情况下跑通——这是可测试的前提。
