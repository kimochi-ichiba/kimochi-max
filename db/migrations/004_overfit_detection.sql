-- 004_overfit_detection.sql
-- 過学習検出 core テーブル群 (groovy v4 ★最重要)

-- trade-level returns (DSR/PBO 計算の前提)
CREATE TABLE IF NOT EXISTS run_returns (
  run_id        TEXT NOT NULL,
  ts            INTEGER NOT NULL,                       -- daily UTC ms
  ret           REAL NOT NULL,                          -- 日次リターン (絶対値、0.01 = +1%)
  equity_cents  INTEGER NOT NULL,
  PRIMARY KEY (run_id, ts),
  FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
) WITHOUT ROWID;

-- Walk-Forward windows (anchored vs rolling、IS/OOS 構造保存)
CREATE TABLE IF NOT EXISTS wf_windows (
  run_id        TEXT NOT NULL,
  window_idx    INTEGER NOT NULL,
  scheme        TEXT NOT NULL CHECK(scheme IN ('anchored','rolling')),
  is_start      TEXT,
  is_end        TEXT,
  oos_start     TEXT,
  oos_end       TEXT,
  is_sharpe     REAL,
  oos_sharpe    REAL,
  is_cagr       REAL,
  oos_cagr      REAL,
  is_max_dd     REAL,
  oos_max_dd    REAL,
  PRIMARY KEY (run_id, window_idx),
  FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE VIEW IF NOT EXISTS v_wf_efficiency AS
  SELECT run_id,
         AVG(oos_sharpe / NULLIF(is_sharpe, 0)) AS oos_efficiency,
         COUNT(*) AS n_windows
  FROM wf_windows
  GROUP BY run_id;

-- regime 別評価 (bull +120% / bear +9.7% を一発で検出)
CREATE TABLE IF NOT EXISTS run_regimes (
  run_id      TEXT NOT NULL,
  regime      TEXT NOT NULL CHECK(regime IN ('bull','bear','chop')),
  regime_def  TEXT NOT NULL,                            -- "BTC_close < EMA200" 等
  n_days      INTEGER,
  cagr        REAL,
  sharpe      REAL,
  max_dd      REAL,
  PRIMARY KEY (run_id, regime),
  FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE VIEW IF NOT EXISTS v_regime_consistency AS
  SELECT run_id,
         MIN(sharpe) AS worst_regime_sharpe,
         MAX(sharpe) AS best_regime_sharpe,
         MIN(sharpe) / NULLIF(MAX(sharpe), 0) AS regime_consistency_score
  FROM run_regimes
  WHERE regime IN ('bull','bear')
  GROUP BY run_id;
-- score < 0.5 で「regime 依存 (bull 期だけ強い)」warning。
-- C3 はこれが約 0.08 になり一発で露呈。

-- レポート再現
CREATE TABLE IF NOT EXISTS reports (
  report_id    TEXT PRIMARY KEY,
  title        TEXT,
  created_at   INTEGER,
  pr_number    INTEGER,
  commit_sha   TEXT
);

CREATE TABLE IF NOT EXISTS report_runs (
  report_id   TEXT NOT NULL,
  run_id      TEXT NOT NULL,
  role        TEXT NOT NULL CHECK(role IN ('main','baseline','sensitivity','benchmark')),
  PRIMARY KEY (report_id, run_id),
  FOREIGN KEY (report_id) REFERENCES reports(report_id) ON DELETE CASCADE,
  FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

-- 監査メタ (existence_check, cache_integrity, logic_verify)
CREATE TABLE IF NOT EXISTS audit_results (
  audit_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT,
  audit_type    TEXT NOT NULL,                          -- 'existence_check' 等
  result        TEXT NOT NULL CHECK(result IN ('pass','fail','warning')),
  details_json  TEXT,
  audited_at    INTEGER NOT NULL,
  FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE SET NULL
);
