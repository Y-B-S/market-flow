"""每日执行助手：判断「现在该不该跑」，并给出可执行的结论。

回答"这个工具要每日执行吗"时，答案取决于**你要它做什么**。
本脚本把这件事变成可执行的判断，而不是让你记一堆规则。

    python3 daily.py            # 现在该不该跑？
    python3 daily.py --check    # 只看状态，不执行
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import store  # noqa: E402

CST = timezone(timedelta(hours=8))


def _parse_hm(s):
    h, m = s.split(":")
    return int(h) * 100 + int(m)


# A 股交易时段
SESSIONS = [(_parse_hm("09:30"), _parse_hm("11:30")),
            (_parse_hm("13:00"), _parse_hm("15:00"))]
# 数据定盘时间：收盘后约 15 分钟，资金流与涨跌停池才完全落定
SETTLED_AFTER = _parse_hm("15:15")


def phase(now: datetime):
    """返回当前所处阶段。

    ⚠️ 必须按时间顺序判断全部分支。早期实现在"交易时段"之后直接判
    "刚收盘"，导致 11:32（午间休市，下午还有交易）被误报为"刚收盘
    数据未定盘"——而实际上它既不是收盘、数据也不该在此时落盘。
    """
    hm = now.hour * 100 + now.minute
    wd = now.weekday()
    if wd >= 5:
        return "weekend", "周末休市"
    if hm < _parse_hm("09:15"):
        return "pre_open", "开盘前（无当日数据）"
    if hm < _parse_hm("09:30"):
        return "auction", "集合竞价"
    if any(a <= hm <= b for a, b in SESSIONS):
        return "in_session", "交易时段"
    # 11:30-13:00 为午间休市：不是"刚收盘"，下午还有交易
    if hm < _parse_hm("13:00"):
        return "noon_break", "午间休市（下午仍有交易）"
    if hm < SETTLED_AFTER:
        return "just_closed", "刚收盘（数据可能未完全定盘）"
    return "settled", "收盘后（数据已定盘）"


def advice(ph, code):
    """针对当前阶段给出建议。"""
    if code == "weekend":
        return ("不要跑", [
            "周末无新行情。重复跑只会覆盖同一份数据，没有意义。",
            "可以用周末做别的事：回看验证（validate.py）、审计（audit.py）。",
        ])
    if code == "pre_open":
        return ("不要跑", [
            "此时接口返回的是占位符（f62 全为 '-'），跑出来是空盘数据。",
            "留着盘后跑。若早上想看昨天的结论，用 --no-fetch 读已存快照。",
        ])
    if code == "auction":
        return ("不要跑", [
            "集合竞价阶段数据不完整且波动极大，不适合生成结论。",
            "等 09:30 之后再考虑。",
        ])
    if code == "in_session":
        return ("可跑，但别落盘", [
            "盘中数据是「截至此刻的半日数据」，不是完整交易日。",
            "程序会自动拒绝盘中落盘（store.save 返回 None），不会污染历史。",
            "盘中跑的价值：看资金实时流向、抓盘中背离。但结论不可当收盘结论用。",
        ])
    if code == "noon_break":
        return ("可跑，但别落盘", [
            "午间休市，上午的数据已固定，但下午还有交易，全天数据未完成。",
            "此时跑看到的是「上午半日数据」。程序同样会拒绝落盘。",
            "午间看盘可以，但别把结论当全天结论。",
        ])
    if code == "just_closed":
        return ("再等一会", [
            "刚收盘，资金流与涨跌停池可能尚未完全落定。",
            "建议 15:15 之后跑，数据更完整。",
        ])
    return ("应该跑", [
        "这是每日唯一的「该跑」窗口：数据已定盘、且是今天。",
        "跑完后 data/ 会自动归档，为将来回测与环比积累历史。",
    ])


def main(argv=None):
    check_only = "--check" in (argv or sys.argv[1:])
    now = datetime.now(CST)
    ph, label = phase(now)

    print("=" * 74)
    print("每日执行判断")
    print("=" * 74)
    print(f"当前时间：{now.strftime('%Y-%m-%d %H:%M')} ({label})")
    print(f"阶段判定：{ph}")

    action, reasons = advice(ph, ph)
    print(f"\n结论：**{action}**\n")
    for r in reasons:
        print(f"  · {r}")

    # 历史积累情况
    dates = store.available_dates()
    print(f"\n已有快照：{len(dates)} 天" +
          (f"（{dates[0]} ~ {dates[-1]}）" if dates else "（还没有）"))

    if check_only:
        return 0

    print()
    if action.startswith("应该跑"):
        print("→ 执行：python3 main.py")
    elif action == "再等一会":
        print("→ 稍后再执行：python3 main.py")
    else:
        print("→ 不执行。若只是想重看已有结论：python3 main.py --no-fetch")

    print("""
"要每日执行吗"的答案是分层的：

  · **收盘后（15:15 之后）跑一次** —— 需要。这是唯一能积累完整历史时点的窗口。
  · **盘前/周末跑** —— 不需要，只会产出空盘或重复数据。
  · **盘中跑** —— 可选。能看实时流向，但结论是半日数据，程序会拒绝落盘。
  · **漏跑了怎么办** —— 板块级数据可回溯 120 个交易日，能补；
    但个股级截面（涨跌家数、个股榜单）**补不回来**，因为接口只给序列不给历史截面。

所以准确说法是：**每天收盘后跑一次就够了，但不是"必须"，因为漏跑影响有限**。
真正会因为漏跑而永久丢失的，只有"当日全市场宽度指标"这一类截面数据。
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
