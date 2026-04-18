"""
kelly_bot_fixed.py
==================
レバレッジ二重計算バグ修正版

修正:
- 旧: PnL = (exit - entry) × size × leverage  ❌ レバ二重
- 新: PnL = (exit - entry) × size             ✅ sizeに既にレバ含

これで Stability testの +10% が再現される筈。
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

CONFIG = {
    "allocations": {"BNB": 0.7, "BTC": 0.3},
    "kelly_fraction": 0.5,
    "lookback_days": 60,
    "max_leverage": 10.0,
    "rebalance_days": 30,
    "min_leverage_threshold": 0.1,
    "fee_rate": 0.0006,
    "slippage": 0.001,
    "mmr": 0.005,
    "initial_capital": 3000.0,
}


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


def compute_kelly(df_hist: pd.DataFrame, cfg: dict) -> float:
    returns = df_hist["close"].pct_change().dropna()
    if len(returns) < cfg["lookback_days"]: return 0.0
    recent = returns.tail(cfg["lookback_days"])
    mean_ann = recent.mean() * 365
    var_ann = recent.var() * 365
    if var_ann <= 0 or mean_ann <= 0: return 0.0
    kelly = (mean_ann / var_ann) * cfg["kelly_fraction"]
    return float(np.clip(kelly, 0, cfg["max_leverage"]))


@dataclass
class Pos:
    symbol: str
    entry_price: float
    size: float  # 注: これは既にレバ込みの通貨単位数
    leverage: float
    margin: float


class FixedKellyBacktest:
    def __init__(self, dfs, cfg, start, end):
        self.dfs = dfs
        self.cfg = cfg
        self.start = start
        self.end = end
        self.cash = cfg["initial_capital"]
        self.positions: Dict[str, Pos] = {}
        self.last_rebal = None

    def total_equity(self, ts):
        total = self.cash
        for sym, pos in self.positions.items():
            if sym in self.dfs and ts in self.dfs[sym].index:
                price = self.dfs[sym].loc[ts]["close"]
                # ⭐️ 修正: * leverage 不要 (sizeに既に含まれている)
                pnl = (price - pos.entry_price) * pos.size
                total += pos.margin + pnl
            else:
                total += pos.margin
        return total

    def daily_check(self, ts):
        # 清算チェック (バグ修正版)
        to_close = []
        for sym, pos in self.positions.items():
            if sym not in self.dfs or ts not in self.dfs[sym].index: continue
            low = self.dfs[sym].loc[ts]["low"]
            # ⭐️ 修正: * leverage 不要
            eq = pos.margin + (low - pos.entry_price) * pos.size
            mm = low * pos.size * self.cfg["mmr"]
            if eq <= mm:
                to_close.append(sym)
        for sym in to_close:
            del self.positions[sym]  # 清算でマージン消失

        if self.last_rebal is None:
            self.rebalance(ts)
        elif (ts - self.last_rebal).days >= self.cfg["rebalance_days"]:
            self.rebalance(ts)

    def close_all(self, ts):
        for sym, pos in list(self.positions.items()):
            if sym not in self.dfs or ts not in self.dfs[sym].index: continue
            price = self.dfs[sym].loc[ts]["close"]
            exit_p = price * (1 - self.cfg["slippage"])
            # ⭐️ 修正: * leverage 不要
            pnl = (exit_p - pos.entry_price) * pos.size
            fee = exit_p * pos.size * self.cfg["fee_rate"]
            final_val = max(pos.margin + pnl - fee, 0)
            self.cash += final_val
            del self.positions[sym]

    def rebalance(self, ts):
        self.close_all(ts)
        total_capital = self.cash

        for sym, weight in self.cfg["allocations"].items():
            if sym not in self.dfs: continue
            if ts not in self.dfs[sym].index: continue

            df_hist = self.dfs[sym][self.dfs[sym].index < ts].tail(self.cfg["lookback_days"] + 30)
            kelly_lev = compute_kelly(df_hist, self.cfg)
            if kelly_lev < self.cfg["min_leverage_threshold"]: continue

            alloc = total_capital * weight
            current = self.dfs[sym].loc[ts]["close"]
            entry = current * (1 + self.cfg["slippage"])
            # size に既にレバレッジが入っている (= alloc * kelly_lev / entry)
            notional = alloc * kelly_lev
            size = notional / entry
            fee = notional * self.cfg["fee_rate"]
            margin = alloc - fee

            self.positions[sym] = Pos(
                symbol=sym, entry_price=entry, size=size,
                leverage=kelly_lev, margin=margin,
            )
            self.cash -= margin

        self.last_rebal = ts

    def run(self):
        all_dates = sorted(set().union(*[set(df.index) for df in self.dfs.values()]))
        all_dates = [d for d in all_dates if self.start <= d.to_pydatetime() <= self.end]
        peak = self.cfg["initial_capital"]
        max_dd = 0
        for ts in all_dates:
            self.daily_check(ts)
            eq = self.total_equity(ts)
            if eq > peak: peak = eq
            if peak > 0:
                dd = (peak - eq) / peak * 100
                max_dd = max(max_dd, dd)
        if all_dates and self.positions:
            self.close_all(all_dates[-1])
        return {"final_capital": self.cash, "max_dd": max_dd}


def main():
    print(f"\n✅ Kelly Bot バグ修正版 (レバ二重計算 FIX)")
    print(f"{'='*95}")
    print(f"戦略: BNB 70% + BTC 30% Kelly 0.5x lb60 max10x rebal30d")
    print(f"初期: ${CONFIG['initial_capital']:,.0f}\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    since_ms = int(datetime(2021, 6, 1).timestamp() * 1000)
    until_ms = int(datetime(2026, 4, 18).timestamp() * 1000)

    dfs = {}
    for name, sym in [("BNB","BNB/USDT:USDT"), ("BTC","BTC/USDT:USDT")]:
        print(f"📥 {name}...")
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty: dfs[name] = df
    print(f"✅ 取得完了\n")

    # 複数1年期間
    periods_1y = [
        (datetime(2022, 6, 1), datetime(2023, 6, 1), "2022-06〜2023-06 (ベア)"),
        (datetime(2023, 1, 1), datetime(2024, 1, 1), "2023-01〜2024-01 (回復)"),
        (datetime(2023, 6, 1), datetime(2024, 6, 1), "2023-06〜2024-06 (ブル転換)"),
        (datetime(2024, 1, 1), datetime(2025, 1, 1), "2024-01〜2025-01 (ブル)"),
        (datetime(2024, 6, 1), datetime(2025, 6, 1), "2024-06〜2025-06 (調整)"),
        (datetime(2025, 1, 1), datetime(2026, 1, 1), "2025-01〜2026-01 (直近)"),
        (datetime(2025, 4, 18), datetime(2026, 4, 18), "2025-04〜2026-04 (直近1年)"),
    ]

    print(f"{'='*95}")
    print(f"  📊 1年バックテスト結果 (バグ修正後)")
    print(f"{'='*95}")
    print(f"  {'期間':<40s} {'最終$':>10s} {'総リターン':>10s} {'月次':>8s} {'DD':>6s}")
    print(f"  {'-'*85}")

    rets_1y = []
    for start, end, label in periods_1y:
        engine = FixedKellyBacktest(dfs, CONFIG, start, end)
        r = engine.run()
        final = r["final_capital"]
        total = (final / CONFIG["initial_capital"] - 1) * 100
        months = (end - start).days / 30.0
        monthly = ((final / CONFIG["initial_capital"]) ** (1/months) - 1) * 100 if final > 0 else -100
        rets_1y.append(monthly)
        print(f"  {label:<40s} ${final:>8,.0f}  {total:+8.1f}%  {monthly:+6.2f}%  {r['max_dd']:5.0f}%")

    # 2年
    periods_2y = [
        (datetime(2022, 6, 1), datetime(2024, 6, 1), "2022-06〜2024-06"),
        (datetime(2023, 1, 1), datetime(2025, 1, 1), "2023-01〜2025-01"),
        (datetime(2023, 6, 1), datetime(2025, 6, 1), "2023-06〜2025-06"),
        (datetime(2024, 1, 1), datetime(2026, 1, 1), "2024-01〜2026-01"),
        (datetime(2024, 4, 18), datetime(2026, 4, 18), "2024-04〜2026-04 (直近2年)"),
    ]

    print(f"\n{'='*95}")
    print(f"  📊 2年バックテスト結果 (バグ修正後)")
    print(f"{'='*95}")
    print(f"  {'期間':<40s} {'最終$':>10s} {'総リターン':>10s} {'月次':>8s} {'DD':>6s}")
    print(f"  {'-'*85}")

    rets_2y = []
    for start, end, label in periods_2y:
        engine = FixedKellyBacktest(dfs, CONFIG, start, end)
        r = engine.run()
        final = r["final_capital"]
        total = (final / CONFIG["initial_capital"] - 1) * 100
        months = (end - start).days / 30.0
        monthly = ((final / CONFIG["initial_capital"]) ** (1/months) - 1) * 100 if final > 0 else -100
        rets_2y.append(monthly)
        print(f"  {label:<40s} ${final:>8,.0f}  {total:+8.1f}%  {monthly:+6.2f}%  {r['max_dd']:5.0f}%")

    # 統計
    print(f"\n{'='*95}")
    print(f"  🏆 統計 (バグ修正版)")
    print(f"{'='*95}")
    print(f"  1年期間 ({len(rets_1y)}個):")
    print(f"    プラス: {sum(1 for m in rets_1y if m > 0)}/{len(rets_1y)}")
    print(f"    平均月次: {np.mean(rets_1y):+.2f}%")
    print(f"    中央値: {np.median(rets_1y):+.2f}%")
    print(f"    最高: {np.max(rets_1y):+.2f}%  最低: {np.min(rets_1y):+.2f}%")
    print(f"    月+8%以上: {sum(1 for m in rets_1y if m >= 8)}/{len(rets_1y)}")
    print(f"    月+10%以上: {sum(1 for m in rets_1y if m >= 10)}/{len(rets_1y)}")

    print(f"\n  2年期間 ({len(rets_2y)}個):")
    print(f"    プラス: {sum(1 for m in rets_2y if m > 0)}/{len(rets_2y)}")
    print(f"    平均月次: {np.mean(rets_2y):+.2f}%")
    print(f"    中央値: {np.median(rets_2y):+.2f}%")
    print(f"    最高: {np.max(rets_2y):+.2f}%  最低: {np.min(rets_2y):+.2f}%")
    print(f"    月+8%以上: {sum(1 for m in rets_2y if m >= 8)}/{len(rets_2y)}")
    print(f"    月+10%以上: {sum(1 for m in rets_2y if m >= 10)}/{len(rets_2y)}")
    print()


if __name__ == "__main__":
    main()
