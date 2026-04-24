"""
iterate_10pct_v2.py
===================
ETH+8.22%を+10%以上に引き上げる第2弾

戦略:
1. ETH 4倍レバ Trend Follow (より高レバ、トレンド時のみ)
2. ETH 5倍レバ Trend Follow
3. ETH 2倍 Buy&Hold (安全版で下限確認)
4. ETH 2.5倍 Trend Follow (バランス型)
5. BTC+ETH 均等 3倍Buy&Hold (分散版)
6. BTC+ETH 均等 3倍 Trend Follow (分散+トレンドフィルター)
7. ETH 3倍 Trend Follow + Chandelier Exit (利益伸ばし)
8. ETH Partial Profit (2倍で利確、残りRunner)
"""

from __future__ import annotations

from pathlib import Path
import sys
import logging
import warnings
import time
from datetime import datetime, timedelta
from typing import Dict

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger().setLevel(logging.WARNING)

INITIAL = 10_000.0
END_DATE = datetime(2026, 4, 18)
START_DATE = END_DATE - timedelta(days=365)
FETCH_START = END_DATE - timedelta(days=365 + 250)
FEE = 0.0006
SLIP = 0.001


def fetch_ohlcv(ex, symbol, since_ms, until_ms):
    tf_ms = 86400 * 1000
    all_data = []
    current = since_ms
    while current < until_ms:
        try:
            batch = ex.fetch_ohlcv(symbol, "1d", since=current, limit=1000)
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


def strategy_trend_follow(df: pd.DataFrame, leverage: float, label: str, use_chandelier=False) -> dict:
    """EMA50>EMA200でLong、逆で決済。Chandelier オプション付き"""
    df = df.copy()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    high = df["high"]; low = df["low"]; close = df["close"]
    tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(20).mean()
    df = df[df.index >= pd.Timestamp(START_DATE)]

    balance = INITIAL
    pos_qty = 0; pos_entry = 0; trail_high = 0
    equity = []
    trades = 0

    for ts, row in df.iterrows():
        if pd.isna(row["ema200"]) or pd.isna(row["atr"]):
            equity.append(balance); continue
        bull = row["ema50"] > row["ema200"]
        price = row["close"]

        # Exit判定
        exit_signal = False
        if pos_qty > 0:
            if not bull:
                exit_signal = True
            elif use_chandelier:
                trail_high = max(trail_high, row["high"])
                chandelier_stop = trail_high - 3.0 * row["atr"]
                if row["low"] <= chandelier_stop:
                    exit_signal = True

        if exit_signal and pos_qty > 0:
            exit_p = price * (1 - SLIP)
            gross = (exit_p - pos_entry) * pos_qty
            fee = exit_p * pos_qty * FEE
            balance += gross - fee
            pos_qty = 0; trail_high = 0

        # Entry
        if pos_qty == 0 and bull:
            entry = price * (1 + SLIP)
            notional = balance * 0.95 * leverage
            pos_qty = notional / entry
            pos_entry = entry
            trail_high = entry
            balance -= notional * FEE
            trades += 1

        eq = balance + (pos_qty * (price - pos_entry) if pos_qty > 0 else 0)
        equity.append(eq)

    if pos_qty > 0:
        exit_p = df.iloc[-1]["close"] * (1 - SLIP)
        balance += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE

    final = balance
    total_ret = (final/INITIAL - 1) * 100
    m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    peak = equity[0] if equity else INITIAL
    max_dd = 0
    for v in equity:
        if v > peak: peak = v
        if peak > 0:
            dd = (peak - v) / peak * 100
            max_dd = max(max_dd, dd)
    return {"name": label, "final": final, "return": total_ret,
            "monthly": m_comp, "dd": max_dd, "trades": trades}


def strategy_buyhold_leveraged(df: pd.DataFrame, leverage: float, label: str) -> dict:
    """レバ付きBuy&Hold + 清算チェック"""
    df = df[df.index >= pd.Timestamp(START_DATE)].copy()
    if df.empty: return {"name": label, "final": INITIAL, "return": 0, "monthly": 0, "dd": 0, "trades": 0}

    entry = df["close"].iloc[0] * (1 + SLIP)
    notional = INITIAL * leverage
    qty = notional / entry
    balance = INITIAL - notional * FEE
    liquidated = False
    max_dd = 0

    for p in df["close"]:
        current_equity = balance + (p - entry) * qty
        dd = (1 - current_equity / INITIAL) * 100
        if dd > max_dd: max_dd = dd
        # 清算チェック (equity <= 0)
        if current_equity <= 0:
            liquidated = True
            balance = 0
            break

    if not liquidated:
        exit_p = df["close"].iloc[-1] * (1 - SLIP)
        balance += (exit_p - entry) * qty - exit_p * qty * FEE

    final = max(balance, 0)
    total_ret = (final/INITIAL - 1) * 100
    m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    return {"name": label + (" (清算)" if liquidated else ""), "final": final,
            "return": total_ret, "monthly": m_comp, "dd": max_dd, "trades": 2}


def strategy_split_portfolio(dfs: Dict[str, pd.DataFrame], symbols: list, leverage: float,
                              label: str, trend_follow: bool) -> dict:
    """BTC/ETH均等配分。trend_follow=Trueなら各々EMAクロス、=FalseならBuy&Hold"""
    # 各銘柄を個別戦略で走らせて、その結果を合算
    total_final = 0
    total_dd = 0
    for sym in symbols:
        if sym not in dfs: continue
        df_sym = dfs[sym]
        if trend_follow:
            r = strategy_trend_follow(df_sym, leverage, f"{sym} {leverage}x")
        else:
            r = strategy_buyhold_leveraged(df_sym, leverage, f"{sym} {leverage}x")
        # 各銘柄にINITIAL/len(symbols)ずつ割当
        alloc = INITIAL / len(symbols)
        # 割合的にfinalを計算
        sym_final = alloc * (r["final"] / INITIAL)
        total_final += sym_final
        total_dd = max(total_dd, r["dd"])

    total_ret = (total_final/INITIAL - 1) * 100
    m_comp = ((total_final/INITIAL) ** (1/12) - 1) * 100 if total_final > 0 else -100
    return {"name": label, "final": total_final, "return": total_ret,
            "monthly": m_comp, "dd": total_dd, "trades": 0}


def main():
    since_ms = int(FETCH_START.timestamp() * 1000)
    until_ms = int(END_DATE.timestamp() * 1000)

    print(f"\n🎯 月+10%達成まで反復検証 v2")
    print(f"{'='*90}")
    print(f"期間: {START_DATE.strftime('%Y-%m-%d')} 〜 {END_DATE.strftime('%Y-%m-%d')}")
    print(f"初期資金: ${INITIAL:,.0f}  /  目標: 月+10%\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    print(f"📥 データ取得中...")
    btc = fetch_ohlcv(ex, "BTC/USDT:USDT", since_ms, until_ms)
    eth = fetch_ohlcv(ex, "ETH/USDT:USDT", since_ms, until_ms)
    sol = fetch_ohlcv(ex, "SOL/USDT:USDT", since_ms, until_ms)
    dfs = {"BTC": btc, "ETH": eth, "SOL": sol}
    print(f"✅ BTC/ETH/SOL 取得完了\n")

    results = []

    tests = [
        ("ETH 4x Trend Follow",        lambda: strategy_trend_follow(eth, 4.0, "ETH 4x Trend")),
        ("ETH 5x Trend Follow",        lambda: strategy_trend_follow(eth, 5.0, "ETH 5x Trend")),
        ("ETH 3x Trend + Chandelier",  lambda: strategy_trend_follow(eth, 3.0, "ETH 3x+Chand", use_chandelier=True)),
        ("ETH 4x Trend + Chandelier",  lambda: strategy_trend_follow(eth, 4.0, "ETH 4x+Chand", use_chandelier=True)),
        ("ETH 2x Buy&Hold",            lambda: strategy_buyhold_leveraged(eth, 2.0, "ETH 2x B&H")),
        ("ETH 2.5x Buy&Hold",          lambda: strategy_buyhold_leveraged(eth, 2.5, "ETH 2.5x B&H")),
        ("ETH 3x Buy&Hold (前回)",      lambda: strategy_buyhold_leveraged(eth, 3.0, "ETH 3x B&H")),
        ("BTC+ETH 3x B&H (均等)",       lambda: strategy_split_portfolio(dfs, ["BTC","ETH"], 3.0, "BTC+ETH 3x B&H", False)),
        ("BTC+ETH+SOL 3x B&H (均等)",   lambda: strategy_split_portfolio(dfs, ["BTC","ETH","SOL"], 3.0, "BTC/ETH/SOL 3x B&H", False)),
        ("BTC+ETH 3x Trend (均等)",     lambda: strategy_split_portfolio(dfs, ["BTC","ETH"], 3.0, "BTC+ETH 3x Trend", True)),
        ("BTC+ETH 4x Trend (均等)",     lambda: strategy_split_portfolio(dfs, ["BTC","ETH"], 4.0, "BTC+ETH 4x Trend", True)),
    ]

    for name, fn in tests:
        print(f"🔬 {name}")
        r = fn()
        results.append(r)
        status = "✅" if r["monthly"] >= 10 else ("🎯" if r["monthly"] >= 5 else "⚠️")
        print(f"   {status} 月次: {r['monthly']:+.2f}%  DD: {r['dd']:.1f}%  最終: ${r['final']:,.0f}")

    # ランキング
    print(f"\n{'='*90}")
    print(f"  📊 ランキング")
    print(f"{'='*90}")
    print(f"  {'戦略':<30s} {'月次':>10s} {'年率':>10s} {'最終':>12s} {'DD':>8s}")
    print(f"  {'-'*75}")
    results.sort(key=lambda x: x["monthly"], reverse=True)
    for r in results:
        print(f"  {r['name']:<30s} {r['monthly']:+8.2f}%  {r['return']:+8.2f}%  ${r['final']:>10,.0f}  {r['dd']:6.1f}%")

    best = results[0]
    print(f"\n  🏆 Best: {best['name']}")
    print(f"     月次 {best['monthly']:+.2f}% / 年率 {best['return']:+.2f}% / DD {best['dd']:.1f}%")
    if best["monthly"] >= 10:
        print(f"  🎯 ✅ 目標 月+10% 達成！")
    else:
        print(f"  🎯 ❌ 最高 {best['monthly']:.2f}% — あと {10-best['monthly']:.2f}%")
    print()


if __name__ == "__main__":
    main()
