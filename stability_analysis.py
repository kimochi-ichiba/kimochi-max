"""
stability_analysis.py — パラメータ感度分析 + 本番用/壊れやすい判定
=================================================================

主要関数:
  - parameter_sensitivity(run_func, base_params, all_data, universe, start, end)
      : 代表 param (top_n / lookback / corr_threshold) を ±20% 動かした時の
        Sharpe 変動幅を計算
  - classify_setting(is_m, oos_m, sensitivity, usdt_w)
      : "production_ready" / "fragile" / "neutral" に分類

判定基準 (ユーザー承認済み):

本番用 (すべて満たす):
  - OOS Sharpe > 0.5
  - OOS max DD < 25%
  - 感度: 対象 param を ±20% 動かした時の Sharpe 変動幅が ±30% 以内
  - IS-OOS CAGR 差 < 5% (絶対値)
  - USDT 比率 ≥ 20%

壊れやすい (いずれか該当):
  - OOS Sharpe < 0.3
  - OOS max DD > 35%
  - 感度 ±50% 以上変動
  - IS-OOS CAGR 差 > 10%

どちらでもない → "neutral"
"""
from __future__ import annotations

import statistics
from typing import Any, Callable


# ─────────────────────────────
# 感度分析
# ─────────────────────────────
def parameter_sensitivity(
    run_cell: Callable[..., dict[str, Any]],
    base_params: dict[str, Any],
    sensitivity_targets: tuple[str, ...] = (
        "top_n",
        "lookback",
        "corr_threshold",
    ),
    perturbation_pct: float = 0.20,
    metric_key: str = "sharpe_ratio",
) -> dict[str, Any]:
    """base_params で run_cell を呼び、その後 sensitivity_targets を ±20% 振って
    metric (既定 Sharpe) の変動幅を測定。

    戻り値:
    {
      "baseline_metric": float,
      "per_param": {
          "top_n": {
              "minus_20_value": float | None, "minus_20_param": int | float,
              "plus_20_value": float | None,  "plus_20_param": int | float,
              "relative_change_pct": float,   # Sharpe の ±変動の最大値 (%)
          }, ...
      },
      "max_relative_change_pct": float,
    }
    """
    base_result = run_cell(**base_params)
    base_metric = float(base_result.get(metric_key, 0.0))

    per_param: dict[str, Any] = {}
    changes: list[float] = []

    for key in sensitivity_targets:
        if key not in base_params:
            continue
        base_val = base_params[key]
        # 整数パラメータは round、その他は float で処理
        if isinstance(base_val, int):
            minus_val = max(1, int(round(base_val * (1 - perturbation_pct))))
            plus_val = max(1, int(round(base_val * (1 + perturbation_pct))))
        else:
            minus_val = float(base_val) * (1 - perturbation_pct)
            plus_val = float(base_val) * (1 + perturbation_pct)

        minus_params = {**base_params, key: minus_val}
        plus_params = {**base_params, key: plus_val}

        try:
            minus_metric = float(run_cell(**minus_params).get(metric_key, 0.0))
        except Exception:
            minus_metric = None
        try:
            plus_metric = float(run_cell(**plus_params).get(metric_key, 0.0))
        except Exception:
            plus_metric = None

        # 相対変化 (%): |minus - base| / |base| と |plus - base| / |base| の max
        rel_changes = []
        if minus_metric is not None and base_metric != 0:
            rel_changes.append(abs(minus_metric - base_metric) / abs(base_metric) * 100.0)
        if plus_metric is not None and base_metric != 0:
            rel_changes.append(abs(plus_metric - base_metric) / abs(base_metric) * 100.0)
        rel_change = max(rel_changes) if rel_changes else 0.0

        per_param[key] = {
            "minus_param": minus_val,
            "minus_metric": minus_metric,
            "plus_param": plus_val,
            "plus_metric": plus_metric,
            "relative_change_pct": round(rel_change, 2),
        }
        changes.append(rel_change)

    max_change = max(changes) if changes else 0.0
    return {
        "baseline_metric": base_metric,
        "per_param": per_param,
        "max_relative_change_pct": round(max_change, 2),
    }


# ─────────────────────────────
# 判定
# ─────────────────────────────
def classify_setting(
    is_metrics: dict[str, Any],
    oos_metrics: dict[str, Any],
    sensitivity: dict[str, Any] | None,
    usdt_weight: float,
) -> dict[str, Any]:
    """本番用 / 壊れやすい / 中立 の 3 区分で判定."""
    oos_sharpe = float(oos_metrics.get("sharpe_ratio", 0.0))
    oos_dd = float(oos_metrics.get("max_drawdown_pct", 100.0))
    is_cagr = float(is_metrics.get("cagr_pct", 0.0))
    oos_cagr = float(oos_metrics.get("cagr_pct", 0.0))
    overfitting_gap = abs(is_cagr - oos_cagr)

    sens_change = (
        float(sensitivity["max_relative_change_pct"])
        if sensitivity is not None
        else 0.0
    )

    # Fragile チェック (いずれか)
    fragile_reasons: list[str] = []
    if oos_sharpe < 0.3:
        fragile_reasons.append(f"OOS Sharpe {oos_sharpe:.2f} < 0.3")
    if oos_dd > 35.0:
        fragile_reasons.append(f"OOS MaxDD {oos_dd:.1f}% > 35%")
    if sens_change > 50.0:
        fragile_reasons.append(f"Sensitivity {sens_change:.1f}% > 50%")
    if overfitting_gap > 10.0:
        fragile_reasons.append(
            f"IS-OOS CAGR gap {overfitting_gap:.1f}% > 10%"
        )

    # Production ready チェック (すべて満たす)
    prod_checks = {
        "oos_sharpe_gt_0.5": oos_sharpe > 0.5,
        "oos_dd_lt_25": oos_dd < 25.0,
        "sensitivity_lt_30": sens_change < 30.0,
        "is_oos_cagr_gap_lt_5": overfitting_gap < 5.0,
        "usdt_weight_ge_0.2": usdt_weight >= 0.2,
    }
    production_ready = all(prod_checks.values())

    if fragile_reasons:
        classification = "fragile"
    elif production_ready:
        classification = "production_ready"
    else:
        classification = "neutral"

    return {
        "classification": classification,
        "fragile_reasons": fragile_reasons,
        "production_checks": prod_checks,
        "metrics_snapshot": {
            "oos_sharpe": oos_sharpe,
            "oos_dd_pct": oos_dd,
            "sensitivity_max_change_pct": sens_change,
            "is_oos_cagr_gap_pct": round(overfitting_gap, 3),
            "usdt_weight": usdt_weight,
        },
    }


# ─────────────────────────────
# トップ N 抽出
# ─────────────────────────────
def top_n_by_metric(
    results: list[dict[str, Any]],
    classification: str,
    metric_key: str = "oos_sharpe",
    n: int = 5,
) -> list[dict[str, Any]]:
    """分類に属するセルを metric_key で降順ソートし上位 n 件返す."""
    filtered = [
        r for r in results if r.get("classification") == classification
    ]
    sorted_ = sorted(
        filtered,
        key=lambda r: r["metrics_snapshot"].get(metric_key, 0.0),
        reverse=True,
    )
    return sorted_[:n]


# ─────────────────────────────
# サマリ: overfitting gap の統計
# ─────────────────────────────
def overfitting_summary(results: list[dict[str, Any]]) -> dict[str, float]:
    """全 cell の IS-OOS CAGR gap を集計."""
    gaps = [
        r["metrics_snapshot"]["is_oos_cagr_gap_pct"]
        for r in results
        if "metrics_snapshot" in r
    ]
    if not gaps:
        return {"count": 0}
    return {
        "count": len(gaps),
        "mean": round(statistics.mean(gaps), 3),
        "median": round(statistics.median(gaps), 3),
        "stdev": round(statistics.pstdev(gaps), 3) if len(gaps) > 1 else 0.0,
        "max": round(max(gaps), 3),
        "min": round(min(gaps), 3),
    }
