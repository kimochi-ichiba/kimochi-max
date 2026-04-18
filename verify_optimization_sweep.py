"""
verify_optimization_sweep.py
============================
最適化スイープ - Kelly Bot の最適設定を探索

探索する軸:
  1. 配分 (allocation): BTC100, BNB100, 50:50, 70:30 (BNB:BTC), 30:70
  2. Kelly Fraction: 0.25, 0.5, 0.75, 1.0
  3. Lookback: 30, 60, 90 日

評価指標:
  - 1年中央値リターン
  - 1年最低値リターン
  - 最大ドローダウン
  - 全期間プラス率

レバレッジ: Binance実上限 (BTC=125x, BNB=75x), 四捨五入モード
"""

from __future__ import annotations

import logging
import warnings
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
logging.getLogger().setLevel(logging.WARNING)

INITIAL = 3000.0
FEE = 0.0006
SLIP = 0.001
MMR = 0.005

BINANCE_MAX_LEV = {
    "BTC": 125,
    "BNB": 75,
}


def fetch_ohlcv(ex, symbol, since_ms, until_ms):
    tf_ms = 86400 * 1000
    all_data = []
    current = since_ms
    while current < until_ms:
        try:
            batch = ex.fetch_ohlcv(symbol, "1d", since=current, limit=1000)
            if not batch:
                break
            batch = [c for c in batch if c[0] < until_ms]
            all_data.extend(batch)
            if len(batch) < 1000:
                break
            current = batch[-1][0] + tf_ms
            time.sleep(0.1)
        except Exception:
            break
    if not all_data:
        return pd.DataFrame()
    df = pd.DataFrame(all_data, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.set_index("timestamp").drop_duplicates().sort_index().astype(float)


def snap_to_int_leverage(kelly_float: float, max_lev: int) -> int:
    if kelly_float < 1.0:
        return 0
    return int(np.clip(int(round(kelly_float)), 1, max_lev))


def compute_kelly(df_hist, lookback=60, fraction=0.5, max_lev=125):
    returns = df_hist["close"].pct_change().dropna()
    if len(returns) < lookback:
        return 0.0
    recent = returns.tail(lookback)
    mean_ann = recent.mean() * 365
    var_ann = recent.var() * 365
    if var_ann <= 0 or mean_ann <= 0:
        return 0.0
    kelly = (mean_ann / var_ann) * fraction
    kelly = float(np.clip(kelly, 0, max_lev))

    if len(returns) >= 180:
        recent_vol = returns.tail(30).std() * np.sqrt(365)
        long_vol = returns.tail(180).std() * np.sqrt(365)
        if long_vol > 0:
            ratio = recent_vol / long_vol
            if ratio >= 3.0:
                kelly *= 0.3
            elif ratio >= 2.0:
                kelly *= 0.5
            elif ratio >= 1.5:
                kelly *= 0.7
    return kelly


@dataclass
class Pos:
    entry: float
    size: float
    lev: float
    margin: float


def run_bot(dfs, start, end, allocations: Dict[str, float],
            kelly_fraction: float, lookback: int):
    cash = INITIAL
    positions: Dict[str, Pos] = {}
    last_rebal = None
    last_snapshot = INITIAL

    cash_buffer = 0.05
    cooldown_threshold = -0.25

    all_dates = sorted(set().union(*[set(df.index) for df in dfs.values()]))
    all_dates = [d for d in all_dates if start <= d.to_pydatetime() <= end]

    peak = INITIAL
    max_dd = 0

    for ts in all_dates:
        for sym in list(positions.keys()):
            pos = positions[sym]
            if sym not in dfs or ts not in dfs[sym].index:
                continue
            low = dfs[sym].loc[ts]["low"]
            eq = pos.margin + (low - pos.entry) * pos.size
            mm = low * pos.size * MMR
            if eq <= mm:
                del positions[sym]

        current_eq = cash
        for sym, pos in positions.items():
            if sym in dfs and ts in dfs[sym].index:
                p = dfs[sym].loc[ts]["close"]
                current_eq += pos.margin + (p - pos.entry) * pos.size
            else:
                current_eq += pos.margin

        if current_eq > peak:
            peak = current_eq
        if peak > 0:
            dd = (peak - current_eq) / peak * 100
            max_dd = max(max_dd, dd)

        if last_rebal is None or (ts - last_rebal).days >= 30:
            for sym in list(positions.keys()):
                pos = positions[sym]
                if ts not in dfs[sym].index:
                    continue
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
                    if w <= 0 or sym not in dfs or ts not in dfs[sym].index:
                        continue
                    max_lev_sym = BINANCE_MAX_LEV.get(sym, 10)
                    hist = dfs[sym][dfs[sym].index < ts]
                    kl_float = compute_kelly(hist, lookback=lookback,
                                             fraction=kelly_fraction,
                                             max_lev=max_lev_sym)
                    kl_int = snap_to_int_leverage(kl_float, max_lev_sym)
                    if kl_int < 1:
                        continue

                    alloc = usable * w
                    current = dfs[sym].loc[ts]["close"]
                    entry = current * (1 + SLIP)
                    notional = alloc * kl_int
                    size = notional / entry
                    fee = notional * FEE
                    margin = alloc - fee

                    positions[sym] = Pos(entry=entry, size=size, lev=kl_int, margin=margin)
                    cash -= margin
            last_rebal = ts

    if all_dates and positions:
        ts = all_dates[-1]
        for sym in list(positions.keys()):
            pos = positions[sym]
            if ts not in dfs[sym].index:
                continue
            price = dfs[sym].loc[ts]["close"]
            exit_p = price * (1 - SLIP)
            pnl = (exit_p - pos.entry) * pos.size
            fee = exit_p * pos.size * FEE
            cash += max(pos.margin + pnl - fee, 0)

    return {"final": max(cash, 0), "max_dd": max_dd}


def evaluate_config(dfs, periods_1y, periods_2y,
                    allocations, kelly_fraction, lookback):
    finals_1y, dds_1y = [], []
    for s, e in periods_1y:
        r = run_bot(dfs, s, e, allocations, kelly_fraction, lookback)
        finals_1y.append(r["final"])
        dds_1y.append(r["max_dd"])

    finals_2y, dds_2y = [], []
    for s, e in periods_2y:
        r = run_bot(dfs, s, e, allocations, kelly_fraction, lookback)
        finals_2y.append(r["final"])
        dds_2y.append(r["max_dd"])

    return {
        "med_1y": np.median(finals_1y),
        "mean_1y": np.mean(finals_1y),
        "min_1y": min(finals_1y),
        "max_1y": max(finals_1y),
        "plus_1y": sum(1 for f in finals_1y if f > INITIAL),
        "n_1y": len(finals_1y),
        "med_2y": np.median(finals_2y),
        "mean_2y": np.mean(finals_2y),
        "min_2y": min(finals_2y),
        "plus_2y": sum(1 for f in finals_2y if f > INITIAL),
        "n_2y": len(finals_2y),
        "max_dd_1y": max(dds_1y),
        "avg_dd_1y": np.mean(dds_1y),
    }


def fmt_alloc(alloc: Dict[str, float]) -> str:
    parts = []
    for sym in ["BNB", "BTC"]:
        if alloc.get(sym, 0) > 0:
            parts.append(f"{sym}{int(alloc[sym]*100)}")
    return "/".join(parts) if parts else "-"


def main():
    print(f"\n🔍 最適化スイープ ($3,000スタート, Binance実レバ, 四捨五入)")
    print(f"{'='*110}")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    since_ms = int(datetime(2021, 6, 1).timestamp() * 1000)
    until_ms = int(datetime(2026, 4, 18).timestamp() * 1000)

    print(f"📥 BNB/BTC データ取得...")
    dfs = {}
    for name, sym in [("BNB", "BNB/USDT:USDT"), ("BTC", "BTC/USDT:USDT")]:
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty:
            dfs[name] = df
    print(f"✅ 取得完了\n")

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

    # スイープ設定
    allocations_list = [
        {"BNB": 1.0, "BTC": 0.0},
        {"BNB": 0.7, "BTC": 0.3},
        {"BNB": 0.5, "BTC": 0.5},
        {"BNB": 0.3, "BTC": 0.7},
        {"BNB": 0.0, "BTC": 1.0},
    ]
    fractions = [0.25, 0.5, 0.75, 1.0]
    lookbacks = [30, 60, 90]

    total_combos = len(allocations_list) * len(fractions) * len(lookbacks)
    print(f"スイープ範囲: 配分{len(allocations_list)}種 × Kelly{len(fractions)}種 × Lookback{len(lookbacks)}種 = {total_combos}パターン")
    print(f"各パターン × 1年{len(periods_1y)}期間 + 2年{len(periods_2y)}期間 = {total_combos * (len(periods_1y)+len(periods_2y))}回のバックテスト\n")

    results: List[Tuple[dict, dict]] = []
    idx = 0
    for alloc in allocations_list:
        for frac in fractions:
            for lb in lookbacks:
                idx += 1
                cfg = {"alloc": alloc, "kelly_fraction": frac, "lookback": lb}
                print(f"  [{idx:>2}/{total_combos}] {fmt_alloc(alloc):>12s}  K={frac:.2f}  LB={lb:>3}日", end="  ", flush=True)
                t0 = time.time()
                res = evaluate_config(dfs, periods_1y, periods_2y, alloc, frac, lb)
                dt = time.time() - t0
                results.append((cfg, res))
                print(f"→ 1yr中央${res['med_1y']:>7,.0f}({res['med_1y']/INITIAL:>4.2f}x)  "
                      f"2yr中央${res['med_2y']:>7,.0f}({res['med_2y']/INITIAL:>5.2f}x)  "
                      f"最低1yr${res['min_1y']:>6,.0f}  DD{res['max_dd_1y']:>4.0f}%  [{dt:.1f}s]")

    # ランキング表示
    print(f"\n{'='*110}")
    print(f"  📊 ランキング TOP10 (1年中央値基準)")
    print(f"{'='*110}")
    print(f"  {'順位':<4s} {'配分':<12s} {'Kelly':<6s} {'LB':<5s} {'1yr中央':>10s} {'1yr平均':>10s} {'1yr最低':>10s} {'2yr中央':>10s} {'最大DD':>8s} {'プラス率':>10s}")
    print(f"  {'-'*110}")
    sorted_by_med1y = sorted(results, key=lambda x: -x[1]["med_1y"])
    for rank, (cfg, res) in enumerate(sorted_by_med1y[:10], 1):
        print(f"  {rank:<4d} {fmt_alloc(cfg['alloc']):<12s} {cfg['kelly_fraction']:<6.2f} {cfg['lookback']:<5d} "
              f"${res['med_1y']:>8,.0f} ${res['mean_1y']:>8,.0f} ${res['min_1y']:>8,.0f} "
              f"${res['med_2y']:>8,.0f} {res['max_dd_1y']:>6.0f}% {res['plus_1y']}/{res['n_1y']}({res['plus_1y']/res['n_1y']*100:.0f}%)")

    print(f"\n{'='*110}")
    print(f"  🛡️ 安定性ランキング TOP10 (1年最低値基準 = 最悪ケースが良い)")
    print(f"{'='*110}")
    print(f"  {'順位':<4s} {'配分':<12s} {'Kelly':<6s} {'LB':<5s} {'1yr中央':>10s} {'1yr最低':>10s} {'2yr中央':>10s} {'2yr最低':>10s} {'最大DD':>8s}")
    print(f"  {'-'*110}")
    sorted_by_min1y = sorted(results, key=lambda x: -x[1]["min_1y"])
    for rank, (cfg, res) in enumerate(sorted_by_min1y[:10], 1):
        print(f"  {rank:<4d} {fmt_alloc(cfg['alloc']):<12s} {cfg['kelly_fraction']:<6.2f} {cfg['lookback']:<5d} "
              f"${res['med_1y']:>8,.0f} ${res['min_1y']:>8,.0f} "
              f"${res['med_2y']:>8,.0f} ${res['min_2y']:>8,.0f} {res['max_dd_1y']:>6.0f}%")

    # リスク調整指標 (中央値 / 最大DD)
    print(f"\n{'='*110}")
    print(f"  ⚖️ リスク効率ランキング TOP10 (1yr中央値 ÷ 最大DD%)")
    print(f"{'='*110}")
    print(f"  {'順位':<4s} {'配分':<12s} {'Kelly':<6s} {'LB':<5s} {'効率':>8s} {'1yr中央':>10s} {'1yr最低':>10s} {'最大DD':>8s}")
    print(f"  {'-'*110}")
    def efficiency(r):
        if r["max_dd_1y"] <= 0:
            return 0
        return r["med_1y"] / r["max_dd_1y"]
    sorted_by_eff = sorted(results, key=lambda x: -efficiency(x[1]))
    for rank, (cfg, res) in enumerate(sorted_by_eff[:10], 1):
        eff = efficiency(res)
        print(f"  {rank:<4d} {fmt_alloc(cfg['alloc']):<12s} {cfg['kelly_fraction']:<6.2f} {cfg['lookback']:<5d} "
              f"{eff:>8.0f} ${res['med_1y']:>8,.0f} ${res['min_1y']:>8,.0f} {res['max_dd_1y']:>6.0f}%")

    # 2年ランキング
    print(f"\n{'='*110}")
    print(f"  💎 長期成績ランキング TOP10 (2年中央値基準)")
    print(f"{'='*110}")
    print(f"  {'順位':<4s} {'配分':<12s} {'Kelly':<6s} {'LB':<5s} {'2yr中央':>10s} {'2yr最低':>10s} {'2yr最高':>10s} {'1yr中央':>10s} {'最大DD':>8s}")
    print(f"  {'-'*110}")
    sorted_by_med2y = sorted(results, key=lambda x: -x[1]["med_2y"])
    for rank, (cfg, res) in enumerate(sorted_by_med2y[:10], 1):
        print(f"  {rank:<4d} {fmt_alloc(cfg['alloc']):<12s} {cfg['kelly_fraction']:<6.2f} {cfg['lookback']:<5d} "
              f"${res['med_2y']:>8,.0f} ${res['min_2y']:>8,.0f} ${res.get('max_2y', res['mean_2y']*2-res['min_2y']):>8,.0f} "
              f"${res['med_1y']:>8,.0f} {res['max_dd_1y']:>6.0f}%")

    # 推奨設定の提案
    print(f"\n{'='*110}")
    print(f"  🎯 推奨設定の候補")
    print(f"{'='*110}")

    best_med1y = sorted_by_med1y[0]
    safest = sorted_by_min1y[0]
    best_eff = sorted_by_eff[0]
    best_med2y = sorted_by_med2y[0]

    def show_cfg(label, cfg, res):
        print(f"\n  【{label}】")
        print(f"    配分     : {fmt_alloc(cfg['alloc'])}")
        print(f"    Kelly係数: {cfg['kelly_fraction']}")
        print(f"    Lookback : {cfg['lookback']}日")
        print(f"    1年中央値: ${res['med_1y']:,.0f} ({res['med_1y']/INITIAL:.2f}倍)")
        print(f"    1年最低値: ${res['min_1y']:,.0f} ({res['min_1y']/INITIAL:.2f}倍)")
        print(f"    2年中央値: ${res['med_2y']:,.0f} ({res['med_2y']/INITIAL:.2f}倍)")
        print(f"    最大DD   : {res['max_dd_1y']:.0f}%")
        print(f"    プラス率 : {res['plus_1y']}/{res['n_1y']}")

    show_cfg("最大リターン重視 (1年中央値トップ)", best_med1y[0], best_med1y[1])
    show_cfg("安全性重視 (最悪ケースが最もマシ)", safest[0], safest[1])
    show_cfg("リスク効率重視 (リターン/DDがベスト)", best_eff[0], best_eff[1])
    show_cfg("長期重視 (2年中央値トップ)", best_med2y[0], best_med2y[1])
    print()


if __name__ == "__main__":
    main()
