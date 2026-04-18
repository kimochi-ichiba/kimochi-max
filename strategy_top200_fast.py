"""
strategy_top200_fast.py
=======================
Binance上場・USDT建て上位100+通貨で Classic Turtle を一括検証

効率化ポイント:
- データ取得は各通貨1回のみ (365+日まとめて)
- 1通貨につき連続バックテスト1回→equity curveからローリング30日リターンを計算
- 全通貨を並列的に評価 (Pythonのmulti-thread不使用・シーケンシャルだが高速化)

戦略: Classic Turtle (最良結果の設定)
- 55日ブレイクアウト → 20日反対ブレイクで利確/損切り
- 2 ATR 初期SL
- ピラミッディング 0.5 ATR毎に最大4段
- レバレッジ3倍
- 手数料 0.06%/片側 (先物テイカー想定・保守的)
"""

from __future__ import annotations

import sys
import logging
import warnings
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
sys.path.insert(0, "/Users/sanosano/projects/crypto-bot-pro")

logging.getLogger().setLevel(logging.WARNING)

# Turtle params
ENTRY_PERIOD = 55
EXIT_PERIOD = 20
ATR_PERIOD = 20
SL_ATR_MULT = 2.0
PYRAMID_ATR_STEP = 0.5
MAX_PYRAMIDS = 4
LEVERAGE = 3.0
RISK_PER_TRADE = 0.01
FEE_RATE = 0.0006
SLIPPAGE = 0.0005

INITIAL_BALANCE = 10_000.0
TIMEFRAME = "1d"
HISTORY_DAYS = 365
BUFFER_DAYS = 120  # 55日breakout + ATR用
WINDOW_DAYS = 30
STEP_DAYS = 15


@dataclass
class Position:
    entry: float
    size: float
    stop: float
    last_add: float
    entry_atr: float
    units: int
    time: pd.Timestamp


@dataclass
class TradeLog:
    open_time: pd.Timestamp
    close_time: pd.Timestamp
    pnl: float
    pnl_pct: float


def compute_indicators(df):
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_PERIOD).mean()
    df["entry_high"] = high.rolling(ENTRY_PERIOD).max().shift(1)
    df["exit_low"] = low.rolling(EXIT_PERIOD).min().shift(1)
    return df


def backtest_symbol(df, initial=INITIAL_BALANCE):
    """1通貨の連続バックテスト → (equity_curve, trades)"""
    pos: Optional[Position] = None
    balance = initial
    equity_curve = []
    trades = []

    for ts, row in df.iterrows():
        price = row["close"]
        atr = row["atr"]
        if pd.isna(atr) or pd.isna(row["entry_high"]):
            equity_curve.append((ts, balance))
            continue

        # 決済判定
        if pos is not None:
            exit_r = None
            exit_p = None
            if row["low"] <= pos.stop:
                exit_r = "sl"; exit_p = pos.stop * (1-SLIPPAGE)
            elif not pd.isna(row["exit_low"]) and row["low"] < row["exit_low"]:
                exit_r = "trail"; exit_p = row["exit_low"] * (1-SLIPPAGE)

            if exit_r:
                gross = (exit_p - pos.entry) * pos.size * LEVERAGE
                fee = exit_p * pos.size * FEE_RATE
                pnl = gross - fee
                balance += pnl
                trades.append(TradeLog(pos.time, ts, pnl,
                                        (exit_p/pos.entry - 1)*100*LEVERAGE))
                pos = None
            elif pos.units < MAX_PYRAMIDS and price >= pos.last_add + PYRAMID_ATR_STEP*pos.entry_atr:
                add_size = pos.size / pos.units
                notional = add_size * price
                balance -= notional * FEE_RATE
                pos.size += add_size
                pos.units += 1
                pos.last_add = price
                pos.stop = price - SL_ATR_MULT * pos.entry_atr

        # エントリー判定 (Long only)
        if pos is None and row["high"] > row["entry_high"]:
            entry = row["entry_high"] * (1+SLIPPAGE)
            risk = balance * RISK_PER_TRADE
            sl_dist = SL_ATR_MULT * atr
            size = risk / sl_dist
            balance -= size * entry * FEE_RATE
            pos = Position(entry, size, entry-sl_dist, entry, atr, 1, ts)

        equity_curve.append((ts, balance + (_unrealized(pos, price) if pos else 0)))

    # 最終決済
    if pos is not None:
        exit_p = df.iloc[-1]["close"]
        gross = (exit_p - pos.entry) * pos.size * LEVERAGE
        fee = exit_p * pos.size * FEE_RATE
        balance += gross - fee
        trades.append(TradeLog(pos.time, df.index[-1], gross-fee,
                                (exit_p/pos.entry - 1)*100*LEVERAGE))

    return equity_curve, trades, balance


def _unrealized(pos: Optional[Position], price: float) -> float:
    if pos is None: return 0
    return (price - pos.entry) * pos.size * LEVERAGE


def compute_window_returns(equity_curve, window_days=30, step_days=15):
    """equity curve → ローリング30日リターン"""
    if not equity_curve:
        return []
    df = pd.DataFrame(equity_curve, columns=["time","equity"]).drop_duplicates("time").set_index("time")
    df.index = pd.to_datetime(df.index)
    results = []
    start = df.index[0]
    end = df.index[-1]
    cursor = start
    while cursor + timedelta(days=window_days) <= end:
        w_end = cursor + timedelta(days=window_days)
        win_df = df[(df.index >= cursor) & (df.index <= w_end)]
        if len(win_df) >= 2:
            ret = (win_df["equity"].iloc[-1] / win_df["equity"].iloc[0] - 1) * 100
            results.append((cursor, ret))
        cursor += timedelta(days=step_days)
    return results


def get_top_symbols(n=100):
    """Binance先物上位をボリューム順に取得"""
    ex = ccxt.binance({"options": {"defaultType": "future"}})
    markets = ex.load_markets()
    tickers = ex.fetch_tickers()
    # USDT建て・アクティブのみ
    rows = []
    for sym, m in markets.items():
        if not m.get("active"): continue
        if m.get("quote") != "USDT": continue
        if not m.get("swap"): continue  # perpetual only
        if sym in tickers:
            t = tickers[sym]
            vol_usd = (t.get("quoteVolume") or 0)
            rows.append((sym, vol_usd))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:n]]


def fetch_ohlcv_safe(ex, symbol, timeframe, since_ms, until_ms):
    tf_ms = 86400 * 1000 if timeframe == "1d" else 3600 * 1000
    all_data = []
    current = since_ms
    while current < until_ms:
        try:
            batch = ex.fetch_ohlcv(symbol, timeframe, since=current, limit=1000)
            if not batch: break
            batch = [c for c in batch if c[0] < until_ms]
            all_data.extend(batch)
            if len(batch) < 1000: break
            current = batch[-1][0] + tf_ms
            time.sleep(0.1)
        except Exception as e:
            print(f"  ⚠️ {symbol}: {e}")
            break
    if not all_data: return pd.DataFrame()
    df = pd.DataFrame(all_data, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.set_index("timestamp").drop_duplicates().sort_index().astype(float)
    return df


def main():
    end_date = datetime(2026, 4, 18)
    fetch_start = end_date - timedelta(days=HISTORY_DAYS + BUFFER_DAYS)
    analysis_start = end_date - timedelta(days=HISTORY_DAYS)
    since_ms = int(fetch_start.timestamp() * 1000)
    until_ms = int(end_date.timestamp() * 1000)
    analysis_start_ts = pd.Timestamp(analysis_start)

    print(f"\n🌐 Top 100+ USDT-Perp通貨 Classic Turtle検証")
    print(f"{'='*90}")
    print(f"期間: {analysis_start.strftime('%Y-%m-%d')} 〜 {end_date.strftime('%Y-%m-%d')} (365日)")
    print(f"{'='*90}")

    print(f"📥 Binance先物市場リスト取得中...")
    symbols = get_top_symbols(n=100)
    print(f"  対象: {len(symbols)}通貨 (上位出来高)")
    print(f"  上位10: {', '.join([s.split(':')[0] for s in symbols[:10]])}")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})

    per_symbol_windows = {}  # symbol → [(start, return), ...]
    per_symbol_final = {}
    success_count = 0
    fail_count = 0

    for i, sym in enumerate(symbols, 1):
        try:
            df = fetch_ohlcv_safe(ex, sym, TIMEFRAME, since_ms, until_ms)
            if df.empty or len(df) < 200:
                fail_count += 1
                continue
            df = compute_indicators(df)
            df = df[df.index >= pd.Timestamp(fetch_start)]
            equity, trades, final_bal = backtest_symbol(df)
            # 分析期間のみの資産曲線
            analysis_equity = [(t, e) for t, e in equity if t >= analysis_start_ts]
            if len(analysis_equity) < 30:
                fail_count += 1
                continue
            win_rets = compute_window_returns(analysis_equity, WINDOW_DAYS, STEP_DAYS)
            per_symbol_windows[sym] = win_rets
            per_symbol_final[sym] = {
                "final": final_bal,
                "return_pct": (final_bal / INITIAL_BALANCE - 1) * 100,
                "trades": len(trades),
            }
            success_count += 1
            if i % 10 == 0:
                print(f"  進捗: {i}/{len(symbols)} (成功{success_count} / 失敗{fail_count})")
        except Exception as e:
            fail_count += 1
            continue

    print(f"\n  ✅ 成功 {success_count} / 失敗 {fail_count}")

    # ポートフォリオ統計: 各ウィンドウで全通貨の均等平均
    # まず全ウィンドウの開始時刻を統一
    all_window_starts = set()
    for wins in per_symbol_windows.values():
        for w_start, _ in wins:
            all_window_starts.add(w_start)
    sorted_starts = sorted(all_window_starts)

    # 各ウィンドウで「その時点でデータがある全通貨の平均」
    portfolio_rets = []
    for w_start in sorted_starts:
        rets_this_window = []
        for sym, wins in per_symbol_windows.items():
            for ws, ret in wins:
                if ws == w_start:
                    rets_this_window.append(ret)
                    break
        if rets_this_window:
            portfolio_rets.append((w_start, np.mean(rets_this_window), len(rets_this_window)))

    print(f"\n{'='*90}")
    print(f"  📊 全通貨均等加重ポートフォリオ: 30日ローリング月次リターン")
    print(f"{'='*90}")
    print(f"  {'期間開始':12s} {'平均月次':>10s} {'含む通貨数':>10s}")
    print(f"  {'-'*50}")
    for ws, avg, n in portfolio_rets:
        print(f"  {ws.strftime('%Y-%m-%d'):12s} {avg:+8.2f}% {n:>10d}")

    rets = np.array([r for _, r, _ in portfolio_rets])
    print(f"\n  📈 統計")
    print(f"  {'='*60}")
    print(f"  ウィンドウ数       : {len(rets)}")
    print(f"  平均月次           : {np.mean(rets):+.2f}%")
    print(f"  中央値             : {np.median(rets):+.2f}%")
    print(f"  最高 / 最低        : {np.max(rets):+.2f}% / {np.min(rets):+.2f}%")
    print(f"  標準偏差           : {np.std(rets):.2f}%")
    print(f"  プラス月           : {sum(1 for r in rets if r > 0)}/{len(rets)}")
    print(f"  +30%以上           : {sum(1 for r in rets if r >= 30)}/{len(rets)}")
    print(f"  +20%以上           : {sum(1 for r in rets if r >= 20)}/{len(rets)}")
    print(f"  +10%以上           : {sum(1 for r in rets if r >= 10)}/{len(rets)}")

    # 全期間複利
    bal = 100000
    for r in rets:
        bal *= (1 + r/100)
    months = len(rets) / 2.0
    mcomp = ((bal/100000)**(1/months) - 1) * 100 if bal > 0 else -100
    print(f"\n  💰 複利: $100k → ${bal:,.0f} (年率{(bal/100000-1)*100:+.1f}% / 月次{mcomp:+.2f}%)")

    # 個別通貨ランキング Top 20
    print(f"\n  🏆 個別通貨365日リターン Top 20")
    print(f"  {'='*60}")
    sorted_syms = sorted(per_symbol_final.items(), key=lambda x: x[1]["return_pct"], reverse=True)
    print(f"  {'通貨':<20s} {'総リターン':>12s} {'取引数':>8s}")
    for sym, r in sorted_syms[:20]:
        print(f"  {sym.split(':')[0]:<20s} {r['return_pct']:+10.2f}% {r['trades']:>8d}")

    print(f"\n  🏆 Bottom 10 (損失大)")
    for sym, r in sorted_syms[-10:]:
        print(f"  {sym.split(':')[0]:<20s} {r['return_pct']:+10.2f}% {r['trades']:>8d}")

    # 判定
    print(f"\n{'='*90}")
    avg = np.mean(rets)
    if avg >= 20:
        print(f"  🎯 ✅ 月{avg:.1f}% — 目標月20%以上を達成！")
    elif avg >= 10:
        print(f"  🎯 ⚠️ 月{avg:.1f}% — 目標+20-30%には届かないが健闘")
    elif avg >= 0:
        print(f"  🎯 ⚠️ 月{avg:.1f}% — プラスだが目標には遠い")
    else:
        print(f"  🎯 ❌ 月{avg:.1f}% — まだ損失、戦略変更が必要")
    print(f"{'='*90}\n")


if __name__ == "__main__":
    main()
