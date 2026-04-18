"""
iterate_10pct_target.py
=======================
月+10%達成まで複数戦略を反復検証

検証戦略 (順次試す):
1. Leveraged ETH Trend Follow (EMA50/200クロスで3倍レバETH)
2. Leveraged BTC Trend Follow (同じロジックBTC)
3. Top 10 Liquid Momentum (流動性のみ、slippage 0.5%現実)
4. BTC/ETH均等 Buy&Hold (レバ無し)
5. BTC/ETH 2倍レバBuy&Hold
6. Triple Momentum (3/6/12ヶ月モメンタム複合)
7. ハイブリッド (最良 + 次善を組み合わせ)

各戦略を$10kで1年運用→ 月次複利を算出 → +10%達成まで反復。
"""

from __future__ import annotations

import sys
import logging
import warnings
import time
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
sys.path.insert(0, "/Users/sanosano/projects/crypto-bot-pro")

logging.getLogger().setLevel(logging.WARNING)

INITIAL = 10_000.0
END_DATE = datetime(2026, 4, 18)
START_DATE = END_DATE - timedelta(days=365)
FETCH_START = END_DATE - timedelta(days=365 + 250)  # EMA200用バッファ
FEE = 0.0006
SLIP_MAJOR = 0.001   # BTC/ETH/SOL等の大型: 0.1%
SLIP_LIQUID = 0.005  # 上位10の中下位: 0.5%

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


# ----------------------------- Strategy 1: Leveraged ETH/BTC Trend ------

def strategy_leveraged_trend(df: pd.DataFrame, leverage: float, slip: float, label: str) -> dict:
    """EMA50 > EMA200 でLong、逆クロスでExit"""
    df = df.copy()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df = df[df.index >= pd.Timestamp(START_DATE)]

    balance = INITIAL
    pos_qty = 0
    pos_entry = 0
    trades = 0
    equity = []

    for ts, row in df.iterrows():
        if pd.isna(row["ema200"]):
            equity.append(balance); continue
        bull = row["ema50"] > row["ema200"]

        # エントリー/決済
        if pos_qty == 0 and bull:
            entry = row["close"] * (1 + slip)
            notional = balance * 0.95 * leverage  # 95%使用
            pos_qty = notional / entry
            pos_entry = entry
            balance -= notional * FEE
            trades += 1
        elif pos_qty > 0 and not bull:
            exit_p = row["close"] * (1 - slip)
            gross = (exit_p - pos_entry) * pos_qty
            fee = exit_p * pos_qty * FEE
            balance += gross - fee
            pos_qty = 0

        eq = balance + (pos_qty * (row["close"] - pos_entry) if pos_qty > 0 else 0)
        equity.append(eq)

    # 最終決済
    if pos_qty > 0:
        exit_p = df.iloc[-1]["close"] * (1 - slip)
        balance += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE
        pos_qty = 0

    final = balance
    total_ret = (final/INITIAL - 1) * 100
    months = 12
    m_comp = ((final/INITIAL) ** (1/months) - 1) * 100 if final > 0 else -100
    peak = INITIAL; max_dd = 0
    for v in equity:
        if v > peak: peak = v
        if peak > 0:
            dd = (peak - v) / peak * 100
            max_dd = max(max_dd, dd)

    return {"name": label, "final": final, "return": total_ret,
            "monthly": m_comp, "dd": max_dd, "trades": trades}


# ----------------------------- Strategy 2: Buy&Hold ---------------------

def strategy_buyhold(df: pd.DataFrame, leverage: float, slip: float, label: str) -> dict:
    df = df[df.index >= pd.Timestamp(START_DATE)].copy()
    if df.empty: return {"name": label, "final": INITIAL, "return": 0, "monthly": 0, "dd": 0, "trades": 0}

    entry = df["close"].iloc[0] * (1 + slip)
    exit_p = df["close"].iloc[-1] * (1 - slip)
    notional = INITIAL * leverage
    qty = notional / entry
    gross = (exit_p - entry) * qty
    fee = (entry + exit_p) * qty * FEE
    final = INITIAL + gross - fee

    # Max DD
    max_dd = 0
    peak = entry
    for p in df["close"]:
        if p > peak: peak = p
        dd = (peak - p) / peak * 100 * leverage  # レバ倍率でDD拡大
        max_dd = max(max_dd, dd)

    total_ret = (final/INITIAL - 1) * 100
    m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    return {"name": label, "final": final, "return": total_ret,
            "monthly": m_comp, "dd": max_dd, "trades": 2}


# ----------------------------- Strategy 3: Top10 Momentum Rebalance -----

def strategy_top10_momentum(all_data: Dict[str, pd.DataFrame], leverage: float, slip: float, label: str) -> dict:
    balance = INITIAL
    holdings = {}
    current = START_DATE
    trades = 0
    equity_pts = []

    while current + timedelta(days=30) <= END_DATE:
        ts = pd.Timestamp(current)
        # 決済
        for sym, h in holdings.items():
            if sym not in all_data: continue
            df_sym = all_data[sym]
            row = df_sym[df_sym.index <= ts]
            if row.empty: continue
            p = row["close"].iloc[-1] * (1 - slip)
            gross = (p - h["entry"]) * h["qty"]
            fee = p * h["qty"] * FEE
            balance += gross - fee
        holdings = {}

        if balance <= 0: break

        # モメンタム計算 (過去30日)
        lookback = ts - timedelta(days=30)
        scores = {}
        for sym in LIQUID_TOP10:
            if sym not in all_data: continue
            df_sym = all_data[sym]
            past = df_sym[df_sym.index <= lookback]
            cur = df_sym[df_sym.index <= ts]
            if past.empty or cur.empty: continue
            p0 = past["close"].iloc[-1]; p1 = cur["close"].iloc[-1]
            if p0 <= 0: continue
            scores[sym] = (p1/p0) - 1

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        # 上位5 (半分) を選ぶ
        top_picks = [s for s, _ in ranked[:5]]
        if not top_picks:
            current += timedelta(days=30); continue

        per_coin = balance / len(top_picks)
        for sym in top_picks:
            df_sym = all_data[sym]
            row = df_sym[df_sym.index <= ts]
            if row.empty: continue
            entry = row["close"].iloc[-1] * (1 + slip)
            notional = per_coin * leverage
            qty = notional / entry
            balance -= notional * FEE
            holdings[sym] = {"entry": entry, "qty": qty}
            trades += 1

        equity_pts.append((current, balance + sum(h["qty"]*h["entry"]/leverage for h in holdings.values())))
        current += timedelta(days=30)

    # 最終決済
    ts_final = pd.Timestamp(END_DATE)
    for sym, h in holdings.items():
        if sym not in all_data: continue
        df_sym = all_data[sym]
        row = df_sym[df_sym.index <= ts_final]
        if row.empty: continue
        p = row["close"].iloc[-1] * (1 - slip)
        balance += (p - h["entry"]) * h["qty"] - p * h["qty"] * FEE

    final = balance
    total_ret = (final/INITIAL - 1) * 100
    m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    return {"name": label, "final": final, "return": total_ret,
            "monthly": m_comp, "dd": 0, "trades": trades}


# ----------------------------- Strategy 4: Triple Momentum --------------

def strategy_triple_momentum(all_data: Dict[str, pd.DataFrame], leverage: float, label: str) -> dict:
    """3/6/12ヶ月モメンタム複合スコア"""
    balance = INITIAL
    holdings = {}
    current = START_DATE
    trades = 0

    while current + timedelta(days=30) <= END_DATE:
        ts = pd.Timestamp(current)
        # 決済
        for sym, h in holdings.items():
            if sym not in all_data: continue
            df_sym = all_data[sym]
            row = df_sym[df_sym.index <= ts]
            if row.empty: continue
            p = row["close"].iloc[-1] * (1 - SLIP_MAJOR)
            balance += (p - h["entry"]) * h["qty"] - p * h["qty"] * FEE
        holdings = {}

        if balance <= 0: break

        # 3つの期間のモメンタム
        scores = {}
        for sym in LIQUID_TOP10:
            if sym not in all_data: continue
            df_sym = all_data[sym]
            cur = df_sym[df_sym.index <= ts]
            if cur.empty: continue
            p_now = cur["close"].iloc[-1]
            total_score = 0
            n_valid = 0
            for days in [30, 90, 180]:
                past = df_sym[df_sym.index <= ts - timedelta(days=days)]
                if past.empty: continue
                p_past = past["close"].iloc[-1]
                if p_past <= 0: continue
                total_score += (p_now/p_past - 1)
                n_valid += 1
            if n_valid > 0:
                scores[sym] = total_score / n_valid

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:5]
        if not ranked:
            current += timedelta(days=30); continue

        per_coin = balance / len(ranked)
        for sym, _ in ranked:
            df_sym = all_data[sym]
            row = df_sym[df_sym.index <= ts]
            if row.empty: continue
            entry = row["close"].iloc[-1] * (1 + SLIP_MAJOR)
            notional = per_coin * leverage
            qty = notional / entry
            balance -= notional * FEE
            holdings[sym] = {"entry": entry, "qty": qty}
            trades += 1

        current += timedelta(days=30)

    # 最終決済
    ts_final = pd.Timestamp(END_DATE)
    for sym, h in holdings.items():
        if sym not in all_data: continue
        df_sym = all_data[sym]
        row = df_sym[df_sym.index <= ts_final]
        if row.empty: continue
        p = row["close"].iloc[-1] * (1 - SLIP_MAJOR)
        balance += (p - h["entry"]) * h["qty"] - p * h["qty"] * FEE

    final = balance
    total_ret = (final/INITIAL - 1) * 100
    m_comp = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    return {"name": label, "final": final, "return": total_ret,
            "monthly": m_comp, "dd": 0, "trades": trades}


# ----------------------------- Main runner ------------------------------

def main():
    since_ms = int(FETCH_START.timestamp() * 1000)
    until_ms = int(END_DATE.timestamp() * 1000)

    print(f"\n🎯 月+10%達成まで反復検証")
    print(f"{'='*90}")
    print(f"期間: {START_DATE.strftime('%Y-%m-%d')} 〜 {END_DATE.strftime('%Y-%m-%d')} (365日)")
    print(f"初期資金: ${INITIAL:,.0f}")
    print(f"目標: 月次複利 +10% (年率+214%)")
    print(f"{'='*90}\n")

    print(f"📥 流動性上位10通貨データ取得中...")
    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    all_data = {}
    for sym in LIQUID_TOP10:
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty: all_data[sym] = df
    print(f"✅ {len(all_data)}/10 取得完了\n")

    results = []

    # 試行1: ETH 3倍レバ Trend Follow
    print(f"🔬 試行1: ETH 3倍レバ Trend Follow")
    eth_df = all_data.get("ETH/USDT:USDT")
    if eth_df is not None:
        r = strategy_leveraged_trend(eth_df, 3.0, SLIP_MAJOR, "ETH 3x Trend")
        results.append(r)
        print(f"   → 月次: {r['monthly']:+.2f}%  DD: {r['dd']:.1f}%  最終: ${r['final']:,.0f}")
        if r["monthly"] >= 10: return check_target(results)

    # 試行2: BTC 3倍レバ Trend Follow
    print(f"🔬 試行2: BTC 3倍レバ Trend Follow")
    btc_df = all_data.get("BTC/USDT:USDT")
    if btc_df is not None:
        r = strategy_leveraged_trend(btc_df, 3.0, SLIP_MAJOR, "BTC 3x Trend")
        results.append(r)
        print(f"   → 月次: {r['monthly']:+.2f}%  DD: {r['dd']:.1f}%  最終: ${r['final']:,.0f}")
        if r["monthly"] >= 10: return check_target(results)

    # 試行3: ETH 3倍レバ Buy&Hold
    print(f"🔬 試行3: ETH 3倍レバ Buy&Hold")
    if eth_df is not None:
        r = strategy_buyhold(eth_df, 3.0, SLIP_MAJOR, "ETH 3x Buy&Hold")
        results.append(r)
        print(f"   → 月次: {r['monthly']:+.2f}%  DD: {r['dd']:.1f}%  最終: ${r['final']:,.0f}")
        if r["monthly"] >= 10: return check_target(results)

    # 試行4: Top10 Momentum (1倍)
    print(f"🔬 試行4: Top10 Momentum Rebalance (1倍レバ)")
    r = strategy_top10_momentum(all_data, 1.0, SLIP_LIQUID, "Top10 Mom 1x")
    results.append(r)
    print(f"   → 月次: {r['monthly']:+.2f}%  最終: ${r['final']:,.0f}  取引: {r['trades']}")
    if r["monthly"] >= 10: return check_target(results)

    # 試行5: Top10 Momentum (3倍)
    print(f"🔬 試行5: Top10 Momentum Rebalance (3倍レバ)")
    r = strategy_top10_momentum(all_data, 3.0, SLIP_LIQUID, "Top10 Mom 3x")
    results.append(r)
    print(f"   → 月次: {r['monthly']:+.2f}%  最終: ${r['final']:,.0f}  取引: {r['trades']}")
    if r["monthly"] >= 10: return check_target(results)

    # 試行6: Triple Momentum
    print(f"🔬 試行6: Triple Momentum (3/6/12ヶ月複合, 2倍レバ)")
    r = strategy_triple_momentum(all_data, 2.0, "Triple Mom 2x")
    results.append(r)
    print(f"   → 月次: {r['monthly']:+.2f}%  最終: ${r['final']:,.0f}")
    if r["monthly"] >= 10: return check_target(results)

    # 試行7: Triple Momentum 高レバ版
    print(f"🔬 試行7: Triple Momentum (3倍レバ)")
    r = strategy_triple_momentum(all_data, 3.0, "Triple Mom 3x")
    results.append(r)
    print(f"   → 月次: {r['monthly']:+.2f}%  最終: ${r['final']:,.0f}")
    if r["monthly"] >= 10: return check_target(results)

    check_target(results)


def check_target(results: List[dict]):
    print(f"\n{'='*90}")
    print(f"  📊 全試行結果ランキング")
    print(f"{'='*90}")
    print(f"  {'戦略':<30s} {'月次複利':>10s} {'年率':>10s} {'最終資金':>12s} {'DD':>8s}")
    print(f"  {'-'*75}")
    results.sort(key=lambda x: x["monthly"], reverse=True)
    for r in results:
        print(f"  {r['name']:<30s} {r['monthly']:+8.2f}%  {r['return']:+8.2f}%  ${r['final']:>10,.0f}  {r['dd']:6.1f}%")

    best = results[0]
    print(f"\n  🏆 Best: {best['name']}")
    print(f"     月次 {best['monthly']:+.2f}% / 年率 {best['return']:+.2f}%")
    print()
    if best["monthly"] >= 10:
        print(f"  🎯 ✅ 目標 月+10% 達成！ ({best['name']})")
    else:
        gap = 10 - best["monthly"]
        print(f"  🎯 ❌ 月+10%未達成。最高{best['monthly']:.2f}%  (目標まであと+{gap:.2f}%)")
    print(f"{'='*90}\n")


if __name__ == "__main__":
    main()
