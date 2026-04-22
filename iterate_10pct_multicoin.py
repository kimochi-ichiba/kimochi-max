"""
iterate_10pct_multicoin.py
==========================
賢い多通貨分散で月+10%を目指す

戦略:
1. Top 3 Momentum 3x (上位3銘柄・均等・月次リバランス)
2. Top 5 Momentum 3x (上位5銘柄)
3. Top 3 Momentum 4x
4. Momentum-Weighted Top 10 (モメンタム強度で重み付け)
5. Dual Momentum (上昇トレンド + 相対モメンタム)
6. Only-Winners Portfolio (プラスモメンタムのみ)
7. Quality 60/40: ETH 60% + BTC 40% の3x
8. Dynamic Switcher: 最強1銘柄のみに集中
"""

from __future__ import annotations

from pathlib import Path
import sys
import logging
import warnings
import time
from datetime import datetime, timedelta
from typing import Dict, List

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger().setLevel(logging.WARNING)

INITIAL = 10_000.0
END_DATE = datetime(2026, 4, 18)
START_DATE = END_DATE - timedelta(days=365)
FETCH_START = END_DATE - timedelta(days=365 + 200)
FEE = 0.0006
SLIP = 0.001
MMR = 0.005

LIQUID_TOP10 = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT", "LINK/USDT:USDT",
    "DOT/USDT:USDT", "UNI/USDT:USDT"
]


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


def simulate_leveraged_hold(df_sym, start_ts, end_ts, leverage, alloc_cash):
    """1銘柄・1期間のレバレッジ保有を厳密シミュ → (final_value, liquidated, max_dd)"""
    month_df = df_sym[(df_sym.index >= start_ts) & (df_sym.index <= end_ts)]
    if month_df.empty or len(month_df) < 2:
        return alloc_cash, False, 0

    entry = month_df["close"].iloc[0] * (1 + SLIP)
    notional = alloc_cash * leverage
    qty = notional / entry
    cash = alloc_cash - notional * FEE

    liquidated = False
    peak = alloc_cash
    max_dd = 0
    for p in month_df["low"]:
        current_equity = cash + (p - entry) * qty
        notional_now = p * qty
        mm = notional_now * MMR
        if current_equity <= mm:
            liquidated = True
            return 0, True, 100
        if current_equity > peak: peak = current_equity
        if peak > 0:
            dd = (peak - current_equity) / peak * 100
            max_dd = max(max_dd, dd)

    if liquidated: return 0, True, 100
    exit_p = month_df["close"].iloc[-1] * (1 - SLIP)
    cash += (exit_p - entry) * qty - exit_p * qty * FEE
    return cash, False, max_dd


def strategy_top_n_momentum(all_data, n_picks, leverage, label, lookback_days=90):
    """毎月、過去lookback日のモメンタム上位N銘柄に均等配分"""
    balance = INITIAL
    current = START_DATE
    liquidations = 0
    max_dd_overall = 0
    rebalance_log = []

    while current + timedelta(days=30) <= END_DATE:
        ts = pd.Timestamp(current)
        ts_end = pd.Timestamp(current + timedelta(days=30))
        lookback_ts = ts - timedelta(days=lookback_days)

        # モメンタム計算
        scores = {}
        for sym, df_sym in all_data.items():
            past = df_sym[df_sym.index <= lookback_ts]
            cur = df_sym[df_sym.index <= ts]
            if past.empty or cur.empty: continue
            p0 = past["close"].iloc[-1]; p1 = cur["close"].iloc[-1]
            if p0 <= 0: continue
            scores[sym] = (p1/p0) - 1

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:n_picks]
        if not ranked:
            current += timedelta(days=30); continue

        # 均等配分
        alloc = balance / len(ranked)
        new_balance = 0
        for sym, _ in ranked:
            final_val, liq, dd = simulate_leveraged_hold(all_data[sym], ts, ts_end, leverage, alloc)
            new_balance += final_val
            if liq: liquidations += 1
            max_dd_overall = max(max_dd_overall, dd)
        balance = new_balance

        rebalance_log.append({"date": ts, "picks": [s for s, _ in ranked], "balance": balance})
        if balance <= 100: break
        current += timedelta(days=30)

    final = balance
    m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    total_ret = (final/INITIAL - 1) * 100
    return {"name": label, "final": final, "monthly": m_comp,
            "return": total_ret, "dd": max_dd_overall, "liquidations": liquidations,
            "log": rebalance_log}


def strategy_only_winners(all_data, leverage, label, lookback_days=90):
    """過去90日モメンタム > 0 の銘柄だけに均等配分 (下降トレンド銘柄は除外)"""
    balance = INITIAL
    current = START_DATE
    liquidations = 0
    max_dd_overall = 0

    while current + timedelta(days=30) <= END_DATE:
        ts = pd.Timestamp(current)
        ts_end = pd.Timestamp(current + timedelta(days=30))
        lookback_ts = ts - timedelta(days=lookback_days)

        winners = []
        for sym, df_sym in all_data.items():
            past = df_sym[df_sym.index <= lookback_ts]
            cur = df_sym[df_sym.index <= ts]
            if past.empty or cur.empty: continue
            p0 = past["close"].iloc[-1]; p1 = cur["close"].iloc[-1]
            if p0 <= 0: continue
            mom = (p1/p0) - 1
            if mom > 0:  # プラスモメンタムのみ
                winners.append(sym)

        if not winners:
            current += timedelta(days=30); continue

        alloc = balance / len(winners)
        new_balance = 0
        for sym in winners:
            final_val, liq, dd = simulate_leveraged_hold(all_data[sym], ts, ts_end, leverage, alloc)
            new_balance += final_val
            if liq: liquidations += 1
            max_dd_overall = max(max_dd_overall, dd)
        balance = new_balance
        if balance <= 100: break
        current += timedelta(days=30)

    final = balance
    m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    return {"name": label, "final": final, "monthly": m_comp,
            "return": (final/INITIAL - 1) * 100, "dd": max_dd_overall,
            "liquidations": liquidations}


def strategy_single_best(all_data, leverage, label, lookback_days=90):
    """毎月、最強モメンタム1銘柄に全資金集中"""
    balance = INITIAL
    current = START_DATE
    liquidations = 0
    max_dd_overall = 0
    log = []

    while current + timedelta(days=30) <= END_DATE:
        ts = pd.Timestamp(current)
        ts_end = pd.Timestamp(current + timedelta(days=30))
        lookback_ts = ts - timedelta(days=lookback_days)

        scores = {}
        for sym, df_sym in all_data.items():
            past = df_sym[df_sym.index <= lookback_ts]
            cur = df_sym[df_sym.index <= ts]
            if past.empty or cur.empty: continue
            p0 = past["close"].iloc[-1]; p1 = cur["close"].iloc[-1]
            if p0 <= 0: continue
            scores[sym] = (p1/p0) - 1

        if not scores:
            current += timedelta(days=30); continue

        best_sym = max(scores.items(), key=lambda x: x[1])[0]
        final_val, liq, dd = simulate_leveraged_hold(all_data[best_sym], ts, ts_end, leverage, balance)
        if liq: liquidations += 1
        max_dd_overall = max(max_dd_overall, dd)
        balance = final_val
        log.append({"date": ts, "best": best_sym, "balance": balance})
        if balance <= 100: break
        current += timedelta(days=30)

    final = balance
    m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    return {"name": label, "final": final, "monthly": m_comp,
            "return": (final/INITIAL - 1) * 100, "dd": max_dd_overall,
            "liquidations": liquidations, "log": log}


def strategy_momentum_weighted(all_data, leverage, label, lookback_days=90):
    """モメンタム強度で重み付け配分 (Top 5)"""
    balance = INITIAL
    current = START_DATE
    liquidations = 0
    max_dd_overall = 0

    while current + timedelta(days=30) <= END_DATE:
        ts = pd.Timestamp(current)
        ts_end = pd.Timestamp(current + timedelta(days=30))
        lookback_ts = ts - timedelta(days=lookback_days)

        scores = {}
        for sym, df_sym in all_data.items():
            past = df_sym[df_sym.index <= lookback_ts]
            cur = df_sym[df_sym.index <= ts]
            if past.empty or cur.empty: continue
            p0 = past["close"].iloc[-1]; p1 = cur["close"].iloc[-1]
            if p0 <= 0: continue
            mom = (p1/p0) - 1
            if mom > 0: scores[sym] = mom

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:5]
        if not ranked:
            current += timedelta(days=30); continue

        total_score = sum(s for _, s in ranked)
        new_balance = 0
        for sym, s in ranked:
            weight = s / total_score
            alloc = balance * weight
            final_val, liq, dd = simulate_leveraged_hold(all_data[sym], ts, ts_end, leverage, alloc)
            new_balance += final_val
            if liq: liquidations += 1
            max_dd_overall = max(max_dd_overall, dd)
        balance = new_balance
        if balance <= 100: break
        current += timedelta(days=30)

    final = balance
    m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    return {"name": label, "final": final, "monthly": m_comp,
            "return": (final/INITIAL - 1) * 100, "dd": max_dd_overall,
            "liquidations": liquidations}


def main():
    since_ms = int(FETCH_START.timestamp() * 1000)
    until_ms = int(END_DATE.timestamp() * 1000)

    print(f"\n🌐 賢い多通貨分散で月+10%挑戦")
    print(f"{'='*90}")
    print(f"期間: {START_DATE.strftime('%Y-%m-%d')} 〜 {END_DATE.strftime('%Y-%m-%d')}")
    print(f"対象: 流動性Top10通貨  /  初期: ${INITIAL:,.0f}\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    print(f"📥 データ取得中...")
    all_data = {}
    for sym in LIQUID_TOP10:
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty and len(df) > 100:
            all_data[sym] = df
    print(f"✅ {len(all_data)}/10 通貨取得\n")

    results = []

    tests = [
        ("Top 1 Momentum 3x (集中)",        lambda: strategy_single_best(all_data, 3.0, "Top1 Mom 3x")),
        ("Top 1 Momentum 4x (集中)",        lambda: strategy_single_best(all_data, 4.0, "Top1 Mom 4x")),
        ("Top 3 Momentum 3x",               lambda: strategy_top_n_momentum(all_data, 3, 3.0, "Top3 Mom 3x")),
        ("Top 3 Momentum 4x",               lambda: strategy_top_n_momentum(all_data, 3, 4.0, "Top3 Mom 4x")),
        ("Top 5 Momentum 3x",               lambda: strategy_top_n_momentum(all_data, 5, 3.0, "Top5 Mom 3x")),
        ("Top 5 Momentum 4x",               lambda: strategy_top_n_momentum(all_data, 5, 4.0, "Top5 Mom 4x")),
        ("Only Winners 3x (上昇銘柄のみ)",  lambda: strategy_only_winners(all_data, 3.0, "Winners 3x")),
        ("Only Winners 4x",                 lambda: strategy_only_winners(all_data, 4.0, "Winners 4x")),
        ("Momentum-Weighted Top5 3x",       lambda: strategy_momentum_weighted(all_data, 3.0, "MomWgt5 3x")),
        ("Momentum-Weighted Top5 4x",       lambda: strategy_momentum_weighted(all_data, 4.0, "MomWgt5 4x")),
    ]

    for name, fn in tests:
        print(f"🔬 {name}")
        r = fn()
        results.append(r)
        status = "✅" if r["monthly"] >= 10 else ("🎯" if r["monthly"] >= 5 else "⚠️")
        liq_str = f" 清算{r['liquidations']}回" if r["liquidations"] > 0 else ""
        print(f"   {status} 月次{r['monthly']:+.2f}%  DD{r['dd']:.1f}%  ${r['final']:,.0f}{liq_str}")

    # ランキング
    print(f"\n{'='*90}")
    print(f"  📊 ランキング")
    print(f"{'='*90}")
    print(f"  {'戦略':<30s} {'月次':>10s} {'年率':>10s} {'最終':>12s} {'DD':>8s} {'清算':>6s}")
    print(f"  {'-'*80}")
    results.sort(key=lambda x: x["monthly"], reverse=True)
    for r in results:
        print(f"  {r['name']:<30s} {r['monthly']:+8.2f}%  {r['return']:+8.2f}%  "
              f"${r['final']:>10,.0f}  {r['dd']:6.1f}%  {r['liquidations']:>4d}")

    best = results[0]
    print(f"\n  🏆 Best: {best['name']}")
    print(f"     月次 {best['monthly']:+.2f}% / DD {best['dd']:.1f}% / 清算{best['liquidations']}回")

    # Top1 Momentum の月別選定ログ
    if "log" in best:
        print(f"\n  📝 月別選定履歴:")
        for entry in best["log"][:12]:
            coin = entry["best"].split(":")[0]
            print(f"    {entry['date'].strftime('%Y-%m-%d')}: {coin:20s} → balance ${entry['balance']:,.0f}")

    if best["monthly"] >= 10:
        print(f"\n  🎯 ✅ 目標月+10%達成！")
    else:
        print(f"\n  🎯 ❌ 最高{best['monthly']:.2f}% — あと{10-best['monthly']:.2f}%")
    print()


if __name__ == "__main__":
    main()
