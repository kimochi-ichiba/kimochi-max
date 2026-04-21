#!/bin/bash
# 気持ちマックス launchd インストーラ
# Mac再起動後も自動的に Webサーバー + demo_runner + health_monitor が起動

set -e

PLIST_DIR="/Users/sanosano/projects/kimochi-max/launchd"
TARGET_DIR="$HOME/Library/LaunchAgents"

echo "🚀 気持ちマックス launchd セットアップ開始"
echo ""

if [ ! -d "$TARGET_DIR" ]; then
    mkdir -p "$TARGET_DIR"
fi

# 既存プロセスを停止
echo "① 既存プロセスを停止中..."
pkill -f "demo_runner.py" 2>/dev/null || true
pkill -f "http.server 8080" 2>/dev/null || true
pkill -f "health_monitor.py" 2>/dev/null || true
sleep 1

# plistをコピー
echo "② plistを LaunchAgents にコピー..."
cp "$PLIST_DIR/com.sanosano.kimochimax.server.plist" "$TARGET_DIR/"
cp "$PLIST_DIR/com.sanosano.kimochimax.demo.plist" "$TARGET_DIR/"
cp "$PLIST_DIR/com.sanosano.kimochimax.health.plist" "$TARGET_DIR/"
echo "   ✅ com.sanosano.kimochimax.server.plist"
echo "   ✅ com.sanosano.kimochimax.demo.plist"
echo "   ✅ com.sanosano.kimochimax.health.plist"

# ロード (既存unload後再ロード)
echo "③ launchd にロード..."
launchctl unload "$TARGET_DIR/com.sanosano.kimochimax.server.plist" 2>/dev/null || true
launchctl unload "$TARGET_DIR/com.sanosano.kimochimax.demo.plist" 2>/dev/null || true
launchctl unload "$TARGET_DIR/com.sanosano.kimochimax.health.plist" 2>/dev/null || true
launchctl load "$TARGET_DIR/com.sanosano.kimochimax.server.plist"
launchctl load "$TARGET_DIR/com.sanosano.kimochimax.demo.plist"
launchctl load "$TARGET_DIR/com.sanosano.kimochimax.health.plist"
echo "   ✅ 3サービスをロード"

# 起動確認
sleep 3
echo "④ 起動確認..."
SERVER=$(launchctl list | grep -c "com.sanosano.kimochimax.server" || true)
DEMO=$(launchctl list | grep -c "com.sanosano.kimochimax.demo" || true)
HEALTH=$(launchctl list | grep -c "com.sanosano.kimochimax.health" || true)
[ "$SERVER" -ge 1 ] && echo "   ✅ Webサーバー: 起動中" || echo "   ❌ Webサーバー: 起動失敗"
[ "$DEMO" -ge 1 ] && echo "   ✅ デモランナー: 起動中" || echo "   ❌ デモランナー: 起動失敗"
[ "$HEALTH" -ge 1 ] && echo "   ✅ ヘルスモニター: 起動中" || echo "   ❌ ヘルスモニター: 起動失敗"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🎉 セットアップ完了！"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "これで Mac を再起動しても 気持ちマックス は自動起動します。"
echo ""
echo "📊 アクセス URL:"
echo "   MacBook: http://localhost:8080/"
echo "   iPhone : http://192.168.100.42:8080/"
echo ""
echo "🔧 管理コマンド:"
echo "   停止      : launchctl unload ~/Library/LaunchAgents/com.sanosano.kimochimax.*.plist"
echo "   demo再起動: launchctl kickstart -k gui/\$(id -u)/com.sanosano.kimochimax.demo"
echo "   health確認: launchctl list | grep kimochimax"
echo "   demoログ  : tail -f /tmp/kimochi_demo.log"
echo "   healthログ: tail -f /tmp/kimochi_health.log"
echo ""
echo "🔐 実取引モード (LIVE) の有効化方法:"
echo "   詳細は http://localhost:8080/live_setup.html を参照"
