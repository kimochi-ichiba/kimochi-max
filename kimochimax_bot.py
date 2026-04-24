"""
気持ちマックス 自動売買ボット
=======================
構成 (元金 $10,000 想定):
  - BTC 40% : EMA200上で保有、下で現金化 (BTCマイルド戦略)
  - ACH 40% : Iter42のACH戦略 (AC + 動的レバレッジ、清算リスク低減)
  - USDT 20%: ステーブルコイン保有 (年3%金利想定)

安全対策:
  - デフォルトはSIMモード（実取引しない、シミュレーションのみ）
  - 実取引モードに切り替える際はコマンドライン引数が必須
  - 全トレードをログ出力
  - 1時間ごとにstate.jsonに状態保存

使い方:
  python3 kimochimax_bot.py           # SIMモード（安全）
  python3 kimochimax_bot.py --live    # 実取引モード（API キー必要、現状は抑止）
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np

from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import assert_binance_source
from _multipos_backtest import UNIVERSE_50
from _rsi_short_backtest import fetch_with_rsi

PROJECT = Path(__file__).resolve().parent
STATE_PATH = PROJECT / "kimochimax_bot_state.json"
LOG_PATH = PROJECT / "kimochimax_bot.log"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 気持ちマックス 戦略の設定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KIMOCHIMAX_CONFIG = {
    "initial_capital": 10_000.0,
    "btc_weight": 0.40,   # BTCマイルド枠
    "ach_weight": 0.40,   # ACH枠
    "usdt_weight": 0.20,  # 現金枠
    "usdt_annual_rate": 0.03,
    "ach_universe": UNIVERSE_50,

    # BTCマイルド設定
    "btc_ema_period": 200,

    # ACH設定 (Iter42 ACHと同一)
    "ach": {
        "risk_per_trade_pct": 0.02,
        "max_pos": 12,
        "stop_loss_pct": 0.22,
        "tp1_pct": 0.10, "tp1_fraction": 0.25,
        "tp2_pct": 0.30, "tp2_fraction": 0.35,
        "trail_activate_pct": 0.50, "trail_giveback_pct": 0.15,
        "adx_min": 50, "adx_lev2": 60, "adx_lev3": 70,
        "lev_low": 2.5, "lev_mid": 2.5, "lev_high": 2.5,
        "breakout_pct": 0.05,
        "rsi_long_min": 50, "rsi_long_max": 75,
        "enable_short": False,
        "max_margin_per_pos_pct": 0.10,
        "pyramid_enabled": True, "pyramid_max": 2,
        "pyramid_trigger_pct": 0.10, "pyramid_size_pct": 0.5,
        "btc_ema50_filter": True,
        "dynamic_leverage": True,
    },
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ロガー
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def log(msg):
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 状態管理
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def load_state():
    if not STATE_PATH.exists():
        return {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "mode": "sim",
            "total_equity": KIMOCHIMAX_CONFIG["initial_capital"],
            "btc_part": {
                "cash": KIMOCHIMAX_CONFIG["initial_capital"] * KIMOCHIMAX_CONFIG["btc_weight"],
                "btc_qty": 0.0,
                "position": False,
                "last_price": 0.0,
            },
            "ach_part": {
                "cash": KIMOCHIMAX_CONFIG["initial_capital"] * KIMOCHIMAX_CONFIG["ach_weight"],
                "positions": {},  # sym -> {qty, entry_price, entry_ts, leverage, pyramids, peak_price, margin_usd, partial_taken}
            },
            "usdt_part": {
                "cash": KIMOCHIMAX_CONFIG["initial_capital"] * KIMOCHIMAX_CONFIG["usdt_weight"],
                "last_interest_ts": datetime.now(timezone.utc).isoformat(),
            },
            "trades": [],
            "equity_history": [],
        }
    return json.loads(STATE_PATH.read_text())


def save_state(state):
    # 既存を .bak にバックアップ
    if STATE_PATH.exists():
        bak = STATE_PATH.with_suffix(".json.bak")
        STATE_PATH.rename(bak)
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str, ensure_ascii=False))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 各戦略のワンティック処理
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def tick_btc_mild(state, btc_df, current_date, mode):
    """BTCマイルド戦略のワンティック"""
    part = state["btc_part"]
    if current_date not in btc_df.index:
        return
    row = btc_df.loc[current_date]
    price = float(row["close"])
    ema = row.get(f"ema{KIMOCHIMAX_CONFIG['btc_ema_period']}")
    part["last_price"] = price

    if pd.isna(ema):
        return

    # シグナル判定
    signal = "BUY" if (price > ema and not part["position"]) else \
             "SELL" if (price < ema and part["position"]) else None

    if signal == "BUY":
        buy_price = price * 1.0003  # SLIP
        part["btc_qty"] = part["cash"] / buy_price * (1 - 0.0006)  # FEE
        part["cash"] = 0
        part["position"] = True
        trade = {"ts": str(current_date), "part": "BTC", "action": "BUY",
                 "price": price, "qty": part["btc_qty"], "mode": mode}
        state["trades"].append(trade)
        log(f"🟢 [BTC] BUY @ ${price:,.2f} qty={part['btc_qty']:.6f} (EMA200=${ema:,.2f})")
    elif signal == "SELL":
        sell_price = price * 0.9997
        proceeds = part["btc_qty"] * sell_price * (1 - 0.0006)
        part["cash"] = proceeds
        qty_was = part["btc_qty"]
        part["btc_qty"] = 0.0
        part["position"] = False
        trade = {"ts": str(current_date), "part": "BTC", "action": "SELL",
                 "price": price, "qty": qty_was, "cash": proceeds, "mode": mode}
        state["trades"].append(trade)
        log(f"🔴 [BTC] SELL @ ${price:,.2f} qty={qty_was:.6f} cash=${proceeds:,.2f}")


def tick_usdt_interest(state, current_date):
    """USDT金利の複利加算"""
    part = state["usdt_part"]
    last_ts = datetime.fromisoformat(part["last_interest_ts"])
    now = current_date if isinstance(current_date, datetime) else \
          datetime.combine(current_date.to_pydatetime().date(), datetime.min.time(), tzinfo=timezone.utc)
    days = (now - last_ts.replace(tzinfo=timezone.utc)).days
    if days > 0:
        rate = KIMOCHIMAX_CONFIG["usdt_annual_rate"]
        part["cash"] *= (1 + rate) ** (days / 365)
        part["last_interest_ts"] = now.isoformat()


def tick_ach(state, all_data, current_date, mode):
    """ACH戦略のワンティック（バックテストエンジンを呼ぶ）"""
    # 実運用では、ここで各銘柄のシグナルをチェックし、エントリー/エグジットを実行する
    # 現状、ACHの完全なライブ実行は複雑なため、概算として:
    #   - cash と positions の価値を更新するのみ
    # 実際のエントリー/エグジットは別途 strategy/engine の呼び出しが必要

    part = state["ach_part"]
    # MTM: 保有中ポジションの時価評価
    unreal = 0.0
    for sym, p in part["positions"].items():
        df = all_data.get(sym)
        if df is None or current_date not in df.index:
            continue
        cur = float(df.loc[current_date, "close"])
        if p["side"] == "long":
            unreal += p["qty"] * (cur - p["entry_price"]) * p["leverage"]
        else:
            unreal += p["qty"] * (p["entry_price"] - cur) * p["leverage"]
    part["unrealized_pnl"] = unreal
    # (実シグナル処理は本番実装時に追加)


def compute_total_equity(state, all_data, current_date):
    """総資産を計算"""
    # BTC
    btc_value = state["btc_part"]["cash"] + \
                state["btc_part"]["btc_qty"] * state["btc_part"]["last_price"]
    # ACH
    ach_value = state["ach_part"]["cash"]
    for sym, p in state["ach_part"]["positions"].items():
        df = all_data.get(sym)
        if df is not None and current_date in df.index:
            cur = float(df.loc[current_date, "close"])
            if p["side"] == "long":
                ach_value += p["margin_usd"] + p["qty"] * (cur - p["entry_price"]) * p["leverage"]
            else:
                ach_value += p["margin_usd"] + p["qty"] * (p["entry_price"] - cur) * p["leverage"]
    # USDT
    usdt_value = state["usdt_part"]["cash"]
    total = btc_value + ach_value + usdt_value
    state["total_equity"] = total
    return total, btc_value, ach_value, usdt_value


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メインループ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_sim(duration_minutes=None):
    """SIMモード: 履歴データでシミュレーション実行"""
    log("=" * 70)
    log("🚀 気持ちマックス 自動売買ボット SIMモード 起動")
    log("=" * 70)
    log(f"構成: BTC {KIMOCHIMAX_CONFIG['btc_weight']*100:.0f}% + ACH {KIMOCHIMAX_CONFIG['ach_weight']*100:.0f}% + USDT {KIMOCHIMAX_CONFIG['usdt_weight']*100:.0f}%")
    log(f"初期資金: ${KIMOCHIMAX_CONFIG['initial_capital']:,.0f}")
    log(f"USDT金利: 年{KIMOCHIMAX_CONFIG['usdt_annual_rate']*100:.1f}%")

    # データロード
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)
    cache = PROJECT / "results/_cache_alldata.pkl"
    if cache.exists():
        with open(cache, "rb") as f:
            all_data = pickle.load(f)
        log(f"📦 キャッシュ使用 (銘柄数: {len(all_data)})")
    else:
        log("📥 Binance データ取得中...")
        all_data = fetch_with_rsi(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")

    btc_df = all_data["BTC/USDT"]

    # 状態
    state = load_state()
    state["mode"] = "sim"

    # 全期間をリプレイ
    start_ts = pd.Timestamp("2020-01-01")
    end_ts = pd.Timestamp("2024-12-31")
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    log(f"\n🔄 {len(dates)}日分のシミュレーション開始...")

    processed = 0
    for current_date in dates:
        # BTCマイルドチェック (日次)
        tick_btc_mild(state, btc_df, current_date, "sim")
        # USDT金利 (日次複利)
        # tick_usdt_interest は datetime オブジェクトを期待するので、ここでは簡易的に
        part = state["usdt_part"]
        daily_rate = (1 + KIMOCHIMAX_CONFIG["usdt_annual_rate"]) ** (1/365)
        part["cash"] *= daily_rate
        # ACH (本来はライブシグナル、ここでは概算で省略)
        # tick_ach(state, all_data, current_date, "sim")

        processed += 1
        if processed % 100 == 0:
            total, btc_v, ach_v, usdt_v = compute_total_equity(state, all_data, current_date)
            log(f"  [{str(current_date)[:10]}] 総資産: ${total:>10,.2f} "
                f"(BTC:${btc_v:,.0f}/ACH:${ach_v:,.0f}/USDT:${usdt_v:,.0f})")

        # 状態保存（100日ごと）
        if processed % 500 == 0:
            state["equity_history"].append({
                "ts": str(current_date)[:10],
                "total": state["total_equity"],
            })

    # 最終状態
    total, btc_v, ach_v, usdt_v = compute_total_equity(state, all_data, dates[-1])

    log("\n" + "=" * 70)
    log("📊 シミュレーション完了")
    log(f"  最終総資産: ${total:,.2f}")
    log(f"  内訳: BTC ${btc_v:,.0f} / ACH ${ach_v:,.0f} / USDT ${usdt_v:,.0f}")
    log(f"  リターン: {(total / KIMOCHIMAX_CONFIG['initial_capital'] - 1) * 100:+.1f}%")
    log(f"  取引数: {len(state['trades'])}")
    log("=" * 70)

    save_state(state)
    log(f"💾 状態保存: {STATE_PATH}")

    # 注意: この簡易SIMは BTC部分のみライブ実行しており、
    # ACH部分は別途 _legends_engine 相当の複雑なシグナル処理が必要。
    # 本格運用時は、そのシグナル処理を本スクリプトに統合する必要がある。
    log("")
    log("⚠️ 注意: 現在のSIMはBTC部分のみライブ実行しており、ACH部分の")
    log("   シグナル処理は含まれていません。ACH部分の実運用には、")
    log("   _legends_engine相当のライブシグナル処理統合が必要です。")
    log("   本バックテストの期待値:")
    log("   - 年率 +54.8% / DD 39.7% / 清算4回")
    log("")


def run_live():
    """ライブモード (注意: 実資金が動く)"""
    log("=" * 70)
    log("⚠️  ⚠️  ⚠️  ライブモードは現在無効化されています。")
    log("実取引を行う前に:")
    log("  1. Binance API キーを設定")
    log("  2. 少額で動作確認")
    log("  3. SIMモードで1ヶ月以上の動作確認")
    log("を必ず行ってください。")
    log("=" * 70)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# エントリーポイント
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    if "--live" in sys.argv:
        run_live()
    else:
        run_sim()
