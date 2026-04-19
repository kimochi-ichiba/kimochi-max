#!/bin/bash
# ===============================================================
# V2 Kelly SIM ローカル起動スクリプト
# バックグラウンドで常駐し、1時間毎に価格チェック＆月次リバランス
# ===============================================================

cd "$(dirname "$0")"

LOG="$(pwd)/v2_sim_log.txt"
PID_FILE="$(pwd)/v2_sim.pid"

# 既存プロセスがあれば終了
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if ps -p "$OLD_PID" > /dev/null 2>&1; then
    echo "⚠️  既に起動中です (PID: $OLD_PID)。停止する場合: bash stop_v2_sim.sh"
    exit 1
  fi
fi

echo "🚀 V2 Kelly SIMをバックグラウンドで開始します..."
echo "   ログ: $LOG"

nohup python3 run_v2_kelly_sim.py >> "$LOG" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

sleep 2
if ps -p "$NEW_PID" > /dev/null 2>&1; then
  echo "✅ 起動成功 (PID: $NEW_PID)"
  echo ""
  echo "  状態確認: cat v2_sim_state.json | python3 -m json.tool | head -30"
  echo "  ログ監視: tail -f v2_sim_log.txt"
  echo "  停止    : bash stop_v2_sim.sh"
else
  echo "❌ 起動失敗。ログを確認してください: tail $LOG"
  exit 1
fi
