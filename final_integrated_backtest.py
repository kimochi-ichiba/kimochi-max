"""
final_integrated_backtest.py
============================
全改善統合版の最終バックテスト

統合した改善:
1. Kelly<1.0スキップ (V2)
2. 現金バッファ5% (V3)
3. ボラ連動レバ調整 (V4)
4. ETH追加で3通貨分散 (V5)

配分比較:
- A: BNB70+BTC30 (現行)
- B: BNB50+BTC30+ETH20
- C: BNB40+BTC30+ETH30
- D: BNB40+ETH30+BTC30
- E: BNB33+BTC33+ETH33 (均等)

異なる相場期間で総合評価
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


def compute_kelly_full(df_hist, lookback=60, fraction=0.5, max_lev=10, use_vol_brake=True):
    """全改善込みKelly"""
    returns = df_hist["close"].pct_change().dropna()
    if len(returns) < lookback: return 0.0
    recent = returns.tail(lookback)
    mean_ann = recent.mean() * 365
    var_ann = recent.var() * 365
    if var_ann <= 0 or mean_ann <= 0: return 0.0
    kelly = (mean_ann / var_ann) * fraction
    kelly = float(np.clip(kelly, 0, max_lev))

    if use_vol_brake and len(returns) >= 180:
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


def run_full_bot(dfs, allocations, start, end):
    """全改善込みボット"""
    MIN_LEV = 1.0
    CASH_BUFFER = 0.05
    COOLDOWN_TH = -0.25

    cash = INITIAL
    positions: Dict[str, Pos] = {}
    last_rebal = None
    last_snapshot = INITIAL
    cooldowns = 0
    liqs = 0
    peak = INITIAL
    max_dd = 0

    all_dates = sorted(set().union(*[set(df.index) for df in dfs.values()]))
    all_dates = [d for d in all_dates if start <= d.to_pydatetime() <= end]

    for ts in all_dates:
        # 清算
        for sym in list(positions.keys()):
            pos = positions[sym]
            if sym not in dfs or ts not in dfs[sym].index: continue
            low = dfs[sym].loc[ts]["low"]
            eq = pos.margin + (low - pos.entry) * pos.size
            mm = low * pos.size * MMR
            if eq <= mm:
                liqs += 1
                del positions[sym]

        # Equity
        current_eq = cash
        for sym, pos in positions.items():
            if sym in dfs and ts in dfs[sym].index:
                p = dfs[sym].loc[ts]["close"]
                current_eq += pos.margin + (p - pos.entry) * pos.size
            else:
                current_eq += pos.margin
        if current_eq > peak: peak = current_eq
        if peak > 0:
            dd = (peak - current_eq) / peak * 100
            max_dd = max(max_dd, dd)

        # リバランス
        if last_rebal is None or (ts - last_rebal).days >= 30:
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
                if pr <= COOLDOWN_TH:
                    cooldown = True; cooldowns += 1
            last_snapshot = total

            if not cooldown:
                usable = total * (1 - CASH_BUFFER)
                for sym, w in allocations.items():
                    if sym not in dfs or ts not in dfs[sym].index: continue
                    hist = dfs[sym][dfs[sym].index < ts]
                    kl = compute_kelly_full(hist, use_vol_brake=True)
                    if kl < MIN_LEV: continue
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

    return {"final": max(cash, 0), "max_dd": max_dd, "cooldowns": cooldowns, "liqs": liqs}


def eval_allocation(dfs, allocations, periods, label):
    rets, liqs_total, cds_total = [], 0, 0
    for s, e in periods:
        r = run_full_bot(dfs, allocations, s, e)
        months = (e - s).days / 30.0
        m = ((r["final"]/INITIAL)**(1/months)-1)*100 if r["final"] > 0 else -100
        rets.append(m)
        liqs_total += r["liqs"]
        cds_total += r["cooldowns"]
    pos_rate = sum(1 for m in rets if m > 0) / len(rets) * 100
    return {
        "label": label, "pos_rate": pos_rate,
        "avg": np.mean(rets), "median": np.median(rets),
        "min": np.min(rets), "max": np.max(rets),
        "liqs": liqs_total, "cds": cds_total,
    }


def main():
    print(f"\n🏆 最終統合版バックテスト (全改善込み)")
    print(f"{'='*110}")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    since_ms = int(datetime(2019, 9, 1).timestamp() * 1000)
    until_ms = int(datetime(2026, 4, 18).timestamp() * 1000)

    print(f"📥 3通貨データ取得...")
    dfs = {}
    for name, sym in [("BNB","BNB/USDT:USDT"), ("BTC","BTC/USDT:USDT"), ("ETH","ETH/USDT:USDT")]:
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty:
            dfs[name] = df
            print(f"  {name}: {len(df)}本")
    print()

    # 配分パターン
    allocations_list = [
        ("A. BNB70+BTC30 (現行)", {"BNB": 0.7, "BTC": 0.3}),
        ("B. BNB50+BTC30+ETH20", {"BNB": 0.5, "BTC": 0.3, "ETH": 0.2}),
        ("C. BNB40+BTC30+ETH30", {"BNB": 0.4, "BTC": 0.3, "ETH": 0.3}),
        ("D. BNB40+ETH30+BTC30", {"BNB": 0.4, "ETH": 0.3, "BTC": 0.3}),
        ("E. 3通貨均等 (BNB33+BTC33+ETH33)", {"BNB": 0.34, "BTC": 0.33, "ETH": 0.33}),
        ("F. BNB60+ETH20+BTC20", {"BNB": 0.6, "ETH": 0.2, "BTC": 0.2}),
        ("G. BNB70+BTC15+ETH15", {"BNB": 0.7, "BTC": 0.15, "ETH": 0.15}),
    ]

    # 1年ウィンドウ (月次スライド)
    analysis_start = datetime(2022, 6, 1)
    analysis_end = datetime(2026, 4, 18)
    windows_1y = []
    cursor = analysis_start
    while cursor + timedelta(days=365) <= analysis_end:
        windows_1y.append((cursor, cursor + timedelta(days=365)))
        cursor += timedelta(days=30)

    windows_2y = []
    cursor = analysis_start
    while cursor + timedelta(days=730) <= analysis_end:
        windows_2y.append((cursor, cursor + timedelta(days=730)))
        cursor += timedelta(days=60)

    print(f"1年: {len(windows_1y)}個  /  2年: {len(windows_2y)}個\n")

    # 評価
    print(f"{'='*110}")
    print(f"  📊 1年ウィンドウ結果")
    print(f"{'='*110}")
    print(f"  {'配分':<40s} {'+率':>5s} {'平均月次':>9s} {'中央値':>9s} {'最低':>7s} {'最高':>7s} {'清算':>5s}")
    print(f"  {'-'*100}")

    results_1y = []
    for label, alloc in allocations_list:
        r = eval_allocation(dfs, alloc, windows_1y, label)
        results_1y.append(r)
        print(f"  {label:<40s} {r['pos_rate']:4.0f}% {r['avg']:+7.2f}% {r['median']:+7.2f}% "
              f"{r['min']:+5.1f}% {r['max']:+5.1f}%  {r['liqs']:>4d}")

    # 2年ウィンドウ
    print(f"\n{'='*110}")
    print(f"  📊 2年ウィンドウ結果")
    print(f"{'='*110}")
    print(f"  {'配分':<40s} {'+率':>5s} {'平均月次':>9s} {'中央値':>9s} {'最低':>7s} {'最高':>7s} {'清算':>5s}")
    print(f"  {'-'*100}")

    results_2y = []
    for label, alloc in allocations_list:
        r = eval_allocation(dfs, alloc, windows_2y, label)
        results_2y.append(r)
        print(f"  {label:<40s} {r['pos_rate']:4.0f}% {r['avg']:+7.2f}% {r['median']:+7.2f}% "
              f"{r['min']:+5.1f}% {r['max']:+5.1f}%  {r['liqs']:>4d}")

    # 総合スコア
    print(f"\n{'='*110}")
    print(f"  🏆 総合ランキング (1年+2年の総合評価)")
    print(f"{'='*110}")
    combined = []
    for i, (label, alloc) in enumerate(allocations_list):
        r1 = results_1y[i]; r2 = results_2y[i]
        score = (r1["avg"] + r2["avg"]) / 2 + (r1["pos_rate"] + r2["pos_rate"]) / 200 * 2 - r1["liqs"] * 2
        combined.append({"label": label, "alloc": alloc, "score": score,
                          "r1": r1, "r2": r2})
    combined.sort(key=lambda x: x["score"], reverse=True)

    print(f"  {'順位':<4s} {'配分':<38s} {'スコア':>7s} {'1Y月次':>8s} {'2Y月次':>8s} {'1Y+率':>7s} {'2Y+率':>7s}")
    for i, c in enumerate(combined, 1):
        print(f"  {i:>3d}. {c['label']:<38s} {c['score']:>6.2f}  {c['r1']['avg']:+6.2f}% {c['r2']['avg']:+6.2f}% "
              f"{c['r1']['pos_rate']:>4.0f}%  {c['r2']['pos_rate']:>4.0f}%")

    # 最優秀詳細
    best = combined[0]
    print(f"\n{'='*110}")
    print(f"  💎 最優秀配分: {best['label']}")
    print(f"{'='*110}")
    print(f"  📊 1年: +率 {best['r1']['pos_rate']:.0f}%, 月次平均 {best['r1']['avg']:+.2f}%, 最低 {best['r1']['min']:+.2f}%")
    print(f"  📊 2年: +率 {best['r2']['pos_rate']:.0f}%, 月次平均 {best['r2']['avg']:+.2f}%, 最低 {best['r2']['min']:+.2f}%")
    print(f"  清算: 1年 {best['r1']['liqs']}回 / 2年 {best['r2']['liqs']}回")

    # 期待値
    monthly_avg = (best["r1"]["avg"] + best["r2"]["avg"]) / 2
    expected_1y = 3000 * (1 + monthly_avg/100) ** 12
    expected_2y = 3000 * (1 + monthly_avg/100) ** 24
    print(f"\n  💰 $3,000 期待値 (月{monthly_avg:+.2f}%):")
    print(f"    1年後: ${expected_1y:,.0f} ({expected_1y/3000:.1f}倍)")
    print(f"    2年後: ${expected_2y:,.0f} ({expected_2y/3000:.1f}倍)")

    # 現行 (A) との比較
    current = next(c for c in combined if "A." in c["label"])
    if best["label"] != current["label"]:
        print(f"\n  📈 現行からの改善:")
        print(f"    1年月次: {current['r1']['avg']:+.2f}% → {best['r1']['avg']:+.2f}% ({best['r1']['avg']-current['r1']['avg']:+.2f}%)")
        print(f"    2年月次: {current['r2']['avg']:+.2f}% → {best['r2']['avg']:+.2f}% ({best['r2']['avg']-current['r2']['avg']:+.2f}%)")

    print()


if __name__ == "__main__":
    main()
