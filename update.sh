#!/bin/bash
# =====================================================
# update.sh — 取引状態を保持したまま安全にコードを更新する
# =====================================================
# 使い方: bash update.sh
#
# やること:
#   1. bot_state.json をバックアップ（大事なデータを守る）
#   2. 動いているボットに「丁寧に止まれ」信号を送る
#      → ボットが自分で状態を保存してから停止する
#   3. 最新コードを git pull で取得
#   4. Python パッケージを最新に（必要なら）
#   5. ボットを再起動
#   6. 健全性チェック（ちゃんと動いているか確認）
# =====================================================

set -e  # エラーが出たら即座に停止（安全のため）
cd "$(dirname "$0")"

STATE_FILE="bot_state.json"
BACKUP_DIR="state_backups"
LOG="/tmp/crypto-bot-pro.log"
PID_FILE="/tmp/crypto-bot-pro.pid"

# カラー表示用
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}=====================================================${NC}"
echo -e "${BLUE}  crypto-bot-pro 安全アップデート開始${NC}"
echo -e "${BLUE}  $(date '+%Y-%m-%d %H:%M:%S')${NC}"
echo -e "${BLUE}=====================================================${NC}"

# ── Step 1: bot_state.json をバックアップ ────────────
echo ""
echo -e "${YELLOW}[Step 1/6] 取引データをバックアップ中...${NC}"
mkdir -p "$BACKUP_DIR"

if [ -f "$STATE_FILE" ]; then
    TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
    BACKUP_FILE="${BACKUP_DIR}/bot_state_${TIMESTAMP}.json"
    cp "$STATE_FILE" "$BACKUP_FILE"
    echo -e "${GREEN}  ✅ バックアップ完了: $BACKUP_FILE${NC}"

    # 古いバックアップを整理（30個以上あったら古いものを削除）
    BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/bot_state_*.json 2>/dev/null | wc -l)
    if [ "$BACKUP_COUNT" -gt 30 ]; then
        ls -1t "$BACKUP_DIR"/bot_state_*.json | tail -n +31 | xargs rm -f
        echo -e "${GREEN}  🗂️  古いバックアップを整理しました（30個まで保持）${NC}"
    fi
else
    echo -e "${YELLOW}  ⚠️  bot_state.json が見つかりません（初回起動前かもしれません）${NC}"
fi

# ── Step 2: 動いているボットを安全に停止 ─────────────
echo ""
echo -e "${YELLOW}[Step 2/6] ボットを安全に停止中...${NC}"

if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "  📡 ボット(PID: $OLD_PID)にSIGTERMを送信中（状態保存して停止）..."
        kill -TERM "$OLD_PID" 2>/dev/null || true

        # 最大15秒待つ（ボットが状態保存+停止するのを待つ）
        for i in $(seq 1 15); do
            if ! kill -0 "$OLD_PID" 2>/dev/null; then
                echo -e "${GREEN}  ✅ ボットが正常に停止しました（${i}秒）${NC}"
                break
            fi
            echo "  ⏳ 停止待機中... ($i/15秒)"
            sleep 1
        done

        # それでも動いていたら強制停止
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo -e "${RED}  ⚠️  強制停止します（SIGKILL）${NC}"
            kill -9 "$OLD_PID" 2>/dev/null || true
            sleep 1
        fi
    else
        echo "  ℹ️  PIDファイルはありましたがプロセスは既に停止していました"
    fi
    rm -f "$PID_FILE"
else
    echo "  ℹ️  実行中のボットはありませんでした（PIDファイルなし）"
fi

# caffeinate も停止
if [ -f "/tmp/crypto-bot-caff.pid" ]; then
    CAFF_PID=$(cat /tmp/crypto-bot-caff.pid)
    kill "$CAFF_PID" 2>/dev/null || true
    rm -f /tmp/crypto-bot-caff.pid
fi

# ── Step 3: 最新コードを取得 ─────────────────────────
echo ""
echo -e "${YELLOW}[Step 3/6] 最新コードを取得中（git pull）...${NC}"
if git status &>/dev/null; then
    # 変更内容を表示
    echo "  現在のブランチ: $(git branch --show-current 2>/dev/null || echo '不明')"
    git pull origin "$(git branch --show-current 2>/dev/null || echo 'main')" 2>&1 | \
        sed 's/^/  /'
    echo -e "${GREEN}  ✅ コード更新完了${NC}"
else
    echo "  ℹ️  Git管理外のディレクトリです（git pullをスキップ）"
fi

# ── Step 4: Python依存パッケージを確認・更新 ──────────
echo ""
echo -e "${YELLOW}[Step 4/6] Pythonパッケージを確認中...${NC}"
if [ -f "requirements.txt" ]; then
    pip3 install -r requirements.txt -q --no-warn-script-location 2>&1 | tail -5 | sed 's/^/  /'
    echo -e "${GREEN}  ✅ パッケージ確認完了${NC}"
fi

# ── Step 5: ボットを再起動 ────────────────────────────
echo ""
echo -e "${YELLOW}[Step 5/6] ボットを再起動中...${NC}"
echo "  📝 ログファイル: $LOG"

# caffeinate 再起動（Mac スリープ防止）
caffeinate -ims &
CAFF_PID=$!
echo "$CAFF_PID" > /tmp/crypto-bot-caff.pid
echo -e "${GREEN}  ✅ スリープ防止 再開 (PID: $CAFF_PID)${NC}"

# ボットをバックグラウンドで起動
echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] update.sh による再起動" >> "$LOG"
python3 main.py --port 8082 --balance 10000 >> "$LOG" 2>&1 &
BOT_PID=$!
echo "$BOT_PID" > "$PID_FILE"
echo -e "${GREEN}  ✅ ボット起動 (PID: $BOT_PID)${NC}"

# ── Step 6: 起動確認 ──────────────────────────────────
echo ""
echo -e "${YELLOW}[Step 6/6] 起動確認中（5秒待機）...${NC}"
sleep 5

if kill -0 "$BOT_PID" 2>/dev/null; then
    echo -e "${GREEN}  ✅ ボットは正常に動いています！${NC}"

    # 直近のログを表示
    echo ""
    echo -e "${BLUE}  === 最新ログ（直近5行）===${NC}"
    tail -5 "$LOG" | sed 's/^/  /'
else
    echo -e "${RED}  ❌ ボットが起動直後に停止しました。ログを確認してください:${NC}"
    echo -e "${RED}     tail -30 $LOG${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}=====================================================${NC}"
echo -e "${GREEN}  ✅ アップデート完了！${NC}"
echo -e "${GREEN}  取引データは引き継がれています。${NC}"
echo -e "${GREEN}  ダッシュボード: http://localhost:8082${NC}"
echo -e "${GREEN}=====================================================${NC}"
