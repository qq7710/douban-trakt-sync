#!/usr/bin/env bash
# NAS 定时调用包装脚本：优先用本地 venv，否则退回系统 python3。
# 用法：把它放进项目目录，cron / 计划任务指向它即可。
set -e
# cron 环境下的 PATH 很窄，显式补全，避免找不到 python3 / bash 内置命令
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if [ -x "$DIR/venv/bin/python" ]; then
  PY="$DIR/venv/bin/python"
elif [ -x "$DIR/.venv/bin/python" ]; then
  PY="$DIR/.venv/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

exec "$PY" wishlist_sync.py --config config.json
