"""
strategy.py — 多時間軸合議制シグナル生成モジュール
====================================================
「3時間軸のうち最低2つが同じ方向を示している場合だけエントリー」する
合議制（コンセンサス）システムを実装する。

1つの時間軸だけのシグナルは「ダマシ」が多い。
複数の時間軸が一致して初めて高確度のシグナルと判断する考え方。

シグナルスコア（0.0〜1.0）:
  各時間軸で EMA, MACD, RSI, Stochastics, BB の5指標を評価し、
  それぞれ0か1のスコアを付ける。スコアが高いほど強いシグナル。

  合議スコア = 全指標スコアの平均 × 合意した時間軸数 / 全時間軸数
"""

import logging
from typing import Optional
import pandas as pd
import numpy as np

from config import Config
from indicators import (
    add_all_indicators, get_latest_row,
    is_ema_bullish, is_ema_bearish,
    is_macd_bullish, is_macd_bearish,
    is_rsi_bullish, is_rsi_bearish,
    is_high_volatility, is_ranging_market,
    is_volume_confirmed,
    is_adx_trending, detect_rsi_divergence,
    is_vwap_bullish, is_vwap_bearish,
)
from utils import setup_logger

logger = setup_logger("strategy")


# ════════════════════════════════════════════════════
# v5.0 防御パターン: 高リスク相場の検出（31個のうち主要なもの）
# ════════════════════════════════════════════════════

def detect_high_risk_conditions(df: pd.DataFrame, direction: str) -> tuple[bool, str]:
    """
    v5.0 防御パターンシステム: エントリーしてはいけない状況を検出する。

    なぜ必要か:
      「いつエントリーするか」と同じくらい「いつエントリーしないか」が重要。
      危険な状況でのエントリーは勝率を大幅に下げる。

    検出する危険パターン:
      D-01: 急騰落直後（3本で±2%以上）→ 追いかけエントリー禁止
      D-02: 長いヒゲ（ダマシの動き）直後 → 逆方向エントリー禁止
      D-03: BBスクイーズ（静止相場）→ エントリー禁止
      D-04: 価格が重要EMAから3ATR以上離れた「オーバーエクステンション」
      D-05: 直近3本に上下どちらにも大きなヒゲがある（迷相場）

    戻り値:
      (True, 理由) = 危険 → エントリーしない方が良い
      (False, "")  = 問題なし → 通過
    """
    if df is None or df.empty or len(df) < 5:
        return False, ""

    try:
        close = df["close"]
        high  = df["high"]
        low   = df["low"]
        c1 = df.iloc[-1]
        atr_col = df["atr"].iloc[-1] if "atr" in df.columns else None

        # ── D-01: 急騰落直後チェック（3本で±2%以上）────────────
        # 「すでに急騰した後に追いかける」のは最も損しやすいパターン。
        # 急騰: ロングエントリーはすでに遅れている。
        # 急落: ショートエントリーはすでに遅れている（かつ反発リスク大）。
        if len(df) >= 4:
            c_now   = float(close.iloc[-1])
            c_3ago  = float(close.iloc[-4])
            if c_3ago > 0:
                move_3bar = (c_now - c_3ago) / c_3ago
                if direction == "long" and move_3bar >= 0.020:  # 3本で+2%急騰後LONG禁止
                    return True, f"D-01: 急騰後追いかけLONG禁止({move_3bar*100:.1f}%/3本)"
                if direction == "short" and move_3bar <= -0.020:  # 3本で-2%急落後SHORT禁止
                    return True, f"D-01: 急落後追いかけSHORT禁止({move_3bar*100:.1f}%/3本)"

        # ── D-02: 長いヒゲ（ダマシ）直後の逆方向エントリー禁止 ────
        # 長い上ヒゲ（価格が上に弾かれた）後にLONGすると、弾かれた方向に再び動く。
        # 長い下ヒゲ後にSHORTすると同様のリスク。
        o, h, l, c = float(c1["open"]), float(c1["high"]), float(c1["low"]), float(c1["close"])
        rng = h - l
        if rng > 0:
            upper_wick = h - max(c, o)
            lower_wick = min(c, o) - l
            if direction == "long" and upper_wick / rng >= 0.70:
                # 上ヒゲが70%以上 = 上値が重い → LONG危険（v7.3: 60%→70%に緩和）
                return True, f"D-02: 上ヒゲ{upper_wick/rng*100:.0f}%の足後LONG危険"
            if direction == "short" and lower_wick / rng >= 0.70:
                # 下ヒゲが70%以上 = 下値が堅い → SHORT危険（v7.3: 60%→70%に緩和）
                # 注: デッドキャットバウンス時はtrading_botで免除済み（_is_dead_cat_flip）
                return True, f"D-02: 下ヒゲ{lower_wick/rng*100:.0f}%の足後SHORT危険"

        # ── D-04: オーバーエクステンション（乖離過大）───────────
        # EMA21から価格が3ATR以上離れている = 「行き過ぎ」の状態。
        # 行き過ぎた状態で更に追いかけると、急反転で損切りされやすい。
        if atr_col is not None and not pd.isna(atr_col) and float(atr_col) > 0:
            ema21 = float(close.ewm(span=21, min_periods=5).mean().iloc[-1])
            atr_val = float(atr_col)
            deviation_atr = abs(float(close.iloc[-1]) - ema21) / atr_val
            if direction == "long" and deviation_atr >= 3.5 and float(close.iloc[-1]) > ema21:
                return True, f"D-04: EMA21から{deviation_atr:.1f}ATR乖離（上方過大）→ LONG禁止"
            if direction == "short" and deviation_atr >= 3.5 and float(close.iloc[-1]) < ema21:
                return True, f"D-04: EMA21から{deviation_atr:.1f}ATR乖離（下方過大）→ SHORT禁止"

        # ── D-05: 迷相場（直近3本に上下両方の長いヒゲ）────────────
        # 「どっちに動くか分からない」混乱した状態 = エントリーを控える。
        if len(df) >= 3:
            wicks_confusion = 0
            for i in range(-3, 0):
                row = df.iloc[i]
                r = float(row["high"]) - float(row["low"])
                if r > 0:
                    up_w  = (float(row["high"]) - max(float(row["close"]), float(row["open"]))) / r
                    dn_w  = (min(float(row["close"]), float(row["open"])) - float(row["low"])) / r
                    if up_w >= 0.35 and dn_w >= 0.35:  # 両方向に長いヒゲ（v7.3: 25%→35%に緩和）
                        wicks_confusion += 1
            if wicks_confusion >= 2:
                return True, "D-05: 迷相場（両方向ヒゲ35%以上×2本）→ エントリー見送り"

        return False, ""

    except Exception:
        return False, ""


# ════════════════════════════════════════════════════
# Feature 1: マーケットレジーム（相場環境）判定
# ════════════════════════════════════════════════════

def detect_market_regime(df: pd.DataFrame, config: Config) -> str:
    """
    現在の相場環境を5種類に分類する関数。

    なぜ必要か:
      相場には「トレンド相場」「レンジ相場」「爆発的相場」など
      全く異なる性質の局面がある。同じ戦略を全ての局面で使うと
      損失が増える。相場環境を判定することで適切な戦略を選べる。

    戻り値:
      "TRENDING_UP"   — ADX>25 かつ EMA20>EMA50 かつ 価格>EMA20 （上昇トレンド）
      "TRENDING_DOWN" — ADX>25 かつ EMA20<EMA50 かつ 価格<EMA20 （下降トレンド）
      "RANGING"       — ADX<20 かつ BBバンド幅が狭い            （横ばい相場）
      "EXPLOSIVE"     — ATRが直近20本の平均ATRの2倍超            （爆発的相場）
      "NEUTRAL"       — 上記のどれにも当てはまらない             （中立）
    """
    if df is None or df.empty or len(df) < 30:
        return "NEUTRAL"

    try:
        # ── 指標の計算 ─────────────────────────────
        close = df["close"]

        # EMA20 と EMA50 を計算
        ema20 = close.ewm(span=20, min_periods=5).mean()
        ema50 = close.ewm(span=50, min_periods=10).mean()
        ema20_now = float(ema20.iloc[-1])
        ema50_now = float(ema50.iloc[-1])
        price_now = float(close.iloc[-1])

        # ADX（トレンド強度）
        adx_val = 0.0
        if "adx" in df.columns:
            adx_raw = df["adx"].iloc[-1]
            if not pd.isna(adx_raw):
                adx_val = float(adx_raw)

        # ボリンジャーバンド幅（BBバンドの広さ = 相場の動き幅）
        bb_width = 0.0
        if all(c in df.columns for c in ["bb_upper", "bb_lower", "bb_mid"]):
            bb_u = df["bb_upper"].iloc[-1]
            bb_l = df["bb_lower"].iloc[-1]
            bb_m = df["bb_mid"].iloc[-1]
            if not any(pd.isna([bb_u, bb_l, bb_m])) and bb_m > 0:
                bb_width = float((bb_u - bb_l) / bb_m)

        # ATR（ボラティリティ = 相場の揺れ幅）
        atr_now = 0.0
        atr_avg20 = 0.0
        if "atr" in df.columns:
            atr_series = df["atr"].dropna()
            if len(atr_series) >= 5:
                atr_now = float(atr_series.iloc[-1])
            if len(atr_series) >= 20:
                atr_avg20 = float(atr_series.tail(20).mean())

        # ── レジーム判定（優先順位: EXPLOSIVE > TRENDING > RANGING > NEUTRAL）──

        # EXPLOSIVE: ATRが直近平均の2倍超 = 「爆発的な動き」の局面
        if atr_now > 0 and atr_avg20 > 0 and atr_now > atr_avg20 * 2.0:
            return "EXPLOSIVE"

        # TRENDING_UP: ADX>25 かつ EMA20>EMA50 かつ 価格がEMA20より上
        if adx_val > 25 and ema20_now > ema50_now and price_now > ema20_now:
            return "TRENDING_UP"

        # TRENDING_DOWN: ADX>25 かつ EMA20<EMA50 かつ 価格がEMA20より下
        if adx_val > 25 and ema20_now < ema50_now and price_now < ema20_now:
            return "TRENDING_DOWN"

        # RANGING: ADX<20 かつ BBバンド幅が狭い（横ばい相場）
        if adx_val < 20 and bb_width < 0.015:
            return "RANGING"

        # NEUTRAL: どれにも当てはまらない（中間的な状態）
        return "NEUTRAL"

    except Exception as e:
        logger.debug(f"マーケットレジーム判定エラー（NEUTRAL返却）: {e}")
        return "NEUTRAL"


# ════════════════════════════════════════════════════
# シグナル定数
# ════════════════════════════════════════════════════

class Signal:
    """シグナルの種類を表す定数"""
    LONG  = "LONG"   # 買い（ロングエントリー）
    SHORT = "SHORT"  # 売り（ショートエントリー）
    CLOSE = "CLOSE"  # 決済（現在のポジションを閉じる）
    HOLD  = "HOLD"   # 保有継続（何もしない）


# ════════════════════════════════════════════════════
# 1時間軸のシグナル評価
# ════════════════════════════════════════════════════

def evaluate_single_timeframe(df: pd.DataFrame, config: Config) -> dict:
    """
    1つの時間軸について、各指標が上昇・下降どちらを示しているかをスコア化する。

    戻り値:
        {
            "long_score":  float,  # ロングシグナルの強さ（0〜1）
            "short_score": float,  # ショートシグナルの強さ（0〜1）
            "direction":   str,    # "long", "short", "neutral"
            "details":     dict,   # 各指標の評価詳細
        }
    """
    if df.empty:
        return {"long_score": 0.0, "short_score": 0.0, "direction": "neutral", "details": {}}

    # 指標を計算してから最新行を取得
    df_with_ind = add_all_indicators(df, config)
    row = get_latest_row(df_with_ind)

    if row.empty:
        return {"long_score": 0.0, "short_score": 0.0, "direction": "neutral", "details": {}}

    # ── レンジ相場チェック①: ボリンジャーバンド幅 ──
    if is_ranging_market(row, config):
        return {"long_score": 0.0, "short_score": 0.0, "direction": "neutral",
                "details": {}, "ranging": True}

    # ── レンジ相場チェック②: ADXフィルター ──────────
    # ADXが低い（トレンドが弱い）= 横ばい相場 → 取引しない
    # これがあるとトレンドのないときの「ダマシ」シグナルをほぼ排除できる
    adx_ok = is_adx_trending(row, config)
    if not adx_ok:
        adx_val = row.get("adx", 0)
        logger.debug(f"ADX={adx_val:.1f} < {config.adx_threshold} → 横ばい相場のためスキップ")
        return {"long_score": 0.0, "short_score": 0.0, "direction": "neutral",
                "details": {"adx": f"{adx_val:.0f}(弱)"}, "ranging": True}

    # ── 価格モメンタム確認（3本前と比較して方向が合っているか）──
    # 例: ロング候補なのに直近3本の価格が下がっていたらダマシの可能性が高い
    price_momentum_bull = False
    price_momentum_bear = False
    if len(df) >= 5:
        close_now  = df["close"].iloc[-1]
        close_3ago = df["close"].iloc[-4]   # 3本前の終値
        if not pd.isna(close_now) and not pd.isna(close_3ago) and close_3ago > 0:
            momentum_pct = (close_now - close_3ago) / close_3ago
            price_momentum_bull = momentum_pct > 0.0001   # 0.01%以上上昇
            price_momentum_bear = momentum_pct < -0.0001  # 0.01%以上下落

    # ── 各指標を評価 ────────────────────────────
    ema_bull  = is_ema_bullish(row, config)
    ema_bear  = is_ema_bearish(row, config)
    macd_bull = is_macd_bullish(row)
    macd_bear = is_macd_bearish(row)
    rsi_bull  = is_rsi_bullish(row, config)
    rsi_bear  = is_rsi_bearish(row, config)

    stoch_k = row.get("stoch_k", None)
    stoch_d = row.get("stoch_d", None)
    stoch_bull = (
        not pd.isna(stoch_k) and not pd.isna(stoch_d)
        and stoch_k < 50 and stoch_k > stoch_d
    ) if stoch_k is not None and stoch_d is not None else False
    stoch_bear = (
        not pd.isna(stoch_k) and not pd.isna(stoch_d)
        and stoch_k > 50 and stoch_k < stoch_d
    ) if stoch_k is not None and stoch_d is not None else False

    close    = row.get("close")
    bb_upper = row.get("bb_upper")
    bb_lower = row.get("bb_lower")
    bb_mid   = row.get("bb_mid")
    bb_bull = (
        not any(pd.isna([close, bb_mid, bb_lower]))
        and close > bb_mid and close > bb_lower * 1.001
    ) if None not in [close, bb_upper, bb_lower, bb_mid] else False
    bb_bear = (
        not any(pd.isna([close, bb_mid, bb_upper]))
        and close < bb_mid and close < bb_upper * 0.999
    ) if None not in [close, bb_upper, bb_lower, bb_mid] else False

    # ── VWAP判定（プロが使う割高・割安の基準）────────
    vwap_bull = is_vwap_bullish(row)
    vwap_bear = is_vwap_bearish(row)

    # ── Feature 2: MACDヒストグラム加速度フィルター ────
    # 「勢いが今まさに加速している」ことを検知する。
    # MACDヒストグラム（MACD - シグナル線）の値が
    # 2本連続で増加中 = 買い圧力が加速している証拠。
    # 2本連続で減少中（より負に）= 売り圧力が加速している証拠。
    macd_accel_bull = False  # ロング方向のMOCD加速
    macd_accel_bear = False  # ショート方向のMACD加速
    if len(df_with_ind) >= 4:
        try:
            macd_col   = df_with_ind.get("macd") if isinstance(df_with_ind, pd.Series) else (
                df_with_ind["macd"] if "macd" in df_with_ind.columns else None
            )
            signal_col = df_with_ind.get("macd_signal") if isinstance(df_with_ind, pd.Series) else (
                df_with_ind["macd_signal"] if "macd_signal" in df_with_ind.columns else None
            )
            if macd_col is not None and signal_col is not None:
                # 直近3本のヒストグラム値を取得
                hist_m1 = float(macd_col.iloc[-1]) - float(signal_col.iloc[-1])  # 最新本
                hist_m2 = float(macd_col.iloc[-2]) - float(signal_col.iloc[-2])  # 1本前
                hist_m3 = float(macd_col.iloc[-3]) - float(signal_col.iloc[-3])  # 2本前

                # ロング方向の加速: ヒストグラムが2連続で増加（プラス方向に大きくなっている）
                if not pd.isna(hist_m1) and not pd.isna(hist_m2) and not pd.isna(hist_m3):
                    macd_accel_bull = (hist_m1 > hist_m2 > hist_m3 and hist_m1 > 0)
                    # ショート方向の加速: ヒストグラムが2連続で減少（マイナス方向に大きくなっている）
                    macd_accel_bear = (hist_m1 < hist_m2 < hist_m3 and hist_m1 < 0)
        except Exception:
            pass  # エラーは無視して続行

    # ── RSIダイバージェンス（反転シグナルの早期検知）─
    # ダイバージェンスは強いシグナルなので、発生時は追加の1票を与える
    rsi_div_bull, rsi_div_bear = detect_rsi_divergence(df_with_ind)

    # ── 出来高確認（方向中立のフィルター）─────────────
    # vol_okは方向を持たないため、スコア加算ではなく「フィルター」として使う。
    # 出来高が平均未満（偽の動き）の場合は全スコアを0.5倍に減衰させる。
    vol_ok = is_volume_confirmed(row, df, config)
    vol_multiplier = 1.0 if vol_ok else 0.5  # 出来高不足なら信頼度半減

    # ── スコア集計（8指標 + MACD加速0.5票追加）──────────────────
    # RSIダイバージェンスは強いシグナルのため1票ではなく1.5票分として計算
    # 価格モメンタム確認を追加（1票）: 直近3本が方向通りに動いているか
    # Feature 2: MACDヒストグラム加速度を追加（0.5票）: 勢いが加速しているかを検知
    # 0.5票にした理由: 強い加速シグナルだが、単独で支配的にならないよう半票にしている
    long_votes  = [ema_bull, macd_bull, rsi_bull, stoch_bull, bb_bull, vwap_bull,
                   rsi_div_bull, rsi_div_bull * 0.5,
                   price_momentum_bull,          # 価格の実際の動き方向
                   macd_accel_bull * 0.5]         # MACD加速度（半票）
    short_votes = [ema_bear, macd_bear, rsi_bear, stoch_bear, bb_bear, vwap_bear,
                   rsi_div_bear, rsi_div_bear * 0.5,
                   price_momentum_bear,           # 価格の実際の動き方向
                   macd_accel_bear * 0.5]         # MACD加速度（半票）

    # 分母は8（モメンタム1票 + MACD加速0.5票 = 実質8.5票分まで可能だが、8で割ることで
    # 加速シグナルはボーナス的な扱いになりスコアが少し高くなる設計）
    base_count = 8
    long_score  = sum(long_votes)  / base_count * vol_multiplier
    short_score = sum(short_votes) / base_count * vol_multiplier

    # スコアは最大1.0にクリップ
    long_score  = min(long_score,  1.0)
    short_score = min(short_score, 1.0)

    if long_score > short_score and long_score >= 0.25:
        direction = "long"
    elif short_score > long_score and short_score >= 0.25:
        direction = "short"
    else:
        direction = "neutral"

    adx_val = row.get("adx", 0) or 0
    details = {
        "ema":        "↑" if ema_bull else ("↓" if ema_bear else "→"),
        "macd":       "↑" if macd_bull else ("↓" if macd_bear else "→"),
        "rsi":        f"{row.get('rsi', 0):.0f}",
        "stoch":      "↑" if stoch_bull else ("↓" if stoch_bear else "→"),
        "bb":         "↑" if bb_bull else ("↓" if bb_bear else "→"),
        "vwap":       "↑" if vwap_bull else ("↓" if vwap_bear else "→"),
        "adx":        f"{adx_val:.0f}",
        "div":        ("↑div" if rsi_div_bull else ("↓div" if rsi_div_bear else "—")),
        "vol":        "✓" if vol_ok else "✗",  # ✗ = スコア×0.5減衰
        "macd_accel": ("↑加速" if macd_accel_bull else ("↓加速" if macd_accel_bear else "—")),
    }

    return {
        "long_score":  round(long_score, 2),
        "short_score": round(short_score, 2),
        "direction":   direction,
        "details":     details,
        "atr":         row.get("atr"),
        "high_vol":    is_high_volatility(row, config),
        "ranging":     False,
    }


# ════════════════════════════════════════════════════
# 多時間軸合議制
# ════════════════════════════════════════════════════

def evaluate_consensus(
    multi_tf_data: dict[str, pd.DataFrame],
    config: Config,
    trend_1h: str = "neutral",
    fear_greed: int = 50,
    btc_trend: str = "neutral",
) -> dict:
    """
    全時間軸のシグナルを評価して「合議スコア」を計算する。

    3時間軸のうち最低2つが同一方向を示しているときだけ
    高確度シグナルと判断する。

    引数:
        multi_tf_data: {"1m": DataFrame, "5m": DataFrame, "15m": DataFrame}

    戻り値:
        {
            "signal":          str,    # "LONG" / "SHORT" / "HOLD"
            "score":           float,  # 合議スコア（0〜1）
            "consensus_count": int,    # 一致した時間軸数
            "tf_results":      dict,   # 各時間軸の評価結果
            "atr":             float,  # 最新ATR（プライマリ時間軸）
            "high_vol":        bool,   # 異常ボラティリティフラグ
        }
    """
    tf_results: dict[str, dict] = {}
    long_tfs    = 0
    short_tfs   = 0
    high_vol    = False
    primary_atr = None

    # Feature 1: プライマリ時間軸でマーケットレジームを検出する
    # レジーム（相場環境）とは「今の相場がどんな状態か」を5種類で分類したもの。
    # これを知ることで「今はトレンドに乗る戦略が向いている」などの判断ができる。
    regime = "NEUTRAL"
    primary_df = multi_tf_data.get(config.primary_tf)
    if primary_df is not None and not primary_df.empty:
        # 指標付きのデータでレジームを判定するため、指標を付与してから渡す
        try:
            from indicators import add_all_indicators as _add_ind
            df_regime = _add_ind(primary_df, config)
            regime = detect_market_regime(df_regime, config)
        except Exception:
            regime = detect_market_regime(primary_df, config)

    for tf, df in multi_tf_data.items():
        result = evaluate_single_timeframe(df, config)
        tf_results[tf] = result

        if result["direction"] == "long":
            long_tfs += 1
        elif result["direction"] == "short":
            short_tfs += 1

        if result.get("high_vol"):
            high_vol = True

        if tf == config.primary_tf:
            primary_atr = result.get("atr")

    # 合議判定
    total_tfs = len(multi_tf_data)
    signal = Signal.HOLD
    consensus_count = 0
    score = 0.0

    # ボラティリティが異常に高い場合は取引停止
    if high_vol:
        logger.info("⚠️ 異常ボラティリティを検知。今回はエントリーを見送ります。")
        return {
            "signal": Signal.HOLD,
            "score":  0.0,
            "consensus_count": 0,
            "tf_results": tf_results,
            "atr":    primary_atr,
            "high_vol": True,
            "regime": regime,  # Feature 1: レジームも返す
        }

    if long_tfs >= config.min_consensus:
        # ロングの合議成立
        signal          = Signal.LONG
        consensus_count = long_tfs
        # v35.0b: 合意した時間軸のスコアのみ平均（非合意TFの低スコアが引き下げないよう修正）
        # 旧: 全TF平均 → 1h=HOLDでlong_score≈0の場合に score が過剰に低下してシグナル消滅
        # 新: direction="long"のTFのみ平均 → 実際に合意しているTFの品質を正確に反映
        _long_voters = [r["long_score"] for r in tf_results.values() if r.get("direction") == "long"]
        avg_long = sum(_long_voters) / len(_long_voters) if _long_voters else 0
        score = (long_tfs / total_tfs) * avg_long

    elif short_tfs >= config.min_consensus:
        # ショートの合議成立
        signal          = Signal.SHORT
        consensus_count = short_tfs
        # v35.0b: 合意した時間軸のスコアのみ平均
        _short_voters = [r["short_score"] for r in tf_results.values() if r.get("direction") == "short"]
        avg_short = sum(_short_voters) / len(_short_voters) if _short_voters else 0
        score = (short_tfs / total_tfs) * avg_short

    # ── v6.1: 緊急ベア相場SHORTモード（1時間軸で合議成立）────────────────
    # 問題: 通常はmin_consensus=2（2時間軸以上の一致）が必要だが、
    #       ベア相場では5m=SHORT/15m=NEUTRAL になりやすく合議が成立しない。
    # 解決: F&G≤25(極度恐怖)+BTC下降中 = 強いベア局面では
    #       5m足だけでSHORTスコア≥0.50なら1時間軸合議を許可する。
    # 根拠: 現在のSHORT勝率60%（3/5件）= SHORTは有効な戦略。
    #       チャンスを逃し続けている機会コストが非常に大きい。
    if signal == Signal.HOLD and short_tfs >= 1:
        _primary_short = tf_results.get(config.primary_tf, {}).get("short_score", 0)
        if fear_greed <= 25 and trend_1h == "down" and _primary_short >= 0.45:
            signal = Signal.SHORT
            consensus_count = short_tfs
            _avg_short_em = sum(r["short_score"] for r in tf_results.values()) / total_tfs if total_tfs > 0 else 0
            score = _primary_short * 0.8  # 1時間軸なので80%に減衰（厳格化）
            logger.info(
                f"🐻 緊急ベア相場モード: F&G={fear_greed}(極度恐怖)+BTC下降 "
                f"→ {config.primary_tf}のみSHORTスコア{_primary_short:.2f}≥0.45で合議成立"
            )

    # ── v88.0: デッドキャットバウンスSHORT検知 ───────────────────────
    # 問題: F&G≤25+BTC上昇中 → 全シグナルがLONG → LONG禁止 → 取引ゼロ
    # 解決: 5m足でSHORTスコア≥0.50の銘柄を「デッドキャットバウンス頂上SHORT」として検出
    # 条件: F&G≤25 + BTC=up + 合議がLONGだが5mにSHORT要素あり
    # 根拠: BTC一時回復（デッドキャットバウンス）中に個別銘柄が弱い = 下落先行のサイン
    if signal == Signal.LONG and fear_greed <= 25 and btc_trend == "up":
        _primary_short_dcb = tf_results.get(config.primary_tf, {}).get("short_score", 0)
        if _primary_short_dcb >= 0.40:
            signal = Signal.SHORT
            consensus_count = 1
            score = _primary_short_dcb * 0.85  # v89.0: 60→85%に緩和（0.6だとmin_score 0.35未満で全滅）
            logger.info(
                f"🐻 v88.0 デッドキャットバウンスSHORT: F&G={fear_greed}+BTC=up "
                f"5m SHORT={_primary_short_dcb:.2f}≥0.40 → SHORT転換(score×0.6={score:.2f})"
            )

    # 最低スコア閾値フィルター（低品質シグナルを除外）
    if signal != Signal.HOLD and score < config.min_signal_score:
        logger.debug(f"スコア{score:.2f}が閾値{config.min_signal_score}未満。シグナルを見送ります。")
        signal = Signal.HOLD
        score  = 0.0

    # ── 上位足トレンドフィルター（v5.1: F&G連動 + デッドキャットバウンス対応）──
    # 設計: 1時間足トレンドとF&Gを組み合わせてエントリー許可を判断する。
    #
    # LONG: 1h が "up" のみ許可（neutral/down は禁止 = 上昇トレンド確認必須）
    # SHORT: 1h が "down" → 通常スコアで許可
    #        1h が "neutral" → F&G次第（下記参照）
    #        1h が "up" + F&G<25 → 1.3倍スコアで許可（デッドキャットバウンス対応）
    #        1h が "up" + F&G>=25 → 禁止（上昇中の逆張りSHORT = 自殺行為）
    # 変更理由: F&G=23(Extreme Fear)でもBTC 1h="up"になることがある（一時的な回復）
    # この局面ではSHORTシグナルが出ても全禁止だとチャンスを逃す。
    # 「極度恐怖 + 短期回復」=「デッドキャットバウンス」の典型で、
    # 高スコアのSHORTなら参入する価値がある。
    if signal == Signal.LONG and trend_1h != "up":
        # v28.0: BTC回復中（BTC 1h=up + F&G≤35）のbtc_recovery時は
        # コイン1h=neutralでもLONG許可（BTCの回復がaltcoinsに波及する前）
        _btc_recovery_now = (btc_trend == "up" and fear_greed <= 35)
        if _btc_recovery_now and trend_1h == "neutral":
            score *= 0.9  # コイン個別の1h確認が弱いため10%減衰
            logger.debug(
                f"v28.0 BTC回復中(btc_trend=up F&G={fear_greed}≤35): "
                f"コイン1h=neutral → LONG許可（スコア×0.9 = {score:.2f}）"
            )
        else:
            logger.debug(f"1時間足トレンド'{trend_1h}'はLONG非対応 → 見送ります（neutralも禁止）")
            signal = Signal.HOLD
            score  = 0.0
    elif signal == Signal.SHORT and trend_1h == "up":
        if fear_greed <= 25:
            # Extreme Fear + 1h上昇 = デッドキャットバウンス可能性
            # 1.3倍スコアが必要（通常より厳しいが完全禁止はしない）
            _dead_cat_min = config.min_signal_score * 1.3
            if score >= _dead_cat_min:
                logger.debug(
                    f"1時間足up + F&G={fear_greed}(Extreme Fear): "
                    f"デッドキャットバウンスSHORT許可（スコア{score:.2f} >= {_dead_cat_min:.2f}）"
                )
            else:
                logger.debug(
                    f"1時間足up + F&G={fear_greed}: SHORTスコア{score:.2f} < {_dead_cat_min:.2f} → 見送り"
                )
                signal = Signal.HOLD
                score  = 0.0
        else:
            logger.debug(f"1時間足トレンド'up'はSHORT非対応 → 見送ります（上昇中の逆張りショート禁止）")
            signal = Signal.HOLD
            score  = 0.0
    elif signal == Signal.SHORT and trend_1h == "neutral":
        # F&G < 30 (Extreme Fear / Fear): 弱気相場で多くの銘柄がneutralになる
        # この状況でSHORTを全ブロックするとチャンスを全て逃す → 通常スコアで許可
        if fear_greed < 30:
            logger.debug(
                f"1時間足neutral + F&G={fear_greed}(Extreme Fear): "
                f"ショート許可（弱気相場ではneutralでもSHORTはトレンドフォロー）"
            )
        else:
            # 中立〜強気相場: 1.5倍のスコアが必要（逆張りSHORTには慎重な審査）
            _neutral_min = config.min_signal_score * 1.5
            if score < _neutral_min:
                logger.debug(
                    f"1時間足neutral: ショートスコア{score:.2f} < "
                    f"閾値{_neutral_min:.2f}（F&G={fear_greed}: neutral時は1.5倍必要）→ 見送り"
                )
                signal = Signal.HOLD
                score  = 0.0
            else:
                logger.debug(
                    f"1時間足neutral + 高スコア({score:.2f} >= {_neutral_min:.2f}): "
                    f"ショート許可（F&G={fear_greed}）"
                )

    if signal != Signal.HOLD:
        tf_summary = " | ".join(
            f"{tf}:{r['direction']}" for tf, r in tf_results.items()
        )
        logger.info(
            f"📊 合議結果: {signal} score={score:.2f} "
            f"（{consensus_count}/{total_tfs}時間軸合意）{tf_summary}"
        )

    return {
        "signal":          signal,
        "score":           round(score, 3),
        "consensus_count": consensus_count,
        "tf_results":      tf_results,
        "atr":             primary_atr,
        "high_vol":        high_vol,
        "trend_1h":        trend_1h,
        "regime":          regime,  # Feature 1: マーケットレジーム
    }


# ════════════════════════════════════════════════════
# ポジション決済シグナル（保有中のポジションを評価）
# ════════════════════════════════════════════════════

def should_exit(
    position: dict,
    current_price: float,
    trail_peak: float,
    config: Config
) -> tuple[bool, str]:
    """
    保有中のポジションを今すぐ決済すべきかどうかを判断する。

    チェック項目:
      0. 最低保有時間（猶予期間）: エントリー直後は様子見
      1. TP（利確ライン）到達
      2. SL（損切りライン）到達 ← 最低保有時間を過ぎてから発動
      3. トレーリングストップ（利益を守る）
      4. 時間切れ（最大保有時間超過）
      5. 緊急損切り（最低保有時間内でも-3%超の急落なら即切り）

    戻り値:
        (True/False, 理由)
    """
    entry_price = position.get("entry_price", 0)
    tp_price    = position.get("tp_price", 0)
    sl_price    = position.get("sl_price", 0)
    side        = position.get("side", "long")
    entry_time  = position.get("entry_time", 0)
    import time as _time
    held_s = _time.time() - entry_time

    # 最低保有時間（猶予期間）
    min_hold_s = getattr(config, "min_hold_seconds", 90)

    if side == "long":
        # TP到達（猶予期間中でも利確はOK）
        if tp_price > 0 and current_price >= tp_price:
            return True, "tp"

        # SL到達（猶予期間を過ぎてから）
        if sl_price > 0 and current_price <= sl_price:
            if held_s >= min_hold_s:
                return True, "sl"
            # 猶予期間中でも-5%超の急落なら即切り（SLを3.5ATRに広げた分、緊急ラインも調整）
            if entry_price > 0:
                drop_pct = (entry_price - current_price) / entry_price
                if drop_pct >= 0.05:
                    return True, "sl"  # 急落は待たずにカット

        # トレーリングストップ
        # v30.0: TP1未達の場合はトレーリングを無効化（TP1を優先させる）
        # v72.0: ATR-based段階トレーリングに全面移行（固定1.5%と3.0×ATRを廃止）
        # trading_bot.py v71.0/v72.0 がSLを動的更新するため、ここは安全ネット。
        if trail_peak > 0 and position.get("tp1_done", False):  # v30.0: TP1済みのみ
            entry_atr = position.get("entry_atr", 0)
            if entry_atr > 0:
                # v91.0: 1h足最適トレーリング（0.5→0.8に拡大）
                if position.get("tp3_done", False):
                    _trail_atr = 1.5 * entry_atr
                elif position.get("tp2_done", False):
                    _trail_atr = 1.2 * entry_atr
                else:
                    _trail_atr = 0.8 * entry_atr   # v91.0: 1h足用
                trail_sl = trail_peak - _trail_atr
                if current_price <= trail_sl:
                    return True, "trailing"
            else:
                # ATR不明のフォールバック: 固定%トレーリング
                peak_pct = (trail_peak / entry_price - 1)
                if peak_pct >= config.trailing_stop_activate:
                    trail_drop = current_price / trail_peak - 1
                    if trail_drop <= -config.trailing_stop_pct:
                        return True, "trailing"
    else:
        # ショートはロングと逆
        if tp_price > 0 and current_price <= tp_price:
            return True, "tp"

        if sl_price > 0 and current_price >= sl_price:
            if held_s >= min_hold_s:
                return True, "sl"
            if entry_price > 0:
                rise_pct = (current_price - entry_price) / entry_price
                if rise_pct >= 0.05:
                    return True, "sl"

        if trail_peak > 0 and position.get("tp1_done", False):  # v30.0: TP1済みのみ
            entry_atr = position.get("entry_atr", 0)
            if entry_atr > 0:
                # v91.0: SHORT 1h足最適トレーリング
                if position.get("tp3_done", False):
                    _trail_atr = 1.5 * entry_atr
                elif position.get("tp2_done", False):
                    _trail_atr = 1.2 * entry_atr
                else:
                    _trail_atr = 0.8 * entry_atr   # v91.0: 1h足用
                trail_sl = trail_peak + _trail_atr
                if current_price >= trail_sl:
                    return True, "trailing"
            else:
                # ATR不明のフォールバック
                peak_pct = (entry_price / trail_peak - 1)
                if peak_pct >= config.trailing_stop_activate:
                    trail_drop = trail_peak / current_price - 1
                    if trail_drop <= -config.trailing_stop_pct:
                        return True, "trailing"

    # ── 停滞タイムアウト（Dead Money Exit）──────────────
    # 「利益も損失もなくただ時間だけが過ぎている」ポジションを解放する。
    # v77.0: F&G連動で恐怖圏ではタイムアウトを短縮
    # 理由: F&G≤25(極度恐怖)では市場環境が急変しやすく、3時間は長すぎる。
    #       動かないポジションは早く切ってスロットを解放すべき。
    stagnation_hours = getattr(config, "stagnation_exit_hours", 2.0)
    _entry_fg = position.get("entry_fg", 50)
    if _entry_fg <= 25:
        stagnation_hours = min(stagnation_hours, 1.5)   # 極度恐怖: 最大1.5h
    elif _entry_fg <= 40:
        stagnation_hours = min(stagnation_hours, 2.0)   # 恐怖: 最大2h
    stagnation_pct   = getattr(config, "stagnation_pct_threshold", 0.005)  # ±0.5%
    stagnation_s = stagnation_hours * 3600
    if held_s >= stagnation_s and entry_price > 0:
        price_change_pct = abs(current_price - entry_price) / entry_price
        if price_change_pct <= stagnation_pct:
            return True, "stagnation"

    # 時間切れ
    # v78.0: F&G連動で恐怖圏は最大保有時間を短縮
    # 理由: F&G≤25では市場急変リスクが高く、長時間保有は危険。
    #       4時間あれば十分にTP1/TP2を試す時間がある。
    max_hold_h = getattr(config, "max_hold_hours", 8.0)
    if _entry_fg <= 25:
        max_hold_h = min(max_hold_h, 4.0)   # 極度恐怖: 最大4h
    elif _entry_fg <= 40:
        max_hold_h = min(max_hold_h, 6.0)   # 恐怖: 最大6h
    max_hold_s = max_hold_h * 3600
    if held_s >= max_hold_s:
        return True, "timeout"

    return False, ""


def should_exit_on_signal_flip(position_side: str, current_signal: str) -> bool:
    """
    シグナルが逆転したときにポジションを閉じるか判断する。
    例: ロング保有中に売りシグナルが出たら即決済。
    """
    if position_side == "long" and current_signal == Signal.SHORT:
        return True
    if position_side == "short" and current_signal == Signal.LONG:
        return True
    return False
