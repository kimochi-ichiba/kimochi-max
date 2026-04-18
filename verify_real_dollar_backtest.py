"""
verify_real_dollar_backtest.py
==============================
実際のドル建て終値バックテスト (理論値ではない)

目的: "$3,000が1年後3.3倍、2年後14倍" が本当か厳密検証

方法:
- V1最優秀版 (Vol Brake込み) の実ロジックで
- 複数の1年・2年期間で実際の$推移を記録
- 最終$値を正確に報告

改善版ボット設定:
- BNB 70% + BTC 30%
- Kelly Fraction 0.5, Lookback 60日, Max Lev 10
- Rebalance 30日, Cooldown -25%
- Min Leverage 1.0, Cash Buffer 5%
- Vol Brake (1.5/2.0/3.0倍で 0.7/0.5/0.3)
"""

from __future__ import annotations

import sys
import logging
import warnings
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
logging.getLogger().setLevel(logging.WARNING)

INITIAL = 3000.0
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


def compute_kelly(df_hist, lookback=60, fraction=0.5, max_lev=10):
    returns = df_hist["close"].pct_change().dropna()
    if len(returns) < lookback: return 0.0
    recent = returns.tail(lookback)
    mean_ann = recent.mean() * 365
    var_ann = recent.var() * 365
    if var_ann <= 0 or mean_ann <= 0: return 0.0
    kelly = (mean_ann / var_ann) * fraction
    kelly = float(np.clip(kelly, 0, max_lev))

    # Vol Brake
    if len(returns) >= 180:
        recent_vol = returns.tail(30).std() * np.sqrt(365)
        long_vol = returns.tail(180).std() * np.sqrt(365)
        if long_vol > 0:
            ratio = recent_vol / long_vol
            if ratio >= 3.0: kelly *= 0.3
            elif ratio >= 2.0: kelly *= 0.5
            elif ratio >= 1.5: kelly *= 0.7
    return kelly


@dataclass
class Pos:
    entry: float
    size: float
    lev: float
    margin: float


def run_bot_exact(dfs, start, end):
    """実装ボットと同じロジックを厳密に再現"""
    cash = INITIAL
    positions: Dict[str, Pos] = {}
    last_rebal = None
    last_snapshot = INITIAL

    allocations = {"BNB": 0.7, "BTC": 0.3}
    min_lev_threshold = 1.0
    cash_buffer = 0.05
    cooldown_threshold = -0.25

    all_dates = sorted(set().union(*[set(df.index) for df in dfs.values()]))
    all_dates = [d for d in all_dates if start <= d.to_pydatetime() <= end]

    peak = INITIAL
    max_dd = 0
    daily_equity = []

    for ts in all_dates:
        # 清算
        for sym in list(positions.keys()):
            pos = positions[sym]
            if sym not in dfs or ts not in dfs[sym].index: continue
            low = dfs[sym].loc[ts]["low"]
            eq = pos.margin + (low - pos.entry) * pos.size
            mm = low * pos.size * MMR
            if eq <= mm:
                del positions[sym]

        # Equity追跡
        current_eq = cash
        for sym, pos in positions.items():
            if sym in dfs and ts in dfs[sym].index:
                p = dfs[sym].loc[ts]["close"]
                current_eq += pos.margin + (p - pos.entry) * pos.size
            else:
                current_eq += pos.margin

        daily_equity.append({"date": ts, "equity": current_eq})
        if current_eq > peak: peak = current_eq
        if peak > 0:
            dd = (peak - current_eq) / peak * 100
            max_dd = max(max_dd, dd)

        # リバランス
        if last_rebal is None or (ts - last_rebal).days >= 30:
            # 決済
            for sym in list(positions.keys()):
                pos = positions[sym]
                if ts not in dfs[sym].index: continue
                price = dfs[sym].loc[ts]["close"]
                exit_p = price * (1 - SLIP)
                pnl = (exit_p - pos.entry) * pos.size
                fee = exit_p * pos.size * FEE
                cash += max(pos.margin + pnl - fee, 0)
                del positions[sym]

            total = cash
            cooldown = False
            if last_snapshot > 0:
                pr = total / last_snapshot - 1
                if pr <= cooldown_threshold:
                    cooldown = True
            last_snapshot = total

            if not cooldown:
                usable = total * (1 - cash_buffer)
                for sym, w in allocations.items():
                    if sym not in dfs or ts not in dfs[sym].index: continue
                    hist = dfs[sym][dfs[sym].index < ts]
                    kl = compute_kelly(hist)
                    if kl < min_lev_threshold: continue

                    alloc = usable * w
                    current = dfs[sym].loc[ts]["close"]
                    entry = current * (1 + SLIP)
                    notional = alloc * kl
                    size = notional / entry
                    fee = notional * FEE
                    margin = alloc - fee

                    positions[sym] = Pos(entry=entry, size=size, lev=kl, margin=margin)
                    cash -= margin
            last_rebal = ts

    # 最終決済
    if all_dates and positions:
        ts = all_dates[-1]
        for sym in list(positions.keys()):
            pos = positions[sym]
            if ts not in dfs[sym].index: continue
            price = dfs[sym].loc[ts]["close"]
            exit_p = price * (1 - SLIP)
            pnl = (exit_p - pos.entry) * pos.size
            fee = exit_p * pos.size * FEE
            cash += max(pos.margin + pnl - fee, 0)

    return {"final": max(cash, 0), "max_dd": max_dd, "daily_equity": daily_equity}


def main():
    print(f"\n💵 実際のドル建て バックテスト検証")
    print(f"{'='*100}")
    print(f"目的: $3,000 が本当に1年後3.3倍/2年後14倍になるか実証\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    since_ms = int(datetime(2021, 6, 1).timestamp() * 1000)
    until_ms = int(datetime(2026, 4, 18).timestamp() * 1000)

    print(f"📥 BNB/BTC データ取得...")
    dfs = {}
    for name, sym in [("BNB","BNB/USDT:USDT"), ("BTC","BTC/USDT:USDT")]:
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty:
            dfs[name] = df
    print(f"✅ 取得完了\n")

    # 複数の1年期間 (月次スライド)
    analysis_start = datetime(2022, 6, 1)
    analysis_end = datetime(2026, 4, 18)

    periods_1y = []
    cursor = analysis_start
    while cursor + timedelta(days=365) <= analysis_end:
        periods_1y.append((cursor, cursor + timedelta(days=365)))
        cursor += timedelta(days=30)

    periods_2y = []
    cursor = analysis_start
    while cursor + timedelta(days=730) <= analysis_end:
        periods_2y.append((cursor, cursor + timedelta(days=730)))
        cursor += timedelta(days=60)

    # 1年期間の実際の$結果
    print(f"{'='*100}")
    print(f"  💵 【1年】実際のドル建て結果 ({len(periods_1y)}期間)")
    print(f"{'='*100}")
    print(f"  {'期間':<28s} {'開始$':>8s} → {'終了$':>10s}  {'利益$':>11s}  {'倍率':>6s} {'DD':>5s}")
    print(f"  {'-'*85}")

    finals_1y = []
    multipliers_1y = []
    for s, e in periods_1y:
        r = run_bot_exact(dfs, s, e)
        final = r["final"]
        multi = final / INITIAL
        profit = final - INITIAL
        finals_1y.append(final)
        multipliers_1y.append(multi)
        print(f"  {s.strftime('%Y-%m-%d')}〜{e.strftime('%Y-%m-%d')}  ${INITIAL:>6,.0f} → ${final:>8,.0f}  "
              f"${profit:>+9,.0f}  {multi:>5.2f}倍 {r['max_dd']:>4.0f}%")

    # 1年統計
    print(f"\n  📊 1年統計 ({len(finals_1y)}期間):")
    print(f"    最終$ 平均  : ${np.mean(finals_1y):,.0f}  (倍率平均: {np.mean(multipliers_1y):.2f}倍)")
    print(f"    最終$ 中央値 : ${np.median(finals_1y):,.0f}  (倍率中央値: {np.median(multipliers_1y):.2f}倍)")
    print(f"    最終$ 最高  : ${max(finals_1y):,.0f}  (最高倍率: {max(multipliers_1y):.2f}倍)")
    print(f"    最終$ 最低  : ${min(finals_1y):,.0f}  (最低倍率: {min(multipliers_1y):.2f}倍)")
    print(f"    プラス率    : {sum(1 for f in finals_1y if f > INITIAL)}/{len(finals_1y)}")

    # 2年期間の実際の$結果
    print(f"\n{'='*100}")
    print(f"  💵 【2年】実際のドル建て結果 ({len(periods_2y)}期間)")
    print(f"{'='*100}")
    print(f"  {'期間':<28s} {'開始$':>8s} → {'終了$':>10s}  {'利益$':>11s}  {'倍率':>6s} {'DD':>5s}")
    print(f"  {'-'*85}")

    finals_2y = []
    multipliers_2y = []
    for s, e in periods_2y:
        r = run_bot_exact(dfs, s, e)
        final = r["final"]
        multi = final / INITIAL
        profit = final - INITIAL
        finals_2y.append(final)
        multipliers_2y.append(multi)
        print(f"  {s.strftime('%Y-%m-%d')}〜{e.strftime('%Y-%m-%d')}  ${INITIAL:>6,.0f} → ${final:>8,.0f}  "
              f"${profit:>+9,.0f}  {multi:>5.2f}倍 {r['max_dd']:>4.0f}%")

    # 2年統計
    print(f"\n  📊 2年統計 ({len(finals_2y)}期間):")
    print(f"    最終$ 平均  : ${np.mean(finals_2y):,.0f}  (倍率平均: {np.mean(multipliers_2y):.2f}倍)")
    print(f"    最終$ 中央値 : ${np.median(finals_2y):,.0f}  (倍率中央値: {np.median(multipliers_2y):.2f}倍)")
    print(f"    最終$ 最高  : ${max(finals_2y):,.0f}  (最高倍率: {max(multipliers_2y):.2f}倍)")
    print(f"    最終$ 最低  : ${min(finals_2y):,.0f}  (最低倍率: {min(multipliers_2y):.2f}倍)")
    print(f"    プラス率    : {sum(1 for f in finals_2y if f > INITIAL)}/{len(finals_2y)}")

    # 前回主張との比較
    print(f"\n{'='*100}")
    print(f"  🎯 前回の主張 vs 実測")
    print(f"{'='*100}")
    print(f"  【1年】")
    print(f"    前回主張       : $3,000 → $9,794 (3.26倍)")
    print(f"    実測 平均      : $3,000 → ${np.mean(finals_1y):,.0f} ({np.mean(multipliers_1y):.2f}倍)")
    print(f"    実測 中央値    : $3,000 → ${np.median(finals_1y):,.0f} ({np.median(multipliers_1y):.2f}倍)")
    print(f"    誤差(平均 vs 主張): {abs(np.mean(multipliers_1y) - 3.26):.2f}倍 乖離")
    print()
    print(f"  【2年】")
    print(f"    前回主張       : $3,000 → $41,982 (14.0倍)")
    print(f"    実測 平均      : $3,000 → ${np.mean(finals_2y):,.0f} ({np.mean(multipliers_2y):.2f}倍)")
    print(f"    実測 中央値    : $3,000 → ${np.median(finals_2y):,.0f} ({np.median(multipliers_2y):.2f}倍)")
    print(f"    誤差(平均 vs 主張): {abs(np.mean(multipliers_2y) - 14.0):.2f}倍 乖離")

    # 信頼性判定
    print(f"\n{'='*100}")
    print(f"  🎯 信頼性判定")
    print(f"{'='*100}")
    diff_1y = abs(np.mean(multipliers_1y) - 3.26) / 3.26 * 100
    diff_2y = abs(np.mean(multipliers_2y) - 14.0) / 14.0 * 100

    if diff_1y < 10 and diff_2y < 20:
        print(f"  ✅ 主張は信頼できる (誤差 1年{diff_1y:.0f}%, 2年{diff_2y:.0f}%)")
    elif diff_1y < 25 and diff_2y < 40:
        print(f"  ⚠️ 主張は概ね正しいが、期間依存 (誤差 1年{diff_1y:.0f}%, 2年{diff_2y:.0f}%)")
    else:
        print(f"  🚨 主張との乖離大 (誤差 1年{diff_1y:.0f}%, 2年{diff_2y:.0f}%)")
        print(f"      理論的月次複利と実バックテストに差あり")

    # より現実的な見方
    print(f"\n{'='*100}")
    print(f"  📊 現実的な $3,000の行方 (実測ベース)")
    print(f"{'='*100}")
    print(f"  【1年運用】")
    print(f"    最悪ケース: ${min(finals_1y):,.0f} ({min(multipliers_1y):.2f}倍)")
    print(f"    普通ケース: ${np.median(finals_1y):,.0f} ({np.median(multipliers_1y):.2f}倍)")
    print(f"    最高ケース: ${max(finals_1y):,.0f} ({max(multipliers_1y):.2f}倍)")
    print(f"\n  【2年運用】")
    print(f"    最悪ケース: ${min(finals_2y):,.0f} ({min(multipliers_2y):.2f}倍)")
    print(f"    普通ケース: ${np.median(finals_2y):,.0f} ({np.median(multipliers_2y):.2f}倍)")
    print(f"    最高ケース: ${max(finals_2y):,.0f} ({max(multipliers_2y):.2f}倍)")
    print()


if __name__ == "__main__":
    main()
