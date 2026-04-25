"""state_repo の integration テスト."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT))

from db.migrate import migrate_up
from db.repositories.state_repo import (
    query_snapshots,
    read_latest_snapshot,
    sync_db_to_state_dict,
    sync_state_to_db,
    write_state_snapshot,
)


@pytest.fixture
def fresh_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    migrate_up(path, verbose=False)
    yield path
    for ext in ("", "-wal", "-shm"):
        p = path.parent / f"{path.name}{ext}"
        if p.exists():
            p.unlink()


def _make_demo_state(equity: float = 10000.0,
                      btc_pos: bool = False,
                      ach_pos: dict | None = None) -> dict:
    """demo_runner.py 形式の state dict を作る."""
    return {
        "version": "2.5",
        "version_name": "気持ちマックス v2.5",
        "started_at": "2026-04-25T00:00:00+00:00",
        "last_update": "2026-04-25T08:40:00+00:00",
        "btc_part": {
            "cash": 4000.0 if not btc_pos else 0.0,
            "btc_qty": 0.05 if btc_pos else 0.0,
            "position": btc_pos,
            "entry_price": 50000.0 if btc_pos else 0.0,
            "last_btc_price": 55000.0 if btc_pos else 0.0,
        },
        "ach_part": {
            "cash": 3000.0 if not ach_pos else 0.0,
            "positions": ach_pos or {},
            "virtual_equity": 3000.0,
        },
        "usdt_part": {"cash": 3000.0},
        "total_equity": equity,
        "max_dd_observed": 5.5,
    }


def test_write_and_read_basic(fresh_db):
    state = _make_demo_state(equity=10500.0)
    sid = write_state_snapshot(state, mode="sim", db_path=fresh_db)
    assert sid > 0

    snap = read_latest_snapshot(mode="sim", db_path=fresh_db)
    assert snap is not None
    assert snap["equity"] == 10500.0
    assert snap["mode"] == "sim"


def test_write_with_btc_position(fresh_db):
    state = _make_demo_state(equity=11000.0, btc_pos=True)
    sid = write_state_snapshot(state, mode="sim", db_path=fresh_db)
    snap = read_latest_snapshot(mode="sim", db_path=fresh_db)
    # BTC ポジション 1 件
    assert len(snap["positions"]) == 1
    assert snap["positions"][0]["symbol"] == "BTC"


def test_write_with_ach_positions(fresh_db):
    ach_pos = {
        "ETH": {"qty": 1.0, "entry_price": 3000.0,
                "current_price": 3300.0, "unrealized_pnl": 300.0},
        "SOL": {"qty": 10.0, "entry_price": 100.0,
                "current_price": 105.0, "unrealized_pnl": 50.0},
    }
    state = _make_demo_state(equity=12000.0, ach_pos=ach_pos)
    sid = write_state_snapshot(state, mode="sim", db_path=fresh_db)
    snap = read_latest_snapshot(mode="sim", db_path=fresh_db)
    syms = sorted(p["symbol"] for p in snap["positions"])
    assert syms == ["ETH", "SOL"]


def test_query_snapshots_filters(fresh_db):
    for eq in [10000.0, 10100.0, 10200.0]:
        write_state_snapshot(_make_demo_state(equity=eq),
                              mode="sim", db_path=fresh_db)

    df = query_snapshots(mode="sim", db_path=fresh_db)
    assert len(df) == 3
    # 最新値が先頭 (DESC)
    assert df.iloc[0]["equity"] in [10000.0, 10100.0, 10200.0]


def test_sync_state_to_db_alias(fresh_db):
    """sync_state_to_db は write_state_snapshot のエイリアス."""
    state = _make_demo_state(equity=12345.67)
    sid = sync_state_to_db(state, mode="sim", db_path=fresh_db)
    snap = read_latest_snapshot(mode="sim", db_path=fresh_db)
    assert snap["equity"] == pytest.approx(12345.67)


def test_sync_db_to_state_dict(fresh_db):
    """DB → state-like dict の変換."""
    state = _make_demo_state(equity=11111.11)
    write_state_snapshot(state, mode="sim", db_path=fresh_db)
    out = sync_db_to_state_dict(mode="sim", db_path=fresh_db)
    assert out is not None
    assert out["total_equity"] == pytest.approx(11111.11)
    assert "raw" in out


def test_empty_db_returns_none(fresh_db):
    assert read_latest_snapshot(mode="sim", db_path=fresh_db) is None
    assert sync_db_to_state_dict(mode="sim", db_path=fresh_db) is None


def test_cents_round_trip(fresh_db):
    """cent 整数化で round-off せず元の小数点まで戻る."""
    state = _make_demo_state(equity=10000.66)  # 実 SIM の値
    write_state_snapshot(state, mode="sim", db_path=fresh_db)
    snap = read_latest_snapshot(mode="sim", db_path=fresh_db)
    assert snap["equity"] == pytest.approx(10000.66, abs=0.01)
