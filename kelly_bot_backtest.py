"""
kelly_bot_backtest.py
=====================
kelly_bot.py の実装を**忠実に再現**した最終バックテスト

ボット側の実装と完全に同じロジックでシミュレーション:
1. 30日ごとにリバランス
2. Kelly推奨レバを計算 (lookback 60日, fraction 0.5, max 10x)
3. 既存ポジション全決済 → 新レバでエントリー
4. BNB 70% + BTC 30% 配分

これでボットが実際にどう動くか確認。
"""

from __future__ import annotations

import sys
import logging
import warnings
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import ccxt

warnings.filterwarnings("ignore")
sys.path.insert(0, "/Users/sanosano/projects/crypto-bot-pro")
logging.getLogger().setLevel(logging.WARNING)

# ボット設定と同じ
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


def compute_kelly_leverage(df_hist: pd.DataFrame, lookback: int, fraction: float, max_lev: float) -> float:
    """ボットと同じKelly計算"""
    returns = df_hist["close"].pct_change().dropna()
    if len(returns) < lookback: return 0.0
    recent = returns.tail(lookback)
    mean_ann = recent.mean() * 365
    var_ann = recent.var() * 365
    if var_ann <= 0 or mean_ann <= 0: return 0.0
    kelly = (mean_ann / var_ann) * fraction
    return float(np.clip(kelly, 0, max_lev))


@dataclass
class BtPosition:
    symbol: str
    entry_price: float
    size: float
    leverage: float
    entry_date: pd.Timestamp
    initial_margin: float
    liquidated: bool = False


class BacktestEngine:
    """ボット動作を忠実再現するバックテストエンジン"""

    def __init__(self, dfs: Dict[str, pd.DataFrame], config: dict, start_date: datetime, end_date: datetime):
        self.dfs = dfs
        self.cfg = config
        self.start_date = start_date
        self.end_date = end_date
        self.capital = config["initial_capital"]
        self.positions: Dict[str, BtPosition] = {}
        self.last_rebalance: Optional[pd.Timestamp] = None
        self.equity_curve = []
        self.trades = []
        self.rebalance_events = []

    def daily_check(self, ts: pd.Timestamp):
        """毎日の処理: 清算チェック + 必要ならリバランス"""
        # 清算チェック
        to_close = []
        for sym, pos in self.positions.items():
            if sym not in self.dfs or ts not in self.dfs[sym].index:
                continue
            row = self.dfs[sym].loc[ts]
            low = row["low"]
            current_equity = pos.initial_margin + (low - pos.entry_price) * pos.size * pos.leverage
            mm = low * pos.size * self.cfg["mmr"]
            if current_equity <= mm:
                # 清算発生
                pos.liquidated = True
                loss = pos.initial_margin
                self.capital -= 0  # already zero
                self.trades.append({
                    "date": ts, "symbol": sym, "action": "liquidation",
                    "entry": pos.entry_price, "exit": low, "pnl": -loss,
                })
                to_close.append(sym)

        for sym in to_close:
            del self.positions[sym]

        # リバランス判定
        if self.last_rebalance is None:
            should_rebal = True
        else:
            days_since = (ts - self.last_rebalance).days
            should_rebal = days_since >= self.cfg["rebalance_days"]

        if should_rebal:
            self.rebalance(ts)

    def close_all_positions(self, ts: pd.Timestamp):
        total_pnl = 0
        for sym, pos in list(self.positions.items()):
            if sym not in self.dfs or ts not in self.dfs[sym].index:
                continue
            price = self.dfs[sym].loc[ts]["close"]
            exit_price = price * (1 - self.cfg["slippage"])
            gross = (exit_price - pos.entry_price) * pos.size * pos.leverage
            fee = exit_price * pos.size * self.cfg["fee_rate"]
            pnl = gross - fee
            total_pnl += pnl
            self.trades.append({
                "date": ts, "symbol": sym, "action": "close",
                "entry": pos.entry_price, "exit": exit_price, "pnl": pnl,
                "leverage": pos.leverage,
            })
            del self.positions[sym]
        return total_pnl

    def rebalance(self, ts: pd.Timestamp):
        # 決済
        pnl = self.close_all_positions(ts)
        self.capital += pnl

        rebal_event = {"date": ts, "capital_after_close": self.capital, "entries": []}

        # 新規エントリー
        for sym_short, weight in self.cfg["allocations"].items():
            sym = sym_short  # "BNB" or "BTC"
            if sym not in self.dfs: continue

            # 過去データでKelly計算 (look-ahead bias回避 - ts直前のデータ使用)
            df_hist = self.dfs[sym][self.dfs[sym].index < ts].tail(self.cfg["lookback_days"] + 30)
            kelly_lev = compute_kelly_leverage(df_hist, self.cfg["lookback_days"],
                                                 self.cfg["kelly_fraction"], self.cfg["max_leverage"])

            if kelly_lev < self.cfg["min_leverage_threshold"]:
                rebal_event["entries"].append({"symbol": sym, "skipped": True, "reason": f"lev={kelly_lev:.2f}"})
                continue

            if ts not in self.dfs[sym].index: continue
            current_price = self.dfs[sym].loc[ts]["close"]
            entry_price = current_price * (1 + self.cfg["slippage"])
            alloc_cash = self.capital * weight
            notional = alloc_cash * kelly_lev
            size = notional / entry_price
            fee = notional * self.cfg["fee_rate"]
            initial_margin = alloc_cash - fee

            self.positions[sym] = BtPosition(
                symbol=sym, entry_price=entry_price, size=size,
                leverage=kelly_lev, entry_date=ts, initial_margin=initial_margin,
            )
            self.capital -= initial_margin  # マージンは拘束
            self.trades.append({
                "date": ts, "symbol": sym, "action": "open",
                "entry": entry_price, "size": size, "leverage": kelly_lev,
                "alloc": alloc_cash,
            })
            rebal_event["entries"].append({
                "symbol": sym, "kelly_lev": kelly_lev, "alloc": alloc_cash,
                "entry_price": entry_price,
            })

        self.last_rebalance = ts
        self.rebalance_events.append(rebal_event)

    def run(self):
        all_dates = sorted(set().union(*[set(df.index) for df in self.dfs.values()]))
        all_dates = [d for d in all_dates if self.start_date <= d.to_pydatetime() <= self.end_date]

        peak = self.cfg["initial_capital"]
        max_dd = 0

        for ts in all_dates:
            self.daily_check(ts)

            # equity計算
            total_eq = self.capital
            for sym, pos in self.positions.items():
                if sym in self.dfs and ts in self.dfs[sym].index:
                    price = self.dfs[sym].loc[ts]["close"]
                    total_eq += pos.initial_margin + (price - pos.entry_price) * pos.size * pos.leverage

            self.equity_curve.append({"date": ts, "equity": total_eq})
            if total_eq > peak: peak = total_eq
            if peak > 0:
                dd = (peak - total_eq) / peak * 100
                max_dd = max(max_dd, dd)

        # 最終決済
        if all_dates and self.positions:
            final_ts = all_dates[-1]
            pnl = self.close_all_positions(final_ts)
            self.capital += pnl

        return {
            "final_capital": self.capital,
            "equity_curve": self.equity_curve,
            "trades": self.trades,
            "rebalances": self.rebalance_events,
            "max_dd": max_dd,
        }


def main():
    print(f"\n🤖 Kelly Bot 最終バックテスト (ボット動作を忠実再現)")
    print(f"{'='*90}")
    print(f"戦略: BNB 70% + BTC 30% Kelly 0.5x lb60 max10x rebal30d")
    print(f"初期資金: ${CONFIG['initial_capital']:,.0f}\n")

    # データ取得
    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
    since_ms = int(datetime(2021, 6, 1).timestamp() * 1000)
    until_ms = int(datetime(2026, 4, 18).timestamp() * 1000)

    dfs = {}
    for name, sym in [("BNB","BNB/USDT:USDT"), ("BTC","BTC/USDT:USDT")]:
        print(f"📥 {name}...")
        df = fetch_ohlcv(ex, sym, since_ms, until_ms)
        if not df.empty:
            dfs[name] = df
    print(f"✅ {len(dfs)}通貨取得\n")

    # 複数期間で検証
    test_periods = [
        # (start, end, label)
        (datetime(2022, 6, 1), datetime(2023, 6, 1), "2022-06〜2023-06 (ベア)"),
        (datetime(2023, 1, 1), datetime(2024, 1, 1), "2023-01〜2024-01 (回復)"),
        (datetime(2023, 6, 1), datetime(2024, 6, 1), "2023-06〜2024-06 (ブル転換)"),
        (datetime(2024, 1, 1), datetime(2025, 1, 1), "2024-01〜2025-01 (ブル続行)"),
        (datetime(2024, 6, 1), datetime(2025, 6, 1), "2024-06〜2025-06 (調整期)"),
        (datetime(2025, 1, 1), datetime(2026, 1, 1), "2025-01〜2026-01 (最近)"),
        (datetime(2025, 4, 18), datetime(2026, 4, 18), "2025-04〜2026-04 (直近1年)"),
    ]

    print(f"{'='*90}")
    print(f"  📊 期間別 バックテスト結果")
    print(f"{'='*90}")
    print(f"  {'期間':<35s} {'最終$':>10s} {'総リターン':>10s} {'月次':>8s} {'DD':>6s}")
    print(f"  {'-'*82}")

    all_results = []
    for start, end, label in test_periods:
        engine = BacktestEngine(dfs, CONFIG, start, end)
        result = engine.run()
        final = result["final_capital"]
        total_ret = (final / CONFIG["initial_capital"] - 1) * 100
        n_months = (end - start).days / 30.0
        monthly = ((final / CONFIG["initial_capital"]) ** (1/n_months) - 1) * 100 if final > 0 else -100
        all_results.append({
            "label": label, "final": final, "total_ret": total_ret,
            "monthly": monthly, "dd": result["max_dd"], "n_trades": len(result["trades"]),
            "n_rebalances": len(result["rebalances"]),
        })
        print(f"  {label:<35s} ${final:>8,.0f}  {total_ret:+8.1f}%  {monthly:+6.2f}%  {result['max_dd']:5.0f}%")

    # 2年ウィンドウ
    print(f"\n{'='*90}")
    print(f"  📊 2年運用 バックテスト")
    print(f"{'='*90}")
    print(f"  {'期間':<35s} {'最終$':>10s} {'総リターン':>10s} {'月次':>8s} {'DD':>6s}")
    print(f"  {'-'*82}")

    two_year_periods = [
        (datetime(2022, 6, 1), datetime(2024, 6, 1), "2022-06〜2024-06"),
        (datetime(2023, 1, 1), datetime(2025, 1, 1), "2023-01〜2025-01"),
        (datetime(2023, 6, 1), datetime(2025, 6, 1), "2023-06〜2025-06"),
        (datetime(2024, 1, 1), datetime(2026, 1, 1), "2024-01〜2026-01"),
        (datetime(2024, 4, 18), datetime(2026, 4, 18), "2024-04〜2026-04 (直近2年)"),
    ]

    for start, end, label in two_year_periods:
        engine = BacktestEngine(dfs, CONFIG, start, end)
        result = engine.run()
        final = result["final_capital"]
        total_ret = (final / CONFIG["initial_capital"] - 1) * 100
        n_months = (end - start).days / 30.0
        monthly = ((final / CONFIG["initial_capital"]) ** (1/n_months) - 1) * 100 if final > 0 else -100
        print(f"  {label:<35s} ${final:>8,.0f}  {total_ret:+8.1f}%  {monthly:+6.2f}%  {result['max_dd']:5.0f}%")

    # 直近1年の詳細を表示
    print(f"\n{'='*90}")
    print(f"  📋 直近1年 (2025-04〜2026-04) リバランス履歴")
    print(f"{'='*90}")
    start = datetime(2025, 4, 18)
    end = datetime(2026, 4, 18)
    engine = BacktestEngine(dfs, CONFIG, start, end)
    result = engine.run()

    for i, rebal in enumerate(result["rebalances"], 1):
        date_str = rebal["date"].strftime("%Y-%m-%d")
        cap = rebal["capital_after_close"]
        print(f"  {i:>2d}. {date_str}  残高${cap:>8,.0f}")
        for entry in rebal["entries"]:
            if entry.get("skipped"):
                print(f"       {entry['symbol']:<5s} → スキップ ({entry['reason']})")
            else:
                print(f"       {entry['symbol']:<5s} → Kelly {entry['kelly_lev']:.2f}x, 配分${entry['alloc']:,.0f}, @${entry['entry_price']:,.2f}")

    print(f"\n  💰 最終資金: ${result['final_capital']:,.2f}  (リバランス{len(result['rebalances'])}回, 取引{len(result['trades'])}回)")
    monthly_final = ((result['final_capital']/CONFIG['initial_capital']) ** (1/12) - 1) * 100 if result['final_capital'] > 0 else -100
    print(f"  📈 月次複利: {monthly_final:+.2f}%")

    # 統計サマリー
    print(f"\n{'='*90}")
    print(f"  🏆 全7期間(1年)の統計")
    print(f"{'='*90}")
    monthlies = [r["monthly"] for r in all_results]
    positive = sum(1 for m in monthlies if m > 0)
    print(f"  プラス期間: {positive}/{len(all_results)} ({positive/len(all_results)*100:.0f}%)")
    print(f"  平均月次: {np.mean(monthlies):+.2f}%")
    print(f"  中央値: {np.median(monthlies):+.2f}%")
    print(f"  最高: {np.max(monthlies):+.2f}%")
    print(f"  最低: {np.min(monthlies):+.2f}%")
    print(f"  目標(月+8〜15%)に入った期間: {sum(1 for m in monthlies if 8 <= m <= 15)}/{len(all_results)}")
    print(f"  月+10%超えた期間: {sum(1 for m in monthlies if m >= 10)}/{len(all_results)}")

    print()


if __name__ == "__main__":
    main()
