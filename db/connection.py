"""SQLite DB 接続ヘルパ.

WAL モード、FK ON、busy_timeout、SQLite version assert を一元化。
generated columns (JSON1) のため SQLite >= 3.31 必須。

Usage:
    from db.connection import get_connection, begin_immediate

    with get_connection('data/kimochi.db') as conn:
        with begin_immediate(conn):
            conn.execute("INSERT INTO ...", (...))
"""
from __future__ import annotations

import contextlib
import sqlite3
import subprocess
from pathlib import Path

SQLITE_MIN = (3, 31, 0)
DEFAULT_DB_PATH = "data/kimochi.db"


class KimochiConnection(sqlite3.Connection):
    """sqlite3.Connection サブクラス。任意属性 (_git_sha 等) を保持できる."""
    pass


def _check_sqlite_version() -> None:
    """SQLite version >= 3.31 を保証 (generated column 必須)."""
    cur_str = sqlite3.sqlite_version
    cur = tuple(map(int, cur_str.split(".")))
    if cur < SQLITE_MIN:
        raise RuntimeError(
            f"SQLite >= {'.'.join(map(str, SQLITE_MIN))} required, got {cur_str}"
        )


def _cache_git_sha(conn: sqlite3.Connection) -> None:
    """起動時 1 回 git rev-parse HEAD を呼び conn._git_sha にキャッシュ."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        conn._git_sha = sha  # type: ignore[attr-defined]
    except Exception:
        conn._git_sha = "unknown"  # type: ignore[attr-defined]


def get_connection(
    db_path: str | Path = DEFAULT_DB_PATH,
    readonly: bool = False,
) -> sqlite3.Connection:
    """共通設定を適用した sqlite3.Connection を返す.

    PRAGMAs:
        journal_mode = WAL
        foreign_keys = ON
        busy_timeout = 5000
        synchronous = NORMAL
        wal_autocheckpoint = 1000

    Notes:
        - isolation_level=None (autocommit、トランザクションは begin_immediate で明示管理)
        - row_factory = sqlite3.Row (dict-like access)
        - readonly=True なら git_sha キャッシュをスキップ
    """
    _check_sqlite_version()

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(db_path),
        timeout=30,
        isolation_level=None,
        detect_types=sqlite3.PARSE_DECLTYPES,
        factory=KimochiConnection,
    )
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA wal_autocheckpoint = 1000")

    if not readonly:
        _cache_git_sha(conn)
    else:
        conn._git_sha = "unknown"  # type: ignore[attr-defined]

    return conn


@contextlib.contextmanager
def begin_immediate(conn: sqlite3.Connection):
    """deadlock 回避のため明示的 BEGIN IMMEDIATE で書き込みロックを取得.

    使い方:
        with begin_immediate(conn):
            conn.execute("INSERT INTO ...", (...))
        # 例外なら ROLLBACK、正常なら COMMIT
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
