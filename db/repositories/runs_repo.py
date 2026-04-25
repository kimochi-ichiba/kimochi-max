"""runs (バックテスト/SIM 実行結果) リポジトリ.

record_run + normalize_metrics ヘルパで全 iter 共通の記録を実現。
ULID で run_id 生成、canonical hash で重複検出、FK CASCADE で子テーブル整合性保証。

Usage:
    from db.repositories.runs_repo import record_run, normalize_metrics

    metrics = normalize_metrics({'cagr_pct': 12.0, 'mdd': 0.45})
    run_id = record_run(
        strategy_id='v2.5_multi_lb',
        run_type='single_backtest',
        params={'LB': 25, 'ACH_TOP_N': 2},
        universe=['BTC/USDT', 'ETH/USDT'],
        period=('2022-01-01', '2022-12-31'),
        metrics=metrics,
    )
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
import warnings
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT))

try:
    import ulid as ulid_mod  # ulid-py
except ImportError:
    ulid_mod = None

from db.connection import begin_immediate, get_connection


# ─────────────────────────────
# normalize_metrics
# ─────────────────────────────
_METRIC_ALIASES = {
    "mdd": "max_dd",
    "max_drawdown": "max_dd",
    "cagr_pct": "cagr",
    "max_dd_pct": "max_dd",
    "sharpe_pct": "sharpe",
    "avg_annual_ret": "_avg_annual_ret_NOT_CAGR",  # 別物だと警告
}

_KNOWN_METRIC_KEYS = {
    "cagr", "max_dd", "sharpe", "sortino", "calmar",
    "total_ret", "n_trades", "win_rate",
    "final_equity", "initial_equity",
    "sharpe_se", "skewness", "kurtosis",
}


def normalize_metrics(raw: dict[str, Any]) -> dict[str, Any]:
    """iter スクリプト内のメトリクス辞書を runs テーブルカラムに正規化.

    - `_pct` サフィックス → 0-1 スケールに変換 (例: max_dd_pct=62 → max_dd=0.62)
    - alias 辞書で吸収 (例: mdd → max_dd)
    - avg_annual_ret は CAGR と意味論違うので警告
    """
    out: dict[str, Any] = {}
    for k, v in raw.items():
        canon = _METRIC_ALIASES.get(k, k)
        if canon.endswith("_pct") and isinstance(v, (int, float)):
            # _pct は alias で先に変換されるが、未知の _pct があれば吸収
            out[canon[:-4]] = v / 100
        elif k.endswith("_pct") and canon != k:
            # alias で _pct → なし に変換された場合 (cagr_pct → cagr)
            out[canon] = v / 100
        else:
            out[canon] = v

    if "_avg_annual_ret_NOT_CAGR" in out:
        warnings.warn(
            "avg_annual_ret detected; do not use as CAGR (semantics differ)",
            stacklevel=2,
        )
    return out


# ─────────────────────────────
# helpers
# ─────────────────────────────
def _new_run_id() -> str:
    if ulid_mod is None:
        # フォールバック: timestamp + random hex
        import os
        ts = int(time.time() * 1000)
        rand = os.urandom(8).hex().upper()
        return f"R{ts:013X}{rand}"
    return str(ulid_mod.new())


def _canonical_hash(obj: Any) -> str:
    """sort_keys + separators 統一の SHA1."""
    s = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(s.encode()).hexdigest()


# ─────────────────────────────
# record_run
# ─────────────────────────────
def record_run(
    strategy_id: str,
    run_type: str,
    params: dict[str, Any],
    universe: list[str],
    period: tuple[str, str],
    metrics: dict[str, Any],
    *,
    yearly: dict[int, float] | None = None,
    periods: dict[str, dict[str, Any]] | None = None,
    parent_run_id: str | None = None,
    trial_group_id: str | None = None,
    n_trials_in_group: int = 1,
    cost_model_id: str = "binance_spot_taker_v1",
    returns_df: pd.DataFrame | None = None,
    wf_windows: list[dict[str, Any]] | None = None,
    regimes: dict[str, dict[str, Any]] | None = None,
    benchmark_id: str | None = None,
    script_name: str | None = None,
    notes: str = "",
    db_path: str | Path = "data/kimochi.db",
    conn: sqlite3.Connection | None = None,
) -> str:
    """run_id (ULID) を返す。trial_group_id 未指定時は run_id 自身を使う.

    FK 順序: runs → run_yearly → run_periods → run_returns → wf_windows → run_regimes
    """
    run_id = _new_run_id()
    if trial_group_id is None:
        trial_group_id = parent_run_id or run_id

    params_json = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    params_canonical_hash = _canonical_hash(params)
    universe_hash = _canonical_hash(sorted(universe))
    universe_json = json.dumps(sorted(universe), separators=(",", ":"))

    own_conn = conn is None
    if own_conn:
        conn = get_connection(db_path)

    git_sha = getattr(conn, "_git_sha", "unknown")

    try:
        with begin_immediate(conn):
            conn.execute("""
                INSERT INTO runs (
                    run_id, parent_run_id, trial_group_id, n_trials_in_group,
                    run_type, strategy_id, script_name, git_sha,
                    params_json, params_canonical_hash, universe_hash, universe_json,
                    period_start, period_end,
                    cagr, max_dd, sharpe, sortino, calmar,
                    total_ret, n_trades, win_rate,
                    final_equity, initial_equity,
                    sharpe_se, skewness, kurtosis,
                    benchmark_id, cost_model_id,
                    notes, created_at
                )
                VALUES (?,?,?,?,?,?,?,?, ?,?,?,?, ?,?, ?,?,?,?,?, ?,?,?, ?,?, ?,?,?, ?,?, ?,?)
            """, (
                run_id, parent_run_id, trial_group_id, n_trials_in_group,
                run_type, strategy_id, script_name, git_sha,
                params_json, params_canonical_hash, universe_hash, universe_json,
                period[0], period[1],
                metrics.get("cagr"), metrics.get("max_dd"), metrics.get("sharpe"),
                metrics.get("sortino"), metrics.get("calmar"),
                metrics.get("total_ret"), metrics.get("n_trades"), metrics.get("win_rate"),
                metrics.get("final_equity"), metrics.get("initial_equity"),
                metrics.get("sharpe_se"), metrics.get("skewness"), metrics.get("kurtosis"),
                benchmark_id, cost_model_id,
                notes, int(time.time() * 1000),
            ))

            if yearly:
                conn.executemany(
                    "INSERT INTO run_yearly (run_id, year, ret_pct) VALUES (?, ?, ?)",
                    [(run_id, int(y), float(v)) for y, v in yearly.items()],
                )
            if periods:
                conn.executemany(
                    "INSERT INTO run_periods (run_id, period_label, period_start, "
                    "period_end, cagr, max_dd, sharpe) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(run_id, label, p["period_start"], p["period_end"],
                      p.get("cagr"), p.get("max_dd"), p.get("sharpe"))
                     for label, p in periods.items()],
                )
            if returns_df is not None and not returns_df.empty:
                rows = []
                for ts, row in returns_df.iterrows():
                    if isinstance(ts, pd.Timestamp):
                        ts_ms = int(ts.value // 10**6)
                    else:
                        ts_ms = int(ts)
                    if "ret" not in row or "equity" not in row:
                        continue
                    rows.append((
                        run_id, ts_ms,
                        float(row["ret"]),
                        int(round(float(row["equity"]) * 100)),
                    ))
                if rows:
                    conn.executemany(
                        "INSERT INTO run_returns (run_id, ts, ret, equity_cents) "
                        "VALUES (?, ?, ?, ?)",
                        rows,
                    )
            if wf_windows:
                conn.executemany(
                    "INSERT INTO wf_windows (run_id, window_idx, scheme, "
                    "is_start, is_end, oos_start, oos_end, "
                    "is_sharpe, oos_sharpe, is_cagr, oos_cagr, "
                    "is_max_dd, oos_max_dd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(run_id, w["window_idx"], w.get("scheme", "rolling"),
                      w.get("is_start"), w.get("is_end"),
                      w.get("oos_start"), w.get("oos_end"),
                      w.get("is_sharpe"), w.get("oos_sharpe"),
                      w.get("is_cagr"), w.get("oos_cagr"),
                      w.get("is_max_dd"), w.get("oos_max_dd"))
                     for w in wf_windows],
                )
            if regimes:
                conn.executemany(
                    "INSERT INTO run_regimes (run_id, regime, regime_def, "
                    "n_days, cagr, sharpe, max_dd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(run_id, reg, m.get("regime_def", ""),
                      m.get("n_days"), m.get("cagr"),
                      m.get("sharpe"), m.get("max_dd"))
                     for reg, m in regimes.items()],
                )
    finally:
        if own_conn:
            conn.close()

    return run_id


# ─────────────────────────────
# query
# ─────────────────────────────
def query_runs(
    strategy_id: str | None = None,
    run_type: str | None = None,
    trial_group_id: str | None = None,
    *,
    db_path: str | Path = "data/kimochi.db",
    conn: sqlite3.Connection | None = None,
) -> pd.DataFrame:
    """フィルタで runs を取得。pandas DataFrame 返却."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection(db_path, readonly=True)
    try:
        clauses, params = [], []
        if strategy_id is not None:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        if run_type is not None:
            clauses.append("run_type = ?")
            params.append(run_type)
        if trial_group_id is not None:
            clauses.append("trial_group_id = ?")
            params.append(trial_group_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return pd.read_sql(
            f"SELECT * FROM runs {where} ORDER BY created_at DESC",
            conn, params=params,
        )
    finally:
        if own_conn:
            conn.close()


def get_run(run_id: str, db_path: str | Path = "data/kimochi.db") -> dict | None:
    conn = get_connection(db_path, readonly=True)
    try:
        r = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def regime_consistency_score(
    run_id: str,
    *,
    db_path: str | Path = "data/kimochi.db",
) -> float | None:
    """min(bull, bear) / max(bull, bear) を返す。bull/bear 両方なければ None."""
    conn = get_connection(db_path, readonly=True)
    try:
        rows = conn.execute(
            "SELECT regime, sharpe FROM run_regimes "
            "WHERE run_id=? AND regime IN ('bull','bear')",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    sharpes = {r["regime"]: r["sharpe"] for r in rows}
    if "bull" not in sharpes or "bear" not in sharpes:
        return None
    vals = [sharpes["bull"], sharpes["bear"]]
    if max(vals) == 0:
        return None
    return min(vals) / max(vals)
