#!/bin/bash
# 发布流程：校验 → 通过则提交并推送 → 失败则保留线上旧版
#
# 设计原则：宁可线上是旧数据，也不让错误数据上线。
# 失败时退出码非 0，调用方可据此判断。
set -u

WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$WORKDIR" || exit 1

PY="${MARKETFLOW_PYTHON:-}"
if [ -z "$PY" ]; then
    PY=$(command -v python3 || echo /opt/homebrew/bin/python3)
fi

TODAY=$(date '+%F')
echo "=== 发布流程 $(date '+%F %T') ==="

# ① 校验（阻塞级问题则退出）
echo "· 校验数据…"
"$PY" validate_publish.py "$TODAY" > /tmp/_pubval.log 2>&1
RC=$?
tail -14 /tmp/_pubval.log

if [ $RC -ne 0 ]; then
    echo
    echo "❌ 校验未通过，**拒绝推送**"
    echo "   线上保留上一版内容。数据修正后重跑 bump 即可。"
    exit 1
fi

# ② 检查是否有变更
if [ -z "$(git status --porcelain)" ]; then
    echo "· 无变更，跳过推送"
    exit 0
fi

# ③ 提交并推送
echo "· 提交并推送…"
git add -A
git -c user.name="Y-B-S" -c user.email="Y-B-S@users.noreply.github.com" \
    commit -q -m "自动更新 $(date '+%F %H:%M')

数据校验通过后自动发布。校验项见 validate_publish.py：
- 涨跌平是否等于有效数
- 涨跌停是否超过总数
- 指数涨跌与上涨占比是否矛盾（防 11-02 那类错位）
- 成交额跨口径偏差
- 成交额与上日是否突变
- 成交额是否为整数填充值"

PUSH_OK=1
git push -q origin main 2>/tmp/_pubpush.err || PUSH_OK=0
if [ $PUSH_OK -eq 0 ]; then
    echo "❌ 推送失败："
    head -5 /tmp/_pubpush.err
    exit 1
fi

echo "✓ 已推送，GitHub Pages 将在 1-2 分钟内更新"
exit 0
