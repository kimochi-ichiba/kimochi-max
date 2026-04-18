"""
kelly_bot_safe.py
=================
破綻しない安全版 Kelly Bot

前回の失敗原因を修正:
1. Max leverage 10x → 5x (連続運用での暴走防止)
2. Kelly fraction 0.5 → 0.3 (Third Kelly - より保守的)
3. Stop Loss -15% 追加 (ポジション毎に)
4. Volatility Brake: 直近ボラが通常の2倍超ならレバ半減
5. 連続破滅の回避: 前月-20%以上の損失で翌月スキップ
"""

from __future__ import annotations

import sys
import logging
import warnings
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
logging.getLogger().setLevel(logging.WARNING)

CONFIG = {
    "allocations": {"BNB": 0.5, "BTC": 0.5},  # 均等配分
    "kelly_fraction": 0.3,                     # 0.5→0.3 (Third Kelly)
    "lookback_days": 60,
    "max_leverage": 5.0,                        # 10→5x
    "rebalance_days": 30,
    "min_leverage_threshold": 0.1,
    "fee_rate": 0.0006,
    "slippage": 0.001,
    "mmr": 0.005,
    "initial_capital": 3000.0,
    # 安全装置
    "stop_loss_pct": 0.15,                      # -15%でポジ閉じる
    "vol_brake_mult": 2.0,                      # ボラ2倍でレバ半減
    "cooldown_threshold": -0.20,                # 前月-20%で次月スキップ
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


def compute_kelly_safe(df_hist: pd.DataFrame, cfg: dict) -> float:
    returns = df_hist["close"].pct_change().dropna()
    if len(returns) < cfg["lookback_days"]: return 0.0
    recent = returns.tail(cfg["lookback_days"])
    mean_ann = recent.mean() * 365
    var_ann = recent.var() * 365
    if var_ann <= 0 or mean_ann <= 0: return 0.0
    kelly = (mean_ann / var_ann) * cfg["kelly_fraction"]

    # Volatility Brake: 直近ボラが長期ボラの2倍以上なら半減
    long_var = returns.tail(180).var() * 365 if len(returns) >= 180 else var_ann
    if long_var > 0 and var_ann / long_var > cfg["vol_brake_mult"]:
        kelly *= 0.5

    return float(np.clip(kelly, 0, cfg["max_leverage"]))


@dataclass
class SafePos:
    symbol: str
    entry_price: float
    size: float
    leverage: float
    initial_margin: float
    entry_date: pd.Timestamp
    stop_loss_price: float


class SafeKellyBacktest:
    def __init__(self, dfs, cfg, start, end):
        self.dfs = dfs
        self.cfg = cfg
        self.start = start
        self.end = end
        self.cash = cfg["initial_capital"]
        self.positions: Dict[str, SafePos] = {}
        self.last_rebalance = None
        self.equity_curve = []
        self.trades = []
        self.cooldown_until = None
        self.last_month_return = 0
        self.last_equity_snapshot = cfg["initial_capital"]

    def total_equity(self, ts):
        total = self.cash
        for sym, pos in self.positions.items():
            if sym in self.dfs and ts in self.dfs[sym].index:
                price = self.dfs[sym].loc[ts]["close"]
                pnl = (price - pos.entry_price) * pos.size * pos.leverage
                total += pos.initial_margin + pnl
            else:
                total += pos.initial_margin
        return total

    def close_position(self, sym: str, ts, exit_reason: str):
        if sym not in self.positions: return 0
        pos = self.positions[sym]
        if sym not in self.dfs or ts not in self.dfs[sym].index:
            price = pos.entry_price
        else:
            price = self.dfs[sym].loc[ts]["close"]
        exit_p = price * (1 - self.cfg["slippage"])
        pnl = (exit_p - pos.entry_price) * pos.size * pos.leverage
        fee = exit_p * pos.size * self.cfg["fee_rate"]
        final_value = max(pos.initial_margin + pnl - fee, 0)
        self.cash += final_value
        self.trades.append({
            "date": ts, "symbol": sym, "action": f"close_{exit_reason}",
            "entry": pos.entry_price, "exit": exit_p, "pnl": pnl - fee,
        })
        del self.positions[sym]
        return pnl - fee

    def daily_check(self, ts):
        # ストップロス & 清算チェック
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            if sym not in self.dfs or ts not in self.dfs[sym].index: continue
            low = self.dfs[sym].loc[ts]["low"]

            # ストップロス
            if low <= pos.stop_loss_price:
                self.close_position(sym, ts, "stop_loss")
                continue

            # 清算チェック
            current_eq = pos.initial_margin + (low - pos.entry_price) * pos.size * pos.leverage
            mm = low * pos.size * self.cfg["mmr"]
            if current_eq <= mm:
                self.trades.append({
                    "date": ts, "symbol": sym, "action": "liquidation",
                    "entry": pos.entry_price, "exit": low,
                    "loss": pos.initial_margin,
                })
                del self.positions[sym]

        # リバランス判定
        if self.last_rebalance is None:
            self.rebalance(ts)
        elif (ts - self.last_rebalance).days >= self.cfg["rebalance_days"]:
            self.rebalance(ts)

    def rebalance(self, ts):
        # 全決済
        for sym in list(self.positions.keys()):
            self.close_position(sym, ts, "rebalance")

        current_equity = self.cash

        # Cooldown (前月損失が大きければスキップ)
        if self.last_equity_snapshot > 0:
            last_month_ret = (current_equity / self.last_equity_snapshot) - 1
            self.last_month_return = last_month_ret
            if last_month_ret <= self.cfg["cooldown_threshold"]:
                # 次月スキップ
                self.last_rebalance = ts
                self.last_equity_snapshot = current_equity
                return
        self.last_equity_snapshot = current_equity

        # 各通貨エントリー
        for sym, weight in self.cfg["allocations"].items():
            if sym not in self.dfs: continue
            if ts not in self.dfs[sym].index: continue

            df_hist = self.dfs[sym][self.dfs[sym].index < ts].tail(self.cfg["lookback_days"] + 200)
            kelly_lev = compute_kelly_safe(df_hist, self.cfg)

            if kelly_lev < self.cfg["min_leverage_threshold"]:
                continue

            alloc = current_equity * weight
            current_price = self.dfs[sym].loc[ts]["close"]
            entry_price = current_price * (1 + self.cfg["slippage"])
            notional = alloc * kelly_lev
            size = notional / entry_price
            fee = notional * self.cfg["fee_rate"]
            initial_margin = alloc - fee
            stop_loss_price = entry_price * (1 - self.cfg["stop_loss_pct"] / kelly_lev)

            self.positions[sym] = SafePos(
                symbol=sym, entry_price=entry_price, size=size,
                leverage=kelly_lev, initial_margin=initial_margin,
                entry_date=ts, stop_loss_price=stop_loss_price,
            )
            self.cash -= initial_margin
            self.trades.append({
                "date": ts, "symbol": sym, "action": "open",
                "entry": entry_price, "leverage": kelly_lev, "alloc": alloc,
            })

        self.last_rebalance = ts

    def run(self):
        all_dates = sorted(set().union(*[set(df.index) for df in self.dfs.values()]))
        all_dates = [d for d in all_dates if self.start <= d.to_pydatetime() <= self.end]
        peak = self.cfg["initial_capital"]
        max_dd = 0
        for ts in all_dates:
            self.daily_check(ts)
            eq = self.total_equity(ts)
            self.equity_curve.append({"date": ts, "equity": eq})
            if eq > peak: peak = eq
            if peak > 0:
                dd = (peak - eq) / peak * 100
                max_dd = max(max_dd, dd)
        if all_dates and self.positions:
            for sym in list(self.positions.keys()):
                self.close_position(sym, all_dates[-1], "final")
        return {"final_capital": self.cash, "trades": self.trades, "max_dd": max_dd}


def main():
    print(f"\n🛡️ Kelly Bot 安全版 バックテスト")
    print(f"{'='*90}")
    print(f"戦略: BNB50 + BTC50  /  Kelly 0.3x lb60 max5x  /  SL-15% + Vol Brake + Cooldown")
    print(f"初期資金: ${CONFIG['initial_capital']:,.0f}\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    since_ms = int(datetime(2021, 6, 1).timestamp() * 1000)
    until_ms = int(datetime(2026, 4, 18).timestamp() * 1000)

    dfs = {}
    for name, sym in [("BNB","BNB/USDT:USDT"), ("BTC","BTC/USDT:USDT")]:
        print(f"📥 {name}...")
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty: dfs[name] = df
    print(f"✅ 取得完了\n")

    # 複数1年期間で検証
    print(f"{'='*90}")
    print(f"  📊 1年バックテスト (複数期間)")
    print(f"{'='*90}")
    print(f"  {'期間':<40s} {'最終$':>10s} {'総リターン':>10s} {'月次':>8s} {'DD':>6s}")
    print(f"  {'-'*85}")

    periods_1y = [
        (datetime(2022, 6, 1), datetime(2023, 6, 1), "2022-06〜2023-06 (ベア)"),
        (datetime(2023, 1, 1), datetime(2024, 1, 1), "2023-01〜2024-01 (回復)"),
        (datetime(2023, 6, 1), datetime(2024, 6, 1), "2023-06〜2024-06 (ブル転換)"),
        (datetime(2024, 1, 1), datetime(2025, 1, 1), "2024-01〜2025-01 (ブル)"),
        (datetime(2024, 6, 1), datetime(2025, 6, 1), "2024-06〜2025-06 (調整)"),
        (datetime(2025, 1, 1), datetime(2026, 1, 1), "2025-01〜2026-01 (直近)"),
        (datetime(2025, 4, 18), datetime(2026, 4, 18), "2025-04〜2026-04 (直近1年)"),
    ]
    rets_1y = []
    for start, end, label in periods_1y:
        engine = SafeKellyBacktest(dfs, CONFIG, start, end)
        r = engine.run()
        final = r["final_capital"]
        total = (final / CONFIG["initial_capital"] - 1) * 100
        months = (end - start).days / 30.0
        monthly = ((final / CONFIG["initial_capital"]) ** (1/months) - 1) * 100 if final > 0 else -100
        rets_1y.append(monthly)
        print(f"  {label:<40s} ${final:>8,.0f}  {total:+8.1f}%  {monthly:+6.2f}%  {r['max_dd']:5.0f}%")

    # 2年
    print(f"\n{'='*90}")
    print(f"  📊 2年バックテスト")
    print(f"{'='*90}")
    print(f"  {'期間':<40s} {'最終$':>10s} {'総リターン':>10s} {'月次':>8s} {'DD':>6s}")
    print(f"  {'-'*85}")
    periods_2y = [
        (datetime(2022, 6, 1), datetime(2024, 6, 1), "2022-06〜2024-06"),
        (datetime(2023, 1, 1), datetime(2025, 1, 1), "2023-01〜2025-01"),
        (datetime(2023, 6, 1), datetime(2025, 6, 1), "2023-06〜2025-06"),
        (datetime(2024, 1, 1), datetime(2026, 1, 1), "2024-01〜2026-01"),
        (datetime(2024, 4, 18), datetime(2026, 4, 18), "2024-04〜2026-04 (直近2年)"),
    ]
    rets_2y = []
    for start, end, label in periods_2y:
        engine = SafeKellyBacktest(dfs, CONFIG, start, end)
        r = engine.run()
        final = r["final_capital"]
        total = (final / CONFIG["initial_capital"] - 1) * 100
        months = (end - start).days / 30.0
        monthly = ((final / CONFIG["initial_capital"]) ** (1/months) - 1) * 100 if final > 0 else -100
        rets_2y.append(monthly)
        print(f"  {label:<40s} ${final:>8,.0f}  {total:+8.1f}%  {monthly:+6.2f}%  {r['max_dd']:5.0f}%")

    # 統計
    print(f"\n{'='*90}")
    print(f"  🏆 安全版 統計")
    print(f"{'='*90}")
    print(f"  1年期間 ({len(rets_1y)}個):")
    print(f"    プラス: {sum(1 for m in rets_1y if m > 0)}/{len(rets_1y)}")
    print(f"    平均月次: {np.mean(rets_1y):+.2f}%")
    print(f"    中央値: {np.median(rets_1y):+.2f}%")
    print(f"    最高 / 最低: {np.max(rets_1y):+.2f}% / {np.min(rets_1y):+.2f}%")
    print(f"\n  2年期間 ({len(rets_2y)}個):")
    print(f"    プラス: {sum(1 for m in rets_2y if m > 0)}/{len(rets_2y)}")
    print(f"    平均月次: {np.mean(rets_2y):+.2f}%")
    print(f"    中央値: {np.median(rets_2y):+.2f}%")
    print(f"    最高 / 最低: {np.max(rets_2y):+.2f}% / {np.min(rets_2y):+.2f}%")
    print()


if __name__ == "__main__":
    main()
