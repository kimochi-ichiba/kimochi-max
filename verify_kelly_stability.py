"""
verify_kelly_stability.py
=========================
Kelly BNB戦略 複数1年期間での安定性検証

目的:
- 「Kelly BNB 0.65 max 12x = 月+9.33%」が再現性あるか確認
- 過去複数の1年ウィンドウで検証
- 期間によってリターンがバラつくか安定か

検証期間:
1年ずつズラして複数の1年ウィンドウを検証
- 終了日が複数 (2026-04, 2025-10, 2025-04, 2024-10, 2024-04, 2023-10)
- 各ウィンドウで $3,000 → 最終資金
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
FEE = 0.0006
SLIP = 0.001
MMR = 0.005

# 最適パラメータ (前回で特定)
LOOKBACK = 90
KELLY_FRAC = 0.65
MAX_LEV = 12
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


def run_kelly_bnb(df_full: pd.DataFrame, start_date: datetime, end_date: datetime) -> dict:
    """指定1年期間でKelly BNBを実行"""
    # バッファ込みデータ作成
    df = df_full.copy()
    df["ret"] = df["close"].pct_change()
    df["roll_mean"] = df["ret"].rolling(LOOKBACK).mean() * 365
    df["roll_var"] = df["ret"].rolling(LOOKBACK).var() * 365
    df["kelly_f"] = (df["roll_mean"] / df["roll_var"]).clip(lower=0, upper=MAX_LEV) * KELLY_FRAC

    # 期間スライス
    df = df[(df.index >= pd.Timestamp(start_date)) & (df.index <= pd.Timestamp(end_date))]
    if df.empty:
        return {"final": INITIAL, "liq": False, "dd": 0, "monthly": 0, "return": 0}

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
                liquidated = True; balance = 0
                break
            if eq > peak: peak = eq
            if peak > 0:
                dd = (peak - eq) / peak * 100
                max_dd = max(max_dd, dd)
        counter += 1

    if pos_qty > 0 and not liquidated:
        exit_p = df.iloc[-1]["close"] * (1 - SLIP)
        balance += (exit_p - pos_entry) * pos_qty - exit_p * pos_qty * FEE

    final = max(balance, 0)
    total_ret = (final/INITIAL - 1) * 100
    monthly = ((final/INITIAL) ** (1/12) - 1) * 100 if final > 0 else -100
    return {"final": final, "liq": liquidated, "dd": max_dd,
            "monthly": monthly, "return": total_ret}


def main():
    print(f"\n📐 Kelly BNB 戦略の安定性検証")
    print(f"{'='*95}")
    print(f"設定: BNB Kelly frac={KELLY_FRAC}, max_lev={MAX_LEV}x, lookback={LOOKBACK}日, rebal={REBAL_DAYS}日")
    print(f"初期: $3,000  /  期間: 各1年\n")

    # データは2年半分取得して複数1年ウィンドウで切り出し
    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    fetch_start = datetime(2023, 1, 1)
    fetch_end = datetime(2026, 4, 18)
    since_ms = int(fetch_start.timestamp() * 1000)
    until_ms = int(fetch_end.timestamp() * 1000)

    print(f"📥 BNB データ取得中 ({fetch_start.strftime('%Y-%m-%d')} 〜 {fetch_end.strftime('%Y-%m-%d')})...")
    df_full = fetch_ohlcv(ex, "BNB/USDT:USDT", since_ms, until_ms)
    if df_full.empty:
        print("データ取得失敗")
        return
    print(f"✅ {len(df_full)}本のデータ取得\n")

    # 複数1年ウィンドウ (6ヶ月ずらし、各1年)
    windows = []
    # 最新から6ヶ月刻みで過去方向へ
    end_dates = [
        datetime(2026, 4, 18),
        datetime(2025, 10, 18),
        datetime(2025, 4, 18),
        datetime(2024, 10, 18),
        datetime(2024, 4, 18),
    ]

    print(f"{'='*95}")
    print(f"  📊 各1年ウィンドウでの Kelly BNB 結果")
    print(f"{'='*95}")
    print(f"  {'期間':30s} {'最終資金':>12s} {'利益':>10s} {'月次':>8s} {'年率':>9s} {'DD':>6s} {'状態':>8s}")
    print(f"  {'-'*90}")

    results = []
    for end_d in end_dates:
        start_d = end_d - timedelta(days=365)
        r = run_kelly_bnb(df_full, start_d, end_d)
        profit = r["final"] - INITIAL
        status = "💀清算" if r["liq"] else "✓生存"
        print(f"  {start_d.strftime('%Y-%m-%d')}〜{end_d.strftime('%Y-%m-%d')}  "
              f"${r['final']:>10,.2f}  ${profit:>+8,.0f}  "
              f"{r['monthly']:+6.2f}%  {r['return']:+7.1f}%  {r['dd']:5.0f}%  {status:>8s}")
        results.append({"period": f"{start_d.strftime('%Y-%m')}~{end_d.strftime('%Y-%m')}", **r})

    # 統計
    print(f"\n{'='*95}")
    print(f"  📈 安定性統計")
    print(f"{'='*95}")
    monthlies = [r["monthly"] for r in results]
    returns = [r["return"] for r in results]
    finals = [r["final"] for r in results]
    dds = [r["dd"] for r in results]
    liqs = sum(1 for r in results if r["liq"])

    print(f"  検証ウィンドウ数     : {len(results)}")
    print(f"  清算回数             : {liqs} / {len(results)}")
    print(f"  月次リターン:")
    print(f"    平均               : {np.mean(monthlies):+.2f}%")
    print(f"    中央値             : {np.median(monthlies):+.2f}%")
    print(f"    最高               : {np.max(monthlies):+.2f}%")
    print(f"    最低               : {np.min(monthlies):+.2f}%")
    print(f"    標準偏差           : {np.std(monthlies):.2f}%")
    print(f"  年率リターン:")
    print(f"    平均               : {np.mean(returns):+.2f}%")
    print(f"    中央値             : {np.median(returns):+.2f}%")
    print(f"  最終資金:")
    print(f"    平均               : ${np.mean(finals):,.2f}")
    print(f"    最高               : ${np.max(finals):,.2f}")
    print(f"    最低               : ${np.min(finals):,.2f}")
    print(f"  最大DD:")
    print(f"    平均               : {np.mean(dds):.0f}%")
    print(f"    最高               : {np.max(dds):.0f}%")

    # 判定
    print(f"\n{'='*95}")
    print(f"  🎯 再現性判定")
    print(f"{'='*95}")
    avg_monthly = np.mean(monthlies)
    std_monthly = np.std(monthlies)
    positive_count = sum(1 for m in monthlies if m > 0)

    if avg_monthly >= 8 and positive_count == len(results) and liqs == 0:
        print(f"  ✅ 極めて安定。全期間プラス、平均{avg_monthly:+.2f}%で再現性高い。")
    elif avg_monthly >= 5 and positive_count >= len(results) * 0.7:
        print(f"  🎯 概ね安定。平均{avg_monthly:+.2f}%、{positive_count}/{len(results)}がプラス。")
    elif liqs > 0:
        print(f"  ⚠️ 清算リスクあり。{liqs}回清算発生。")
    elif positive_count >= len(results) * 0.5:
        print(f"  ⚠️ ブレ大きい。平均{avg_monthly:+.2f}%、プラスは{positive_count}/{len(results)}")
    else:
        print(f"  ❌ 不安定。期間によって大きく結果が変動。")

    print(f"\n  📌 結論:")
    print(f"     Kelly BNB 戦略は「過去1年（2025-04〜2026-04）に最適化された結果」です。")
    print(f"     上記検証で{positive_count}/{len(results)}期間プラスなら信頼度そこそこ。")
    print(f"     清算期間があれば、レバを下げるか別戦略が必要。")
    print()


if __name__ == "__main__":
    main()
