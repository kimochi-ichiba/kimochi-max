"""
verify_racsm_rolling.py — RACSMの月次ローリング検証
ベンチマーク (Buy&Hold BTC/ETH) と比較。
"""
from __future__ import annotations

import json
import argparse
import logging
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd

from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import run_racsm_backtest, assert_binance_source

logging.getLogger("data_fetcher").setLevel(logging.WARNING)


def buy_hold_return(fetcher: DataFetcher, symbol: str,
                     start: str, end: str) -> float:
    """Buy&Holdのリターン (%)"""
    df = fetcher.fetch_historical_ohlcv(symbol, "1d", start, end)
    if df.empty:
        return 0.0
    return (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100


def run_year_verification(year: int, rebalance_days: int,
                           out_dir: Path) -> dict:
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)

    print(f"\n{'═' * 70}")
    print(f"🧪 RACSM 年次検証: {year}年  (リバランス{rebalance_days}日毎)")
    print(f"{'═' * 70}")

    # 通年通して実行
    start = f"{year}-01-01"
    end   = f"{year}-12-31"
    result = run_racsm_backtest(start, end, rebalance_days=rebalance_days)

    # ベンチマーク: Buy&Hold
    btc_ret = buy_hold_return(fetcher, "BTC/USDT", start, end)
    eth_ret = buy_hold_return(fetcher, "ETH/USDT", start, end)

    print(f"\n📊 {year}年 最終結果")
    print(f"  RACSM:        {result['total_return_pct']:+7.2f}%  "
          f"月平均{result['monthly_avg']:+.2f}%  勝率{result['win_rate_pct']}%  "
          f"DD{result['max_dd_pct']:.1f}%")
    print(f"  BTC Buy&Hold: {btc_ret:+7.2f}%")
    print(f"  ETH Buy&Hold: {eth_ret:+7.2f}%")

    # 月別テーブル
    if result["monthly_returns"]:
        print(f"\n  📅 月別リターン (%)")
        for k, v in result["monthly_returns"].items():
            marker = "📈" if v > 0 else "📉"
            print(f"    {k}  {marker}  {v:+6.2f}%")

    combined = {
        "year": year,
        "rebalance_days": rebalance_days,
        "racsm": result,
        "benchmarks": {
            "BTC_buy_hold": round(btc_ret, 2),
            "ETH_buy_hold": round(eth_ret, 2),
        },
    }

    out_path = out_dir / f"racsm_{year}_rb{rebalance_days}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(combined, indent=2, ensure_ascii=False))
    print(f"\n💾 保存: {out_path}")
    return combined


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2024,2023",
                    help="検証対象年(カンマ区切り)")
    ap.add_argument("--rebalance", default="7,30",
                    help="リバランス間隔(カンマ区切り)")
    args = ap.parse_args()

    years = [int(y) for y in args.years.split(",")]
    rbs   = [int(r) for r in args.rebalance.split(",")]

    out_dir = Path("/Users/sanosano/projects/kimochi-max/results")
    all_results = []
    for year in years:
        for rb in rbs:
            res = run_year_verification(year, rb, out_dir)
            all_results.append(res)

    # 総合比較表
    print(f"\n\n{'═' * 80}")
    print(f"🏆 総合比較表")
    print(f"{'═' * 80}")
    print(f"{'戦略':25s} | {'年次':>8s} | {'月平均':>7s} | {'勝率':>6s} | {'DD':>6s}")
    print(f"{'-' * 80}")
    for r in all_results:
        label = f"RACSM {r['year']} (rb{r['rebalance_days']})"
        racsm = r["racsm"]
        print(f"{label:25s} | {racsm['total_return_pct']:+7.2f}% | "
              f"{racsm['monthly_avg']:+6.2f}% | {racsm['win_rate_pct']:>5.1f}% | "
              f"{racsm['max_dd_pct']:>5.1f}%")
        print(f"{'  └ BTC Buy&Hold':25s} | {r['benchmarks']['BTC_buy_hold']:+7.2f}% | "
              f"{'':>7s} | {'':>6s} | {'':>6s}")
        print(f"{'  └ ETH Buy&Hold':25s} | {r['benchmarks']['ETH_buy_hold']:+7.2f}% | "
              f"{'':>7s} | {'':>6s} | {'':>6s}")
        print(f"{'-' * 80}")

    # 成功基準チェック
    print("\n🎯 成功基準チェック（主評価=2024年 rb7）")
    main_result = next(
        (r for r in all_results if r["year"] == 2024 and r["rebalance_days"] == 7),
        None
    )
    if main_result:
        m = main_result["racsm"]
        btc = main_result["benchmarks"]["BTC_buy_hold"]
        checks = [
            ("月平均 ≥ +2%", m["monthly_avg"] >= 2.0,
             f"実績 {m['monthly_avg']:+.2f}%"),
            ("最大DD ≤ 15%", m["max_dd_pct"] <= 15.0,
             f"実績 {m['max_dd_pct']:.2f}%"),
            ("BTC Buy&Hold超過", m["total_return_pct"] > btc,
             f"RACSM {m['total_return_pct']:+.2f}% vs BTC {btc:+.2f}%"),
        ]
        all_pass = True
        for label, ok, detail in checks:
            mark = "✅" if ok else "❌"
            print(f"  {mark} {label}  [{detail}]")
            if not ok:
                all_pass = False

        if all_pass:
            print("\n🎉 全基準クリア — 採用候補")
        else:
            print("\n⚠️ 基準未達 — 正直に不採用を推奨")


if __name__ == "__main__":
    main()
