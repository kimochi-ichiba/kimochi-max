"""
backtester_pyramiding.py
========================
ピラミッディング + ブレイクアウト + レバレッジ3倍 戦略の
半年バックテスト（1時間足、複数通貨）。

戦略の要点:
- Donchian 20期間のブレイクアウトでエントリー
- ADX > 25 とボリューム増加でフィルター
- 勝ちトレードに最大3回の買い増し（各0.5 ATR毎）
- 初期SL = エントリー - 0.8 ATR、その後Chandelier Exit (3 ATR trail)
- レバレッジ3倍、1トレード2%リスク
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


# ----------------------------- Strategy params -----------------------------

BREAKOUT_PERIOD = 20          # Donchianチャネルの期間
ADX_PERIOD = 14
ADX_MIN = 25.0                # ADXがこれ未満ならブレイクアウト不採用
ATR_PERIOD = 14
VOL_AVG_PERIOD = 20           # 出来高平均の期間
VOL_MULT = 1.3                # 出来高がこの倍数を超えれば確証

INITIAL_SL_ATR = 0.8          # 初期ストップロス (ATR倍率)
TRAIL_ATR = 3.0               # Chandelier Exit trail距離
PYRAMID_ADD_ATR = 0.5         # 買い増し発動距離
MAX_PYRAMID_UNITS = 4         # 初期1 + 追加3 = 最大4
LEVERAGE = 3.0
RISK_PER_TRADE = 0.02         # 総資産の2%を初期リスク
FEE_RATE = 0.0006             # 0.06% (Binance先物テイカー想定)
SLIPPAGE = 0.0005             # 0.05%のスリッページ

INITIAL_BALANCE = 10_000.0
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
DAYS = 180
TIMEFRAME = "1h"


# ----------------------------- Indicators (self-contained) -----------------

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    high, low, close = df["high"], df["low"], df["close"]

    # ATR
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_PERIOD).mean()

    # ADX
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr_adx = tr.rolling(ADX_PERIOD).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(ADX_PERIOD).mean() / atr_adx
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(ADX_PERIOD).mean() / atr_adx
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = dx.rolling(ADX_PERIOD).mean()

    # Donchian breakout levels
    df["donchian_high"] = high.rolling(BREAKOUT_PERIOD).max().shift(1)
    df["donchian_low"] = low.rolling(BREAKOUT_PERIOD).min().shift(1)

    # Volume
    df["vol_avg"] = df["volume"].rolling(VOL_AVG_PERIOD).mean()

    return df


# ----------------------------- Data structures ----------------------------

@dataclass
class Position:
    symbol: str
    side: str                   # "long" or "short"
    units: List[dict] = field(default_factory=list)  # {entry, size, time}
    stop: float = 0.0
    high_water: float = 0.0     # 最高値（long）or 最安値（short）
    entry_atr: float = 0.0
    last_add_price: float = 0.0

    @property
    def total_size(self) -> float:
        return sum(u["size"] for u in self.units)

    @property
    def avg_entry(self) -> float:
        if not self.units:
            return 0.0
        total_cost = sum(u["entry"] * u["size"] for u in self.units)
        return total_cost / self.total_size


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
    """
    equity_tracker: {"balance": float, "curve": list of (timestamp, equity)}
    通貨ごとに実行するが、残高はグローバルで共有する。
    """
    trades: list[Trade] = []
    pos: Optional[Position] = None

    for ts, row in df.iterrows():
        price = row["close"]
        atr = row["atr"]
        if pd.isna(atr) or pd.isna(row["donchian_high"]) or pd.isna(row["adx"]):
            continue

        # ---- ポジションがある場合: 決済/ピラミッド判定 ----
        if pos is not None:
            if pos.side == "long":
                pos.high_water = max(pos.high_water, row["high"])
                # Chandelier trailing stop
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
                    # Pyramid追加判定
                    if len(pos.units) < MAX_PYRAMID_UNITS and \
                       price >= pos.last_add_price + PYRAMID_ADD_ATR * pos.entry_atr:
                        _add_pyramid_unit(pos, price, atr, equity_tracker, ts)
            else:  # short
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
                        _add_pyramid_unit(pos, price, atr, equity_tracker, ts)

        # ---- ポジションがない場合: ブレイクアウト判定 ----
        if pos is None and row["adx"] >= ADX_MIN and row["volume"] >= row["vol_avg"] * VOL_MULT:
            if row["high"] > row["donchian_high"]:
                # Long breakout
                entry = row["donchian_high"] * (1 + SLIPPAGE)
                pos = _open_position(symbol, "long", entry, atr, equity_tracker, ts)
            elif row["low"] < row["donchian_low"]:
                # Short breakout
                entry = row["donchian_low"] * (1 - SLIPPAGE)
                pos = _open_position(symbol, "short", entry, atr, equity_tracker, ts)

        # equity curve
        equity_tracker["curve"].append((ts, equity_tracker["balance"] + _unrealized(pos, price)))

    # 最後にポジションが残っていれば強制決済
    if pos is not None:
        exit_price = df.iloc[-1]["close"]
        pnl = _close_position(pos, exit_price, equity_tracker)
        trades.append(Trade(
            symbol=symbol, side=pos.side,
            open_time=pos.units[0]["time"], close_time=df.index[-1],
            avg_entry=pos.avg_entry, exit_price=exit_price,
            size=pos.total_size, pnl=pnl,
            pnl_pct=(exit_price / pos.avg_entry - 1) * 100 * LEVERAGE * (1 if pos.side=="long" else -1),
            num_adds=len(pos.units) - 1,
        ))

    return trades


def _open_position(symbol, side, entry, atr, tracker, ts) -> Position:
    risk_amount = tracker["balance"] * RISK_PER_TRADE
    sl_distance = INITIAL_SL_ATR * atr
    # size (通貨単位) = risk / sl_distance, レバレッジで必要証拠金が減る
    size = risk_amount / sl_distance
    notional = size * entry
    margin = notional / LEVERAGE
    fee = notional * FEE_RATE
    tracker["balance"] -= fee

    stop = entry - sl_distance if side == "long" else entry + sl_distance
    pos = Position(
        symbol=symbol, side=side,
        units=[{"entry": entry, "size": size, "time": ts, "margin": margin}],
        stop=stop, high_water=entry, entry_atr=atr, last_add_price=entry,
    )
    return pos


def _add_pyramid_unit(pos: Position, price: float, atr: float, tracker: dict, ts):
    # 既存サイズの50%を追加（段階的に小さく）
    add_size = pos.units[0]["size"] * 0.5
    notional = add_size * price
    margin = notional / LEVERAGE
    fee = notional * FEE_RATE
    tracker["balance"] -= fee
    pos.units.append({"entry": price, "size": add_size, "time": ts, "margin": margin})
    pos.last_add_price = price


def _close_position(pos: Position, exit_price: float, tracker: dict) -> float:
    if pos.side == "long":
        gross_pnl = (exit_price - pos.avg_entry) * pos.total_size
    else:
        gross_pnl = (pos.avg_entry - exit_price) * pos.total_size
    fee = exit_price * pos.total_size * FEE_RATE
    pnl = gross_pnl - fee
    tracker["balance"] += pnl
    return pnl


def _unrealized(pos: Optional[Position], price: float) -> float:
    if pos is None:
        return 0.0
    if pos.side == "long":
        return (price - pos.avg_entry) * pos.total_size
    else:
        return (pos.avg_entry - price) * pos.total_size


# ----------------------------- Reporting ----------------------------------

def report(trades: list[Trade], curve: list[tuple], start_balance: float, end_balance: float, days: int):
    n = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / n * 100 if n else 0
    total_pnl = sum(t.pnl for t in trades)
    avg_win = np.mean([t.pnl for t in wins]) if wins else 0
    avg_loss = np.mean([t.pnl for t in losses]) if losses else 0
    pf = abs(sum(t.pnl for t in wins) / sum(t.pnl for t in losses)) if losses else float("inf")

    # drawdown
    curve_df = pd.DataFrame(curve, columns=["time", "equity"]).drop_duplicates("time").set_index("time")
    running_max = curve_df["equity"].cummax()
    dd = (curve_df["equity"] - running_max) / running_max
    max_dd = dd.min() * 100

    total_return = (end_balance / start_balance - 1) * 100
    months = days / 30.0
    monthly_return = ((end_balance / start_balance) ** (1 / months) - 1) * 100

    print("\n" + "=" * 60)
    print("  ピラミッディング＋ブレイクアウト戦略 バックテスト結果")
    print("=" * 60)
    print(f"  期間               : {days}日（約{months:.1f}ヶ月）")
    print(f"  対象通貨           : {', '.join(SYMBOLS)}")
    print(f"  時間足             : {TIMEFRAME}")
    print(f"  初期残高           : ${start_balance:,.2f}")
    print(f"  最終残高           : ${end_balance:,.2f}")
    print(f"  総リターン         : {total_return:+.2f}%")
    print(f"  月次平均リターン   : {monthly_return:+.2f}% (複利)")
    print(f"  最大ドローダウン   : {max_dd:.2f}%")
    print("-" * 60)
    print(f"  トレード回数       : {n}")
    print(f"  勝率               : {win_rate:.1f}%")
    print(f"  平均利益           : ${avg_win:,.2f}")
    print(f"  平均損失           : ${avg_loss:,.2f}")
    print(f"  プロフィットファクター : {pf:.2f}")
    print(f"  ピラミッド平均段数 : {np.mean([t.num_adds for t in trades]):.2f}" if trades else "  -")
    print("=" * 60)

    # 通貨別
    print("\n  通貨別内訳:")
    for sym in SYMBOLS:
        s_trades = [t for t in trades if t.symbol == sym]
        if not s_trades:
            print(f"    {sym}: (取引なし)")
            continue
        s_pnl = sum(t.pnl for t in s_trades)
        s_wins = len([t for t in s_trades if t.pnl > 0])
        print(f"    {sym}: 取引{len(s_trades)}回 勝率{s_wins/len(s_trades)*100:.1f}% PnL ${s_pnl:+,.2f}")

    return {
        "total_return_pct": total_return,
        "monthly_return_pct": monthly_return,
        "max_dd_pct": max_dd,
        "pf": pf,
        "win_rate": win_rate,
        "trades": n,
    }


# ----------------------------- Main ---------------------------------------

def main():
    end_date = datetime(2026, 4, 18)
    start_date = end_date - timedelta(days=DAYS)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    print(f"📥 データ取得: {start_str} 〜 {end_str} ({DAYS}日分 / {TIMEFRAME})")
    config = Config()
    fetcher = DataFetcher(config)

    equity_tracker = {"balance": INITIAL_BALANCE, "curve": []}
    all_trades: list[Trade] = []

    for sym in SYMBOLS:
        print(f"\n--- {sym} ---")
        df = fetcher.fetch_historical_ohlcv(sym, TIMEFRAME, start_str, end_str)
        if df.empty:
            print(f"  ⚠️ {sym} データなし、スキップ")
            continue
        df = compute_indicators(df)
        trades = run_backtest_for_symbol(df, sym, equity_tracker)
        all_trades.extend(trades)
        print(f"  {sym}: {len(trades)}トレード完了")

    summary = report(all_trades, equity_tracker["curve"],
                     INITIAL_BALANCE, equity_tracker["balance"], DAYS)
    return summary


if __name__ == "__main__":
    main()
