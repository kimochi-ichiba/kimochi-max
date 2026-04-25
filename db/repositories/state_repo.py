"""SIM state リポジトリ.

demo_runner.py の `state.json` (BTC/ACH/USDT 3 部分構造) を
SQLite (snapshots + snapshot_positions + positions テーブル) と双方向で同期。

Phase 5 dual-write の核。JSON が primary、DB が secondary という設計で、
DB 障害時も JSON 書き込みは継続する。

Usage:
    from db.repositories.state_repo import (
        write_state_snapshot, read_latest_snapshot,
        sync_state_to_db, sync_db_to_state_dict,
    )

    # 既存 state dict (demo_state.json の中身) を DB スナップショットへ
    snapshot_id = write_state_snapshot(state_dict, mode='sim')

    # DB から最新 state を取得
    state = sync_db_to_state_dict(mode='sim')
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT))

from db.connection import begin_immediate, get_connection


def _to_cents(v: float | None) -> int | None:
    """金額 (USD) を cent 整数 (round-off 防止)."""
    if v is None:
        return None
    return int(round(float(v) * 100))


def _from_cents(v: int | None) -> float | None:
    if v is None:
        return None
    return v / 100.0


# ─────────────────────────────
# write
# ─────────────────────────────
def write_state_snapshot(
    state: dict[str, Any],
    *,
    mode: str = "sim",
    db_path: str | Path = "data/kimochi.db",
    conn: sqlite3.Connection | None = None,
) -> int:
    """demo_runner.py 形式の state dict を 1 件の snapshot として記録.

    state の想定構造 (demo_runner.py):
        {
            "version": "2.5",
            "btc_part": {"cash": ..., "btc_qty": ..., "position": ...},
            "ach_part": {"cash": ..., "positions": {sym: {...}}, ...},
            "usdt_part": {"cash": ...},
            "total_equity": ...,
            "peak_equity": ...,
            "max_dd_observed": ...,
        }

    Returns: snapshot_id (autoincrement)
    """
    if mode not in ("sim", "backtest"):
        raise ValueError(f"invalid mode: {mode}")

    own_conn = conn is None
    if own_conn:
        conn = get_connection(db_path)

    # 集計
    btc = state.get("btc_part", {})
    ach = state.get("ach_part", {})
    usdt = state.get("usdt_part", {})
    total_equity = state.get("total_equity", 0)
    cash_total = (btc.get("cash", 0) or 0) + (ach.get("cash", 0) or 0) + \
                 (usdt.get("cash", 0) or 0)

    # last_update を ts に変換
    ts_iso = state.get("last_update") or state.get("started_at")
    if ts_iso:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
            ts_ms = int(dt.timestamp() * 1000)
        except Exception:
            ts_ms = int(time.time() * 1000)
    else:
        ts_ms = int(time.time() * 1000)

    notes_json = json.dumps({
        "version": state.get("version"),
        "ach_strategy": ach.get("strategy"),
        "n_milestone_btc_exits": state.get("n_milestone_btc_exits", 0),
        "n_milestone_ach_exits": state.get("n_milestone_ach_exits", 0),
    }, ensure_ascii=False)

    drawdown_pct = state.get("max_dd_observed")

    try:
        with begin_immediate(conn):
            cur = conn.execute("""
                INSERT INTO snapshots (
                    ts, mode, equity_cents, cash_cents,
                    unrealized_pnl_cents, realized_pnl_cents,
                    drawdown_pct, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ts_ms, mode,
                _to_cents(total_equity),
                _to_cents(cash_total),
                None,  # unrealized 集計は positions から計算するが省略
                None,  # realized も省略 (trades 側で集計可)
                drawdown_pct,
                notes_json,
            ))
            snapshot_id = cur.lastrowid

            # positions (BTC + ACH 各銘柄) を snapshot_positions へ
            rows = []
            # BTC 部分
            if btc.get("position") and btc.get("btc_qty", 0) > 0:
                rows.append((
                    snapshot_id, "BTC", "long",
                    float(btc["btc_qty"]),
                    float(btc.get("entry_price", 0) or 0),
                    float(btc.get("last_btc_price", 0) or 0),
                    _to_cents((btc.get("last_btc_price", 0) or 0) * btc["btc_qty"]
                              - (btc.get("entry_price", 0) or 0) * btc["btc_qty"]),
                ))
            # ACH 各銘柄
            for sym, pos in (ach.get("positions") or {}).items():
                if not isinstance(pos, dict):
                    continue
                qty = pos.get("qty", 0) or 0
                if qty <= 0:
                    continue
                cur_price = pos.get("current_price",
                                     pos.get("entry_price", 0)) or 0
                rows.append((
                    snapshot_id, sym, "long",
                    float(qty),
                    float(pos.get("entry_price", 0) or 0),
                    float(cur_price),
                    _to_cents(pos.get("unrealized_pnl", 0)),
                ))

            if rows:
                conn.executemany(
                    "INSERT INTO snapshot_positions (snapshot_id, symbol, side, "
                    "qty, entry_price, current_price, unrealized_pnl_cents) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
    finally:
        if own_conn:
            conn.close()
    return snapshot_id


# ─────────────────────────────
# read
# ─────────────────────────────
def read_latest_snapshot(
    *,
    mode: str = "sim",
    db_path: str | Path = "data/kimochi.db",
) -> dict | None:
    """最新 snapshot を dict で返す。なければ None."""
    conn = get_connection(db_path, readonly=True)
    try:
        r = conn.execute(
            "SELECT * FROM snapshots WHERE mode=? "
            "ORDER BY ts DESC LIMIT 1",
            (mode,),
        ).fetchone()
        if not r:
            return None
        d = dict(r)
        d["equity"] = _from_cents(d["equity_cents"])
        d["cash"] = _from_cents(d["cash_cents"])
        d["unrealized_pnl"] = _from_cents(d.get("unrealized_pnl_cents"))
        d["realized_pnl"] = _from_cents(d.get("realized_pnl_cents"))

        # positions
        pos_rows = conn.execute(
            "SELECT * FROM snapshot_positions WHERE snapshot_id=?",
            (d["snapshot_id"],),
        ).fetchall()
        d["positions"] = [
            {**dict(p), "unrealized_pnl":
             _from_cents(p["unrealized_pnl_cents"])}
            for p in pos_rows
        ]
        return d
    finally:
        conn.close()


def query_snapshots(
    *,
    mode: str = "sim",
    start_ts: int | None = None,
    end_ts: int | None = None,
    limit: int | None = None,
    db_path: str | Path = "data/kimochi.db",
):
    """期間/件数でフィルタした snapshots を pandas DataFrame で返す."""
    import pandas as pd
    conn = get_connection(db_path, readonly=True)
    try:
        clauses = ["mode = ?"]
        params: list[Any] = [mode]
        if start_ts is not None:
            clauses.append("ts >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("ts <= ?")
            params.append(end_ts)
        sql = "SELECT * FROM snapshots WHERE " + " AND ".join(clauses) + \
              " ORDER BY ts DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        df = pd.read_sql(sql, conn, params=params)
        if not df.empty:
            df["equity"] = df["equity_cents"] / 100
            df["cash"] = df["cash_cents"] / 100
        return df
    finally:
        conn.close()


# ─────────────────────────────
# sync (high-level helper)
# ─────────────────────────────
def sync_state_to_db(
    state_dict: dict[str, Any],
    *,
    mode: str = "sim",
    db_path: str | Path = "data/kimochi.db",
) -> int:
    """state.json 由来 dict を DB へ 1 件 snapshot 化 (dual-write メイン関数).

    Phase 5 で demo_runner.py の save_state() の中から呼ばれる想定:
        # demo_runner.py 改修例
        def save_state(state):
            tmp = STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ...))   # 既存 JSON 書き込み (primary)
            tmp.replace(STATE_PATH)
            try:
                from db.repositories.state_repo import sync_state_to_db
                sync_state_to_db(state, mode='sim')   # DB dual-write (secondary)
            except Exception as e:
                log(f'⚠️ DB dual-write failed: {e}')   # JSON は成功しているので継続
    """
    return write_state_snapshot(state_dict, mode=mode, db_path=db_path)


def sync_db_to_state_dict(
    *,
    mode: str = "sim",
    db_path: str | Path = "data/kimochi.db",
) -> dict | None:
    """最新 snapshot を demo_runner.py 形式の state dict に近い形へ変換.

    完全互換ではない (trades / equity_history 等は含まれない)。
    災害復旧時、最後の状態を確認するための参照用。
    """
    snap = read_latest_snapshot(mode=mode, db_path=db_path)
    if snap is None:
        return None
    return {
        "snapshot_id": snap["snapshot_id"],
        "ts": snap["ts"],
        "total_equity": snap["equity"],
        "cash_total": snap["cash"],
        "max_dd_observed": snap["drawdown_pct"],
        "positions": snap["positions"],
        "raw": snap,
    }
