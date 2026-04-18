"""
kelly_bot_backtest_v2.py
========================
バグ修正版: 正確な資本追跡でボット動作を再現

修正点:
1. 決済時に initial_margin + pnl を資本に戻す (元本も返却)
2. リバランス時は全資金を一度清算 → 総資金で配分計算
3. total_equity 方式で正確にトラッキング
"""

from __future__ import annotations

import sys
import logging
import warnings
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
logging.getLogger().setLevel(logging.WARNING)

CONFIG = {
    "allocations": {"BNB": 0.70, "BTC": 0.30},
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


def compute_kelly(df_hist: pd.DataFrame, lookback: int, fraction: float, max_lev: float) -> float:
    returns = df_hist["close"].pct_change().dropna()
    if len(returns) < lookback: return 0.0
    recent = returns.tail(lookback)
    mean_ann = recent.mean() * 365
    var_ann = recent.var() * 365
    if var_ann <= 0 or mean_ann <= 0: return 0.0
    kelly = (mean_ann / var_ann) * fraction
    return float(np.clip(kelly, 0, max_lev))


@dataclass
class Position:
    symbol: str
    entry_price: float
    size: float  # 通貨数
    leverage: float
    entry_date: pd.Timestamp
    initial_margin: float  # このポジションに拘束された資本

    def current_value(self, current_price: float) -> float:
        """このポジションの現在の評価額 (=元本+未実現損益)"""
        pnl = (current_price - self.entry_price) * self.size * self.leverage
        return self.initial_margin + pnl


class KellyBacktest:
    def __init__(self, dfs: Dict[str, pd.DataFrame], cfg: dict, start: datetime, end: datetime):
        self.dfs = dfs
        self.cfg = cfg
        self.start = start
        self.end = end
        self.cash = cfg["initial_capital"]  # 手元現金 (ポジション外)
        self.positions: Dict[str, Position] = {}
        self.last_rebalance: Optional[pd.Timestamp] = None
        self.equity_curve = []
        self.trades = []
        self.rebalances = []

    def total_equity(self, ts: pd.Timestamp) -> float:
        """総資産 = 現金 + 全ポジション評価額"""
        total = self.cash
        for sym, pos in self.positions.items():
            if sym in self.dfs and ts in self.dfs[sym].index:
                price = self.dfs[sym].loc[ts]["close"]
                total += pos.current_value(price)
            else:
                # データない日はエントリー時評価で代用
                total += pos.initial_margin
        return total

    def daily_check(self, ts: pd.Timestamp):
        # 清算チェック (low値で厳密)
        to_close = []
        for sym, pos in self.positions.items():
            if sym not in self.dfs or ts not in self.dfs[sym].index:
                continue
            low = self.dfs[sym].loc[ts]["low"]
            pos_value = pos.current_value(low)
            mm = low * pos.size * self.cfg["mmr"]
            if pos_value <= mm:
                # 清算: 元本全損
                self.trades.append({
                    "date": ts, "symbol": sym, "action": "liquidation",
                    "entry": pos.entry_price, "exit": low,
                    "loss": pos.initial_margin,
                })
                to_close.append(sym)
        for sym in to_close:
            del self.positions[sym]
            # 清算されたのでそのポジションの資本は失われる (cashには戻らない)

        # リバランス判定
        if self.last_rebalance is None:
            return self.rebalance(ts)
        elif (ts - self.last_rebalance).days >= self.cfg["rebalance_days"]:
            self.rebalance(ts)

    def close_all(self, ts: pd.Timestamp):
        """全ポジション決済 → 現金に戻す"""
        for sym, pos in list(self.positions.items()):
            if sym not in self.dfs or ts not in self.dfs[sym].index:
                continue
            price = self.dfs[sym].loc[ts]["close"]
            exit_p = price * (1 - self.cfg["slippage"])
            # 決済後のこのポジションの価値
            pnl = (exit_p - pos.entry_price) * pos.size * pos.leverage
            fee = exit_p * pos.size * self.cfg["fee_rate"]
            final_value = pos.initial_margin + pnl - fee
            final_value = max(final_value, 0)  # 負は0
            self.cash += final_value  # 現金に戻す (元本+損益)

            self.trades.append({
                "date": ts, "symbol": sym, "action": "close",
                "entry": pos.entry_price, "exit": exit_p,
                "pnl": pnl - fee, "leverage": pos.leverage,
                "margin": pos.initial_margin, "final_value": final_value,
            })
            del self.positions[sym]

    def rebalance(self, ts: pd.Timestamp):
        # 全決済
        self.close_all(ts)
        # この時点で self.cash = 全資金
        total_capital = self.cash

        rebal_log = {"date": ts, "capital": total_capital, "entries": []}

        for sym, weight in self.cfg["allocations"].items():
            if sym not in self.dfs: continue
            if ts not in self.dfs[sym].index: continue

            df_hist = self.dfs[sym][self.dfs[sym].index < ts].tail(self.cfg["lookback_days"] + 30)
            kelly_lev = compute_kelly(df_hist, self.cfg["lookback_days"],
                                        self.cfg["kelly_fraction"], self.cfg["max_leverage"])

            if kelly_lev < self.cfg["min_leverage_threshold"]:
                rebal_log["entries"].append({"symbol": sym, "skipped": True, "kelly": kelly_lev})
                continue

            # 重要: 割当は**total_capital(全資金)×weight**で計算
            alloc = total_capital * weight
            current_price = self.dfs[sym].loc[ts]["close"]
            entry_price = current_price * (1 + self.cfg["slippage"])
            notional = alloc * kelly_lev
            size = notional / entry_price
            fee = notional * self.cfg["fee_rate"]
            initial_margin = alloc - fee  # 拘束される資本

            self.positions[sym] = Position(
                symbol=sym, entry_price=entry_price, size=size,
                leverage=kelly_lev, entry_date=ts, initial_margin=initial_margin,
            )
            self.cash -= initial_margin  # 現金から差し引く

            self.trades.append({
                "date": ts, "symbol": sym, "action": "open",
                "entry": entry_price, "size": size, "leverage": kelly_lev,
                "alloc": alloc, "margin": initial_margin,
            })
            rebal_log["entries"].append({
                "symbol": sym, "kelly": kelly_lev, "alloc": alloc,
                "entry_price": entry_price,
            })

        self.last_rebalance = ts
        self.rebalances.append(rebal_log)

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

        # 最終決済
        if all_dates and self.positions:
            self.close_all(all_dates[-1])

        return {
            "final_capital": self.cash,
            "equity_curve": self.equity_curve,
            "trades": self.trades,
            "rebalances": self.rebalances,
            "max_dd": max_dd,
        }


def main():
    print(f"\n🤖 Kelly Bot 最終バックテスト v2 (バグ修正版)")
    print(f"{'='*95}")
    print(f"戦略: BNB 70% + BTC 30% Kelly 0.5x lb60 max10x rebal30d")
    print(f"初期資金: ${CONFIG['initial_capital']:,.0f}\n")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    since_ms = int(datetime(2021, 6, 1).timestamp() * 1000)
    until_ms = int(datetime(2026, 4, 18).timestamp() * 1000)

    dfs = {}
    for name, sym in [("BNB","BNB/USDT:USDT"), ("BTC","BTC/USDT:USDT")]:
        print(f"📥 {name}...")
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty: dfs[name] = df
    print(f"✅ {len(dfs)}通貨取得\n")

    # 複数期間で検証
    periods_1y = [
        (datetime(2022, 6, 1), datetime(2023, 6, 1), "2022-06〜2023-06 (ベア)"),
        (datetime(2023, 1, 1), datetime(2024, 1, 1), "2023-01〜2024-01 (回復)"),
        (datetime(2023, 6, 1), datetime(2024, 6, 1), "2023-06〜2024-06 (ブル転換)"),
        (datetime(2024, 1, 1), datetime(2025, 1, 1), "2024-01〜2025-01 (ブル続行)"),
        (datetime(2024, 6, 1), datetime(2025, 6, 1), "2024-06〜2025-06 (調整期)"),
        (datetime(2025, 1, 1), datetime(2026, 1, 1), "2025-01〜2026-01 (直近)"),
        (datetime(2025, 4, 18), datetime(2026, 4, 18), "2025-04〜2026-04 (直近1年)"),
    ]

    print(f"{'='*95}")
    print(f"  📊 期間別 バックテスト結果 (1年)")
    print(f"{'='*95}")
    print(f"  {'期間':<40s} {'最終$':>10s} {'総リターン':>10s} {'月次':>8s} {'DD':>6s}")
    print(f"  {'-'*85}")

    results_1y = []
    for start, end, label in periods_1y:
        engine = KellyBacktest(dfs, CONFIG, start, end)
        r = engine.run()
        final = r["final_capital"]
        ret = (final / CONFIG["initial_capital"] - 1) * 100
        months = (end - start).days / 30.0
        monthly = ((final / CONFIG["initial_capital"]) ** (1/months) - 1) * 100 if final > 0 else -100
        results_1y.append({"label": label, "final": final, "total_ret": ret,
                            "monthly": monthly, "dd": r["max_dd"]})
        print(f"  {label:<40s} ${final:>8,.0f}  {ret:+8.1f}%  {monthly:+6.2f}%  {r['max_dd']:5.0f}%")

    # 2年
    print(f"\n{'='*95}")
    print(f"  📊 期間別 バックテスト結果 (2年)")
    print(f"{'='*95}")
    print(f"  {'期間':<40s} {'最終$':>10s} {'総リターン':>10s} {'月次':>8s} {'DD':>6s}")
    print(f"  {'-'*85}")

    periods_2y = [
        (datetime(2022, 6, 1), datetime(2024, 6, 1), "2022-06〜2024-06"),
        (datetime(2023, 1, 1), datetime(2025, 1, 1), "2023-01〜2025-01"),
        (datetime(2023, 6, 1), datetime(2025, 6, 1), "2023-06〜2025-06"),
        (datetime(2024, 1, 1), datetime(2026, 1, 1), "2024-01〜2026-01"),
        (datetime(2024, 4, 18), datetime(2026, 4, 18), "2024-04〜2026-04 (直近2年)"),
    ]

    results_2y = []
    for start, end, label in periods_2y:
        engine = KellyBacktest(dfs, CONFIG, start, end)
        r = engine.run()
        final = r["final_capital"]
        ret = (final / CONFIG["initial_capital"] - 1) * 100
        months = (end - start).days / 30.0
        monthly = ((final / CONFIG["initial_capital"]) ** (1/months) - 1) * 100 if final > 0 else -100
        results_2y.append({"label": label, "final": final, "total_ret": ret,
                            "monthly": monthly, "dd": r["max_dd"]})
        print(f"  {label:<40s} ${final:>8,.0f}  {ret:+8.1f}%  {monthly:+6.2f}%  {r['max_dd']:5.0f}%")

    # 直近1年の詳細
    print(f"\n{'='*95}")
    print(f"  📋 直近1年 (2025-04〜2026-04) リバランス履歴")
    print(f"{'='*95}")
    start = datetime(2025, 4, 18)
    end = datetime(2026, 4, 18)
    engine = KellyBacktest(dfs, CONFIG, start, end)
    r = engine.run()

    for i, rebal in enumerate(r["rebalances"], 1):
        date_str = rebal["date"].strftime("%Y-%m-%d")
        cap = rebal["capital"]
        print(f"  {i:>2d}. {date_str}  リバランス時残高: ${cap:>8,.2f}")
        for e in rebal["entries"]:
            if e.get("skipped"):
                print(f"       {e['symbol']:<4s} → スキップ (Kelly={e['kelly']:.2f})")
            else:
                print(f"       {e['symbol']:<4s} → Kelly {e['kelly']:.2f}x, 配分${e['alloc']:,.0f}, @${e['entry_price']:,.2f}")

    monthly_final = ((r['final_capital']/CONFIG['initial_capital']) ** (1/12) - 1) * 100 if r['final_capital'] > 0 else -100
    print(f"\n  💰 最終: ${r['final_capital']:,.2f}  月次複利{monthly_final:+.2f}%")

    # 統計
    print(f"\n{'='*95}")
    print(f"  🏆 統計サマリー")
    print(f"{'='*95}")
    print(f"  1年期間 ({len(results_1y)}個):")
    m1 = [r["monthly"] for r in results_1y]
    print(f"    プラス: {sum(1 for m in m1 if m > 0)}/{len(m1)}")
    print(f"    平均月次: {np.mean(m1):+.2f}%  /  中央値: {np.median(m1):+.2f}%")
    print(f"    最高: {np.max(m1):+.2f}%  /  最低: {np.min(m1):+.2f}%")
    print(f"    月+8%以上: {sum(1 for m in m1 if m >= 8)}/{len(m1)}")
    print(f"    月+10%以上: {sum(1 for m in m1 if m >= 10)}/{len(m1)}")

    print(f"\n  2年期間 ({len(results_2y)}個):")
    m2 = [r["monthly"] for r in results_2y]
    print(f"    プラス: {sum(1 for m in m2 if m > 0)}/{len(m2)}")
    print(f"    平均月次: {np.mean(m2):+.2f}%  /  中央値: {np.median(m2):+.2f}%")
    print(f"    最高: {np.max(m2):+.2f}%  /  最低: {np.min(m2):+.2f}%")
    print(f"    月+8%以上: {sum(1 for m in m2 if m >= 8)}/{len(m2)}")
    print(f"    月+10%以上: {sum(1 for m in m2 if m >= 10)}/{len(m2)}")
    print()


if __name__ == "__main__":
    main()
