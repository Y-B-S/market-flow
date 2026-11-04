#!/bin/bash
# 每个交易日收盘后自动运行的包装脚本（通用版）。
#
# ⚠️ 部署提示：本脚本若要通过 macOS launchd 定时执行，
# **不要放在 ~/Documents 等 TCC 保护目录**，否则会报
#   getcwd: Operation not permitted / Operation not permitted
# 正确做法是把本文件与日志目录放到 ~/.marketflow/，
# 只让 Python 在项目目录里工作。详见 README「每日用法」。
set -u

# 脚本所在目录即项目根目录（不写死绝对路径）
WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGDIR="${MARKETFLOW_LOG_DIR:-$WORKDIR/logs}"
LOG="$LOGDIR/daily.log"
mkdir -p "$LOGDIR"

# 周末跳过
DOW=$(date +%u)
if [ "$DOW" -ge 6 ]; then
    echo "[$(date '+%F %T')] 周末，跳过" >> "$LOG"
    exit 0
fi

PY="${MARKETFLOW_PYTHON:-}"
if [ -z "$PY" ]; then
    PY=$(command -v python3)
fi
[ -x "$PY" ] || { echo "[$(date '+%F %T')] 找不到 python3" >> "$LOG"; exit 1; }

echo "[$(date '+%F %T')] 开始运行" >> "$LOG"

OUT=$(cd "$WORKDIR" && "$PY" main.py 2>&1)
RC=$?
echo "$OUT" >> "$LOG"

if [ $RC -ne 0 ]; then
    echo "[$(date '+%F %T')] ❌ 失败 (exit=$RC)" >> "$LOG"
    exit $RC
fi

TODAY=$(date '+%F')
if [ -f "$WORKDIR/data/$TODAY.json" ]; then
    echo "[$(date '+%F %T')] ✓ 完成，已归档 data/$TODAY.json" >> "$LOG"
else
    echo "[$(date '+%F %T')] ⚠ 完成但未归档今天快照（盘前/非交易日）" >> "$LOG"
fi
