#!/bin/bash
# =====================================================
# デプロイ前スナップショット
# 再起動・コード変更前に bot_state.json を不変コピーとして保全
# =====================================================

cd "$(dirname "$0")"

STATE="bot_state.json"
LEDGER="trade_ledger.jsonl"
BACKUP_DIR="state_backups"

mkdir -p "$BACKUP_DIR"

TS=$(date +%Y%m%d_%H%M%S)

if [ -f "$STATE" ]; then
  cp "$STATE" "$BACKUP_DIR/bot_state_PREDEPLOY_${TS}.json"
  echo "✅ 状態スナップショット: $BACKUP_DIR/bot_state_PREDEPLOY_${TS}.json"
fi

if [ -f "$LEDGER" ]; then
  cp "$LEDGER" "$BACKUP_DIR/trade_ledger_PREDEPLOY_${TS}.jsonl"
  echo "✅ 台帳スナップショット: $BACKUP_DIR/trade_ledger_PREDEPLOY_${TS}.jsonl"
fi

# 90日より古いPREDEPLOYバックアップは削除（運用開始時は残す）
# find "$BACKUP_DIR" -name "bot_state_PREDEPLOY_*.json" -mtime +90 -delete 2>/dev/null
# find "$BACKUP_DIR" -name "trade_ledger_PREDEPLOY_*.jsonl" -mtime +90 -delete 2>/dev/null
