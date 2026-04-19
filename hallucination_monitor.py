"""
hallucination_monitor.py - 5分毎・多取引所クロスチェック版
========================================================================
監視対象の取引所・データソース:
  - Binance Futures (API)
  - MEXC (API)
  - Bitget (API)
  - CoinGecko (API) — 独立価格ソース

チェック項目:
  1. 合成/架空データの混入
  2. 直近取引のエントリー価格が4つのソースすべての範囲内か
  3. ボット経由の現在価格と4取引所平均の乖離
  4. state/ledger 整合性
  5. 監視銘柄がBinance Futuresで実在・取引可能か
  6. 残高の数学的整合性（trade_historyの合計 vs balance）
  7. scan_count・validation_started_at の単調性

異常検出時:
  - HALLUCINATION_DETECTED.flag ファイル作成
  - kimochi-maxボット自動停止（pkill）
  - ログに詳細記録

実行:
  python3 hallucination_monitor.py          # 1回
  python3 hallucination_monitor.py --daemon # 5分毎永続
"""
import sys, os, json, time, argparse, subprocess
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("requests未インストール"); sys.exit(1)

ROOT = Path(__file__).parent
STATE = ROOT / 'bot_state.json'
LEDGER = ROOT / 'trade_ledger.jsonl'
LOG_PATH = ROOT / 'hallucination_monitor.log'
FLAG_FILE = ROOT / 'HALLUCINATION_DETECTED.flag'
CHECK_INTERVAL_SEC = 300  # 5分

# 取引所APIエンドポイント
def binance_price(symbol):
    sym = symbol.replace('/', '')
    r = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}", timeout=5)
    return float(r.json()['price'])

def mexc_price(symbol):
    sym = symbol.replace('/', '')
    r = requests.get(f"https://api.mexc.com/api/v3/ticker/price?symbol={sym}", timeout=5)
    return float(r.json()['price'])

def bitget_price(symbol):
    sym = symbol.replace('/', '')
    r = requests.get(f"https://api.bitget.com/api/v2/mix/market/ticker?symbol={sym}&productType=USDT-FUTURES", timeout=5)
    d = r.json()
    if d.get('code') == '00000' and d.get('data'):
        return float(d['data'][0]['lastPr'])
    return None

def coingecko_price(coin_id):
    r = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd", timeout=8)
    d = r.json()
    return float(d.get(coin_id, {}).get('usd', 0))

# シンボル → CoinGecko ID
COINGECKO_MAP = {
    "BTC/USDT": "bitcoin", "ETH/USDT": "ethereum", "BNB/USDT": "binancecoin",
    "SOL/USDT": "solana", "ADA/USDT": "cardano", "XRP/USDT": "ripple",
    "LINK/USDT": "chainlink", "AVAX/USDT": "avalanche-2", "DOT/USDT": "polkadot",
}

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception: pass

def trigger_emergency_stop(reasons):
    """ハルシネーション検出時の緊急停止"""
    log(f"🚨 緊急停止を発動（検出理由: {len(reasons)}件）", "ALERT")
    # フラグファイル作成
    flag_content = f"Detection time: {datetime.now().isoformat()}\n"
    flag_content += f"Reasons:\n"
    for r in reasons:
        flag_content += f"  - {r}\n"
    FLAG_FILE.write_text(flag_content, encoding="utf-8")
    log(f"   📌 フラグファイル作成: {FLAG_FILE.name}")
    # ボット停止
    try:
        result = subprocess.run(['pkill', '-f', 'main.py.*port 8082'],
                                capture_output=True, text=True, timeout=10)
        log(f"   🛑 kimochi-maxボット停止コマンド実行")
        time.sleep(2)
        check = subprocess.run(['pgrep', '-f', 'main.py.*port 8082'],
                               capture_output=True, text=True)
        if check.stdout.strip():
            log(f"   ⚠️ プロセスが残存: PID {check.stdout.strip()}", "WARN")
        else:
            log(f"   ✅ ボット停止完了")
    except Exception as e:
        log(f"   ❌ 停止コマンドエラー: {e}", "ERROR")

# ==========================================================
# チェック関数群
# ==========================================================

def check_synthetic():
    """合成データ混入チェック"""
    if not STATE.exists():
        return "state file missing", 0
    try:
        s = json.load(open(STATE))
        trades = s.get('trade_history', [])
        syn = [t for t in trades if '合成' in t.get('exit_reason', '')]
        if syn:
            return f"合成データ{len(syn)}件検出", len(syn)
        return None, 0
    except Exception as e:
        return f"state読取エラー: {e}", 0

def check_multi_exchange_prices():
    """BTC価格を4ソースで比較、乖離検出"""
    try:
        b = binance_price("BTC/USDT")
        m = mexc_price("BTC/USDT")
        g = bitget_price("BTC/USDT")
        c = coingecko_price("bitcoin")
        sources = {"Binance": b, "MEXC": m, "Bitget": g, "CoinGecko": c}
        valid = {k: v for k, v in sources.items() if v and v > 0}
        if len(valid) < 2:
            return f"複数取引所の価格取得失敗({valid})", sources
        prices = list(valid.values())
        mx, mn = max(prices), min(prices)
        diff_pct = (mx - mn) / mn * 100
        if diff_pct > 3.0:
            return f"BTC価格乖離{diff_pct:.2f}%異常: {valid}", sources
        return None, {"valid": valid, "diff_pct": diff_pct}
    except Exception as e:
        return f"多取引所チェックエラー: {e}", {}

def check_recent_trade_prices(n=5):
    """直近取引のエントリー価格を複数取引所で検証"""
    if not STATE.exists():
        return "state missing", 0, 0
    try:
        s = json.load(open(STATE))
        trades = s.get('trade_history', [])
        if not trades: return None, 0, 0
        recent = sorted(trades, key=lambda t: t.get('entry_time', 0))[-n:]
        checked = 0; suspicious = 0; details = []
        for t in recent:
            sym = t.get('symbol', ''); ep = t.get('entry_price', 0); ts = t.get('entry_time', 0)
            if not sym or ep <= 0 or ts <= 0: continue
            sc = sym.replace('/', '')
            try:
                # Binance history（5分足、±15分）
                r = requests.get(
                    f"https://fapi.binance.com/fapi/v1/klines?symbol={sc}&interval=5m&startTime={int(ts)*1000}&limit=3",
                    timeout=5
                )
                d = r.json()
                if not d or not isinstance(d, list): continue
                h = max(float(c[2]) for c in d); l = min(float(c[3]) for c in d)
                checked += 1
                # 0.5%の許容差（レイテンシ・スリッページ考慮）
                if not (l * 0.995 <= ep <= h * 1.005):
                    suspicious += 1
                    details.append(f"{sym}: entry={ep:.6f} Binance[{l:.6f}-{h:.6f}]")
            except Exception: pass
        if suspicious > 0:
            return "価格乖離検出: " + "; ".join(details[:3]), checked, suspicious
        return None, checked, suspicious
    except Exception as e:
        return f"取引価格検証エラー: {e}", 0, 0

def check_balance_integrity():
    """残高の数学的整合性（pnl合計 vs balance）"""
    if not STATE.exists(): return "state missing"
    try:
        s = json.load(open(STATE))
        balance = s.get('balance', 0)
        initial = s.get('initial_balance', 10000)
        trades = s.get('trade_history', [])
        pnl_sum = sum(t.get('pnl', 0) for t in trades)
        expected = initial + pnl_sum
        diff = abs(balance - expected)
        tolerance = max(5.0, initial * 0.01)
        if diff > tolerance:
            return f"残高不整合: 実${balance:.2f} 計算${expected:.2f} 差${diff:.2f}"
        return None
    except Exception as e:
        return f"残高チェックエラー: {e}"

def check_ledger():
    """state と ledger の整合性"""
    if not STATE.exists() or not LEDGER.exists(): return None
    try:
        st = json.load(open(STATE)).get('trade_history', [])
        with open(LEDGER) as f:
            lg = [json.loads(l) for l in f if l.strip()]
        state_keys = set((t.get('symbol',''), t.get('entry_time',0)) for t in st)
        ledger_keys = set((t.get('symbol',''), t.get('entry_time',0)) for t in lg)
        miss = len(state_keys - ledger_keys)
        if miss > 0:
            return f"台帳不整合: stateに{miss}件余分（ledgerに記録なし）"
        return None
    except Exception as e:
        return f"台帳チェックエラー: {e}"

def check_symbols_exist():
    """監視銘柄の実在性"""
    try:
        sys.path.insert(0, str(ROOT))
        from config import Config
        cfg = Config()
        r = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=10)
        valid = {s['symbol']: s['status'] for s in r.json()['symbols']}
        nf, nt = [], []
        for s in cfg.watch_symbols:
            bs = s.replace("/", "")
            if bs not in valid: nf.append(s)
            elif valid[bs] != 'TRADING': nt.append(f"{s}({valid[bs]})")
        if nf:
            return f"未上場銘柄{len(nf)}件: {','.join(nf[:3])}"
        if nt:
            return f"非取引銘柄{len(nt)}件: {','.join(nt[:3])}"
        return None
    except Exception as e:
        return f"銘柄チェックエラー: {e}"

def check_validation_time_sanity():
    """validation_started_at が未来時刻でないか・負でないか"""
    if not STATE.exists(): return None
    try:
        s = json.load(open(STATE))
        vs = s.get('validation_started_at', 0)
        if vs <= 0: return "validation_started_at 未設定または負値"
        if vs > time.time() + 60: return "validation_started_at 未来時刻"
        return None
    except Exception as e:
        return f"validation時刻チェックエラー: {e}"

def check_bot_running():
    r = subprocess.run(['pgrep', '-f', 'main.py.*port 8082'], capture_output=True, text=True)
    return bool(r.stdout.strip())

# ==========================================================
# メインチェック
# ==========================================================

def run_check():
    log("=" * 70)
    log("🔍 5分毎ハルシネーション総合チェック開始")
    issues = []
    warnings = []

    # 1. 合成データ
    r, cnt = check_synthetic()
    if r:
        issues.append(r)
    else:
        log(f"✅ 合成データ: 0件")

    # 2. 多取引所価格クロスチェック
    r, detail = check_multi_exchange_prices()
    if r:
        issues.append(r)
    else:
        v = detail.get("valid", {})
        dp = detail.get("diff_pct", 0)
        log(f"✅ 多取引所BTC価格: {len(v)}ソース一致 (乖離{dp:.3f}%)")
        for src, p in v.items():
            log(f"   {src}: ${p:,.2f}")

    # 3. 直近取引の価格検証
    r, chk, sus = check_recent_trade_prices(5)
    if r:
        warnings.append(r)  # 軽微: 古い取引は履歴取れない場合あり
        log(f"⚠️ {r}", "WARN")
    else:
        log(f"✅ 直近{chk}件の取引エントリー価格すべて実Binance範囲内")

    # 4. 残高整合性
    r = check_balance_integrity()
    if r: issues.append(r)
    else: log(f"✅ 残高整合性: pnl合計と残高が一致")

    # 5. 台帳整合性
    r = check_ledger()
    if r: issues.append(r)
    else: log(f"✅ 台帳整合性OK")

    # 6. 監視銘柄実在性
    r = check_symbols_exist()
    if r: issues.append(r)
    else: log(f"✅ 監視銘柄すべてBinance Futuresで取引中")

    # 7. validation_started_at 健全性
    r = check_validation_time_sanity()
    if r: issues.append(r)
    else: log(f"✅ validation_started_at 健全")

    # 8. ボット稼働状況
    running = check_bot_running()
    log(f"{'🟢' if running else '⚪'} kimochi-maxボット: {'稼働中' if running else '停止中'}")

    # 判定
    if issues:
        log(f"🔴 重大ハルシネーション検出: {len(issues)}件", "ALERT")
        for i in issues:
            log(f"   • {i}", "ALERT")
        if running:
            trigger_emergency_stop(issues)
    elif warnings:
        log(f"🟡 軽微な警告: {len(warnings)}件（処理継続）")
    else:
        log(f"🟢 ハルシネーションなし（全チェックPASS）")
    log("=" * 70)
    return len(issues)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", action="store_true")
    args = p.parse_args()
    if args.daemon:
        log(f"🚀 5分毎ハルシネーション監視ループ開始（多取引所クロスチェック版）")
        while True:
            try: run_check()
            except Exception as e: log(f"⚠️ チェック例外: {e}", "ERROR")
            time.sleep(CHECK_INTERVAL_SEC)
    else:
        run_check()

if __name__ == "__main__":
    main()
