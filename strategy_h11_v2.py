"""
H11 v2 (Alpha戦略) — ガチホに勝つための改善版
=======================================================

改善ポイント:
  1. USDT高金利化 (年3% → 年8%, USDE/Aave想定)
  2. マルチ指標トレンド判定 (EMA200 + EMA50 + ADX + RSI) で偽シグナル削減
  3. Bull時のダイナミックレバレッジ (1.0x〜1.5x)
  4. Bear時の小口ショート (10-20%)
  5. ACH二段階モメンタムフィルタ (30日&90日両方上位)
  6. ATRベーストレイリングストップ
  7. 税引き後ベース評価 (雑所得55% vs ガチホ20%)

使い方:
  from strategy_h11_v2 import H11V2Strategy
  strat = H11V2Strategy()
  decisions = strat.evaluate(market_state)
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 設定パラメータ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dataclass
class H11V2Config:
    # 資金配分（レジーム別ベース）
    bull_strong_risk_pct: float = 0.90   # 強気確認時 総資産の90%をリスク資産へ
    bull_weak_risk_pct: float = 0.60
    neutral_risk_pct: float = 0.40
    bear_weak_risk_pct: float = 0.20
    bear_strong_short_pct: float = 0.15  # 確定ベア時 15%でショート

    # BTC:ACH 比率 (リスク資産内で)
    btc_share: float = 0.50  # リスク資産の50%をBTC, 50%をACH
    ach_share: float = 0.50

    # レバレッジ設定
    leverage_bull_strong: float = 1.5
    leverage_bull_weak: float = 1.0
    leverage_neutral: float = 1.0
    leverage_bear_short: float = 1.0  # ショートは1倍に抑える

    # USDTパート高金利 (USDE/Aave 想定)
    usdt_annual_rate: float = 0.08  # 年8%

    # マルチ指標判定
    ema_fast: int = 50
    ema_slow: int = 200
    adx_period: int = 14
    adx_trend_threshold: float = 25
    rsi_period: int = 14
    rsi_overbought: float = 75
    rsi_oversold: float = 25

    # ACH設定
    ach_momentum_short_days: int = 30
    ach_momentum_long_days: int = 90
    ach_top_n: int = 3
    ach_min_short_momentum: float = 5.0   # 30日で+5%以上
    ach_min_long_momentum: float = 10.0   # 90日で+10%以上
    ach_rebalance_days: int = 14          # 2週間ごと (月次→2週で鮮度UP)
    ach_volatility_penalty: float = 0.3   # ボラ調整の重み
    ach_correlation_max: float = 0.85     # 相関 > 0.85 なら分散不足 → 2銘柄に削減

    # リスク管理
    atr_period: int = 14
    trailing_stop_atr_mult: float = 3.0   # ATR×3 がトレイリング幅
    max_drawdown_pct: float = 25          # 最大DD 25%でクールダウン
    cooldown_days: int = 14

    # 税金設定 (シミュレーション用)
    short_term_tax_rate: float = 0.55  # 雑所得: 最大55%
    long_term_tax_rate: float = 0.20   # 長期譲渡: 2028以降 20% (予定)
    long_term_threshold_days: int = 365


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 指標計算ユーティリティ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]; low = df["low"]; close = df["close"]
    plus_dm = (high.diff()).where((high.diff() > low.diff().abs()) & (high.diff() > 0), 0)
    minus_dm = (-low.diff()).where((low.diff().abs() > high.diff()) & (low.diff() < 0), 0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1/period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]; low = df["low"]; close = df["close"]
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# レジーム判定 (4段階)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def detect_regime(btc_df: pd.DataFrame, cfg: H11V2Config) -> str:
    """
    BTCの価格DFから現在のマーケットレジームを返す。
    Returns: "bull_strong", "bull_weak", "neutral", "bear_weak", "bear_strong"
    """
    df = btc_df.copy()
    df["ema50"] = ema(df["close"], cfg.ema_fast)
    df["ema200"] = ema(df["close"], cfg.ema_slow)
    df["rsi"] = rsi(df["close"], cfg.rsi_period)
    df["adx"] = adx(df, cfg.adx_period)

    last = df.iloc[-1]
    price = last["close"]
    e50 = last["ema50"]
    e200 = last["ema200"]
    r = last["rsi"]
    a = last["adx"]

    # スコア化 (4指標)
    score = 0
    if price > e50: score += 1
    if price > e200: score += 2  # 長期が重要
    if e50 > e200: score += 1    # ゴールデンクロス
    if r > 50: score += 0.5
    if a > cfg.adx_trend_threshold and price > e200: score += 1
    if a > cfg.adx_trend_threshold and price < e200: score -= 1  # 強い下落トレンド

    # レジーム判定
    if score >= 4.5:
        return "bull_strong"
    elif score >= 3.0:
        return "bull_weak"
    elif score >= 1.5:
        return "neutral"
    elif score >= 0:
        return "bear_weak"
    else:
        return "bear_strong"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ACH: 二段階モメンタム選定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def select_momentum_candidates(
    data: dict[str, pd.DataFrame],
    current_date: pd.Timestamp,
    cfg: H11V2Config,
) -> list[dict]:
    """
    二段階フィルタでモメンタム上位銘柄を選定:
      1. 30日・90日 両方のリターンが閾値以上
      2. ボラ調整Sharpe相当でスコア化
      3. 相関高いペアは除外して分散確保
    """
    candidates = []
    for sym, df in data.items():
        if df is None or len(df) < cfg.ach_momentum_long_days + 5:
            continue
        if current_date not in df.index:
            continue

        idx = df.index.get_loc(current_date)
        if idx < cfg.ach_momentum_long_days:
            continue

        closes = df["close"].iloc[:idx+1]
        # 30日・90日リターン
        r_short = (closes.iloc[-1] / closes.iloc[-cfg.ach_momentum_short_days] - 1) * 100
        r_long = (closes.iloc[-1] / closes.iloc[-cfg.ach_momentum_long_days] - 1) * 100

        # 両方の閾値を満たす銘柄のみ
        if r_short < cfg.ach_min_short_momentum or r_long < cfg.ach_min_long_momentum:
            continue

        # ボラ調整 (30日日次リターンの標準偏差)
        daily_ret = closes.iloc[-cfg.ach_momentum_short_days:].pct_change().dropna()
        vol = daily_ret.std() * np.sqrt(365) * 100 if len(daily_ret) > 5 else 100
        sharpe_like = r_long / max(vol, 1)  # ボラが大きいほどペナルティ

        # スコア = 両モメンタムの平均 + Sharpe調整
        score = (r_short + r_long) / 2 + sharpe_like * cfg.ach_volatility_penalty * 10

        candidates.append({
            "symbol": sym,
            "r_short": r_short,
            "r_long": r_long,
            "vol": vol,
            "sharpe": sharpe_like,
            "score": score,
            "price": float(closes.iloc[-1]),
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[: cfg.ach_top_n * 2]  # 余裕を持って上位6銘柄


def diversify_by_correlation(
    candidates: list[dict],
    data: dict[str, pd.DataFrame],
    current_date: pd.Timestamp,
    cfg: H11V2Config,
) -> list[dict]:
    """
    相関高いペアを除外して top_n を確保:
      - 候補の中から score 順に追加していき、既選定との相関が
        cfg.ach_correlation_max を超えるものはスキップ
    """
    if not candidates:
        return []

    # 相関計算用の日次リターン系列
    returns_map = {}
    for c in candidates:
        sym = c["symbol"]
        df = data[sym]
        idx = df.index.get_loc(current_date)
        closes = df["close"].iloc[max(0, idx-cfg.ach_momentum_short_days):idx+1]
        returns_map[sym] = closes.pct_change().dropna()

    selected = []
    for c in candidates:
        if len(selected) >= cfg.ach_top_n:
            break
        # 既選定との相関チェック
        ok = True
        for s in selected:
            r1 = returns_map[c["symbol"]]
            r2 = returns_map[s["symbol"]]
            # indexを揃える
            common = r1.index.intersection(r2.index)
            if len(common) < 10:
                continue
            corr = r1.loc[common].corr(r2.loc[common])
            if corr > cfg.ach_correlation_max:
                ok = False
                break
        if ok:
            selected.append(c)

    return selected


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 配分決定: レジームからリスク資産・USDT・ショート配分を返す
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dataclass
class AllocationPlan:
    regime: str
    risk_asset_pct: float         # BTC + ACH 合計
    btc_pct: float
    ach_pct: float
    usdt_pct: float
    short_pct: float              # ベア時のショート枠
    leverage: float               # BTC部分のレバレッジ


def compute_allocation(regime: str, cfg: H11V2Config) -> AllocationPlan:
    """レジームから各枠の配分比率を決定"""
    if regime == "bull_strong":
        risk = cfg.bull_strong_risk_pct
        lev = cfg.leverage_bull_strong
        short = 0.0
    elif regime == "bull_weak":
        risk = cfg.bull_weak_risk_pct
        lev = cfg.leverage_bull_weak
        short = 0.0
    elif regime == "neutral":
        risk = cfg.neutral_risk_pct
        lev = cfg.leverage_neutral
        short = 0.0
    elif regime == "bear_weak":
        risk = cfg.bear_weak_risk_pct
        lev = 1.0
        short = 0.05  # 5%のみ軽くショート
    else:  # bear_strong
        risk = 0.0
        lev = 1.0
        short = cfg.bear_strong_short_pct

    btc = risk * cfg.btc_share
    ach = risk * cfg.ach_share
    usdt = 1.0 - risk - short

    return AllocationPlan(
        regime=regime,
        risk_asset_pct=risk,
        btc_pct=btc,
        ach_pct=ach,
        usdt_pct=usdt,
        short_pct=short,
        leverage=lev,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 税金シミュレーション
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def apply_tax_on_realized(pnl_usd: float, holding_days: int, cfg: H11V2Config) -> float:
    """
    実現損益に税金を適用し、税引き後P&Lを返す。
    含み損は非課税。
    """
    if pnl_usd <= 0:
        return pnl_usd  # 損失は課税対象外 (ただし損益通算はモデル外)

    rate = cfg.long_term_tax_rate if holding_days >= cfg.long_term_threshold_days else cfg.short_term_tax_rate
    tax = pnl_usd * rate
    return pnl_usd - tax


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メインクラス: H11 v2 Strategy
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class H11V2Strategy:
    """
    H11 v2 戦略本体。
    毎tick: evaluate(current_date, btc_df, all_data, state) を呼ぶ。
    戻り値: (regime, allocation, selected_symbols, action_signals)
    """
    def __init__(self, cfg: H11V2Config = None):
        self.cfg = cfg or H11V2Config()

    def evaluate(self, current_date, btc_df, all_data):
        """
        Returns: dict with keys
          regime, allocation (AllocationPlan), top_candidates (list)
        """
        regime = detect_regime(btc_df, self.cfg)
        allocation = compute_allocation(regime, self.cfg)

        # ベア時はACH選定不要
        if regime in ("bear_strong", "bear_weak"):
            return {
                "regime": regime,
                "allocation": allocation,
                "top_candidates": [],
            }

        # モメンタム選定
        candidates = select_momentum_candidates(all_data, current_date, self.cfg)
        selected = diversify_by_correlation(candidates, all_data, current_date, self.cfg)

        return {
            "regime": regime,
            "allocation": allocation,
            "top_candidates": selected,
        }


if __name__ == "__main__":
    # 簡易テスト
    print("H11 v2 戦略モジュール読み込み OK")
    cfg = H11V2Config()
    print(f"設定: USDT利率={cfg.usdt_annual_rate*100}%")
    print(f"      Bull Strong レバ={cfg.leverage_bull_strong}x")
    print(f"      ACH リバランス={cfg.ach_rebalance_days}日")
