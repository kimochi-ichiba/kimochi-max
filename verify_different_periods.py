"""
verify_different_periods.py
===========================
異なる相場期間での頑健性検証

検証対象:
- 2020年: COVIDショック (BTC -50%) からの急回復
- 2021年: 仮想通貨バブル
- 2022年: ルナ・FTXクラッシュ (BTC -65%)
- 2023年: 回復局面
- 2024年: ブル相場
- 2025-2026年: 直近

V2改善版ボット (min_leverage_threshold=1.0, cash_buffer=5%) で検証
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


def compute_kelly(df_hist, lookback=60, fraction=0.5, max_lev=10):
    returns = df_hist["close"].pct_change().dropna()
    if len(returns) < lookback: return 0.0
    recent = returns.tail(lookback)
    mean_ann = recent.mean() * 365
    var_ann = recent.var() * 365
    if var_ann <= 0 or mean_ann <= 0: return 0.0
    kelly = (mean_ann / var_ann) * fraction
    return float(np.clip(kelly, 0, max_lev))


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


def run_bot(dfs, start, end):
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

        # Equity計算
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
                    kl = compute_kelly(hist)
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
    print(f"\n🔬 V2改善版ボット 異なる相場期間での頑健性検証")
    print(f"{'='*100}")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})

    # データ取得 (できるだけ長期)
    # Binance Futures は 2019-09 から
    since_ms = int(datetime(2019, 9, 1).timestamp() * 1000)
    until_ms = int(datetime(2026, 4, 18).timestamp() * 1000)

    print(f"📥 データ取得 (2019-09 〜 2026-04)...")
    dfs = {}
    for name, sym in [("BNB","BNB/USDT:USDT"), ("BTC","BTC/USDT:USDT")]:
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty:
            dfs[name] = df
            print(f"  {name}: {len(df)}本 ({df.index[0].strftime('%Y-%m-%d')} 〜)")
    print()

    # 相場期間別テスト
    periods = [
        # 1年ウィンドウ
        (datetime(2020, 1, 1), datetime(2021, 1, 1), "2020年 (COVID→回復)"),
        (datetime(2020, 6, 1), datetime(2021, 6, 1), "2020H2〜2021H1 (急上昇)"),
        (datetime(2021, 1, 1), datetime(2022, 1, 1), "2021年 (バブル)"),
        (datetime(2021, 6, 1), datetime(2022, 6, 1), "2021H2〜2022H1 (天井→下落)"),
        (datetime(2022, 1, 1), datetime(2023, 1, 1), "2022年 (クラッシュ・LUNA・FTX)"),
        (datetime(2022, 6, 1), datetime(2023, 6, 1), "2022H2〜2023H1 (底)"),
        (datetime(2023, 1, 1), datetime(2024, 1, 1), "2023年 (回復)"),
        (datetime(2023, 6, 1), datetime(2024, 6, 1), "2023H2〜2024H1 (ETF承認)"),
        (datetime(2024, 1, 1), datetime(2025, 1, 1), "2024年 (ブル)"),
        (datetime(2024, 6, 1), datetime(2025, 6, 1), "2024H2〜2025H1 (調整)"),
        (datetime(2025, 1, 1), datetime(2026, 1, 1), "2025年 (最近)"),
        (datetime(2025, 4, 18), datetime(2026, 4, 18), "直近1年"),
        # 2年ウィンドウ
        (datetime(2020, 1, 1), datetime(2022, 1, 1), "【2年】2020-2021 (ブル)"),
        (datetime(2021, 1, 1), datetime(2023, 1, 1), "【2年】2021-2022 (天井→底)"),
        (datetime(2022, 1, 1), datetime(2024, 1, 1), "【2年】2022-2023 (ベア→回復)"),
        (datetime(2023, 1, 1), datetime(2025, 1, 1), "【2年】2023-2024 (回復→ブル)"),
        (datetime(2024, 1, 1), datetime(2026, 1, 1), "【2年】2024-2025"),
    ]

    print(f"{'='*100}")
    print(f"  📊 相場期間別 バックテスト結果")
    print(f"{'='*100}")
    print(f"  {'期間':<40s} {'最終$':>10s} {'月次':>8s} {'DD':>6s} {'清算':>5s} {'CD':>4s}")
    print(f"  {'-'*90}")

    results = []
    for start, end, label in periods:
        r = run_bot(dfs, start, end)
        final = r["final"]
        months = (end - start).days / 30.0
        monthly = ((final/INITIAL)**(1/months)-1)*100 if final > 0 else -100
        results.append({
            "label": label, "final": final, "monthly": monthly,
            "dd": r["max_dd"], "liqs": r["liqs"], "cooldowns": r["cooldowns"],
            "is_2y": "【2年】" in label,
        })
        liq_str = f"{r['liqs']}" if r["liqs"] > 0 else "0"
        cd_str = f"{r['cooldowns']}" if r["cooldowns"] > 0 else "0"
        print(f"  {label:<40s} ${final:>8,.0f}  {monthly:+6.2f}%  {r['max_dd']:5.0f}%  {liq_str:>4s}  {cd_str:>3s}")

    # 統計
    r_1y = [r for r in results if not r["is_2y"]]
    r_2y = [r for r in results if r["is_2y"]]

    print(f"\n{'='*100}")
    print(f"  📈 統計")
    print(f"{'='*100}")

    print(f"\n  🗓 1年期間 ({len(r_1y)}個):")
    monthlies = [r["monthly"] for r in r_1y]
    print(f"    プラス期間: {sum(1 for m in monthlies if m > 0)}/{len(monthlies)} ({sum(1 for m in monthlies if m > 0)/len(monthlies)*100:.0f}%)")
    print(f"    平均月次: {np.mean(monthlies):+.2f}%")
    print(f"    中央値: {np.median(monthlies):+.2f}%")
    print(f"    最高: {np.max(monthlies):+.2f}%")
    print(f"    最低: {np.min(monthlies):+.2f}%")
    print(f"    最大DD平均: {np.mean([r['dd'] for r in r_1y]):.0f}%")
    print(f"    清算総計: {sum(r['liqs'] for r in r_1y)}回")

    print(f"\n  🗓 2年期間 ({len(r_2y)}個):")
    monthlies_2y = [r["monthly"] for r in r_2y]
    print(f"    プラス期間: {sum(1 for m in monthlies_2y if m > 0)}/{len(monthlies_2y)} ({sum(1 for m in monthlies_2y if m > 0)/len(monthlies_2y)*100:.0f}%)")
    print(f"    平均月次: {np.mean(monthlies_2y):+.2f}%")
    print(f"    中央値: {np.median(monthlies_2y):+.2f}%")
    print(f"    最高/最低: {np.max(monthlies_2y):+.2f}% / {np.min(monthlies_2y):+.2f}%")

    # 最悪期間での生存確認
    print(f"\n  ⚠️ 危険相場期間での成績:")
    danger_periods = [r for r in r_1y if "クラッシュ" in r["label"] or "天井" in r["label"] or "底" in r["label"]]
    for r in danger_periods:
        status = "✅生存" if r["liqs"] == 0 and r["final"] > 0 else "💀"
        print(f"    {r['label']}: 月{r['monthly']:+.2f}% 最終${r['final']:,.0f} {status}")

    print()


if __name__ == "__main__":
    main()
