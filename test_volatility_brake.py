"""
test_volatility_brake.py
========================
ボラティリティ連動レバ調整機能の効果検証

追加機能:
- 直近30日ボラが長期180日ボラの1.5倍以上 → Kelly × 0.7
- 直近30日ボラが長期180日ボラの2倍以上 → Kelly × 0.5
- 直近30日ボラが長期180日ボラの3倍以上 → Kelly × 0.3

前回2020-2021で清算した理由: Kellyが過去データで高レバ推奨したが、
実際の価格変動は荒くて即清算。事前にボラが急上昇したらレバ下げる。
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


def compute_kelly_with_vol_brake(df_hist, use_vol_brake=True, lookback=60, fraction=0.5, max_lev=10):
    """ボラ連動Kelly"""
    returns = df_hist["close"].pct_change().dropna()
    if len(returns) < lookback: return 0.0
    recent = returns.tail(lookback)
    mean_ann = recent.mean() * 365
    var_ann = recent.var() * 365
    if var_ann <= 0 or mean_ann <= 0: return 0.0
    kelly = (mean_ann / var_ann) * fraction
    kelly = float(np.clip(kelly, 0, max_lev))

    if not use_vol_brake:
        return kelly

    # ボラ比較: 直近30日 vs 長期180日
    if len(returns) >= 180:
        recent_vol = returns.tail(30).std() * np.sqrt(365)
        long_vol = returns.tail(180).std() * np.sqrt(365)
        if long_vol > 0:
            vol_ratio = recent_vol / long_vol
            if vol_ratio >= 3.0:
                kelly *= 0.3  # 異常ボラ → 70%削減
            elif vol_ratio >= 2.0:
                kelly *= 0.5  # 高ボラ → 50%削減
            elif vol_ratio >= 1.5:
                kelly *= 0.7  # やや高ボラ → 30%削減

    return kelly


@dataclass
class Pos:
    entry: float
    size: float
    lev: float
    margin: float


CONFIG = {
    "allocations": {"BNB": 0.7, "BTC": 0.3},
    "min_leverage_threshold": 1.0,
    "cash_buffer_pct": 0.05,
    "cooldown_threshold": -0.25,
}


def run_bot(dfs, start, end, use_vol_brake=True):
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
                period_ret = total / last_snapshot - 1
                if period_ret <= CONFIG["cooldown_threshold"]:
                    cooldown = True
                    cooldowns += 1
            last_snapshot = total

            if not cooldown:
                usable = total * (1 - CONFIG["cash_buffer_pct"])
                for sym, w in CONFIG["allocations"].items():
                    if sym not in dfs or ts not in dfs[sym].index: continue
                    hist = dfs[sym][dfs[sym].index < ts]
                    kl = compute_kelly_with_vol_brake(hist, use_vol_brake=use_vol_brake)
                    if kl < CONFIG["min_leverage_threshold"]: continue

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


def main():
    print(f"\n🔬 ボラ連動レバ調整の効果検証")
    print(f"{'='*100}")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    since_ms = int(datetime(2019, 9, 1).timestamp() * 1000)
    until_ms = int(datetime(2026, 4, 18).timestamp() * 1000)

    print(f"📥 データ取得...")
    dfs = {}
    for name, sym in [("BNB","BNB/USDT:USDT"), ("BTC","BTC/USDT:USDT")]:
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty: dfs[name] = df
    print()

    # 全期間テスト
    periods = [
        (datetime(2020, 1, 1), datetime(2021, 1, 1), "2020 (COVID→回復)"),
        (datetime(2021, 1, 1), datetime(2022, 1, 1), "2021 (バブル)"),
        (datetime(2021, 6, 1), datetime(2022, 6, 1), "2021H2→2022H1 (天井→下落)"),
        (datetime(2022, 1, 1), datetime(2023, 1, 1), "2022 (LUNA・FTX)"),
        (datetime(2022, 6, 1), datetime(2023, 6, 1), "2022H2→2023H1 (底)"),
        (datetime(2023, 1, 1), datetime(2024, 1, 1), "2023 (回復)"),
        (datetime(2023, 6, 1), datetime(2024, 6, 1), "2023H2→2024H1 (ETF)"),
        (datetime(2024, 1, 1), datetime(2025, 1, 1), "2024 (ブル)"),
        (datetime(2024, 6, 1), datetime(2025, 6, 1), "2024H2→2025H1"),
        (datetime(2025, 1, 1), datetime(2026, 1, 1), "2025"),
        (datetime(2025, 4, 18), datetime(2026, 4, 18), "直近1年"),
    ]

    print(f"{'='*100}")
    print(f"  📊 ボラ連動なし vs あり 比較")
    print(f"{'='*100}")
    print(f"  {'期間':<32s} | {'なし月次':>9s} {'清算':>4s} | {'あり月次':>9s} {'清算':>4s} | {'改善':>7s}")
    print(f"  {'-'*92}")

    results_no = []
    results_yes = []
    for start, end, label in periods:
        r_no = run_bot(dfs, start, end, use_vol_brake=False)
        r_yes = run_bot(dfs, start, end, use_vol_brake=True)

        months = (end - start).days / 30.0
        m_no = ((r_no["final"]/INITIAL)**(1/months)-1)*100 if r_no["final"] > 0 else -100
        m_yes = ((r_yes["final"]/INITIAL)**(1/months)-1)*100 if r_yes["final"] > 0 else -100
        diff = m_yes - m_no

        results_no.append(m_no)
        results_yes.append(m_yes)

        print(f"  {label:<32s} | {m_no:+7.2f}% {r_no['liqs']:>3d} | {m_yes:+7.2f}% {r_yes['liqs']:>3d} | {diff:+6.2f}%")

    # 2年ウィンドウも
    print(f"\n{'='*100}")
    print(f"  📊 2年ウィンドウ")
    print(f"{'='*100}")
    print(f"  {'期間':<32s} | {'なし月次':>9s} {'清算':>4s} | {'あり月次':>9s} {'清算':>4s}")
    print(f"  {'-'*85}")

    periods_2y = [
        (datetime(2020, 1, 1), datetime(2022, 1, 1), "【2年】2020-2021"),
        (datetime(2021, 1, 1), datetime(2023, 1, 1), "【2年】2021-2022"),
        (datetime(2022, 1, 1), datetime(2024, 1, 1), "【2年】2022-2023"),
        (datetime(2023, 1, 1), datetime(2025, 1, 1), "【2年】2023-2024"),
        (datetime(2024, 1, 1), datetime(2026, 1, 1), "【2年】2024-2025"),
    ]

    results_no_2y = []
    results_yes_2y = []
    for start, end, label in periods_2y:
        r_no = run_bot(dfs, start, end, use_vol_brake=False)
        r_yes = run_bot(dfs, start, end, use_vol_brake=True)
        months = (end - start).days / 30.0
        m_no = ((r_no["final"]/INITIAL)**(1/months)-1)*100 if r_no["final"] > 0 else -100
        m_yes = ((r_yes["final"]/INITIAL)**(1/months)-1)*100 if r_yes["final"] > 0 else -100
        results_no_2y.append(m_no)
        results_yes_2y.append(m_yes)
        print(f"  {label:<32s} | {m_no:+7.2f}% {r_no['liqs']:>3d} | {m_yes:+7.2f}% {r_yes['liqs']:>3d}")

    # 統計
    print(f"\n{'='*100}")
    print(f"  📈 統計サマリー")
    print(f"{'='*100}")
    print(f"  【1年期間 {len(periods)}個】")
    print(f"    なし: プラス率 {sum(1 for m in results_no if m > 0)/len(results_no)*100:.0f}%, 平均 {np.mean(results_no):+.2f}%")
    print(f"    あり: プラス率 {sum(1 for m in results_yes if m > 0)/len(results_yes)*100:.0f}%, 平均 {np.mean(results_yes):+.2f}%")
    print(f"    最低: {np.min(results_no):+.2f}% → {np.min(results_yes):+.2f}%")

    print(f"\n  【2年期間 {len(periods_2y)}個】")
    print(f"    なし: プラス率 {sum(1 for m in results_no_2y if m > 0)/len(results_no_2y)*100:.0f}%, 平均 {np.mean(results_no_2y):+.2f}%")
    print(f"    あり: プラス率 {sum(1 for m in results_yes_2y if m > 0)/len(results_yes_2y)*100:.0f}%, 平均 {np.mean(results_yes_2y):+.2f}%")

    print()


if __name__ == "__main__":
    main()
