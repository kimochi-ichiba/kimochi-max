"""
metrics.py — グリッドサーチ用のメトリクス集計
=============================================
equity_curve (list of dict) + trades (list of dict) から 10 項目の指標 +
symbol 別 + regime 別のブレイクダウンを計算する純粋関数群。

設計方針:
  - 既存の utils.calc_sharpe_ratio / calc_max_drawdown / calc_profit_factor
    および _walk_forward_verify.cagr / sharpe / max_drawdown_from_returns を
    パターン踏襲。新規実装は max_losing_streak と exposure と regime/symbol 別。
  - equity_curve の要素は {"ts": datetime-like, "equity": float} で統一。
  - trades の要素は {"symbol": str, "entry_ts", "exit_ts", "pnl": float,
    "won": bool, ...} で統一。
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Iterable

import pandas as pd


# ─────────────────────────────
# 週次リターン / 基本統計
# ─────────────────────────────
def weekly_returns(equity_curve: list[dict]) -> list[float]:
    """equity_curve から週次リターン列を作る。

    入力が日次・月次のどちらでも、pandas の resample("W") で週次化してから
    pct_change を取る。週次リターンは [-1, ∞) の実数。
    """
    if len(equity_curve) < 2:
        return []
    df = pd.DataFrame(equity_curve).set_index("ts")
    df.index = pd.to_datetime(df.index)
    weekly = df["equity"].resample("W").last().dropna()
    rets = weekly.pct_change().dropna().tolist()
    return [float(r) for r in rets]


def cagr_from_equity(equity_curve: list[dict]) -> float:
    """equity_curve から CAGR (%) を計算。実経過日数ベースで年率化。"""
    if len(equity_curve) < 2:
        return 0.0
    df = pd.DataFrame(equity_curve).set_index("ts")
    df.index = pd.to_datetime(df.index)
    initial = float(df["equity"].iloc[0])
    final = float(df["equity"].iloc[-1])
    if initial <= 0 or final <= 0:
        return 0.0
    days = (df.index[-1] - df.index[0]).days
    if days <= 0:
        return 0.0
    years = days / 365.25
    if years <= 0:
        return 0.0
    return (final / initial) ** (1.0 / years) * 100.0 - 100.0


def yearly_returns(equity_curve: list[dict]) -> dict[str, float]:
    """年別リターン (%) を dict {year: pct} で返す。"""
    if len(equity_curve) < 2:
        return {}
    df = pd.DataFrame(equity_curve).set_index("ts")
    df.index = pd.to_datetime(df.index)
    by_year = df["equity"].resample("YE").last().dropna()
    out: dict[str, float] = {}
    prev: float | None = None
    # 初期 equity = 最初の値の直前 (= 初日の equity) と同値扱い
    initial = float(df["equity"].iloc[0])
    for ts, val in by_year.items():
        base = prev if prev is not None else initial
        if base > 0:
            out[str(ts.year)] = round((float(val) / base - 1.0) * 100.0, 4)
        prev = float(val)
    return out


def max_drawdown_pct(equity_curve: list[dict]) -> float:
    """equity_curve から最大 DD (%) を計算 (正の数)."""
    if not equity_curve:
        return 0.0
    equities = [float(pt["equity"]) for pt in equity_curve]
    peak = equities[0]
    max_dd = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return max_dd


def sharpe_ratio(returns: list[float], periods_per_year: int = 52) -> float:
    """週次リターンから年率 Sharpe (rf=0)."""
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    stdev = statistics.pstdev(returns)
    if stdev <= 0:
        return 0.0
    return float((mean / stdev) * math.sqrt(periods_per_year))


# ─────────────────────────────
# トレード系指標
# ─────────────────────────────
def profit_factor(trades: list[dict]) -> float:
    """総利益 / 総損失 (損失 0 の時は inf、両方 0 の時は 0)."""
    gross_profit = sum(t["pnl"] for t in trades if t.get("pnl", 0.0) > 0)
    gross_loss = sum(abs(t["pnl"]) for t in trades if t.get("pnl", 0.0) < 0)
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def win_rate(trades: list[dict]) -> float:
    """勝率 (%)."""
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.get("won", t.get("pnl", 0.0) > 0))
    return wins / len(trades) * 100.0


def expectancy(trades: list[dict]) -> float:
    """1 トレードあたりの平均 PnL (通貨建て)."""
    if not trades:
        return 0.0
    return sum(t.get("pnl", 0.0) for t in trades) / len(trades)


def max_losing_streak(trades: list[dict]) -> int:
    """連続負けトレードの最大長. trades は時系列順前提."""
    max_streak = 0
    current = 0
    for t in trades:
        won = t.get("won", t.get("pnl", 0.0) > 0)
        if not won:
            current += 1
            if current > max_streak:
                max_streak = current
        else:
            current = 0
    return max_streak


def exposure(trades: list[dict], equity_curve: list[dict]) -> float:
    """市場参加率 = (ポジション保有日数合計) / (全期間日数).

    trades の entry_ts / exit_ts を datetime 化して保有日数を合算、
    equity_curve の期間で割る。重複期間は加算（複数ポジション同時保有時は
    exposure が 1.0 を超え得る）。
    """
    if not trades or len(equity_curve) < 2:
        return 0.0
    total_days = (
        pd.to_datetime(equity_curve[-1]["ts"])
        - pd.to_datetime(equity_curve[0]["ts"])
    ).days
    if total_days <= 0:
        return 0.0
    held = 0.0
    for t in trades:
        entry = pd.to_datetime(t.get("entry_ts"))
        exit_ = pd.to_datetime(t.get("exit_ts"))
        if entry is pd.NaT or exit_ is pd.NaT:
            continue
        held += max((exit_ - entry).total_seconds() / 86400.0, 0.0)
    return held / total_days


# ─────────────────────────────
# symbol / regime 別ブレイクダウン
# ─────────────────────────────
def symbol_breakdown(trades: list[dict]) -> dict[str, dict[str, Any]]:
    """銘柄ごとに trades を集計。{symbol: {n, pnl_sum, win_rate, pf, expectancy}}"""
    out: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict]] = {}
    for t in trades:
        sym = t.get("symbol", "UNKNOWN")
        grouped.setdefault(sym, []).append(t)
    for sym, ts in grouped.items():
        out[sym] = {
            "n_trades": len(ts),
            "pnl_sum": round(sum(t.get("pnl", 0.0) for t in ts), 4),
            "win_rate_pct": round(win_rate(ts), 2),
            "profit_factor": round(profit_factor(ts), 3)
            if profit_factor(ts) != float("inf")
            else float("inf"),
            "expectancy": round(expectancy(ts), 4),
        }
    return out


def regime_breakdown(
    equity_curve: list[dict],
    btc_ema200_bool: pd.Series | None = None,
) -> dict[str, dict[str, float]]:
    """BTC EMA200 上下を bull/bear として equity_curve を 2 分し、
    それぞれの区間で CAGR / MaxDD / 週数を計算。

    btc_ema200_bool が None の時は equity_curve の平均を超えるかで代替分類 (粗い)。
    """
    if len(equity_curve) < 2:
        return {"bull": {}, "bear": {}}
    df = pd.DataFrame(equity_curve).set_index("ts")
    df.index = pd.to_datetime(df.index)
    weekly = df["equity"].resample("W").last().dropna()

    if btc_ema200_bool is not None and len(btc_ema200_bool) > 0:
        # btc_ema200_bool は close > ema200 の bool Series (True=bull)
        aligned = btc_ema200_bool.reindex(weekly.index, method="ffill").fillna(False)
        bull_mask = aligned.astype(bool)
    else:
        median = weekly.median()
        bull_mask = weekly > median

    out: dict[str, dict[str, float]] = {}
    for label, mask in [("bull", bull_mask), ("bear", ~bull_mask)]:
        segment = weekly[mask]
        if len(segment) < 2:
            out[label] = {"weeks": len(segment), "return_pct": 0.0, "max_dd_pct": 0.0}
            continue
        rets = segment.pct_change().dropna().tolist()
        if not rets:
            out[label] = {"weeks": len(segment), "return_pct": 0.0, "max_dd_pct": 0.0}
            continue
        total = 1.0
        for r in rets:
            total *= 1.0 + float(r)
        out[label] = {
            "weeks": len(segment),
            "return_pct": round((total - 1.0) * 100.0, 2),
            "max_dd_pct": round(_dd_from_rets(rets), 2),
        }
    return out


def _dd_from_rets(rets: Iterable[float]) -> float:
    """週次リターン列から MaxDD (%) を計算."""
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in rets:
        equity *= 1.0 + float(r)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd * 100.0


# ─────────────────────────────
# 統合集計
# ─────────────────────────────
def compute_all_metrics(
    equity_curve: list[dict],
    trades: list[dict],
    *,
    btc_ema200_bool: pd.Series | None = None,
) -> dict[str, Any]:
    """ユーザー指定 10 項目 + symbol/regime 別を一括計算."""
    rets = weekly_returns(equity_curve)
    pf = profit_factor(trades)
    return {
        "cagr_pct": round(cagr_from_equity(equity_curve), 3),
        "yearly_return_pct": yearly_returns(equity_curve),
        "max_drawdown_pct": round(max_drawdown_pct(equity_curve), 3),
        "profit_factor": round(pf, 3) if pf != float("inf") else float("inf"),
        "win_rate_pct": round(win_rate(trades), 3),
        "expectancy": round(expectancy(trades), 4),
        "max_losing_streak": max_losing_streak(trades),
        "exposure": round(exposure(trades, equity_curve), 4),
        "sharpe_ratio": round(sharpe_ratio(rets), 3),
        "num_trades": len(trades),
        "num_weeks": len(rets) + 1 if rets else 0,
        "symbol_breakdown": symbol_breakdown(trades),
        "regime_breakdown": regime_breakdown(equity_curve, btc_ema200_bool),
    }
