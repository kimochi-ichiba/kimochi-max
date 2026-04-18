"""
verify_integer_leverage_backtest.py
===================================
整数レバレッジ版 バックテスト検証

目的: 取引所で実際に設定可能な整数レバレッジ (1,2,3,...,10倍) で
      $3,000 の1年/2年成績を再検証

背景:
  元の verify_real_dollar_backtest.py は Kelly計算値をそのまま
  小数レバレッジ (例: 1.84倍) で使っていた。
  実際の Binance Futures では整数レバレッジしか設定できないため、
  この版では計算値を整数に丸めて再シミュレーションする。

丸めモード比較:
  - round: 四捨五入 (1.84 -> 2) 最も実運用に近い
  - floor: 切り捨て (1.84 -> 1) 保守的

設定:
  - BNB 70% + BTC 30%
  - Kelly Fraction 0.5, Lookback 60日, Max Lev 10
  - Rebalance 30日, Cooldown -25%
  - Min Leverage 1.0, Cash Buffer 5%
  - Vol Brake (1.5/2.0/3.0倍で 0.7/0.5/0.3)
"""

from __future__ import annotations

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
ALLOWED_LEV = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


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


def snap_to_int_leverage(kelly_float: float, mode: str) -> int:
    """小数レバレッジを取引所で選択可能な整数レバレッジに丸める"""
    if kelly_float < 1.0:
        return 0
    if mode == "round":
        v = int(round(kelly_float))
    elif mode == "floor":
        v = int(np.floor(kelly_float))
    else:
        v = int(round(kelly_float))
    return int(np.clip(v, 1, 10))


def compute_kelly(df_hist, lookback=60, fraction=0.5, max_lev=10):
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


def run_bot_int(dfs, start, end, rounding_mode: str):
    """整数レバレッジ版シミュレーション"""
    cash = INITIAL
    positions: Dict[str, Pos] = {}
    last_rebal = None
    last_snapshot = INITIAL
    lev_log = []

    allocations = {"BNB": 0.7, "BTC": 0.3}
    cash_buffer = 0.05
    cooldown_threshold = -0.25

    all_dates = sorted(set().union(*[set(df.index) for df in dfs.values()]))
    all_dates = [d for d in all_dates if start <= d.to_pydatetime() <= end]

    peak = INITIAL
    max_dd = 0

    for ts in all_dates:
        # 清算チェック
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

        # リバランス判定
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
                    if sym not in dfs or ts not in dfs[sym].index:
                        continue
                    hist = dfs[sym][dfs[sym].index < ts]
                    kl_float = compute_kelly(hist)
                    kl_int = snap_to_int_leverage(kl_float, rounding_mode)
                    if kl_int < 1:
                        continue
                    lev_log.append(kl_int)

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

    return {
        "final": max(cash, 0),
        "max_dd": max_dd,
        "lev_log": lev_log,
    }


def run_mode(dfs, periods_1y, periods_2y, mode_name, mode_key):
    print(f"\n{'#'*100}")
    print(f"  モード: {mode_name}  ({mode_key})")
    print(f"{'#'*100}")

    print(f"\n  💵 【1年】({len(periods_1y)}期間)")
    print(f"  {'-'*85}")
    finals_1y, multis_1y, all_levs = [], [], []
    for s, e in periods_1y:
        r = run_bot_int(dfs, s, e, mode_key)
        final = r["final"]
        multi = final / INITIAL
        finals_1y.append(final)
        multis_1y.append(multi)
        all_levs.extend(r["lev_log"])
        print(f"  {s.strftime('%Y-%m-%d')}〜{e.strftime('%Y-%m-%d')}  ${INITIAL:>6,.0f} → ${final:>8,.0f}  "
              f"${final-INITIAL:>+9,.0f}  {multi:>5.2f}倍 DD{r['max_dd']:>4.0f}%")

    print(f"\n  📊 1年統計:")
    print(f"    最終$ 平均    : ${np.mean(finals_1y):,.0f}  (倍率平均: {np.mean(multis_1y):.2f}倍)")
    print(f"    最終$ 中央値  : ${np.median(finals_1y):,.0f}  (倍率中央値: {np.median(multis_1y):.2f}倍)")
    print(f"    最終$ 最低    : ${min(finals_1y):,.0f}  (最低倍率: {min(multis_1y):.2f}倍)")
    print(f"    最終$ 最高    : ${max(finals_1y):,.0f}  (最高倍率: {max(multis_1y):.2f}倍)")
    print(f"    プラス率      : {sum(1 for f in finals_1y if f > INITIAL)}/{len(finals_1y)}")

    print(f"\n  💵 【2年】({len(periods_2y)}期間)")
    print(f"  {'-'*85}")
    finals_2y, multis_2y = [], []
    for s, e in periods_2y:
        r = run_bot_int(dfs, s, e, mode_key)
        final = r["final"]
        multi = final / INITIAL
        finals_2y.append(final)
        multis_2y.append(multi)
        all_levs.extend(r["lev_log"])
        print(f"  {s.strftime('%Y-%m-%d')}〜{e.strftime('%Y-%m-%d')}  ${INITIAL:>6,.0f} → ${final:>8,.0f}  "
              f"${final-INITIAL:>+9,.0f}  {multi:>5.2f}倍 DD{r['max_dd']:>4.0f}%")

    print(f"\n  📊 2年統計:")
    print(f"    最終$ 平均    : ${np.mean(finals_2y):,.0f}  (倍率平均: {np.mean(multis_2y):.2f}倍)")
    print(f"    最終$ 中央値  : ${np.median(finals_2y):,.0f}  (倍率中央値: {np.median(multis_2y):.2f}倍)")
    print(f"    最終$ 最低    : ${min(finals_2y):,.0f}  (最低倍率: {min(multis_2y):.2f}倍)")
    print(f"    最終$ 最高    : ${max(finals_2y):,.0f}  (最高倍率: {max(multis_2y):.2f}倍)")
    print(f"    プラス率      : {sum(1 for f in finals_2y if f > INITIAL)}/{len(finals_2y)}")

    # 実際に使われたレバレッジの分布
    if all_levs:
        from collections import Counter
        c = Counter(all_levs)
        print(f"\n  🔢 実使用レバレッジ分布:")
        for lev in sorted(c.keys()):
            cnt = c[lev]
            pct = cnt / len(all_levs) * 100
            bar = "█" * int(pct / 2)
            print(f"    {lev:>2}x : {cnt:>3}回 ({pct:>5.1f}%) {bar}")

    return {
        "mode": mode_name,
        "finals_1y": finals_1y,
        "multis_1y": multis_1y,
        "finals_2y": finals_2y,
        "multis_2y": multis_2y,
    }


def main():
    print(f"\n💵 整数レバレッジ版 バックテスト検証 ($3,000スタート)")
    print(f"{'='*100}")
    print(f"目的: 取引所で選択可能な整数レバレッジ (1〜10倍) のみで月次/年次成績を確認\n")

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

    # 2モード実行
    r_round = run_mode(dfs, periods_1y, periods_2y, "四捨五入 (最も実運用に近い)", "round")
    r_floor = run_mode(dfs, periods_1y, periods_2y, "切り捨て (保守的)", "floor")

    # 最終比較
    print(f"\n{'='*100}")
    print(f"  🎯 最終比較 ($3,000スタート)")
    print(f"{'='*100}")
    print(f"\n  {'モード':<28s} {'1年中央値':>14s} {'1年平均':>12s} {'2年中央値':>14s} {'2年平均':>12s}")
    print(f"  {'-'*85}")
    for r in [r_round, r_floor]:
        print(f"  {r['mode']:<28s} "
              f"${np.median(r['finals_1y']):>10,.0f}({np.median(r['multis_1y']):>4.2f}倍) "
              f"${np.mean(r['finals_1y']):>8,.0f}({np.mean(r['multis_1y']):>4.2f}倍) "
              f"${np.median(r['finals_2y']):>10,.0f}({np.median(r['multis_2y']):>4.2f}倍) "
              f"${np.mean(r['finals_2y']):>8,.0f}({np.mean(r['multis_2y']):>4.2f}倍)")

    print(f"\n  📌 参考: 旧バージョン (小数レバ許容)")
    print(f"    1年中央値    : $11,553 (3.85倍)")
    print(f"    1年平均      : $12,388 (4.13倍)")
    print(f"    2年中央値    : $37,505 (12.50倍)")
    print(f"    2年平均      : $52,499 (17.50倍)")
    print()


if __name__ == "__main__":
    main()
