"""OHLCV (価格データ) リポジトリ.

normalize_symbol 一元化 + JOIN 一発 fetch_universe + ON CONFLICT no-op skip upsert。

Usage:
    from db.repositories.ohlcv_repo import (
        normalize_symbol, upsert_ohlcv, fetch_universe, integrity_check
    )

    df = fetch_universe(['BTC/USDT','ETH/USDT'], '1d',
                        start_ts=1577836800000, end_ts=1735603200000)
    # df['BTC/USDT'] -> DataFrame indexed by ts
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT))

from db.connection import begin_immediate, get_connection


# ─────────────────────────────
# Symbol 正規化
# ─────────────────────────────
_KNOWN_QUOTES = ("USDT", "USDC", "BUSD", "BTC", "ETH", "JPY")


def normalize_symbol(s: str) -> str:
    """ "BTCUSDT", "BTC-USDT", "btc/usdt" → "BTC/USDT" に統一.

    既に "/" が入っていれば大文字化のみ。
    """
    if not s:
        return s
    s = s.upper().replace("-", "/").replace("_", "/")
    if "/" in s:
        return s
    # "BTCUSDT" → "BTC/USDT"
    for quote in _KNOWN_QUOTES:
        if s.endswith(quote) and len(s) > len(quote):
            base = s[: -len(quote)]
            return f"{base}/{quote}"
    return s


# ─────────────────────────────
# upsert
# ─────────────────────────────
_REQUIRED_COLS = ("open", "high", "low", "close", "volume")
_OPTIONAL_COLS = ("quote_volume", "trade_count", "taker_buy_volume")


def upsert_ohlcv(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    *,
    db_path: str | Path = "data/kimochi.db",
    conn: sqlite3.Connection | None = None,
) -> int:
    """DataFrame を ohlcv に UPSERT.

    Args:
        df: index=ts (UTC ms or datetime)、columns=open/high/low/close/volume + 任意
        symbol: CCXT 形式 (内部で normalize_symbol 適用)
        timeframe: "1d" 等
        conn: 外部接続を使う場合 (テスト用、トランザクション制御も呼び出し側で)

    Returns: insert+update された行数 (skip された no-op を除く近似)
    """
    if df.empty:
        return 0

    symbol = normalize_symbol(symbol)
    own_conn = conn is None
    if own_conn:
        conn = get_connection(db_path)

    # ts を UTC ms int に変換
    idx = df.index
    if isinstance(idx, pd.DatetimeIndex):
        ts_ms = (idx.astype("int64") // 10**6).tolist()
    elif pd.api.types.is_integer_dtype(df.index):
        ts_ms = idx.tolist()
    else:
        # 文字列等を datetime 経由で
        ts_ms = (pd.to_datetime(idx).astype("int64") // 10**6).tolist()

    fetched_at = int(time.time() * 1000)

    rows = []
    for ts, (_, r) in zip(ts_ms, df.iterrows()):
        rows.append((
            symbol, timeframe, int(ts),
            float(r["open"]), float(r["high"]), float(r["low"]),
            float(r["close"]), float(r["volume"]),
            float(r["quote_volume"]) if "quote_volume" in r and pd.notna(r.get("quote_volume")) else None,
            int(r["trade_count"]) if "trade_count" in r and pd.notna(r.get("trade_count")) else None,
            float(r["taker_buy_volume"]) if "taker_buy_volume" in r and pd.notna(r.get("taker_buy_volume")) else None,
            fetched_at,
        ))

    sql = """
    INSERT INTO ohlcv (symbol, timeframe, ts, open, high, low, close, volume,
                       quote_volume, trade_count, taker_buy_volume, fetched_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(symbol, timeframe, ts) DO UPDATE SET
        close = excluded.close,
        volume = excluded.volume,
        fetched_at = excluded.fetched_at
    WHERE excluded.close != ohlcv.close
       OR excluded.volume != ohlcv.volume
    """

    try:
        with begin_immediate(conn):
            conn.executemany(sql, rows)
        update_meta(symbol, timeframe, conn=conn)
    finally:
        if own_conn:
            conn.close()
    return len(rows)


# ─────────────────────────────
# fetch
# ─────────────────────────────
def fetch_universe(
    symbols: list[str],
    timeframe: str,
    start_ts: int,
    end_ts: int,
    *,
    db_path: str | Path = "data/kimochi.db",
    conn: sqlite3.Connection | None = None,
) -> dict[str, pd.DataFrame]:
    """複数銘柄を JOIN 一発で取得し、シンボル別 dict[symbol, DataFrame] を返す.

    DataFrame は index=ts (datetime)、columns=open/high/low/close/volume + ext.
    """
    if not symbols:
        return {}
    symbols = [normalize_symbol(s) for s in symbols]
    own_conn = conn is None
    if own_conn:
        conn = get_connection(db_path, readonly=True)

    placeholders = ",".join("?" * len(symbols))
    try:
        df = pd.read_sql(
            f"""
            SELECT symbol, ts, open, high, low, close, volume,
                   quote_volume, trade_count, taker_buy_volume
            FROM ohlcv
            WHERE timeframe = ? AND symbol IN ({placeholders})
              AND ts BETWEEN ? AND ?
            ORDER BY symbol, ts
            """,
            conn,
            params=[timeframe, *symbols, start_ts, end_ts],
        )
    finally:
        if own_conn:
            conn.close()

    if df.empty:
        return {}

    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    out: dict[str, pd.DataFrame] = {}
    for sym, g in df.groupby("symbol"):
        sub = g.drop(columns="symbol").set_index("ts").sort_index()
        out[sym] = sub
    return out


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    start_ts: int,
    end_ts: int,
    *,
    db_path: str | Path = "data/kimochi.db",
    conn: sqlite3.Connection | None = None,
) -> pd.DataFrame:
    """単一銘柄を取得."""
    res = fetch_universe([symbol], timeframe, start_ts, end_ts,
                         db_path=db_path, conn=conn)
    sym = normalize_symbol(symbol)
    return res.get(sym, pd.DataFrame())


# ─────────────────────────────
# meta
# ─────────────────────────────
def update_meta(
    symbol: str,
    timeframe: str,
    *,
    source: str = "binance",
    db_path: str | Path = "data/kimochi.db",
    conn: sqlite3.Connection | None = None,
) -> None:
    """ohlcv_meta を最新値で更新."""
    symbol = normalize_symbol(symbol)
    own_conn = conn is None
    if own_conn:
        conn = get_connection(db_path)

    try:
        agg = conn.execute(
            "SELECT MIN(ts), MAX(ts), MAX(fetched_at), COUNT(*) "
            "FROM ohlcv WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()
        if agg is None or agg[3] == 0:
            return
        earliest, latest, last_fetched, n = agg

        with begin_immediate(conn):
            conn.execute("""
                INSERT INTO ohlcv_meta (symbol, timeframe, source,
                                         earliest_ts, latest_ts, last_fetched_at, row_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timeframe, source) DO UPDATE SET
                    earliest_ts = excluded.earliest_ts,
                    latest_ts = excluded.latest_ts,
                    last_fetched_at = excluded.last_fetched_at,
                    row_count = excluded.row_count
            """, (symbol, timeframe, source, earliest, latest, last_fetched, n))
    finally:
        if own_conn:
            conn.close()


def get_meta(
    symbol: str,
    timeframe: str,
    *,
    source: str = "binance",
    db_path: str | Path = "data/kimochi.db",
    conn: sqlite3.Connection | None = None,
) -> dict | None:
    """meta 行を dict で返す。なければ None."""
    symbol = normalize_symbol(symbol)
    own_conn = conn is None
    if own_conn:
        conn = get_connection(db_path, readonly=True)
    try:
        r = conn.execute(
            "SELECT * FROM ohlcv_meta WHERE symbol=? AND timeframe=? AND source=?",
            (symbol, timeframe, source),
        ).fetchone()
        return dict(r) if r else None
    finally:
        if own_conn:
            conn.close()


# ─────────────────────────────
# integrity check
# ─────────────────────────────
def integrity_check(
    symbol: str,
    timeframe: str = "1d",
    *,
    db_path: str | Path = "data/kimochi.db",
    conn: sqlite3.Connection | None = None,
) -> bool:
    """連続性チェック (timeframe ごとに ts 間隔が想定どおりか)."""
    symbol = normalize_symbol(symbol)
    own_conn = conn is None
    if own_conn:
        conn = get_connection(db_path, readonly=True)

    try:
        rows = conn.execute(
            "SELECT ts FROM ohlcv WHERE symbol=? AND timeframe=? "
            "ORDER BY ts",
            (symbol, timeframe),
        ).fetchall()
    finally:
        if own_conn:
            conn.close()

    if len(rows) < 2:
        return False  # データ不足

    expected_step_ms = {
        "1m": 60_000, "5m": 300_000, "15m": 900_000,
        "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
    }.get(timeframe)
    if expected_step_ms is None:
        return True  # 未知 timeframe はスキップ

    # 想定間隔と一致する diff 比率が 95% 以上ならOK (祝日・取引停止等の許容)
    diffs = [rows[i][0] - rows[i - 1][0] for i in range(1, len(rows))]
    correct = sum(1 for d in diffs if d == expected_step_ms)
    ratio = correct / len(diffs) if diffs else 0
    return ratio >= 0.90  # 取引所メンテで欠落することがあるので 90%
