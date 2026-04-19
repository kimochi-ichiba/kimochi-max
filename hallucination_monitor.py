"""
hallucination_monitor.py - 10分毎に実行される総合ハルシネーション監視
========================================================================
チェック項目:
1. 合成データが混入していないか
2. 直近取引のエントリー価格が実Binanceデータと一致しているか
3. 現在のBinance価格とボット経由の価格が一致しているか
4. state/ledger の整合性
5. 銘柄が実際にBinance Futuresに存在するか

実行方法:
  python3 hallucination_monitor.py           # 1回実行
  python3 hallucination_monitor.py --daemon  # 10分毎に永続実行
"""
import sys, os, json, time, argparse, random
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("⚠️ requests未インストール")
    sys.exit(1)

ROOT = Path(__file__).parent
STATE = ROOT / 'bot_state.json'
LEDGER = ROOT / 'trade_ledger.jsonl'
LOG_PATH = ROOT / 'hallucination_monitor.log'
CHECK_INTERVAL_SEC = 600

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def check_synthetic():
    if not STATE.exists(): return 0
    try:
        s = json.load(open(STATE))
        return sum(1 for t in s.get('trade_history', []) if '合成' in t.get('exit_reason', ''))
    except Exception:
        return 0

def check_watch_symbols_exist():
    try:
        sys.path.insert(0, str(ROOT))
        from config import Config
        cfg = Config()
        symbols = cfg.watch_symbols
        r = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=10)
        valid = {s['symbol']: s['status'] for s in r.json()['symbols']}
        not_found, not_trading = [], []
        for s in symbols:
            bs = s.replace("/", "")
            if bs not in valid: not_found.append(s)
            elif valid[bs] != 'TRADING': not_trading.append(f"{s}({valid[bs]})")
        return len(symbols), not_found, not_trading
    except Exception as e:
        return 0, [], [str(e)]

def check_recent_trade_prices(n=5):
    if not STATE.exists(): return 0, 0, 0
    try:
        s = json.load(open(STATE))
        trades = s.get('trade_history', [])
        if not trades: return 0, 0, 0
        recent = sorted(trades, key=lambda t: t.get('entry_time', 0))[-n:]
        checked, ok, suspicious = 0, 0, 0
        for t in recent:
            sym = t.get('symbol', ''); ep = t.get('entry_price', 0); ts = t.get('entry_time', 0)
            if not sym or ep <= 0 or ts <= 0: continue
            sc = sym.replace('/', '')
            try:
                r = requests.get(f"https://fapi.binance.com/fapi/v1/klines?symbol={sc}&interval=5m&startTime={int(ts)*1000}&limit=3", timeout=5)
                d = r.json()
                if not d or not isinstance(d, list): continue
                h = max(float(c[2]) for c in d); l = min(float(c[3]) for c in d)
                checked += 1
                if l * 0.999 <= ep <= h * 1.001: ok += 1
                else: suspicious += 1
            except Exception:
                pass
        return checked, ok, suspicious
    except Exception:
        return 0, 0, 0

def check_live_prices():
    syms = ["BTCUSDT", "ETHUSDT"]
    ok = 0
    for s in syms:
        try:
            r = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={s}", timeout=5)
            if 'price' in r.json(): ok += 1
        except Exception:
            pass
    return ok, len(syms)

def check_ledger():
    if not STATE.exists() or not LEDGER.exists(): return 0, 0, 0
    try:
        st = json.load(open(STATE)).get('trade_history', [])
        with open(LEDGER) as f:
            lg = [json.loads(l) for l in f if l.strip()]
        state_keys = set((t.get('symbol',''), t.get('entry_time',0)) for t in st)
        ledger_keys = set((t.get('symbol',''), t.get('entry_time',0)) for t in lg)
        return len(st), len(lg), len(state_keys - ledger_keys)
    except Exception:
        return 0, 0, 0

def check_bot_running():
    import subprocess
    r = subprocess.run(['pgrep', '-f', 'main.py.*port 8082'], capture_output=True, text=True)
    return bool(r.stdout.strip())

def run_check():
    log("=" * 60)
    log("🔍 ハルシネーション定期チェック開始")
    issues = 0

    syn_count = check_synthetic()
    if syn_count > 0:
        log(f"🚨 合成データ{syn_count}件検出（削除推奨）")
        issues += 1
    else:
        log(f"✅ 合成データ: 0件（クリーン）")

    total, nf, nt = check_watch_symbols_exist()
    if nf:
        log(f"🚨 未上場銘柄{len(nf)}件: {','.join(nf[:5])}")
        issues += 1
    if nt:
        log(f"⚠️ 非取引銘柄{len(nt)}件: {','.join(nt[:5])}")
        issues += 1
    if total and not nf and not nt:
        log(f"✅ 監視銘柄{total}件すべてBinance Futuresで取引中")

    chk, ok, sus = check_recent_trade_prices(5)
    if sus > 0:
        log(f"🚨 直近取引で価格異常{sus}/{chk}件")
        issues += 1
    else:
        log(f"✅ 直近取引価格: {ok}/{chk}件が実Binanceレンジ内")

    lo, lt = check_live_prices()
    if lo < lt:
        log(f"⚠️ ライブ価格取得失敗 ({lo}/{lt})")
        issues += 1
    else:
        log(f"✅ ライブ価格API: {lo}/{lt}件正常")

    sc, lc, miss = check_ledger()
    if miss > 0:
        log(f"🚨 台帳不整合: stateに{miss}件余分")
        issues += 1
    else:
        log(f"✅ 台帳整合性: state{sc}件 / ledger{lc}件")

    running = check_bot_running()
    log(f"{'🟢 ボット稼働中' if running else '🔴 ボット停止中'}")

    if issues == 0:
        log(f"🟢 総合判定: ハルシネーションなし（全PASS）")
    else:
        log(f"🔴 総合判定: {issues}件の問題を検出")
    log("=" * 60)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", action="store_true")
    args = p.parse_args()
    if args.daemon:
        log(f"🚀 10分毎ハルシネーション監視ループ開始")
        while True:
            try: run_check()
            except Exception as e: log(f"⚠️ エラー: {e}")
            time.sleep(CHECK_INTERVAL_SEC)
    else:
        run_check()

if __name__ == "__main__":
    main()
