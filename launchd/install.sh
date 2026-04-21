#!/bin/bash
# 気持ちマックス launchd インストーラ
# Mac再起動後も自動的に Webサーバー + demo_runner が起動するように設定

set -e

PLIST_DIR="/Users/sanosano/projects/kimochi-max/launchd"
TARGET_DIR="$HOME/Library/LaunchAgents"

echo "🚀 気持ちマックス launchd セットアップ開始"
echo ""

# ターゲットディレクトリ確認
if [ ! -d "$TARGET_DIR" ]; then
    mkdir -p "$TARGET_DIR"
fi

# 既存プロセスを停止 (plistによる管理に切り替える前)
echo "① 既存プロセスを停止中..."
pkill -f "demo_runner.py" 2>/dev/null || true
pkill -f "http.server 8080" 2>/dev/null || true
sleep 1

# plistをコピー
echo "② plistを LaunchAgents にコピー..."
cp "$PLIST_DIR/com.sanosano.kimochimax.server.plist" "$TARGET_DIR/"
cp "$PLIST_DIR/com.sanosano.kimochimax.demo.plist" "$TARGET_DIR/"
echo "   ✅ $TARGET_DIR/com.sanosano.kimochimax.server.plist"
echo "   ✅ $TARGET_DIR/com.sanosano.kimochimax.demo.plist"

# ロード (既存unload後再ロード)
echo "③ launchd にロード..."
launchctl unload "$TARGET_DIR/com.sanosano.kimochimax.server.plist" 2>/dev/null || true
launchctl unload "$TARGET_DIR/com.sanosano.kimochimax.demo.plist" 2>/dev/null || true
launchctl load "$TARGET_DIR/com.sanosano.kimochimax.server.plist"
launchctl load "$TARGET_DIR/com.sanosano.kimochimax.demo.plist"
echo "   ✅ サービスをロード"

# 起動確認
sleep 3
echo "④ 起動確認..."
SERVER_RUNNING=$(launchctl list | grep -c "com.sanosano.kimochimax.server" || true)
DEMO_RUNNING=$(launchctl list | grep -c "com.sanosano.kimochimax.demo" || true)
if [ "$SERVER_RUNNING" -ge 1 ]; then
    echo "   ✅ Webサーバー: 起動中"
else
    echo "   ❌ Webサーバー: 起動失敗"
fi
if [ "$DEMO_RUNNING" -ge 1 ]; then
    echo "   ✅ デモランナー: 起動中"
else
    echo "   ❌ デモランナー: 起動失敗"
fi

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
echo "   停止  : launchctl unload ~/Library/LaunchAgents/com.sanosano.kimochimax.*.plist"
echo "   再起動: launchctl kickstart -k gui/\$(id -u)/com.sanosano.kimochimax.demo"
echo "   状態  : launchctl list | grep kimochimax"
echo "   ログ  : tail -f /tmp/kimochi_demo.log"
