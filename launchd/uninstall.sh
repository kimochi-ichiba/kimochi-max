#!/bin/bash
# 気持ちマックス launchd アンインストーラ

set -e

TARGET_DIR="$HOME/Library/LaunchAgents"

echo "🛑 気持ちマックス launchd 停止・削除"

for svc in server demo health; do
    plist="$TARGET_DIR/com.sanosano.kimochimax.${svc}.plist"
    if [ -f "$plist" ]; then
        launchctl unload "$plist" 2>/dev/null || true
        rm "$plist"
        echo "   ✅ ${svc} plist を削除"
    fi
done

echo ""
echo "🎉 アンインストール完了"
echo ""
echo "今後は手動で起動してください:"
echo "  cd /Users/sanosano/projects/kimochi-max/results && nohup python3 -m http.server 8080 --bind 0.0.0.0 > /tmp/kimochi_server.log 2>&1 &"
echo "  cd /Users/sanosano/projects/kimochi-max && nohup python3 demo_runner.py > /tmp/kimochi_demo.log 2>&1 &"
echo "  cd /Users/sanosano/projects/kimochi-max && nohup python3 health_monitor.py > /tmp/kimochi_health.log 2>&1 &"
