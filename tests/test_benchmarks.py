"""benchmarks.py の単体テスト."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks import (
    BenchmarkResult,
    buy_hold_benchmark,
    monthly_dca_benchmark,
    random_entry_benchmark,
    trend_follow_benchmark,
)


@pytest.fixture
def synthetic_data() -> dict[str, pd.DataFrame]:
    """合成 OHLC データ: 2023-2024 の 500 日、上昇 → 下降 → 上昇パターン."""
    dates = pd.date_range("2023-01-01", periods=500, freq="D")
    # BTC: 中央でピーク、その後底、最後に回復
    t = np.linspace(0, 4 * np.pi, 500)
    base = 20000 + 5000 * np.sin(t / 2) + np.linspace(0, 5000, 500)
    btc = pd.DataFrame(
        {
            "open": base,
            "high": base * 1.02,
            "low": base * 0.98,
            "close": base,
            "volume": np.ones(500) * 1000,
        },
        index=dates,
    )
    btc["ema200"] = btc["close"].ewm(span=200, adjust=False).mean()

    # ACH 銘柄を 3 つ (相関を変える)
    symbols = {}
    symbols["BTC/USDT"] = btc
    for i, sym in enumerate(["ETH/USDT", "SOL/USDT", "BNB/USDT"]):
        shift = i * 0.3
        price = base * (1 + shift) + np.random.default_rng(seed=i).normal(0, 100, 500)
        symbols[sym] = pd.DataFrame(
            {
                "open": price,
                "high": price * 1.02,
                "low": price * 0.98,
                "close": price,
                "volume": np.ones(500) * 500,
            },
            index=dates,
        )
    return symbols


def test_buy_hold_returns_equity_curve(synthetic_data):
    r = buy_hold_benchmark(
        synthetic_data, "BTC/USDT", "2023-01-01", "2024-05-15", 10_000
    )
    assert isinstance(r, BenchmarkResult)
    assert len(r.equity_curve) > 400
    assert r.equity_curve[0]["equity"] > 0
    assert len(r.trades) == 1


def test_monthly_dca_makes_monthly_trades(synthetic_data):
    r = monthly_dca_benchmark(
        synthetic_data, "BTC/USDT", "2023-01-01", "2024-05-15", 10_000
    )
    # 約 17 ヶ月で 15+ 回 DCA
    assert len(r.trades) >= 15
    assert all(t.get("entry_ts") is not None for t in r.trades)


def test_trend_follow_may_trade_zero_or_more(synthetic_data):
    r = trend_follow_benchmark(
        synthetic_data, "BTC/USDT", "2023-01-01", "2024-05-15", 10_000
    )
    # EMA200 上下変動があるので trade は発生しうる
    assert isinstance(r.trades, list)
    assert len(r.equity_curve) > 0


def test_random_entry_is_reproducible(synthetic_data):
    universe = ["ETH/USDT", "SOL/USDT", "BNB/USDT"]
    r1 = random_entry_benchmark(
        synthetic_data, universe, "2023-01-01", "2023-06-30",
        10_000, top_n=2, rebalance_days=14, seed=42
    )
    r2 = random_entry_benchmark(
        synthetic_data, universe, "2023-01-01", "2023-06-30",
        10_000, top_n=2, rebalance_days=14, seed=42
    )
    # 同じ seed なので equity_curve 終端が一致
    assert len(r1.equity_curve) == len(r2.equity_curve)
    assert abs(r1.equity_curve[-1]["equity"] - r2.equity_curve[-1]["equity"]) < 1e-6


def test_random_entry_different_seed_differs(synthetic_data):
    universe = ["ETH/USDT", "SOL/USDT", "BNB/USDT"]
    r1 = random_entry_benchmark(
        synthetic_data, universe, "2023-01-01", "2023-06-30",
        10_000, top_n=2, rebalance_days=14, seed=42
    )
    r2 = random_entry_benchmark(
        synthetic_data, universe, "2023-01-01", "2023-06-30",
        10_000, top_n=2, rebalance_days=14, seed=7
    )
    # 選ばれる銘柄が違うので結果も違う
    assert (
        abs(r1.equity_curve[-1]["equity"] - r2.equity_curve[-1]["equity"]) > 0.01
        or len(r1.trades) != len(r2.trades)
    )


def test_benchmark_to_dict_serializes(synthetic_data):
    r = buy_hold_benchmark(
        synthetic_data, "BTC/USDT", "2023-01-01", "2023-03-31", 10_000
    )
    d = r.to_dict()
    assert set(d.keys()) == {"name", "equity_curve", "trades"}
