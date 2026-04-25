-- 001_init_ohlcv.sql
-- OHLCV 価格データ層 (groovy-sprouting-origami v4)

CREATE TABLE IF NOT EXISTS ohlcv (
  symbol           TEXT NOT NULL,        -- "BTC/USDT" CCXT 形式正規化
  timeframe        TEXT NOT NULL,
  ts               INTEGER NOT NULL,     -- UTC ms
  open             REAL NOT NULL,
  high             REAL NOT NULL,
  low              REAL NOT NULL,
  close            REAL NOT NULL,
  volume           REAL NOT NULL,
  quote_volume     REAL,
  trade_count      INTEGER,
  taker_buy_volume REAL,
  fetched_at       INTEGER NOT NULL,     -- UTC ms
  PRIMARY KEY (symbol, timeframe, ts)
) WITHOUT ROWID;

-- Covering index (carrying close/volume を含めて値読み込み高速化)
CREATE INDEX IF NOT EXISTS idx_ohlcv_ts_symbol_covering
  ON ohlcv(ts, symbol, timeframe, close, volume);

CREATE TABLE IF NOT EXISTS ohlcv_meta (
  symbol           TEXT NOT NULL,
  timeframe        TEXT NOT NULL,
  source           TEXT NOT NULL DEFAULT 'binance',
  earliest_ts      INTEGER,
  latest_ts        INTEGER,
  last_fetched_at  INTEGER,
  row_count        INTEGER,
  PRIMARY KEY (symbol, timeframe, source)
);

CREATE TABLE IF NOT EXISTS source_check_anomalies (
  symbol         TEXT NOT NULL,
  ts             INTEGER NOT NULL,
  binance_close  REAL,
  mexc_close     REAL,
  cg_close       REAL,
  cmc_close      REAL,
  max_diff_pct   REAL NOT NULL,
  resolved       INTEGER DEFAULT 0,
  checked_at     INTEGER NOT NULL,
  PRIMARY KEY (symbol, ts)
);
