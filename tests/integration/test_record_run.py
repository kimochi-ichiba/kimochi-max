"""runs_repo.record_run / normalize_metrics の integration テスト."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import warnings
from pathlib import Path

import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT))

from db.connection import get_connection
from db.migrate import migrate_up
from db.repositories.runs_repo import (
    normalize_metrics,
    query_runs,
    record_run,
    regime_consistency_score,
)


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
# normalize_metrics
# ─────────────────────────────
def test_normalize_pct_suffix():
    out = normalize_metrics({"max_dd_pct": 62.0})
    assert out["max_dd"] == pytest.approx(0.62)


def test_normalize_alias_mdd():
    out = normalize_metrics({"mdd": 0.45})
    assert out["max_dd"] == 0.45


def test_normalize_avg_annual_ret_warns():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        normalize_metrics({"avg_annual_ret": 1.2})
        assert any("avg_annual_ret" in str(x.message) for x in w)


def test_normalize_passthrough_known():
    out = normalize_metrics({"cagr": 0.15, "sharpe": 1.5})
    assert out["cagr"] == 0.15
    assert out["sharpe"] == 1.5


# ─────────────────────────────
# record_run
# ─────────────────────────────
def test_record_run_basic(fresh_db):
    run_id = record_run(
        strategy_id="test_v25",
        run_type="single_backtest",
        params={"LB": 25, "ACH_TOP_N": 2},
        universe=["BTC/USDT", "ETH/USDT"],
        period=("2022-01-01", "2022-12-31"),
        metrics={"cagr": 0.10, "max_dd": 0.20, "sharpe": 1.0},
        db_path=fresh_db,
    )
    assert run_id
    df = query_runs(strategy_id="test_v25", db_path=fresh_db)
    assert len(df) == 1
    assert df.iloc[0]["cagr"] == pytest.approx(0.10)


def test_record_run_with_yearly_periods(fresh_db):
    run_id = record_run(
        strategy_id="test_yp",
        run_type="single_backtest",
        params={"LB": 25},
        universe=["BTC/USDT"],
        period=("2020-01-01", "2024-12-31"),
        metrics={"cagr": 0.50},
        yearly={2020: 100.0, 2021: 200.0, 2022: -40.0},
        periods={"bear_2022": {"period_start": "2022-01-01",
                                "period_end": "2022-12-31",
                                "cagr": -0.40, "max_dd": 0.65, "sharpe": -0.5}},
        db_path=fresh_db,
    )
    conn = get_connection(fresh_db, readonly=True)
    try:
        n_y = conn.execute(
            "SELECT COUNT(*) FROM run_yearly WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        n_p = conn.execute(
            "SELECT COUNT(*) FROM run_periods WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert n_y == 3
    assert n_p == 1


def test_record_run_with_returns_df(fresh_db):
    dates = pd.date_range("2022-01-01", periods=10, freq="D", tz="UTC")
    returns_df = pd.DataFrame({
        "ret": [0.01, -0.005, 0.02, 0.0, 0.015, -0.01, 0.005, 0.0, 0.02, 0.01],
        "equity": [10100, 10049.5, 10250.5, 10250.5, 10404.3,
                   10300.3, 10351.8, 10351.8, 10558.9, 10664.5],
    }, index=dates)
    run_id = record_run(
        strategy_id="test_returns",
        run_type="single_backtest",
        params={"LB": 25},
        universe=["BTC/USDT"],
        period=("2022-01-01", "2022-01-10"),
        metrics={"cagr": 0.066, "sharpe": 1.5},
        returns_df=returns_df,
        db_path=fresh_db,
    )
    conn = get_connection(fresh_db, readonly=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM run_returns WHERE run_id=?",
                         (run_id,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 10


def test_record_run_with_wf_windows(fresh_db):
    wf = [
        {"window_idx": 1, "scheme": "rolling",
         "is_start": "2020-01-01", "is_end": "2021-12-31",
         "oos_start": "2022-01-01", "oos_end": "2022-12-31",
         "is_sharpe": 1.5, "oos_sharpe": 0.3,
         "is_cagr": 1.2, "oos_cagr": 0.05,
         "is_max_dd": 0.4, "oos_max_dd": 0.2},
        {"window_idx": 2, "scheme": "rolling",
         "is_start": "2021-01-01", "is_end": "2022-12-31",
         "oos_start": "2023-01-01", "oos_end": "2023-12-31",
         "is_sharpe": 0.8, "oos_sharpe": 1.0,
         "is_cagr": 0.3, "oos_cagr": 0.45,
         "is_max_dd": 0.55, "oos_max_dd": 0.4},
    ]
    run_id = record_run(
        strategy_id="test_wf", run_type="wf_validation",
        params={"LB": 25}, universe=["BTC/USDT"],
        period=("2020-01-01", "2023-12-31"),
        metrics={"sharpe": 0.65},
        wf_windows=wf, db_path=fresh_db,
    )
    conn = get_connection(fresh_db, readonly=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM wf_windows WHERE run_id=?",
                         (run_id,)).fetchone()[0]
        eff = conn.execute(
            "SELECT oos_efficiency FROM v_wf_efficiency WHERE run_id=?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    assert n == 2
    # avg(0.3/1.5, 1.0/0.8) = avg(0.2, 1.25) = 0.725
    assert eff["oos_efficiency"] == pytest.approx(0.725, abs=0.001)


def test_record_run_with_regimes_c3_pattern(fresh_db):
    """C3 ぽい regime 偏り (bull +120% / bear +9.7%) を記録、
    regime_consistency_score が 0.08 程度になることを確認."""
    regimes = {
        "bull": {"regime_def": "BTC_close > EMA200",
                 "n_days": 700, "cagr": 1.20, "sharpe": 2.5, "max_dd": 0.30},
        "bear": {"regime_def": "BTC_close < EMA200",
                 "n_days": 365, "cagr": 0.097, "sharpe": 0.20, "max_dd": 0.18},
    }
    run_id = record_run(
        strategy_id="C3_simulation", run_type="production_sim",
        params={"LB": 25, "ACH_TOP_N": 2}, universe=["BTC/USDT"],
        period=("2020-01-01", "2024-12-31"),
        metrics={"cagr": 1.20, "sharpe": 1.23},
        regimes=regimes, db_path=fresh_db,
    )
    score = regime_consistency_score(run_id, db_path=fresh_db)
    assert score is not None
    assert score < 0.5, f"C3 should fail consistency: got {score}"
    # 0.20 / 2.5 = 0.08
    assert score == pytest.approx(0.08, abs=0.01)


# ─────────────────────────────
# dedup
# ─────────────────────────────
def test_record_run_dedup_unique_violation(fresh_db):
    args = dict(
        strategy_id="dup_test", run_type="single_backtest",
        params={"LB": 25}, universe=["BTC/USDT"],
        period=("2022-01-01", "2022-12-31"),
        metrics={"cagr": 0.1}, db_path=fresh_db,
    )
    record_run(**args)
    with pytest.raises(sqlite3.IntegrityError):
        record_run(**args)  # 同じ canonical_hash で 2 回目は失敗


def test_canonical_hash_independent_of_key_order(fresh_db):
    """params の key 順序が違っても重複と判定される."""
    record_run(
        strategy_id="hash_test", run_type="single_backtest",
        params={"LB": 25, "ACH_TOP_N": 2, "FEE": 0.001},
        universe=["BTC/USDT"],
        period=("2022-01-01", "2022-12-31"),
        metrics={"cagr": 0.1}, db_path=fresh_db,
    )
    with pytest.raises(sqlite3.IntegrityError):
        record_run(
            strategy_id="hash_test", run_type="single_backtest",
            # key 順序違いだが内容同じ
            params={"FEE": 0.001, "ACH_TOP_N": 2, "LB": 25},
            universe=["BTC/USDT"],
            period=("2022-01-01", "2022-12-31"),
            metrics={"cagr": 0.1}, db_path=fresh_db,
        )


# ─────────────────────────────
# FK CASCADE
# ─────────────────────────────
def test_fk_cascade_on_run_delete(fresh_db):
    """run 削除で run_yearly / run_returns / wf_windows / run_regimes が消える."""
    dates = pd.date_range("2022-01-01", periods=5, freq="D", tz="UTC")
    returns_df = pd.DataFrame({"ret": [0.01]*5, "equity": [10000]*5}, index=dates)

    run_id = record_run(
        strategy_id="cascade_test", run_type="single_backtest",
        params={"LB": 99}, universe=["BTC/USDT"],
        period=("2022-01-01", "2022-12-31"),
        metrics={"cagr": 0.1},
        yearly={2022: 10.0},
        returns_df=returns_df,
        regimes={"bull": {"regime_def": "x", "n_days": 100,
                          "cagr": 0.5, "sharpe": 1.0, "max_dd": 0.3},
                 "bear": {"regime_def": "x", "n_days": 50,
                          "cagr": -0.1, "sharpe": -0.2, "max_dd": 0.4}},
        db_path=fresh_db,
    )
    conn = get_connection(fresh_db)
    try:
        # 子テーブル件数確認
        for tbl in ("run_yearly", "run_returns", "run_regimes"):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE run_id=?",
                             (run_id,)).fetchone()[0]
            assert n > 0, f"{tbl} has no rows"
        # 親 runs 削除
        conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        # CASCADE で子も消える
        for tbl in ("run_yearly", "run_returns", "run_regimes"):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE run_id=?",
                             (run_id,)).fetchone()[0]
            assert n == 0, f"{tbl} cascade failed"
    finally:
        conn.close()
