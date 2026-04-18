#!/bin/bash
# Kelly Bot Monitor を15分毎のcronに登録

BOT_DIR="/Users/sanosano/projects/crypto-bot-pro"
PYTHON_PATH=$(which python3)
CRON_LINE="*/15 * * * * cd $BOT_DIR && $PYTHON_PATH monitor_kelly_bot.py >> $BOT_DIR/monitor_cron.log 2>&1"

echo "🤖 Kelly Bot Monitor 自動化セットアップ"
echo "========================================"
echo ""
echo "Python: $PYTHON_PATH"
echo "Dir:    $BOT_DIR"
echo ""
echo "追加するcron行:"
echo "  $CRON_LINE"
echo ""

# 既に登録されているかチェック
if crontab -l 2>/dev/null | grep -q "monitor_kelly_bot.py"; then
    echo "⚠️ 既に登録されています。上書きする場合は手動で:"
    echo "   crontab -e で既存行を削除してから再実行してください。"
    echo ""
    echo "現在の登録:"
    crontab -l 2>/dev/null | grep "monitor_kelly_bot.py"
    exit 1
fi

# 登録
(crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
echo "✅ 15分毎の自動実行を登録しました"
echo ""
echo "確認:"
crontab -l | grep monitor
echo ""
echo "📝 ログ確認:"
echo "  tail -f $BOT_DIR/monitor.log         # 全ログ"
echo "  tail -f $BOT_DIR/monitor_alerts.log  # 警告のみ"
echo "  tail -f $BOT_DIR/monitor_cron.log    # Cron出力"
echo ""
echo "🛑 停止するには:"
echo "  crontab -e で該当行を削除"
