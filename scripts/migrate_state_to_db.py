"""state.json + state_backups + snapshots を SQLite に一括移行.

既存資産:
- results/demo_state.json: 現在の SIM state (最新)
- state_backups/*.json: SIM 起動ごとのバックアップ
- snapshots/*.json: 1 分粒度の equity 履歴

Phase 5 の dual-write 開始前に、過去の state を DB に取り込む。

Usage:
    python scripts/migrate_state_to_db.py
    python scripts/migrate_state_to_db.py --dry-run
    python scripts/migrate_state_to_db.py --skip-snapshots  # snapshots/ は重いので除外
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from db.connection import get_connection
from db.repositories.state_repo import write_state_snapshot


def _migrate_demo_state(
    state_path: Path,
    *,
    db_path: Path,
    dry_run: bool = False,
    verbose: bool = True,
) -> int:
    """results/demo_state.json (現在の SIM 状態) を取り込み."""
    if not state_path.exists():
        if verbose:
            print(f"  [skip-not-found] {state_path}")
        return 0
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        if verbose:
            print(f"  [skip-bad-json] {state_path.name}: {e}")
        return 0

    if dry_run:
        eq = state.get("total_equity", 0)
        if verbose:
            print(f"  [dry] {state_path.name}: total_equity=${eq:.2f}")
        return 1
    sid = write_state_snapshot(state, mode="sim", db_path=db_path)
    if verbose:
        print(f"  [imported] {state_path.name} → snapshot_id={sid}")
    return 1


def _is_demo_runner_state(state: dict) -> bool:
    """demo_runner.py 形式の state か判定 (bot_state.py 等は別フォーマット)."""
    if not isinstance(state, dict):
        return False
    # demo_runner.py 必須フィールド
    has_total = "total_equity" in state
    has_btc_part = isinstance(state.get("btc_part"), dict)
    has_ach_part = isinstance(state.get("ach_part"), dict)
    return has_total and has_btc_part and has_ach_part


def _migrate_state_backups(
    backups_dir: Path,
    *,
    db_path: Path,
    dry_run: bool = False,
    verbose: bool = True,
) -> int:
    """state_backups/demo_state_*.json を取り込む (bot_state_*.json は skip)."""
    if not backups_dir.exists():
        return 0
    # demo_state_*.json のみ対象 (kimochimax_bot.py の bot_state_*.json は別形式)
    files = sorted(backups_dir.glob("demo_state*.json"))
    if verbose:
        print(f"  [scan] {backups_dir}/demo_state*.json ({len(files)} files, "
              f"bot_state_*.json は別形式で skip)")
    n = 0
    for f in files:
        try:
            state = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not _is_demo_runner_state(state):
            if verbose:
                print(f"    [skip-format] {f.name}: not demo_runner format")
            continue
        if dry_run:
            n += 1
            continue
        try:
            write_state_snapshot(state, mode="sim", db_path=db_path)
            n += 1
        except Exception as e:
            if verbose:
                print(f"    [skip-err] {f.name}: {e}")
    return n


def _migrate_snapshots_dir(
    snapshots_dir: Path,
    *,
    db_path: Path,
    dry_run: bool = False,
    verbose: bool = True,
    limit: int | None = None,
) -> int:
    """snapshots/*.json (1 分粒度) を取り込む。重いので限定的に."""
    if not snapshots_dir.exists():
        return 0
    files = sorted(snapshots_dir.glob("*.json"))
    if limit:
        files = files[-limit:]  # 最新 N 件のみ
    if verbose:
        print(f"  [scan] {snapshots_dir} ({len(files)} files, limit={limit})")
    n = 0
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        # snapshots/*.json は equity_history 形式 (state 形式ではない)
        # として保存されることが多いので、interpret 不能ならスキップ
        if not isinstance(data, dict) or "btc_part" not in data:
            continue
        if dry_run:
            n += 1
            continue
        try:
            write_state_snapshot(data, mode="sim", db_path=db_path)
            n += 1
        except Exception:
            continue
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description="state migration")
    parser.add_argument("--state-path",
                        default=str(PROJECT / "results" / "demo_state.json"))
    parser.add_argument("--backups-dir",
                        default=str(PROJECT / "state_backups"))
    parser.add_argument("--snapshots-dir",
                        default=str(PROJECT / "snapshots"))
    parser.add_argument("--db",
                        default=str(PROJECT / "data" / "kimochi.db"))
    parser.add_argument("--skip-snapshots", action="store_true",
                        help="snapshots/ ディレクトリ取り込みをスキップ (重い)")
    parser.add_argument("--snapshots-limit", type=int, default=100,
                        help="snapshots/ から取り込む最大件数 (最新 N 件)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    verbose = not args.quiet
    start = time.time()

    print("=== state migration ===")
    n1 = _migrate_demo_state(Path(args.state_path),
                              db_path=db_path,
                              dry_run=args.dry_run, verbose=verbose)
    n2 = _migrate_state_backups(Path(args.backups_dir),
                                 db_path=db_path,
                                 dry_run=args.dry_run, verbose=verbose)
    n3 = 0
    if not args.skip_snapshots:
        n3 = _migrate_snapshots_dir(Path(args.snapshots_dir),
                                     db_path=db_path,
                                     dry_run=args.dry_run, verbose=verbose,
                                     limit=args.snapshots_limit)

    elapsed = time.time() - start
    total = n1 + n2 + n3
    print(f"\nOK: demo_state={n1}, backups={n2}, snapshots={n3} "
          f"= {total} snapshots imported in {elapsed:.1f}s "
          f"({'dry-run' if args.dry_run else 'committed'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
