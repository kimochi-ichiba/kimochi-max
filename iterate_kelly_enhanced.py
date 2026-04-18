"""
iterate_kelly_enhanced.py
=========================
Kelly BNBを月+10%まで引き上げる強化版

前回Best: Kelly BNB 90日 = 月+7.67%, DD 77%

強化方針:
1. Full Kelly / 0.75 Kelly (Half → より積極的)
2. Max leverage 10x, 12x (8x上限を緩和)
3. Lookback 30日/45日 (短期反応性)
4. Rebalance 15日/7日 (機敏化)
5. Kelly BNB + ETH 分散 (リスク分散)
6. Kelly + Momentum Filter (弱トレンドで停止)
7. Kelly Multi-Coin (BNB/ETH/BTC の3銘柄Kelly)
8. Quarterly rebalance (長期トレンド補足)
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
sys.path.insert(0, "/Users/sanosano/projects/crypto-bot-pro")
logging.getLogger().setLevel(logging.WARNING)

INITIAL = 3_000.0
END_DATE = datetime(2026, 4, 18)
YEAR_DAYS = 365
FETCH_START = END_DATE - timedelta(days=YEAR_DAYS + 300)
FEE = 0.0006
SLIP = 0.001
MMR = 0.005


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


def add_kelly_columns(df: pd.DataFrame, lookback: int, fraction: float, max_lev: float) -> pd.DataFrame:
    """Kelly基準レバレッジ列を追加"""
    df = df.copy()
    df["ret"] = df["close"].pct_change()
    df["roll_mean"] = df["ret"].rolling(lookback).mean() * 365
    df["roll_var"] = df["ret"].rolling(lookback).var() * 365
    df["kelly_f"] = (df["roll_mean"] / df["roll_var"]).clip(lower=0, upper=max_lev) * fraction
    df["momentum"] = df["close"] / df["close"].shift(lookback) - 1
    return df


def strategy_kelly(df: pd.DataFrame, lookback: int, fraction: float, max_lev: float,
                    rebal_days: int, label: str, momentum_filter: bool = False):
    """Kelly基準の単一銘柄戦略"""
    df = add_kelly_columns(df, lookback, fraction, max_lev)
    df = df[df.index >= pd.Timestamp(END_DATE - timedelta(days=YEAR_DAYS))]

    balance = INITIAL
    pos_qty = 0
    pos_entry = 0
    peak = INITIAL
    max_dd = 0
    liquidated = False
    counter = 0

    for ts, row in df.iterrows():
        if pd.isna(row["kelly_f"]): continue
        price = row["close"]

        # リバランス判定
        if counter % rebal_days == 0:
            # 決済
            if pos_qty > 0:
                exit_p = price * (1 - SLIP)
                balance += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE
                pos_qty = 0

            # エントリー条件
            kelly_lev = row["kelly_f"]
            should_enter = kelly_lev > 0.1
            if momentum_filter and row["momentum"] <= 0:
                should_enter = False

            if should_enter:
                entry = price * (1 + SLIP)
                notional = balance * kelly_lev
                pos_qty = notional / entry
                pos_entry = entry
                balance -= notional * FEE

        # 清算チェック
        if pos_qty > 0:
            eq = balance + (row["low"] - pos_entry) * pos_qty
            mm = row["low"] * pos_qty * MMR
            if eq <= mm:
                liquidated = True
                balance = 0
                break
            if eq > peak: peak = eq
            if peak > 0:
                dd = (peak - eq) / peak * 100
                max_dd = max(max_dd, dd)

        counter += 1

    if pos_qty > 0 and not liquidated:
        exit_p = df.iloc[-1]["close"] * (1 - SLIP)
        balance += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE

    return balance, liquidated, max_dd


def strategy_kelly_multi(dfs: Dict[str, pd.DataFrame], symbols: list, allocs: Dict[str, float],
                          lookback: int, fraction: float, max_lev: float, rebal_days: int,
                          label: str, momentum_filter: bool = False):
    """複数銘柄にKelly基準でそれぞれ運用"""
    total_final = 0
    worst_dd = 0
    any_liq = False
    for sym in symbols:
        if sym not in dfs: continue
        w = allocs.get(sym, 0)
        if w <= 0: continue
        alloc = INITIAL * w
        # 各銘柄に独立Kelly
        df_sym = dfs[sym]
        # allocation-scaled simulation
        df_prep = add_kelly_columns(df_sym, lookback, fraction, max_lev)
        df_prep = df_prep[df_prep.index >= pd.Timestamp(END_DATE - timedelta(days=YEAR_DAYS))]

        balance = alloc
        pos_qty = 0
        pos_entry = 0
        peak = alloc
        sym_dd = 0
        liq = False
        counter = 0

        for ts, row in df_prep.iterrows():
            if pd.isna(row["kelly_f"]): continue
            price = row["close"]
            if counter % rebal_days == 0:
                if pos_qty > 0:
                    exit_p = price * (1 - SLIP)
                    balance += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE
                    pos_qty = 0

                kelly_lev = row["kelly_f"]
                should_enter = kelly_lev > 0.1
                if momentum_filter and row["momentum"] <= 0:
                    should_enter = False

                if should_enter:
                    entry = price * (1 + SLIP)
                    notional = balance * kelly_lev
                    pos_qty = notional / entry
                    pos_entry = entry
                    balance -= notional * FEE

            if pos_qty > 0:
                eq = balance + (row["low"] - pos_entry) * pos_qty
                mm = row["low"] * pos_qty * MMR
                if eq <= mm:
                    liq = True; balance = 0; break
                if eq > peak: peak = eq
                if peak > 0:
                    dd = (peak - eq) / peak * 100
                    sym_dd = max(sym_dd, dd)
            counter += 1

        if pos_qty > 0 and not liq:
            exit_p = df_prep.iloc[-1]["close"] * (1 - SLIP)
            balance += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE

        total_final += balance
        worst_dd = max(worst_dd, sym_dd)
        if liq: any_liq = True

    return total_final, any_liq, worst_dd


def main():
    since_ms = int(FETCH_START.timestamp() * 1000)
    until_ms = int(END_DATE.timestamp() * 1000)

    print(f"\n💎 Kelly BNB 強化版 → 月+10%挑戦 ($3,000スタート)")
    print(f"{'='*95}")
    print(f"期間: {(END_DATE - timedelta(days=YEAR_DAYS)).strftime('%Y-%m-%d')} 〜 {END_DATE.strftime('%Y-%m-%d')}\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    symbols = {"BTC": "BTC/USDT:USDT", "ETH": "ETH/USDT:USDT", "BNB": "BNB/USDT:USDT"}
    dfs = {}
    for name, sym in symbols.items():
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty: dfs[name] = df
    print(f"✅ {len(dfs)}通貨取得\n")

    results = []

    # 1) Kelly fraction のチューニング (Half → Three-quarter → Full)
    print(f"🔬 Kelly Fraction Tuning (BNB, lookback=90)")
    for frac in [0.5, 0.65, 0.75, 1.0]:
        for max_lev in [8, 10, 12]:
            final, liq, dd = strategy_kelly(dfs["BNB"], 90, frac, max_lev, 30,
                                              f"BNB frac{frac} max{max_lev}")
            label = f"BNB Kelly frac{frac} max{max_lev}x"
            monthly = ((final/INITIAL)**(1/12)-1)*100 if final > 0 else -100
            r = {"name": label, "final": final, "liq": liq, "dd": dd, "monthly": monthly,
                 "return": (final/INITIAL-1)*100}
            results.append(r)
            status = "🎯✅" if monthly >= 10 else ("🎯" if monthly >= 5 else "⚠️")
            liq_s = " 💀" if liq else ""
            print(f"  {status} {label:<35s} 月{monthly:+.2f}% DD{dd:.0f}% → ${final:,.0f}{liq_s}")

    # 2) Lookback のチューニング
    print(f"\n🔬 Lookback Tuning (BNB, 0.75 Kelly, max 10x)")
    for lb in [30, 45, 60, 120, 180]:
        final, liq, dd = strategy_kelly(dfs["BNB"], lb, 0.75, 10, 30, f"BNB lb{lb}")
        label = f"BNB Kelly lb{lb}"
        monthly = ((final/INITIAL)**(1/12)-1)*100 if final > 0 else -100
        r = {"name": label, "final": final, "liq": liq, "dd": dd, "monthly": monthly,
             "return": (final/INITIAL-1)*100}
        results.append(r)
        status = "🎯✅" if monthly >= 10 else ("🎯" if monthly >= 5 else "⚠️")
        liq_s = " 💀" if liq else ""
        print(f"  {status} {label:<35s} 月{monthly:+.2f}% DD{dd:.0f}% → ${final:,.0f}{liq_s}")

    # 3) Rebalance Frequency
    print(f"\n🔬 Rebalance Frequency (BNB, 0.75 Kelly, lb90, max 10x)")
    for rd in [7, 15, 30, 60, 90]:
        final, liq, dd = strategy_kelly(dfs["BNB"], 90, 0.75, 10, rd, f"BNB rd{rd}")
        label = f"BNB Kelly rebal{rd}d"
        monthly = ((final/INITIAL)**(1/12)-1)*100 if final > 0 else -100
        r = {"name": label, "final": final, "liq": liq, "dd": dd, "monthly": monthly,
             "return": (final/INITIAL-1)*100}
        results.append(r)
        status = "🎯✅" if monthly >= 10 else ("🎯" if monthly >= 5 else "⚠️")
        liq_s = " 💀" if liq else ""
        print(f"  {status} {label:<35s} 月{monthly:+.2f}% DD{dd:.0f}% → ${final:,.0f}{liq_s}")

    # 4) BNB + ETH 分散
    print(f"\n🔬 BNB+ETH 分散 (0.75 Kelly, lb90, max 10x)")
    for bnb_pct in [0.5, 0.6, 0.7, 0.8, 0.9]:
        allocs = {"BNB": bnb_pct, "ETH": 1 - bnb_pct}
        final, liq, dd = strategy_kelly_multi(dfs, ["BNB","ETH"], allocs, 90, 0.75, 10, 30,
                                                f"BNB{int(bnb_pct*100)}+ETH{int((1-bnb_pct)*100)}")
        label = f"BNB{int(bnb_pct*100)}+ETH{int((1-bnb_pct)*100)} Kelly"
        monthly = ((final/INITIAL)**(1/12)-1)*100 if final > 0 else -100
        r = {"name": label, "final": final, "liq": liq, "dd": dd, "monthly": monthly,
             "return": (final/INITIAL-1)*100}
        results.append(r)
        status = "🎯✅" if monthly >= 10 else ("🎯" if monthly >= 5 else "⚠️")
        liq_s = " 💀" if liq else ""
        print(f"  {status} {label:<35s} 月{monthly:+.2f}% DD{dd:.0f}% → ${final:,.0f}{liq_s}")

    # 5) Momentum Filter 追加
    print(f"\n🔬 Momentum Filter 付き (弱トレンド時は停止)")
    for coin in ["BNB", "ETH"]:
        final, liq, dd = strategy_kelly(dfs[coin], 90, 0.75, 10, 30,
                                          f"{coin} + MomFilter", momentum_filter=True)
        label = f"{coin} Kelly + MomFilter"
        monthly = ((final/INITIAL)**(1/12)-1)*100 if final > 0 else -100
        r = {"name": label, "final": final, "liq": liq, "dd": dd, "monthly": monthly,
             "return": (final/INITIAL-1)*100}
        results.append(r)
        status = "🎯✅" if monthly >= 10 else ("🎯" if monthly >= 5 else "⚠️")
        liq_s = " 💀" if liq else ""
        print(f"  {status} {label:<35s} 月{monthly:+.2f}% DD{dd:.0f}% → ${final:,.0f}{liq_s}")

    # 6) 3銘柄 Kelly (BNB+ETH+BTC)
    print(f"\n🔬 3銘柄Kelly (BNB/ETH/BTC)")
    allocs_variants = [
        {"BNB": 0.5, "ETH": 0.3, "BTC": 0.2},
        {"BNB": 0.6, "ETH": 0.3, "BTC": 0.1},
        {"BNB": 0.7, "ETH": 0.2, "BTC": 0.1},
        {"BNB": 0.4, "ETH": 0.4, "BTC": 0.2},
    ]
    for allocs in allocs_variants:
        final, liq, dd = strategy_kelly_multi(dfs, ["BNB","ETH","BTC"], allocs, 90, 0.75, 10, 30,
                                                "3coin")
        label = f"B{int(allocs['BNB']*100)}+E{int(allocs['ETH']*100)}+Bt{int(allocs['BTC']*100)} Kelly"
        monthly = ((final/INITIAL)**(1/12)-1)*100 if final > 0 else -100
        r = {"name": label, "final": final, "liq": liq, "dd": dd, "monthly": monthly,
             "return": (final/INITIAL-1)*100}
        results.append(r)
        status = "🎯✅" if monthly >= 10 else ("🎯" if monthly >= 5 else "⚠️")
        liq_s = " 💀" if liq else ""
        print(f"  {status} {label:<35s} 月{monthly:+.2f}% DD{dd:.0f}% → ${final:,.0f}{liq_s}")

    # ランキング
    print(f"\n{'='*95}")
    print(f"  📊 Kelly強化版 ランキング")
    print(f"{'='*95}")
    results.sort(key=lambda x: x["monthly"], reverse=True)
    print(f"  {'戦略':<35s} {'月次':>9s} {'年率':>10s} {'DD':>6s} {'最終':>12s}")
    print(f"  {'-'*76}")
    for r in results:
        liq_s = " 💀" if r["liq"] else ""
        print(f"  {r['name']:<35s} {r['monthly']:+7.2f}%  {r['return']:+7.2f}%  "
              f"{r['dd']:5.0f}%  ${r['final']:>9,.0f}{liq_s}")

    # Top 5 詳細
    safe_10 = [r for r in results if not r["liq"] and r["monthly"] >= 10]
    safe = [r for r in results if not r["liq"]]
    print(f"\n{'='*95}")
    if safe_10:
        print(f"  🎯✅ 月+10%達成 (清算なし) {len(safe_10)}個")
        for r in safe_10[:5]:
            profit = r["final"] - INITIAL
            print(f"\n  {r['name']}")
            print(f"    📈 $3,000 → ${r['final']:,.0f}  (+${profit:,.0f}, {r['final']/INITIAL:.2f}倍)")
            print(f"    📊 月次{r['monthly']:+.2f}% / 年{r['return']:+.1f}% / DD{r['dd']:.0f}%")
    else:
        best = safe[0] if safe else None
        if best:
            profit = best["final"] - INITIAL
            print(f"  ⚠️ 月+10%未達成 (ベスト: {best['name']} = 月{best['monthly']:+.2f}%)")
            print(f"\n  🏆 Best: {best['name']}")
            print(f"     📈 $3,000 → ${best['final']:,.0f}  (+${profit:,.0f}, {best['final']/INITIAL:.2f}倍)")
            print(f"     📊 月次{best['monthly']:+.2f}% / 年{best['return']:+.1f}% / DD{best['dd']:.0f}%")

    print()


if __name__ == "__main__":
    main()
