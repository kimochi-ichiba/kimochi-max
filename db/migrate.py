"""SQLite migration runner.

`db/migrations/*.sql` をファイル名昇順で読み、未適用のみを単一トランザクションで実行。
SHA256 checksum で改竄検知、冪等性保証。

Usage:
    python -m db.migrate
    python -m db.migrate --db data/kimochi.db
    python -m db.migrate --status     # 適用済 migration 一覧表示
    python -m db.migrate --down 003   # down migration (慎重に使う)
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT / "db" / "migrations"

sys.path.insert(0, str(PROJECT))
from db.connection import begin_immediate, get_connection


def _checksum(path: Path) -> str:
    """SHA256 of file content."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _list_migrations() -> list[tuple[str, Path]]:
    """`*.sql` (excluding `*.down.sql`) を昇順で返す.

    Returns: [(version, path), ...]
    version はファイル名から拡張子を除いた部分。
    """
    out: list[tuple[str, Path]] = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if p.name.endswith(".down.sql"):
            continue
        version = p.stem  # "000_schema_migrations"
        out.append((version, p))
    return out


def _ensure_meta_table(conn: sqlite3.Connection) -> None:
    """schema_migrations テーブルがなければ作成 (chicken-and-egg 回避)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at INTEGER NOT NULL,
            checksum TEXT NOT NULL
        )
        """
    )


def _applied_versions(conn: sqlite3.Connection) -> dict[str, str]:
    """{version: checksum} for applied migrations."""
    return {r["version"]: r["checksum"]
            for r in conn.execute("SELECT version, checksum FROM schema_migrations")}


def migrate_up(db_path: str | Path = "data/kimochi.db", verbose: bool = True) -> int:
    """未適用 migration を昇順で適用.

    Returns: 適用件数。
    """
    conn = get_connection(db_path)
    try:
        _ensure_meta_table(conn)
        applied = _applied_versions(conn)
        n_applied = 0

        for version, path in _list_migrations():
            sum_now = _checksum(path)
            if version in applied:
                if applied[version] != sum_now:
                    raise RuntimeError(
                        f"Checksum mismatch for {version}: "
                        f"applied={applied[version][:8]}... "
                        f"current file={sum_now[:8]}... "
                        f"(migration file was modified after apply)"
                    )
                if verbose:
                    print(f"  [skip] {version} (already applied)")
                continue

            sql = path.read_text(encoding="utf-8")
            if verbose:
                print(f"  [apply] {version}")
            # executescript は内部で COMMIT を発行するため、外側でトランザクションを
            # 張れない。各 migration は CREATE TABLE IF NOT EXISTS / CREATE INDEX
            # IF NOT EXISTS を使って冪等に書くこと (中断時の再実行で部分適用済の
            # オブジェクト再作成を回避するため)。
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, checksum) "
                "VALUES (?, ?, ?)",
                (version, int(time.time() * 1000), sum_now),
            )
            n_applied += 1

        if verbose:
            print(f"OK: {n_applied} migration(s) applied, "
                  f"{len(applied)} already up-to-date")
        return n_applied
    finally:
        conn.close()


def migrate_status(db_path: str | Path = "data/kimochi.db") -> list[dict]:
    """適用状態を返す.

    Returns: [{"version": ..., "applied_at": ..., "checksum": ...}, ...] (適用済のみ)
    """
    conn = get_connection(db_path, readonly=True)
    try:
        _ensure_meta_table(conn)
        rows = conn.execute(
            "SELECT version, applied_at, checksum FROM schema_migrations "
            "ORDER BY version"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def migrate_down(version: str, db_path: str | Path = "data/kimochi.db") -> None:
    """指定 version の down migration を実行 (危険、データ破壊的)."""
    down_path = MIGRATIONS_DIR / f"{version}.down.sql"
    if not down_path.exists():
        raise FileNotFoundError(f"down migration not found: {down_path}")
    conn = get_connection(db_path)
    try:
        sql = down_path.read_text(encoding="utf-8")
        with begin_immediate(conn):
            conn.executescript(sql)
            conn.execute("DELETE FROM schema_migrations WHERE version=?", (version,))
        print(f"OK: down migration {version} applied")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite migration runner")
    parser.add_argument("--db", default="data/kimochi.db",
                        help="DB path (default: data/kimochi.db)")
    parser.add_argument("--status", action="store_true",
                        help="show applied migrations and exit")
    parser.add_argument("--down", metavar="VERSION",
                        help="run down migration (e.g. --down 003_state_trades)")
    args = parser.parse_args()

    if args.status:
        for r in migrate_status(args.db):
            print(f"{r['version']}\tchecksum={r['checksum'][:8]}... "
                  f"applied_at={r['applied_at']}")
        return 0

    if args.down:
        migrate_down(args.down, args.db)
        return 0

    migrate_up(args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
