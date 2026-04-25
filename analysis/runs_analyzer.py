"""runs テーブルの高水準分析 API.

pandas DataFrame ベース、内部は SQLite (DuckDB ATTACH に切替可)。

Usage:
    from analysis.runs_analyzer import RunsAnalyzer

    a = RunsAnalyzer()
    df = a.to_df(run_type='grid_search')
    pbo = a.pbo(trial_group_id='abc')
    dsr = a.dsr(run_id='def')
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from analysis.overfit import (
    bootstrap_sharpe_ci,
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
    regime_consistency_score_from_db,
)
from db.connection import get_connection


class RunsAnalyzer:
    """runs テーブルの分析高水準 API."""

    def __init__(self, db_path: str | Path = "data/kimochi.db"):
        self.db_path = Path(db_path)

    # ────────────────────
    # query
    # ────────────────────
    def to_df(self, **filters) -> pd.DataFrame:
        """runs を DataFrame で取得。filters は SQL where 条件 (= equality)."""
        conn = get_connection(self.db_path, readonly=True)
        try:
            clauses, params = [], []
            for k, v in filters.items():
                clauses.append(f"{k} = ?")
                params.append(v)
            where = "WHERE " + " AND ".join(clauses) if clauses else ""
            return pd.read_sql(
                f"SELECT * FROM runs {where} ORDER BY created_at DESC",
                conn, params=params,
            )
        finally:
            conn.close()

    def get_returns(self, run_id: str) -> pd.Series:
        """run_returns から ret 系列を取得."""
        conn = get_connection(self.db_path, readonly=True)
        try:
            df = pd.read_sql(
                "SELECT ts, ret FROM run_returns WHERE run_id=? ORDER BY ts",
                conn, params=[run_id],
            )
        finally:
            conn.close()
        if df.empty:
            return pd.Series(dtype=float)
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.set_index("ts")["ret"]

    # ────────────────────
    # 集計
    # ────────────────────
    def sharpe_distribution(self, group_by: str = "strategy_id") -> pd.DataFrame:
        """{group_by} ごとの Sharpe 分布統計."""
        conn = get_connection(self.db_path, readonly=True)
        try:
            return pd.read_sql(
                f"""
                SELECT {group_by} AS grp,
                       COUNT(*) AS n,
                       AVG(sharpe) AS mean_sharpe,
                       MAX(sharpe) AS max_sharpe,
                       MIN(sharpe) AS min_sharpe
                FROM runs
                WHERE sharpe IS NOT NULL
                GROUP BY {group_by}
                ORDER BY mean_sharpe DESC
                """,
                conn,
            )
        finally:
            conn.close()

    def regime_breakdown(self, run_id: str) -> pd.DataFrame:
        """run の regime 別ブレークダウン."""
        conn = get_connection(self.db_path, readonly=True)
        try:
            return pd.read_sql(
                "SELECT regime, n_days, cagr, sharpe, max_dd "
                "FROM run_regimes WHERE run_id=?",
                conn, params=[run_id],
            )
        finally:
            conn.close()

    # ────────────────────
    # 過学習検出
    # ────────────────────
    def dsr(self, run_id: str, n_trials: int | None = None) -> float:
        """run の Deflated Sharpe Ratio.

        n_trials 未指定なら trial_group の n_trials_in_group を使う。
        """
        conn = get_connection(self.db_path, readonly=True)
        try:
            r = conn.execute(
                "SELECT n_trials_in_group FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        finally:
            conn.close()
        if n_trials is None and r:
            n_trials = r["n_trials_in_group"]
        n_trials = n_trials or 2

        rets = self.get_returns(run_id)
        if rets.empty:
            return float("nan")
        return deflated_sharpe_ratio(rets, n_trials=n_trials)

    def pbo(self, trial_group_id: str) -> float:
        """trial_group 全 run の PBO 算出."""
        conn = get_connection(self.db_path, readonly=True)
        try:
            df = pd.read_sql(
                """
                SELECT r.run_id, rr.ts, rr.ret
                FROM runs r
                JOIN run_returns rr ON rr.run_id = r.run_id
                WHERE r.trial_group_id = ?
                ORDER BY r.run_id, rr.ts
                """,
                conn, params=[trial_group_id],
            )
        finally:
            conn.close()
        if df.empty:
            return 0.0
        # pivot: index=ts, columns=run_id, values=ret
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        mat = df.pivot(index="ts", columns="run_id", values="ret").fillna(0)
        if mat.shape[1] < 2:
            return 0.0
        return probability_of_backtest_overfitting(mat)

    def regime_consistency(self, run_id: str) -> float | None:
        return regime_consistency_score_from_db(run_id, db_path=str(self.db_path))

    def bootstrap_ci(self, run_id: str, n_iter: int = 1000) -> tuple[float, float]:
        rets = self.get_returns(run_id)
        if rets.empty:
            return (float("nan"), float("nan"))
        return bootstrap_sharpe_ci(rets, n_iter=n_iter)

    # ────────────────────
    # promotion gate
    # ────────────────────
    def is_promotable(
        self,
        run_id: str,
        *,
        dsr_threshold: float = 0.95,
        regime_threshold: float = 0.5,
    ) -> tuple[bool, dict]:
        """新 strategy 採用可否を判定。理由つきで返す."""
        dsr_val = self.dsr(run_id)
        regime = self.regime_consistency(run_id)

        reasons = {
            "dsr": dsr_val,
            "dsr_threshold": dsr_threshold,
            "regime_consistency": regime,
            "regime_threshold": regime_threshold,
            "passed": True,
            "issues": [],
        }
        if dsr_val < dsr_threshold:
            reasons["passed"] = False
            reasons["issues"].append(f"DSR insufficient: {dsr_val:.3f} < {dsr_threshold}")
        if regime is not None and regime < regime_threshold:
            reasons["passed"] = False
            reasons["issues"].append(
                f"regime_consistency too low: {regime:.3f} < {regime_threshold} "
                "(bull/bear で大きく成績が違う = regime 依存)"
            )
        return reasons["passed"], reasons
