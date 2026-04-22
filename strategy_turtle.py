"""
strategy_turtle.py
==================
Turtle Trading System for Crypto — 手数料があってもプラスになる仕組み

クラシックTurtle System 1 (Richard Dennis 1983):
- エントリー: 55日(約2ヶ月)の高値を上抜け → ロング
                   55日の安値を下抜け → ショート
- 損切り: 2 × ATR(20日)
- 利確: 20日の逆側終値を割る(ロングは安値、ショートは高値)
- 累積ポジション: 最大4段 (N=ATR単位で0.5刻み追加)
- ポジションサイズ: 1%リスク/取引
- レバレッジ: 3倍
- 日足ベースなので取引回数が少なく、手数料負けしない

仮想通貨用の調整:
- 10通貨並列運用で分散効果
- ショートも有効(先物で両建て可)
- 1ヶ月あたり取引回数: 10通貨合計で5〜15回程度(低頻度)
"""

from __future__ import annotations

from pathlib import Path
import sys
import logging
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_fetcher import DataFetcher
from config import Config

logging.getLogger("data_fetcher").setLevel(logging.WARNING)


# ----------------------------- Turtle Params -----------------------------

ENTRY_PERIOD = 55       # System 1: 55日ブレイクアウト
EXIT_PERIOD = 20        # System 1: 20日反対側ブレイク
ATR_PERIOD = 20
SL_ATR_MULT = 2.0       # 損切り: 2 × ATR (Dennis classic)
PYRAMID_ATR_STEP = 0.5  # 0.5 N毎に追加ポジション
MAX_PYRAMIDS = 4        # 最大4段

LEVERAGE = 3.0
RISK_PER_TRADE = 0.01   # 1トレード1%リスク
FEE_RATE = 0.0006       # Binance Futures taker
SLIPPAGE = 0.0005

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "ADA/USDT",
    "LINK/USDT", "AVAX/USDT", "DOT/USDT", "UNI/USDT", "NEAR/USDT"
]
TIMEFRAME = "1d"
INITIAL_BALANCE = 10_000.0


# ----------------------------- Data structures ---------------------------

@dataclass
class TurtlePosition:
    symbol: str
    side: str
    units: List[dict] = field(default_factory=list)
    stop: float = 0.0
    last_add_price: float = 0.0
    entry_atr: float = 0.0
    entry_time: pd.Timestamp = None

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
    pnl: float
    pnl_pct: float
    num_adds: int
    exit_reason: str


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

    df["entry_high"] = high.rolling(ENTRY_PERIOD).max().shift(1)
    df["entry_low"] = low.rolling(ENTRY_PERIOD).min().shift(1)
    df["exit_high"] = high.rolling(EXIT_PERIOD).max().shift(1)
    df["exit_low"] = low.rolling(EXIT_PERIOD).min().shift(1)
    return df


# ----------------------------- Trading logic -----------------------------

def run_turtle_on_symbol(df: pd.DataFrame, symbol: str, tracker: dict) -> list[Trade]:
    trades = []
    pos: Optional[TurtlePosition] = None

    for ts, row in df.iterrows():
        price = row["close"]
        atr = row["atr"]
        if pd.isna(atr) or pd.isna(row["entry_high"]) or pd.isna(row["exit_low"]):
            continue

        # --- 決済判定 ---
        if pos is not None:
            exit_reason = None
            exit_price = None

            if pos.side == "long":
                # 損切り
                if row["low"] <= pos.stop:
                    exit_price = pos.stop * (1 - SLIPPAGE)
                    exit_reason = "sl"
                # 20日安値割れ(利確 or 引き下がり)
                elif row["low"] < row["exit_low"]:
                    exit_price = row["exit_low"] * (1 - SLIPPAGE)
                    exit_reason = "trail"
            else:
                if row["high"] >= pos.stop:
                    exit_price = pos.stop * (1 + SLIPPAGE)
                    exit_reason = "sl"
                elif row["high"] > row["exit_high"]:
                    exit_price = row["exit_high"] * (1 + SLIPPAGE)
                    exit_reason = "trail"

            if exit_reason:
                pnl = _close_position(pos, exit_price, tracker)
                direction = 1 if pos.side == "long" else -1
                trades.append(Trade(
                    symbol=symbol, side=pos.side,
                    open_time=pos.entry_time, close_time=ts,
                    avg_entry=pos.avg_entry, exit_price=exit_price,
                    pnl=pnl,
                    pnl_pct=(exit_price / pos.avg_entry - 1) * 100 * LEVERAGE * direction,
                    num_adds=len(pos.units) - 1,
                    exit_reason=exit_reason,
                ))
                pos = None

            # ピラミッディング追加判定
            elif len(pos.units) < MAX_PYRAMIDS:
                if pos.side == "long" and price >= pos.last_add_price + PYRAMID_ATR_STEP * pos.entry_atr:
                    _add_pyramid(pos, price, tracker, ts)
                elif pos.side == "short" and price <= pos.last_add_price - PYRAMID_ATR_STEP * pos.entry_atr:
                    _add_pyramid(pos, price, tracker, ts)

        # --- 新規エントリー判定 ---
        if pos is None:
            if row["high"] > row["entry_high"]:
                entry = row["entry_high"] * (1 + SLIPPAGE)
                pos = _open_position(symbol, "long", entry, atr, tracker, ts)
            elif row["low"] < row["entry_low"]:
                entry = row["entry_low"] * (1 - SLIPPAGE)
                pos = _open_position(symbol, "short", entry, atr, tracker, ts)

    # 最後にポジション残っていれば強制決済
    if pos is not None:
        exit_price = df.iloc[-1]["close"]
        pnl = _close_position(pos, exit_price, tracker)
        direction = 1 if pos.side == "long" else -1
        trades.append(Trade(
            symbol=symbol, side=pos.side,
            open_time=pos.entry_time, close_time=df.index[-1],
            avg_entry=pos.avg_entry, exit_price=exit_price,
            pnl=pnl,
            pnl_pct=(exit_price / pos.avg_entry - 1) * 100 * LEVERAGE * direction,
            num_adds=len(pos.units) - 1,
            exit_reason="end",
        ))

    return trades


def _open_position(symbol, side, entry, atr, tracker, ts) -> TurtlePosition:
    risk_amt = tracker["balance"] * RISK_PER_TRADE
    sl_distance = SL_ATR_MULT * atr
    size = risk_amt / sl_distance
    notional = size * entry
    tracker["balance"] -= notional * FEE_RATE
    stop = entry - sl_distance if side == "long" else entry + sl_distance
    return TurtlePosition(
        symbol=symbol, side=side,
        units=[{"entry": entry, "size": size}],
        stop=stop, last_add_price=entry, entry_atr=atr, entry_time=ts,
    )


def _add_pyramid(pos: TurtlePosition, price: float, tracker: dict, ts):
    add_size = pos.units[0]["size"]
    notional = add_size * price
    tracker["balance"] -= notional * FEE_RATE
    pos.units.append({"entry": price, "size": add_size})
    pos.last_add_price = price
    # ストップを各追加ごとに引き上げ
    if pos.side == "long":
        pos.stop = price - SL_ATR_MULT * pos.entry_atr
    else:
        pos.stop = price + SL_ATR_MULT * pos.entry_atr


def _close_position(pos: TurtlePosition, exit_price: float, tracker: dict) -> float:
    if pos.side == "long":
        gross = (exit_price - pos.avg_entry) * pos.total_size * LEVERAGE
    else:
        gross = (pos.avg_entry - exit_price) * pos.total_size * LEVERAGE
    fee = exit_price * pos.total_size * FEE_RATE
    pnl = gross - fee
    tracker["balance"] += pnl
    return pnl


# ----------------------------- Validation runner -------------------------

def run_window(start: str, end: str, buffer_start: str) -> dict:
    """1ウィンドウの10通貨ポートフォリオ実行 (各通貨に$10k)"""
    cfg = Config()
    fetcher = DataFetcher(cfg)
    tracker = {"balance": INITIAL_BALANCE * len(SYMBOLS), "curve": []}
    # 各通貨のバックテストを逐次実行 (独立)
    per_sym = {}
    for sym in SYMBOLS:
        df = fetcher.fetch_historical_ohlcv(sym, TIMEFRAME, buffer_start, end)
        if df.empty:
            per_sym[sym] = {"trades": [], "pnl": 0, "pnl_pct": 0.0}
            continue
        df = compute_indicators(df)
        df = df[df.index >= pd.Timestamp(start)]
        sym_tracker = {"balance": INITIAL_BALANCE, "curve": []}
        trades = run_turtle_on_symbol(df, sym, sym_tracker)
        per_sym[sym] = {
            "trades": trades,
            "pnl": sym_tracker["balance"] - INITIAL_BALANCE,
            "pnl_pct": (sym_tracker["balance"] / INITIAL_BALANCE - 1) * 100,
        }
    total_pnl = sum(r["pnl"] for r in per_sym.values())
    total_trades = sum(len(r["trades"]) for r in per_sym.values())
    return {
        "per_sym": per_sym,
        "port_return": total_pnl / (INITIAL_BALANCE * len(SYMBOLS)) * 100,
        "total_trades": total_trades,
    }


def main():
    end_date = datetime(2026, 4, 18)
    history_days = 365
    window_days = 30
    step_days = 15

    analysis_start = end_date - timedelta(days=history_days)
    buffer_start = end_date - timedelta(days=history_days + 120)  # 55日ブレイク+ATR用

    windows = []
    cursor = analysis_start
    while cursor + timedelta(days=window_days) <= end_date:
        w_s = cursor
        w_e = cursor + timedelta(days=window_days)
        windows.append((w_s.strftime("%Y-%m-%d"), w_e.strftime("%Y-%m-%d")))
        cursor += timedelta(days=step_days)

    print(f"\n🐢 Turtle Trading System — 12ヶ月ローリング検証")
    print(f"{'='*90}")
    print(f"期間: {analysis_start.strftime('%Y-%m-%d')} 〜 {end_date.strftime('%Y-%m-%d')}")
    print(f"銘柄: {len(SYMBOLS)}通貨 (各$10,000割当)")
    print(f"日足ベース、55日ブレイクアウト、2ATR損切り、20日反転で利確")
    print(f"レバレッジ{LEVERAGE}倍、手数料{FEE_RATE*100}%/片側")
    print(f"{'='*90}")
    header_sym = " ".join(f"{s.split('/')[0]:>5s}" for s in SYMBOLS)
    print(f"  {'#':3s} {'期間':27s} {'月次':>8s} {'取引数':>6s}  [{header_sym}]")
    print(f"  {'-'*90}")

    all_returns = []
    total_trades_all = 0
    for i, (s, e) in enumerate(windows, 1):
        r = run_window(s, e, buffer_start.strftime("%Y-%m-%d"))
        per_str = " ".join(f"{r['per_sym'][s]['pnl_pct']:+5.1f}" for s in SYMBOLS)
        print(f"  [{i:2d}] {s} 〜 {e}  {r['port_return']:+7.2f}% {r['total_trades']:5d}  [{per_str}]")
        all_returns.append(r["port_return"])
        total_trades_all += r["total_trades"]

    rets = np.array(all_returns)
    print(f"\n{'='*90}")
    print(f"  📊 Turtle System 集計")
    print(f"{'='*90}")
    print(f"  ウィンドウ数          : {len(rets)}")
    print(f"  平均月次リターン      : {np.mean(rets):+.2f}%")
    print(f"  中央値                : {np.median(rets):+.2f}%")
    print(f"  最高                  : {np.max(rets):+.2f}%")
    print(f"  最低                  : {np.min(rets):+.2f}%")
    print(f"  標準偏差              : {np.std(rets):.2f}%")
    print(f"  プラス月              : {sum(1 for r in rets if r > 0)}/{len(rets)} ({sum(1 for r in rets if r > 0)/len(rets)*100:.0f}%)")
    print(f"  +30%以上              : {sum(1 for r in rets if r >= 30)}/{len(rets)}")
    print(f"  +15%以上              : {sum(1 for r in rets if r >= 15)}/{len(rets)}")
    print(f"  +10%以上              : {sum(1 for r in rets if r >= 10)}/{len(rets)}")
    print(f"  -10%以下              : {sum(1 for r in rets if r <= -10)}/{len(rets)}")
    print(f"  1ウィンドウ平均取引数 : {total_trades_all/len(rets):.1f}")

    # 複利シミュ
    balance = 100000.0
    for r in rets:
        balance *= (1 + r / 100)
    months = len(rets) / 2.0  # 15日刻み = 月2ウィンドウ
    monthly_comp = ((balance / 100000) ** (1 / months) - 1) * 100 if balance > 0 else -100
    print(f"\n  💰 複利シミュ ($100k): 最終${balance:,.0f} (月次複利{monthly_comp:+.2f}%)")

    # 全戦略比較
    print(f"\n  🏆 全戦略比較 (平均月次)")
    print(f"  {'='*65}")
    print(f"  {'Buy&Hold (何もしない)':<30s}: {'+0.41%':>8s}  勝率 57%")
    print(f"  {'v95.0 (3通貨)':<30s}: {'-11.92%':>8s}  勝率 22%")
    print(f"  {'v95.0 (10通貨)':<30s}: {'-9.70%':>8s}  勝率 30%")
    print(f"  {'v95.0 + Regime Filter':<30s}: {'-1.69%':>8s}")
    print(f"  {'🐢 Turtle (新戦略)':<30s}: {f'{np.mean(rets):+.2f}%':>8s}  "
          f"勝率 {sum(1 for r in rets if r > 0)/len(rets)*100:.0f}%")
    print(f"  {'='*65}\n")


if __name__ == "__main__":
    main()
