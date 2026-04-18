"""
iterate_10pct_static.py
=======================
静的配分の多通貨Buy&Holdで月+10%挑戦

動的モメンタム追跡は失敗 (清算で全滅)。
代わりに「強い上位コインを静的比率で固定保有」を試す。

戦略:
1. BTC+ETH 50/50 3x (均等)
2. BTC+ETH 30/70 3x (ETH寄せ)
3. BTC+ETH 20/80 3x (ETH強調)
4. BTC+ETH 40/60 3x (ETH若干強め)
5. BTC 50 + ETH 50 (4x版)
6. 5銘柄分散 BTC/ETH/BNB/SOL/XRP 3x均等
7. 3銘柄 BTC/ETH/BNB 3x均等 (SOLとXRP除外・今年弱かった)
8. Annual Top 3 Buy&Hold 3x (前年強者をピック)
9. 長期モメンタム(1年)で選ぶ静的トップ3
10. ETH 4x + BTC 2x (非対称レバ)
"""

from __future__ import annotations

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
sys.path.insert(0, "/Users/sanosano/projects/crypto-bot-pro")
logging.getLogger().setLevel(logging.WARNING)

INITIAL = 10_000.0
END_DATE = datetime(2026, 4, 18)
START_DATE = END_DATE - timedelta(days=365)
FETCH_START = END_DATE - timedelta(days=365 + 200)
FEE = 0.0006
SLIP = 0.001
MMR = 0.005

LIQUID = {
    "BTC": "BTC/USDT:USDT", "ETH": "ETH/USDT:USDT", "SOL": "SOL/USDT:USDT",
    "BNB": "BNB/USDT:USDT", "XRP": "XRP/USDT:USDT", "ADA": "ADA/USDT:USDT",
    "AVAX": "AVAX/USDT:USDT", "LINK": "LINK/USDT:USDT",
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
    """1銘柄レバレッジBuy&Hold厳密シミュ"""
    df = df[df.index >= pd.Timestamp(START_DATE)]
    if df.empty: return alloc_cash, False, 0

    entry = df["close"].iloc[0] * (1 + SLIP)
    notional = alloc_cash * leverage
    qty = notional / entry
    cash = alloc_cash - notional * FEE
    liquidated = False
    peak = alloc_cash
    max_dd = 0

    for p in df["low"]:
        current_equity = cash + (p - entry) * qty
        mm = p * qty * MMR
        if current_equity <= mm:
            liquidated = True
            return 0, True, 100
        if current_equity > peak: peak = current_equity
        if peak > 0:
            dd = (peak - current_equity) / peak * 100
            max_dd = max(max_dd, dd)

    exit_p = df["close"].iloc[-1] * (1 - SLIP)
    cash += (exit_p - entry) * qty - exit_p * qty * FEE
    return cash, False, max_dd


def static_portfolio(dfs, allocations: Dict[str, float], leverage_map: Dict[str, float], label: str):
    """静的配分ポートフォリオ
    allocations: {"ETH": 0.6, "BTC": 0.4} (合計1.0)
    leverage_map: {"ETH": 3.0, "BTC": 2.0} 銘柄ごとのレバ
    """
    total_final = 0
    worst_dd = 0
    any_liq = False
    detail = []
    for sym, weight in allocations.items():
        if sym not in dfs or weight <= 0: continue
        alloc = INITIAL * weight
        lev = leverage_map.get(sym, 3.0)
        final_val, liq, dd = simulate_buyhold(dfs[sym], alloc, lev)
        total_final += final_val
        worst_dd = max(worst_dd, dd)
        if liq: any_liq = True
        detail.append({"sym": sym, "weight": weight, "lev": lev,
                        "final": final_val, "liq": liq, "dd": dd})

    m_comp = ((total_final/INITIAL) ** (1/12) - 1) * 100 if total_final > 0 else -100
    total_ret = (total_final/INITIAL - 1) * 100
    return {"name": label + (" 💀清算発生" if any_liq else ""),
            "final": total_final, "monthly": m_comp, "return": total_ret,
            "dd": worst_dd, "detail": detail}


def main():
    since_ms = int(FETCH_START.timestamp() * 1000)
    until_ms = int(END_DATE.timestamp() * 1000)

    print(f"\n🌐 静的配分・多通貨Buy&Holdで月+10%挑戦")
    print(f"{'='*90}")
    print(f"期間: {START_DATE.strftime('%Y-%m-%d')} 〜 {END_DATE.strftime('%Y-%m-%d')}")
    print(f"初期: ${INITIAL:,.0f}  /  目標: 月+10%\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    print(f"📥 データ取得中...")
    dfs = {}
    for name, sym in LIQUID.items():
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty: dfs[name] = df
    print(f"✅ {len(dfs)}通貨取得\n")

    # 各銘柄の1年リターンを確認
    print(f"📊 各銘柄の単純Buy&Hold 1年リターン:")
    base_returns = {}
    for name, df in dfs.items():
        df_slice = df[df.index >= pd.Timestamp(START_DATE)]
        if df_slice.empty: continue
        start_p = df_slice["close"].iloc[0]
        end_p = df_slice["close"].iloc[-1]
        ret = (end_p/start_p - 1) * 100
        base_returns[name] = ret
        print(f"    {name:<5s}: {ret:+7.2f}%")
    print()

    tests = []

    # 1-4: BTC+ETH 比率調整
    for btc_pct, eth_pct, label in [(0.5, 0.5, "BTC50/ETH50"), (0.3, 0.7, "BTC30/ETH70"),
                                      (0.2, 0.8, "BTC20/ETH80"), (0.4, 0.6, "BTC40/ETH60")]:
        tests.append((f"{label} 3x",
                      lambda b=btc_pct, e=eth_pct, lbl=label:
                      static_portfolio(dfs, {"BTC": b, "ETH": e},
                                        {"BTC": 3.0, "ETH": 3.0}, f"{lbl} 3x")))
        tests.append((f"{label} 4x",
                      lambda b=btc_pct, e=eth_pct, lbl=label:
                      static_portfolio(dfs, {"BTC": b, "ETH": e},
                                        {"BTC": 4.0, "ETH": 4.0}, f"{lbl} 4x")))

    # 5: 3銘柄 BTC/ETH/BNB (今年強かった組)
    tests.append(("BTC/ETH/BNB 3x 均等",
                   lambda: static_portfolio(dfs, {"BTC": 0.33, "ETH": 0.34, "BNB": 0.33},
                                             {"BTC": 3.0, "ETH": 3.0, "BNB": 3.0}, "3coins 3x")))
    tests.append(("BTC/ETH/BNB 4x 均等",
                   lambda: static_portfolio(dfs, {"BTC": 0.33, "ETH": 0.34, "BNB": 0.33},
                                             {"BTC": 4.0, "ETH": 4.0, "BNB": 4.0}, "3coins 4x")))

    # 6: 5銘柄分散 (保守版)
    tests.append(("5coins 3x (BTC/ETH/BNB/SOL/XRP)",
                   lambda: static_portfolio(dfs,
                         {"BTC": 0.2, "ETH": 0.2, "BNB": 0.2, "SOL": 0.2, "XRP": 0.2},
                         {k: 3.0 for k in ["BTC","ETH","BNB","SOL","XRP"]}, "5coins 3x")))

    # 7: 長期年間リターン上位3を重み付け (各銘柄の年リターンで重み)
    # ETH/BNB/BTC が上位3の想定
    top3_by_perf = sorted(base_returns.items(), key=lambda x: x[1], reverse=True)[:3]
    total = sum(r for _, r in top3_by_perf if r > 0)
    if total > 0:
        weights = {s: (r/total) for s, r in top3_by_perf if r > 0}
        tests.append((f"Performance-Weighted Top3 3x ({','.join(weights.keys())})",
                       lambda: static_portfolio(dfs, weights,
                                                 {k: 3.0 for k in weights}, "PerfWgt3 3x")))
        tests.append((f"Performance-Weighted Top3 4x",
                       lambda: static_portfolio(dfs, weights,
                                                 {k: 4.0 for k in weights}, "PerfWgt3 4x")))

    # 8: 非対称レバ (ETH強めに)
    tests.append(("ETH 4x + BTC 2x (60/40)",
                   lambda: static_portfolio(dfs, {"BTC": 0.4, "ETH": 0.6},
                                             {"BTC": 2.0, "ETH": 4.0}, "ETH4x+BTC2x")))
    tests.append(("ETH 5x + BTC 2x (50/50)",
                   lambda: static_portfolio(dfs, {"BTC": 0.5, "ETH": 0.5},
                                             {"BTC": 2.0, "ETH": 5.0}, "ETH5x+BTC2x")))

    # 9: ETH + BNB 分散(今年強かった2銘柄)
    tests.append(("ETH/BNB 50/50 3x",
                   lambda: static_portfolio(dfs, {"ETH": 0.5, "BNB": 0.5},
                                             {"ETH": 3.0, "BNB": 3.0}, "ETH+BNB 3x")))
    tests.append(("ETH/BNB 50/50 4x",
                   lambda: static_portfolio(dfs, {"ETH": 0.5, "BNB": 0.5},
                                             {"ETH": 4.0, "BNB": 4.0}, "ETH+BNB 4x")))

    # 10: ETH+BTC+BNB 均等 (保守分散)
    tests.append(("ETH/BTC/BNB 50/25/25 4x",
                   lambda: static_portfolio(dfs, {"ETH": 0.5, "BTC": 0.25, "BNB": 0.25},
                                             {"ETH": 4.0, "BTC": 4.0, "BNB": 4.0}, "ETH-heavy 4x")))

    # 実行
    results = []
    for name, fn in tests:
        print(f"🔬 {name}")
        r = fn()
        results.append(r)
        status = "✅" if r["monthly"] >= 10 else ("🎯" if r["monthly"] >= 5 else "⚠️")
        print(f"   {status} 月次{r['monthly']:+.2f}%  DD{r['dd']:.1f}%  ${r['final']:,.0f}")

    # ランキング
    print(f"\n{'='*90}")
    print(f"  📊 ランキング")
    print(f"{'='*90}")
    print(f"  {'戦略':<38s} {'月次':>10s} {'年率':>10s} {'最終':>12s} {'DD':>8s}")
    print(f"  {'-'*82}")
    results.sort(key=lambda x: x["monthly"], reverse=True)
    for r in results:
        print(f"  {r['name']:<38s} {r['monthly']:+8.2f}%  {r['return']:+8.2f}%  ${r['final']:>10,.0f}  {r['dd']:6.1f}%")

    best = results[0]
    print(f"\n  🏆 Best: {best['name']}")
    print(f"     月次 {best['monthly']:+.2f}% / DD {best['dd']:.1f}%")
    if "detail" in best:
        print(f"     配分内訳:")
        for d in best["detail"]:
            print(f"       {d['sym']:<5s} 配分{d['weight']*100:.0f}% レバ{d['lev']}x → ${d['final']:,.0f} (DD{d['dd']:.1f}%)")

    if best["monthly"] >= 10:
        print(f"\n  🎯 ✅ 目標月+10%達成！")
    else:
        print(f"\n  🎯 ❌ 最高{best['monthly']:.2f}% — あと{10-best['monthly']:.2f}%")
    print()


if __name__ == "__main__":
    main()
