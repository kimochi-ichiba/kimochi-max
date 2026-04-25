-- 003_state_trades.sql
-- trades + snapshots + positions (SIM/backtest 単一テーブル、cent 整数化)

CREATE TABLE IF NOT EXISTS trades (
  trade_id            TEXT PRIMARY KEY,
  mode                TEXT NOT NULL CHECK(mode IN ('sim','backtest')),
  run_id              TEXT,                                  -- FK to runs (CASCADE)
  symbol              TEXT NOT NULL,
  side                TEXT NOT NULL,
  status              TEXT NOT NULL CHECK(status IN ('open','partial','closed')),
  qty_initial         REAL NOT NULL,
  qty_remaining       REAL NOT NULL,
  avg_entry_price     REAL NOT NULL,
  avg_exit_price      REAL,
  entry_ts            INTEGER NOT NULL,
  exit_ts             INTEGER,
  pnl_quote_cents     INTEGER,                               -- cent 整数
  quote_ccy           TEXT NOT NULL DEFAULT 'USDT',
  fee_paid_cents      INTEGER DEFAULT 0,
  slippage            REAL DEFAULT 0,
  leverage            REAL DEFAULT 1.0,
  reason_in           TEXT,
  reason_out          TEXT,
  created_at          INTEGER NOT NULL,
  FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trades_mode_run
  ON trades(mode, run_id);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts
  ON trades(symbol, entry_ts);

CREATE TABLE IF NOT EXISTS trade_fills (
  fill_id             TEXT PRIMARY KEY,
  trade_id            TEXT NOT NULL,
  fill_ts             INTEGER NOT NULL,
  fill_side           TEXT NOT NULL CHECK(fill_side IN ('entry','exit')),
  qty                 REAL NOT NULL,
  price               REAL NOT NULL,
  fee_cents           INTEGER DEFAULT 0,
  FOREIGN KEY (trade_id) REFERENCES trades(trade_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                     INTEGER NOT NULL,
  mode                   TEXT NOT NULL,
  equity_cents           INTEGER NOT NULL,                   -- cent 整数
  cash_cents             INTEGER NOT NULL,
  unrealized_pnl_cents   INTEGER,
  realized_pnl_cents     INTEGER,
  drawdown_pct           REAL,
  notes                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshots_mode_ts
  ON snapshots(mode, ts);

CREATE TABLE IF NOT EXISTS snapshot_positions (
  snapshot_id          INTEGER NOT NULL,
  symbol               TEXT NOT NULL,
  side                 TEXT NOT NULL,
  qty                  REAL NOT NULL,
  entry_price          REAL NOT NULL,
  current_price        REAL NOT NULL,
  unrealized_pnl_cents INTEGER,
  PRIMARY KEY (snapshot_id, symbol),
  FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_snap_pos_symbol
  ON snapshot_positions(symbol, snapshot_id);

CREATE TABLE IF NOT EXISTS positions (
  symbol         TEXT NOT NULL,
  mode           TEXT NOT NULL,
  side           TEXT NOT NULL,
  qty            REAL NOT NULL,
  entry_price    REAL NOT NULL,
  entry_ts       INTEGER NOT NULL,
  leverage       REAL DEFAULT 1.0,
  stop_loss      REAL,
  take_profit    REAL,
  trailing_high  REAL,
  updated_at     INTEGER NOT NULL,
  PRIMARY KEY (symbol, mode)
);
