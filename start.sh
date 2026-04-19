#!/bin/bash
# =====================================================
# crypto-bot-pro 24時間自動起動スクリプト
# 落ちたら自動で再起動する。Macが眠らないようにする。
# 使い方: bash start.sh
# 止めるとき: bash stop.sh
# =====================================================

cd "$(dirname "$0")"

LOG="/tmp/crypto-bot-pro.log"
PID_FILE="/tmp/crypto-bot-pro.pid"

echo "🚀 crypto-bot-pro を24時間モードで起動します..."

# 起動前スナップショット（絶対にデータを失わないため）
if [ -f "pre_deploy_snapshot.sh" ]; then
  bash pre_deploy_snapshot.sh
fi

# 古いプロセスを停止
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  kill "$OLD_PID" 2>/dev/null
  sleep 1
fi

# Macが眠らないようにする（caffeinate）
# -i: アイドルスリープ防止  -m: ディスクスリープ防止  -s: システムスリープ防止
caffeinate -ims &
CAFF_PID=$!
echo "$CAFF_PID" > /tmp/crypto-bot-caff.pid
echo "✅ スリープ防止 開始 (PID: $CAFF_PID)"

# 自動再起動ループ
while true; do
  echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] ボット起動..." >> "$LOG"
  python3 main.py --port 8082 --balance 10000 >> "$LOG" 2>&1
  EXIT_CODE=$?
  echo "$(date '+%Y-%m-%d %H:%M:%S') [WARN] ボットが停止しました (終了コード: $EXIT_CODE)。10秒後に再起動します..." >> "$LOG"
  echo "⚠️  ボットが停止。10秒後に自動再起動します..."
  sleep 10
done
