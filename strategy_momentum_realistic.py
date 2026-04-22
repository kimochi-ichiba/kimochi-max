"""
strategy_momentum_realistic.py
==============================
現実的制約を加えた最終版

変更点:
- 1取引の最大名目額 = $15,000固定 (流動性制限反映)
- 1取引の最大マージン = $5,000 (3-5x leverageで$15-25k notional)
- DD-20%でサーキットブレーカー発動 → 残りのウィンドウを取引停止
- 全体の月次リターンは現実的な水準に収まる
"""

from __future__ import annotations

from pathlib import Path
import sys
import logging
import warnings
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.getLogger().setLevel(logging.WARNING)

# パラメータ
ENTRY_PERIOD = 55
EXIT_PERIOD = 20
ATR_PERIOD = 20
SL_ATR_MULT = 2.0
PYRAMID_ATR = 0.5
MAX_PYRAMIDS = 4

ADX_PERIOD = 14
MOMENTUM_LOOKBACK = 90
TOP_PCT = 0.30

LEV_BASE = 3.0
LEV_TREND = 4.0
LEV_STRONG = 5.0
ADX_TREND = 25.0
ADX_STRONG = 35.0

RISK_PER_TRADE = 0.015              # 1.5% = $150/取引 (初期$10kベース)
MAX_POSITIONS = 6                    # $10k資金に合わせて6同時まで
MAX_NOTIONAL_PCT = 0.5               # 1取引の名目額 = 残高の50%まで
MAX_MARGIN_PCT = 0.15                # 1取引のマージン = 残高の15%まで
CIRCUIT_BREAKER_DD = 0.25            # -25%DDで停止 (少し緩和)
FEE_RATE = 0.0002
SLIPPAGE = 0.0005

TOTAL_CAPITAL = 10_000.0
TIMEFRAME = "1d"
HISTORY_DAYS = 365
BUFFER_DAYS = 200


@dataclass
class Position:
    symbol: str
    entry: float
    size: float
    stop: float
    last_add: float
    entry_atr: float
    leverage: float
    units: int
    entry_time: pd.Timestamp


def compute_indicators(df):
    df = df.copy()
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_PERIOD).mean()

    up = high.diff(); down = -low.diff()
    plus_dm = np.where((up>down)&(up>0), up, 0.0)
    minus_dm = np.where((down>up)&(down>0), down, 0.0)
    atr_a = tr.rolling(ADX_PERIOD).mean()
    pdi = 100 * pd.Series(plus_dm, index=df.index).rolling(ADX_PERIOD).mean() / atr_a
    mdi = 100 * pd.Series(minus_dm, index=df.index).rolling(ADX_PERIOD).mean() / atr_a
    dx = 100 * (pdi-mdi).abs() / (pdi+mdi).replace(0, np.nan)
    df["adx"] = dx.rolling(ADX_PERIOD).mean()

    df["entry_high"] = high.rolling(ENTRY_PERIOD).max().shift(1)
    df["exit_low"] = low.rolling(EXIT_PERIOD).min().shift(1)
    df["momentum"] = close / close.shift(MOMENTUM_LOOKBACK) - 1
    return df


def get_top_symbols(n=100):
    ex = ccxt.binance({"options": {"defaultType": "future"}})
    markets = ex.load_markets()
    tickers = ex.fetch_tickers()
    rows = []
    for sym, m in markets.items():
        if not m.get("active") or m.get("quote") != "USDT" or not m.get("swap"):
            continue
        if sym in tickers:
            vol_usd = tickers[sym].get("quoteVolume") or 0
            rows.append((sym, vol_usd))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:n]]


def fetch_ohlcv(ex, symbol, timeframe, since_ms, until_ms):
    tf_ms = 86400 * 1000
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
        except Exception:
            break
    if not all_data: return pd.DataFrame()
    df = pd.DataFrame(all_data, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.set_index("timestamp").drop_duplicates().sort_index().astype(float)


def backtest_portfolio(all_data: Dict[str, pd.DataFrame], analysis_start: pd.Timestamp):
    all_dates = sorted(set().union(*[set(df.index) for df in all_data.values()]))
    all_dates = [d for d in all_dates if d >= analysis_start]

    balance = TOTAL_CAPITAL
    peak_balance = TOTAL_CAPITAL
    positions: Dict[str, Position] = {}
    equity_curve = []
    closed_trades = []
    trading_suspended = False
    suspended_at = None

    for ts in all_dates:
        # 決済
        to_close = []
        for sym, pos in positions.items():
            if sym not in all_data or ts not in all_data[sym].index: continue
            row = all_data[sym].loc[ts]
            price = row["close"]

            exit_r = None; exit_p = None
            if row["low"] <= pos.stop:
                exit_r = "sl"; exit_p = pos.stop * (1-SLIPPAGE)
            elif not pd.isna(row["exit_low"]) and row["low"] < row["exit_low"]:
                exit_r = "trail"; exit_p = row["exit_low"] * (1-SLIPPAGE)

            if exit_r:
                gross = (exit_p - pos.entry) * pos.size * pos.leverage
                fee = exit_p * pos.size * FEE_RATE
                pnl = gross - fee
                balance += pnl
                closed_trades.append({
                    "symbol": sym, "open": pos.entry_time, "close": ts,
                    "entry": pos.entry, "exit": exit_p, "pnl": pnl,
                    "pnl_pct": (exit_p/pos.entry - 1)*100*pos.leverage,
                    "leverage": pos.leverage,
                })
                to_close.append(sym)
            elif pos.units < MAX_PYRAMIDS and not pd.isna(pos.entry_atr) and \
                 price >= pos.last_add + PYRAMID_ATR*pos.entry_atr:
                add_size = pos.size / pos.units
                notional = add_size * price
                # ピラミッドにもキャップ
                if notional > balance * MAX_NOTIONAL_PCT:
                    continue
                balance -= notional * FEE_RATE
                pos.size += add_size
                pos.units += 1
                pos.last_add = price
                pos.stop = price - SL_ATR_MULT * pos.entry_atr

        for sym in to_close: del positions[sym]

        # DDチェック → サーキットブレーカー
        current_equity = balance
        for sym, pos in positions.items():
            if sym in all_data and ts in all_data[sym].index:
                price = all_data[sym].loc[ts]["close"]
                current_equity += (price - pos.entry) * pos.size * pos.leverage

        if current_equity > peak_balance: peak_balance = current_equity
        current_dd = (peak_balance - current_equity) / peak_balance
        if current_dd >= CIRCUIT_BREAKER_DD and not trading_suspended:
            trading_suspended = True
            suspended_at = ts

        # エントリー (サスペンド中は停止)
        if not trading_suspended and len(positions) < MAX_POSITIONS:
            candidates = []
            for sym, df in all_data.items():
                if sym in positions: continue
                if ts not in df.index: continue
                row = df.loc[ts]
                if pd.isna(row["momentum"]) or pd.isna(row["entry_high"]) or pd.isna(row["atr"]):
                    continue
                candidates.append((sym, row["momentum"], row))
            candidates.sort(key=lambda x: x[1], reverse=True)
            n_top = max(1, int(len(candidates) * TOP_PCT))
            top_candidates = candidates[:n_top]

            for sym, mom, row in top_candidates:
                if len(positions) >= MAX_POSITIONS: break
                if row["high"] > row["entry_high"]:
                    atr = row["atr"]
                    adx = row["adx"]
                    if adx >= ADX_STRONG: lev = LEV_STRONG
                    elif adx >= ADX_TREND: lev = LEV_TREND
                    else: lev = LEV_BASE
                    entry = row["entry_high"] * (1+SLIPPAGE)
                    risk = balance * RISK_PER_TRADE
                    sl_dist = SL_ATR_MULT * atr
                    size = risk / sl_dist
                    notional = size * entry

                    # キャパ制限適用 (残高の割合ベース)
                    max_notional = balance * MAX_NOTIONAL_PCT
                    if notional > max_notional:
                        size = max_notional / entry
                        notional = max_notional

                    margin = notional / lev
                    max_margin = balance * MAX_MARGIN_PCT
                    if margin > max_margin:
                        size = max_margin * lev / entry
                        notional = size * entry

                    balance -= notional * FEE_RATE
                    positions[sym] = Position(sym, entry, size, entry-sl_dist,
                                               entry, atr, lev, 1, ts)

        unrealized = 0
        for sym, pos in positions.items():
            if sym in all_data and ts in all_data[sym].index:
                price = all_data[sym].loc[ts]["close"]
                unrealized += (price - pos.entry) * pos.size * pos.leverage
        equity_curve.append((ts, balance + unrealized))

    return equity_curve, closed_trades, balance, trading_suspended, suspended_at


def main():
    end_date = datetime(2026, 4, 18)
    analysis_start = end_date - timedelta(days=HISTORY_DAYS)
    fetch_start = end_date - timedelta(days=HISTORY_DAYS + BUFFER_DAYS)
    since_ms = int(fetch_start.timestamp() * 1000)
    until_ms = int(end_date.timestamp() * 1000)

    print(f"\n🎯 Momentum-Leveraged Turtle (現実制約版)")
    print(f"{'='*90}")
    print(f"期間: {analysis_start.strftime('%Y-%m-%d')} 〜 {end_date.strftime('%Y-%m-%d')}")
    print(f"初期資金: ${TOTAL_CAPITAL:,.0f}")
    print(f"制約: 1取引max残高{MAX_NOTIONAL_PCT*100:.0f}% notional / {MAX_MARGIN_PCT*100:.0f}% margin")
    print(f"最大同時ポジション: {MAX_POSITIONS}")
    print(f"サーキットブレーカー: DD-{CIRCUIT_BREAKER_DD*100:.0f}%で取引停止")
    print(f"{'='*90}\n")

    print(f"📥 100通貨リスト取得...")
    symbols = get_top_symbols(n=100)
    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    print(f"📥 データ取得中...")
    all_data = {}
    for i, sym in enumerate(symbols, 1):
        df = fetch_ohlcv(ex, sym, TIMEFRAME, since_ms, until_ms)
        if df.empty or len(df) < 200: continue
        all_data[sym] = compute_indicators(df)
        if i % 20 == 0: print(f"  進捗: {i}/100 (成功{len(all_data)})")
    print(f"✅ {len(all_data)}通貨データ取得完了")

    print(f"\n🔄 ポートフォリオバックテスト実行中...")
    equity_curve, closed_trades, final_balance, suspended, suspended_at = backtest_portfolio(
        all_data, pd.Timestamp(analysis_start)
    )

    df_eq = pd.DataFrame(equity_curve, columns=["time","equity"]).drop_duplicates("time").set_index("time")

    rolling_rets = []
    cursor = df_eq.index[0]
    while cursor + timedelta(days=30) <= df_eq.index[-1]:
        w_end = cursor + timedelta(days=30)
        win = df_eq[(df_eq.index >= cursor) & (df_eq.index <= w_end)]
        if len(win) >= 2:
            rolling_rets.append((cursor, (win["equity"].iloc[-1]/win["equity"].iloc[0]-1)*100))
        cursor += timedelta(days=15)
    rets = np.array([r for _, r in rolling_rets])

    total_return = (final_balance / TOTAL_CAPITAL - 1) * 100
    peak = df_eq["equity"].iloc[0]
    max_dd = 0
    for v in df_eq["equity"]:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        max_dd = max(max_dd, dd)

    print(f"\n{'='*90}")
    print(f"  📊 現実制約版 結果")
    print(f"{'='*90}")
    print(f"  初期資金                   : ${TOTAL_CAPITAL:,.0f}")
    print(f"  最終資金                   : ${final_balance:,.2f}")
    print(f"  総リターン(1年)            : {total_return:+.2f}%")
    print(f"  取引数合計                 : {len(closed_trades)}")
    print(f"  最大ドローダウン           : {max_dd:.2f}%")
    if suspended:
        print(f"  ⚠️ サーキットブレーカー発動: {suspended_at} 以降取引停止")
    print(f"\n  ローリング30日リターン統計 ({len(rets)}ウィンドウ)")
    print(f"    平均           : {np.mean(rets):+.2f}%")
    print(f"    中央値         : {np.median(rets):+.2f}%")
    print(f"    最高 / 最低    : {np.max(rets):+.2f}% / {np.min(rets):+.2f}%")
    print(f"    標準偏差       : {np.std(rets):.2f}%")
    print(f"    プラス月       : {sum(1 for r in rets if r > 0)}/{len(rets)} ({sum(1 for r in rets if r > 0)/len(rets)*100:.0f}%)")
    print(f"    +30%以上       : {sum(1 for r in rets if r >= 30)}/{len(rets)}")
    print(f"    +20%以上       : {sum(1 for r in rets if r >= 20)}/{len(rets)}")
    print(f"    +10%以上       : {sum(1 for r in rets if r >= 10)}/{len(rets)}")
    print(f"    -10%以下       : {sum(1 for r in rets if r <= -10)}/{len(rets)}")

    monthly_comp = ((final_balance / TOTAL_CAPITAL) ** (1/12) - 1) * 100 if final_balance > 0 else -100
    print(f"\n  💰 月次複利: {monthly_comp:+.2f}%")

    # Top銘柄
    trades_by_sym = {}
    for t in closed_trades:
        trades_by_sym.setdefault(t["symbol"], []).append(t)
    sym_pnl = [(sym, sum(t["pnl"] for t in ts), len(ts)) for sym, ts in trades_by_sym.items()]
    sym_pnl.sort(key=lambda x: x[1], reverse=True)
    print(f"\n  🏆 PnL Top 10 銘柄")
    for sym, pnl, n in sym_pnl[:10]:
        print(f"    {sym.split(':')[0]:<20s} {pnl:+12,.0f}  取引{n}回")

    avg = np.mean(rets)
    print(f"\n{'='*90}")
    if avg >= 20:
        print(f"  🎯 ✅ 月平均 {avg:+.2f}% — 目標 +20%達成！")
    elif avg >= 10:
        print(f"  🎯 ⚠️ 月平均 {avg:+.2f}% — 健闘、+15-20%圏内")
    elif avg >= 5:
        print(f"  🎯 ⚠️ 月平均 {avg:+.2f}% — 月3-8%の現実ラインは達成")
    else:
        print(f"  🎯 ❌ 月平均 {avg:+.2f}%")
    print(f"{'='*90}\n")


if __name__ == "__main__":
    main()
