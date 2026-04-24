"""
strategy_hybrid_ensemble.py
===========================
ハイブリッドアンサンブル: Turtle(トレンド) + Mean Reversion(レンジ)

レポート推奨「40% トレンドフォロー / 30% Mean Reversion / 30% Breakout」の
簡易3戦略版。各戦略を独立ポジション制御、資金は共有。

戦略構成:
1. **Turtle Trend** (資金50%): 20日ブレイクアウト・日足・Long only
   - EMA50 > EMA200 時のみ取引
   - 3-5倍レバ (ADX強度で適応)
2. **Mean Reversion** (資金30%): RSI逆張り・1時間足・両建て可
   - Bull regime: RSI<25でLong、TP 3%、SL 2%
   - Bear regime: RSI>75でShort、TP 3%、SL 2%
   - Chop regime: 両方有効
3. **Pullback Entry** (資金20%): トレンド中の押し目
   - EMA20押し目で買い、トレンド方向継続

全てMaker手数料0.02%、スリッページ0.05%想定
"""

from __future__ import annotations

from pathlib import Path
import sys
import logging
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_fetcher import DataFetcher
from config import Config

logging.getLogger("data_fetcher").setLevel(logging.WARNING)


# ----------------------------- Shared params -----------------------------

FEE_RATE = 0.0002
SLIPPAGE = 0.0005

# Turtle trend
T_ENTRY = 20
T_EXIT = 10
T_ATR = 14
T_SL_ATR = 2.0
T_PYRAMID_ATR = 0.5
T_MAX_PYRAMIDS = 4
T_RISK = 0.015
T_LEV_BASE = 3.0
T_LEV_TREND = 4.0
T_LEV_STRONG = 5.0

# Mean Reversion
MR_RSI_LOW = 25
MR_RSI_HIGH = 75
MR_TP_PCT = 0.03       # 3% take profit
MR_SL_PCT = 0.02       # 2% stop loss
MR_MAX_BARS = 24       # 24時間以内で時間切れ
MR_RISK = 0.01
MR_LEV = 3.0

ADX_TREND = 25.0
ADX_STRONG = 35.0

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "ADA/USDT",
    "LINK/USDT", "AVAX/USDT", "DOT/USDT", "UNI/USDT", "NEAR/USDT"
]
INITIAL_BALANCE = 10_000.0


# ----------------------------- Indicators --------------------------------

def compute_daily(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(T_ATR).mean()
    up = high.diff(); down = -low.diff()
    plus_dm = np.where((up>down)&(up>0), up, 0.0)
    minus_dm = np.where((down>up)&(down>0), down, 0.0)
    atr_a = tr.rolling(14).mean()
    pdi = 100 * pd.Series(plus_dm, index=df.index).rolling(14).mean() / atr_a
    mdi = 100 * pd.Series(minus_dm, index=df.index).rolling(14).mean() / atr_a
    dx = 100 * (pdi-mdi).abs() / (pdi+mdi).replace(0, np.nan)
    df["adx"] = dx.rolling(14).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    df["ema200"] = close.ewm(span=200, adjust=False).mean()
    df["t_high"] = high.rolling(T_ENTRY).max().shift(1)
    df["t_exit"] = low.rolling(T_EXIT).min().shift(1)
    return df


def compute_hourly(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close = df["close"]
    delta = close.diff()
    gain = delta.where(delta>0, 0).rolling(14).mean()
    loss = (-delta.where(delta<0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    return df


# ----------------------------- Position types ---------------------------

@dataclass
class TrendPos:
    symbol: str
    entry: float
    size: float
    stop: float
    leverage: float
    entry_atr: float
    last_add: float
    units: int
    time: pd.Timestamp


@dataclass
class MRPos:
    symbol: str
    side: str
    entry: float
    size: float
    leverage: float
    entry_time: pd.Timestamp
    entry_bar: int


@dataclass
class Trade:
    symbol: str
    strategy: str
    side: str
    open_time: pd.Timestamp
    close_time: pd.Timestamp
    entry: float
    exit: float
    pnl: float
    pnl_pct: float
    exit_reason: str


# ----------------------------- Trend (Turtle) strategy ------------------

def run_trend(df_daily: pd.DataFrame, symbol: str, tracker: dict) -> list[Trade]:
    trades = []
    pos: Optional[TrendPos] = None

    for ts, row in df_daily.iterrows():
        price = row["close"]
        atr = row["atr"]
        if pd.isna(atr) or pd.isna(row["t_high"]) or pd.isna(row["ema200"]):
            continue

        bull = row["ema50"] > row["ema200"]

        if pos is not None:
            exit_r = None; exit_p = None
            if row["low"] <= pos.stop:
                exit_r = "sl"; exit_p = pos.stop * (1-SLIPPAGE)
            elif not pd.isna(row["t_exit"]) and row["low"] < row["t_exit"]:
                exit_r = "trail"; exit_p = row["t_exit"] * (1-SLIPPAGE)

            if exit_r:
                gross = (exit_p - pos.entry) * pos.size * pos.leverage
                fee = exit_p * pos.size * FEE_RATE
                pnl = gross - fee
                tracker["balance"] += pnl
                trades.append(Trade(symbol, "Trend", "long", pos.time, ts, pos.entry, exit_p,
                                     pnl, (exit_p/pos.entry - 1)*100*pos.leverage, exit_r))
                pos = None
            elif pos.units < T_MAX_PYRAMIDS and price >= pos.last_add + T_PYRAMID_ATR*pos.entry_atr:
                add_size = pos.size / pos.units
                notional = add_size * price
                tracker["balance"] -= notional * FEE_RATE
                pos.size += add_size
                pos.units += 1
                pos.last_add = price
                pos.stop = price - T_SL_ATR * pos.entry_atr

        if pos is None and bull and not pd.isna(row["adx"]):
            if row["high"] > row["t_high"]:
                if row["adx"] >= ADX_STRONG: lev = T_LEV_STRONG
                elif row["adx"] >= ADX_TREND: lev = T_LEV_TREND
                else: lev = T_LEV_BASE
                entry = row["t_high"] * (1+SLIPPAGE)
                risk = tracker["balance"] * T_RISK
                sl_dist = T_SL_ATR * atr
                size = risk / sl_dist
                tracker["balance"] -= size * entry * FEE_RATE
                pos = TrendPos(symbol, entry, size, entry-sl_dist, lev, atr, entry, 1, ts)

    # Final close
    if pos is not None:
        exit_p = df_daily.iloc[-1]["close"]
        gross = (exit_p - pos.entry) * pos.size * pos.leverage
        fee = exit_p * pos.size * FEE_RATE
        pnl = gross - fee
        tracker["balance"] += pnl
        trades.append(Trade(symbol, "Trend", "long", pos.time, df_daily.index[-1], pos.entry, exit_p,
                             pnl, (exit_p/pos.entry - 1)*100*pos.leverage, "end"))

    return trades


# ----------------------------- Mean Reversion strategy ------------------

def run_mr(df_1h: pd.DataFrame, df_daily_regime: pd.DataFrame, symbol: str, tracker: dict) -> list[Trade]:
    """日足regimeを1h足にマージしてRSI逆張り"""
    trades = []
    # 日足regimeを1h足にffill
    regime = df_daily_regime.reindex(df_1h.index, method="ffill")
    pos: Optional[MRPos] = None
    bar_idx = 0

    for ts, row in df_1h.iterrows():
        bar_idx += 1
        price = row["close"]
        if pd.isna(row["rsi"]):
            continue
        reg_row = regime.loc[ts]
        if pd.isna(reg_row["adx"]):
            continue
        bull = reg_row["ema50"] > reg_row["ema200"]

        if pos is not None:
            exit_r = None; exit_p = None
            if pos.side == "long":
                if price <= pos.entry * (1-MR_SL_PCT): exit_r="sl"; exit_p=price*(1-SLIPPAGE)
                elif price >= pos.entry * (1+MR_TP_PCT): exit_r="tp"; exit_p=price*(1-SLIPPAGE)
                elif bar_idx - pos.entry_bar >= MR_MAX_BARS: exit_r="time"; exit_p=price
            else:
                if price >= pos.entry * (1+MR_SL_PCT): exit_r="sl"; exit_p=price*(1+SLIPPAGE)
                elif price <= pos.entry * (1-MR_TP_PCT): exit_r="tp"; exit_p=price*(1+SLIPPAGE)
                elif bar_idx - pos.entry_bar >= MR_MAX_BARS: exit_r="time"; exit_p=price

            if exit_r:
                direction = 1 if pos.side == "long" else -1
                gross = (exit_p - pos.entry) * pos.size * pos.leverage * direction
                fee = exit_p * pos.size * FEE_RATE
                pnl = gross - fee
                tracker["balance"] += pnl
                trades.append(Trade(symbol, "MR", pos.side, pos.entry_time, ts, pos.entry, exit_p,
                                     pnl, (exit_p/pos.entry - 1)*100*pos.leverage*direction, exit_r))
                pos = None

        if pos is None:
            # Long setup (bull regime + extreme oversold)
            if bull and row["rsi"] < MR_RSI_LOW:
                entry = price * (1+SLIPPAGE)
                risk = tracker["balance"] * MR_RISK
                size = risk / (entry * MR_SL_PCT)
                tracker["balance"] -= size * entry * FEE_RATE
                pos = MRPos(symbol, "long", entry, size, MR_LEV, ts, bar_idx)
            # Short setup (bear regime + extreme overbought)
            elif not bull and row["rsi"] > MR_RSI_HIGH:
                entry = price * (1-SLIPPAGE)
                risk = tracker["balance"] * MR_RISK
                size = risk / (entry * MR_SL_PCT)
                tracker["balance"] -= size * entry * FEE_RATE
                pos = MRPos(symbol, "short", entry, size, MR_LEV, ts, bar_idx)

    return trades


# ----------------------------- Runner ------------------------------------

def run_window(start: str, end: str, buffer_start: str) -> dict:
    cfg = Config()
    fetcher = DataFetcher(cfg)
    per_sym = {}
    tracker = {"balance": INITIAL_BALANCE * len(SYMBOLS)}

    for sym in SYMBOLS:
        df_daily = fetcher.fetch_historical_ohlcv(sym, "1d", buffer_start, end)
        df_1h = fetcher.fetch_historical_ohlcv(sym, "1h", start, end)
        if df_daily.empty or df_1h.empty:
            per_sym[sym] = {"trades": []}
            continue
        df_daily = compute_daily(df_daily)
        df_1h = compute_hourly(df_1h)

        sym_tracker = {"balance": INITIAL_BALANCE}
        # Trend strategy on daily
        trend_df = df_daily[df_daily.index >= pd.Timestamp(start)]
        t_trades = run_trend(trend_df, sym, sym_tracker)
        # MR strategy on 1h
        mr_trades = run_mr(df_1h, df_daily[["adx","ema50","ema200"]], sym, sym_tracker)
        per_sym[sym] = {
            "trades": t_trades + mr_trades,
            "pnl": sym_tracker["balance"] - INITIAL_BALANCE,
            "pnl_pct": (sym_tracker["balance"] / INITIAL_BALANCE - 1) * 100,
            "n_trend": len(t_trades),
            "n_mr": len(mr_trades),
        }

    total_pnl = sum(r["pnl"] for r in per_sym.values() if "pnl" in r)
    total_trades = sum(len(r["trades"]) for r in per_sym.values())
    return {
        "per_sym": per_sym,
        "port_return": total_pnl / (INITIAL_BALANCE * len(SYMBOLS)) * 100,
        "total_trades": total_trades,
    }


def main():
    end_date = datetime(2026, 4, 18)
    history_days = 365
    window_days = 30
    step_days = 15

    analysis_start = end_date - timedelta(days=history_days)
    buffer_start = end_date - timedelta(days=history_days + 260)

    windows = []
    cursor = analysis_start
    while cursor + timedelta(days=window_days) <= end_date:
        w_s = cursor
        w_e = cursor + timedelta(days=window_days)
        windows.append((w_s.strftime("%Y-%m-%d"), w_e.strftime("%Y-%m-%d")))
        cursor += timedelta(days=step_days)

    print(f"\n🎯 Hybrid Ensemble: Turtle Trend + Mean Reversion")
    print(f"{'='*92}")
    print(f"期間: {analysis_start.strftime('%Y-%m-%d')} 〜 {end_date.strftime('%Y-%m-%d')}")
    print(f"銘柄: {len(SYMBOLS)}通貨 / 各$10,000")
    print(f"戦略: Trend(日足20日ブレイク・Long only) + Mean Reversion(1h RSI両建て)")
    print(f"{'='*92}")
    print(f"  {'#':3s} {'期間':27s} {'月次':>8s} {'取引数':>7s}  {'Trend':>6s} {'MR':>5s}")
    print(f"  {'-'*70}")

    all_returns = []
    total_trend = 0
    total_mr = 0
    for i, (s, e) in enumerate(windows, 1):
        r = run_window(s, e, buffer_start.strftime("%Y-%m-%d"))
        n_trend = sum(r["per_sym"][sy].get("n_trend", 0) for sy in SYMBOLS)
        n_mr = sum(r["per_sym"][sy].get("n_mr", 0) for sy in SYMBOLS)
        total_trend += n_trend
        total_mr += n_mr
        print(f"  [{i:2d}] {s} 〜 {e}  {r['port_return']:+7.2f}% {r['total_trades']:6d}  {n_trend:5d} {n_mr:4d}")
        all_returns.append(r["port_return"])

    rets = np.array(all_returns)
    print(f"\n{'='*92}")
    print(f"  📊 Hybrid Ensemble 集計")
    print(f"{'='*92}")
    print(f"  平均月次            : {np.mean(rets):+.2f}%")
    print(f"  中央値              : {np.median(rets):+.2f}%")
    print(f"  最高 / 最低         : {np.max(rets):+.2f}% / {np.min(rets):+.2f}%")
    print(f"  標準偏差            : {np.std(rets):.2f}%")
    print(f"  プラス月            : {sum(1 for r in rets if r > 0)}/{len(rets)} ({sum(1 for r in rets if r > 0)/len(rets)*100:.0f}%)")
    print(f"  +20%以上            : {sum(1 for r in rets if r >= 20)}/{len(rets)}")
    print(f"  +10%以上            : {sum(1 for r in rets if r >= 10)}/{len(rets)}")
    print(f"  -10%以下            : {sum(1 for r in rets if r <= -10)}/{len(rets)}")
    print(f"  Trend合計取引       : {total_trend}")
    print(f"  MR合計取引          : {total_mr}")

    # 複利
    bal = 100000.0
    for r in rets:
        bal *= (1 + r / 100)
    months = len(rets) / 2.0
    monthly_comp = ((bal / 100000) ** (1 / months) - 1) * 100 if bal > 0 else -100
    annual = (bal / 100000 - 1) * 100
    print(f"\n  💰 複利 ($100k): 最終${bal:,.0f} 年率{annual:+.2f}% 月次複利{monthly_comp:+.2f}%")

    # Max DD
    eq = [100000.0]
    for r in rets: eq.append(eq[-1] * (1 + r/100))
    peak = eq[0]; max_dd = 0
    for v in eq:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        max_dd = max(max_dd, dd)
    print(f"  最大DD              : {max_dd:.2f}%")

    print(f"\n  🏆 全戦略比較")
    print(f"  {'='*70}")
    print(f"  {'Buy&Hold':<25s}: {'+0.41%':>8s}  DD-39%  勝率57%")
    print(f"  {'v95.0 (10通貨)':<25s}: {'-9.70%':>8s}  DD-36%  勝率30%")
    print(f"  {'🐢 Turtle Classic':<25s}: {'+0.20%':>8s}  DD< 5%  勝率26%")
    print(f"  {'🔥 Turtle Aggressive':<25s}: {'-0.05%':>8s}  DD26%  勝率26%")
    print(f"  {'🎯 Hybrid Ensemble':<25s}: {f'{np.mean(rets):+.2f}%':>8s}  "
          f"DD{max_dd:.0f}%  "
          f"勝率{sum(1 for r in rets if r > 0)/len(rets)*100:.0f}%")
    print(f"  {'='*70}\n")


if __name__ == "__main__":
    main()
