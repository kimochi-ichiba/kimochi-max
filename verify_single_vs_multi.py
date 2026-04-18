"""
verify_single_vs_multi.py
=========================
BNB単独 vs BNB+BTC 多通貨 の厳密比較

目的: 前回 "月+10%" と "破綻" の食い違いの真相解明

同じKellyロジックで:
1. BNB単独バージョン (stability test再現)
2. BNB+BTC 70:30 多通貨バージョン
3. BNB単独だが bot logic で運用
4. 複数期間で比較
"""

from __future__ import annotations

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
logging.getLogger().setLevel(logging.WARNING)

INITIAL = 3_000.0
FEE = 0.0006
SLIP = 0.001
MMR = 0.005
LOOKBACK = 60
FRACTION = 0.5
MAX_LEV = 10
REBAL_DAYS = 30


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


# ---------- 実装1: stability test再現 (BNB単独) ----------

def stability_test_bnb(df: pd.DataFrame, start_date: datetime, end_date: datetime):
    """verify_rigorous_stability.py の run_window とまったく同じロジック"""
    df = df.copy()
    df["ret"] = df["close"].pct_change()
    df["roll_mean"] = df["ret"].rolling(LOOKBACK).mean() * 365
    df["roll_var"] = df["ret"].rolling(LOOKBACK).var() * 365
    df["kelly_f"] = (df["roll_mean"] / df["roll_var"]).clip(lower=0, upper=MAX_LEV) * FRACTION
    df = df[(df.index >= pd.Timestamp(start_date)) & (df.index <= pd.Timestamp(end_date))]
    if df.empty or len(df) < 30:
        return INITIAL, False, 0

    balance = INITIAL
    pos_qty = 0; pos_entry = 0
    peak = INITIAL; max_dd = 0
    liquidated = False
    counter = 0

    for ts, row in df.iterrows():
        if pd.isna(row["kelly_f"]): continue
        price = row["close"]

        if counter % REBAL_DAYS == 0:
            if pos_qty > 0:
                exit_p = price * (1 - SLIP)
                balance += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE
                pos_qty = 0
            kelly_lev = row["kelly_f"]
            if kelly_lev > 0.1:
                entry = price * (1 + SLIP)
                notional = balance * kelly_lev
                pos_qty = notional / entry
                pos_entry = entry
                balance -= notional * FEE

        if pos_qty > 0:
            eq = balance + (row["low"] - pos_entry) * pos_qty
            mm = row["low"] * pos_qty * MMR
            if eq <= mm:
                liquidated = True; balance = 0; break
            if eq > peak: peak = eq
            if peak > 0:
                dd = (peak - eq) / peak * 100
                max_dd = max(max_dd, dd)
        counter += 1

    if pos_qty > 0 and not liquidated:
        exit_p = df.iloc[-1]["close"] * (1 - SLIP)
        balance += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE

    return max(balance, 0), liquidated, max_dd


# ---------- 実装2: Bot logic (BNB+BTC 多通貨) ----------

def bot_multi_coin(dfs: Dict[str, pd.DataFrame], weights: Dict[str, float],
                    start_date: datetime, end_date: datetime):
    """bot実装と同じマルチコイン運用"""
    # Kelly計算は各通貨独立
    for sym in dfs:
        df = dfs[sym].copy()
        df["ret"] = df["close"].pct_change()
        df["roll_mean"] = df["ret"].rolling(LOOKBACK).mean() * 365
        df["roll_var"] = df["ret"].rolling(LOOKBACK).var() * 365
        df["kelly_f"] = (df["roll_mean"] / df["roll_var"]).clip(lower=0, upper=MAX_LEV) * FRACTION
        dfs[sym] = df

    all_dates = sorted(set().union(*[set(df.index) for df in dfs.values()]))
    all_dates = [d for d in all_dates if start_date <= d.to_pydatetime() <= end_date]

    cash = INITIAL
    positions = {}  # sym -> {entry, size, leverage, margin}
    last_rebal = None
    counter = 0
    peak = INITIAL
    max_dd = 0
    liquidated = False

    for ts in all_dates:
        # 清算チェック
        to_close = []
        for sym, pos in positions.items():
            if sym not in dfs or ts not in dfs[sym].index: continue
            low = dfs[sym].loc[ts]["low"]
            eq = pos["margin"] + (low - pos["entry"]) * pos["size"] * pos["leverage"]
            mm = low * pos["size"] * MMR
            if eq <= mm:
                to_close.append(sym)
        for sym in to_close:
            del positions[sym]  # margin lost

        # リバランス
        if last_rebal is None or (ts - last_rebal).days >= REBAL_DAYS:
            # 全決済
            for sym, pos in list(positions.items()):
                if sym in dfs and ts in dfs[sym].index:
                    price = dfs[sym].loc[ts]["close"]
                    exit_p = price * (1 - SLIP)
                    pnl = (exit_p - pos["entry"]) * pos["size"] * pos["leverage"]
                    fee = exit_p * pos["size"] * FEE
                    final = max(pos["margin"] + pnl - fee, 0)
                    cash += final
                del positions[sym]

            total_capital = cash
            for sym, weight in weights.items():
                if sym not in dfs or ts not in dfs[sym].index: continue
                row = dfs[sym].loc[ts]
                if pd.isna(row["kelly_f"]): continue
                kelly_lev = row["kelly_f"]
                if kelly_lev < 0.1: continue

                alloc = total_capital * weight
                current = row["close"]
                entry = current * (1 + SLIP)
                notional = alloc * kelly_lev
                size = notional / entry
                fee = notional * FEE
                margin = alloc - fee

                positions[sym] = {"entry": entry, "size": size, "leverage": kelly_lev, "margin": margin}
                cash -= margin

            last_rebal = ts
            counter += 1

        # equity追跡
        eq = cash
        for sym, pos in positions.items():
            if sym in dfs and ts in dfs[sym].index:
                p = dfs[sym].loc[ts]["close"]
                eq += pos["margin"] + (p - pos["entry"]) * pos["size"] * pos["leverage"]
            else:
                eq += pos["margin"]
        if eq > peak: peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100
            max_dd = max(max_dd, dd)

    # 最終決済
    if all_dates and positions:
        ts = all_dates[-1]
        for sym, pos in list(positions.items()):
            if sym in dfs and ts in dfs[sym].index:
                price = dfs[sym].loc[ts]["close"]
                exit_p = price * (1 - SLIP)
                pnl = (exit_p - pos["entry"]) * pos["size"] * pos["leverage"]
                fee = exit_p * pos["size"] * FEE
                final = max(pos["margin"] + pnl - fee, 0)
                cash += final

    return max(cash, 0), False, max_dd


# ---------- 実装3: Bot logic を BNB単独で ----------

def bot_single_coin(df: pd.DataFrame, start_date: datetime, end_date: datetime):
    """Bot実装で BNB 100% 単独運用"""
    return bot_multi_coin({"BNB": df}, {"BNB": 1.0}, start_date, end_date)


def main():
    print(f"\n🔬 BNB単独 vs BNB+BTC 多通貨の厳密比較")
    print(f"{'='*100}")
    print(f"パラメータ: Kelly frac={FRACTION} lb={LOOKBACK} max={MAX_LEV}x rebal={REBAL_DAYS}d")
    print(f"初期資金: ${INITIAL:,.0f}\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    since_ms = int(datetime(2021, 6, 1).timestamp() * 1000)
    until_ms = int(datetime(2026, 4, 18).timestamp() * 1000)

    print(f"📥 データ取得...")
    bnb = fetch_ohlcv(ex, "BNB/USDT:USDT", since_ms, until_ms)
    btc = fetch_ohlcv(ex, "BTC/USDT:USDT", since_ms, until_ms)
    print(f"✅ BNB: {len(bnb)}本, BTC: {len(btc)}本\n")

    # 複数期間
    periods = [
        (datetime(2022, 6, 1), datetime(2023, 6, 1), "2022-06〜2023-06 (ベア)"),
        (datetime(2023, 1, 1), datetime(2024, 1, 1), "2023-01〜2024-01 (回復)"),
        (datetime(2023, 6, 1), datetime(2024, 6, 1), "2023-06〜2024-06 (ブル転換)"),
        (datetime(2024, 1, 1), datetime(2025, 1, 1), "2024-01〜2025-01 (ブル)"),
        (datetime(2024, 6, 1), datetime(2025, 6, 1), "2024-06〜2025-06 (調整)"),
        (datetime(2025, 1, 1), datetime(2026, 1, 1), "2025-01〜2026-01 (直近)"),
        (datetime(2025, 4, 18), datetime(2026, 4, 18), "2025-04〜2026-04 (直近1年)"),
    ]

    print(f"{'='*100}")
    print(f"  📊 3手法の結果比較")
    print(f"{'='*100}")
    print(f"  {'期間':<35s} | {'Stab BNB':>10s} | {'Bot BNB単独':>12s} | {'Bot BNB+BTC':>13s}")
    print(f"  {'-'*95}")

    for start, end, label in periods:
        months = (end - start).days / 30.0

        # 手法1: stability test ロジック
        final1, liq1, _ = stability_test_bnb(bnb.copy(), start, end)
        monthly1 = ((final1/INITIAL)**(1/months)-1)*100 if final1 > 0 else -100
        r1 = f"${final1:.0f}({monthly1:+.1f}%)"
        if liq1: r1 = "💀清算"

        # 手法2: bot logic BNB単独
        final2, _, _ = bot_single_coin(bnb.copy(), start, end)
        monthly2 = ((final2/INITIAL)**(1/months)-1)*100 if final2 > 0 else -100
        r2 = f"${final2:.0f}({monthly2:+.1f}%)"

        # 手法3: bot logic BNB+BTC
        final3, _, _ = bot_multi_coin({"BNB": bnb.copy(), "BTC": btc.copy()},
                                        {"BNB": 0.7, "BTC": 0.3}, start, end)
        monthly3 = ((final3/INITIAL)**(1/months)-1)*100 if final3 > 0 else -100
        r3 = f"${final3:.0f}({monthly3:+.1f}%)"

        print(f"  {label:<35s} | {r1:>10s} | {r2:>12s} | {r3:>13s}")

    print(f"\n{'='*100}")
    print(f"  🎯 分析")
    print(f"{'='*100}")
    print(f"  Stab BNB   : 独立1年ウィンドウでのKelly BNB単独 (前回 '+10%' 主張)")
    print(f"  Bot BNB単独: Bot実装でBNB100%運用")
    print(f"  Bot BNB+BTC: Bot実装で BNB70 + BTC30 多通貨運用")
    print(f"\n  → Stab と Bot BNB単独 が一致すれば 前回の数字は本物")
    print(f"  → Bot BNB+BTC が単独より悪ければ、多通貨が問題")
    print()


if __name__ == "__main__":
    main()
