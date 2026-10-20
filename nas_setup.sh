#!/usr/bin/env bash
# 飞牛OS(fnOS) / Debian 一键部署：豆瓣「想看」 → Trakt「待看」 每 10 分钟自动同步
# 用法：把本脚本放进项目目录，SSH 登录 NAS 后执行：
#   cd /vol1/@appshare/Gocron/douban-trakt-sync && bash nas_setup.sh
# 幂等：重复执行不会重复写 crontab，也不会重复装依赖。

set -e
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "=============================================="
echo " 豆瓣想看 → Trakt 待看  一键部署"
echo "=============================================="
echo "项目目录: $DIR"
echo ""

# ---------- 1. 检查 python ----------
PY_SYS="$(command -v python3 || command -v python || true)"
if [ -z "$PY_SYS" ]; then
  echo "❌ 没找到 python3。"
  echo "   请到飞牛「应用中心」安装 Python3，然后重新执行本脚本。"
  exit 1
fi
echo "[1/5] 系统 python: $PY_SYS ($($PY_SYS -V 2>&1))"

# ---------- 2. 虚拟环境 + 依赖 ----------
if [ ! -x "$DIR/venv/bin/python" ]; then
  echo "[2/5] 创建虚拟环境 venv ..."
  if ! "$PY_SYS" -m venv venv 2>/dev/null; then
    echo "      ⚠️ venv 创建失败（可能缺 python3-venv），改为直接用系统 python"
  fi
fi

if [ -x "$DIR/venv/bin/python" ]; then
  echo "      安装依赖到 venv ..."
  "$DIR/venv/bin/python" -m pip install -q --upgrade pip 2>/dev/null || true
  "$DIR/venv/bin/python" -m pip install -q -r requirements.txt
  echo "      ✅ venv 依赖就绪"
else
  echo "      安装依赖到系统 python ..."
  "$PY_SYS" -m pip install -q -r requirements.txt 2>/dev/null \
    || "$PY_SYS" -m pip install -q --break-system-packages -r requirements.txt
  echo "      ✅ 系统 python 依赖就绪"
fi

# ---------- 3. 权限 ----------
chmod +x "$DIR/run_sync.sh"
chmod 600 "$DIR/config.json" "$DIR/token.json" 2>/dev/null || true
echo "[3/5] 权限已设置（config.json / token.json 设为 600）"

# ---------- 4. 写 crontab（幂等） ----------
CRON_LINE="*/10 * * * * /bin/bash $DIR/run_sync.sh >> $DIR/cron.log 2>&1"
MARKER="douban-trakt-sync/run_sync.sh"

if crontab -l 2>/dev/null | grep -Fq "$MARKER"; then
  echo "[4/5] 发现旧的定时任务，先移除"
  crontab -l 2>/dev/null | grep -Fv "$MARKER" | crontab - || true
fi
( crontab -l 2>/dev/null; echo "$CRON_LINE" ) | crontab -
echo "      已写入："
echo "      $CRON_LINE"

# ---------- 5. 试跑一次 ----------
echo ""
echo "[5/5] 试跑一次（约 10~30 秒）..."
bash "$DIR/run_sync.sh" || echo "      ⚠️ 试跑返回非 0，请看下面日志"

echo ""
echo "=============================================="
echo " 最近日志（sync.log 末尾 25 行）"
echo "=============================================="
tail -n 25 "$DIR/sync.log" 2>/dev/null || echo "(还没有 sync.log，说明脚本没跑起来)"

echo ""
echo "=============================================="
echo " 部署完成"
echo "=============================================="
echo " · 每 10 分钟自动同步一次"
echo " · 查看同步日志: tail -f $DIR/sync.log"
echo " · 查看 cron 输出: tail -f $DIR/cron.log"
echo " · 查看定时任务: crontab -l"
echo ""
