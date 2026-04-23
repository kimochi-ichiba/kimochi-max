"""
benchmarks.py — 4 種ベンチマーク戦略
====================================
すべて同じインターフェース (all_data, start, end, initial, **kwargs) で動き、
統一形式 {"name", "equity_curve", "trades"} を返す。

提供:
  - buy_hold_benchmark          : 対象銘柄を期間始めに全量買って終わりまで保持
  - monthly_dca_benchmark       : 月初に均等分割で対象銘柄を買い積みまし
  - trend_follow_benchmark      : BTC (or 対象) EMA200 上で保有 / 下で現金
  - random_entry_benchmark      : 毎リバランスで universe から top_n をランダム選択

all_data は dict[symbol -> DataFrame]、DataFrame は DatetimeIndex + "close" 列必須。
既存の _btc_trend_follow.run_btc_trend / _final_comparison.buy_hold /
iterate_to_90 の DCA 実装と同じロジックを、DataFetcher 非依存に再実装。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

# 手数料・スリッページ (iter59 / _btc_trend_follow と揃える)
DEFAULT_FEE = 0.0006
DEFAULT_SLIP = 0.0003


@dataclass
class BenchmarkResult:
    name: str
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "equity_curve": self.equity_curve,
            "trades": self.trades,
        }


def _slice_period(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if df.index.tz is not None:
        start_ts = start_ts.tz_localize(df.index.tz)
        end_ts = end_ts.tz_localize(df.index.tz)
    return df[(df.index >= start_ts) & (df.index <= end_ts)]


# ─────────────────────────────
# 1. Buy & Hold
# ─────────────────────────────
def buy_hold_benchmark(
    all_data: dict[str, pd.DataFrame],
    symbol: str = "BTC/USDT",
    start: str = "2020-01-01",
    end: str = "2024-12-31",
    initial: float = 10_000.0,
    fee: float = DEFAULT_FEE,
    slip: float = DEFAULT_SLIP,
) -> BenchmarkResult:
    df = _slice_period(all_data[symbol], start, end)
    if df.empty:
        return BenchmarkResult(name=f"buy_hold_{symbol}")

    entry_price = float(df["close"].iloc[0]) * (1.0 + slip)
    qty = initial * (1.0 - fee) / entry_price
    equity: list[dict[str, Any]] = []
    for ts, row in df.iterrows():
        equity.append({"ts": ts, "equity": qty * float(row["close"])})
    exit_price = float(df["close"].iloc[-1]) * (1.0 - slip)
    final = qty * exit_price * (1.0 - fee)
    equity[-1]["equity"] = final
    trades = [
        {
            "symbol": symbol,
            "entry_ts": df.index[0],
            "exit_ts": df.index[-1],
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": final - initial,
            "won": final > initial,
            "side": "long",
        }
    ]
    return BenchmarkResult(
        name=f"buy_hold_{symbol}", equity_curve=equity, trades=trades
    )


# ─────────────────────────────
# 2. 毎月 DCA
# ─────────────────────────────
def monthly_dca_benchmark(
    all_data: dict[str, pd.DataFrame],
    symbol: str = "BTC/USDT",
    start: str = "2020-01-01",
    end: str = "2024-12-31",
    initial: float = 10_000.0,
    fee: float = DEFAULT_FEE,
    slip: float = DEFAULT_SLIP,
) -> BenchmarkResult:
    df = _slice_period(all_data[symbol], start, end)
    if df.empty:
        return BenchmarkResult(name=f"dca_{symbol}")

    # 月初ごとに初期資金を均等分割で投入
    month_starts = df.resample("MS").first().dropna().index
    n_months = len(month_starts)
    if n_months == 0:
        return BenchmarkResult(name=f"dca_{symbol}")
    per_month = initial / n_months

    qty = 0.0
    cash = initial
    trades: list[dict[str, Any]] = []
    month_idx = 0
    equity: list[dict[str, Any]] = []
    for ts, row in df.iterrows():
        # 月初に購入
        if month_idx < n_months and ts == month_starts[month_idx]:
            if cash >= per_month:
                entry_price = float(row["close"]) * (1.0 + slip)
                bought = per_month * (1.0 - fee) / entry_price
                qty += bought
                cash -= per_month
                trades.append(
                    {
                        "symbol": symbol,
                        "entry_ts": ts,
                        "exit_ts": df.index[-1],
                        "entry_price": entry_price,
                        "exit_price": None,
                        "pnl_cash_cost": per_month,
                        "qty": bought,
                        "side": "long",
                    }
                )
            month_idx += 1
        equity.append({"ts": ts, "equity": cash + qty * float(row["close"])})

    # 最終清算
    final_price = float(df["close"].iloc[-1]) * (1.0 - slip)
    final = cash + qty * final_price * (1.0 - fee)
    equity[-1]["equity"] = final
    # 各 trade に概算 PnL (最終価格ベース) を付与
    for t in trades:
        gross = t["qty"] * final_price * (1.0 - fee)
        t["exit_price"] = final_price
        t["pnl"] = gross - t["pnl_cash_cost"]
        t["won"] = t["pnl"] > 0
    return BenchmarkResult(
        name=f"monthly_dca_{symbol}", equity_curve=equity, trades=trades
    )


# ─────────────────────────────
# 3. 単純トレンドフォロー (EMA200)
# ─────────────────────────────
def trend_follow_benchmark(
    all_data: dict[str, pd.DataFrame],
    symbol: str = "BTC/USDT",
    start: str = "2020-01-01",
    end: str = "2024-12-31",
    initial: float = 10_000.0,
    ema_period: int = 200,
    fee: float = DEFAULT_FEE,
    slip: float = DEFAULT_SLIP,
) -> BenchmarkResult:
    # EMA 算出には期間前バッファを取るのが理想。ここでは all_data[symbol] 全体で
    # EMA を計算し、期間内のみスライス。
    full = all_data[symbol].copy()
    if "ema200" not in full.columns:
        full["_ema"] = full["close"].ewm(span=ema_period, adjust=False).mean()
    else:
        full["_ema"] = full["ema200"]

    df = _slice_period(full, start, end)
    if df.empty:
        return BenchmarkResult(name=f"trend_follow_{symbol}")

    cash = initial
    qty = 0.0
    in_pos = False
    entry_price = 0.0
    entry_ts = None
    equity: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []

    for ts, row in df.iterrows():
        price = float(row["close"])
        ema = float(row["_ema"]) if not pd.isna(row["_ema"]) else price
        bullish = price > ema
        if bullish and not in_pos:
            ep = price * (1.0 + slip)
            qty = cash * (1.0 - fee) / ep
            cash = 0.0
            in_pos = True
            entry_price = ep
            entry_ts = ts
        elif not bullish and in_pos:
            xp = price * (1.0 - slip)
            cash = qty * xp * (1.0 - fee)
            trades.append(
                {
                    "symbol": symbol,
                    "entry_ts": entry_ts,
                    "exit_ts": ts,
                    "entry_price": entry_price,
                    "exit_price": xp,
                    "pnl": cash - (qty * entry_price),
                    "won": cash > (qty * entry_price),
                    "side": "long",
                }
            )
            qty = 0.0
            in_pos = False
        equity.append(
            {"ts": ts, "equity": cash + qty * price if in_pos else cash}
        )

    # 最終清算
    if in_pos:
        xp = float(df["close"].iloc[-1]) * (1.0 - slip)
        cash = qty * xp * (1.0 - fee)
        trades.append(
            {
                "symbol": symbol,
                "entry_ts": entry_ts,
                "exit_ts": df.index[-1],
                "entry_price": entry_price,
                "exit_price": xp,
                "pnl": cash - (qty * entry_price),
                "won": cash > (qty * entry_price),
                "side": "long",
            }
        )
        qty = 0.0
        equity[-1]["equity"] = cash
    return BenchmarkResult(
        name=f"trend_follow_{symbol}", equity_curve=equity, trades=trades
    )


# ─────────────────────────────
# 4. ランダムエントリー
# ─────────────────────────────
def random_entry_benchmark(
    all_data: dict[str, pd.DataFrame],
    universe: list[str],
    start: str = "2020-01-01",
    end: str = "2024-12-31",
    initial: float = 10_000.0,
    top_n: int = 3,
    rebalance_days: int = 7,
    seed: int = 42,
    fee: float = DEFAULT_FEE,
    slip: float = DEFAULT_SLIP,
) -> BenchmarkResult:
    """各リバランスで universe から top_n をランダム選択。
    重み付けは均等、売却時の stop 等はなし (純粋なランダム比較)。
    """
    rnd = random.Random(seed)
    # BTC の日付を基準にループ (universe の要素に日付が揃っている前提)
    base_symbol = "BTC/USDT" if "BTC/USDT" in all_data else universe[0]
    base = _slice_period(all_data[base_symbol], start, end)
    if base.empty:
        return BenchmarkResult(name="random_entry")

    cash = initial
    positions: dict[str, float] = {}  # symbol -> qty
    equity: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    last_key = None

    for ts, _ in base.iterrows():
        key = (ts.dayofyear + (ts.year - 2020) * 366) // rebalance_days

        # リバランス
        if key != last_key:
            # 既存ポジション売却
            for sym, qty in list(positions.items()):
                df = all_data.get(sym)
                if df is not None and ts in df.index:
                    price = float(df.loc[ts, "close"]) * (1.0 - slip)
                    pnl_cash = qty * price * (1.0 - fee)
                    cash += pnl_cash
                    trades.append(
                        {
                            "symbol": sym,
                            "entry_ts": trades[-1].get("entry_ts")
                            if trades
                            else ts,
                            "exit_ts": ts,
                            "entry_price": None,  # 追跡していない (簡易版)
                            "exit_price": price,
                            "pnl": 0.0,  # PnL 計算は後述で補填
                            "won": False,
                            "side": "long",
                            "exit_cash": pnl_cash,
                        }
                    )
            positions.clear()
            # 新規選択
            available = [s for s in universe if s in all_data and ts in all_data[s].index]
            if len(available) >= top_n:
                picks = rnd.sample(available, top_n)
                per_sym = cash / top_n
                for sym in picks:
                    price = float(all_data[sym].loc[ts, "close"]) * (1.0 + slip)
                    qty = per_sym * (1.0 - fee) / price
                    positions[sym] = qty
                    trades.append(
                        {
                            "symbol": sym,
                            "entry_ts": ts,
                            "exit_ts": None,
                            "entry_price": price,
                            "exit_price": None,
                            "pnl": None,
                            "won": None,
                            "side": "long",
                            "entry_cash": per_sym,
                        }
                    )
                cash -= per_sym * top_n
            last_key = key

        # 時価評価
        total = cash
        for sym, qty in positions.items():
            df = all_data.get(sym)
            if df is not None and ts in df.index:
                total += qty * float(df.loc[ts, "close"])
        equity.append({"ts": ts, "equity": total})

    # 期末清算
    final_ts = base.index[-1]
    for sym, qty in list(positions.items()):
        df = all_data.get(sym)
        if df is not None and final_ts in df.index:
            price = float(df.loc[final_ts, "close"]) * (1.0 - slip)
            cash += qty * price * (1.0 - fee)
    if equity:
        equity[-1]["equity"] = cash
    positions.clear()

    # trades の pnl を推定 (entry_cash と exit_cash が揃った分だけ)
    # 簡易版: entry/exit ペアが symbol で隣接している前提
    paired: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    for t in trades:
        sym = t["symbol"]
        if t.get("exit_ts") is None and t.get("pnl") is None:
            pending[sym] = t
        elif sym in pending and t.get("exit_cash") is not None:
            entry = pending.pop(sym)
            entry["exit_ts"] = t["exit_ts"]
            entry["exit_price"] = t["exit_price"]
            entry["pnl"] = t["exit_cash"] - entry["entry_cash"]
            entry["won"] = entry["pnl"] > 0
            paired.append(entry)
    # pending の残りは期末までホールドとして pnl 計算
    for sym, entry in pending.items():
        df = all_data.get(sym)
        if df is not None and final_ts in df.index:
            xp = float(df.loc[final_ts, "close"]) * (1.0 - slip)
            gross = (initial * 0)  # 既に cash に加算済み、pnl だけ推定
            entry["exit_ts"] = final_ts
            entry["exit_price"] = xp
            # 簡易: exit_price * qty vs entry_cash (qty は保存していないので略)
            # 代わりに entry_cash を 1 単位とみなした return ratio で代替
            entry["pnl"] = 0.0  # 詳細 PnL は grid_search 側で equity_curve から再計算
            entry["won"] = False
            paired.append(entry)

    return BenchmarkResult(
        name="random_entry", equity_curve=equity, trades=paired
    )
