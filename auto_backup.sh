#!/bin/bash
# =====================================================
# auto_backup.sh — 自動バックアップ（cron で呼び出す）
# 30分ごとに bot_state.json を自動保存
# 古いバックアップは最新48個（約24時間分）だけ残す
# =====================================================

cd "$(dirname "$0")"

STATE_FILE="bot_state.json"
BACKUP_DIR="state_backups"
KEEP_COUNT=48  # 30分×48 = 約24時間分

mkdir -p "$BACKUP_DIR"

# bot_state.json が存在しない場合はスキップ
if [ ! -f "$STATE_FILE" ]; then
    exit 0
fi

# タイムスタンプ付きでバックアップ
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
BACKUP_FILE="${BACKUP_DIR}/bot_state_${TIMESTAMP}_自動バックアップ.json"
cp "$STATE_FILE" "$BACKUP_FILE"

# 古いバックアップを削除（自動バックアップのみ、手動バックアップは残す）
AUTO_BACKUPS=$(ls -1t "$BACKUP_DIR"/bot_state_*_自動バックアップ.json 2>/dev/null)
COUNT=$(echo "$AUTO_BACKUPS" | grep -c . 2>/dev/null || echo 0)

if [ "$COUNT" -gt "$KEEP_COUNT" ]; then
    DELETE_COUNT=$((COUNT - KEEP_COUNT))
    echo "$AUTO_BACKUPS" | tail -n "$DELETE_COUNT" | xargs rm -f
fi

# ログに記録
LOG_FILE="$BACKUP_DIR/auto_backup.log"
BALANCE=$(python3 -c "
import json
try:
    d = json.load(open('$BACKUP_FILE'))
    b = d.get('balance', 0)
    p = len(d.get('positions', {}))
    t = len(d.get('trade_history', []))
    print(f'残高=\${b:,.2f} ポジション={p}件 履歴={t}件')
except:
    print('解析エラー')
" 2>/dev/null)

echo "$(date '+%Y-%m-%d %H:%M:%S') バックアップ完了: $BALANCE" >> "$LOG_FILE"

# ログは最新200行だけ残す
tail -n 200 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
