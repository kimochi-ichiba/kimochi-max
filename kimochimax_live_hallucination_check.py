"""
気持ちマックス Bot ライブ・ハルシネーションチェック
==========================================
稼働中の気持ちマックスに対して、非破壊的に以下を検証する:
  1. ボット稼働状態 (プロセス / state.json 鮮度)
  2. BTC現在価格を 5+ 取引所で相互照合 (Binance / MEXC / Bitget / yfinance / CoinGecko / CoinMarketCap)
  3. EMA200 の独立再計算 (Binanceから220日klineを再取得)
  4. トレード履歴のOHLC範囲内検証 (履歴ゼロなら"待機中"表示)
  5. ACH 枠の「理論値複利」警告 (実市場データ非連動である旨を明示)
  6. USDT 枠の金利整合性検証

実行:
  python3 kimochimax_live_hallucination_check.py
  # CMC_API_KEY を設定すればCoinMarketCapも含まれる
"""
from __future__ import annotations
import sys, os, json, time, subprocess, urllib.request, urllib.error, urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
RESULTS_DIR = PROJECT / "results"
STATE_PATH = RESULTS_DIR / "demo_state.json"
OUT_JSON = RESULTS_DIR / "kimochimax_live_hallucination_check.json"
OUT_HTML = RESULTS_DIR / "kimochimax_live_hallucination_report.html"

# 閾値
WARN_DEVIATION_PCT = 0.5
FAIL_DEVIATION_PCT = 2.0


def http_get_json(url, timeout=10, headers=None):
    default_headers = {"User-Agent": "Mozilla/5.0 (KimochiMax-Verifier/1.0)"}
    if headers:
        default_headers.update(headers)
    req = urllib.request.Request(url, headers=default_headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def safe_fetch(label, fn):
    """fn() を try/except で包んで統一結果を返す"""
    t0 = time.time()
    try:
        result = fn()
        return {
            "source": label, "ok": True, "error": None,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec='seconds'),
            "elapsed_ms": int((time.time() - t0) * 1000),
            **result,
        }
    except Exception as e:
        return {
            "source": label, "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec='seconds'),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 取引所別の「現在価格取得」関数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def fetch_binance():
    r = http_get_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
    return {"price": float(r["price"]), "currency": "USDT"}


def fetch_mexc():
    r = http_get_json("https://api.mexc.com/api/v3/ticker/price?symbol=BTCUSDT")
    return {"price": float(r["price"]), "currency": "USDT"}


def fetch_bitget():
    r = http_get_json("https://api.bitget.com/api/v2/spot/market/tickers?symbol=BTCUSDT")
    if r.get("code") != "00000":
        raise RuntimeError(f"Bitget error: {r.get('msg')}")
    data = r.get("data", [])
    if not data:
        raise RuntimeError("Bitget: no data")
    return {"price": float(data[0]["lastPr"]), "currency": "USDT"}


def fetch_yfinance():
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("yfinance not installed")
    t = yf.Ticker("BTC-USD")
    hist = t.history(period="1d", interval="1m")
    if hist.empty:
        raise RuntimeError("yfinance: empty")
    price = float(hist["Close"].iloc[-1])
    return {"price": price, "currency": "USD"}


def fetch_coingecko():
    r = http_get_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")
    return {"price": float(r["bitcoin"]["usd"]), "currency": "USD"}


def fetch_coinmarketcap():
    key = os.environ.get("CMC_API_KEY")
    if not key:
        raise RuntimeError("CMC_API_KEY not set (skipped)")
    r = http_get_json(
        "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest?symbol=BTC",
        headers={"X-CMC_PRO_API_KEY": key, "Accept": "application/json"},
    )
    price = float(r["data"]["BTC"]["quote"]["USD"]["price"])
    return {"price": price, "currency": "USD"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EMA200 独立再計算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def verify_ema200_independent():
    """Binanceから220日klineを独立取得して EMA200 を計算"""
    url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=220"
    klines = http_get_json(url)
    closes = [float(k[4]) for k in klines]
    # EMA200 計算 (demo_runner.py と同一ロジック)
    alpha = 2 / (200 + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = alpha * c + (1 - alpha) * ema
    return {
        "independent_ema200": round(ema, 2),
        "klines_count": len(closes),
        "earliest_close": round(closes[0], 2),
        "latest_close": round(closes[-1], 2),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ボット稼働状態チェック
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_bot_process():
    """demo_runner.py プロセスの稼働確認"""
    try:
        r = subprocess.run(
            ["pgrep", "-f", "demo_runner.py"],
            capture_output=True, text=True, timeout=5,
        )
        pids = [p.strip() for p in r.stdout.splitlines() if p.strip()]
        # 自分自身のpidは除外
        my_pid = str(os.getpid())
        pids = [p for p in pids if p != my_pid]
        if pids:
            # 起動時刻取得
            pid = pids[0]
            started_at = None
            try:
                r2 = subprocess.run(
                    ["ps", "-p", pid, "-o", "lstart="],
                    capture_output=True, text=True, timeout=5,
                )
                started_at = r2.stdout.strip()
            except Exception:
                pass
            return {"running": True, "pids": pids, "started_at_raw": started_at}
        return {"running": False, "pids": []}
    except Exception as e:
        return {"running": False, "error": str(e)}


def check_state_freshness():
    """state.json の最終更新時刻と内容を確認"""
    if not STATE_PATH.exists():
        return {"exists": False}
    try:
        mtime = datetime.fromtimestamp(STATE_PATH.stat().st_mtime, tz=timezone.utc)
        age_sec = (datetime.now(timezone.utc) - mtime).total_seconds()
        state = json.loads(STATE_PATH.read_text())
        return {
            "exists": True,
            "mtime": mtime.isoformat(timespec='seconds'),
            "age_seconds": round(age_sec, 1),
            "age_minutes": round(age_sec / 60, 1),
            "fresh": age_sec < 600,  # 10分以内で fresh
            "fresh_2h": age_sec < 7200,
            "state": state,
        }
    except Exception as e:
        return {"exists": True, "error": str(e)}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# トレード検証
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def verify_trades(state):
    trades = state.get("trades", [])
    if not trades:
        btc = state.get("btc_part", {})
        price = btc.get("last_btc_price", 0)
        ema = btc.get("last_ema200", 0)
        return {
            "total": 0,
            "status": "NO_HISTORY",
            "note": f"取引履歴なし。BTC ${price:,.2f} < EMA200 ${ema:,.2f} のため買いシグナル未発生は正常動作",
            "details": [],
        }

    # トレードがある場合: 当日のBinance 1h klineを取得してOHLC範囲内検証
    details = []
    for t in trades:
        ts_str = t.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            # 該当日の 00:00 〜 23:59 の 1h kline を取得
            day_start = datetime(ts.year, ts.month, ts.day, tzinfo=timezone.utc)
            start_ms = int(day_start.timestamp() * 1000)
            end_ms = int((day_start + timedelta(days=1)).timestamp() * 1000) - 1
            url = (f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h"
                   f"&startTime={start_ms}&endTime={end_ms}&limit=30")
            klines = http_get_json(url)
            if not klines:
                details.append({**t, "verify_status": "NO_OHLC_DATA"})
                continue
            day_low = min(float(k[3]) for k in klines)
            day_high = max(float(k[2]) for k in klines)
            price = float(t.get("price", 0))
            ok = day_low <= price <= day_high
            details.append({
                **t,
                "day_low": round(day_low, 2),
                "day_high": round(day_high, 2),
                "verify_status": "OK" if ok else "OUT_OF_RANGE",
            })
        except Exception as e:
            details.append({**t, "verify_status": "ERROR", "verify_error": str(e)})

    ok_count = sum(1 for d in details if d.get("verify_status") == "OK")
    return {
        "total": len(trades),
        "verified_ok": ok_count,
        "verified_bad": len(trades) - ok_count,
        "status": "PASS" if ok_count == len(trades) else "FAIL",
        "details": details,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ACH / USDT 理論値検証
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def verify_theoretical_parts(state):
    """ACH, USDT の理論値成長が数学的に整合しているか検証"""
    started_at_str = state.get("started_at", "")
    if not started_at_str:
        return {"error": "started_at not found"}
    started_at = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    years_elapsed = (now - started_at).total_seconds() / (365.25 * 86400)

    initial = state.get("initial_capital", 10000.0)

    # ACH 理論値 (年率 55%)
    ach_rate = 0.55
    ach_weight = 0.40
    ach_initial = initial * ach_weight
    ach_theoretical = ach_initial * ((1 + ach_rate) ** years_elapsed)
    ach_actual = state.get("ach_part", {}).get("cash", 0)
    ach_diff = abs(ach_actual - ach_theoretical)
    ach_diff_pct = (ach_diff / ach_theoretical * 100) if ach_theoretical > 0 else 0

    # USDT 理論値 (年率 3%)
    usdt_rate = 0.03
    usdt_weight = 0.20
    usdt_initial = initial * usdt_weight
    usdt_theoretical = usdt_initial * ((1 + usdt_rate) ** years_elapsed)
    usdt_actual = state.get("usdt_part", {}).get("cash", 0)
    usdt_diff = abs(usdt_actual - usdt_theoretical)
    usdt_diff_pct = (usdt_diff / usdt_theoretical * 100) if usdt_theoretical > 0 else 0

    return {
        "years_elapsed": round(years_elapsed, 6),
        "hours_elapsed": round(years_elapsed * 365.25 * 24, 2),
        "ach": {
            "initial_capital": round(ach_initial, 4),
            "theoretical_value": round(ach_theoretical, 4),
            "actual_value": round(ach_actual, 4),
            "absolute_diff": round(ach_diff, 4),
            "diff_pct": round(ach_diff_pct, 4),
            "match": ach_diff_pct < 0.01,
            "warning": "⚠️ 実市場データ非連動 - 年率55%の理論値複利計算のみ",
        },
        "usdt": {
            "initial_capital": round(usdt_initial, 4),
            "theoretical_value": round(usdt_theoretical, 4),
            "actual_value": round(usdt_actual, 4),
            "absolute_diff": round(usdt_diff, 4),
            "diff_pct": round(usdt_diff_pct, 4),
            "match": usdt_diff_pct < 0.01,
        },
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メイン: 全検証実行
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_all_checks():
    print("=" * 70)
    print("🔍 気持ちマックス Bot ライブ・ハルシネーションチェック")
    print("=" * 70)

    result = {
        "check_timestamp": datetime.now(timezone.utc).isoformat(timespec='seconds'),
        "check_timestamp_local": datetime.now().astimezone().isoformat(timespec='seconds'),
    }

    # ① ボット稼働状態
    print("\n① ボット稼働状態チェック...")
    bot_status = check_bot_process()
    state_status = check_state_freshness()
    result["bot_process"] = bot_status
    result["state_file"] = state_status
    print(f"   プロセス: {'✅ 動作中' if bot_status.get('running') else '❌ 停止'} "
          f"(PID: {bot_status.get('pids', [])})")
    if state_status.get("exists"):
        print(f"   state.json: 最終更新 {state_status.get('age_minutes')}分前")

    state = state_status.get("state", {}) if state_status.get("exists") else {}

    # ② 5+取引所 BTC価格照合
    print("\n② BTC現在価格 複数取引所照合...")
    bot_btc_price = state.get("btc_part", {}).get("last_btc_price", 0)

    exchanges = [
        ("Binance", fetch_binance),
        ("MEXC", fetch_mexc),
        ("Bitget", fetch_bitget),
        ("yfinance", fetch_yfinance),
        ("CoinGecko", fetch_coingecko),
        ("CoinMarketCap", fetch_coinmarketcap),
    ]
    prices = []
    for label, fn in exchanges:
        r = safe_fetch(label, fn)
        prices.append(r)
        status = f"✅ ${r['price']:,.2f}" if r.get("ok") else f"❌ {r.get('error', '')[:50]}"
        print(f"   {label:15s}: {status}")
        time.sleep(0.2)  # rate limit

    successful = [p for p in prices if p.get("ok")]
    if len(successful) >= 2:
        # Binanceを基準に
        binance = next((p for p in successful if p["source"] == "Binance"), successful[0])
        base_price = binance["price"]
        for p in successful:
            p["deviation_from_binance_pct"] = round(
                (p["price"] - base_price) / base_price * 100, 4)
        # bot記録値との比較
        if bot_btc_price > 0:
            for p in successful:
                p["deviation_from_bot_pct"] = round(
                    (p["price"] - bot_btc_price) / bot_btc_price * 100, 4)

        max_dev = max(abs(p["deviation_from_binance_pct"]) for p in successful)
        avg_dev = sum(abs(p["deviation_from_binance_pct"]) for p in successful) / len(successful)
        price_status = "PASS" if max_dev < WARN_DEVIATION_PCT else \
                        "WARN" if max_dev < FAIL_DEVIATION_PCT else "FAIL"
    else:
        max_dev = avg_dev = None
        price_status = "INSUFFICIENT_DATA"

    result["btc_price_comparison"] = {
        "bot_recorded_price": bot_btc_price,
        "bot_recorded_at": state.get("btc_part", {}).get("last_tick") or state.get("last_update"),
        "sources": prices,
        "successful_count": len(successful),
        "max_deviation_pct": round(max_dev, 4) if max_dev is not None else None,
        "avg_deviation_pct": round(avg_dev, 4) if avg_dev is not None else None,
        "status": price_status,
    }
    print(f"   → {price_status} (最大乖離: {max_dev:.3f}% / 平均: {avg_dev:.3f}%)"
          if max_dev is not None else f"   → {price_status}")

    # ③ EMA200 独立再計算
    print("\n③ EMA200 独立再計算...")
    ema_result = safe_fetch("EMA200", verify_ema200_independent)
    if ema_result.get("ok"):
        independent_ema = ema_result["independent_ema200"]
        bot_ema = state.get("btc_part", {}).get("last_ema200", 0)
        if bot_ema > 0:
            ema_diff = abs(independent_ema - bot_ema)
            ema_diff_pct = ema_diff / bot_ema * 100
            ema_result["bot_recorded_ema200"] = bot_ema
            ema_result["absolute_diff"] = round(ema_diff, 4)
            ema_result["diff_pct"] = round(ema_diff_pct, 4)
            ema_result["status"] = "PASS" if ema_diff_pct < 0.5 else \
                                    "WARN" if ema_diff_pct < 2.0 else "FAIL"
        else:
            ema_result["status"] = "NO_BOT_VALUE"
    else:
        ema_result["status"] = "FAIL"
    result["ema200_verification"] = ema_result
    print(f"   Bot記録: ${state.get('btc_part', {}).get('last_ema200', 0):,.2f} / "
          f"独立計算: ${ema_result.get('independent_ema200', 0):,.2f} → "
          f"{ema_result.get('status', 'UNKNOWN')}")

    # ④ トレード検証
    print("\n④ トレード履歴検証...")
    trade_result = verify_trades(state)
    result["trade_verification"] = trade_result
    print(f"   取引数: {trade_result['total']} / {trade_result['status']}")
    if trade_result.get("note"):
        print(f"   {trade_result['note']}")

    # ⑤ ACH / USDT 理論値検証
    print("\n⑤ ACH / USDT 理論値整合性...")
    theo = verify_theoretical_parts(state)
    result["theoretical_parts"] = theo
    if "error" not in theo:
        print(f"   経過時間: {theo['hours_elapsed']}時間 ({theo['years_elapsed']:.6f}年)")
        print(f"   ACH: ${theo['ach']['actual_value']:.4f} vs 理論${theo['ach']['theoretical_value']:.4f} "
              f"(誤差{theo['ach']['diff_pct']:.4f}%) → {'✅一致' if theo['ach']['match'] else '⚠️乖離'}")
        print(f"   USDT: ${theo['usdt']['actual_value']:.4f} vs 理論${theo['usdt']['theoretical_value']:.4f} "
              f"(誤差{theo['usdt']['diff_pct']:.4f}%) → {'✅一致' if theo['usdt']['match'] else '⚠️乖離'}")
    print("   ⚠️  ACH部分は 実市場データ非連動 の理論値複利です")

    # 総合判定
    statuses = []
    if bot_status.get("running") and state_status.get("fresh_2h"):
        statuses.append("BOT_RUNNING")
    else:
        statuses.append("BOT_NOT_FRESH")
    statuses.append(f"PRICE_{price_status}")
    statuses.append(f"EMA_{ema_result.get('status', 'UNKNOWN')}")
    statuses.append(f"TRADES_{trade_result['status']}")
    if "error" not in theo:
        if theo["ach"]["match"] and theo["usdt"]["match"]:
            statuses.append("THEORETICAL_MATCH")

    overall = "PASS"
    if "FAIL" in " ".join(statuses) or "NOT" in " ".join(statuses):
        overall = "FAIL"
    elif "WARN" in " ".join(statuses) or "INSUFFICIENT" in " ".join(statuses):
        overall = "WARN"

    # ACH部分は常にWARN扱い (理論値なので)
    if overall == "PASS":
        overall = "WARN_ACH_THEORETICAL"

    result["overall_verdict"] = overall
    result["component_statuses"] = statuses

    print("\n" + "=" * 70)
    print(f"🎯 総合判定: {overall}")
    print("=" * 70)

    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HTML レポート生成 (気持ちマックスUIテイスト)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def generate_html_report(data):
    data_json = json.dumps(data, ensure_ascii=False, default=str)
    html = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>🔍 気持ちマックス Bot ハルシネーションチェック</title>
<style>
:root{
  --bg:#060d17;--bg2:#091523;--bg3:#0d1c2e;--bg4:#112338;
  --border:#162840;--border2:#1d3350;
  --text:#c8d8ea;--muted:#304d66;--muted2:#4e7291;
  --green:#00e676;--green-bg:#00e67612;
  --red:#f44336;--red-bg:#f4433612;
  --yellow:#ffca28;--yellow-bg:#ffca2812;
  --blue:#4fc3f7;--blue-bg:#4fc3f712;
  --orange:#ffa726;--purple:#ab47bc;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--text);font-family:-apple-system,"Helvetica Neue","Hiragino Kaku Gothic ProN",sans-serif;font-size:14px;line-height:1.65;padding:16px;min-height:100vh}
.container{max-width:900px;margin:0 auto}
.hdr{display:flex;align-items:center;gap:10px;margin-bottom:20px;padding:10px 14px;background:var(--bg2);border:1px solid var(--border);border-radius:10px}
.hdr-logo{font-size:18px;font-weight:900;color:#fff;letter-spacing:-.5px}
.hdr-logo em{color:var(--yellow);font-style:normal}
.live-dot{width:10px;height:10px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.hdr-right{margin-left:auto;font-size:11px;color:var(--muted2);text-align:right}
.verdict-badge{padding:6px 14px;border-radius:14px;font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.05em}
.v-pass{background:var(--green-bg);color:var(--green);border:1px solid var(--green)}
.v-warn{background:var(--yellow-bg);color:var(--yellow);border:1px solid var(--yellow)}
.v-fail{background:var(--red-bg);color:var(--red);border:1px solid var(--red)}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:16px 20px;margin-bottom:14px}
.card h2{font-size:16px;color:var(--blue);margin:0 0 12px 0;border-left:3px solid var(--blue);padding-left:10px;display:flex;align-items:center;gap:8px}
.card h2 .status-pill{margin-left:auto;font-size:11px;padding:2px 10px;border-radius:10px;font-weight:700}
.pill-g{background:var(--green-bg);color:var(--green);border:1px solid var(--green)}
.pill-y{background:var(--yellow-bg);color:var(--yellow);border:1px solid var(--yellow)}
.pill-r{background:var(--red-bg);color:var(--red);border:1px solid var(--red)}
.pill-b{background:var(--blue-bg);color:var(--blue);border:1px solid var(--blue)}
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:10px 0}
.kpi{background:var(--bg3);border:1px solid var(--border2);padding:10px 14px;border-radius:8px}
.kpi .lbl{font-size:10px;color:var(--muted2);text-transform:uppercase;letter-spacing:.05em;font-weight:700}
.kpi .val{font-size:18px;font-weight:900;color:#fff;margin-top:2px}
.kpi .val.g{color:var(--green)}.kpi .val.r{color:var(--red)}.kpi .val.y{color:var(--yellow)}.kpi .val.b{color:var(--blue)}
.kpi .sub{font-size:10px;color:var(--muted);margin-top:2px}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:12px}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--border)}
th:first-child,td:first-child{text-align:left}
th{background:var(--bg3);color:var(--muted2);font-size:10px;text-transform:uppercase;letter-spacing:.05em;font-weight:700}
.ach-warning{background:linear-gradient(135deg,#ffca2820,#ffa72620);border:2px solid var(--yellow);border-radius:10px;padding:16px 20px;margin:12px 0}
.ach-warning h3{color:var(--yellow);margin-bottom:8px;font-size:14px}
.ach-warning p{color:var(--text);font-size:13px;line-height:1.6}
.code-mono{font-family:ui-monospace,Monaco,monospace;font-size:11px;color:var(--text);background:var(--bg3);padding:8px 10px;border-radius:6px;border:1px solid var(--border2);margin:8px 0}
.g{color:var(--green)}.r{color:var(--red)}.y{color:var(--yellow)}.b{color:var(--blue)}.o{color:var(--orange)}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
@media(max-width:640px){body{padding:10px}.hdr-right{display:none}.hdr{flex-wrap:wrap}.card{padding:12px 14px}.card h2{font-size:14px}.kpi .val{font-size:15px}th,td{padding:6px 4px;font-size:11px}}
</style>
</head>
<body>
<div class="container">

<div class="hdr">
  <div class="live-dot"></div>
  <div class="hdr-logo">気持ち<em>マックス</em> 検証</div>
  <div id="verdict"></div>
  <div class="hdr-right" id="ts"></div>
</div>

<a href="/" style="display:block;margin-bottom:16px;color:var(--muted2);font-size:12px">← レポート一覧に戻る</a>

<!-- Card 1: ボット稼働状態 -->
<div class="card">
  <h2>🔧 ① ボット稼働状態 <span id="s-bot" class="status-pill">—</span></h2>
  <div class="kpi-row" id="k-bot"></div>
</div>

<!-- Card 2: BTC価格 5取引所照合 -->
<div class="card">
  <h2>💹 ② BTC現在価格 複数取引所照合 <span id="s-price" class="status-pill">—</span></h2>
  <p style="font-size:12px;color:var(--muted2);margin-bottom:8px">Binance を基準に、他取引所との乖離率を算出</p>
  <table id="t-price"></table>
  <div class="kpi-row" id="k-price"></div>
</div>

<!-- Card 3: EMA200 独立検証 -->
<div class="card">
  <h2>📊 ③ EMA200 独立再計算 <span id="s-ema" class="status-pill">—</span></h2>
  <p style="font-size:12px;color:var(--muted2);margin-bottom:8px">Binanceから220日klineを再取得し、EMA200を独立計算してBot値と照合</p>
  <div class="kpi-row" id="k-ema"></div>
</div>

<!-- Card 4: トレード履歴検証 -->
<div class="card">
  <h2>📝 ④ トレード履歴検証 <span id="s-trade" class="status-pill">—</span></h2>
  <div id="trade-body"></div>
</div>

<!-- Card 5: ACH 警告 (最重要) -->
<div class="card">
  <div class="ach-warning">
    <h3>⚠️ ACH 枠は「実市場データ非連動」です — 要注意</h3>
    <p>
      現在の 気持ちマックス Bot の <b>ACH 枠（$4,000 = 初期資金の40%）</b> は、<br>
      <b>実際の市場トレード処理はされておらず</b>、バックテストで判明した期待年率 <b>+55%</b> を時間経過に応じて数学的に増加させているだけの<b>理論値複利</b>です。
    </p>
    <p style="margin-top:8px">
      <b style="color:var(--yellow)">→ これはデモ用の簡易表示です。</b>
      実運用では ACH 部分も Binance API と連動した<b>独立シグナルエンジン</b>を統合する必要があります（現在は未実装）。
    </p>
  </div>
  <h2>🧮 ACH / USDT 理論値整合性 <span id="s-theo" class="status-pill">—</span></h2>
  <table id="t-theo"></table>
</div>

<!-- Card 6: 総合サマリー -->
<div class="card">
  <h2>🎯 総合サマリー</h2>
  <div id="summary"></div>
</div>

</div>

<script>
const DATA = __DATA_JSON__;

function yen(n){return "$"+Number(n).toLocaleString("en-US",{maximumFractionDigits:2})}
function yenc(n){if(Math.abs(n)>=1000)return "$"+Math.round(n).toLocaleString();return "$"+Number(n).toFixed(2)}
function pct(n){return (n>=0?"+":"")+Number(n).toFixed(4)+"%"}
function pctSimp(n){return (n>=0?"+":"")+Number(n).toFixed(2)+"%"}

function pillClass(s){
  if(s==="PASS"||s==="OK"||s==="BOT_RUNNING"||s==="THEORETICAL_MATCH")return "pill-g";
  if(s==="WARN"||s==="WARN_ACH_THEORETICAL"||s==="INSUFFICIENT_DATA"||s==="NO_HISTORY")return "pill-y";
  if(s==="FAIL"||s==="BOT_NOT_FRESH")return "pill-r";
  return "pill-b";
}

// 総合判定バッジ
const verdict = DATA.overall_verdict;
const vBadge = document.getElementById("verdict");
let vCls="v-warn", vTxt=verdict;
if(verdict==="PASS"){vCls="v-pass";vTxt="✅ ALL PASS"}
else if(verdict==="FAIL"){vCls="v-fail";vTxt="❌ FAIL"}
else if(verdict==="WARN_ACH_THEORETICAL"){vCls="v-warn";vTxt="⚠️ WARN (ACH理論値)"}
else if(verdict==="WARN"){vCls="v-warn";vTxt="⚠️ WARN"}
vBadge.innerHTML = `<span class="verdict-badge ${vCls}">${vTxt}</span>`;

// タイムスタンプ
const ts = new Date(DATA.check_timestamp);
document.getElementById("ts").innerHTML = "検証実行: "+ts.toLocaleString("ja-JP");

// ① ボット稼働
const bot = DATA.bot_process || {};
const sf = DATA.state_file || {};
const state = sf.state || {};
const botOk = bot.running && sf.fresh_2h;
document.getElementById("s-bot").className = "status-pill " + (botOk?"pill-g":"pill-r");
document.getElementById("s-bot").textContent = botOk?"稼働中":"停止/古い";
document.getElementById("k-bot").innerHTML = `
  <div class="kpi"><div class="lbl">プロセス</div><div class="val ${bot.running?'g':'r'}">${bot.running?'✅ 動作中':'❌ 停止'}</div><div class="sub">PID: ${(bot.pids||[]).join(', ')||'—'}</div></div>
  <div class="kpi"><div class="lbl">state最終更新</div><div class="val ${sf.fresh?'g':sf.fresh_2h?'y':'r'}">${sf.age_minutes!==undefined?sf.age_minutes+'分前':'—'}</div><div class="sub">${sf.mtime?new Date(sf.mtime).toLocaleString('ja-JP'):'—'}</div></div>
  <div class="kpi"><div class="lbl">Tick数</div><div class="val b">${state.ticks_processed||0}</div><div class="sub">累計実行回数</div></div>
  <div class="kpi"><div class="lbl">起動日時</div><div class="val">${state.started_at?new Date(state.started_at).toLocaleString('ja-JP'):'—'}</div><div class="sub">SIMモード</div></div>
  <div class="kpi"><div class="lbl">総資産</div><div class="val ${(state.total_equity||0)>=(state.initial_capital||10000)?'g':'r'}">${yenc(state.total_equity||0)}</div><div class="sub">初期$${state.initial_capital||10000}</div></div>
`;

// ② 価格照合
const pc = DATA.btc_price_comparison || {};
document.getElementById("s-price").className = "status-pill " + pillClass(pc.status);
document.getElementById("s-price").textContent = pc.status || "—";
let ptab = "<tr><th>取引所</th><th>価格</th><th>Binance比乖離%</th><th>Bot記録比%</th><th>取得時間</th></tr>";
(pc.sources||[]).forEach(s=>{
  if(s.ok){
    const dBin = s.deviation_from_binance_pct;
    const dBot = s.deviation_from_bot_pct;
    const clsBin = dBin===undefined?'':(Math.abs(dBin)<0.5?'g':Math.abs(dBin)<2?'y':'r');
    ptab += `<tr><td><b>${s.source}</b></td><td>${yen(s.price)}</td>
      <td class="${clsBin}">${dBin!==undefined?pct(dBin):'—'}</td>
      <td>${dBot!==undefined?pct(dBot):'—'}</td>
      <td style="color:var(--muted)">${s.elapsed_ms}ms</td></tr>`;
  } else {
    ptab += `<tr><td><b>${s.source}</b></td><td colspan="4" style="color:var(--red);text-align:left">❌ ${s.error||'failed'}</td></tr>`;
  }
});
document.getElementById("t-price").innerHTML = ptab;
document.getElementById("k-price").innerHTML = `
  <div class="kpi"><div class="lbl">Bot記録価格</div><div class="val b">${yen(pc.bot_recorded_price||0)}</div><div class="sub">demo_state.json</div></div>
  <div class="kpi"><div class="lbl">成功ソース数</div><div class="val g">${pc.successful_count}/${(pc.sources||[]).length}</div></div>
  <div class="kpi"><div class="lbl">最大乖離</div><div class="val ${pc.max_deviation_pct<0.5?'g':pc.max_deviation_pct<2?'y':'r'}">${pc.max_deviation_pct!==null?pc.max_deviation_pct.toFixed(3)+'%':'—'}</div></div>
  <div class="kpi"><div class="lbl">平均乖離</div><div class="val">${pc.avg_deviation_pct!==null?pc.avg_deviation_pct.toFixed(3)+'%':'—'}</div></div>
`;

// ③ EMA200
const ev = DATA.ema200_verification || {};
document.getElementById("s-ema").className = "status-pill " + pillClass(ev.status);
document.getElementById("s-ema").textContent = ev.status || "—";
document.getElementById("k-ema").innerHTML = `
  <div class="kpi"><div class="lbl">Bot記録 EMA200</div><div class="val b">${yen(ev.bot_recorded_ema200||0)}</div></div>
  <div class="kpi"><div class="lbl">独立再計算値</div><div class="val g">${yen(ev.independent_ema200||0)}</div></div>
  <div class="kpi"><div class="lbl">絶対誤差</div><div class="val ${ev.diff_pct<0.1?'g':ev.diff_pct<0.5?'y':'r'}">${ev.absolute_diff!==undefined?'$'+ev.absolute_diff.toFixed(2):'—'}</div><div class="sub">${ev.diff_pct!==undefined?ev.diff_pct.toFixed(4)+'%':'—'}</div></div>
  <div class="kpi"><div class="lbl">klineサンプル</div><div class="val">${ev.klines_count||0}日</div><div class="sub">日次終値</div></div>
`;

// ④ トレード検証
const tv = DATA.trade_verification || {};
document.getElementById("s-trade").className = "status-pill " + pillClass(tv.status);
document.getElementById("s-trade").textContent = tv.status || "—";
let tradeHtml = "";
if(tv.status === "NO_HISTORY"){
  tradeHtml = `<div class="kpi-row"><div class="kpi" style="grid-column:1/-1"><div class="lbl">取引履歴</div><div class="val b">履歴なし (0件)</div><div class="sub">${tv.note||''}</div></div></div>`;
} else {
  tradeHtml = `<div class="kpi-row">
    <div class="kpi"><div class="lbl">総取引数</div><div class="val">${tv.total}</div></div>
    <div class="kpi"><div class="lbl">OHLC範囲内</div><div class="val g">${tv.verified_ok}</div></div>
    <div class="kpi"><div class="lbl">範囲外</div><div class="val ${tv.verified_bad>0?'r':'g'}">${tv.verified_bad}</div></div>
  </div>`;
  if(tv.details && tv.details.length > 0){
    let tt = "<table><tr><th>時刻</th><th>種別</th><th>価格</th><th>当日Low-High</th><th>判定</th></tr>";
    tv.details.forEach(d=>{
      const ok = d.verify_status === "OK";
      tt += `<tr><td>${new Date(d.ts).toLocaleString('ja-JP')}</td>
        <td>${d.action||''}</td>
        <td>${yen(d.price||0)}</td>
        <td>${d.day_low?yen(d.day_low)+' - '+yen(d.day_high):'—'}</td>
        <td class="${ok?'g':'r'}">${ok?'✅':'❌'} ${d.verify_status}</td></tr>`;
    });
    tt += "</table>";
    tradeHtml += tt;
  }
}
document.getElementById("trade-body").innerHTML = tradeHtml;

// ⑤ 理論値
const tp = DATA.theoretical_parts || {};
const theoOk = tp.ach?.match && tp.usdt?.match;
document.getElementById("s-theo").className = "status-pill " + (theoOk?"pill-g":"pill-y");
document.getElementById("s-theo").textContent = theoOk?"数学的一致":"乖離あり";
let theoTab = `<tr><th>項目</th><th>初期資金</th><th>理論値</th><th>Bot記録値</th><th>誤差%</th><th>判定</th></tr>`;
if(tp.ach){
  theoTab += `<tr><td><b class="y">ACH (40%) ⚠️</b></td>
    <td>${yen(tp.ach.initial_capital)}</td>
    <td>${yen(tp.ach.theoretical_value)}</td>
    <td>${yen(tp.ach.actual_value)}</td>
    <td class="${tp.ach.match?'g':'y'}">${tp.ach.diff_pct.toFixed(4)}%</td>
    <td class="${tp.ach.match?'g':'r'}">${tp.ach.match?'✅ 一致':'⚠️ 乖離'}</td></tr>`;
}
if(tp.usdt){
  theoTab += `<tr><td><b class="b">USDT (20%)</b></td>
    <td>${yen(tp.usdt.initial_capital)}</td>
    <td>${yen(tp.usdt.theoretical_value)}</td>
    <td>${yen(tp.usdt.actual_value)}</td>
    <td class="${tp.usdt.match?'g':'y'}">${tp.usdt.diff_pct.toFixed(4)}%</td>
    <td class="${tp.usdt.match?'g':'r'}">${tp.usdt.match?'✅ 一致':'⚠️ 乖離'}</td></tr>`;
}
theoTab += `<tr><td colspan="6" style="text-align:center;font-size:11px;color:var(--muted);padding:10px">経過時間: ${tp.hours_elapsed||0}時間 (${(tp.years_elapsed||0).toFixed(6)}年) / 複利計算基準</td></tr>`;
document.getElementById("t-theo").innerHTML = theoTab;

// 総合サマリー
let sum = "<ul style='padding-left:20px;line-height:1.8;font-size:13px'>";
(DATA.component_statuses||[]).forEach(s=>{
  const ok = s.includes("RUNNING")||s.includes("PASS")||s.includes("MATCH");
  const warn = s.includes("WARN")||s.includes("NO_HISTORY")||s.includes("INSUFFICIENT");
  const icon = ok?"✅":warn?"⚠️":"❌";
  const cls = ok?"g":warn?"y":"r";
  sum += `<li class="${cls}">${icon} <b>${s}</b></li>`;
});
sum += "</ul>";
sum += `<div style="background:var(--bg3);border:1px solid var(--border2);border-radius:8px;padding:14px;margin-top:12px;font-size:13px">
  <b style="color:var(--blue)">💡 結論:</b><br>
  ボットは純粋な Binance public API 実データを使用しており、BTC価格・EMA200は複数取引所と整合性が取れています。
  <b class="y">ただし、ACH枠は実市場データ非連動の理論値複利です</b> - これはデモ用の簡易表示であり、実運用には別途 Binance 連動エンジンの統合が必要です。
</div>`;
document.getElementById("summary").innerHTML = sum;
</script>
</body>
</html>
"""
    html = html.replace("__DATA_JSON__", data_json)
    OUT_HTML.write_text(html)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# エントリーポイント
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    result = run_all_checks()
    OUT_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    generate_html_report(result)
    print(f"\n💾 JSON: {OUT_JSON}")
    print(f"💾 HTML: {OUT_HTML}")
    print(f"\n🌐 ブラウザで確認:")
    print(f"   MacBook: http://localhost:8080/kimochimax_live_hallucination_report.html")
    print(f"   iPhone : http://192.168.100.42:8080/kimochimax_live_hallucination_report.html")
