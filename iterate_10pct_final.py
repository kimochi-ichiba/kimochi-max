"""
iterate_10pct_final.py
======================
最終ラスト: 月+10%達成に向けた決定版

前回 Best = PerfWgt3 4x (ETH 86% + BNB 14%) → 月+9.17%

追加試行:
1. ETH 80 + BNB 20 @ 5x
2. ETH 70 + BNB 20 + BTC 10 @ 4.5x (BTC小量で分散維持)
3. ETH 85 + BNB 15 @ 4.5x
4. ETH 90 + BNB 10 @ 5x
5. ETH 75 + BNB 25 @ 4.5x
6. ETH + BNB + XRP等 パフォーマンス加重
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
FETCH_START = END_DATE - timedelta(days=365 + 200)
FEE = 0.0006
SLIP = 0.001
MMR = 0.005

LIQUID = {
    "BTC": "BTC/USDT:USDT", "ETH": "ETH/USDT:USDT", "BNB": "BNB/USDT:USDT",
    "SOL": "SOL/USDT:USDT", "XRP": "XRP/USDT:USDT", "ADA": "ADA/USDT:USDT",
    "LINK": "LINK/USDT:USDT", "AVAX": "AVAX/USDT:USDT",
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


def run_portfolio(dfs, allocs: Dict[str, float], levs: Dict[str, float], label: str):
    total_final = 0; worst_dd = 0; any_liq = False
    detail = []
    for sym, w in allocs.items():
        if sym not in dfs or w <= 0: continue
        alloc = INITIAL * w
        lev = levs.get(sym, 3.0)
        fv, liq, dd = simulate_buyhold(dfs[sym], alloc, lev)
        total_final += fv
        worst_dd = max(worst_dd, dd)
        if liq: any_liq = True
        detail.append({"sym": sym, "w": w, "lev": lev, "final": fv, "dd": dd, "liq": liq})
    m_comp = ((total_final/INITIAL) ** (1/12) - 1) * 100 if total_final > 0 else -100
    ret = (total_final/INITIAL - 1) * 100
    return {"name": label + (" 💀" if any_liq else ""),
            "final": total_final, "monthly": m_comp, "return": ret,
            "dd": worst_dd, "detail": detail, "liq": any_liq}


def main():
    since_ms = int(FETCH_START.timestamp() * 1000)
    until_ms = int(END_DATE.timestamp() * 1000)

    print(f"\n🎯 月+10%達成 最終ラウンド")
    print(f"{'='*90}")
    print(f"期間: {START_DATE.strftime('%Y-%m-%d')} 〜 {END_DATE.strftime('%Y-%m-%d')}\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    dfs = {}
    for name, sym in LIQUID.items():
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty: dfs[name] = df
    print(f"✅ {len(dfs)}通貨取得\n")

    # 各銘柄の1年リターン再表示
    print(f"📊 1年Buy&Holdリターン:")
    returns = {}
    for name, df in dfs.items():
        d = df[df.index >= pd.Timestamp(START_DATE)]
        if d.empty: continue
        r = (d["close"].iloc[-1] / d["close"].iloc[0] - 1) * 100
        returns[name] = r
        print(f"    {name:<5s}: {r:+7.2f}%")
    print()

    tests = [
        ("ETH80 + BNB20 @ 5x",          {"ETH": 0.8, "BNB": 0.2}, {"ETH": 5, "BNB": 5}),
        ("ETH85 + BNB15 @ 4.5x",        {"ETH": 0.85, "BNB": 0.15}, {"ETH": 4.5, "BNB": 4.5}),
        ("ETH85 + BNB15 @ 5x",          {"ETH": 0.85, "BNB": 0.15}, {"ETH": 5, "BNB": 5}),
        ("ETH90 + BNB10 @ 5x",          {"ETH": 0.9, "BNB": 0.1}, {"ETH": 5, "BNB": 5}),
        ("ETH75 + BNB25 @ 4.5x",        {"ETH": 0.75, "BNB": 0.25}, {"ETH": 4.5, "BNB": 4.5}),
        ("ETH70 + BNB20 + BTC10 @ 4.5x",{"ETH": 0.7, "BNB": 0.2, "BTC": 0.1}, {"ETH": 4.5, "BNB": 4.5, "BTC": 4.5}),
        ("ETH80 + BNB10 + BTC10 @ 5x",  {"ETH": 0.8, "BNB": 0.1, "BTC": 0.1}, {"ETH": 5, "BNB": 5, "BTC": 5}),
        ("ETH70 + BNB30 @ 5x",          {"ETH": 0.7, "BNB": 0.3}, {"ETH": 5, "BNB": 5}),
        ("ETH60 + BNB40 @ 5x",          {"ETH": 0.6, "BNB": 0.4}, {"ETH": 5, "BNB": 5}),
        ("ETH100 @ 4.5x (参考)",         {"ETH": 1.0}, {"ETH": 4.5}),
        ("ETH100 @ 5x (参考)",           {"ETH": 1.0}, {"ETH": 5.0}),
        ("ETH70 + BNB20 + LINK10 @ 4.5x", {"ETH": 0.7, "BNB": 0.2, "LINK": 0.1}, {"ETH": 4.5, "BNB": 4.5, "LINK": 4.5}),
    ]

    results = []
    for label, allocs, levs in tests:
        print(f"🔬 {label}")
        r = run_portfolio(dfs, allocs, levs, label)
        results.append(r)
        status = "✅" if r["monthly"] >= 10 else ("🎯" if r["monthly"] >= 5 else "⚠️")
        liq_str = " 清算" if r["liq"] else ""
        print(f"   {status} 月次{r['monthly']:+.2f}%  DD{r['dd']:.1f}%  ${r['final']:,.0f}{liq_str}")

    # ランキング
    print(f"\n{'='*90}")
    print(f"  📊 最終ランキング")
    print(f"{'='*90}")
    print(f"  {'戦略':<40s} {'月次':>10s} {'年率':>10s} {'最終':>12s} {'DD':>8s}")
    print(f"  {'-'*85}")
    results.sort(key=lambda x: x["monthly"], reverse=True)
    for r in results:
        print(f"  {r['name']:<40s} {r['monthly']:+8.2f}%  {r['return']:+8.2f}%  ${r['final']:>10,.0f}  {r['dd']:6.1f}%")

    # Best 3 detail
    print(f"\n  🏆 Top 3 詳細:")
    for i, r in enumerate(results[:3], 1):
        print(f"\n  {i}. {r['name']}")
        print(f"     月次{r['monthly']:+.2f}% / 年率{r['return']:+.2f}% / DD{r['dd']:.1f}%")
        for d in r["detail"]:
            print(f"       {d['sym']}: 配分{d['w']*100:.0f}% × レバ{d['lev']}x → ${d['final']:,.0f} (DD{d['dd']:.1f}%)")

    # 清算なしで +10% 達成のもの
    safe_winners = [r for r in results if r["monthly"] >= 10 and not r["liq"]]
    if safe_winners:
        print(f"\n  ✅ 清算なしで月+10%達成した戦略 ({len(safe_winners)}個)")
        for r in safe_winners:
            print(f"    - {r['name']}: 月{r['monthly']:+.2f}% DD{r['dd']:.1f}%")
    else:
        any_winners = [r for r in results if r["monthly"] >= 10]
        if any_winners:
            print(f"\n  ⚠️ +10%達成したが一部清算:")
            for r in any_winners:
                print(f"    - {r['name']}: 月{r['monthly']:+.2f}%")
        else:
            best = results[0]
            print(f"\n  ❌ 月+10%未達成。最高{best['monthly']:.2f}%")
    print()


if __name__ == "__main__":
    main()
