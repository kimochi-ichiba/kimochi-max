"""
strategy_turtle_aggressive.py
=============================
強化版Turtle: 月20-30%に挑戦する攻撃的設計

基本Turtleからの強化点:
1. **System 1 (55日) + System 2 (20日) 並列運用** - Dennis原典
2. **Long-only** - 仮想通貨の長期バイアスを活用(下落相場は取引しない)
3. **適応型レバレッジ**:
   - ADX>35: 5倍 (強トレンド)
   - ADX>25: 4倍 (トレンド)
   - ADX<25: 3倍 (通常)
4. **Maker注文想定** - 手数料0.02%/側 (0.06%→0.02%で1/3)
5. **リスク2%/取引** - Kelly半分想定 (通常1%→2%)
6. **ピラミッドMAX6段** - 強トレンドで伸ばす (4→6)
7. **日足EMAフィルター** - EMA50 > EMA200 の時だけ取引

検証: 23ウィンドウ (過去1年) で月次リターン分布を測定
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


# ----------------------------- Aggressive Params -------------------------

# Dual system
SYS1_ENTRY = 55      # System 1 breakout period
SYS1_EXIT = 20       # System 1 exit
SYS2_ENTRY = 20      # System 2 breakout period (classic Turtle S2)
SYS2_EXIT = 10       # System 2 exit

ATR_PERIOD = 20
ADX_PERIOD = 14
EMA_SHORT = 50
EMA_LONG = 200

SL_ATR_MULT = 2.0
PYRAMID_ATR_STEP = 0.5
MAX_PYRAMIDS = 6     # 強化: 4→6段

# Adaptive leverage
LEV_BASE = 3.0
LEV_TREND = 4.0
LEV_STRONG = 5.0
ADX_TREND = 25.0
ADX_STRONG = 35.0

RISK_PER_TRADE = 0.02  # 強化: 1%→2%
FEE_RATE = 0.0002      # 強化: Maker想定 0.06% → 0.02%
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
    system: int  # 1 or 2
    units: List[dict] = field(default_factory=list)
    stop: float = 0.0
    last_add_price: float = 0.0
    entry_atr: float = 0.0
    entry_time: pd.Timestamp = None
    leverage: float = 3.0

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
    system: int
    open_time: pd.Timestamp
    close_time: pd.Timestamp
    avg_entry: float
    exit_price: float
    pnl: float
    pnl_pct: float
    num_adds: int
    exit_reason: str
    leverage: float


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

    # EMA filters
    df["ema50"] = close.ewm(span=EMA_SHORT, adjust=False).mean()
    df["ema200"] = close.ewm(span=EMA_LONG, adjust=False).mean()

    # Breakout levels (use .shift(1) to avoid look-ahead)
    df["sys1_high"] = high.rolling(SYS1_ENTRY).max().shift(1)
    df["sys1_exit"] = low.rolling(SYS1_EXIT).min().shift(1)
    df["sys2_high"] = high.rolling(SYS2_ENTRY).max().shift(1)
    df["sys2_exit"] = low.rolling(SYS2_EXIT).min().shift(1)
    return df


def get_leverage(adx: float) -> float:
    if pd.isna(adx):
        return LEV_BASE
    if adx >= ADX_STRONG:
        return LEV_STRONG
    if adx >= ADX_TREND:
        return LEV_TREND
    return LEV_BASE


# ----------------------------- Trading logic -----------------------------

def run_turtle_on_symbol(df: pd.DataFrame, symbol: str, tracker: dict) -> list[Trade]:
    trades = []
    pos: Optional[TurtlePosition] = None

    for ts, row in df.iterrows():
        price = row["close"]
        atr = row["atr"]
        if pd.isna(atr) or pd.isna(row["sys1_high"]) or pd.isna(row["ema200"]):
            continue

        # Long-only: 日足EMA50 > EMA200 でなければエントリーしない
        bull_regime = row["ema50"] > row["ema200"]

        # --- 決済判定 ---
        if pos is not None:
            exit_reason = None
            exit_price = None
            exit_level = row["sys1_exit"] if pos.system == 1 else row["sys2_exit"]

            # Long-only: ロングの決済のみ
            if row["low"] <= pos.stop:
                exit_price = pos.stop * (1 - SLIPPAGE)
                exit_reason = "sl"
            elif not pd.isna(exit_level) and row["low"] < exit_level:
                exit_price = exit_level * (1 - SLIPPAGE)
                exit_reason = "trail"

            if exit_reason:
                pnl = _close_position(pos, exit_price, tracker)
                trades.append(Trade(
                    symbol=symbol, system=pos.system,
                    open_time=pos.entry_time, close_time=ts,
                    avg_entry=pos.avg_entry, exit_price=exit_price,
                    pnl=pnl,
                    pnl_pct=(exit_price / pos.avg_entry - 1) * 100 * pos.leverage,
                    num_adds=len(pos.units) - 1,
                    exit_reason=exit_reason,
                    leverage=pos.leverage,
                ))
                pos = None
            # ピラミッディング
            elif len(pos.units) < MAX_PYRAMIDS and \
                 price >= pos.last_add_price + PYRAMID_ATR_STEP * pos.entry_atr:
                _add_pyramid(pos, price, tracker, ts)

        # --- 新規エントリー判定 ---
        if pos is None and bull_regime:
            leverage = get_leverage(row["adx"])
            if row["high"] > row["sys1_high"]:
                # System 1 breakout
                entry = row["sys1_high"] * (1 + SLIPPAGE)
                pos = _open_position(symbol, 1, entry, atr, leverage, tracker, ts)
            elif row["high"] > row["sys2_high"]:
                # System 2 breakout
                entry = row["sys2_high"] * (1 + SLIPPAGE)
                pos = _open_position(symbol, 2, entry, atr, leverage, tracker, ts)

    # 残ポジは最終足で決済
    if pos is not None:
        exit_price = df.iloc[-1]["close"]
        pnl = _close_position(pos, exit_price, tracker)
        trades.append(Trade(
            symbol=symbol, system=pos.system,
            open_time=pos.entry_time, close_time=df.index[-1],
            avg_entry=pos.avg_entry, exit_price=exit_price,
            pnl=pnl,
            pnl_pct=(exit_price / pos.avg_entry - 1) * 100 * pos.leverage,
            num_adds=len(pos.units) - 1,
            exit_reason="end",
            leverage=pos.leverage,
        ))

    return trades


def _open_position(symbol, system, entry, atr, leverage, tracker, ts) -> TurtlePosition:
    risk_amt = tracker["balance"] * RISK_PER_TRADE
    sl_distance = SL_ATR_MULT * atr
    size = risk_amt / sl_distance
    notional = size * entry
    tracker["balance"] -= notional * FEE_RATE
    return TurtlePosition(
        symbol=symbol, system=system,
        units=[{"entry": entry, "size": size}],
        stop=entry - sl_distance,
        last_add_price=entry, entry_atr=atr, entry_time=ts,
        leverage=leverage,
    )


def _add_pyramid(pos: TurtlePosition, price: float, tracker: dict, ts):
    add_size = pos.units[0]["size"]
    notional = add_size * price
    tracker["balance"] -= notional * FEE_RATE
    pos.units.append({"entry": price, "size": add_size})
    pos.last_add_price = price
    pos.stop = price - SL_ATR_MULT * pos.entry_atr


def _close_position(pos: TurtlePosition, exit_price: float, tracker: dict) -> float:
    gross = (exit_price - pos.avg_entry) * pos.total_size * pos.leverage
    fee = exit_price * pos.total_size * FEE_RATE
    pnl = gross - fee
    tracker["balance"] += pnl
    return pnl


# ----------------------------- Runner ------------------------------------

def run_window(start: str, end: str, buffer_start: str) -> dict:
    cfg = Config()
    fetcher = DataFetcher(cfg)
    per_sym = {}
    for sym in SYMBOLS:
        df = fetcher.fetch_historical_ohlcv(sym, TIMEFRAME, buffer_start, end)
        if df.empty:
            per_sym[sym] = {"trades": [], "pnl": 0, "pnl_pct": 0.0}
            continue
        df = compute_indicators(df)
        df = df[df.index >= pd.Timestamp(start)]
        sym_tracker = {"balance": INITIAL_BALANCE}
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
    buffer_start = end_date - timedelta(days=history_days + 260)  # EMA200用

    windows = []
    cursor = analysis_start
    while cursor + timedelta(days=window_days) <= end_date:
        w_s = cursor
        w_e = cursor + timedelta(days=window_days)
        windows.append((w_s.strftime("%Y-%m-%d"), w_e.strftime("%Y-%m-%d")))
        cursor += timedelta(days=step_days)

    print(f"\n🔥 Turtle Aggressive v2 — 月20-30%挑戦版")
    print(f"{'='*90}")
    print(f"期間: {analysis_start.strftime('%Y-%m-%d')} 〜 {end_date.strftime('%Y-%m-%d')}")
    print(f"銘柄: {len(SYMBOLS)}通貨 / 各$10,000")
    print(f"強化: Dual System1&2 + Long-only + 適応レバ(3/4/5x) + Maker手数料 + リスク2%")
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
    print(f"  📊 Aggressive Turtle v2 集計")
    print(f"{'='*90}")
    print(f"  ウィンドウ数          : {len(rets)}")
    print(f"  平均月次リターン      : {np.mean(rets):+.2f}%")
    print(f"  中央値                : {np.median(rets):+.2f}%")
    print(f"  最高                  : {np.max(rets):+.2f}%")
    print(f"  最低                  : {np.min(rets):+.2f}%")
    print(f"  標準偏差              : {np.std(rets):.2f}%")
    print(f"  プラス月              : {sum(1 for r in rets if r > 0)}/{len(rets)} ({sum(1 for r in rets if r > 0)/len(rets)*100:.0f}%)")
    print(f"  +30%以上              : {sum(1 for r in rets if r >= 30)}/{len(rets)}")
    print(f"  +20%以上              : {sum(1 for r in rets if r >= 20)}/{len(rets)}")
    print(f"  +15%以上              : {sum(1 for r in rets if r >= 15)}/{len(rets)}")
    print(f"  +10%以上              : {sum(1 for r in rets if r >= 10)}/{len(rets)}")
    print(f"  -10%以下              : {sum(1 for r in rets if r <= -10)}/{len(rets)}")
    print(f"  -20%以下              : {sum(1 for r in rets if r <= -20)}/{len(rets)}")
    print(f"  1ウィンドウ平均取引数 : {total_trades_all/len(rets):.1f}")

    # 複利シミュレーション
    balance = 100000.0
    for r in rets:
        balance *= (1 + r / 100)
    months = len(rets) / 2.0
    monthly_comp = ((balance / 100000) ** (1 / months) - 1) * 100 if balance > 0 else -100
    annual = (balance / 100000 - 1) * 100
    print(f"\n  💰 複利シミュ ($100k)")
    print(f"  {'-'*60}")
    print(f"  最終残高            : ${balance:,.0f}")
    print(f"  年率(1年)           : {annual:+.2f}%")
    print(f"  月次複利            : {monthly_comp:+.2f}%")

    # Max DD
    equity = [100000.0]
    for r in rets:
        equity.append(equity[-1] * (1 + r / 100))
    peak = equity[0]
    max_dd = 0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        max_dd = max(max_dd, dd)
    print(f"  最大ドローダウン     : {max_dd:.2f}%")

    # 全戦略比較
    print(f"\n  🏆 全戦略比較 (平均月次 / DD / 勝率)")
    print(f"  {'='*70}")
    print(f"  {'Buy&Hold':<25s}: {'+0.41%':>8s}  DD-39%  勝率57%")
    print(f"  {'v95.0 (10通貨)':<25s}: {'-9.70%':>8s}  DD-36%  勝率30%")
    print(f"  {'v95.0 + Regime':<25s}: {'-1.69%':>8s}")
    print(f"  {'🐢 Turtle Classic':<25s}: {'+0.20%':>8s}  DD< 5%  勝率26%")
    print(f"  {'🔥 Turtle Aggressive':<25s}: {f'{np.mean(rets):+.2f}%':>8s}  "
          f"DD{max_dd:.0f}%  "
          f"勝率{sum(1 for r in rets if r > 0)/len(rets)*100:.0f}%")
    print(f"  {'='*70}\n")


if __name__ == "__main__":
    main()
