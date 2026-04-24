"""
benchmark_buy_and_hold.py
=========================
「何もせず持っているだけ」(Buy&Hold) のベンチマーク比較。
取引アルゴリズムの価値を現実的に評価する。
"""

from __future__ import annotations

from pathlib import Path
import sys
import logging
import warnings
from datetime import datetime, timedelta

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config
from data_fetcher import DataFetcher

logging.getLogger("data_fetcher").setLevel(logging.WARNING)

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
WINDOW_DAYS = 30
STEP_DAYS = 15
HISTORY_DAYS = 365


def get_monthly_returns(symbol: str, end_date: datetime) -> list[tuple[str, float]]:
    cfg = Config()
    fetcher = DataFetcher(cfg)
    start = end_date - timedelta(days=HISTORY_DAYS + 5)
    df = fetcher.fetch_historical_ohlcv(symbol, "1d", start.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
    if df.empty:
        return []
    results = []
    cursor = end_date - timedelta(days=HISTORY_DAYS)
    while cursor + timedelta(days=WINDOW_DAYS) <= end_date:
        w_s = cursor
        w_e = cursor + timedelta(days=WINDOW_DAYS)
        df_win = df[(df.index >= w_s) & (df.index <= w_e)]
        if len(df_win) >= 2:
            ret = (df_win["close"].iloc[-1] / df_win["close"].iloc[0] - 1) * 100
            results.append((w_s.strftime("%Y-%m-%d"), ret))
        cursor += timedelta(days=STEP_DAYS)
    return results


def main():
    end_date = datetime(2026, 4, 18)
    all_sym_returns = {}
    for sym in SYMBOLS:
        print(f"取得中: {sym}...")
        all_sym_returns[sym] = get_monthly_returns(sym, end_date)

    # 均等加重ポートフォリオ
    window_avgs = []
    dates = [d for d, _ in all_sym_returns[SYMBOLS[0]]]
    print(f"\n{'='*80}")
    print(f"  📊 Buy&Hold ベンチマーク (過去1年・30日ウィンドウ・均等3通貨)")
    print(f"{'='*80}")
    print(f"  {'期間開始':12s} {'平均リターン':>12s}  {'通貨別':<40s}")
    print(f"  {'-'*70}")
    for i, date in enumerate(dates):
        per_sym = [all_sym_returns[s][i][1] for s in SYMBOLS]
        avg = np.mean(per_sym)
        window_avgs.append(avg)
        sym_str = " ".join(f"{x:+7.1f}" for x in per_sym)
        print(f"  {date} {avg:+9.2f}%    [{sym_str}]")

    returns = np.array(window_avgs)
    print(f"\n{'='*80}")
    print(f"  📈 Buy&Hold 統計")
    print(f"{'='*80}")
    print(f"  平均月次リターン : {np.mean(returns):+.2f}%")
    print(f"  中央値            : {np.median(returns):+.2f}%")
    print(f"  最高              : {np.max(returns):+.2f}%")
    print(f"  最低              : {np.min(returns):+.2f}%")
    print(f"  標準偏差          : {np.std(returns):.2f}%")
    print(f"  プラス月          : {sum(1 for r in returns if r > 0)}/{len(returns)} ({sum(1 for r in returns if r > 0)/len(returns)*100:.0f}%)")
    print(f"  +30%以上          : {sum(1 for r in returns if r >= 30)}/{len(returns)}")
    print(f"  +20%以上          : {sum(1 for r in returns if r >= 20)}/{len(returns)}")
    print(f"  +10%以上          : {sum(1 for r in returns if r >= 10)}/{len(returns)}")
    print(f"  -10%以下          : {sum(1 for r in returns if r <= -10)}/{len(returns)}")

    # 年間単純保有リターン
    print(f"\n{'='*80}")
    print(f"  💰 1年間単純保有した場合")
    print(f"{'='*80}")
    for sym in SYMBOLS:
        cfg = Config()
        fetcher = DataFetcher(cfg)
        start = end_date - timedelta(days=HISTORY_DAYS)
        df = fetcher.fetch_historical_ohlcv(sym, "1d", start.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
        if not df.empty:
            annual = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
            print(f"  {sym:12s}: {annual:+.2f}%")

    # 各戦略との比較
    print(f"\n{'='*80}")
    print(f"  🏆 全戦略比較 (平均月次リターン)")
    print(f"{'='*80}")
    print(f"  Buy&Hold (何もせず) : {np.mean(returns):+.2f}%  勝率 {sum(1 for r in returns if r > 0)/len(returns)*100:.0f}%")
    print(f"  v95.0 (現行)        : -11.92%  勝率 22%")
    print(f"  v3 (Regime戦略)     : -18.86%  勝率  4%")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    main()
