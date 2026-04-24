"""stability_analysis.py の単体テスト."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stability_analysis import (
    CRYPTO_PROFILE,
    DEFAULT_PROFILE,
    classify_setting,
    overfitting_summary,
    parameter_sensitivity,
    top_n_by_metric,
)


def test_parameter_sensitivity_calculates_relative_changes():
    """base=1.0 → minus=0.9*? / plus=1.1*? で Sharpe が線形変化する run_cell."""

    def run_cell(top_n: int = 10, lookback: int = 25, corr_threshold: float = 0.8):
        # Sharpe = lookback / 25 (基準 25 で Sharpe=1)
        return {"sharpe_ratio": lookback / 25.0}

    base = {"top_n": 10, "lookback": 25, "corr_threshold": 0.8}
    s = parameter_sensitivity(run_cell, base)
    # top_n を 20% 動かしても Sharpe は変わらない (関数が top_n を見ないため)
    assert s["per_param"]["top_n"]["relative_change_pct"] == 0.0
    # lookback を ±20% 動かすと Sharpe も ±20% 変動 (base=1.0 の 20%)
    assert 15 < s["per_param"]["lookback"]["relative_change_pct"] < 25


def test_classify_production_ready():
    is_m = {"cagr_pct": 30.0, "sharpe_ratio": 1.0, "max_drawdown_pct": 15.0}
    oos_m = {"cagr_pct": 28.0, "sharpe_ratio": 0.9, "max_drawdown_pct": 18.0}
    sensitivity = {"max_relative_change_pct": 20.0}
    r = classify_setting(is_m, oos_m, sensitivity, usdt_weight=0.30)
    assert r["classification"] == "production_ready"


def test_classify_fragile_low_sharpe():
    is_m = {"cagr_pct": 30.0, "sharpe_ratio": 0.8, "max_drawdown_pct": 20.0}
    oos_m = {"cagr_pct": 20.0, "sharpe_ratio": 0.2, "max_drawdown_pct": 40.0}
    sensitivity = {"max_relative_change_pct": 20.0}
    r = classify_setting(is_m, oos_m, sensitivity, usdt_weight=0.30)
    assert r["classification"] == "fragile"
    assert len(r["fragile_reasons"]) >= 2  # Sharpe < 0.3 と DD > 35%


def test_classify_fragile_overfitting_gap():
    """IS が異常に良くて OOS で崩壊するパターン."""
    is_m = {"cagr_pct": 100.0, "sharpe_ratio": 2.0, "max_drawdown_pct": 5.0}
    oos_m = {"cagr_pct": 10.0, "sharpe_ratio": 0.6, "max_drawdown_pct": 15.0}
    sensitivity = {"max_relative_change_pct": 10.0}
    r = classify_setting(is_m, oos_m, sensitivity, usdt_weight=0.30)
    # CAGR gap = 90% で fragile
    assert r["classification"] == "fragile"
    assert any("CAGR gap" in reason for reason in r["fragile_reasons"])


def test_classify_neutral_when_close():
    """条件をいくつか満たさないが壊れてもいない中間."""
    is_m = {"cagr_pct": 20.0, "sharpe_ratio": 0.7, "max_drawdown_pct": 20.0}
    oos_m = {"cagr_pct": 15.0, "sharpe_ratio": 0.45, "max_drawdown_pct": 22.0}
    sensitivity = {"max_relative_change_pct": 25.0}
    r = classify_setting(is_m, oos_m, sensitivity, usdt_weight=0.30)
    # Sharpe 0.45 は > 0.3 かつ < 0.5 → fragile でなく production_ready でもない
    assert r["classification"] == "neutral"


def test_classify_fragile_low_usdt_still_not_fragile():
    """USDT < 0.2 は production_ready を満たさないが fragile にも該当しない."""
    is_m = {"cagr_pct": 30.0, "sharpe_ratio": 1.0, "max_drawdown_pct": 15.0}
    oos_m = {"cagr_pct": 28.0, "sharpe_ratio": 0.9, "max_drawdown_pct": 18.0}
    sensitivity = {"max_relative_change_pct": 20.0}
    r = classify_setting(is_m, oos_m, sensitivity, usdt_weight=0.10)
    # fragile 条件はクリアしているが production_ready の usdt_w < 0.2 で失格
    assert r["classification"] == "neutral"


def test_top_n_by_metric():
    cells = [
        {
            "classification": "production_ready",
            "metrics_snapshot": {"oos_sharpe": 1.0},
        },
        {
            "classification": "production_ready",
            "metrics_snapshot": {"oos_sharpe": 0.8},
        },
        {
            "classification": "fragile",
            "metrics_snapshot": {"oos_sharpe": 0.2},
        },
    ]
    top = top_n_by_metric(cells, "production_ready", "oos_sharpe", 2)
    assert len(top) == 2
    assert top[0]["metrics_snapshot"]["oos_sharpe"] == 1.0


def test_classify_crypto_profile_allows_higher_dd():
    """CRYPTO_PROFILE は OOS MaxDD 40% まで許容する."""
    is_m = {"cagr_pct": 50.0, "sharpe_ratio": 1.2, "max_drawdown_pct": 35.0}
    oos_m = {"cagr_pct": 45.0, "sharpe_ratio": 1.0, "max_drawdown_pct": 38.0}
    sensitivity = {"max_relative_change_pct": 20.0}
    r = classify_setting(
        is_m, oos_m, sensitivity, usdt_weight=0.30, profile=CRYPTO_PROFILE
    )
    # Crypto: DD 38% は < 40% で OK、Sharpe 1.0 > 0.8 で OK、gap 5% < 10% で OK
    assert r["classification"] == "production_ready"


def test_classify_default_profile_same_case_is_fragile():
    """同じケースをデフォルト基準で判定すると fragile になる (対比)."""
    is_m = {"cagr_pct": 50.0, "sharpe_ratio": 1.2, "max_drawdown_pct": 35.0}
    oos_m = {"cagr_pct": 45.0, "sharpe_ratio": 1.0, "max_drawdown_pct": 38.0}
    sensitivity = {"max_relative_change_pct": 20.0}
    r = classify_setting(is_m, oos_m, sensitivity, usdt_weight=0.30)
    # Default: DD 38% > 35% で fragile
    assert r["classification"] == "fragile"
    assert DEFAULT_PROFILE["fragile_dd_min"] == 35.0
    assert CRYPTO_PROFILE["fragile_dd_min"] == 55.0


def test_overfitting_summary_basic():
    cells = [
        {"metrics_snapshot": {"is_oos_cagr_gap_pct": 2.0}},
        {"metrics_snapshot": {"is_oos_cagr_gap_pct": 5.0}},
        {"metrics_snapshot": {"is_oos_cagr_gap_pct": 10.0}},
    ]
    s = overfitting_summary(cells)
    assert s["count"] == 3
    assert s["max"] == 10.0
    assert abs(s["mean"] - 5.666) < 0.1
