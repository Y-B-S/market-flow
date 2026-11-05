"""发布前校验：数据不自相矛盾才允许推送到线上。

## 为什么需要

自动推送意味着错误数据会直接出现在线上。今天的教训很具体：

  11-02：上证真实 +1.08%（上涨日），但记录的涨跌家数是
         涨2108 / 跌3309（跌多涨少）——内部矛盾，却被推上了网站。

所以推送前必须过一道**自动化校验**。原则：

  1. 校验用的是**多个独立角度交叉验证同一份数据**，不是单看一个字段
  2. 校验不过 → **不推送**，保留上次的线上版本（宁可旧，不可错）
  3. 每条校验必须能说清「违反意味着什么」

## 校验分层

  BLOCK  阻塞级：必然错误，绝对不能发（如 vip 数据自相矛盾）
  WARN   警告级：可疑但可能正常（如涨停数异常高）
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DASH = "—"


def _n(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


class Result:
    def __init__(self):
        self.blocks = []   # (规则名, 说明)
        self.warns = []
        self.checks = []   # (规则名, 是否通过, 说明)

    def block(self, name, msg):
        self.blocks.append((name, msg))
        self.checks.append((name, False, msg))
        return False

    def warn(self, name, msg):
        self.warns.append((name, msg))
        self.checks.append((name, True, msg))
        return True

    def ok(self, name, msg=""):
        self.checks.append((name, True, msg))
        return True

    @property
    def passed(self):
        return not self.blocks


def validate(snapshot, analysis=None):
    """对一份快照做发布前校验。返回 Result。"""
    r = Result()
    s = snapshot if isinstance(snapshot, dict) else {}
    a = analysis or {}
    b = s.get("breadth") or {}
    as_of = s.get("as_of")

    # ── 1. 基础完整性 ────────────────────────────────────────────────
    if not as_of:
        r.block("交易日缺失", "快照没有 as_of，无法确定是哪天的数据")
    if not b:
        r.block("宽度数据缺失", "breadth 为空——回补快照或采集失败，不应发布")
        return r

    amt = _n(b.get("amount_total"))
    cnt = _n(b.get("count"))
    valid = _n(b.get("valid"))
    up = _n(b.get("up"))
    down = _n(b.get("down"))
    flat = _n(b.get("flat"))
    lu = _n(b.get("limit_up"))
    ld = _n(b.get("limit_down"))

    if amt is None or amt <= 0:
        r.block("成交额无效", f"成交额={amt}，不可能为 0 或空")
    else:
        r.ok("成交额有效", f"{amt/1e8:.0f} 亿")
    # 成交额是否整数（真实接口值不会整万整亿）
    if amt is not None and amt > 0:
        yi = amt / 1e8
        if abs(yi - round(yi, 0)) < 1e-9 and yi in (1000, 5000, 10000, 15000, 20000):
            r.block("成交额疑似填充值",
                    f"成交额恰为 {yi:.0f} 亿整——接口真实值不会这么整，"
                    f"疑似人工填充或占位数据")
    if cnt is None or cnt <= 0:
        r.block("个股总数无效", f"count={cnt}")
    if valid is not None and cnt is not None and valid > cnt:
        r.block("有效数超过总数", f"valid={valid} > count={cnt}")

    # ── 2. 内部一致性（涨跌平 == 有效数）─────────────────────────────
    if None not in (up, down, flat, valid):
        s3 = up + down + flat
        if s3 != valid:
            r.block("涨跌平不等于有效数",
                    f"{up}+{down}+{flat}={s3}，但 valid={valid}，"
                    f"说明宽度数据有缺失或重复统计")
        else:
            r.ok("涨跌平等于有效数", f"{int(s3):,} 家")
    elif None in (up, down, flat):
        r.block("涨跌家数缺失", f"up={up} down={down} flat={flat}")

    # ── 3. 涨跌幅不可能超过总数 ──────────────────────────────────────
    if lu is not None and cnt is not None and lu > cnt:
        r.block("涨停数超过总数", f"涨停 {lu} > 个股总数 {cnt}")
    if ld is not None and cnt is not None and ld > cnt:
        r.block("跌停数超过总数", f"跌停 {ld} > 个股总数 {cnt}")
    if lu is not None and ld is not None and cnt is not None:
        if lu + ld > cnt * 0.5:
            r.block("涨跌停占比过高",
                    f"涨停{lu}+跌停{ld}={lu+ld}，占总数{cnt}的"
                    f"{(lu+ld)/cnt*100:.1f}%——历史罕见，疑似数据异常")
        else:
            r.ok("涨跌停数量合理", f"涨停{int(lu)}/跌停{int(ld)}")

    # ── 4. 与指数交叉验证（关键：能抓出 11-02 那类错误）─────────────
    #
    # 逻辑：上证指数涨跌方向应与「涨跌家数占优方向」大体一致。
    # 指数大涨却「跌多涨少」是内部矛盾——11-02 事件正是如此。
    idx = s.get("indices") or []
    sh = next((x for x in idx if "上证" in str(x.get("name"))), None)
    sh_chg = _n(sh.get("chg")) if sh else None
    if sh_chg is not None and None not in (up, down):
        total_ud = up + down
        if total_ud > 0:
            up_ratio = up / total_ud
            # 判据设计：不用固定阈值（样本不足，无法统计出可靠阈值），
            # 改用两个更本质的条件，任一命中即判矛盾：
            #
            # ① 方向矛盾：指数涨≥1% 但上涨占比<50%，或指数跌≤-1% 但占比>50%
            #    —— 这是"涨的少但指数涨"，只有极少数权重股拉盘才可能，
            #       在普通交易日几乎必然意味着涨跌家数错位。
            # ② 幅度严重失配：|指数涨跌| ≥ 1.5% 且占比偏离 50% 的方向相反超过 15pp
            #
            # 收紧原因（实测）：原阈值 1.5% 时，11-02 的
            #   上证 +1.08% / 上涨占比 39% 竟然判定"不矛盾"而漏网。
            dir_flip = ((sh_chg >= 1.0 and up_ratio < 0.50)
                        or (sh_chg <= -1.0 and up_ratio > 0.50))
            gap_big = (abs(sh_chg) >= 1.5
                       and ((sh_chg > 0 and up_ratio < 0.35)
                            or (sh_chg < 0 and up_ratio > 0.65)))
            if dir_flip or gap_big:
                r.block("指数与涨跌家数矛盾",
                        f"上证 {sh_chg:+.2f}%，但上涨占比 {up_ratio*100:.1f}%"
                        f"（{int(up)}涨/{int(down)}跌）——方向不一致，"
                        f"疑似涨跌家数错位（11-02 那类错误）")
            else:
                r.ok("指数与涨跌家数不矛盾",
                      f"上证{sh_chg:+.2f}% / 上涨占比{up_ratio*100:.0f}%")

    # ── 5. 成交额跨口径校验（指数加总 vs 个股加总）──────────────────
    cons = s.get("consistency") or {}
    diff = _n(cons.get("diff_pct"))
    if diff is not None:
        if diff > 5.0:
            r.block("成交额跨口径偏差过大",
                    f"指数加总与个股加总偏差 {diff:.2f}%（阈值 5%）")
        elif diff > 2.0:
            r.warn("成交额跨口径偏差偏大", f"偏差 {diff:.2f}%")
        else:
            r.ok("成交额跨口径一致", f"偏差 {diff:.2f}%")
    else:
        r.warn("无法做成交额跨口径校验", "consistency 字段缺失")

    # ── 6. 与上一交易日连续性（突变检测）─────────────────────────────
    prev = _load_prev(as_of)
    if prev:
        pb = prev.get("breadth") or {}
        p_amt = _n(pb.get("amount_total"))
        p_up = _n(pb.get("up"))
        if p_amt and amt:
            ratio = amt / p_amt
            if ratio < 0.5 or ratio > 2.0:
                r.block("成交额单日突变",
                        f"较上一交易日（{prev.get('as_of')}）变化 "
                        f"{(ratio-1)*100:+.0f}%（{p_amt/1e8:.0f}亿 → {amt/1e8:.0f}亿），"
                        f"超出 50% 波动范围")
            else:
                r.ok("成交额与上日连续", f"{(ratio-1)*100:+.1f}%")
        # 涨跌家数完全相同的日子极少见（除非两天都横盘）
        if p_up is not None and up is not None and p_up == up:
            p_down = _n(pb.get("down"))
            if p_down is not None and down is not None and p_down == down:
                r.warn("涨跌家数与上日完全相同",
                       f"涨{up}/跌{down} 与前一日完全一致——"
                       f"可能是数据错位，也可能确实两天横盘")

    # ── 7. 结论合理性（与 metrics 输出交叉）─────────────────────────
    if a:
        reg = (a.get("regime") or {}).get("label")
        if reg and None not in (up, down):
            total_ud = up + down
            if total_ud > 0:
                ratio = up / total_ud
                # 上涨占比 >70% 却被判为「防守」，或 <30% 被判为「进攻」
                if reg == "防守" and ratio > 0.70:
                    r.warn("状态与宽度矛盾",
                           f"上涨占比 {ratio*100:.0f}% 却判定为「防守」")
                elif reg == "进攻" and ratio < 0.30:
                    r.warn("状态与宽度矛盾",
                           f"上涨占比仅 {ratio*100:.0f}% 却判定为「进攻」")
                else:
                    r.ok("状态与宽度自洽", f"{reg} / 上涨占比 {ratio*100:.0f}%")

    return r


_PREV_CACHE = {}


def _load_prev(as_of):
    """取 as_of 之前最近一个完整快照。"""
    if as_of in _PREV_CACHE:
        return _PREV_CACHE[as_of]
    d = os.path.join(HERE, "data")
    cands = []
    if os.path.isdir(d):
        for fn in os.listdir(d):
            if fn.endswith(".json") and fn[:4].isdigit() and fn[:-5] < as_of:
                cands.append(fn[:-5])
    best = None
    for dt in sorted(cands, reverse=True):
        try:
            s0 = json.load(open(os.path.join(d, f"{dt}.json"), encoding="utf-8"))
            if (s0.get("breadth") or {}).get("amount_total"):
                best = s0
                break
        except Exception:  # noqa: BLE001
            continue
    _PREV_CACHE[as_of] = best
    return best


def report(r, verbose=True):
    if verbose:
        print(f"{'='*62}")
        print("发布前校验")
        print(f"{'='*62}")
        for name, ok, msg in r.checks:
            mark = "✓" if ok else "✗"
            print(f"  {mark} {name}" + (f"  [{msg}]" if msg else ""))
        print()
        if r.blocks:
            print(f"❌ 阻塞级问题 {len(r.blocks)} 项 —— 拒绝发布：")
            for name, msg in r.blocks:
                print(f"   · {name}: {msg}")
            print("\n保留上一版线上内容，等数据修正后重试。")
        elif r.warns:
            print(f"⚠ 警告 {len(r.warns)} 项（不阻塞发布）：")
            for name, msg in r.warns:
                print(f"   · {name}: {msg}")
            print("\n✓ 允许发布")
        else:
            print("✓ 全部校验通过，允许发布")
    return r.passed


if __name__ == "__main__":
    d = os.path.join(HERE, "data")
    snaps = sorted([f[:-5] for f in os.listdir(d)
                    if f.endswith(".json") and f[:4].isdigit()], reverse=True)
    # ⚠️ 无参数时必须校验「今天」，不能退回最新快照。
    # 实测问题：不指定日期时会自动取 11-04（昨天）并通过校验，
    # 导致「今天没数据」被误判为「校验通过」。
    target = sys.argv[1] if len(sys.argv) > 1 else __import__("datetime").date.today().strftime("%Y-%m-%d")
    if not target:
        raise SystemExit("无快照可校验")
    path = os.path.join(d, f"{target}.json")
    if not os.path.exists(path):
        print(f"{'='*62}")
        print("发布前校验")
        print(f"{'='*62}")
        print(f"  ✗ 指定日期的快照不存在: {target}")
        print()
        print(f"❌ 拒绝发布：{target} 没有数据")
        print("   （若当天是交易日且未收盘，这是正常状态——等采集完成）")
        raise SystemExit(1)
    snap = json.load(open(path, encoding="utf-8"))
    import metrics
    try:
        a = metrics.compute(snap)
    except Exception:  # noqa: BLE001
        a = {}
    res = validate(snap, a)
    ok = report(res)
    raise SystemExit(0 if ok else 1)
