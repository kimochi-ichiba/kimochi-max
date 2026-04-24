"""「素早く版」DL (ADX>20=1x, 25=2x, 30=3x) を5年検証で最終確認"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import Config
from data_fetcher import DataFetcher
from _dynamic_lev_max2x import dynamic_leverage_custom
from _racsm_backtest import assert_binance_source

fetcher = DataFetcher(Config())
assert_binance_source(fetcher)

FAST_LEVELS     = [(20, 1.0), (25, 2.0), (30, 3.0)]
AGGRESSIVE      = [(20, 1.5), (30, 2.5), (40, 3.5)]
STANDARD_LEVELS = [(20, 1.0), (30, 2.0), (40, 3.0)]

configs = [
    ("標準 (20/30/40)",    STANDARD_LEVELS),
    ("素早く (20/25/30)",   FAST_LEVELS),
    ("アグレッシブ (20/30/40 @ 1.5/2.5/3.5)", AGGRESSIVE),
]

periods = [
    ("2020-01-01", "2020-12-31", "2020 ブルラン"),
    ("2021-01-01", "2021-12-31", "2021 頂点"),
    ("2022-01-01", "2022-12-31", "2022 ベア市場"),
    ("2023-01-01", "2023-12-31", "2023 回復"),
    ("2024-01-01", "2024-12-31", "2024 新高値"),
    ("2020-01-01", "2024-12-31", "5年通期"),
]

print(f"\n{'=' * 110}")
print(f"🔬 DL 3バリエーション × 5年 + 1年ごと ストレステスト")
print(f"{'=' * 110}")
print(f"{'期間':30s} | {'標準 月平均/DD':20s} | {'素早く 月平均/DD':20s} | {'アグレッシブ 月平均/DD':25s}")
print("-" * 110)

for s, e, label in periods:
    row = [label + f" {s[:4]}-{e[:4]}"]
    for name, levels in configs:
        try:
            r = dynamic_leverage_custom(fetcher, s, e, levels)
            row.append(f"{r['monthly_avg']:+6.2f}%/{r['max_dd_pct']:>5.1f}% ({r['total_return_pct']:+5.1f}%)")
        except Exception as ex:
            row.append(f"ERROR {ex}")
    print(f"{row[0]:30s} | {row[1]:20s} | {row[2]:20s} | {row[3]:25s}")
