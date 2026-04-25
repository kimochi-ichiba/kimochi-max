"""iter テンプレート (Phase 3.5).

新規 iter スクリプトはこのテンプレを起点に書く。
record_run + normalize_metrics で全結果を SQLite に保存し、
後の analysis (PBO/DSR/regime_consistency) で横断比較できる状態にする。

最重要ポイント:
1. trial_group_id を全 SCENARIOS で共有 (多重検定の分母)
2. n_trials_in_group = len(SCENARIOS) (DSR 計算で使われる)
3. metrics は normalize_metrics で揺れ吸収
4. returns_df を渡せば run_returns に保存され、DSR/PBO 計算可能
5. regimes を渡せば run_regimes に保存され、regime_consistency 検出可能

Usage:
    python _iter_template.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

import ulid
from db.repositories.runs_repo import normalize_metrics, record_run

# ─────────────────────────────
# テンプレ設定
# ─────────────────────────────
STRATEGY_ID = "template_v1"             # ← この iter の strategy_id
SCRIPT_NAME = Path(__file__).name
RUN_TYPE = "grid_search"                 # 'grid_search'/'wf_validation'/'oos_test' 等
PERIOD_START = "2020-01-01"
PERIOD_END = "2024-12-31"
UNIVERSE = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
COST_MODEL = "binance_spot_taker_v1"     # cost_models テーブルの id

# 探索する scenarios (grid 例)
SCENARIOS = [
    {"name": "lb25_top2", "params": {"LB": 25, "ACH_TOP_N": 2}},
    {"name": "lb25_top3", "params": {"LB": 25, "ACH_TOP_N": 3}},
    {"name": "lb45_top2", "params": {"LB": 45, "ACH_TOP_N": 2}},
]


def run_backtest(params: dict) -> dict:
    """ここに実際のバックテストロジックを書く.

    Returns: dict like {
        'metrics': {'cagr': 0.10, 'max_dd': 0.30, 'sharpe': 1.0, ...},
        'returns_df': DataFrame(index=ts, columns=['ret', 'equity']),
        'regimes': {'bull': {...}, 'bear': {...}},
    }
    """
    # ━━ ダミー実装 (実 iter ではここを実装) ━━
    import numpy as np
    np.random.seed(hash(str(params)) & 0xFFFFFFFF)
    n = 365 * 5
    rets = np.random.normal(0.0005, 0.02, n)
    equity = 10000 * np.cumprod(1 + rets)
    dates = pd.date_range(PERIOD_START, periods=n, freq="D", tz="UTC")
    returns_df = pd.DataFrame({"ret": rets, "equity": equity}, index=dates)

    # メトリクス算出 (実 iter では既存ライブラリを使う)
    cagr = (equity[-1] / equity[0]) ** (252 / n) - 1
    sharpe = rets.mean() / rets.std() * (252 ** 0.5)
    max_dd = (1 - equity / np.maximum.accumulate(equity)).max()

    # regime 別 (実 iter では BTC > EMA200 でフィルタして再計算)
    bull_mask = np.arange(n) < n // 2
    bear_mask = ~bull_mask
    bull_sharpe = rets[bull_mask].mean() / rets[bull_mask].std() * (252 ** 0.5)
    bear_sharpe = rets[bear_mask].mean() / rets[bear_mask].std() * (252 ** 0.5)

    return {
        "metrics": {"cagr": cagr, "max_dd": max_dd, "sharpe": sharpe},
        "returns_df": returns_df,
        "regimes": {
            "bull": {"regime_def": "BTC_close > EMA200",
                     "n_days": int(bull_mask.sum()),
                     "cagr": cagr, "sharpe": bull_sharpe, "max_dd": max_dd},
            "bear": {"regime_def": "BTC_close < EMA200",
                     "n_days": int(bear_mask.sum()),
                     "cagr": cagr * 0.5, "sharpe": bear_sharpe,
                     "max_dd": max_dd * 1.2},
        },
    }


def main() -> None:
    # 全 scenarios で共有する trial_group_id (多重検定の分母)
    trial_group_id = str(ulid.new())
    n_trials = len(SCENARIOS)

    print(f"=== {STRATEGY_ID} ({RUN_TYPE}, n_trials={n_trials}) ===")
    print(f"trial_group_id: {trial_group_id}")

    parent_run_id = None  # 親 run を作る場合は最初に record_run しておく

    for scenario in SCENARIOS:
        print(f"\n--- {scenario['name']} ---")
        result = run_backtest(scenario["params"])

        # メトリクス正規化 (cagr_pct → cagr 等を吸収)
        metrics = normalize_metrics(result["metrics"])

        run_id = record_run(
            strategy_id=f"{STRATEGY_ID}::{scenario['name']}",
            run_type=RUN_TYPE,
            params=scenario["params"],
            universe=UNIVERSE,
            period=(PERIOD_START, PERIOD_END),
            metrics=metrics,
            trial_group_id=trial_group_id,
            n_trials_in_group=n_trials,
            parent_run_id=parent_run_id,
            cost_model_id=COST_MODEL,
            returns_df=result["returns_df"],         # DSR/PBO 計算の前提
            regimes=result["regimes"],               # regime_consistency 検出
            script_name=SCRIPT_NAME,
            notes=f"scenario={scenario['name']}",
        )
        print(f"  run_id: {run_id}")
        print(f"  cagr={metrics['cagr']:.2%}  sharpe={metrics['sharpe']:.2f}  "
              f"max_dd={metrics['max_dd']:.2%}")

    print("\nDone. 結果は data/kimochi.db の runs テーブル参照。")
    print("  分析: from analysis.runs_analyzer import RunsAnalyzer")
    print(f"        RunsAnalyzer().to_df(trial_group_id='{trial_group_id}')")


if __name__ == "__main__":
    main()
