"""db.migrate の単体テスト."""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from db.connection import get_connection
from db.migrate import migrate_status, migrate_up


@pytest.fixture
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    yield path
    for ext in ("", "-wal", "-shm"):
        p = path.parent / f"{path.name}{ext}"
        if p.exists():
            p.unlink()


def test_migrate_up_creates_schema_migrations(tmp_db):
    n = migrate_up(tmp_db, verbose=False)
    assert n >= 1
    conn = get_connection(tmp_db, readonly=True)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='schema_migrations'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        conn.close()


def test_migrate_up_idempotent(tmp_db):
    """2 回実行で 2 回目は no-op."""
    n1 = migrate_up(tmp_db, verbose=False)
    n2 = migrate_up(tmp_db, verbose=False)
    assert n1 >= 1
    assert n2 == 0


def test_migrate_status_returns_applied(tmp_db):
    migrate_up(tmp_db, verbose=False)
    rows = migrate_status(tmp_db)
    assert len(rows) >= 1
    assert all("version" in r for r in rows)
    assert all("applied_at" in r for r in rows)
    assert all("checksum" in r for r in rows)
    assert any(r["version"] == "000_schema_migrations" for r in rows)


def test_migrate_checksum_recorded_correctly(tmp_db):
    migrate_up(tmp_db, verbose=False)
    rows = migrate_status(tmp_db)
    rec = next(r for r in rows if r["version"] == "000_schema_migrations")
    # 実ファイルの checksum と一致
    project = Path(__file__).resolve().parent.parent.parent
    sql_path = project / "db" / "migrations" / "000_schema_migrations.sql"
    expected = hashlib.sha256(sql_path.read_bytes()).hexdigest()
    assert rec["checksum"] == expected


def test_down_migrations_exist():
    """各 .sql に対応する .down.sql が存在することを保証."""
    project = Path(__file__).resolve().parent.parent.parent
    migrations_dir = project / "db" / "migrations"
    for sql in migrations_dir.glob("*.sql"):
        if sql.name.endswith(".down.sql"):
            continue
        down = sql.parent / f"{sql.stem}.down.sql"
        assert down.exists(), f"missing down migration: {down}"
