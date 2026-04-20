"""
strategy_racsm.py — Regime-Aware Cross-Sectional Momentum
========================================================
学術的根拠:
  - Jegadeesh & Titman (1993): Cross-Sectional Momentum (銘柄間相対強度)
  - Moskowitz, Ooi, Pedersen (2012): Time-Series Momentum (時系列モメンタム)
  - Antonacci: Dual Momentum (絶対モメンタム+相対モメンタム)

ロジック:
  1. BTC レジームフィルタ (日足 EMA200 上 & EMA50>EMA200)
  2. 候補銘柄プールから過去30日+90日の相対強度スコアを計算
  3. Dual Momentum: 過去3ヶ月リターン>0 のみ残す
  4. Top N=5 を選択、逆ボラ加重でリスク均等配分
  5. 週次 or 月次リバランス
  6. ポートフォリオDD-10%超 or BTCがEMA200割れで全撤退

使用データ: Binance 日足 OHLCV (data_fetcher経由、合成禁止)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
import numpy as np


# ═══ パラメータ（固定・保守的） ═══
LOOKBACK_SHORT = 30       # 短期モメンタム (日)
LOOKBACK_LONG  = 90       # 長期モメンタム (日)
ABS_MOM_DAYS   = 90       # Dual Momentum: 絶対モメンタム窓 (日)
TOP_N          = 5        # 保有銘柄数
ATR_WINDOW     = 14       # 逆ボラ加重用 ATR
EMA_SHORT      = 50
EMA_LONG       = 200
DD_STOP_PCT    = 0.10     # ポートフォリオDD-10%で撤退
LEVERAGE       = 1.5      # 保守レバ


# ═══ サバイバルバイアス対策: 2023年以前から安定的に取引されている上位銘柄 ═══
# これらは Binance で 2023-01-01 以前から上場している主要銘柄
UNIVERSE = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
    "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT", "MATIC/USDT",
    "ATOM/USDT", "UNI/USDT", "LTC/USDT", "BCH/USDT", "ETC/USDT",
    "NEAR/USDT", "FIL/USDT", "ICP/USDT", "DOGE/USDT", "SHIB/USDT",
    "TRX/USDT", "XLM/USDT", "ALGO/USDT", "VET/USDT", "HBAR/USDT",
    "EGLD/USDT", "FTM/USDT", "AAVE/USDT", "SAND/USDT", "MANA/USDT",
]


# ═══ データ構造 ═══
@dataclass
class Position:
    symbol: str
    entry_price: float
    qty: float
    entry_ts: pd.Timestamp
    target_weight: float = 0.0

    def notional_at(self, price: float) -> float:
        return self.qty * price * LEVERAGE

    def pnl_at(self, price: float) -> float:
        return (price - self.entry_price) * self.qty * LEVERAGE


# ═══ レジームフィルタ ═══
def check_btc_regime(df_btc: pd.DataFrame, as_of: pd.Timestamp) -> bool:
    """
    BTCが強気レジームか判定。
    条件: close > EMA200 かつ EMA50 > EMA200
    """
    df = df_btc[df_btc.index <= as_of]
    if len(df) < EMA_LONG + 5:
        return False
    close = df["close"]
    ema50  = close.ewm(span=EMA_SHORT, adjust=False).mean()
    ema200 = close.ewm(span=EMA_LONG,  adjust=False).mean()
    c = close.iloc[-1]
    e50, e200 = ema50.iloc[-1], ema200.iloc[-1]
    return (c > e200) and (e50 > e200)


# ═══ モメンタム計算 ═══
def compute_momentum_scores(ohlcv_map: dict[str, pd.DataFrame],
                             as_of: pd.Timestamp) -> pd.DataFrame:
    """
    各銘柄の過去 30日・90日 リターンを z-score化して平均した総合スコアを返す。
    """
    rows = []
    for sym, df in ohlcv_map.items():
        df = df[df.index <= as_of]
        if len(df) < LOOKBACK_LONG + 5:
            continue
        close = df["close"].iloc[-1]
        r30 = close / df["close"].iloc[-LOOKBACK_SHORT] - 1
        r90 = close / df["close"].iloc[-LOOKBACK_LONG]  - 1
        rows.append({"symbol": sym, "r30": r30, "r90": r90})

    if not rows:
        return pd.DataFrame(columns=["symbol", "score", "r30", "r90"])

    df_scores = pd.DataFrame(rows)
    for col in ("r30", "r90"):
        mu, sd = df_scores[col].mean(), df_scores[col].std(ddof=0)
        df_scores[f"z_{col}"] = (df_scores[col] - mu) / sd if sd > 0 else 0.0
    df_scores["score"] = (df_scores["z_r30"] + df_scores["z_r90"]) / 2
    return df_scores.sort_values("score", ascending=False).reset_index(drop=True)


def apply_absolute_momentum(scores: pd.DataFrame,
                             ohlcv_map: dict[str, pd.DataFrame],
                             as_of: pd.Timestamp) -> pd.DataFrame:
    """
    Dual Momentum: 過去 ABS_MOM_DAYS のリターンがプラスの銘柄のみ残す。
    """
    keep = []
    for _, row in scores.iterrows():
        df = ohlcv_map[row["symbol"]]
        df = df[df.index <= as_of]
        if len(df) < ABS_MOM_DAYS + 1:
            continue
        ret = df["close"].iloc[-1] / df["close"].iloc[-ABS_MOM_DAYS] - 1
        if ret > 0:
            keep.append(row["symbol"])
    return scores[scores["symbol"].isin(keep)].reset_index(drop=True)


def select_top_n(scores: pd.DataFrame, n: int = TOP_N) -> list[str]:
    return scores.head(n)["symbol"].tolist()


# ═══ 逆ボラ加重 ═══
def compute_inverse_vol_weights(ohlcv_map: dict[str, pd.DataFrame],
                                  symbols: list[str],
                                  as_of: pd.Timestamp) -> dict[str, float]:
    """
    各銘柄のATR（%）の逆数で正規化 → リスク均等配分。
    """
    vols = {}
    for sym in symbols:
        df = ohlcv_map[sym]
        df = df[df.index <= as_of]
        if len(df) < ATR_WINDOW + 5:
            continue
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(ATR_WINDOW).mean().iloc[-1]
        price = df["close"].iloc[-1]
        atr_pct = atr / price if price > 0 else 0.01
        vols[sym] = 1.0 / max(atr_pct, 0.005)  # ゼロ除算防止

    if not vols:
        return {}
    total = sum(vols.values())
    return {s: v / total for s, v in vols.items()}


# ═══ ポートフォリオ撤退判定 ═══
def check_portfolio_stop(equity_curve: list[float],
                          btc_df: pd.DataFrame,
                          as_of: pd.Timestamp) -> tuple[bool, str]:
    """
    撤退条件: 1) ピークから DD_STOP_PCT 超、 2) BTCがEMA200割れ
    """
    if equity_curve:
        peak = max(equity_curve)
        current = equity_curve[-1]
        dd = (peak - current) / peak if peak > 0 else 0
        if dd >= DD_STOP_PCT:
            return True, f"portfolio_dd_{dd*100:.1f}pct"

    df = btc_df[btc_df.index <= as_of]
    if len(df) >= EMA_LONG + 5:
        ema200 = df["close"].ewm(span=EMA_LONG, adjust=False).mean().iloc[-1]
        if df["close"].iloc[-1] < ema200:
            return True, "btc_below_ema200"
    return False, ""
