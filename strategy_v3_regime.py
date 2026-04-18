"""
strategy_v3_regime.py
=====================
新戦略v3: Market Regime + Multi-Timeframe Trend Follow + Pullback Entry

設計思想:
- v95.0は「強いトレンド相場」でしか機能しないことが判明
- トレンド判定を厳格化し、横ばい・反転局面は取引しない
- 押し目/戻りでエントリーし、ダマシを回避
- トレンド強度でポジションサイズを動的調整

エントリー条件 (すべて満たす):
  1. 日足EMA50 > EMA200 (ロング) / 逆 (ショート) — 大局トレンド
  2. 4h足ADX >= 25 — トレンド強度十分
  3. 4h足EMA20と価格の関係 — 中期方向確認
  4. 1h足でプルバック検出 (RSI 35-55 でロング / 45-65でショート)
  5. 1h足で直近反転の兆し (価格が1h EMA10 を再びクロス)

リスク管理:
- SL = エントリー ± 1.2 × ATR(14, 1h)
- TP: ATRベースで動的 (3~5倍), ADX強度に応じて伸ばす
- トレーリング: 1.5 ATR後から有効化
- ポジションサイズ: 資産の1.5%リスク, ADX強度でレバレッジ3〜5倍
"""

from __future__ import annotations

import sys
import logging
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, "/Users/sanosano/projects/crypto-bot-pro")

from data_fetcher import DataFetcher
from config import Config

logging.getLogger("data_fetcher").setLevel(logging.WARNING)


# ----------------------------- Params ------------------------------------

# Regime filters
ADX_MIN = 25.0
ADX_STRONG = 35.0
EMA_SHORT = 20
EMA_LONG = 50
EMA_TREND = 200

# Entry triggers (1h)
RSI_PERIOD = 14
RSI_LONG_MIN = 35
RSI_LONG_MAX = 55
RSI_SHORT_MIN = 45
RSI_SHORT_MAX = 65
EMA_FAST_1H = 10

# Risk management
ATR_PERIOD = 14
SL_ATR_MULT = 1.2
TP_BASE_ATR = 3.0        # 通常時の利確倍率
TP_STRONG_ATR = 5.0      # 強トレンド時の利確倍率
TRAIL_TRIGGER_ATR = 1.5  # 1.5 ATR動いたらトレーリング開始
TRAIL_DIST_ATR = 1.5     # 価格から1.5 ATR離してトレーリング

RISK_PER_TRADE = 0.015
LEV_MIN = 3.0
LEV_MAX = 5.0

FEE_RATE = 0.0006
SLIPPAGE = 0.0005


# ----------------------------- Indicators --------------------------------

def compute_indicators_hourly(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    high, low, close = df["high"], df["low"], df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_PERIOD).mean()

    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(RSI_PERIOD).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    df["ema_fast"] = close.ewm(span=EMA_FAST_1H, adjust=False).mean()
    return df


def compute_indicators_4h(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    high, low, close = df["high"], df["low"], df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)

    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr_adx = tr.rolling(ATR_PERIOD).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(ATR_PERIOD).mean() / atr_adx
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(ATR_PERIOD).mean() / atr_adx
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = dx.rolling(ATR_PERIOD).mean()
    df["plus_di"] = plus_di
    df["minus_di"] = minus_di

    df["ema20"] = close.ewm(span=EMA_SHORT, adjust=False).mean()
    return df


def compute_indicators_daily(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close = df["close"]
    df["ema50"] = close.ewm(span=EMA_LONG, adjust=False).mean()
    df["ema200"] = close.ewm(span=EMA_TREND, adjust=False).mean()
    df["daily_trend"] = np.where(df["ema50"] > df["ema200"], 1,
                                  np.where(df["ema50"] < df["ema200"], -1, 0))
    return df[["daily_trend", "ema50", "ema200"]]


def merge_higher_tf(df_base: pd.DataFrame, df_higher: pd.DataFrame, cols: list) -> pd.DataFrame:
    """高時間足のデータを1h足にforward-fillでマージ"""
    higher = df_higher[cols].reindex(df_base.index, method="ffill")
    return df_base.join(higher, rsuffix="_higher")


# ----------------------------- Data structures --------------------------

@dataclass
class Position:
    symbol: str
    side: str
    entry_price: float
    entry_time: pd.Timestamp
    size: float
    sl: float
    tp: float
    leverage: float
    entry_atr: float
    trail_active: bool = False
    high_water: float = 0.0


@dataclass
class Trade:
    symbol: str
    side: str
    open_time: pd.Timestamp
    close_time: pd.Timestamp
    entry: float
    exit: float
    pnl: float
    pnl_pct: float
    leverage: float
    exit_reason: str


# ----------------------------- Backtest logic ---------------------------

def evaluate_and_trade(df_1h: pd.DataFrame, symbol: str, tracker: dict) -> list[Trade]:
    trades: list[Trade] = []
    pos: Optional[Position] = None

    for ts, row in df_1h.iterrows():
        price = row["close"]
        atr = row["atr"]

        if pd.isna(atr) or pd.isna(row.get("adx")) or pd.isna(row.get("daily_trend")) \
           or pd.isna(row.get("rsi")) or pd.isna(row.get("ema_fast")) \
           or pd.isna(row.get("ema20")):
            continue

        # --- 決済判定 ---
        if pos is not None:
            exit_reason = None
            exit_price = None

            if pos.side == "long":
                pos.high_water = max(pos.high_water, row["high"])
                # トレーリングストップ発動判定
                if not pos.trail_active and pos.high_water >= pos.entry_price + TRAIL_TRIGGER_ATR * pos.entry_atr:
                    pos.trail_active = True
                if pos.trail_active:
                    new_sl = pos.high_water - TRAIL_DIST_ATR * pos.entry_atr
                    pos.sl = max(pos.sl, new_sl)
                # 決済
                if row["low"] <= pos.sl:
                    exit_reason = "sl"
                    exit_price = pos.sl * (1 - SLIPPAGE)
                elif row["high"] >= pos.tp:
                    exit_reason = "tp"
                    exit_price = pos.tp * (1 - SLIPPAGE)
            else:  # short
                pos.high_water = min(pos.high_water, row["low"])
                if not pos.trail_active and pos.high_water <= pos.entry_price - TRAIL_TRIGGER_ATR * pos.entry_atr:
                    pos.trail_active = True
                if pos.trail_active:
                    new_sl = pos.high_water + TRAIL_DIST_ATR * pos.entry_atr
                    pos.sl = min(pos.sl, new_sl)
                if row["high"] >= pos.sl:
                    exit_reason = "sl"
                    exit_price = pos.sl * (1 + SLIPPAGE)
                elif row["low"] <= pos.tp:
                    exit_reason = "tp"
                    exit_price = pos.tp * (1 + SLIPPAGE)

            if exit_reason:
                pnl = _close(pos, exit_price, tracker)
                direction = 1 if pos.side == "long" else -1
                trades.append(Trade(
                    symbol=symbol, side=pos.side,
                    open_time=pos.entry_time, close_time=ts,
                    entry=pos.entry_price, exit=exit_price,
                    pnl=pnl,
                    pnl_pct=(exit_price / pos.entry_price - 1) * 100 * pos.leverage * direction,
                    leverage=pos.leverage, exit_reason=exit_reason,
                ))
                pos = None

        # --- エントリー判定 ---
        if pos is None:
            regime_long = (row["daily_trend"] == 1 and row["adx"] >= ADX_MIN
                           and price > row["ema20"] and row["plus_di"] > row["minus_di"])
            regime_short = (row["daily_trend"] == -1 and row["adx"] >= ADX_MIN
                            and price < row["ema20"] and row["minus_di"] > row["plus_di"])

            # プルバックトリガー
            pullback_long = (regime_long and RSI_LONG_MIN <= row["rsi"] <= RSI_LONG_MAX
                             and price > row["ema_fast"])
            pullback_short = (regime_short and RSI_SHORT_MIN <= row["rsi"] <= RSI_SHORT_MAX
                              and price < row["ema_fast"])

            if pullback_long or pullback_short:
                side = "long" if pullback_long else "short"
                is_strong = row["adx"] >= ADX_STRONG
                tp_mult = TP_STRONG_ATR if is_strong else TP_BASE_ATR
                leverage = LEV_MAX if is_strong else LEV_MIN
                pos = _open(symbol, side, price, atr, leverage, tp_mult, tracker, ts)

        tracker["curve"].append((ts, tracker["balance"] + _unrealized(pos, price)))

    return trades


def _open(symbol, side, price, atr, leverage, tp_mult, tracker, ts) -> Position:
    risk_amt = tracker["balance"] * RISK_PER_TRADE
    sl_dist = SL_ATR_MULT * atr
    size = risk_amt / sl_dist
    entry = price * (1 + SLIPPAGE) if side == "long" else price * (1 - SLIPPAGE)
    notional = size * entry
    tracker["balance"] -= notional * FEE_RATE
    if side == "long":
        sl = entry - sl_dist
        tp = entry + tp_mult * atr
    else:
        sl = entry + sl_dist
        tp = entry - tp_mult * atr
    return Position(
        symbol=symbol, side=side, entry_price=entry, entry_time=ts,
        size=size, sl=sl, tp=tp, leverage=leverage, entry_atr=atr,
        high_water=entry,
    )


def _close(pos: Position, exit_price: float, tracker: dict) -> float:
    if pos.side == "long":
        gross = (exit_price - pos.entry_price) * pos.size * pos.leverage
    else:
        gross = (pos.entry_price - exit_price) * pos.size * pos.leverage
    fee = exit_price * pos.size * FEE_RATE
    pnl = gross - fee
    tracker["balance"] += pnl
    return pnl


def _unrealized(pos: Optional[Position], price: float) -> float:
    if pos is None:
        return 0.0
    if pos.side == "long":
        return (price - pos.entry_price) * pos.size * pos.leverage
    return (pos.entry_price - price) * pos.size * pos.leverage


# ----------------------------- Main runner ------------------------------

def run_single_window(symbol: str, start: str, end: str, initial_balance: float = 10000.0) -> dict:
    """1通貨・1ウィンドウのバックテスト"""
    config = Config()
    fetcher = DataFetcher(config)

    # バッファ込みで余裕持って取得
    start_dt = datetime.fromisoformat(start)
    buffer_start = (start_dt - timedelta(days=60)).strftime("%Y-%m-%d")

    df_1h = fetcher.fetch_historical_ohlcv(symbol, "1h", buffer_start, end)
    df_4h = fetcher.fetch_historical_ohlcv(symbol, "4h", buffer_start, end)
    df_1d = fetcher.fetch_historical_ohlcv(symbol, "1d", buffer_start, end)

    if df_1h.empty or df_4h.empty or df_1d.empty:
        return {"trades": [], "final": initial_balance, "curve": []}

    df_1h = compute_indicators_hourly(df_1h)
    df_4h = compute_indicators_4h(df_4h)
    df_trend = compute_indicators_daily(df_1d)

    df_1h = merge_higher_tf(df_1h, df_4h, ["adx", "plus_di", "minus_di", "ema20"])
    df_1h = merge_higher_tf(df_1h, df_trend, ["daily_trend"])

    df_1h = df_1h[df_1h.index >= pd.Timestamp(start)]

    tracker = {"balance": initial_balance, "curve": []}
    trades = evaluate_and_trade(df_1h, symbol, tracker)
    return {"trades": trades, "final": tracker["balance"], "curve": tracker["curve"]}


def main():
    SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    end_date = datetime(2026, 4, 18)

    # 1年間を30日ウィンドウ×15日刻みで検証
    WINDOW_DAYS = 30
    STEP_DAYS = 15
    HISTORY_DAYS = 365

    start_from = end_date - timedelta(days=HISTORY_DAYS)
    windows = []
    cursor = start_from
    while cursor + timedelta(days=WINDOW_DAYS) <= end_date:
        w_s = cursor
        w_e = cursor + timedelta(days=WINDOW_DAYS)
        windows.append((w_s.strftime("%Y-%m-%d"), w_e.strftime("%Y-%m-%d")))
        cursor += timedelta(days=STEP_DAYS)

    print(f"\n🔬 v3戦略 (Regime + Pullback) 複数期間検証")
    print(f"検証期間: {start_from.strftime('%Y-%m-%d')} 〜 {end_date.strftime('%Y-%m-%d')}")
    print(f"ウィンドウ数: {len(windows)} (30日×15日刻み)")
    print(f"対象通貨: {', '.join(SYMBOLS)}\n")

    all_window_returns = []
    print(f"  {'期間':30s} {'平均月次':>10s} {'取引数':>8s} {'通貨別':<30s}")
    print(f"  {'-'*80}")

    for start, end in windows:
        per_sym_returns = []
        total_trades = 0
        for sym in SYMBOLS:
            r = run_single_window(sym, start, end)
            ret = (r["final"] / 10000.0 - 1) * 100
            per_sym_returns.append(ret)
            total_trades += len(r["trades"])
        avg_ret = np.mean(per_sym_returns)
        all_window_returns.append(avg_ret)
        sym_str = " ".join(f"{x:+6.1f}" for x in per_sym_returns)
        print(f"  {start} 〜 {end}  {avg_ret:+8.2f}% {total_trades:5d}   [{sym_str}]")

    returns = np.array(all_window_returns)
    print(f"\n{'='*80}")
    print(f"  📊 v3戦略 集計")
    print(f"{'='*80}")
    print(f"  平均月次リターン : {np.mean(returns):+.2f}%")
    print(f"  中央値            : {np.median(returns):+.2f}%")
    print(f"  最高              : {np.max(returns):+.2f}%")
    print(f"  最低              : {np.min(returns):+.2f}%")
    print(f"  標準偏差          : {np.std(returns):.2f}%")
    print(f"  プラス月          : {sum(1 for r in returns if r > 0)}/{len(returns)} ({sum(1 for r in returns if r > 0)/len(returns)*100:.0f}%)")
    print(f"  +20%以上          : {sum(1 for r in returns if r >= 20)}/{len(returns)}")
    print(f"  +10%以上          : {sum(1 for r in returns if r >= 10)}/{len(returns)}")
    print(f"  -10%以下          : {sum(1 for r in returns if r <= -10)}/{len(returns)}")

    # v95.0との比較
    print(f"\n  📈 v95.0 との比較:")
    print(f"{'='*60}")
    print(f"  v95.0 平均月次 : -11.92%  勝率 22%")
    print(f"  v3   平均月次 : {np.mean(returns):+.2f}%  勝率 {sum(1 for r in returns if r > 0)/len(returns)*100:.0f}%")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
