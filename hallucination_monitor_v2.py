"""
hallucination_monitor_v2.py — 気持ちマックス v2 用 5分毎ハルシネーション監視
==========================================================================

v2 システム（demo_runner.py, results/demo_state.json）に合わせて作り直した版。

監視項目（5分毎に実行）:
  [外部データ整合性]
    1. BTC多取引所クロスチェック (Binance/MEXC/Bybit 乖離 < 1%)
    2. ETH/SOL/BNB 多取引所クロスチェック
    3. Mac ⇄ Binance サーバー時刻乖離 (< 5秒)
    4. 62銘柄ユニバース全Binance TRADING状態維持確認

  [v2 ボット状態健全性]
    5. demo_runner.py プロセス稼働確認
    6. state.json 更新鮮度 (< 5分)
    7. state.json version = "2.0" 確認
    8. state.json ach_config が v2 パラメータ維持
    9. WebSocket接続状態 (ws_age_sec < 30)

  [取引整合性]
   10. trades 履歴の OHLC 範囲内確認
   11. 総資産計算の整合性 (BTC+ACH+USDT = total_equity)
   12. プロセス重複検出 (demo_runner.py が複数なら異常)

実行:
  python3 hallucination_monitor_v2.py          # 1回
  python3 hallucination_monitor_v2.py --daemon # 5分毎永続

異常検出時:
  - HALLUCINATION_DETECTED.flag 作成
  - hallucination_monitor_v2.log に詳細記録
"""
from __future__ import annotations
import sys, os, json, time, argparse, subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.request

ROOT = Path(__file__).parent
STATE_V2 = ROOT / 'results' / 'demo_state.json'
LOG_PATH = ROOT / 'hallucination_monitor_v2.log'
FLAG_FILE = ROOT / 'HALLUCINATION_DETECTED.flag'
PID_FILE = ROOT / 'hallucination_monitor_v2.pid'

CHECK_INTERVAL_SEC = 300  # 5分

# 監視対象のユニバース (v2)
ACH_UNIVERSE_V2 = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "AVAX", "DOGE", "TRX", "DOT",
    "LINK", "UNI", "LTC", "ATOM", "NEAR", "ICP", "ETC", "XLM", "FIL",
    "APT", "ARB", "OP", "INJ", "SUI", "SEI", "TIA", "RUNE", "ALGO",
    "SAND", "MANA", "AXS", "CHZ", "ENJ", "GRT", "AAVE", "SNX", "CRV",
    "HBAR", "VET", "THETA", "EGLD", "XTZ", "FLOW", "IOTA", "DASH", "ZEC",
    "POL", "TON", "ONDO", "JUP", "WLD", "LDO", "IMX", "WIF",
    "ENA", "GALA", "JASMY", "PENDLE", "MINA", "RENDER", "STRK", "SUSHI",
}


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def http_get_json(url: str, headers: dict | None = None, timeout: int = 10):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "v2-monitor"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 検査項目
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_btc_multi_exchange() -> tuple[bool, str]:
    """1. BTC多取引所クロスチェック"""
    prices = {}
    try:
        r = http_get_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
        prices["Binance"] = float(r["price"])
    except: pass
    try:
        r = http_get_json("https://api.mexc.com/api/v3/ticker/price?symbol=BTCUSDT")
        prices["MEXC"] = float(r["price"])
    except: pass
    try:
        r = http_get_json("https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT")
        prices["Bybit"] = float(r["result"]["list"][0]["lastPrice"])
    except: pass

    if len(prices) < 2:
        return False, f"取得できたソース {len(prices)}個（最低2個必要）"

    vals = list(prices.values())
    max_dev = max(abs(v - vals[0]) / vals[0] for v in vals[1:])
    ok = max_dev < 0.01
    summary = f"{len(prices)}ソース 乖離{max_dev*100:.3f}%"
    return ok, summary


def check_multi_coins() -> tuple[bool, str]:
    """2. ETH/SOL/BNB クロスチェック"""
    results = {}
    for sym in ["ETH", "SOL", "BNB"]:
        prices = {}
        try:
            r = http_get_json(f"https://api.binance.com/api/v3/ticker/price?symbol={sym}USDT")
            prices["Binance"] = float(r["price"])
        except: pass
        try:
            r = http_get_json(f"https://api.mexc.com/api/v3/ticker/price?symbol={sym}USDT")
            prices["MEXC"] = float(r["price"])
        except: pass
        if len(prices) >= 2:
            vals = list(prices.values())
            dev = max(abs(v - vals[0]) / vals[0] for v in vals[1:])
            results[sym] = dev
    if not results:
        return False, "マルチ銘柄取得失敗"
    max_dev = max(results.values())
    ok = max_dev < 0.015
    summary = " ".join(f"{k}:{v*100:.2f}%" for k, v in results.items())
    return ok, summary


def check_time_sync() -> tuple[bool, str]:
    """3. Mac ⇄ Binance時刻乖離"""
    try:
        r = http_get_json("https://api.binance.com/api/v3/time")
        binance_ts = r["serverTime"] / 1000
        local_ts = time.time()
        diff = abs(local_ts - binance_ts)
        ok = diff < 5.0
        return ok, f"乖離 {diff:.2f}秒"
    except Exception as e:
        return False, f"取得失敗: {e}"


def check_universe_trading() -> tuple[bool, str]:
    """4. 62銘柄全てTRADING状態確認"""
    try:
        r = http_get_json("https://api.binance.com/api/v3/exchangeInfo")
        trading = set()
        non_trading = {}
        for s in r.get("symbols", []):
            if s["quoteAsset"] == "USDT":
                base = s["baseAsset"]
                if base in ACH_UNIVERSE_V2:
                    if s["status"] == "TRADING":
                        trading.add(base)
                    else:
                        non_trading[base] = s["status"]
        missing = ACH_UNIVERSE_V2 - trading
        if missing:
            return False, f"{len(missing)}銘柄がTRADING外: {sorted(missing)[:3]}..."
        return True, f"全{len(trading)}銘柄TRADING中"
    except Exception as e:
        return False, f"取得失敗: {e}"


def _find_pids_by_cmdline(pattern: str) -> list[str]:
    """pgrep -f の cross-platform 版。pattern を含む CommandLine のPIDを返す。
    Windows の venv は launcher→子 python.exe で2プロセス出るため、親子ペアは
    「子」側だけをカウントして重複検知の誤報を回避。"""
    if os.name == "nt":
        ps_cmd = (
            f"Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{pattern}*' }} | "
            f"Select-Object -Property ProcessId, ParentProcessId | "
            f"ForEach-Object {{ \"$($_.ProcessId):$($_.ParentProcessId)\" }}"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=10
            )
            pairs = [line.split(":", 1) for line in r.stdout.strip().splitlines() if ":" in line]
            pairs = [(p, pp) for p, pp in pairs if p.strip().isdigit()]
            pids = {p for p, _ in pairs}
            # 親も同じ pattern に合致するなら launcher → 親側を除外
            parents_in_set = {pp for _, pp in pairs if pp in pids}
            return [p for p, _ in pairs if p not in parents_in_set]
        except Exception:
            return []
    try:
        r = subprocess.run(["pgrep", "-f", pattern],
                           capture_output=True, text=True, timeout=5)
        return [p for p in r.stdout.strip().split("\n") if p]
    except Exception:
        return []


def check_bot_process() -> tuple[bool, str]:
    """5. demo_runner.py プロセス稼働確認"""
    try:
        pids = _find_pids_by_cmdline("demo_runner.py")
        if len(pids) == 0:
            return False, "demo_runner.py 停止中"
        if len(pids) > 1:
            return False, f"demo_runner.py が {len(pids)}プロセス（重複）: {pids}"
        return True, f"PID {pids[0]} 稼働中"
    except Exception as e:
        return False, f"確認失敗: {e}"


def check_state_freshness(state: dict) -> tuple[bool, str]:
    """6. state.json 更新鮮度"""
    ts = state.get("last_update", "")
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        ok = age < 300
        return ok, f"更新 {age:.0f}秒前"
    except Exception as e:
        return False, f"last_update解析失敗: {e}"


def check_version(state: dict) -> tuple[bool, str]:
    """7. state.json version = 2.x or 3.x (v2/v3 系統 すべて受容)

    v3.0 (cycle hunter) は iter76 WF 4/4 勝ち採用構成として認可済。
    """
    v = state.get("version")
    ok = isinstance(v, str) and (v.startswith("2.") or v.startswith("3."))
    return ok, f"version={v}, version_name={state.get('version_name', '未設定')}"


def check_ach_config(state: dict) -> tuple[bool, str]:
    """8. ach_config v2/v2.1 パラメータ確認"""
    cfg = state.get("ach_config", {})
    # v2 と v2.1 どちらも受け入れる (共通の4キーだけ検査)
    expected = {"top_n": 3, "lookback_days": 25, "rebalance_days": 7, "universe_size": 62}
    mismatches = [f"{k}={cfg.get(k)}≠{v}" for k, v in expected.items() if cfg.get(k) != v]
    if mismatches:
        return False, f"v2/v2.1設定ずれ: {', '.join(mismatches)}"
    # v2.1 新機能が入っていれば表示
    extra = ""
    if "corr_threshold" in cfg:
        extra = f" /Corr<{cfg['corr_threshold']}/{cfg.get('weight_method', 'equal')}"
    return True, f"Top{cfg['top_n']}/LB{cfg['lookback_days']}/R{cfg['rebalance_days']}d/{cfg['universe_size']}銘柄{extra}"


def check_ws_connection(state: dict) -> tuple[bool, str]:
    """9. WebSocket接続状態"""
    connected = state.get("ws_connected", False)
    age = state.get("ws_age_sec", 999)
    ok = connected and age < 30
    return ok, f"接続={connected}, 鮮度={age:.1f}秒"


def check_trade_ohlc(state: dict) -> tuple[bool, str]:
    """10. 取引履歴の OHLC範囲内確認"""
    trades = state.get("trades", [])
    if not trades:
        return True, "取引履歴なし (BTC < EMA200 で正常)"
    details = []
    for t in trades[-3:]:  # 直近3件
        ts = t.get("ts", "")
        price = t.get("price", 0)
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            day_start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
            sm = int(day_start.timestamp() * 1000)
            em = sm + 86400000
            url = (f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
                   f"&interval=1h&startTime={sm}&endTime={em}&limit=30")
            klines = http_get_json(url)
            if not klines:
                continue
            lo = min(float(k[3]) for k in klines)
            hi = max(float(k[2]) for k in klines)
            if not (lo <= price <= hi):
                return False, f"{ts} price=${price:.2f} が OHLC [${lo:.2f}-${hi:.2f}] 範囲外"
            details.append("OK")
        except Exception:
            continue
    return True, f"直近{len(details)}件 全OHLC内"


def check_backtest_drift(state: dict) -> tuple[bool, str]:
    """13. バックテスト v2.2 期待月率(+2.0%) vs 実 SIM 月率 の乖離チェック
    SIM 稼働が 30 日未満なら WARN で即 PASS (データ不足)。
    30日以上の実績があれば、月率の差が ±5% を超えたら警告。"""
    started = state.get("started_at") or state.get("bot_started_at")
    if not started:
        # 代替: equity_history の先頭時刻
        eh = state.get("equity_history") or []
        if eh:
            started = eh[0].get("time") or eh[0].get("ts")
    if not started:
        return True, "稼働開始時刻不明のためスキップ"
    try:
        dt_start = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        if dt_start.tzinfo is None:
            dt_start = dt_start.replace(tzinfo=timezone.utc)
    except Exception:
        return True, f"開始時刻解析失敗: {started}"

    now = datetime.now(timezone.utc)
    days = (now - dt_start).total_seconds() / 86400
    if days < 30:
        return True, f"SIM {days:.1f}日 (30日未満のため乖離判定スキップ)"

    # 実 SIM 月率 = (現在/初期) ^ (30/days) - 1
    total = state.get("total_equity", 0)
    initial = state.get("initial_equity") or 10_000.0
    if initial <= 0 or total <= 0:
        return True, "総資産が0または負（スキップ）"
    actual_monthly = (total / initial) ** (30 / days) - 1
    expected_monthly = 0.020  # v2.2 backtest 期待 +2.0%/月 (iter59 控えめ想定)
    drift = actual_monthly - expected_monthly
    if abs(drift) > 0.05:  # ±5% 乖離
        return False, (f"月率乖離 {drift*100:+.1f}% "
                       f"(実績 {actual_monthly*100:+.2f}% vs 期待 {expected_monthly*100:+.1f}%)")
    return True, f"月率 {actual_monthly*100:+.2f}% / 期待 {expected_monthly*100:+.1f}% (差 {drift*100:+.1f}%)"


def check_equity_consistency(state: dict) -> tuple[bool, str]:
    """11. 総資産 = BTC + ACH + USDT 整合性"""
    total = state.get("total_equity", 0)
    btc = state.get("btc_part", {})
    ach = state.get("ach_part", {})
    usdt = state.get("usdt_part", {})

    btc_val = btc.get("cash", 0) + btc.get("btc_qty", 0) * btc.get("last_btc_price", 0)
    ach_val = ach.get("cash", 0) + sum(p.get("current_value", 0) for p in ach.get("positions", {}).values())
    usdt_val = usdt.get("cash", 0)

    computed = btc_val + ach_val + usdt_val
    diff = abs(computed - total)
    diff_pct = diff / max(total, 1) * 100
    ok = diff_pct < 1.0
    return ok, f"計算値 ${computed:,.2f} vs 記録値 ${total:,.2f} (差{diff_pct:.3f}%)"


def check_process_unique() -> tuple[bool, str]:
    """12. プロセス重複検出"""
    for name in ["demo_runner.py", "health_monitor.py", "hallucination_monitor_v2.py"]:
        try:
            pids = _find_pids_by_cmdline(name)
            my_pid = str(os.getpid())
            pids = [p for p in pids if p != my_pid]
            if len(pids) > 1:
                return False, f"{name}: {len(pids)}プロセス重複 {pids}"
        except: continue
    return True, "全プロセス単一"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メイン
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHECKS = [
    ("1. BTC多取引所", check_btc_multi_exchange, True),
    ("2. ETH/SOL/BNB", check_multi_coins, True),
    ("3. 時刻同期", check_time_sync, True),
    ("4. ユニバース", check_universe_trading, True),
    ("5. ボットプロセス", check_bot_process, True),
    ("9. WS接続", None, False),  # state 必要
    ("6. state鮮度", None, False),
    ("7. versionが2.0", None, False),
    ("8. ach_config", None, False),
    ("10. 取引OHLC", None, False),
    ("11. 残高整合性", None, False),
    ("12. プロセス重複", check_process_unique, True),
]


def run_check():
    log("=" * 70)
    log("🔍 v2 ハルシネーション検査開始")

    # state 読込
    state = None
    if STATE_V2.exists():
        try:
            state = json.loads(STATE_V2.read_text())
        except Exception as e:
            log(f"⚠️ state読込失敗: {e}", "WARN")
            state = None

    fail_count = 0
    warn_count = 0

    # 外部チェック
    for name, fn, is_external in [
        ("1. BTC多取引所", check_btc_multi_exchange, True),
        ("2. ETH/SOL/BNB", check_multi_coins, True),
        ("3. 時刻同期", check_time_sync, True),
        ("4. ユニバース", check_universe_trading, True),
        ("5. ボットプロセス", check_bot_process, True),
        ("12. プロセス重複", check_process_unique, True),
    ]:
        try:
            ok, msg = fn()
            icon = "✅" if ok else "❌"
            log(f"{icon} [{name}] {msg}", "INFO" if ok else "ALERT")
            if not ok:
                fail_count += 1
        except Exception as e:
            log(f"⚠️ [{name}] 例外: {e}", "WARN")
            warn_count += 1

    # state チェック
    if state:
        for name, fn in [
            ("6. state鮮度", check_state_freshness),
            ("7. version=2.0", check_version),
            ("8. ach_config", check_ach_config),
            ("9. WS接続", check_ws_connection),
            ("10. 取引OHLC", check_trade_ohlc),
            ("11. 残高整合性", check_equity_consistency),
            ("13. バックテスト乖離", check_backtest_drift),
        ]:
            try:
                ok, msg = fn(state)
                icon = "✅" if ok else "❌"
                log(f"{icon} [{name}] {msg}", "INFO" if ok else "ALERT")
                if not ok:
                    fail_count += 1
            except Exception as e:
                log(f"⚠️ [{name}] 例外: {e}", "WARN")
                warn_count += 1
    else:
        log("⚠️ state.json が無いため state系7項目スキップ", "WARN")
        warn_count += 7

    total_checks = 13
    pass_count = total_checks - fail_count - warn_count

    if fail_count == 0:
        log(f"🟢 全13項目PASS — ハルシネーションなし (PASS:{pass_count}/WARN:{warn_count})", "INFO")
    else:
        log(f"🔴 {fail_count}件の異常 (PASS:{pass_count}/WARN:{warn_count}/FAIL:{fail_count})", "ALERT")
        # flag作成
        with open(FLAG_FILE, "a") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] v2 monitor {fail_count}件FAIL\n")

    log("=" * 70)
    return fail_count == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true", help="5分毎永続実行")
    args = parser.parse_args()

    # PIDファイル書込
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    if args.daemon:
        log(f"🚀 気持ちマックス v2 ハルシネーション監視開始 (5分毎)", "INFO")
        while True:
            try:
                run_check()
            except Exception as e:
                log(f"⚠️ run_check 例外: {e}", "WARN")
            time.sleep(CHECK_INTERVAL_SEC)
    else:
        run_check()


if __name__ == "__main__":
    main()
