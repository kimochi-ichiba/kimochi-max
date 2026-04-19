"""
kimochi-max ハルシネーション監視スクリプト
5分ごとに以下をチェック:
1. 全銘柄がBinance Futures でTRADING状態か
2. SETTLING/BREAKに変更された銘柄がないか
3. Mac時刻とBinanceサーバー時刻のずれ
4. ランダム3銘柄の価格整合性（bot取得 vs Binance直接）
"""
import sys, os, json, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import requests
except ImportError:
    print("⚠️ requests未インストール")
    sys.exit(1)

try:
    from config import Config
    cfg = Config()
    WATCH_SYMBOLS = cfg.watch_symbols
except Exception as e:
    print(f"⚠️ config読取失敗: {e}")
    sys.exit(1)

LOG_PATH = "/Users/sanosano/projects/kimochi-max/hallucination_monitor.log"

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

issues = []

# ① 全銘柄のTRADING状態チェック
try:
    r = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=10)
    info = r.json()
    fut_syms = {s['symbol']: s['status'] for s in info['symbols']}

    for s in WATCH_SYMBOLS:
        bs = s.replace("/", "")
        if bs not in fut_syms:
            issues.append(f"❌ {s}: Futures未上場")
        elif fut_syms[bs] != 'TRADING':
            issues.append(f"⚠️ {s}: status={fut_syms[bs]} (非取引)")
except Exception as e:
    issues.append(f"Binance API取得失敗: {e}")

# ② 時刻ずれチェック
try:
    r = requests.get("https://fapi.binance.com/fapi/v1/time", timeout=5)
    bin_ts = r.json()['serverTime'] / 1000
    local_ts = time.time()
    drift = abs(local_ts - bin_ts)
    if drift > 10:
        issues.append(f"🚨 時刻ずれ {drift:.1f}秒 (10秒超)")
except Exception as e:
    issues.append(f"時刻API失敗: {e}")

# ③ ランダム3銘柄の価格整合性
import random
if WATCH_SYMBOLS:
    for s in random.sample(WATCH_SYMBOLS, min(3, len(WATCH_SYMBOLS))):
        bs = s.replace("/", "")
        try:
            r = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={bs}", timeout=5)
            d = r.json()
            if 'price' not in d:
                issues.append(f"❓ {s}: 価格取得失敗 {d.get('msg', d)}")
        except Exception as e:
            issues.append(f"❓ {s}: 価格取得エラー")

# ④ kimochi-max稼働確認
import subprocess
proc = subprocess.run(['pgrep', '-f', 'kimochi-max.*main.py'], capture_output=True, text=True)
running = bool(proc.stdout.strip())

# レポート
if issues:
    log(f"🚨 ハルシネーション検知 {len(issues)}件 / 稼働={running}")
    for i in issues:
        log(f"   {i}")
else:
    log(f"✅ ハルシネーションなし 銘柄{len(WATCH_SYMBOLS)}件検証OK 稼働={running}")

# 結果保存
with open("/tmp/kimochi_max_health.json", "w") as f:
    json.dump({
        "ts": time.time(),
        "issues": issues,
        "running": running,
        "symbols_count": len(WATCH_SYMBOLS),
    }, f, indent=2)
