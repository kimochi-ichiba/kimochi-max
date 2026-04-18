#!/bin/bash
# =====================================================
# crypto-bot-pro 停止スクリプト
# 使い方: bash stop.sh
# =====================================================

echo "🛑 crypto-bot-pro を停止します..."

# ボットプロセスを停止
pkill -f "main.py --port 8082" 2>/dev/null && echo "✅ ボット停止" || echo "（ボットは動いていませんでした）"

# caffeinate（スリープ防止）を停止
if [ -f /tmp/crypto-bot-caff.pid ]; then
  kill $(cat /tmp/crypto-bot-caff.pid) 2>/dev/null
  rm /tmp/crypto-bot-caff.pid
  echo "✅ スリープ防止 解除"
fi

# start.sh の自動再起動ループも停止
pkill -f "start.sh" 2>/dev/null

echo "✅ 全て停止しました"
