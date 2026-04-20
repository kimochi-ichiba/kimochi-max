#!/bin/bash
# 気持ちマックス(kimochi-max) 健康チェックスクリプト
# 20分ごとに実行し、異常があれば通知する

PROJECT=/Users/sanosano/projects/kimochi-max
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
ALERTS=()
NORMAL_STATUS=""

# ================ 1. ボットプロセス生存確認 ================
# crypto-bot-pro ディレクトリで動いている main.py を監視（=気持ちマックスの元プロセス）
BOT1_PID=$(pgrep -f "main.py.*port 8082" | head -1)
BOT2_PID=$(pgrep -f "main.py.*port 8080" | head -1)

if [ -z "$BOT1_PID" ]; then
    ALERTS+=("🚨 ボット(ポート8082)が停止しています")
fi
if [ -z "$BOT2_PID" ]; then
    ALERTS+=("🚨 ボット(ポート8080)が停止しています")
fi

# ================ 2. bot_state の鮮度確認 (tmp or json の新しい方) ================
STATE_MAIN="/Users/sanosano/projects/crypto-bot-pro/bot_state.json"
STATE_TMP="/Users/sanosano/projects/crypto-bot-pro/bot_state.json.tmp"
STATE_FILE="$STATE_MAIN"
if [ -f "$STATE_TMP" ]; then
    TMP_MTIME=$(stat -f %m "$STATE_TMP" 2>/dev/null || echo 0)
    MAIN_MTIME=$(stat -f %m "$STATE_MAIN" 2>/dev/null || echo 0)
    if [ "$TMP_MTIME" -gt "$MAIN_MTIME" ]; then
        STATE_FILE="$STATE_TMP"
    fi
fi

if [ -f "$STATE_FILE" ]; then
    MTIME=$(stat -f %m "$STATE_FILE")
    NOW=$(date +%s)
    DIFF=$((NOW - MTIME))
    if [ $DIFF -gt 600 ]; then
        ALERTS+=("⚠️ 状態ファイルが${DIFF}秒（$((DIFF/60))分）更新なし - ボット停止中かも")
    fi
fi

# ================ 3. ボット残高・DD確認 (Python で JSON解析) ================
STATE_INFO=$(python3 -c "
import json
import os
try:
    with open('$STATE_FILE') as f:
        s = json.load(f)
    balance = s.get('balance', 0)
    initial = s.get('initial_balance', 10000)
    positions = len(s.get('positions', {}))
    max_dd = s.get('max_dd_pct', 0)
    consec = s.get('consecutive_losses', 0)
    daily_pnl = s.get('daily_pnl', 0)
    ret_pct = (balance / initial - 1) * 100 if initial > 0 else 0
    print(f'{balance}|{ret_pct}|{positions}|{max_dd}|{consec}|{daily_pnl}')
except Exception as e:
    print(f'ERROR|{e}|0|0|0|0')
" 2>/dev/null)

IFS='|' read -r BALANCE RET_PCT POSITIONS MAX_DD CONSEC DAILY_PNL <<< "$STATE_INFO"

if [ "$BALANCE" != "ERROR" ]; then
    # 残高急落チェック (初期比-10%以下なら警告)
    if (( $(echo "$RET_PCT < -10" | bc -l 2>/dev/null || echo 0) )); then
        ALERTS+=("🚨 残高が初期比${RET_PCT}%と大幅マイナス！")
    fi
    # DD 10%超えで警告
    if (( $(echo "$MAX_DD > 10" | bc -l 2>/dev/null || echo 0) )); then
        ALERTS+=("⚠️ 最大ドローダウン${MAX_DD}%が10%を超過")
    fi
    # 5連敗以上で警告
    if [ "${CONSEC:-0}" -ge 5 ] 2>/dev/null; then
        ALERTS+=("⚠️ ${CONSEC}連敗中 - ロジック点検推奨")
    fi
    # 日次損失 5% 超過で警告
    DAILY_LOSS_PCT=$(python3 -c "print($DAILY_PNL / 10000 * 100)" 2>/dev/null)
    if (( $(echo "$DAILY_LOSS_PCT < -5" | bc -l 2>/dev/null || echo 0) )); then
        ALERTS+=("🚨 本日損失${DAILY_LOSS_PCT}%が-5%を超過")
    fi

    NORMAL_STATUS="残高\$${BALANCE} (${RET_PCT}%) | ポジ${POSITIONS}件 | DD${MAX_DD}% | 連敗${CONSEC}"
fi

# ================ 4. エラーログ確認 ================
LOG_FILE="/Users/sanosano/projects/crypto-bot-pro/dashboard.log"
if [ -f "$LOG_FILE" ]; then
    # 直近5分のエラーを検索
    RECENT_ERRORS=$(tail -1000 "$LOG_FILE" 2>/dev/null | grep -iE "ERROR|CRITICAL|Exception|Traceback" | tail -3)
    if [ -n "$RECENT_ERRORS" ]; then
        ALERTS+=("⚠️ 直近のエラーログ検出:")
        ALERTS+=("$(echo "$RECENT_ERRORS" | sed 's/^/    /')")
    fi
fi

# ================ 5. 結果出力 ================
if [ ${#ALERTS[@]} -eq 0 ]; then
    echo "[$TIMESTAMP] ✅ 気持ちマックス正常稼働 | $NORMAL_STATUS"
else
    echo "[$TIMESTAMP] 🚨 異常検知 ${#ALERTS[@]}件"
    for alert in "${ALERTS[@]}"; do
        echo "  $alert"
    done
    echo "  正常時情報: $NORMAL_STATUS"
fi
