#!/bin/bash
# 気持ちマックス launchd アンインストーラ

set -e

TARGET_DIR="$HOME/Library/LaunchAgents"

echo "🛑 気持ちマックス launchd 停止・削除"

# unload
if [ -f "$TARGET_DIR/com.sanosano.kimochimax.server.plist" ]; then
    launchctl unload "$TARGET_DIR/com.sanosano.kimochimax.server.plist" 2>/dev/null || true
    rm "$TARGET_DIR/com.sanosano.kimochimax.server.plist"
    echo "   ✅ Webサーバー plist を削除"
fi

if [ -f "$TARGET_DIR/com.sanosano.kimochimax.demo.plist" ]; then
    launchctl unload "$TARGET_DIR/com.sanosano.kimochimax.demo.plist" 2>/dev/null || true
    rm "$TARGET_DIR/com.sanosano.kimochimax.demo.plist"
    echo "   ✅ デモランナー plist を削除"
fi

echo ""
echo "🎉 アンインストール完了"
echo ""
echo "今後は手動で起動してください:"
echo "  cd /Users/sanosano/projects/kimochi-max/results && nohup python3 -m http.server 8080 --bind 0.0.0.0 > /tmp/kimochi_server.log 2>&1 &"
echo "  cd /Users/sanosano/projects/kimochi-max && nohup python3 demo_runner.py > /tmp/kimochi_demo.log 2>&1 &"
