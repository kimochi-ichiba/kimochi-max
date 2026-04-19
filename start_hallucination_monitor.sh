#!/bin/bash
# ハルシネーション監視を5分毎に多取引所クロスチェック（Binance/MEXC/Bitget/CoinGecko）
cd "$(dirname "$0")"

LOG="$(pwd)/hallucination_check.log"
PID_FILE="$(pwd)/hallucination_monitor.pid"

if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if ps -p "$OLD_PID" > /dev/null 2>&1; then
    echo "⚠️ 既に起動中です (PID: $OLD_PID)"
    exit 1
  fi
fi

echo "🚀 ハルシネーション監視を5分毎に起動（多取引所クロスチェック版）..."
nohup python3 hallucination_monitor.py --daemon >> "$LOG" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 2
if ps -p "$NEW_PID" > /dev/null 2>&1; then
  echo "✅ 起動成功 (PID: $NEW_PID)"
  echo "   ログ: $LOG"
  echo "   停止: kill $NEW_PID"
else
  echo "❌ 起動失敗"
fi
