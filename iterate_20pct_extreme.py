"""
iterate_20pct_extreme.py
========================
月+20% 最終挑戦: 極端な高レバと動的切り替え

前回 Best: ETH @ 10x = +16.53%
+20%到達のための試行:
1. ETH 12x / 15x / 20x / 25x
2. BNB 12x / 15x / 20x
3. 月次切り替え (前月最強コインに全賭け高レバ)
4. ボラ調整ポジションサイジング (Carver方式)
5. 2倍 + 再投資月次 (複利最大化)
"""

from __future__ import annotations

from pathlib import Path
import sys
import logging
import warnings
import time
from datetime import datetime, timedelta
from typing import Dict

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

COINS = {
    "BTC": "BTC/USDT:USDT", "ETH": "ETH/USDT:USDT", "BNB": "BNB/USDT:USDT",
}


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


def simulate_buyhold(df, alloc_cash, leverage):
    df = df[df.index >= pd.Timestamp(START_DATE)]
    if df.empty: return alloc_cash, False, 0
    entry = df["close"].iloc[0] * (1 + SLIP)
    notional = alloc_cash * leverage
    qty = notional / entry
    cash = alloc_cash - notional * FEE
    peak = alloc_cash
    max_dd = 0
    for p in df["low"]:
        current_equity = cash + (p - entry) * qty
        mm = p * qty * MMR
        if current_equity <= mm:
            return 0, True, 100
        if current_equity > peak: peak = current_equity
        if peak > 0:
            dd = (peak - current_equity) / peak * 100
            max_dd = max(max_dd, dd)
    exit_p = df["close"].iloc[-1] * (1 - SLIP)
    cash += (exit_p - entry) * qty - exit_p * qty * FEE
    return cash, False, max_dd


def strategy_dynamic_switch(dfs: Dict[str, pd.DataFrame], leverage: float, label: str):
    """毎月、前月最強コインに全資金集中 + 高レバ"""
    balance = INITIAL
    current = START_DATE
    peak = INITIAL
    max_dd = 0
    liquidated = False
    log = []

    while current + timedelta(days=30) <= END_DATE:
        ts = pd.Timestamp(current)
        ts_end = pd.Timestamp(current + timedelta(days=30))
        lookback_ts = ts - timedelta(days=30)

        # 前月最強コイン選定
        scores = {}
        for sym, df_sym in dfs.items():
            past = df_sym[df_sym.index <= lookback_ts]
            cur = df_sym[df_sym.index <= ts]
            if past.empty or cur.empty: continue
            p0 = past["close"].iloc[-1]; p1 = cur["close"].iloc[-1]
            if p0 <= 0: continue
            scores[sym] = (p1/p0) - 1

        if not scores:
            current += timedelta(days=30); continue

        best_sym = max(scores.items(), key=lambda x: x[1])[0]
        df_best = dfs[best_sym]
        month_df = df_best[(df_best.index >= ts) & (df_best.index <= ts_end)]
        if month_df.empty or len(month_df) < 2:
            current += timedelta(days=30); continue

        entry = month_df["close"].iloc[0] * (1 + SLIP)
        notional = balance * leverage
        qty = notional / entry
        balance_cash = balance - notional * FEE

        # 月内清算チェック
        liquid_this_month = False
        month_peak = balance
        for p in month_df["low"]:
            eq = balance_cash + (p - entry) * qty
            mm = p * qty * MMR
            if eq <= mm:
                liquid_this_month = True
                balance = 0
                break
            if eq > month_peak: month_peak = eq
            if month_peak > peak: peak = month_peak
            if peak > 0:
                dd = (peak - eq) / peak * 100
                max_dd = max(max_dd, dd)

        if liquid_this_month:
            liquidated = True
            break

        exit_p = month_df["close"].iloc[-1] * (1 - SLIP)
        balance = balance_cash + (exit_p - entry) * qty - exit_p * qty * FEE
        log.append({"date": ts, "coin": best_sym, "balance": balance})
        current += timedelta(days=30)

    final = max(balance, 0)
    m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    return {"name": label + (" 💀" if liquidated else ""), "final": final,
            "monthly": m_comp, "return": (final/INITIAL - 1) * 100,
            "dd": max_dd, "log": log, "liq": liquidated}


def strategy_monthly_reinvest(df_sym, leverage: float, label: str):
    """毎月最終日に全決済→翌月最初に再投入 (複利最大化)"""
    balance = INITIAL
    current = START_DATE
    peak = INITIAL
    max_dd = 0
    liquidated = False

    while current + timedelta(days=30) <= END_DATE:
        ts = pd.Timestamp(current)
        ts_end = pd.Timestamp(current + timedelta(days=30))
        month_df = df_sym[(df_sym.index >= ts) & (df_sym.index <= ts_end)]
        if month_df.empty or len(month_df) < 2:
            current += timedelta(days=30); continue

        entry = month_df["close"].iloc[0] * (1 + SLIP)
        notional = balance * leverage
        qty = notional / entry
        balance_cash = balance - notional * FEE

        liquid = False
        month_peak = balance
        for p in month_df["low"]:
            eq = balance_cash + (p - entry) * qty
            mm = p * qty * MMR
            if eq <= mm:
                liquid = True; balance = 0; break
            if eq > month_peak: month_peak = eq
            if month_peak > peak: peak = month_peak
            if peak > 0:
                dd = (peak - eq) / peak * 100
                max_dd = max(max_dd, dd)

        if liquid:
            liquidated = True; break

        exit_p = month_df["close"].iloc[-1] * (1 - SLIP)
        balance = balance_cash + (exit_p - entry) * qty - exit_p * qty * FEE
        current += timedelta(days=30)

    final = max(balance, 0)
    m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    return {"name": label + (" 💀" if liquidated else ""), "final": final,
            "monthly": m_comp, "return": (final/INITIAL - 1) * 100,
            "dd": max_dd, "liq": liquidated}


def main():
    since_ms = int(FETCH_START.timestamp() * 1000)
    until_ms = int(END_DATE.timestamp() * 1000)

    print(f"\n🔥🔥 月+20% 最終極端挑戦")
    print(f"{'='*90}")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    dfs = {}
    for name, sym in COINS.items():
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty: dfs[name] = df
    print(f"✅ {len(dfs)}通貨取得\n")

    results = []

    # 極限レバレッジテスト
    print(f"🔥 ETH/BNB極限レバレッジ")
    for sym in ["ETH", "BNB"]:
        for lev in [12, 15, 20, 25, 30]:
            if sym not in dfs: continue
            final, liq, dd = simulate_buyhold(dfs[sym], INITIAL, lev)
            m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
            r = {"name": f"{sym} 100% @ {lev}x", "final": final, "monthly": m_comp,
                 "return": (final/INITIAL - 1) * 100, "dd": dd, "liq": liq}
            results.append(r)
            status = "🎯✅" if m_comp >= 20 else ("✅" if m_comp >= 10 else "⚠️")
            liq_s = " 清算" if liq else ""
            print(f"  {status} {sym} @ {lev}x → 月{m_comp:+.2f}%  DD{dd:.1f}%{liq_s}")

    # 月次切り替え
    print(f"\n🔄 動的切り替え (前月最強コインに全賭け)")
    for lev in [3, 4, 5, 6, 8, 10]:
        r = strategy_dynamic_switch(dfs, lev, f"Switch BNB/ETH/BTC @ {lev}x")
        results.append(r)
        status = "🎯✅" if r["monthly"] >= 20 else ("✅" if r["monthly"] >= 10 else "⚠️")
        liq_s = " 清算" if r["liq"] else ""
        print(f"  {status} Switch {lev}x → 月{r['monthly']:+.2f}%  DD{r['dd']:.1f}%{liq_s}")

    # 月次再投資 (ETH複利最大化)
    print(f"\n💹 月次再投資 (ETH, 複利最大化)")
    for lev in [5, 6, 8, 10]:
        r = strategy_monthly_reinvest(dfs["ETH"], lev, f"ETH Monthly @ {lev}x")
        results.append(r)
        status = "🎯✅" if r["monthly"] >= 20 else ("✅" if r["monthly"] >= 10 else "⚠️")
        liq_s = " 清算" if r["liq"] else ""
        print(f"  {status} ETH Monthly {lev}x → 月{r['monthly']:+.2f}%  DD{r['dd']:.1f}%{liq_s}")

    # ランキング
    print(f"\n{'='*90}")
    print(f"  📊 最終ランキング")
    print(f"{'='*90}")
    print(f"  {'戦略':<40s} {'月次':>10s} {'年率':>10s} {'最終':>12s} {'DD':>8s}")
    print(f"  {'-'*85}")
    results.sort(key=lambda x: x["monthly"], reverse=True)
    for r in results:
        print(f"  {r['name']:<40s} {r['monthly']:+8.2f}%  {r['return']:+8.2f}%  ${r['final']:>10,.0f}  {r['dd']:6.1f}%")

    safe_20 = [r for r in results if r["monthly"] >= 20 and not r.get("liq", False)]
    safe_15 = [r for r in results if r["monthly"] >= 15 and not r.get("liq", False)]

    print(f"\n  🏆 Top 5:")
    for i, r in enumerate(results[:5], 1):
        print(f"  {i}. {r['name']}: 月{r['monthly']:+.2f}% DD{r['dd']:.1f}%")

    if safe_20:
        print(f"\n  🎯✅✅ 清算なしで月+20%達成！ ({len(safe_20)}個)")
        for r in safe_20:
            print(f"    - {r['name']}: 月{r['monthly']:+.2f}% DD{r['dd']:.1f}%")
    elif safe_15:
        print(f"\n  ✅ 清算なしで月+15%達成 ({len(safe_15)}個)、+20%未達")
    else:
        best_safe = [r for r in results if not r.get("liq", False)]
        if best_safe:
            b = best_safe[0]
            print(f"\n  ⚠️ 清算なし最高: {b['name']} 月{b['monthly']:+.2f}%")
    print()


if __name__ == "__main__":
    main()
