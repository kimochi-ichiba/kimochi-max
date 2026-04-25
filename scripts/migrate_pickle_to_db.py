"""pickle キャッシュ → SQLite 移行スクリプト.

results/_iter61_cache.pkl, _bear_test_cache.pkl 等を SQLite に取り込む。
- 銘柄単位で commit (途中失敗でも部分復旧可能)
- ON CONFLICT で冪等
- 進捗を ohlcv_meta.row_count に保存

Usage:
    python scripts/migrate_pickle_to_db.py
    python scripts/migrate_pickle_to_db.py --pickle results/_iter61_cache.pkl
    python scripts/migrate_pickle_to_db.py --dry-run
"""
from __future__ import annotations

import argparse
import pickle
import sys
import time
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from db.connection import get_connection
from db.repositories.ohlcv_repo import upsert_ohlcv

DEFAULT_CANDIDATES = [
    PROJECT / "results" / "_cache_alldata.pkl",
    PROJECT / "results" / "_iter61_cache.pkl",
    PROJECT / "results" / "_bear_test_cache.pkl",
    PROJECT / "results" / "_iter62a_cache_1h.pkl",
    PROJECT / "results" / "_iter63_score_cache.pkl",
]


def _load_pickle(path: Path) -> dict[str, pd.DataFrame]:
    """pickle が dict[str, DataFrame] であることを期待."""
    with open(path, "rb") as f:
        data = pickle.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"unexpected pickle format in {path}: {type(data)}")
    return data


def _infer_timeframe(path: Path, df: pd.DataFrame) -> str:
    """ファイル名 / index から timeframe を推定."""
    if "1h" in path.name.lower():
        return "1h"
    if isinstance(df.index, pd.DatetimeIndex) and len(df) > 1:
        diffs = df.index.to_series().diff().dropna()
        if not diffs.empty:
            median = diffs.median()
            if median == pd.Timedelta(hours=1):
                return "1h"
            if median == pd.Timedelta(days=1):
                return "1d"
    return "1d"


def migrate_pickle(
    pickle_path: Path,
    db_path: Path,
    *,
    dry_run: bool = False,
    verbose: bool = True,
) -> tuple[int, int]:
    """1 つの pickle ファイルを DB に移行.

    Returns: (n_symbols_migrated, n_rows_total)
    """
    if not pickle_path.exists():
        if verbose:
            print(f"  [skip] {pickle_path} (not found)")
        return 0, 0

    data = _load_pickle(pickle_path)
    if verbose:
        print(f"  [load] {pickle_path.name} ({len(data)} symbols)")

    n_sym = 0
    n_rows = 0
    conn = get_connection(db_path) if not dry_run else None

    try:
        for symbol, df in data.items():
            # OHLCV pickle は dict[str, DataFrame] 形式を想定。
            # スコアキャッシュ等の dict[str, dict/list] は OHLCV ではないので skip
            if not isinstance(df, pd.DataFrame):
                if verbose and len(data) <= 5:
                    print(f"    [skip non-DataFrame] {symbol}: {type(df).__name__}")
                continue
            if df is None or df.empty:
                continue
            tf = _infer_timeframe(pickle_path, df)
            if dry_run:
                if verbose:
                    print(f"    [dry] {symbol} ({tf}): {len(df)} rows")
            else:
                # 重要な OHLCV カラムのみ抽出
                cols = [c for c in ("open", "high", "low", "close", "volume",
                                    "quote_volume", "trade_count", "taker_buy_volume")
                        if c in df.columns]
                if not cols or "close" not in cols:
                    if verbose:
                        print(f"    [skip] {symbol}: missing OHLC columns")
                    continue
                upsert_ohlcv(df[cols], symbol, tf, conn=conn)
                n_rows += len(df)
            n_sym += 1
            if verbose and n_sym % 10 == 0:
                print(f"    ... {n_sym} symbols migrated")
    finally:
        if conn:
            conn.close()

    return n_sym, n_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="pickle → SQLite 移行")
    parser.add_argument("--pickle", action="append",
                        help="移行する pickle ファイル (複数指定可、デフォルト=既知 5 種)")
    parser.add_argument("--db", default=str(PROJECT / "data" / "kimochi.db"))
    parser.add_argument("--dry-run", action="store_true", help="実際には書かない")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    pickles = [Path(p) for p in args.pickle] if args.pickle else DEFAULT_CANDIDATES
    db_path = Path(args.db)
    verbose = not args.quiet

    start = time.time()
    total_sym = 0
    total_rows = 0
    for p in pickles:
        n_sym, n_rows = migrate_pickle(p, db_path, dry_run=args.dry_run, verbose=verbose)
        total_sym += n_sym
        total_rows += n_rows

    elapsed = time.time() - start
    print(f"\nOK: {total_sym} symbol migrations, {total_rows:,} rows total "
          f"in {elapsed:.1f}s ({'dry-run' if args.dry_run else 'committed'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
