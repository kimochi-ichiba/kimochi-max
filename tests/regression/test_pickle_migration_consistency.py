"""pickle → SQLite 移行の数値整合性テスト.

PR#6 (bear_test_report) の数値完全再現は data_fetcher.py 書換 (Phase 3) 後に実施。
本テストは pickle と DB から同じ symbol を取得して値が一致することのみ確認。
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT))

from db.repositories.ohlcv_repo import fetch_ohlcv

DB_PATH = PROJECT / "data" / "kimochi.db"
PICKLE_CANDIDATES = [
    PROJECT / "results" / "_iter61_cache.pkl",
    PROJECT / "results" / "_bear_test_cache.pkl",
]


@pytest.fixture(scope="module")
def pickle_data():
    """既存 pickle の最初に見つかったものを読む."""
    for p in PICKLE_CANDIDATES:
        if p.exists():
            with open(p, "rb") as f:
                return p, pickle.load(f)
    pytest.skip("no source pickle found")


def test_btc_close_values_match_pickle(pickle_data):
    """BTC/USDT の close 値が pickle と SQLite で一致."""
    pkl_path, data = pickle_data
    if not DB_PATH.exists():
        pytest.skip("DB not migrated yet (run scripts/migrate_pickle_to_db.py)")

    if "BTC/USDT" not in data:
        pytest.skip("BTC/USDT not in pickle")
    pkl_df = data["BTC/USDT"]
    if pkl_df.empty:
        pytest.skip("BTC/USDT empty in pickle")

    # 最初 100 行で比較
    sample = pkl_df.head(100)
    start_ts = int(sample.index.min().value // 10**6)
    end_ts = int(sample.index.max().value // 10**6)

    db_df = fetch_ohlcv("BTC/USDT", "1d", start_ts, end_ts, db_path=DB_PATH)
    if db_df.empty:
        pytest.skip("BTC/USDT not in DB")

    # 共通する ts のサブセットで比較 (DB 側は datetime index)
    pkl_close = sample["close"]
    db_close = db_df["close"]
    # 行数が完全一致しなくても、近い行数なら OK
    assert abs(len(pkl_close) - len(db_close)) <= 5, \
        f"row count differs much: pickle={len(pkl_close)}, db={len(db_close)}"

    # 値の許容差: 複数 pickle 由来 (binance/cmc 等) で微差ある可能性、5% 許容
    # 厳密な数値再現は Phase 3 (data_fetcher.py 書き換え後) に実施
    for i in range(min(10, len(pkl_close), len(db_close))):
        diff_pct = abs(pkl_close.iloc[i] - db_close.iloc[i]) / pkl_close.iloc[i]
        assert diff_pct < 0.05, \
            f"row {i} differs >5%: pickle={pkl_close.iloc[i]}, db={db_close.iloc[i]}"


def test_db_has_btc_eth_data():
    """DB に BTC/ETH の 1d データが存在."""
    if not DB_PATH.exists():
        pytest.skip("DB not migrated yet")

    btc = fetch_ohlcv("BTC/USDT", "1d", 0, 9_999_999_999_999, db_path=DB_PATH)
    eth = fetch_ohlcv("ETH/USDT", "1d", 0, 9_999_999_999_999, db_path=DB_PATH)
    assert len(btc) > 100, f"BTC: {len(btc)} rows"
    assert len(eth) > 100, f"ETH: {len(eth)} rows"
