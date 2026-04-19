#!/bin/bash
# V2 Kelly SIM 停止スクリプト

cd "$(dirname "$0")"
PID_FILE="$(pwd)/v2_sim.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "❌ PIDファイルがありません。起動していない可能性があります"
  exit 1
fi

PID=$(cat "$PID_FILE")
if ps -p "$PID" > /dev/null 2>&1; then
  kill "$PID"
  sleep 1
  if ps -p "$PID" > /dev/null 2>&1; then
    kill -9 "$PID"
  fi
  echo "✅ V2 Kelly SIMを停止しました (PID: $PID)"
  rm -f "$PID_FILE"
else
  echo "⚠️  プロセスは既に停止しています"
  rm -f "$PID_FILE"
fi
