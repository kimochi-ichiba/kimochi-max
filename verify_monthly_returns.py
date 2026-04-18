"""
verify_monthly_returns.py
=========================
v95.0戦略の月次リターン再現性検証

目的: 「月+30%」が本当に出るのか、過去12ヶ月を月単位でローリング検証
方法:
  1. 過去12ヶ月のデータを取得
  2. 30日ずつのウィンドウをずらしながら(5日刻み)バックテスト
  3. 各ウィンドウのリターン分布を集計
  4. +30%がどれだけ頻繁に出現するかを確認
"""

from __future__ import annotations

import sys
import logging
import warnings
from datetime import datetime, timedelta

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, "/Users/sanosano/projects/crypto-bot-pro")

from config import Config
from backtester import Backtester

logging.getLogger("data_fetcher").setLevel(logging.WARNING)
logging.getLogger("backtester").setLevel(logging.WARNING)

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]  # 3通貨で高速化
TIMEFRAME = "1h"
INITIAL_BALANCE = 10_000.0
WINDOW_DAYS = 30        # 1ヶ月ウィンドウ
STEP_DAYS = 15          # 15日刻みでシフト
HISTORY_DAYS = 365      # 検証対象の過去データ範囲


def run_window(symbol: str, start: str, end: str) -> dict:
    cfg = Config()
    bt = Backtester(cfg)
    r = bt.run(symbol, start, end, timeframe=TIMEFRAME, initial_balance=INITIAL_BALANCE)
    if not r.trades:
        return {"trades": 0, "return": 0.0, "win_rate": 0.0}
    wins = [t for t in r.trades if t.won]
    return {
        "trades": len(r.trades),
        "return": (r.final / r.initial - 1) * 100,
        "win_rate": len(wins) / len(r.trades) * 100,
    }


def run_portfolio_window(start: str, end: str) -> dict:
    """5通貨ポートフォリオの1ウィンドウ結果"""
    rets = []
    total_trades = 0
    for sym in SYMBOLS:
        r = run_window(sym, start, end)
        rets.append(r["return"])
        total_trades += r["trades"]
    avg_return = np.mean(rets)
    return {
        "start": start,
        "end": end,
        "avg_return": avg_return,
        "total_trades": total_trades,
        "per_symbol": rets,
    }


def main():
    end_date = datetime(2026, 4, 18)
    windows = []
    # ウィンドウを生成: 過去365日を30日ウィンドウ×15日刻み
    start_from = end_date - timedelta(days=HISTORY_DAYS)
    cursor = start_from
    while cursor + timedelta(days=WINDOW_DAYS) <= end_date:
        w_start = cursor
        w_end = cursor + timedelta(days=WINDOW_DAYS)
        windows.append((w_start.strftime("%Y-%m-%d"), w_end.strftime("%Y-%m-%d")))
        cursor += timedelta(days=STEP_DAYS)

    print(f"\n🔍 v95.0月次リターン再現性検証")
    print(f"検証期間: {start_from.strftime('%Y-%m-%d')} 〜 {end_date.strftime('%Y-%m-%d')}")
    print(f"ウィンドウ数: {len(windows)} (30日ウィンドウ × 15日刻み)")
    print(f"対象通貨: {', '.join(SYMBOLS)}")
    print(f"{'='*78}")
    print(f"  {'期間':28s} {'平均リターン':>12s} {'取引数':>7s}  {'通貨別リターン':<30s}")
    print(f"  {'-'*78}")

    results = []
    for start, end in windows:
        r = run_portfolio_window(start, end)
        results.append(r)
        per_sym_str = " ".join(f"{x:+5.1f}" for x in r["per_symbol"])
        print(f"  {start} 〜 {end}  {r['avg_return']:+10.2f}% {r['total_trades']:5d}   [{per_sym_str}]")

    # 集計
    returns = [r["avg_return"] for r in results]
    print(f"\n{'='*78}")
    print(f"  📊 集計統計")
    print(f"{'='*78}")
    print(f"  総ウィンドウ数        : {len(returns)}")
    print(f"  平均月次リターン      : {np.mean(returns):+.2f}%")
    print(f"  中央値                : {np.median(returns):+.2f}%")
    print(f"  最高                  : {np.max(returns):+.2f}%")
    print(f"  最低                  : {np.min(returns):+.2f}%")
    print(f"  標準偏差              : {np.std(returns):.2f}%")
    print(f"  プラスウィンドウ数    : {sum(1 for r in returns if r > 0)} / {len(returns)} ({sum(1 for r in returns if r > 0)/len(returns)*100:.0f}%)")
    print(f"  +30%以上の月          : {sum(1 for r in returns if r >= 30)} / {len(returns)} ({sum(1 for r in returns if r >= 30)/len(returns)*100:.0f}%)")
    print(f"  +20%以上の月          : {sum(1 for r in returns if r >= 20)} / {len(returns)} ({sum(1 for r in returns if r >= 20)/len(returns)*100:.0f}%)")
    print(f"  +10%以上の月          : {sum(1 for r in returns if r >= 10)} / {len(returns)} ({sum(1 for r in returns if r >= 10)/len(returns)*100:.0f}%)")
    print(f"  -10%以下の月          : {sum(1 for r in returns if r <= -10)} / {len(returns)} ({sum(1 for r in returns if r <= -10)/len(returns)*100:.0f}%)")

    # 判定
    avg = np.mean(returns)
    positive_rate = sum(1 for r in returns if r > 0) / len(returns) * 100
    print(f"\n  {'='*60}")
    if avg >= 20 and positive_rate >= 60:
        print(f"  ✅ 判定: 「月+30%」は再現性あり。平均{avg:+.1f}%、勝率{positive_rate:.0f}%")
    elif avg >= 5 and positive_rate >= 50:
        print(f"  ⚠️ 判定: 利益は出るが「月+30%」ほどではない。平均{avg:+.1f}%")
    else:
        print(f"  ❌ 判定: 「月+30%」は再現性なし。平均{avg:+.1f}%、勝率{positive_rate:.0f}%")
        print(f"        → 過去の「月+30%」は偶然のカーブフィッティングだった可能性大")
    print(f"  {'='*60}\n")


if __name__ == "__main__":
    main()
