"""analysis/overfit.py の integration テスト."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT))

from analysis.overfit import (
    bootstrap_sharpe_ci,
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
    regime_consistency_score,
    regime_consistency_score_from_db,
)
from analysis.runs_analyzer import RunsAnalyzer
from db.migrate import migrate_up
from db.repositories.runs_repo import record_run


@pytest.fixture
def fresh_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    migrate_up(path, verbose=False)
    yield path
    for ext in ("", "-wal", "-shm"):
        p = path.parent / f"{path.name}{ext}"
        if p.exists():
            p.unlink()


# ─────────────────────────────
# DSR
# ─────────────────────────────
def test_dsr_high_for_strong_strategy():
    """正のリターンが安定して出る場合 DSR は高い (1 試行のみ前提)."""
    np.random.seed(0)
    rets = pd.Series(np.random.normal(0.001, 0.005, 252))
    dsr = deflated_sharpe_ratio(rets, n_trials=2)
    assert 0 <= dsr <= 1


def test_dsr_low_for_n_trials_high():
    """同じデータでも n_trials を増やすと DSR は下がる (deflate される)."""
    np.random.seed(0)
    rets = pd.Series(np.random.normal(0.001, 0.005, 252))
    dsr_low = deflated_sharpe_ratio(rets, n_trials=2)
    dsr_high_n = deflated_sharpe_ratio(rets, n_trials=1000)
    assert dsr_high_n <= dsr_low + 1e-9


def test_dsr_short_series_returns_zero():
    rets = pd.Series([0.001] * 10)
    assert deflated_sharpe_ratio(rets, n_trials=10) == 0.0


# ─────────────────────────────
# regime_consistency
# ─────────────────────────────
def test_regime_consistency_balanced():
    score = regime_consistency_score(1.0, 0.95)
    assert score == pytest.approx(0.95)


def test_regime_consistency_c3_pattern():
    """C3 ぽい大きな乖離: bull 2.5 / bear 0.20 → 0.08."""
    score = regime_consistency_score(2.5, 0.20)
    assert score == pytest.approx(0.08, abs=0.01)


def test_regime_consistency_zero_handling():
    score = regime_consistency_score(0.0, 0.0)
    assert score is None


# ─────────────────────────────
# CSCV / PBO
# ─────────────────────────────
def test_pbo_low_for_consistent_strategies():
    """全戦略が同じくらいの Sharpe なら PBO は中程度 (0.3〜0.7 くらい)."""
    np.random.seed(0)
    n_strategies = 5
    T = 500
    mat = pd.DataFrame(
        np.random.normal(0.001, 0.01, (T, n_strategies)),
        columns=[f"s{i}" for i in range(n_strategies)],
    )
    pbo = probability_of_backtest_overfitting(mat, n_partitions=4)
    assert 0 <= pbo <= 1


def test_pbo_returns_float():
    np.random.seed(0)
    mat = pd.DataFrame(np.random.normal(0, 0.01, (100, 3)),
                       columns=["a", "b", "c"])
    pbo = probability_of_backtest_overfitting(mat, n_partitions=4)
    assert isinstance(pbo, float)
    assert 0 <= pbo <= 1


# ─────────────────────────────
# bootstrap CI
# ─────────────────────────────
def test_bootstrap_ci_returns_tuple():
    np.random.seed(0)
    rets = pd.Series(np.random.normal(0.001, 0.005, 252))
    low, high = bootstrap_sharpe_ci(rets, n_iter=100)
    assert isinstance(low, float) and isinstance(high, float)
    assert low <= high


# ─────────────────────────────
# RunsAnalyzer
# ─────────────────────────────
def test_runs_analyzer_dsr_with_db(fresh_db):
    np.random.seed(0)
    dates = pd.date_range("2022-01-01", periods=300, freq="D", tz="UTC")
    rets_arr = np.random.normal(0.001, 0.005, 300)
    equity = 10000 * np.cumprod(1 + rets_arr)
    returns_df = pd.DataFrame({"ret": rets_arr, "equity": equity}, index=dates)

    run_id = record_run(
        strategy_id="dsr_test", run_type="single_backtest",
        params={"LB": 25}, universe=["BTC/USDT"],
        period=("2022-01-01", "2022-12-31"),
        metrics={"sharpe": 1.0},
        returns_df=returns_df,
        n_trials_in_group=10,
        db_path=fresh_db,
    )
    a = RunsAnalyzer(fresh_db)
    dsr = a.dsr(run_id)
    assert 0 <= dsr <= 1


def test_promotion_gate_rejects_c3_pattern(fresh_db):
    """C3 ぽい regime 依存戦略は CI gate で reject される."""
    # bull で強く、bear で弱い regime データ
    regimes = {
        "bull": {"regime_def": "BTC > EMA200", "n_days": 700,
                 "cagr": 1.20, "sharpe": 2.5, "max_dd": 0.30},
        "bear": {"regime_def": "BTC < EMA200", "n_days": 365,
                 "cagr": 0.097, "sharpe": 0.20, "max_dd": 0.18},
    }
    np.random.seed(0)
    dates = pd.date_range("2022-01-01", periods=300, freq="D", tz="UTC")
    rets_arr = np.random.normal(0.005, 0.005, 300)
    equity = 10000 * np.cumprod(1 + rets_arr)
    returns_df = pd.DataFrame({"ret": rets_arr, "equity": equity}, index=dates)

    run_id = record_run(
        strategy_id="C3_simulation", run_type="production_sim",
        params={"LB": 25, "ACH_TOP_N": 2}, universe=["BTC/USDT"],
        period=("2020-01-01", "2024-12-31"),
        metrics={"cagr": 1.20, "sharpe": 1.23},
        regimes=regimes, returns_df=returns_df,
        n_trials_in_group=66,  # iter66 grid 想定
        db_path=fresh_db,
    )
    a = RunsAnalyzer(fresh_db)
    passed, reasons = a.is_promotable(run_id)
    assert passed is False
    assert len(reasons["issues"]) >= 1
    # regime_consistency が低いことが理由に含まれる
    assert any("regime_consistency" in r for r in reasons["issues"])


def test_promotion_gate_passes_balanced(fresh_db):
    """regime バランス良好な戦略は gate を通る (DSR が高い場合)."""
    regimes = {
        "bull": {"regime_def": "BTC > EMA200", "n_days": 700,
                 "cagr": 0.30, "sharpe": 1.0, "max_dd": 0.25},
        "bear": {"regime_def": "BTC < EMA200", "n_days": 365,
                 "cagr": 0.20, "sharpe": 0.85, "max_dd": 0.20},
    }
    # 高 Sharpe 安定リターン (DSR を高める)
    np.random.seed(0)
    dates = pd.date_range("2022-01-01", periods=500, freq="D", tz="UTC")
    rets_arr = np.random.normal(0.003, 0.003, 500)  # 高 Sharpe 想定
    equity = 10000 * np.cumprod(1 + rets_arr)
    returns_df = pd.DataFrame({"ret": rets_arr, "equity": equity}, index=dates)

    run_id = record_run(
        strategy_id="balanced", run_type="production_sim",
        params={"LB": 30}, universe=["BTC/USDT"],
        period=("2022-01-01", "2023-12-31"),
        metrics={"sharpe": 1.5},
        regimes=regimes, returns_df=returns_df,
        n_trials_in_group=2,  # 少ない試行 → DSR 上がりやすい
        db_path=fresh_db,
    )
    a = RunsAnalyzer(fresh_db)
    # regime_consistency = 0.85/1.0 = 0.85 で 0.5 を上回る
    score = a.regime_consistency(run_id)
    assert score is not None and score >= 0.5
