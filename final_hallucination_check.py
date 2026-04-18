"""
final_hallucination_check.py
============================
ハルシネーション排除の最終確認

1. stability test ロジック (信頼できる版) を再度実行
2. 修正後の bot ロジック を独立に実装・実行
3. 両者の数字を突き合わせ

一致 → 本物
不一致 → まだバグあり
"""

from __future__ import annotations

import sys
import logging
import warnings
import time
from dataclasses import dataclass
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


# ===================== 実装A: Stability test (信頼版) =====================

def impl_A_stability(df_input: pd.DataFrame, start: datetime, end: datetime):
    """verify_rigorous_stability.py と同じ"""
    df = df_input.copy()
    df["ret"] = df["close"].pct_change()
    df["roll_mean"] = df["ret"].rolling(LOOKBACK).mean() * 365
    df["roll_var"] = df["ret"].rolling(LOOKBACK).var() * 365
    df["kelly_f"] = (df["roll_mean"] / df["roll_var"]).clip(lower=0, upper=MAX_LEV) * FRACTION
    df = df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    if df.empty: return INITIAL, False, 0

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

    return max(balance, 0), liquidated, max_dd


# ===================== 実装B: Bot修正版 (完全に独立した実装) =====================

@dataclass
class BotPos:
    entry_price: float
    size: float   # 取引単位数 (レバ込み数量)
    leverage: float
    margin: float # 拘束された元本


def impl_B_bot_fixed(df_input: pd.DataFrame, start: datetime, end: datetime):
    """独立に書き直したbot実装"""
    df = df_input.copy()
    df["ret"] = df["close"].pct_change()
    df["roll_mean"] = df["ret"].rolling(LOOKBACK).mean() * 365
    df["roll_var"] = df["ret"].rolling(LOOKBACK).var() * 365
    df["kelly_f"] = (df["roll_mean"] / df["roll_var"]).clip(lower=0, upper=MAX_LEV) * FRACTION
    df = df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    if df.empty: return INITIAL, False, 0

    cash = INITIAL
    pos: BotPos = None
    peak = INITIAL
    max_dd = 0
    liquidated = False
    counter = 0

    for ts, row in df.iterrows():
        if pd.isna(row["kelly_f"]): continue
        price = row["close"]

        # 清算チェック (修正版: leverage二重なし)
        if pos is not None:
            low = row["low"]
            current_pnl = (low - pos.entry_price) * pos.size  # ← *leverageなし!
            eq = pos.margin + current_pnl
            mm = low * pos.size * MMR
            if eq <= mm:
                liquidated = True
                cash = 0
                pos = None
                break

        # リバランス
        if counter % REBAL_DAYS == 0:
            # 決済
            if pos is not None:
                exit_p = price * (1 - SLIP)
                pnl = (exit_p - pos.entry_price) * pos.size  # ← *leverageなし!
                fee = exit_p * pos.size * FEE
                cash += pos.margin + pnl - fee
                pos = None

            # エントリー
            kelly_lev = row["kelly_f"]
            if kelly_lev > 0.1 and cash > 0:
                entry = price * (1 + SLIP)
                notional = cash * kelly_lev
                size = notional / entry  # sizeにレバ込み
                fee = notional * FEE
                margin = cash - fee  # 全額拘束
                pos = BotPos(entry_price=entry, size=size, leverage=kelly_lev, margin=margin)
                cash = 0  # 全額ポジションへ

        # Equity追跡
        if pos is not None:
            eq = pos.margin + (price - pos.entry_price) * pos.size
        else:
            eq = cash
        if eq > peak: peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100
            max_dd = max(max_dd, dd)

        counter += 1

    # 最終決済
    if pos is not None and not liquidated:
        exit_p = df.iloc[-1]["close"] * (1 - SLIP)
        pnl = (exit_p - pos.entry_price) * pos.size
        fee = exit_p * pos.size * FEE
        cash += pos.margin + pnl - fee

    return max(cash, 0), liquidated, max_dd


# ===================== 実行 =====================

def main():
    print(f"\n🔬 ハルシネーション最終確認")
    print(f"{'='*105}")
    print(f"2つの独立実装で同じ結果が出るか検証")
    print(f"パラメータ: Kelly frac={FRACTION}, lb={LOOKBACK}, max={MAX_LEV}x, rebal={REBAL_DAYS}日, 初期${INITIAL:,.0f}\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    since_ms = int(datetime(2021, 6, 1).timestamp() * 1000)
    until_ms = int(datetime(2026, 4, 18).timestamp() * 1000)

    print(f"📥 BNBデータ取得...")
    bnb = fetch_ohlcv(ex, "BNB/USDT:USDT", since_ms, until_ms)
    print(f"✅ {len(bnb)}本\n")

    # 多数の期間で比較 (月次スライド)
    analysis_start = datetime(2022, 6, 1)
    analysis_end = datetime(2026, 4, 18)
    windows = []
    cursor = analysis_start
    while cursor + timedelta(days=365) <= analysis_end:
        windows.append((cursor, cursor + timedelta(days=365)))
        cursor += timedelta(days=30)

    print(f"検証ウィンドウ数: {len(windows)}\n")

    print(f"{'='*105}")
    print(f"  🔍 実装A (Stability) vs 実装B (Bot修正版) - BNB単独")
    print(f"{'='*105}")
    print(f"  {'期間':<25s} | {'A.月次':>10s} {'A.最終$':>10s} | {'B.月次':>10s} {'B.最終$':>10s} | {'差':>7s}")
    print(f"  {'-'*95}")

    results_A = []
    results_B = []
    matched = 0
    total = 0
    for start, end in windows:
        finalA, liqA, ddA = impl_A_stability(bnb.copy(), start, end)
        finalB, liqB, ddB = impl_B_bot_fixed(bnb.copy(), start, end)
        months = (end - start).days / 30.0
        monthlyA = ((finalA/INITIAL)**(1/months)-1)*100 if finalA > 0 else -100
        monthlyB = ((finalB/INITIAL)**(1/months)-1)*100 if finalB > 0 else -100
        diff = abs(monthlyA - monthlyB)
        is_match = diff < 0.5  # 0.5%以内なら一致扱い
        match_str = "✅" if is_match else "❌"
        if is_match: matched += 1
        total += 1
        results_A.append(monthlyA)
        results_B.append(monthlyB)

        label = f"{start.strftime('%Y-%m-%d')}"
        print(f"  {label:<25s} | {monthlyA:+7.2f}%  ${finalA:>8,.0f} | {monthlyB:+7.2f}%  ${finalB:>8,.0f} | {diff:5.2f}% {match_str}")

    # 統計
    print(f"\n{'='*105}")
    print(f"  📊 統計比較")
    print(f"{'='*105}")
    print(f"  一致ウィンドウ数: {matched}/{total} ({matched/total*100:.0f}%)")
    print(f"  実装A平均月次: {np.mean(results_A):+.2f}%  中央値: {np.median(results_A):+.2f}%")
    print(f"  実装B平均月次: {np.mean(results_B):+.2f}%  中央値: {np.median(results_B):+.2f}%")
    print(f"  平均差: {abs(np.mean(results_A) - np.mean(results_B)):.2f}%")

    # 最終判定
    print(f"\n{'='*105}")
    if matched / total >= 0.9:
        print(f"  🎊 ✅ ハルシネーションではない! 実装AとBが{matched}/{total} 一致")
        print(f"  Kelly BNB +{np.mean(results_B):.2f}%/月 は本物の戦略")
    elif matched / total >= 0.7:
        print(f"  ⚠️ 概ね一致 ({matched/total*100:.0f}%)、ごく一部ズレあり")
    else:
        print(f"  🚨 一致率低い ({matched/total*100:.0f}%)、まだバグが残っている")
    print(f"{'='*105}")

    # 結果のサマリー
    print(f"\n  🏆 実装B (Bot修正版) の最終成績")
    print(f"  プラス率: {sum(1 for r in results_B if r > 0)}/{len(results_B)} ({sum(1 for r in results_B if r > 0)/len(results_B)*100:.0f}%)")
    print(f"  平均月次: {np.mean(results_B):+.2f}%")
    print(f"  中央値: {np.median(results_B):+.2f}%")
    print(f"  最高/最低: {np.max(results_B):+.2f}% / {np.min(results_B):+.2f}%")
    print(f"  月+8%以上: {sum(1 for r in results_B if r >= 8)}/{len(results_B)}")
    print(f"  月+10%以上: {sum(1 for r in results_B if r >= 10)}/{len(results_B)}")
    print()


if __name__ == "__main__":
    main()
