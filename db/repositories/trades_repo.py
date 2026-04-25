"""trades + trade_fills + snapshots リポジトリ.

PnL は cent 整数化 (round-off 防止)。
mode='sim' (本番 SIM) と mode='backtest' (iter スクリプト) を単一テーブルで管理。

Usage:
    from db.repositories.trades_repo import open_trade, close_trade, record_snapshot

    tid = open_trade(symbol='BTC/USDT', side='long', qty=1.0, entry_price=50000,
                     entry_ts=1577836800000, mode='sim', run_id=None)
    close_trade(tid, exit_price=55000, exit_ts=1577923200000, fee_cents=50)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT))

from db.connection import begin_immediate, get_connection


def _new_id() -> str:
    """trade_id / fill_id 用の短い ULID 風 ID."""
    try:
        import ulid as ulid_mod
        return str(ulid_mod.new())
    except ImportError:
        ts = int(time.time() * 1000)
        return f"T{ts:013X}{os.urandom(8).hex().upper()}"


def open_trade(
    symbol: str,
    side: str,
    qty: float,
    entry_price: float,
    entry_ts: int,
    *,
    mode: str = "sim",
    run_id: str | None = None,
    leverage: float = 1.0,
    reason_in: str | None = None,
    db_path: str | Path = "data/kimochi.db",
    conn: sqlite3.Connection | None = None,
) -> str:
    """新規トレードを open status で記録。trade_id を返す."""
    if mode not in ("sim", "backtest"):
        raise ValueError(f"invalid mode: {mode}")
    own_conn = conn is None
    if own_conn:
        conn = get_connection(db_path)
    trade_id = _new_id()
    try:
        with begin_immediate(conn):
            conn.execute("""
                INSERT INTO trades (
                    trade_id, mode, run_id, symbol, side, status,
                    qty_initial, qty_remaining, avg_entry_price,
                    entry_ts, leverage, reason_in, created_at
                ) VALUES (?,?,?,?,?,'open', ?,?,?, ?,?,?, ?)
            """, (
                trade_id, mode, run_id, symbol, side,
                qty, qty, entry_price,
                entry_ts, leverage, reason_in,
                int(time.time() * 1000),
            ))
    finally:
        if own_conn:
            conn.close()
    return trade_id


def close_trade(
    trade_id: str,
    exit_price: float,
    exit_ts: int,
    *,
    fee_cents: int = 0,
    slippage: float = 0.0,
    reason_out: str | None = None,
    db_path: str | Path = "data/kimochi.db",
    conn: sqlite3.Connection | None = None,
) -> None:
    """完全クローズ (qty_remaining=0、status='closed', PnL 算出)."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection(db_path)
    try:
        with begin_immediate(conn):
            t = conn.execute(
                "SELECT side, qty_remaining, avg_entry_price "
                "FROM trades WHERE trade_id=?",
                (trade_id,),
            ).fetchone()
            if t is None:
                raise ValueError(f"trade not found: {trade_id}")
            qty = t["qty_remaining"]
            entry = t["avg_entry_price"]
            sign = 1 if t["side"] == "long" else -1
            pnl_quote = (exit_price - entry) * qty * sign
            pnl_cents = int(round(pnl_quote * 100)) - fee_cents

            conn.execute("""
                UPDATE trades SET
                    status='closed',
                    qty_remaining=0,
                    avg_exit_price=?,
                    exit_ts=?,
                    pnl_quote_cents=?,
                    fee_paid_cents=fee_paid_cents + ?,
                    slippage=slippage + ?,
                    reason_out=?
                WHERE trade_id=?
            """, (
                exit_price, exit_ts, pnl_cents,
                fee_cents, slippage, reason_out, trade_id,
            ))
    finally:
        if own_conn:
            conn.close()


def record_fill(
    trade_id: str,
    fill_ts: int,
    fill_side: str,
    qty: float,
    price: float,
    *,
    fee_cents: int = 0,
    db_path: str | Path = "data/kimochi.db",
    conn: sqlite3.Connection | None = None,
) -> str:
    """部分決済の fill を記録 (trade.qty_remaining も減算)."""
    if fill_side not in ("entry", "exit"):
        raise ValueError(f"invalid fill_side: {fill_side}")
    own_conn = conn is None
    if own_conn:
        conn = get_connection(db_path)
    fill_id = _new_id()
    try:
        with begin_immediate(conn):
            conn.execute(
                "INSERT INTO trade_fills (fill_id, trade_id, fill_ts, "
                "fill_side, qty, price, fee_cents) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (fill_id, trade_id, fill_ts, fill_side, qty, price, fee_cents),
            )
            if fill_side == "exit":
                conn.execute(
                    "UPDATE trades SET qty_remaining=qty_remaining-?, "
                    "status=CASE WHEN qty_remaining-?<=1e-9 THEN 'closed' "
                    "ELSE 'partial' END, "
                    "fee_paid_cents=fee_paid_cents+? "
                    "WHERE trade_id=?",
                    (qty, qty, fee_cents, trade_id),
                )
    finally:
        if own_conn:
            conn.close()
    return fill_id


def record_snapshot(
    ts: int,
    mode: str,
    equity: float,
    cash: float,
    *,
    unrealized_pnl: float | None = None,
    realized_pnl: float | None = None,
    drawdown_pct: float | None = None,
    notes: str | None = None,
    db_path: str | Path = "data/kimochi.db",
    conn: sqlite3.Connection | None = None,
) -> int:
    """snapshot を記録。snapshot_id (autoincrement) を返す."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection(db_path)
    try:
        with begin_immediate(conn):
            cur = conn.execute("""
                INSERT INTO snapshots (
                    ts, mode, equity_cents, cash_cents,
                    unrealized_pnl_cents, realized_pnl_cents, drawdown_pct, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ts, mode,
                int(round(equity * 100)),
                int(round(cash * 100)),
                int(round(unrealized_pnl * 100)) if unrealized_pnl is not None else None,
                int(round(realized_pnl * 100)) if realized_pnl is not None else None,
                drawdown_pct, notes,
            ))
            snapshot_id = cur.lastrowid
    finally:
        if own_conn:
            conn.close()
    return snapshot_id


def query_trades(
    mode: str | None = None,
    run_id: str | None = None,
    symbol: str | None = None,
    *,
    db_path: str | Path = "data/kimochi.db",
) -> pd.DataFrame:
    conn = get_connection(db_path, readonly=True)
    try:
        clauses, params = [], []
        if mode:
            clauses.append("mode = ?"); params.append(mode)
        if run_id:
            clauses.append("run_id = ?"); params.append(run_id)
        if symbol:
            clauses.append("symbol = ?"); params.append(symbol)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        return pd.read_sql(
            f"SELECT * FROM trades {where} ORDER BY entry_ts DESC",
            conn, params=params,
        )
    finally:
        conn.close()
