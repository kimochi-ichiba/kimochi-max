"""
backtester_pyramiding_v2.py
===========================
改善版: 4時間足 + マルチタイムフレーム確認 + 厳格フィルター + 調整SL

主な変更点（v1→v2）:
- メイン時間足: 1h → 4h（ノイズ大幅削減）
- ADX閾値:     25 → 30（強いトレンドのみ）
- 出来高フィルター: 1.3x → 1.5x（より厳格）
- 初期SL:     0.8 ATR → 1.5 ATR（ダマシ耐性）
- トレーリング: 3.0 ATR → 2.5 ATR（利益確保強化）
- Donchian期間: 20 → 30（偽ブレイク削減）
- ピラミッド追加間隔: 0.5 ATR → 1.0 ATR（強い動きのみ）
- 日足トレンドフィルター追加（200 EMA方向と一致する時のみ取引）
- リスク: 2% → 1.5%（レバレッジとの整合）
"""

from __future__ import annotations

from pathlib import Path
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_fetcher import DataFetcher
from config import Config


# ----------------------------- Strategy params (v2) -----------------------

BREAKOUT_PERIOD = 30
ADX_PERIOD = 14
ADX_MIN = 30.0
ATR_PERIOD = 14
VOL_AVG_PERIOD = 20
VOL_MULT = 1.5

INITIAL_SL_ATR = 1.5
TRAIL_ATR = 2.5
PYRAMID_ADD_ATR = 1.0
MAX_PYRAMID_UNITS = 4
LEVERAGE = 3.0
RISK_PER_TRADE = 0.015
FEE_RATE = 0.0006
SLIPPAGE = 0.0005

EMA_TREND_PERIOD = 200  # 日足EMA200で大きなトレンド判定

INITIAL_BALANCE = 10_000.0
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
DAYS = 180
TIMEFRAME = "4h"
TREND_TIMEFRAME = "1d"


# ----------------------------- Indicators --------------------------------

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    high, low, close = df["high"], df["low"], df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_PERIOD).mean()

    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr_adx = tr.rolling(ADX_PERIOD).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(ADX_PERIOD).mean() / atr_adx
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(ADX_PERIOD).mean() / atr_adx
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = dx.rolling(ADX_PERIOD).mean()

    df["donchian_high"] = high.rolling(BREAKOUT_PERIOD).max().shift(1)
    df["donchian_low"] = low.rolling(BREAKOUT_PERIOD).min().shift(1)
    df["vol_avg"] = df["volume"].rolling(VOL_AVG_PERIOD).mean()
    return df


def compute_daily_trend(df_daily: pd.DataFrame) -> pd.DataFrame:
    df = df_daily.copy()
    df["ema200"] = df["close"].ewm(span=EMA_TREND_PERIOD, adjust=False).mean()
    df["trend"] = np.where(df["close"] > df["ema200"], 1,
                           np.where(df["close"] < df["ema200"], -1, 0))
    return df[["trend"]]


def merge_daily_trend(df_4h: pd.DataFrame, df_trend: pd.DataFrame) -> pd.DataFrame:
    """4h足に日足トレンドをforward-fillでマージ"""
    df_4h = df_4h.copy()
    trend_reindexed = df_trend.reindex(df_4h.index, method="ffill")
    df_4h["daily_trend"] = trend_reindexed["trend"]
    return df_4h


# ----------------------------- Data structures ----------------------------

@dataclass
class Position:
    symbol: str
    side: str
    units: List[dict] = field(default_factory=list)
    stop: float = 0.0
    high_water: float = 0.0
    entry_atr: float = 0.0
    last_add_price: float = 0.0

    @property
    def total_size(self) -> float:
        return sum(u["size"] for u in self.units)

    @property
    def avg_entry(self) -> float:
        if not self.units:
            return 0.0
        return sum(u["entry"] * u["size"] for u in self.units) / self.total_size


@dataclass
class Trade:
    symbol: str
    side: str
    open_time: pd.Timestamp
    close_time: pd.Timestamp
    avg_entry: float
    exit_price: float
    size: float
    pnl: float
    pnl_pct: float
    num_adds: int


# ----------------------------- Core backtest ------------------------------

def run_backtest_for_symbol(
    df: pd.DataFrame, symbol: str, equity_tracker: dict
) -> list[Trade]:
    trades: list[Trade] = []
    pos: Optional[Position] = None

    for ts, row in df.iterrows():
        price = row["close"]
        atr = row["atr"]
        if pd.isna(atr) or pd.isna(row["donchian_high"]) or pd.isna(row["adx"]) \
           or pd.isna(row["daily_trend"]):
            continue

        # ---- ポジションあり: 決済/ピラミッド ----
        if pos is not None:
            if pos.side == "long":
                pos.high_water = max(pos.high_water, row["high"])
                new_stop = pos.high_water - TRAIL_ATR * pos.entry_atr
                pos.stop = max(pos.stop, new_stop)

                if row["low"] <= pos.stop:
                    exit_price = pos.stop * (1 - SLIPPAGE)
                    pnl = _close_position(pos, exit_price, equity_tracker)
                    trades.append(Trade(
                        symbol=symbol, side="long",
                        open_time=pos.units[0]["time"], close_time=ts,
                        avg_entry=pos.avg_entry, exit_price=exit_price,
                        size=pos.total_size, pnl=pnl,
                        pnl_pct=(exit_price / pos.avg_entry - 1) * 100 * LEVERAGE,
                        num_adds=len(pos.units) - 1,
                    ))
                    pos = None
                else:
                    if len(pos.units) < MAX_PYRAMID_UNITS and \
                       price >= pos.last_add_price + PYRAMID_ADD_ATR * pos.entry_atr:
                        _add_pyramid_unit(pos, price, equity_tracker, ts)
            else:
                pos.high_water = min(pos.high_water, row["low"])
                new_stop = pos.high_water + TRAIL_ATR * pos.entry_atr
                pos.stop = min(pos.stop, new_stop) if pos.stop > 0 else new_stop

                if row["high"] >= pos.stop:
                    exit_price = pos.stop * (1 + SLIPPAGE)
                    pnl = _close_position(pos, exit_price, equity_tracker)
                    trades.append(Trade(
                        symbol=symbol, side="short",
                        open_time=pos.units[0]["time"], close_time=ts,
                        avg_entry=pos.avg_entry, exit_price=exit_price,
                        size=pos.total_size, pnl=pnl,
                        pnl_pct=(pos.avg_entry / exit_price - 1) * 100 * LEVERAGE,
                        num_adds=len(pos.units) - 1,
                    ))
                    pos = None
                else:
                    if len(pos.units) < MAX_PYRAMID_UNITS and \
                       price <= pos.last_add_price - PYRAMID_ADD_ATR * pos.entry_atr:
                        _add_pyramid_unit(pos, price, equity_tracker, ts)

        # ---- ポジションなし: ブレイクアウト判定（日足トレンドと一致の場合のみ） ----
        if pos is None and row["adx"] >= ADX_MIN and row["volume"] >= row["vol_avg"] * VOL_MULT:
            if row["high"] > row["donchian_high"] and row["daily_trend"] == 1:
                entry = row["donchian_high"] * (1 + SLIPPAGE)
                pos = _open_position(symbol, "long", entry, atr, equity_tracker, ts)
            elif row["low"] < row["donchian_low"] and row["daily_trend"] == -1:
                entry = row["donchian_low"] * (1 - SLIPPAGE)
                pos = _open_position(symbol, "short", entry, atr, equity_tracker, ts)

        equity_tracker["curve"].append((ts, equity_tracker["balance"] + _unrealized(pos, price)))

    if pos is not None:
        exit_price = df.iloc[-1]["close"]
        pnl = _close_position(pos, exit_price, equity_tracker)
        direction = 1 if pos.side == "long" else -1
        trades.append(Trade(
            symbol=symbol, side=pos.side,
            open_time=pos.units[0]["time"], close_time=df.index[-1],
            avg_entry=pos.avg_entry, exit_price=exit_price,
            size=pos.total_size, pnl=pnl,
            pnl_pct=(exit_price / pos.avg_entry - 1) * 100 * LEVERAGE * direction,
            num_adds=len(pos.units) - 1,
        ))

    return trades


def _open_position(symbol, side, entry, atr, tracker, ts) -> Position:
    risk_amount = tracker["balance"] * RISK_PER_TRADE
    sl_distance = INITIAL_SL_ATR * atr
    size = risk_amount / sl_distance
    notional = size * entry
    fee = notional * FEE_RATE
    tracker["balance"] -= fee
    stop = entry - sl_distance if side == "long" else entry + sl_distance
    return Position(
        symbol=symbol, side=side,
        units=[{"entry": entry, "size": size, "time": ts}],
        stop=stop, high_water=entry, entry_atr=atr, last_add_price=entry,
    )


def _add_pyramid_unit(pos: Position, price: float, tracker: dict, ts):
    add_size = pos.units[0]["size"] * 0.5
    notional = add_size * price
    fee = notional * FEE_RATE
    tracker["balance"] -= fee
    pos.units.append({"entry": price, "size": add_size, "time": ts})
    pos.last_add_price = price


def _close_position(pos: Position, exit_price: float, tracker: dict) -> float:
    if pos.side == "long":
        gross = (exit_price - pos.avg_entry) * pos.total_size
    else:
        gross = (pos.avg_entry - exit_price) * pos.total_size
    fee = exit_price * pos.total_size * FEE_RATE
    pnl = gross - fee
    tracker["balance"] += pnl
    return pnl


def _unrealized(pos: Optional[Position], price: float) -> float:
    if pos is None:
        return 0.0
    if pos.side == "long":
        return (price - pos.avg_entry) * pos.total_size
    return (pos.avg_entry - price) * pos.total_size


# ----------------------------- Reporting ----------------------------------

def report(trades, curve, start_balance, end_balance, days):
    n = len(trades)
    if n == 0:
        print("取引なし")
        return {}
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / n * 100
    avg_win = np.mean([t.pnl for t in wins]) if wins else 0
    avg_loss = np.mean([t.pnl for t in losses]) if losses else 0
    pf = abs(sum(t.pnl for t in wins) / sum(t.pnl for t in losses)) if losses else float("inf")

    curve_df = pd.DataFrame(curve, columns=["time", "equity"]).drop_duplicates("time").set_index("time")
    running_max = curve_df["equity"].cummax()
    dd = (curve_df["equity"] - running_max) / running_max
    max_dd = dd.min() * 100

    total_return = (end_balance / start_balance - 1) * 100
    months = days / 30.0
    if end_balance > 0:
        monthly_return = ((end_balance / start_balance) ** (1 / months) - 1) * 100
    else:
        monthly_return = -100.0

    print("\n" + "=" * 62)
    print("  改善版v2: ピラミッディング+ブレイクアウト+日足フィルター")
    print("=" * 62)
    print(f"  期間               : {days}日（約{months:.1f}ヶ月）")
    print(f"  時間足             : {TIMEFRAME} (日足でトレンド確認)")
    print(f"  初期残高           : ${start_balance:,.2f}")
    print(f"  最終残高           : ${end_balance:,.2f}")
    print(f"  総リターン         : {total_return:+.2f}%")
    print(f"  月次平均(複利)     : {monthly_return:+.2f}%")
    print(f"  最大ドローダウン   : {max_dd:.2f}%")
    print("-" * 62)
    print(f"  トレード回数       : {n}")
    print(f"  勝率               : {win_rate:.1f}%")
    print(f"  平均利益           : ${avg_win:,.2f}")
    print(f"  平均損失           : ${avg_loss:,.2f}")
    print(f"  プロフィットファクター : {pf:.2f}")
    print(f"  ピラミッド平均段数 : {np.mean([t.num_adds for t in trades]):.2f}")
    print("=" * 62)

    print("\n  通貨別内訳:")
    for sym in SYMBOLS:
        s = [t for t in trades if t.symbol == sym]
        if not s:
            print(f"    {sym}: (取引なし)")
            continue
        s_pnl = sum(t.pnl for t in s)
        s_wins = len([t for t in s if t.pnl > 0])
        print(f"    {sym}: 取引{len(s)}回 勝率{s_wins/len(s)*100:.1f}% PnL ${s_pnl:+,.2f}")

    return {"monthly": monthly_return, "dd": max_dd, "pf": pf, "win_rate": win_rate, "n": n}


# ----------------------------- Main ---------------------------------------

def main():
    end_date = datetime(2026, 4, 18)
    start_date = end_date - timedelta(days=DAYS + 60)  # 日足EMA200のバッファ
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    print(f"📥 データ取得: {start_str} 〜 {end_str} ({DAYS}日分+バッファ / {TIMEFRAME} & {TREND_TIMEFRAME})")
    config = Config()
    fetcher = DataFetcher(config)

    equity_tracker = {"balance": INITIAL_BALANCE, "curve": []}
    all_trades = []
    analysis_start = pd.Timestamp(end_date - timedelta(days=DAYS))

    for sym in SYMBOLS:
        print(f"\n--- {sym} ---")
        df_4h = fetcher.fetch_historical_ohlcv(sym, TIMEFRAME, start_str, end_str)
        df_1d = fetcher.fetch_historical_ohlcv(sym, TREND_TIMEFRAME, start_str, end_str)
        if df_4h.empty or df_1d.empty:
            print(f"  ⚠️ {sym} データなし、スキップ")
            continue
        df_4h = compute_indicators(df_4h)
        df_trend = compute_daily_trend(df_1d)
        df_4h = merge_daily_trend(df_4h, df_trend)
        # バッファ期間を除外
        df_4h = df_4h[df_4h.index >= analysis_start]
        trades = run_backtest_for_symbol(df_4h, sym, equity_tracker)
        all_trades.extend(trades)
        print(f"  {sym}: {len(trades)}トレード")

    report(all_trades, equity_tracker["curve"],
           INITIAL_BALANCE, equity_tracker["balance"], DAYS)


if __name__ == "__main__":
    main()
