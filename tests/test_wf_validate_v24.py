"""wf_validate_v24.py の単体テスト."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wf_validate_v24 import (
    WINDOWS,
    BULL_ACH_WEIGHT_GRID,
    TRAIL_STOP_ACH_GRID,
    TRAIL_STOP_BTC_GRID,
    BTResult,
    _finalize,
    _verdict,
    best_by_is_calmar,
    make_universe,
    run_bt_v24,
)


# ─────────────────────────────
# 窓定義の整合性
# ─────────────────────────────
def test_windows_have_unique_ids():
    ids = [w["id"] for w in WINDOWS]
    assert len(ids) == len(set(ids)), f"重複 ID: {ids}"


def test_windows_is_before_oos():
    """各窓で IS が OOS より前 (リーク防止)."""
    for w in WINDOWS:
        is_end = pd.Timestamp(w["is_end"])
        oos_start = pd.Timestamp(w["oos_start"])
        assert is_end < oos_start, f"{w['id']}: IS {is_end} >= OOS {oos_start}"


def test_windows_is_shorter_than_1year_not_allowed():
    """IS 期間が短すぎないことを保証 (半年以上)."""
    for w in WINDOWS:
        is_start = pd.Timestamp(w["is_start"])
        is_end = pd.Timestamp(w["is_end"])
        assert (is_end - is_start).days >= 180, f"{w['id']}: IS 期間が短すぎる"


def test_windows_cover_2022_bear_as_oos():
    """W1 は 2022 ベア相場を OOS に含むこと (検証の要)."""
    w1 = next(w for w in WINDOWS if w["id"] == "W1")
    assert w1["oos_start"].startswith("2022")
    assert w1["oos_end"].startswith("2022")


# ─────────────────────────────
# グリッド妥当性
# ─────────────────────────────
def test_grid_covers_pr8_defaults():
    """PR #8 の既定値 (0.60, 0.30, 0.20) がグリッドに含まれる."""
    assert 0.60 in BULL_ACH_WEIGHT_GRID
    assert 0.30 in TRAIL_STOP_ACH_GRID
    assert 0.20 in TRAIL_STOP_BTC_GRID


def test_grid_values_in_sane_range():
    assert all(0.3 <= x <= 0.8 for x in BULL_ACH_WEIGHT_GRID)
    assert all(0.1 <= x <= 0.5 for x in TRAIL_STOP_ACH_GRID)
    assert all(0.1 <= x <= 0.5 for x in TRAIL_STOP_BTC_GRID)


# ─────────────────────────────
# _finalize: メトリクス計算
# ─────────────────────────────
def test_finalize_flat_curve():
    """エクイティが横ばいなら DD=0, CAGR=0."""
    dates = pd.date_range("2022-01-01", periods=365, freq="D")
    curve = [{"ts": d, "equity": 10_000.0} for d in dates]
    r = _finalize(curve, 10_000.0, n_trades=0, n_bear_exits=0, n_trail_ach=0, n_trail_btc=0)
    assert r.max_dd == 0.0
    assert abs(r.cagr) < 0.01
    assert r.total_ret == 0.0


def test_finalize_dd_calculation():
    """10000 → 15000 → 7500 で DD 50%."""
    dates = pd.date_range("2022-01-01", periods=3, freq="D")
    curve = [
        {"ts": dates[0], "equity": 10_000.0},
        {"ts": dates[1], "equity": 15_000.0},
        {"ts": dates[2], "equity": 7_500.0},
    ]
    r = _finalize(curve, 10_000.0, n_trades=0, n_bear_exits=0, n_trail_ach=0, n_trail_btc=0)
    assert r.max_dd == 50.0


def test_finalize_cagr_positive():
    """1 年で 2 倍 → CAGR ≈ +100%."""
    dates = pd.date_range("2022-01-01", periods=366, freq="D")
    # 線形に 10000 → 20000
    vals = [10_000.0 + (10_000.0 * i / 365) for i in range(366)]
    curve = [{"ts": d, "equity": v} for d, v in zip(dates, vals)]
    r = _finalize(curve, 10_000.0, n_trades=0, n_bear_exits=0, n_trail_ach=0, n_trail_btc=0)
    assert 95.0 < r.cagr < 105.0


# ─────────────────────────────
# _verdict
# ─────────────────────────────
def test_verdict_loss():
    assert _verdict({"oos_cagr": -10, "oos_max_dd": 30}) == "🔴 損失"


def test_verdict_dd_excessive():
    assert _verdict({"oos_cagr": 20, "oos_max_dd": 65}) == "🟡 DD 過大"


def test_verdict_good():
    assert _verdict({"oos_cagr": 40, "oos_max_dd": 40}) == "✅ 良好"


def test_verdict_medium():
    assert _verdict({"oos_cagr": 15, "oos_max_dd": 45}) == "🟡 可"


# ─────────────────────────────
# best_by_is_calmar
# ─────────────────────────────
def test_best_by_is_calmar_picks_highest_calmar():
    rows = [
        {"window": "W1", "is_calmar": 0.5, "bull_ach_weight": 0.50},
        {"window": "W1", "is_calmar": 1.2, "bull_ach_weight": 0.60},
        {"window": "W1", "is_calmar": 0.8, "bull_ach_weight": 0.55},
        {"window": "W2", "is_calmar": 2.0, "bull_ach_weight": 0.65},  # 別窓で最大だが選ばれない
    ]
    best = best_by_is_calmar(rows, "W1")
    assert best["bull_ach_weight"] == 0.60


# ─────────────────────────────
# run_bt_v24: 実データで smoke test (キャッシュがある環境のみ)
# ─────────────────────────────
@pytest.fixture
def cached_data():
    """キャッシュが無い環境ではスキップ."""
    from wf_validate_v24 import load_cache
    try:
        return load_cache()
    except FileNotFoundError:
        pytest.skip("キャッシュ未検出: smoke test スキップ")


def test_run_bt_v24_smoke_short_window(cached_data):
    """短期間 (3 か月) を走らせて例外が出ないこと。"""
    universe = make_universe(cached_data)
    assert len(universe) > 10
    r = run_bt_v24(
        cached_data, universe, "2024-01-01", "2024-03-31",
        bull_ach_weight=0.60, trail_stop_ach=0.30, trail_stop_btc=0.20,
    )
    assert isinstance(r, BTResult)
    assert r.days > 80
    assert r.final > 0


def test_run_bt_v24_trail_btc_fires_in_bear(cached_data):
    """2022 年 (ベア相場) を走らせると BTC/ACH トレイルが 1 回以上発動するか
    Bear 退避が発生する。"""
    universe = make_universe(cached_data)
    r = run_bt_v24(
        cached_data, universe, "2022-01-01", "2022-12-31",
        bull_ach_weight=0.60, trail_stop_ach=0.30, trail_stop_btc=0.20,
    )
    # ベア相場では少なくとも Bear 退避か BTC trail のどちらかが発動するはず
    assert r.n_bear_exits + r.n_trail_btc > 0, (
        f"2022 ベア相場で退避機構が一切発動していない: "
        f"bear={r.n_bear_exits}, trail_btc={r.n_trail_btc}"
    )
