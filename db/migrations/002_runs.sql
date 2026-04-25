-- 002_runs.sql
-- バックテスト/SIM 実行結果の中央テーブル (groovy v4 拡張版)

-- コストモデル正規化 (fair comparison)
CREATE TABLE IF NOT EXISTS cost_models (
  cost_model_id        TEXT PRIMARY KEY,
  fee_bps              REAL NOT NULL,
  slip_bps             REAL NOT NULL,
  funding_bps_per_8h   REAL DEFAULT 0,
  notes                TEXT
);

INSERT OR IGNORE INTO cost_models (cost_model_id, fee_bps, slip_bps, funding_bps_per_8h, notes)
VALUES
  ('binance_spot_taker_v1', 10, 5, 0,
   'Binance spot taker 0.10%/0.05% (kimochi-max default)'),
  ('binance_spot_buyhold_v1', 6, 3, 0,
   'BTC buy&hold 比較用 (元コード一致)'),
  ('binance_spot_maker_v1', 2, 5, 0,
   'Binance spot maker 0.020%/slip 0.05% (v2.4 maker_effective_fee 想定)');

CREATE TABLE IF NOT EXISTS runs (
  run_id                  TEXT PRIMARY KEY,                  -- ULID 推奨
  parent_run_id           TEXT,                              -- FK to runs(run_id)
  trial_group_id          TEXT NOT NULL,                     -- 同 grid 探索の括り
  n_trials_in_group       INTEGER NOT NULL DEFAULT 1,        -- 多重検定の分母
  run_type                TEXT NOT NULL CHECK (run_type IN
                            ('grid_search','wf_validation','oos_test',
                             'bear_test','production_sim','single_backtest')),
  strategy_id             TEXT NOT NULL,
  script_name             TEXT,
  git_sha                 TEXT NOT NULL,
  params_json             TEXT NOT NULL,
  -- generated columns (JSON1)
  param_lb                INTEGER GENERATED ALWAYS AS (json_extract(params_json,'$.LB')) VIRTUAL,
  param_top_n             INTEGER GENERATED ALWAYS AS (json_extract(params_json,'$.ACH_TOP_N')) VIRTUAL,
  param_fee               REAL GENERATED ALWAYS AS (json_extract(params_json,'$.FEE')) VIRTUAL,
  param_slip              REAL GENERATED ALWAYS AS (json_extract(params_json,'$.SLIP')) VIRTUAL,
  -- canonical hash (Python 側で sort_keys + SHA1)
  params_canonical_hash   TEXT NOT NULL,
  universe_hash           TEXT NOT NULL,
  universe_json           TEXT NOT NULL,
  period_start            TEXT NOT NULL,
  period_end              TEXT NOT NULL,
  -- 集計 metrics (0-1 スケール、`_pct` サフィックス禁止)
  cagr                    REAL,
  max_dd                  REAL,
  sharpe                  REAL,
  sortino                 REAL,
  calmar                  REAL,
  total_ret               REAL,
  n_trades                INTEGER,
  win_rate                REAL,
  final_equity            REAL,
  initial_equity          REAL,
  -- 統計指標 (DSR/PBO 計算用)
  sharpe_se               REAL,                              -- bootstrap SE
  skewness                REAL,
  kurtosis                REAL,
  benchmark_id            TEXT,
  cost_model_id           TEXT,
  notes                   TEXT,
  created_at              INTEGER NOT NULL,
  FOREIGN KEY (parent_run_id) REFERENCES runs(run_id),
  FOREIGN KEY (cost_model_id) REFERENCES cost_models(cost_model_id)
);

CREATE INDEX IF NOT EXISTS idx_runs_parent
  ON runs(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_runs_trial_group
  ON runs(trial_group_id);
CREATE INDEX IF NOT EXISTS idx_runs_type_strategy
  ON runs(run_type, strategy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_lb_topn
  ON runs(param_lb, param_top_n) WHERE run_type='grid_search';
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_dedup
  ON runs(strategy_id, params_canonical_hash, universe_hash, period_start, period_end);

CREATE TABLE IF NOT EXISTS run_yearly (
  run_id    TEXT NOT NULL,
  year      INTEGER NOT NULL,
  ret_pct   REAL NOT NULL,
  PRIMARY KEY (run_id, year),
  FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS run_periods (
  run_id        TEXT NOT NULL,
  period_label  TEXT NOT NULL,
  period_start  TEXT NOT NULL,
  period_end    TEXT NOT NULL,
  cagr          REAL,
  max_dd        REAL,
  sharpe        REAL,
  PRIMARY KEY (run_id, period_label),
  FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
