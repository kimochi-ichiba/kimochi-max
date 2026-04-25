"""db.connection の単体テスト."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from db.connection import (
    KimochiConnection,
    SQLITE_MIN,
    begin_immediate,
    get_connection,
)


@pytest.fixture
def tmp_db():
    """空の一時 DB ファイル."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    yield path
    # WAL/SHM も削除
    for ext in ("", "-wal", "-shm"):
        p = path.parent / f"{path.name}{ext}"
        if p.exists():
            p.unlink()


def test_get_connection_returns_kimochi_connection(tmp_db):
    conn = get_connection(tmp_db)
    try:
        assert isinstance(conn, KimochiConnection)
        assert isinstance(conn, sqlite3.Connection)
    finally:
        conn.close()


def test_get_connection_wal_mode(tmp_db):
    conn = get_connection(tmp_db)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal", f"expected WAL, got {mode}"
    finally:
        conn.close()


def test_get_connection_foreign_keys_on(tmp_db):
    conn = get_connection(tmp_db)
    try:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
    finally:
        conn.close()


def test_get_connection_busy_timeout(tmp_db):
    conn = get_connection(tmp_db)
    try:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 5000
    finally:
        conn.close()


def test_get_connection_caches_git_sha(tmp_db):
    conn = get_connection(tmp_db)
    try:
        # git sha は 40 文字 (SHA1) または "unknown"
        assert hasattr(conn, "_git_sha")
        assert isinstance(conn._git_sha, str)
        assert conn._git_sha == "unknown" or len(conn._git_sha) == 40
    finally:
        conn.close()


def test_get_connection_readonly_skips_git_sha(tmp_db):
    conn = get_connection(tmp_db, readonly=True)
    try:
        # readonly でも _git_sha は "unknown" がセットされる
        assert conn._git_sha == "unknown"
    finally:
        conn.close()


def test_sqlite_version_meets_minimum():
    """環境の SQLite が 3.31 以上 (generated column 必須)."""
    cur = tuple(map(int, sqlite3.sqlite_version.split(".")))
    assert cur >= SQLITE_MIN


def test_begin_immediate_starts_transaction(tmp_db):
    conn = get_connection(tmp_db)
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        with begin_immediate(conn):
            conn.execute("INSERT INTO t VALUES (1)")
        rows = conn.execute("SELECT x FROM t").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 1
    finally:
        conn.close()


def test_begin_immediate_rollback_on_exception(tmp_db):
    conn = get_connection(tmp_db)
    try:
        conn.execute("CREATE TABLE t (x INTEGER PRIMARY KEY)")
        try:
            with begin_immediate(conn):
                conn.execute("INSERT INTO t VALUES (1)")
                conn.execute("INSERT INTO t VALUES (1)")  # PK 重複で例外
        except sqlite3.IntegrityError:
            pass
        # ロールバックされて行数 0 のはず
        n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        assert n == 0
    finally:
        conn.close()
