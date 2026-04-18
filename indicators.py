"""
indicators.py — テクニカル指標計算モジュール
==============================================
`ta` ライブラリを使って各種テクニカル指標を計算する（Python 3.9対応版）。
全ての関数は pandas DataFrame を受け取り、指標列を追加して返す設計。

使用指標:
  EMA  (8, 21, 200) — 短期・長期・トレンドEMA
  MACD (12, 26, 9)  — モメンタム・トレンド転換を検知
  RSI  (14)         — 過熱・底値ゾーンを判定
  Stochastics       — 短期の過熱・底値を RSIと組み合わせて判定
  Bollinger Bands   — 価格の「バンド突破」で大きな動きを検知
  ATR  (14)         — ボラティリティ計測（損切り幅・ポジションサイズに使う）
  ADX  (14)         — トレンド強度計測（横ばい相場のフィルタリングに使う）
  VWAP (20)         — 出来高加重平均価格（プロが使う割高・割安の基準値）
  RSI Divergence    — RSIダイバージェンス検出（反転シグナルの早期検知）
"""

import logging
import pandas as pd
import numpy as np

try:
    import ta
    from ta.trend import EMAIndicator, MACD, ADXIndicator
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.volatility import BollingerBands, AverageTrueRange
    _USE_TA = True
except ImportError:
    _USE_TA = False

from config import Config
from utils import setup_logger

logger = setup_logger("indicators")


def add_all_indicators(df: pd.DataFrame, config: Config) -> pd.DataFrame:
    """
    OHLCVのDataFrameに全テクニカル指標を追加して返す。
    これ1つ呼べば全ての指標が計算される。
    """
    # 修正: 以前は len(df) < config.ema_trend (200本) を要求していたため
    # ohlcv_limit=100 の場合に全ての指標が計算されないバグがあった。
    # 各指標関数は min_periods で少ないデータでも動作するので、最低10本あれば計算を試みる。
    if df.empty or len(df) < 10:
        return df

    df = df.copy()
    df = add_ema(df, config)
    df = add_macd(df, config)
    df = add_rsi(df, config)
    df = add_stochastics(df, config)
    df = add_bollinger_bands(df, config)
    df = add_atr(df, config)
    df = add_adx(df, config)
    df = add_vwap(df, config)
    return df


# ════════════════════════════════════════════════════
# 個別指標計算
# ════════════════════════════════════════════════════

def add_ema(df: pd.DataFrame, config: Config) -> pd.DataFrame:
    """EMA（指数移動平均）を計算する"""
    for period in [config.ema_short, config.ema_long, config.ema_trend]:
        if _USE_TA:
            df[f"ema_{period}"] = EMAIndicator(close=df["close"], window=period).ema_indicator()
        else:
            df[f"ema_{period}"] = df["close"].ewm(span=period, adjust=False).mean()
    return df


def add_macd(df: pd.DataFrame, config: Config) -> pd.DataFrame:
    """MACD を計算する"""
    if _USE_TA:
        macd_obj = MACD(
            close=df["close"],
            window_fast=config.macd_fast,
            window_slow=config.macd_slow,
            window_sign=config.macd_signal,
        )
        df["macd"]        = macd_obj.macd()
        df["macd_signal"] = macd_obj.macd_signal()
        df["macd_hist"]   = macd_obj.macd_diff()
    else:
        ema_fast = df["close"].ewm(span=config.macd_fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=config.macd_slow, adjust=False).mean()
        df["macd"]        = ema_fast - ema_slow
        df["macd_signal"] = df["macd"].ewm(span=config.macd_signal, adjust=False).mean()
        df["macd_hist"]   = df["macd"] - df["macd_signal"]
    return df


def add_rsi(df: pd.DataFrame, config: Config) -> pd.DataFrame:
    """RSI（相対力指数）を計算する"""
    if _USE_TA:
        df["rsi"] = RSIIndicator(close=df["close"], window=config.rsi_period).rsi()
    else:
        delta  = df["close"].diff()
        gain   = delta.clip(lower=0).rolling(config.rsi_period).mean()
        loss   = (-delta.clip(upper=0)).rolling(config.rsi_period).mean()
        rs     = gain / loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))
    return df


def add_stochastics(df: pd.DataFrame, config: Config) -> pd.DataFrame:
    """ストキャスティクスを計算する"""
    if _USE_TA:
        stoch = StochasticOscillator(
            high=df["high"], low=df["low"], close=df["close"],
            window=config.stoch_k, smooth_window=config.stoch_d,
        )
        df["stoch_k"] = stoch.stoch()
        df["stoch_d"] = stoch.stoch_signal()
    else:
        low_min  = df["low"].rolling(config.stoch_k).min()
        high_max = df["high"].rolling(config.stoch_k).max()
        denom    = (high_max - low_min).replace(0, np.nan)
        df["stoch_k"] = ((df["close"] - low_min) / denom * 100).rolling(config.stoch_d).mean()
        df["stoch_d"] = df["stoch_k"].rolling(config.stoch_d).mean()
    return df


def add_bollinger_bands(df: pd.DataFrame, config: Config) -> pd.DataFrame:
    """ボリンジャーバンドを計算する"""
    if _USE_TA:
        bb = BollingerBands(close=df["close"], window=config.bb_period, window_dev=config.bb_std)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_mid"]   = bb.bollinger_mavg()
        df["bb_lower"] = bb.bollinger_lband()
    else:
        ma = df["close"].rolling(config.bb_period).mean()
        sd = df["close"].rolling(config.bb_period).std()
        df["bb_upper"] = ma + config.bb_std * sd
        df["bb_mid"]   = ma
        df["bb_lower"] = ma - config.bb_std * sd
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"].replace(0, np.nan)
    return df


def add_atr(df: pd.DataFrame, config: Config) -> pd.DataFrame:
    """ATR（平均真の値幅）を計算する"""
    if _USE_TA:
        df["atr"] = AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"],
            window=config.atr_period
        ).average_true_range()
    else:
        high_low    = df["high"] - df["low"]
        high_close  = (df["high"] - df["close"].shift()).abs()
        low_close   = (df["low"]  - df["close"].shift()).abs()
        true_range  = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df["atr"]   = true_range.rolling(config.atr_period).mean()
    df["atr_ma"] = df["atr"].rolling(20).mean()
    return df


def add_adx(df: pd.DataFrame, config: Config) -> pd.DataFrame:
    """
    ADX（Average Directional Index / 平均方向性指数）を計算する。

    ADXとは「トレンドがどれだけ強いか」を0〜100で表す指標。
    ・ADX < 20 → 横ばい相場（トレンドなし）→ エントリーを見送る
    ・ADX > 25 → トレンドあり → エントリー候補
    ・ADX > 40 → 強いトレンド

    +DI（プラスDI）と -DI（マイナスDI）で方向も判断できる:
    ・+DI > -DI → 上昇トレンド
    ・-DI > +DI → 下降トレンド
    """
    if _USE_TA:
        adx_obj = ADXIndicator(
            high=df["high"], low=df["low"], close=df["close"],
            window=config.adx_period
        )
        df["adx"]     = adx_obj.adx()
        df["adx_pos"] = adx_obj.adx_pos()   # +DI（買い方向の強さ）
        df["adx_neg"] = adx_obj.adx_neg()   # -DI（売り方向の強さ）
    else:
        # taなし手動計算（Wilder平滑化）
        period = config.adx_period
        high, low, close = df["high"], df["low"], df["close"]

        # 方向移動量（Directional Movement）
        up_move   = high.diff()
        down_move = -low.diff()
        pos_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        neg_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        # TR計算
        hl = high - low
        hc = (high - close.shift()).abs()
        lc = (low  - close.shift()).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)

        # Wilder平滑化（指数移動平均の一種）
        def wilder_smooth(s: pd.Series, n: int) -> pd.Series:
            result = s.copy() * np.nan
            result.iloc[n-1] = s.iloc[:n].sum()
            for i in range(n, len(s)):
                result.iloc[i] = result.iloc[i-1] - result.iloc[i-1] / n + s.iloc[i]
            return result

        tr_smooth  = wilder_smooth(tr, period)
        pos_smooth = wilder_smooth(pd.Series(pos_dm, index=df.index), period)
        neg_smooth = wilder_smooth(pd.Series(neg_dm, index=df.index), period)

        df["adx_pos"] = (pos_smooth / tr_smooth.replace(0, np.nan)) * 100
        df["adx_neg"] = (neg_smooth / tr_smooth.replace(0, np.nan)) * 100
        dx = ((df["adx_pos"] - df["adx_neg"]).abs() /
              (df["adx_pos"] + df["adx_neg"]).replace(0, np.nan)) * 100
        df["adx"] = dx.rolling(period).mean()

    return df


def add_vwap(df: pd.DataFrame, config: Config) -> pd.DataFrame:
    """
    VWAP（Volume Weighted Average Price / 出来高加重平均価格）を計算する。

    VWAPとは「出来高（売買量）が多かった価格帯の加重平均」のこと。
    機関投資家（プロ）が取引の基準価格として使うため、
    価格がVWAPを上回っているか下回っているかで強弱を判断できる。

    ・価格 > VWAP → 市場参加者の平均より高い → 強気
    ・価格 < VWAP → 市場参加者の平均より低い → 弱気

    ローリングVWAP（期間=BB期間と同じ20本）を使う。
    """
    # 中央値価格 = (高値 + 安値 + 終値) / 3 （HLC3 とも呼ぶ）
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    period = config.bb_period  # 20本ローリング

    tp_vol = typical_price * df["volume"]
    df["vwap"] = tp_vol.rolling(period).sum() / df["volume"].rolling(period).sum().replace(0, np.nan)
    return df


# ════════════════════════════════════════════════════
# シグナル抽出ヘルパー
# ════════════════════════════════════════════════════

def get_latest_row(df: pd.DataFrame) -> pd.Series:
    """DataFrameの最新の行を返す"""
    if df.empty:
        return pd.Series(dtype=float)
    return df.iloc[-1]


def is_ema_bullish(row: pd.Series, config: Config) -> bool:
    """EMAが強気（上昇トレンド）かを判定する"""
    ema_s = row.get(f"ema_{config.ema_short}")
    ema_l = row.get(f"ema_{config.ema_long}")
    ema_t = row.get(f"ema_{config.ema_trend}")
    close = row.get("close")
    if any(pd.isna([ema_s, ema_l, ema_t, close])):
        return False
    return ema_s > ema_l and close > ema_t


def is_ema_bearish(row: pd.Series, config: Config) -> bool:
    """EMAが弱気（下降トレンド）かを判定する"""
    ema_s = row.get(f"ema_{config.ema_short}")
    ema_l = row.get(f"ema_{config.ema_long}")
    ema_t = row.get(f"ema_{config.ema_trend}")
    close = row.get("close")
    if any(pd.isna([ema_s, ema_l, ema_t, close])):
        return False
    return ema_s < ema_l and close < ema_t


def is_macd_bullish(row: pd.Series) -> bool:
    """MACDが強気かを判定する"""
    macd = row.get("macd")
    sig  = row.get("macd_signal")
    hist = row.get("macd_hist")
    if any(pd.isna([macd, sig, hist])):
        return False
    return macd > sig and hist > 0


def is_macd_bearish(row: pd.Series) -> bool:
    """MACDが弱気かを判定する"""
    macd = row.get("macd")
    sig  = row.get("macd_signal")
    hist = row.get("macd_hist")
    if any(pd.isna([macd, sig, hist])):
        return False
    return macd < sig and hist < 0


def is_rsi_bullish(row: pd.Series, config: Config) -> bool:
    """RSIが強気（上昇モメンタム）かを判定する — RSI > 50 でモメンタムが上向き"""
    rsi = row.get("rsi")
    if pd.isna(rsi):
        return False
    return 50 < rsi < 75  # 中央より上・過熱しすぎていない


def is_rsi_bearish(row: pd.Series, config: Config) -> bool:
    """RSIが弱気（下降モメンタム）かを判定する — RSI < 50 でモメンタムが下向き"""
    rsi = row.get("rsi")
    if pd.isna(rsi):
        return False
    return 25 < rsi < 50  # 中央より下・売られすぎていない


def is_adx_trending(row: pd.Series, config: Config) -> bool:
    """
    ADXがトレンドありの状態かを判定する。
    ADX < adx_threshold（デフォルト20）は横ばい相場 → Falseを返す。
    横ばい相場ではトレンドフォロー戦略が機能しないため取引しない。
    """
    adx = row.get("adx")
    if adx is None or pd.isna(adx):
        return True  # ADXが計算できない場合はフィルタしない（安全側）
    return float(adx) >= config.adx_threshold


def detect_rsi_divergence(df: pd.DataFrame, lookback: int = 14) -> tuple:
    """
    最新ローソク足のRSIダイバージェンス（乖離）を検出する。

    ダイバージェンスとは「価格とRSIが逆の動きをすること」。
    これは相場の転換（反転）を事前に教えてくれる早期警告シグナル。

    ・強気ダイバージェンス（Bull Div）:
      価格は安値を更新したのに、RSIは安値を更新しない
      → 売り圧力が弱まっている → 上昇転換の可能性

    ・弱気ダイバージェンス（Bear Div）:
      価格は高値を更新したのに、RSIは高値を更新しない
      → 買い圧力が弱まっている → 下落転換の可能性

    戻り値: (bull_div: bool, bear_div: bool)
    """
    if "rsi" not in df.columns or len(df) < lookback + 2:
        return False, False

    recent = df.tail(lookback + 1)
    if recent["rsi"].isna().any():
        return False, False

    window = recent.iloc[:-1]    # 直近lookback本（比較対象）
    last   = recent.iloc[-1]     # 最新足（現在）

    # ── 強気ダイバージェンス ──────────────────────────
    # 条件: 現在の安値 ≤ 窓内最安値 × 1.005（ほぼ同水準か下）
    # かつ: 現在のRSI > 窓内最安値時のRSI + 3
    # かつ: 現在のRSI < 45（売られすぎゾーン付近のみ有効）
    try:
        price_min_idx  = window["low"].idxmin()
        rsi_at_low     = window.loc[price_min_idx, "rsi"]
        bull_div = bool(
            float(last["low"])  <= float(window["low"].min()) * 1.005
            and float(last["rsi"]) > float(rsi_at_low) + 3
            and float(last["rsi"]) < 45
        )
    except Exception:
        bull_div = False

    # ── 弱気ダイバージェンス ──────────────────────────
    # 条件: 現在の高値 ≥ 窓内最高値 × 0.995（ほぼ同水準か上）
    # かつ: 現在のRSI < 窓内最高値時のRSI - 3
    # かつ: 現在のRSI > 55（買われすぎゾーン付近のみ有効）
    try:
        price_max_idx  = window["high"].idxmax()
        rsi_at_high    = window.loc[price_max_idx, "rsi"]
        bear_div = bool(
            float(last["high"]) >= float(window["high"].max()) * 0.995
            and float(last["rsi"]) < float(rsi_at_high) - 3
            and float(last["rsi"]) > 55
        )
    except Exception:
        bear_div = False

    return bull_div, bear_div


def is_vwap_bullish(row: pd.Series) -> bool:
    """価格がVWAPを上回っているか（強気）を判定する"""
    close = row.get("close")
    vwap  = row.get("vwap")
    if close is None or vwap is None or pd.isna(close) or pd.isna(vwap) or vwap == 0:
        return False
    return float(close) > float(vwap)


def is_vwap_bearish(row: pd.Series) -> bool:
    """価格がVWAPを下回っているか（弱気）を判定する"""
    close = row.get("close")
    vwap  = row.get("vwap")
    if close is None or vwap is None or pd.isna(close) or pd.isna(vwap) or vwap == 0:
        return False
    return float(close) < float(vwap)


def is_high_volatility(row: pd.Series, config: Config) -> bool:
    """ATRが通常の何倍も高い異常ボラティリティ状態かを判定する"""
    atr    = row.get("atr")
    atr_ma = row.get("atr_ma")
    if pd.isna(atr) or pd.isna(atr_ma) or atr_ma == 0:
        return False
    return (atr / atr_ma) >= config.atr_volatility_threshold


def is_ranging_market(row: pd.Series, config: Config) -> bool:
    """
    ボリンジャーバンドの幅が狭い = 横ばい相場（レンジ）かを判定する。
    レンジ相場でトレンド追従の売買をすると損しやすいので取引しない。
    """
    bb_width = row.get("bb_width")
    if bb_width is None or pd.isna(bb_width):
        return False
    return float(bb_width) < config.bb_range_threshold


def is_volume_confirmed(row: pd.Series, df: pd.DataFrame, config: Config) -> bool:
    """
    直近ローソク足の出来高が平均より多いかを確認する。
    出来高が多い = 本物の動き。少ない = 誰も動いていない偽の動き。
    """
    volume = row.get("volume")
    if volume is None or pd.isna(volume):
        return True  # 出来高データなければスキップ（チェックしない）
    avg_vol = df["volume"].tail(20).mean()
    if avg_vol == 0:
        return True
    return float(volume) >= avg_vol * config.volume_surge_factor


def get_1h_trend(df_1h: "pd.DataFrame", config: Config) -> str:
    """
    1時間足のEMAを使って大きなトレンドの方向を判定する。
    戻り値: "up"（上昇）/ "down"（下降）/ "neutral"（中立）

    EMA21 vs EMA50 を使うことで短期ノイズを除去し、
    「本物のトレンド」がある場合のみ up/down を返す。
    EMA8 vs EMA21 では些細な動きでも常に「up」になってしまうため変更。

    up/down の条件を両方とも満たさない = neutral → LONG/SHORT どちらも許可
    """
    _EMA_FAST = 21   # 中期線（旧: ema_short=8 → 21に変更）
    _EMA_SLOW = 50   # 長期線（旧: ema_long=21  → 50に変更）

    if df_1h is None or df_1h.empty or len(df_1h) < _EMA_SLOW:
        return "neutral"
    df = df_1h.copy()
    df["_ema_fast"] = df["close"].ewm(span=_EMA_FAST, adjust=False).mean()
    df["_ema_slow"] = df["close"].ewm(span=_EMA_SLOW, adjust=False).mean()
    row   = df.iloc[-1]
    ema_f = row["_ema_fast"]
    ema_s = row["_ema_slow"]
    close = row["close"]
    if any(pd.isna([ema_f, ema_s, close])):
        return "neutral"

    # 条件を厳しくする: EMAのクロスに加え、価格も同方向にある場合のみ確定
    if ema_f > ema_s * 1.001 and close > ema_s:   # 0.1%以上上回っている = 明確な上昇
        return "up"
    if ema_f < ema_s * 0.999 and close < ema_s:   # 0.1%以上下回っている = 明確な下降
        return "down"
    return "neutral"  # それ以外 = どちらとも言えない → LONG/SHORT どちらも許可
