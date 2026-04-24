"""metrics.py の単体テスト."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metrics import (
    compute_all_metrics,
    expectancy,
    exposure,
    max_drawdown_pct,
    max_losing_streak,
    profit_factor,
    regime_breakdown,
    sharpe_ratio,
    symbol_breakdown,
    weekly_returns,
    win_rate,
    yearly_returns,
)


def _make_equity_curve(values: list[float], start: str = "2023-01-01") -> list[dict]:
    """日次 equity curve を合成する."""
    dates = pd.date_range(start, periods=len(values), freq="D")
    return [{"ts": d, "equity": float(v)} for d, v in zip(dates, values)]


def test_weekly_returns_monotonic_growth():
    eq = _make_equity_curve([1000.0 * (1.01 ** i) for i in range(30)])
    rets = weekly_returns(eq)
    assert len(rets) > 0
    # 単調増加なので全リターンがプラス
    assert all(r >= 0 for r in rets)


def test_max_drawdown_pct_simple():
    # 100 → 150 → 75 → 100, ピーク 150, 底 75, DD = 50%
    eq = _make_equity_curve([100, 150, 75, 100])
    dd = max_drawdown_pct(eq)
    assert abs(dd - 50.0) < 0.01


def test_profit_factor_basic():
    trades = [
        {"pnl": 100.0, "won": True},
        {"pnl": -50.0, "won": False},
        {"pnl": 75.0, "won": True},
        {"pnl": -25.0, "won": False},
    ]
    # gross_profit = 175, gross_loss = 75, pf = 175/75 ≈ 2.333
    pf = profit_factor(trades)
    assert abs(pf - (175.0 / 75.0)) < 1e-6


def test_profit_factor_no_loss_returns_inf():
    trades = [{"pnl": 100.0, "won": True}, {"pnl": 50.0, "won": True}]
    assert profit_factor(trades) == float("inf")


def test_profit_factor_empty():
    assert profit_factor([]) == 0.0


def test_win_rate():
    trades = [{"pnl": 1, "won": True}, {"pnl": -1, "won": False}, {"pnl": 1, "won": True}]
    assert abs(win_rate(trades) - (2 / 3 * 100)) < 1e-6


def test_expectancy():
    trades = [{"pnl": 10}, {"pnl": -5}, {"pnl": 20}]
    # (10-5+20)/3 = 25/3
    assert abs(expectancy(trades) - (25 / 3)) < 1e-6


def test_max_losing_streak():
    trades = [
        {"won": True, "pnl": 1},
        {"won": False, "pnl": -1},
        {"won": False, "pnl": -1},
        {"won": True, "pnl": 1},
        {"won": False, "pnl": -1},
        {"won": False, "pnl": -1},
        {"won": False, "pnl": -1},
        {"won": True, "pnl": 1},
    ]
    assert max_losing_streak(trades) == 3


def test_max_losing_streak_all_wins():
    trades = [{"won": True, "pnl": 1} for _ in range(5)]
    assert max_losing_streak(trades) == 0


def test_exposure_half_period():
    eq = _make_equity_curve([100] * 100)  # 99 days
    # 1 trade で 50 日保有
    trades = [
        {
            "entry_ts": eq[0]["ts"],
            "exit_ts": eq[49]["ts"],
            "pnl": 0,
        }
    ]
    ex = exposure(trades, eq)
    assert 0.4 < ex < 0.55


def test_symbol_breakdown():
    trades = [
        {"symbol": "BTC", "pnl": 10, "won": True},
        {"symbol": "BTC", "pnl": -5, "won": False},
        {"symbol": "ETH", "pnl": 20, "won": True},
    ]
    out = symbol_breakdown(trades)
    assert set(out.keys()) == {"BTC", "ETH"}
    assert out["BTC"]["n_trades"] == 2
    assert out["ETH"]["n_trades"] == 1
    assert abs(out["ETH"]["pnl_sum"] - 20.0) < 1e-6


def test_sharpe_ratio_constant_returns_zero():
    # 定数リターンは stdev=0 で Sharpe=0 を返す
    assert sharpe_ratio([0.01, 0.01, 0.01, 0.01]) == 0.0


def test_sharpe_ratio_positive():
    rets = [0.01, 0.02, -0.005, 0.015, 0.01]
    s = sharpe_ratio(rets, periods_per_year=52)
    # プラスであるべき
    assert s > 0


def test_regime_breakdown_no_btc():
    eq = _make_equity_curve([100 * (1.001 ** i) for i in range(60)])
    rb = regime_breakdown(eq, btc_ema200_bool=None)
    assert "bull" in rb and "bear" in rb


def test_yearly_returns_spans_multi_year():
    # 2 年分、1 年目は +50%, 2 年目は +10%
    y1_end = 100.0 * 1.5
    y2_end = y1_end * 1.1
    values = (
        [100.0 + i * 0.1 for i in range(365)]
        + [y1_end + i * 0.05 for i in range(365)]
    )
    # 最終値を揃える
    values[364] = y1_end
    values[-1] = y2_end
    eq = _make_equity_curve(values, start="2023-01-01")
    yr = yearly_returns(eq)
    assert "2023" in yr
    assert "2024" in yr
    # 2023 は +50% 近辺
    assert 40 < yr["2023"] < 60


def test_compute_all_metrics_integration():
    eq = _make_equity_curve([1000 * (1.001 ** i) for i in range(365)])
    trades = [
        {"symbol": "BTC", "entry_ts": eq[0]["ts"], "exit_ts": eq[100]["ts"],
         "pnl": 50, "won": True},
        {"symbol": "ETH", "entry_ts": eq[100]["ts"], "exit_ts": eq[200]["ts"],
         "pnl": -20, "won": False},
        {"symbol": "BTC", "entry_ts": eq[200]["ts"], "exit_ts": eq[300]["ts"],
         "pnl": 30, "won": True},
    ]
    m = compute_all_metrics(eq, trades)
    # 10 項目 + symbol/regime breakdown が揃うこと
    expected_keys = {
        "cagr_pct", "yearly_return_pct", "max_drawdown_pct",
        "profit_factor", "win_rate_pct", "expectancy",
        "max_losing_streak", "exposure", "sharpe_ratio",
        "num_trades", "num_weeks", "symbol_breakdown", "regime_breakdown",
    }
    assert expected_keys.issubset(set(m.keys()))
    assert m["num_trades"] == 3
    # 単調増加 equity なので DD は非常に小さい
    assert m["max_drawdown_pct"] < 1.0
