#!/bin/bash
# Kelly Bot 健康チェックスクリプト (20分ごと実行)

PROJECT=/Users/sanosano/projects/crypto-bot-pro
STATE_FILE="$PROJECT/kelly_bot_state.json"
LOG_FILE="$PROJECT/kelly_bot.log"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
ALERTS=()

# ================ 1. Kelly Botダッシュボード生存確認 ================
if ! pgrep -f "kelly_bot_dashboard" > /dev/null; then
    ALERTS+=("🚨 Kelly Botダッシュボード(ポート8083)が停止")
fi

# ================ 2. ログ鮮度確認（Kelly Botは30日ごとリバランスのみ活動） ================
# 48時間以上更新なし = 確実に停止している可能性
if [ -f "$LOG_FILE" ]; then
    MTIME=$(stat -f %m "$LOG_FILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    DIFF=$((NOW - MTIME))
    if [ $DIFF -gt 172800 ]; then
        ALERTS+=("⚠️ kelly_bot.logが$((DIFF/3600))時間更新なし - 停止中かも")
    fi
fi

# ================ 2. state.json から状況把握 ================
STATE_INFO=$(python3 -c "
import json
try:
    with open('$STATE_FILE') as f:
        s = json.load(f)
    capital = s.get('total_capital', 0)
    start_capital = s.get('start_capital', 3000)
    ret_pct = (capital / start_capital - 1) * 100 if start_capital > 0 else 0
    positions = list(s.get('positions', {}).keys())
    cooldown = s.get('cooldown_active', False)
    # 各ポジションのレバ表示
    lev_info = []
    for sym, p in s.get('positions', {}).items():
        lev_info.append(f'{sym}:lev{p.get(\"leverage\", 0)}x')
    lev_str = ','.join(lev_info) if lev_info else 'なし'
    print(f'{capital}|{ret_pct}|{len(positions)}|{lev_str}|{cooldown}')
except Exception as e:
    print(f'ERROR|{e}|0|ERR|False')
" 2>/dev/null)

IFS='|' read -r CAPITAL RET_PCT NPOS LEV_INFO COOLDOWN <<< "$STATE_INFO"

if [ "$CAPITAL" != "ERROR" ]; then
    # 残高大幅マイナス警告
    if (( $(echo "$RET_PCT < -10" | bc -l 2>/dev/null || echo 0) )); then
        ALERTS+=("🚨 Kelly Bot資本が初期比${RET_PCT}%と大幅マイナス！")
    fi
    # レバが非整数のチェック（0.5などの小数が混ざってたら修正漏れ）
    if echo "$LEV_INFO" | grep -qE "lev[0-9]+\.[0-9]+" 2>/dev/null; then
        ALERTS+=("⚠️ 非整数レバレッジを検出: $LEV_INFO")
    fi
    STATUS_INFO="資本\$${CAPITAL} (${RET_PCT}%) | ポジ${NPOS}件 ($LEV_INFO) | クールダウン:${COOLDOWN}"
fi

# ================ 3. 結果出力 ================
if [ ${#ALERTS[@]} -eq 0 ]; then
    echo "[$TIMESTAMP] ✅ Kelly Bot正常 | $STATUS_INFO"
else
    echo "[$TIMESTAMP] 🚨 Kelly Bot異常検知 ${#ALERTS[@]}件"
    for alert in "${ALERTS[@]}"; do
        echo "  $alert"
    done
    echo "  正常時情報: $STATUS_INFO"
fi
