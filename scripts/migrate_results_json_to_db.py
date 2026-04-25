"""results/iter*.json → SQLite runs テーブル へのマイグレーション.

既存の iter JSON はキー揺れ (cagr/cagr_pct/avg_annual_ret 等) があるため、
normalize_metrics で吸収。重複は params_canonical_hash で skip。

Usage:
    python scripts/migrate_results_json_to_db.py
    python scripts/migrate_results_json_to_db.py --dry-run
    python scripts/migrate_results_json_to_db.py --pattern 'iter5*.json'
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from db.connection import get_connection
from db.repositories.runs_repo import (
    _canonical_hash,
    _new_run_id,
    normalize_metrics,
    record_run,
)


_METRIC_KEYS = (
    "cagr", "cagr_pct", "avg_annual_ret",
    "max_dd", "max_dd_pct", "mdd",
    "sharpe", "sortino", "calmar",
    "total_ret", "n_trades", "win_rate",
    "final", "final_equity", "initial_equity",
)

_PARAM_KEYS = (
    "lookback", "lookback_days", "LB",
    "top_n", "ACH_TOP_N",
    "fee", "FEE", "slip", "SLIP",
    "btc_w", "ach_w", "usdt_w",
    "rebalance_days", "ACH_REBALANCE_DAYS",
    "adx_min", "ADX_MIN",
    "corr_threshold", "ACH_CORR_THRESHOLD",
)


def _extract_metrics(d: dict) -> dict:
    out = {}
    for k in _METRIC_KEYS:
        if k in d:
            out[k] = d[k]
    return out


def _extract_params(d: dict) -> dict:
    out = {}
    for k in _PARAM_KEYS:
        if k in d:
            out[k] = d[k]
    return out


def _is_results_dict(d) -> bool:
    """dict が「結果オブジェクト」 (cagr 等のメトリクスを持つ) か判定."""
    if not isinstance(d, dict):
        return False
    return any(k in d for k in ("cagr", "cagr_pct", "max_dd",
                                  "mdd", "sharpe", "total_ret"))


def _walk_for_results(obj, path: str = "") -> list[tuple[str, dict]]:
    """ネストされた dict から結果オブジェクトを拾い集める."""
    found: list[tuple[str, dict]] = []
    if _is_results_dict(obj):
        found.append((path or "ROOT", obj))
        # 兄弟も探索
    if isinstance(obj, dict):
        for k, v in obj.items():
            child_path = f"{path}.{k}" if path else k
            found.extend(_walk_for_results(v, child_path))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(_walk_for_results(v, f"{path}[{i}]"))
    return found


def _migrate_single_file(
    json_path: Path,
    *,
    db_path: Path,
    dry_run: bool = False,
    verbose: bool = True,
) -> int:
    """1 つの JSON から見つかる「結果オブジェクト」を runs に取り込む."""
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        if verbose:
            print(f"  [skip-bad] {json_path.name}: {e}")
        return 0

    results = _walk_for_results(data)
    if not results:
        if verbose:
            print(f"  [skip-no-results] {json_path.name}")
        return 0

    strategy_id_base = json_path.stem  # "iter59_v22_verify"
    n_imported = 0

    for label, result in results:
        metrics = normalize_metrics(_extract_metrics(result))
        if "cagr" not in metrics and "max_dd" not in metrics:
            continue
        params = _extract_params(result)
        # ファイル名 + path label を strategy id 化
        sid = f"{strategy_id_base}::{label}" if label != "ROOT" else strategy_id_base
        sid = sid[:200]  # SQLite TEXT に十分

        # period_start/end は推測 (なければ "unknown")
        period_start = result.get("period_start") or result.get("start", "unknown")
        period_end = result.get("period_end") or result.get("end", "unknown")
        if isinstance(period_start, (int, float)):
            period_start = str(period_start)
        if isinstance(period_end, (int, float)):
            period_end = str(period_end)

        if dry_run:
            if verbose:
                print(f"  [dry] {sid}: cagr={metrics.get('cagr')}, "
                      f"max_dd={metrics.get('max_dd')}")
            n_imported += 1
            continue

        try:
            record_run(
                strategy_id=sid,
                run_type="single_backtest",
                params=params,
                universe=[],  # universe 不明、空でも record 可能
                period=(str(period_start), str(period_end)),
                metrics=metrics,
                script_name=json_path.name,
                notes=f"migrated from {json_path.name} (legacy)",
                db_path=db_path,
            )
            n_imported += 1
        except sqlite3.IntegrityError as e:
            if verbose:
                print(f"  [skip-dup] {sid}: {e}")

    if verbose and n_imported > 0:
        print(f"  [imported {n_imported}] {json_path.name}")
    return n_imported


def main() -> int:
    parser = argparse.ArgumentParser(description="results JSON → DB migration")
    parser.add_argument("--results-dir", default=str(PROJECT / "results"))
    parser.add_argument("--db", default=str(PROJECT / "data" / "kimochi.db"))
    parser.add_argument("--pattern", default="iter*.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    db_path = Path(args.db)
    verbose = not args.quiet

    files = sorted(results_dir.glob(args.pattern))
    print(f"target: {len(files)} JSON files in {results_dir}")
    start = time.time()
    total = 0
    for f in files:
        total += _migrate_single_file(f, db_path=db_path,
                                       dry_run=args.dry_run, verbose=verbose)
    elapsed = time.time() - start
    print(f"\nOK: {total} runs imported "
          f"in {elapsed:.1f}s ({'dry-run' if args.dry_run else 'committed'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
