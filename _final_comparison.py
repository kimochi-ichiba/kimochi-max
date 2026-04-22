"""
最終比較: 全戦略を2023・2024の実データで横並び評価
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import run_racsm_backtest, assert_binance_source
from _btc_trend_follow import run_btc_trend
import strategy_racsm

fetcher = DataFetcher(Config())
assert_binance_source(fetcher)


def buy_hold(sym: str, start: str, end: str) -> float:
    df = fetcher.fetch_historical_ohlcv(sym, "1d", start, end)
    return (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100


results = []

for start, end in [("2023-01-01", "2023-12-31"), ("2024-01-01", "2024-12-31")]:
    year = start[:4]
    print(f"\n{'=' * 80}\n🔬 {year}年 戦略比較\n{'=' * 80}")

    # BTC Buy&Hold
    btc_bh = buy_hold("BTC/USDT", start, end)
    print(f"  📌 BTC Buy&Hold               {btc_bh:+7.2f}%")

    # BTC Trend Follow
    r_btc = run_btc_trend(start, end)
    print(f"  🐂 BTC Trend Follow (EMA200)  {r_btc['total_return_pct']:+7.2f}%  "
          f"月平均 {r_btc['monthly_avg']:+.2f}%  勝率 {r_btc['win_rate_pct']}%  "
          f"DD {r_btc['max_dd_pct']:.2f}%")

    # RACSM Majors rb60
    strategy_racsm.UNIVERSE = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT",
                                "XRP/USDT", "DOGE/USDT", "ADA/USDT", "AVAX/USDT"]
    strategy_racsm.TOP_N = 3
    strategy_racsm.LEVERAGE = 1.0
    strategy_racsm.DD_STOP_PCT = 0.15
    import _racsm_backtest as rb
    rb.LEVERAGE = 1.0
    try:
        r_rac = run_racsm_backtest(start, end, rebalance_days=60)
        print(f"  🔷 RACSM Majors rb60         {r_rac['total_return_pct']:+7.2f}%  "
              f"月平均 {r_rac['monthly_avg']:+.2f}%  勝率 {r_rac['win_rate_pct']}%  "
              f"DD {r_rac['max_dd_pct']:.2f}%")
    except Exception as e:
        print(f"  🔷 RACSM Majors rb60         ERROR: {e}")
        r_rac = None

    results.append({
        "year": year, "btc_bh": btc_bh,
        "btc_trend": r_btc,
        "racsm_majors": r_rac,
    })

# 採用判定
print(f"\n{'=' * 80}\n🏆 採用判定\n{'=' * 80}")
bt23 = results[0]["btc_trend"]
bt24 = results[1]["btc_trend"]
diff_month = abs(bt24["monthly_avg"] - bt23["monthly_avg"])

print(f"\n  📊 BTC Trend Follow 成績")
print(f"    2023: 月平均 {bt23['monthly_avg']:+.2f}%  DD {bt23['max_dd_pct']:.1f}%  勝率 {bt23['win_rate_pct']}%")
print(f"    2024: 月平均 {bt24['monthly_avg']:+.2f}%  DD {bt24['max_dd_pct']:.1f}%  勝率 {bt24['win_rate_pct']}%")
print(f"    差: {diff_month:.2f}pp")

print(f"\n  🎯 成功基準チェック")
checks = [
    ("月平均 ≥ +2%", min(bt23["monthly_avg"], bt24["monthly_avg"]) >= 2.0,
     f"最小 {min(bt23['monthly_avg'], bt24['monthly_avg']):+.2f}%"),
    ("最大DD ≤ 15%", max(bt23["max_dd_pct"], bt24["max_dd_pct"]) <= 15.0,
     f"最大 {max(bt23['max_dd_pct'], bt24['max_dd_pct']):.2f}%"),
    ("BTC B&H 超過", min(bt23["total_return_pct"]-results[0]["btc_bh"],
                         bt24["total_return_pct"]-results[1]["btc_bh"]) > 0,
     f"'23 {bt23['total_return_pct']-results[0]['btc_bh']:+.1f}pp / "
     f"'24 {bt24['total_return_pct']-results[1]['btc_bh']:+.1f}pp"),
    ("年次劣化 ≤ 1.5pp", diff_month <= 1.5, f"差 {diff_month:.2f}pp"),
    ("両年プラス", bt23["total_return_pct"] > 0 and bt24["total_return_pct"] > 0,
     f"'23 {bt23['total_return_pct']:+.1f}% / '24 {bt24['total_return_pct']:+.1f}%"),
]
ok = 0
for label, passed, detail in checks:
    mark = "✅" if passed else "❌"
    print(f"    {mark} {label}  [{detail}]")
    if passed: ok += 1

print(f"\n  達成: {ok}/{len(checks)}")
if ok == len(checks):
    print("  🎉 全基準達成 — 採用推奨")
elif ok >= 3:
    print("  ⚖️  部分達成 — BTC B&H超過は難しいが、安定性と低DDで実用価値あり")
else:
    print("  ⚠️  基準未達 — 不採用を推奨")

# 結果保存
out = (Path(__file__).resolve().parent / "results" / "final_comparison.json")
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
print(f"\n💾 保存: {out}")
