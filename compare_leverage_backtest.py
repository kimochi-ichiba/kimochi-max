"""
compare_leverage_backtest.py
============================
策①検証: v95.0戦略のレバレッジ3倍(現状) vs 4倍(強化版) 比較

使い方: python3 compare_leverage_backtest.py
既存 Backtester クラスを流用し、config.min_leverage/max_leverage のみ変更して比較する。
"""

from __future__ import annotations

from pathlib import Path
import sys
import logging
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config
from backtester import Backtester

# 余計なログを抑制して結果を読みやすく
logging.getLogger("data_fetcher").setLevel(logging.WARNING)
logging.getLogger("backtester").setLevel(logging.WARNING)

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
DAYS = 180
TIMEFRAME = "1h"
INITIAL_BALANCE = 10_000.0


def build_config(min_lev: float, max_lev: float) -> Config:
    cfg = Config()
    cfg.min_leverage = min_lev
    cfg.max_leverage = max_lev
    return cfg


def run_single(symbol: str, start: str, end: str, min_lev: float, max_lev: float) -> dict:
    cfg = build_config(min_lev, max_lev)
    bt = Backtester(cfg)
    result = bt.run(symbol, start, end, timeframe=TIMEFRAME, initial_balance=INITIAL_BALANCE)
    if not result.trades:
        return {"symbol": symbol, "trades": 0, "final": result.initial, "pnl_pct": 0.0,
                "win_rate": 0.0, "max_dd": 0.0, "pf": 0.0}

    wins = [t for t in result.trades if t.won]
    pnl_pct = (result.final / result.initial - 1) * 100
    win_rate = len(wins) / len(result.trades) * 100

    # プロフィットファクター
    total_win = sum(t.pnl for t in wins)
    total_loss = abs(sum(t.pnl for t in result.trades if not t.won))
    pf = total_win / total_loss if total_loss > 0 else float("inf")

    # Max DD
    peak = result.equity_curve[0] if result.equity_curve else result.initial
    max_dd = 0.0
    for v in result.equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    return {
        "symbol": symbol,
        "trades": len(result.trades),
        "final": result.final,
        "pnl_pct": pnl_pct,
        "win_rate": win_rate,
        "max_dd": max_dd,
        "pf": pf,
    }


def run_portfolio(min_lev: float, max_lev: float, label: str, start: str, end: str) -> dict:
    """全銘柄を独立にバックテストし、ポートフォリオ結果を集計"""
    print(f"\n{'='*70}")
    print(f"  {label}  (min_lev={min_lev}, max_lev={max_lev})")
    print(f"{'='*70}")
    results = []
    for sym in SYMBOLS:
        r = run_single(sym, start, end, min_lev, max_lev)
        results.append(r)
        print(f"  {sym:12s}: 取引{r['trades']:3d}回 "
              f"勝率{r['win_rate']:5.1f}% "
              f"PF{r['pf']:5.2f} "
              f"DD{r['max_dd']:5.1f}% "
              f"リターン{r['pnl_pct']:+7.2f}%")

    total_pnl = sum(r["final"] - INITIAL_BALANCE for r in results)
    total_initial = INITIAL_BALANCE * len(SYMBOLS)
    port_return = total_pnl / total_initial * 100
    total_trades = sum(r["trades"] for r in results)
    avg_win_rate = sum(r["win_rate"] for r in results) / len(results)
    avg_pf = sum(r["pf"] for r in results if r["pf"] != float("inf")) / len(results)
    avg_dd = sum(r["max_dd"] for r in results) / len(results)

    months = DAYS / 30.0
    monthly = ((1 + port_return / 100) ** (1 / months) - 1) * 100 if port_return > -100 else -100

    print(f"  {'-'*66}")
    print(f"  ポートフォリオ合計:")
    print(f"    総リターン(6ヶ月): {port_return:+.2f}%")
    print(f"    月次平均(複利)   : {monthly:+.2f}%")
    print(f"    平均勝率         : {avg_win_rate:.1f}%")
    print(f"    平均PF           : {avg_pf:.2f}")
    print(f"    平均最大DD       : {avg_dd:.1f}%")
    print(f"    総取引回数       : {total_trades}")

    return {
        "label": label,
        "port_return": port_return,
        "monthly": monthly,
        "avg_win_rate": avg_win_rate,
        "avg_pf": avg_pf,
        "avg_dd": avg_dd,
        "total_trades": total_trades,
    }


def main():
    end_date = datetime(2026, 4, 18)
    start_date = end_date - timedelta(days=DAYS)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    print(f"\n📊 策①検証: v95.0 レバレッジ比較バックテスト")
    print(f"期間: {start_str} 〜 {end_str} ({DAYS}日)")
    print(f"銘柄: {', '.join(SYMBOLS)}")
    print(f"各銘柄に独立して${INITIAL_BALANCE:,.0f}を割り当て")

    baseline = run_portfolio(3.0, 5.0, "ベースライン (v95.0 現行: 3~5倍)", start_str, end_str)
    boosted = run_portfolio(4.0, 5.0, "策①強化版 (4~5倍)", start_str, end_str)
    aggressive = run_portfolio(4.0, 4.0, "策①固定4倍版 (常に4倍)", start_str, end_str)

    # 最終比較表
    print(f"\n{'='*70}")
    print(f"  📋 最終比較サマリー")
    print(f"{'='*70}")
    print(f"  {'戦略':<35s} {'月次':>8s} {'平均DD':>8s} {'PF':>6s} {'勝率':>7s}")
    print(f"  {'-'*70}")
    for res in [baseline, boosted, aggressive]:
        print(f"  {res['label']:<35s} "
              f"{res['monthly']:+7.2f}% "
              f"{res['avg_dd']:6.1f}% "
              f"{res['avg_pf']:5.2f} "
              f"{res['avg_win_rate']:5.1f}%")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
