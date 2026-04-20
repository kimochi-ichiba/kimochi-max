"""
H11 デモトレード・ライブランナー
===================================
Binance public API から最新価格を取得し、H11戦略でSIM取引を実行。
結果を demo_state.json に書き出し、ダッシュボードから閲覧可能。

構成:
  - BTC 40% : EMA200上で保有、下で現金化 (BTCマイルド)
  - ACH 40% : 期待年率+55%で複利成長 (概算)
  - USDT 20%: 年3%金利 (日割)

起動:
  python3 demo_runner.py           # 通常ループ (5分ごと)
  python3 demo_runner.py --once    # 1回だけ実行してstate更新
  python3 demo_runner.py --reset   # 状態リセットして最初から
"""
from __future__ import annotations
import sys, json, time, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")
try:
    import discord_notify
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
RESULTS_DIR = PROJECT / "results"
STATE_PATH = RESULTS_DIR / "demo_state.json"
LOG_PATH = PROJECT / "demo_runner.log"

# 設定
INITIAL = 10_000.0
BTC_WEIGHT = 0.40
ACH_WEIGHT = 0.40
USDT_WEIGHT = 0.20
USDT_ANNUAL_RATE = 0.03
ACH_ANNUAL_RATE = 0.55  # バックテストでの期待値 +54.8%
LOOP_INTERVAL = 300  # 5分
MAX_TRADE_HISTORY = 100
MAX_EQUITY_HISTORY = 2000


def log(msg, also_print=True):
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
    line = f"[{ts}] {msg}"
    if also_print:
        print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def http_get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_btc_price_and_ema200():
    """Binance public API から BTC の現在価格と EMA200 を取得"""
    # 現在価格
    ticker_url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    current_price = float(http_get_json(ticker_url)["price"])

    # 過去220日の日次終値取得
    klines_url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=220"
    klines = http_get_json(klines_url)
    closes = [float(k[4]) for k in klines]

    # EMA200 計算 (最後の価格時点)
    alpha = 2 / (200 + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = alpha * c + (1 - alpha) * ema

    # 24h変化率
    ticker_24h_url = "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"
    ticker_24h = http_get_json(ticker_24h_url)
    change_24h_pct = float(ticker_24h["priceChangePercent"])
    volume_24h = float(ticker_24h["quoteVolume"])

    return {
        "current_price": round(current_price, 2),
        "ema200": round(ema, 2),
        "change_24h_pct": round(change_24h_pct, 2),
        "volume_24h_usdt": round(volume_24h, 0),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }


def fresh_state():
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return {
        "version": "1.0",
        "mode": "SIM",
        "started_at": now,
        "initial_capital": INITIAL,
        "last_update": now,

        "btc_part": {
            "cash": INITIAL * BTC_WEIGHT,
            "btc_qty": 0.0,
            "position": False,
            "last_btc_price": 0.0,
            "last_ema200": 0.0,
            "last_signal": "HOLD",
            "entry_price": 0.0,
            "entry_ts": None,
        },
        "ach_part": {
            "cash": INITIAL * ACH_WEIGHT,
            "virtual_equity": INITIAL * ACH_WEIGHT,
            "last_tick": now,
            "note": "期待年率+55%で複利成長（バックテスト実績ベースの概算）",
        },
        "usdt_part": {
            "cash": INITIAL * USDT_WEIGHT,
            "last_tick": now,
        },

        "total_equity": INITIAL,
        "peak_equity": INITIAL,
        "max_dd_observed": 0.0,
        "ticks_processed": 0,

        "trades": [],
        "equity_history": [
            {"ts": now, "total": INITIAL,
             "btc": INITIAL * BTC_WEIGHT,
             "ach": INITIAL * ACH_WEIGHT,
             "usdt": INITIAL * USDT_WEIGHT}
        ],
        "btc_price_history": [],
    }


def load_state():
    if not STATE_PATH.exists():
        return fresh_state()
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception as e:
        log(f"⚠️ state読込失敗: {e} → 初期化")
        return fresh_state()


def save_state(state):
    # 履歴を制限してファイルサイズを抑える
    state["trades"] = state["trades"][-MAX_TRADE_HISTORY:]
    state["equity_history"] = state["equity_history"][-MAX_EQUITY_HISTORY:]
    state["btc_price_history"] = state["btc_price_history"][-MAX_EQUITY_HISTORY:]

    # アトミック書き込み
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str, ensure_ascii=False))
    tmp.replace(STATE_PATH)


def process_tick(state, btc_data):
    """1tick分の処理: 価格取得→各部の更新"""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec='seconds')

    btc_price = btc_data["current_price"]
    ema200 = btc_data["ema200"]

    # ━━ BTCマイルド部分 ━━━━━━━━━━━━━━━━━
    btc = state["btc_part"]
    btc["last_btc_price"] = btc_price
    btc["last_ema200"] = ema200

    signal_action = None
    if btc_price > ema200 and not btc["position"]:
        # BUY
        fee = 0.0006; slip = 0.0003
        buy_price = btc_price * (1 + slip)
        btc_qty = btc["cash"] / buy_price * (1 - fee)
        btc["btc_qty"] = btc_qty
        btc["cash"] = 0
        btc["position"] = True
        btc["last_signal"] = "BUY"
        btc["entry_price"] = btc_price
        btc["entry_ts"] = now_iso
        state["trades"].append({
            "ts": now_iso, "part": "BTC", "action": "BUY",
            "price": btc_price, "qty": round(btc_qty, 6),
            "value_usd": round(btc_qty * btc_price, 2),
            "ema200": ema200, "mode": "SIM",
        })
        signal_action = f"🟢 BTC BUY @ ${btc_price:,.2f} (EMA200=${ema200:,.2f})"
        log(signal_action)
        # Discord通知
        if DISCORD_AVAILABLE:
            try:
                discord_notify.notify_trade("BUY", "BTC", btc_price,
                                             btc_qty, btc_qty * btc_price, ema200=ema200)
            except Exception as e:
                log(f"⚠️ Discord通知失敗: {e}")
    elif btc_price < ema200 and btc["position"]:
        # SELL
        fee = 0.0006; slip = 0.0003
        sell_price = btc_price * (1 - slip)
        proceeds = btc["btc_qty"] * sell_price * (1 - fee)
        pnl = proceeds - (btc["entry_price"] * btc["btc_qty"] if btc["entry_price"] else 0)
        qty_was = btc["btc_qty"]
        btc["cash"] = proceeds
        btc["btc_qty"] = 0
        btc["position"] = False
        btc["last_signal"] = "SELL"
        state["trades"].append({
            "ts": now_iso, "part": "BTC", "action": "SELL",
            "price": btc_price, "qty": round(qty_was, 6),
            "value_usd": round(proceeds, 2),
            "pnl_usd": round(pnl, 2),
            "ema200": ema200, "mode": "SIM",
        })
        signal_action = f"🔴 BTC SELL @ ${btc_price:,.2f} (EMA200=${ema200:,.2f}, P&L: ${pnl:+,.2f})"
        log(signal_action)
        # Discord通知
        if DISCORD_AVAILABLE:
            try:
                discord_notify.notify_trade("SELL", "BTC", btc_price,
                                             qty_was, proceeds, pnl_usd=pnl, ema200=ema200)
            except Exception as e:
                log(f"⚠️ Discord通知失敗: {e}")
    else:
        btc["last_signal"] = "HOLD-IN" if btc["position"] else "HOLD-OUT"

    # ━━ ACH部分 (期待値で複利成長) ━━━━━━━━━━━━━━━━━
    ach = state["ach_part"]
    last_ach_tick = datetime.fromisoformat(ach["last_tick"].replace("Z", "+00:00"))
    if last_ach_tick.tzinfo is None:
        last_ach_tick = last_ach_tick.replace(tzinfo=timezone.utc)
    days_elapsed = (now - last_ach_tick).total_seconds() / 86400
    if days_elapsed > 0:
        daily_rate = (1 + ACH_ANNUAL_RATE) ** (1/365) - 1
        ach["virtual_equity"] *= (1 + daily_rate) ** days_elapsed
        ach["cash"] = ach["virtual_equity"]
        ach["last_tick"] = now_iso

    # ━━ USDT部分 (年3%複利) ━━━━━━━━━━━━━━━━━
    usdt = state["usdt_part"]
    last_usdt_tick = datetime.fromisoformat(usdt["last_tick"].replace("Z", "+00:00"))
    if last_usdt_tick.tzinfo is None:
        last_usdt_tick = last_usdt_tick.replace(tzinfo=timezone.utc)
    usdt_days = (now - last_usdt_tick).total_seconds() / 86400
    if usdt_days > 0:
        daily_rate = (1 + USDT_ANNUAL_RATE) ** (1/365) - 1
        usdt["cash"] *= (1 + daily_rate) ** usdt_days
        usdt["last_tick"] = now_iso

    # ━━ 総資産計算 ━━━━━━━━━━━━━━━━━
    btc_value = btc["cash"] + btc["btc_qty"] * btc_price
    ach_value = ach["cash"]
    usdt_value = usdt["cash"]
    total = btc_value + ach_value + usdt_value

    state["total_equity"] = round(total, 2)
    state["peak_equity"] = round(max(state["peak_equity"], total), 2)
    state["max_dd_observed"] = round(
        max(state["max_dd_observed"],
            (state["peak_equity"] - total) / state["peak_equity"] * 100), 2)
    state["ticks_processed"] += 1
    state["last_update"] = now_iso

    # 履歴
    state["equity_history"].append({
        "ts": now_iso,
        "total": round(total, 2),
        "btc": round(btc_value, 2),
        "ach": round(ach_value, 2),
        "usdt": round(usdt_value, 2),
    })
    state["btc_price_history"].append({
        "ts": now_iso,
        "price": btc_price,
        "ema200": ema200,
    })

    # 24h変化情報保持
    state["btc_24h_change_pct"] = btc_data["change_24h_pct"]
    state["btc_24h_volume_usdt"] = btc_data["volume_24h_usdt"]

    log(f"📊 総資産 ${total:,.2f} | BTC:${btc_value:,.0f} ACH:${ach_value:,.0f} "
        f"USDT:${usdt_value:,.0f} | BTC=${btc_price:,.2f} (24h: {btc_data['change_24h_pct']:+.2f}%)")

    # Discord通知: DD警告 & 日次サマリー
    if DISCORD_AVAILABLE:
        try:
            dd_pct = state["max_dd_observed"]
            discord_notify.notify_dd_alert(dd_pct, total, state["peak_equity"], INITIAL)
            # 日次サマリー (JST 21:00頃に1回)
            jst_hour = (now.hour + 9) % 24
            if jst_hour == 21:
                pnl = total - INITIAL
                pnl_pct = (total / INITIAL - 1) * 100
                discord_notify.notify_daily_summary(
                    total, INITIAL, pnl, pnl_pct, dd_pct,
                    btc_price, ema200, btc["last_signal"],
                    btc_value, ach_value, usdt_value, len(state["trades"])
                )
        except Exception as e:
            log(f"⚠️ Discord通知処理でエラー: {e}")


def run_once():
    """1回実行"""
    try:
        log("🔄 Binance API から最新価格取得中...")
        btc_data = fetch_btc_price_and_ema200()
        log(f"   BTC: ${btc_data['current_price']:,.2f} | EMA200: ${btc_data['ema200']:,.2f} "
            f"| 24h: {btc_data['change_24h_pct']:+.2f}%")

        state = load_state()
        process_tick(state, btc_data)
        save_state(state)
        return True
    except urllib.error.URLError as e:
        log(f"⚠️ ネットワークエラー: {e}")
        return False
    except Exception as e:
        log(f"⚠️ エラー: {e}")
        import traceback
        log(traceback.format_exc(), also_print=False)
        return False


def run_loop():
    """永続ループ"""
    log("=" * 60)
    log("🚀 H11 デモトレードランナー 起動")
    log(f"   初期資金: ${INITIAL:,.0f}")
    log(f"   構成: BTC {BTC_WEIGHT*100:.0f}% + ACH {ACH_WEIGHT*100:.0f}% + USDT {USDT_WEIGHT*100:.0f}%")
    log(f"   更新間隔: {LOOP_INTERVAL}秒 ({LOOP_INTERVAL//60}分)")
    # Discord起動通知
    if DISCORD_AVAILABLE:
        try:
            cfg = discord_notify.load_config()
            if cfg.get("enabled"):
                discord_notify.notify_startup(INITIAL)
                log("   Discord通知: 有効")
            else:
                log("   Discord通知: 未設定 (python3 discord_notify.py setup で設定)")
        except Exception as e:
            log(f"   Discord通知: エラー {e}")
    log("=" * 60)

    while True:
        run_once()
        try:
            time.sleep(LOOP_INTERVAL)
        except KeyboardInterrupt:
            log("⚠️ 中断されました")
            break


if __name__ == "__main__":
    if "--reset" in sys.argv:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
            log("🗑️ 状態リセット完了")
    if "--once" in sys.argv:
        run_once()
    else:
        run_loop()
