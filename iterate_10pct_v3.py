"""
iterate_10pct_v3.py
===================
+8.23%から+10%へのラストマイル

アイデア:
1. ETH 3.5x/4x/4.5x Buy&Hold (レバ微増)
2. ETH 3x B&H + 月次リバランス (利益の一部を再レバレッジ)
3. ETH 3x B&H + 部分利確戦略 (TP毎にpartial close)
4. "Smart Leverage" ETH (4x bull / 0x bear)
5. ETH + SOL 50/50 3x (SOL高ボラで押し上げ狙い)
6. 流動性マイナー8通貨の2倍モメンタム (ETH, SOL等中堅高ボラ)

清算チェックも厳密に実装。
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
FETCH_START = END_DATE - timedelta(days=365 + 250)
FEE = 0.0006
SLIP = 0.001
MMR = 0.005  # Maintenance margin rate 0.5%


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


def check_liquidation(equity: float, entry: float, current: float, qty: float, leverage: float) -> bool:
    """Binance Futures Isolated型の清算判定"""
    # Margin Ratio = Maintenance Margin / Equity
    # Maintenance Margin = notional * MMR
    # 清算 = Equity <= Maintenance Margin
    notional = current * qty
    maintenance_margin = notional * MMR
    if equity <= maintenance_margin: return True
    return False


def strategy_buyhold_strict(df: pd.DataFrame, leverage: float, label: str) -> dict:
    """厳密な清算判定付きBuy&Hold"""
    df = df[df.index >= pd.Timestamp(START_DATE)].copy()
    if df.empty: return {"name": label, "final": INITIAL, "monthly": 0, "dd": 0}

    entry = df["close"].iloc[0] * (1 + SLIP)
    notional = INITIAL * leverage
    qty = notional / entry
    margin = notional / leverage
    cash = INITIAL - notional * FEE
    liquidated = False
    peak_equity = INITIAL
    max_dd = 0

    for p in df["low"]:  # Low使用で慎重
        current_equity = cash + (p - entry) * qty
        if current_equity > peak_equity: peak_equity = current_equity
        if peak_equity > 0:
            dd = (peak_equity - current_equity) / peak_equity * 100
            max_dd = max(max_dd, dd)
        if check_liquidation(current_equity, entry, p, qty, leverage):
            liquidated = True
            cash = 0
            break

    if not liquidated:
        exit_p = df["close"].iloc[-1] * (1 - SLIP)
        cash += (exit_p - entry) * qty - exit_p * qty * FEE

    final = max(cash, 0)
    m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    total_ret = (final/INITIAL - 1) * 100
    return {"name": label + (" 💀清算" if liquidated else ""), "final": final,
            "monthly": m_comp, "return": total_ret, "dd": max_dd}


def strategy_smart_leverage(df: pd.DataFrame, bull_lev: float, label: str) -> dict:
    """Bull時(EMA50>EMA200)のみbull_leverage、Bear時は0x (キャッシュ)"""
    df = df.copy()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df = df[df.index >= pd.Timestamp(START_DATE)]

    balance = INITIAL
    pos_qty = 0; pos_entry = 0
    peak_equity = INITIAL
    max_dd = 0
    trades = 0
    liquidated = False

    for ts, row in df.iterrows():
        if pd.isna(row["ema200"]): continue
        price = row["close"]
        bull = row["ema50"] > row["ema200"]

        # 清算チェック
        if pos_qty > 0:
            current_equity = balance + (price - pos_entry) * pos_qty
            if check_liquidation(current_equity, pos_entry, row["low"], pos_qty, bull_lev):
                liquidated = True
                balance = 0; pos_qty = 0
                break

        # Exit
        if pos_qty > 0 and not bull:
            exit_p = price * (1 - SLIP)
            balance += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE
            pos_qty = 0

        # Entry
        if pos_qty == 0 and bull:
            entry = price * (1 + SLIP)
            notional = balance * 0.95 * bull_lev
            pos_qty = notional / entry
            pos_entry = entry
            balance -= notional * FEE
            trades += 1

        current_equity = balance + (pos_qty * (price - pos_entry) if pos_qty > 0 else 0)
        if current_equity > peak_equity: peak_equity = current_equity
        if peak_equity > 0:
            dd = (peak_equity - current_equity) / peak_equity * 100
            max_dd = max(max_dd, dd)

    if pos_qty > 0 and not liquidated:
        exit_p = df.iloc[-1]["close"] * (1 - SLIP)
        balance += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE

    final = max(balance, 0)
    m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    total_ret = (final/INITIAL - 1) * 100
    return {"name": label + (" 💀清算" if liquidated else ""), "final": final,
            "monthly": m_comp, "return": total_ret, "dd": max_dd, "trades": trades}


def strategy_monthly_rebalance(df: pd.DataFrame, leverage: float, label: str) -> dict:
    """毎月ポジション再構築 (利益を再レバレッジ)"""
    df = df[df.index >= pd.Timestamp(START_DATE)].copy()
    balance = INITIAL
    current = START_DATE
    liquidated = False
    peak = INITIAL
    max_dd = 0

    while current + timedelta(days=30) <= END_DATE:
        ts_start = pd.Timestamp(current)
        ts_end = pd.Timestamp(current + timedelta(days=30))
        month_df = df[(df.index >= ts_start) & (df.index <= ts_end)]
        if month_df.empty or len(month_df) < 2:
            current += timedelta(days=30); continue

        entry = month_df["close"].iloc[0] * (1 + SLIP)
        notional = balance * leverage
        qty = notional / entry
        balance -= notional * FEE  # 手数料

        # 月内の清算チェック
        liquidated_this_month = False
        for p in month_df["low"]:
            current_equity = balance + (p - entry) * qty
            if current_equity > peak: peak = current_equity
            dd = (peak - current_equity) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)
            if check_liquidation(current_equity, entry, p, qty, leverage):
                liquidated_this_month = True
                break

        if liquidated_this_month:
            balance = 0
            liquidated = True
            break

        exit_p = month_df["close"].iloc[-1] * (1 - SLIP)
        balance += (exit_p - entry) * qty - exit_p * qty * FEE
        current += timedelta(days=30)

    final = max(balance, 0)
    m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    total_ret = (final/INITIAL - 1) * 100
    return {"name": label + (" 💀清算" if liquidated else ""), "final": final,
            "monthly": m_comp, "return": total_ret, "dd": max_dd}


def strategy_split(dfs: dict, symbols: list, leverage: float, label: str) -> dict:
    """均等配分ポートフォリオ"""
    final_total = 0
    worst_dd = 0
    liquidated_any = False
    for sym in symbols:
        if sym not in dfs: continue
        df_sym = dfs[sym]
        r = strategy_buyhold_strict(df_sym, leverage, f"{sym}")
        alloc = INITIAL / len(symbols)
        sym_final = alloc * (r["final"] / INITIAL)
        final_total += sym_final
        worst_dd = max(worst_dd, r["dd"])
        if "清算" in r["name"]: liquidated_any = True

    m_comp = ((final_total/INITIAL) ** (1/12) - 1) * 100 if final_total > 0 else -100
    return {"name": label + (" 💀清算発生" if liquidated_any else ""),
            "final": final_total, "monthly": m_comp,
            "return": (final_total/INITIAL - 1) * 100, "dd": worst_dd}


def main():
    since_ms = int(FETCH_START.timestamp() * 1000)
    until_ms = int(END_DATE.timestamp() * 1000)

    print(f"\n🎯 月+10%達成へ ラストチャレンジ v3")
    print(f"{'='*90}")
    print(f"期間: {START_DATE.strftime('%Y-%m-%d')} 〜 {END_DATE.strftime('%Y-%m-%d')}")
    print(f"初期資金: ${INITIAL:,.0f}  /  目標: 月+10% / 清算判定厳密")
    print(f"{'='*90}\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    print(f"📥 データ取得中...")
    dfs = {}
    for sym, ccxt_sym in [("BTC","BTC/USDT:USDT"),("ETH","ETH/USDT:USDT"),
                           ("SOL","SOL/USDT:USDT"),("BNB","BNB/USDT:USDT")]:
        df = fetch_ohlcv(ex, ccxt_sym, since_ms, until_ms)
        if not df.empty: dfs[sym] = df
    print(f"✅ {len(dfs)}通貨取得\n")

    results = []
    eth = dfs.get("ETH")
    sol = dfs.get("SOL")

    tests = [
        ("ETH 3.5x Buy&Hold",          lambda: strategy_buyhold_strict(eth, 3.5, "ETH 3.5x B&H")),
        ("ETH 4x Buy&Hold",            lambda: strategy_buyhold_strict(eth, 4.0, "ETH 4x B&H")),
        ("ETH 4.5x Buy&Hold",          lambda: strategy_buyhold_strict(eth, 4.5, "ETH 4.5x B&H")),
        ("ETH 5x Buy&Hold",            lambda: strategy_buyhold_strict(eth, 5.0, "ETH 5x B&H")),
        ("ETH 3x Smart Leverage",      lambda: strategy_smart_leverage(eth, 3.0, "ETH 3x Smart")),
        ("ETH 4x Smart Leverage",      lambda: strategy_smart_leverage(eth, 4.0, "ETH 4x Smart")),
        ("ETH 5x Smart Leverage",      lambda: strategy_smart_leverage(eth, 5.0, "ETH 5x Smart")),
        ("ETH 3x Monthly Rebalance",   lambda: strategy_monthly_rebalance(eth, 3.0, "ETH 3x Monthly")),
        ("ETH 4x Monthly Rebalance",   lambda: strategy_monthly_rebalance(eth, 4.0, "ETH 4x Monthly")),
        ("SOL 3x Buy&Hold",            lambda: strategy_buyhold_strict(sol, 3.0, "SOL 3x B&H") if sol is not None else None),
        ("ETH+SOL 3x 均等",            lambda: strategy_split(dfs, ["ETH","SOL"], 3.0, "ETH+SOL 3x")),
        ("ETH+BNB 3x 均等",            lambda: strategy_split(dfs, ["ETH","BNB"], 3.0, "ETH+BNB 3x")),
        ("ETH+SOL 4x 均等",            lambda: strategy_split(dfs, ["ETH","SOL"], 4.0, "ETH+SOL 4x")),
    ]

    for name, fn in tests:
        print(f"🔬 {name}")
        r = fn()
        if r is None: continue
        results.append(r)
        status = "✅" if r["monthly"] >= 10 else ("🎯" if r["monthly"] >= 5 else "⚠️")
        print(f"   {status} 月次: {r['monthly']:+.2f}%  DD: {r['dd']:.1f}%  最終: ${r['final']:,.0f}")

    # ランキング
    print(f"\n{'='*90}")
    print(f"  📊 ランキング")
    print(f"{'='*90}")
    print(f"  {'戦略':<35s} {'月次':>10s} {'年率':>10s} {'最終':>12s} {'DD':>8s}")
    print(f"  {'-'*80}")
    results.sort(key=lambda x: x["monthly"], reverse=True)
    for r in results:
        print(f"  {r['name']:<35s} {r['monthly']:+8.2f}%  {r['return']:+8.2f}%  ${r['final']:>10,.0f}  {r['dd']:6.1f}%")

    best = results[0]
    print(f"\n  🏆 Best: {best['name']}")
    print(f"     月次 {best['monthly']:+.2f}% / 年率 {best['return']:+.2f}% / DD {best['dd']:.1f}%")
    if best["monthly"] >= 10:
        print(f"\n  🎯 ✅✅ 目標月+10%達成！ {best['name']}")
    else:
        print(f"\n  🎯 ❌ 最高 {best['monthly']:.2f}% — あと {10-best['monthly']:.2f}%")
    print()


if __name__ == "__main__":
    main()
