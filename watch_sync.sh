#!/usr/bin/env bash
# 豆瓣想看 -> Trakt 待看：准实时守护进程的启停脚本
#
#   ./watch_sync.sh start     后台启动
#   ./watch_sync.sh stop      停止
#   ./watch_sync.sh restart   重启
#   ./watch_sync.sh status    查看是否在跑
#   ./watch_sync.sh log       实时看日志（Ctrl+C 退出，不会杀掉进程）
#   ./watch_sync.sh once      只跑一轮（调试用）
#
# 开机自启（fnOS 任务计划 / cron）：
#   @reboot /bin/bash /vol1/1000/定时任务/douban-trakt-sync/watch_sync.sh start >> /vol1/1000/定时任务/douban-trakt-sync/watch.log 2>&1

set -u
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

PIDFILE="$DIR/watch.pid"
LOCK="$DIR/.once.lock"
LOG="$DIR/watch.log"
NAME="douban-trakt-watch"

is_running() {
  [ -f "$PIDFILE" ] || return 1
  local pid
  pid="$(cat "$PIDFILE" 2>/dev/null)"
  [ -n "${pid:-}" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

start() {
  if is_running; then
    echo "$NAME 已在运行 (pid $(cat "$PIDFILE"))"
    return 0
  fi
  # setsid + nohup：脱离终端，父进程退出也不受影响
  setsid nohup "$PY" "$DIR/watch_sync.py" >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  sleep 2
  if is_running; then
    echo "$NAME 已启动 (pid $(cat "$PIDFILE"))，日志: $LOG"
  else
    echo "启动失败，请看日志: $LOG"
    return 1
  fi
}

stop() {
  if ! is_running; then
    echo "$NAME 未在运行"
    rm -f "$PIDFILE"
    return 0
  fi
  local pid
  pid="$(cat "$PIDFILE")"
  kill "$pid" 2>/dev/null
  for _ in $(seq 1 15); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "仍未退出，强制 kill -9"
    kill -9 "$pid" 2>/dev/null
  fi
  rm -f "$PIDFILE"
  echo "$NAME 已停止"
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  # 给 cron 当"看门狗"用：已在跑就静默退出，没在跑就拉起来
  #   */5 * * * * /bin/bash /path/watch_sync.sh ensure >> /path/watch.log 2>&1
  ensure)  is_running && exit 0; start ;;
  status)
    if is_running; then echo "运行中 (pid $(cat "$PIDFILE"))"; else echo "未运行"; fi ;;
  log)     tail -f "$LOG" ;;
  # once: 只跑一轮（探测一次，有新增才同步）。给「任务计划/minute cron」用最简单，
  #       不需要后台常驻进程，也不用 chmod +x（用 bash 显式调用即可）。
  #       自带互斥锁：上一轮没跑完就直接退出，避免堆积。
  once)
    # 注意：这里不能用 exec —— exec 会替换掉 shell 进程，EXIT trap 不会执行，锁就永远删不掉了
    if [ -d "$LOCK" ]; then
      old="$(cat "$LOCK/pid" 2>/dev/null)"
      if [ -n "${old:-}" ] && kill -0 "$old" 2>/dev/null; then
        exit 0                       # 上一轮真的还在跑，这次跳过
      fi
      rm -rf "$LOCK" 2>/dev/null     # 进程已死的残留锁，立刻清掉
    fi
    mkdir "$LOCK" 2>/dev/null || exit 0
    echo $$ > "$LOCK/pid" 2>/dev/null
    "$PY" "$DIR/watch_sync.py" --once
    rc=$?
    rm -rf "$LOCK" 2>/dev/null
    exit $rc ;;
  *)
    echo "用法: $0 {start|stop|restart|status|log|once}"
    exit 1
    ;;
esac
