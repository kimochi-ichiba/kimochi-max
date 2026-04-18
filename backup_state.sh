#!/bin/bash
# =====================================================
# backup_state.sh — 取引データを手動でバックアップする
# =====================================================
# 使い方: bash backup_state.sh [メモ]
# 例:     bash backup_state.sh "更新前の保存"
#         bash backup_state.sh "好調なとき"
#         bash backup_state.sh  ← メモなしでもOK
# =====================================================

cd "$(dirname "$0")"

STATE_FILE="bot_state.json"
BACKUP_DIR="state_backups"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

mkdir -p "$BACKUP_DIR"

if [ ! -f "$STATE_FILE" ]; then
    echo -e "${RED}❌ bot_state.json が見つかりません${NC}"
    exit 1
fi

# タイムスタンプ + メモをファイル名に含める
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
MEMO="${1:-手動バックアップ}"
# ファイル名に使えない文字を除去
SAFE_MEMO=$(echo "$MEMO" | tr ' ' '_' | tr -dc 'a-zA-Z0-9_\-あ-んア-ン一-龯')
BACKUP_FILE="${BACKUP_DIR}/bot_state_${TIMESTAMP}_${SAFE_MEMO}.json"

cp "$STATE_FILE" "$BACKUP_FILE"

# ファイルサイズと概要を表示
SIZE=$(wc -c < "$BACKUP_FILE")
BALANCE=$(python3 -c "
import json
try:
    d = json.load(open('$BACKUP_FILE'))
    b = d.get('balance', 0)
    p = len(d.get('positions', {}))
    t = len(d.get('trade_history', []))
    print(f'残高: \${b:,.2f} / ポジション: {p}件 / 履歴: {t}件')
except:
    print('解析できませんでした')
" 2>/dev/null)

echo -e "${GREEN}✅ バックアップ完了！${NC}"
echo -e "   ファイル: $BACKUP_FILE"
echo -e "   サイズ:   ${SIZE} bytes"
echo -e "   内容:     ${BALANCE}"
echo ""

# バックアップ一覧を表示（最新5件）
echo -e "${YELLOW}📁 最近のバックアップ（最新5件）:${NC}"
ls -1t "$BACKUP_DIR"/bot_state_*.json 2>/dev/null | head -5 | while read f; do
    echo "   $(basename $f)"
done

echo ""
echo -e "復元するには: ${YELLOW}cp $BACKUP_FILE $STATE_FILE${NC}"
