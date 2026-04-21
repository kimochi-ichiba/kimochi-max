#!/bin/bash
# 気持ちマックス 観察用クイックステータス表示
# 使い方: bash /Users/sanosano/projects/kimochi-max/kimochimax_status.sh

STATE_FILE="/Users/sanosano/projects/kimochi-max/results/demo_state.json"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🤖 気持ちマックス Bot クイックステータス"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ① launchd
echo "① 自動起動 (launchd) 状態:"
LAUNCHD_OUT=$(launchctl list | grep kimochimax || true)
if [ -z "$LAUNCHD_OUT" ]; then
    echo "   ❌ launchd未登録 (bash launchd/install.sh で登録)"
else
    echo "$LAUNCHD_OUT" | awk '{printf "   ✅ %s (PID: %s)\n", $3, $1}'
fi
echo ""

# ② プロセス
echo "② プロセス状態:"
SERVER_PID=$(pgrep -f "http.server 8080" | head -1)
DEMO_PID=$(pgrep -f "demo_runner.py" | head -1)
echo "   Webサーバー:    ${SERVER_PID:-❌停止}"
echo "   デモランナー:   ${DEMO_PID:-❌停止}"
echo ""

# ③ state概要
echo "③ 現在の資産状況:"
python3 -c "
import json, sys
from datetime import datetime, timezone
try:
    s = json.load(open('$STATE_FILE'))
    total = s['total_equity']
    initial = s['initial_capital']
    pnl = total - initial
    pnl_pct = (total/initial - 1) * 100
    btc = s['btc_part']
    ach = s['ach_part']
    usdt = s['usdt_part']
    last = datetime.fromisoformat(s['last_update'].replace('Z', '+00:00'))
    if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
    age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
    sign = '+' if pnl >= 0 else ''
    print(f'   💰 総資産:        \${total:,.2f} ({sign}\${pnl:,.2f} / {sign}{pnl_pct:+.3f}%)')
    print(f'   📊 ピーク:        \${s[\"peak_equity\"]:,.2f}')
    print(f'   📉 最大DD記録:    {s[\"max_dd_observed\"]:.2f}%')
    print(f'   🔄 Tick数:        {s[\"ticks_processed\"]}回')
    print(f'   📝 取引数:        {len(s[\"trades\"])}件')
    print(f'   🕐 最終更新:      {age_min:.1f}分前')
    print()
    print('   【内訳】')
    btc_val = btc['cash'] + btc['btc_qty'] * btc['last_btc_price']
    print(f'   ₿  BTC (40%):     \${btc_val:,.2f}  シグナル: {btc[\"last_signal\"]}')
    ach_val = ach.get('virtual_equity', ach.get('cash', 0))
    n_pos = len(ach.get('positions', {}))
    strat = ach.get('strategy', '理論値')
    print(f'   ⚡ ACH (40%):     \${ach_val:,.2f}  戦略: {strat} / ポジ{n_pos}件')
    if ach.get('last_top3'):
        tops = ', '.join([f'{t[\"symbol\"]}+{t[\"return_pct\"]:.0f}%' for t in ach['last_top3']])
        print(f'      Top3選定: {tops}')
    print(f'   💵 USDT (20%):    \${usdt[\"cash\"]:,.2f}  (年3%金利)')
    print()
    print(f'   【市場】')
    print(f'   BTC価格:   \${btc[\"last_btc_price\"]:,.2f}')
    print(f'   EMA200:    \${btc[\"last_ema200\"]:,.2f}')
    gap = btc['last_btc_price'] - btc['last_ema200']
    gap_pct = gap / btc['last_ema200'] * 100
    if gap > 0:
        print(f'   → BTC ＞ EMA200 (+{gap_pct:.2f}%) 📈 買いシグナル活性')
    else:
        print(f'   → BTC ＜ EMA200 ({gap_pct:.2f}%) ⏸ 現金待機中')
except Exception as e:
    print(f'   ⚠️ 読込失敗: {e}')
"
echo ""

# ④ 最新ログ
echo "④ 最新ログ (5行):"
tail -5 /tmp/kimochi_demo.log 2>/dev/null | sed 's/^/   /' || echo "   (ログなし)"
echo ""

# ⑤ アクセスURL
echo "⑤ アクセスURL:"
echo "   🖥  http://localhost:8080/"
echo "   📱  http://192.168.100.42:8080/"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
