"""
entry_scorer.py — v5.0 完全システム 100点満点エントリースコアリング
=================================================================
11の観点から現在の相場を点数化する。70点以上のみエントリー可。
85点以上でフルサイズ、70〜84点でハーフサイズ、69点以下は禁止。

採点項目:
  A: 上位足トレンド確認         (20点) ★0点で即スキップ
  B: キーレベルとの一致         (20点)
  C: ローソク足パターン         (15点)
  D: 出来高確認                (10点)
  E: RSIモメンタム              (10点)
  F: MACDシグナル               (10点)
  G: リスクリワード比           (10点) ★0点で即スキップ
  H: 市場環境フィルター         ( 5点) ★-1点で即スキップ
  I: 価格モメンタム確認         (10点)
  J: センチメント整合            (±10点)
  K: 出来高プロファイル          ( 8点)
  L: v5.0パターン一致ボーナス    (15点) ← NEW: A-09/A-05/B-04/A-03

合計: 最大115点（100点でキャップ）
"""

import time
import pandas as pd
import numpy as np
from typing import Optional

from utils import setup_logger

logger = setup_logger("entry_scorer")


def calc_entry_score(
    df_primary: pd.DataFrame,   # エントリー足（5m）の OHLCVデータ（指標計算済み）
    df_trend: pd.DataFrame,     # トレンド足（15m）の OHLCVデータ
    direction: str,             # "long" or "short"
    entry_price: float,
    tp_price: float,
    sl_price: float,
    atr: float,
    fear_greed: int = 50,       # 恐怖貪欲指数（0〜100）: 市場全体の方向バイアスに使用
    btc_trend: str = "neutral", # BTCトレンド（"up"/"down"/"range"）: F&Gペナルティ緩和に使用
) -> tuple[int, dict]:
    """
    8項目100点満点でエントリーの品質を評価する。

    Returns:
        (score: int, details: dict)  ← スコアと各項目の内訳
    """
    score   = 0
    details = {}

    # ── A: 上位足トレンド確認（20点）★0点で即スキップ ──────
    score_a, detail_a = _score_trend(df_trend, direction)
    # v7.2: 極度恐怖（F&G≤25）のSHORTは、15m EMAがまだ上昇整列でも最低5点を保証する。
    # 理由: BTC下降中は当然だが、BTC短期回復中（デッドキャットバウンス）でも
    #      F&G=23のような超恐怖圏ではSHORT勝率が高い。EMAは価格変化に遅れるため
    #      バウンス頂上でのSHORTではEMAがまだLONG整列のままになる。
    # 適用条件: F&G≤25 + direction=short（BTC方向に関係なく）
    if score_a == 0 and direction == "short" and fear_greed <= 25:
        score_a   = 5
        detail_a  = f"極度恐怖(F&G={fear_greed})SHORT最低保証5点（EMA未反転 = デッドキャットバウンス補正）"
        logger.debug(f"A項目SHORT最低保証: 15m EMA未反転だがF&G={fear_greed}≤25 → 5点付与（btc_trend={btc_trend}）")
    details["A_トレンド"] = {"点数": score_a, "理由": detail_a}
    if score_a == 0:
        details["判定"] = "A項目0点: 上位足トレンド不一致 → 即スキップ"
        return 0, details
    score += score_a

    # ── B: キーレベルとの一致（20点）★即スキップなし（トレンドフォロー向け）──
    # 変更理由: 以前は0点で即スキップしていたが、トレンドフォロー型エントリーでは
    # サポート・レジスタンスの「外側」（ブレイクアウト後）にいることが多い。
    # 0点 = ただのペナルティとし、他の指標が良ければエントリーを許可する。
    score_b, detail_b = _score_key_level(df_primary, entry_price, atr, direction)
    details["B_キーレベル"] = {"点数": score_b, "理由": detail_b}
    score += score_b  # 0点でもスキップしない（他の項目でカバー可能）

    # ── C: ローソク足パターン（15点）────────────────────────
    score_c, detail_c = _score_candlestick(df_primary, direction)
    details["C_ローソク"] = {"点数": score_c, "理由": detail_c}
    score += score_c

    # ── D: 出来高確認（10点）────────────────────────────────
    score_d, detail_d = _score_volume(df_primary)
    details["D_出来高"] = {"点数": score_d, "理由": detail_d}
    score += score_d

    # ── E: RSIモメンタム（10点）─────────────────────────────
    score_e, detail_e = _score_rsi(df_primary, direction)
    details["E_RSI"] = {"点数": score_e, "理由": detail_e}
    score += score_e

    # ── F: MACDシグナル（10点）──────────────────────────────
    score_f, detail_f = _score_macd(df_primary, direction)
    details["F_MACD"] = {"点数": score_f, "理由": detail_f}
    score += score_f

    # ── G: リスクリワード比（10点）★0点で即スキップ ─────────
    score_g, detail_g = _score_rr(entry_price, tp_price, sl_price)
    details["G_RR比"] = {"点数": score_g, "理由": detail_g}
    if score_g == 0:
        details["判定"] = "G項目0点: RR不足 → 即スキップ"
        return 0, details
    score += score_g

    # ── H: 市場環境フィルター（5点）─────────────────────────
    score_h, detail_h = _score_market_env(df_primary, atr)
    details["H_環境"] = {"点数": max(0, score_h), "理由": detail_h}
    if score_h < 0:
        details["判定"] = "H項目: 市場環境禁止条件 → 即スキップ"
        return 0, details
    score += score_h

    # ── K: 出来高プロファイル（8点）─────────────────────────
    # Feature 4: 出来高プロファイルキーレベル検出
    # どの価格帯で多く取引されたか（重要な価格帯）を分析する
    score_k, detail_k = _score_volume_profile(df_primary, entry_price, atr, direction)
    details["K_出来高プロファイル"] = {"点数": score_k, "理由": detail_k}
    score += score_k  # スキップなし（0点は単純に加算なし）

    # ── I: 価格モメンタム確認（10点）────────────────────────
    # 「指標が揃っているだけ」ではなく「実際に価格が動いているか」を確認する
    # 直近5本の終値が全体的にエントリー方向に動いていれば高スコア
    score_i, detail_i = _score_price_momentum(df_primary, direction)
    details["I_モメンタム"] = {"点数": score_i, "理由": detail_i}
    score += score_i

    # ── J: 市場センチメント整合ボーナス/ペナルティ（±10点）──────
    # Fear & Greed Index（F&G）と取引方向の整合性を評価する。
    # 市場の大きな流れに「逆らう」取引にはペナルティ、「乗る」取引にはボーナスを与える。
    # 例: F&G=23（超恐怖）でLONG → 市場は下落モード → ペナルティ
    #     F&G=23（超恐怖）でSHORT → 市場の流れに乗る → ボーナス
    #
    # 【v5.1改善】BTC回復中（1h=up）のLONGペナルティを半減する。
    # 理由: F&Gは市場センチメントの「遅行指標」（実際の価格より遅れる）。
    # BTCがすでに回復している場合、F&Gのペナルティは過大評価になる。
    # 実際の価格動向（BTC 1h=up）を優先することで「底値買いチャンス」を逃さない。
    score_j, detail_j = _score_sentiment_alignment(direction, fear_greed)
    # BTC回復中(1h=up) + LONGの場合: F&Gペナルティを半減（-10→-5, -7→-3, -3→0）
    if direction == "long" and btc_trend == "up" and score_j < 0:
        original_j = score_j
        score_j = score_j // 2  # 整数除算（-7//2 = -3, -10//2 = -5）
        detail_j = f"[BTC回復中緩和] {detail_j} → {original_j}→{score_j}点"
    details["J_センチメント"] = {"点数": score_j, "理由": detail_j}
    score += score_j  # マイナスあり（ペナルティ）

    # ── L: v5.0 エントリーパターン一致ボーナス（最大15点）────────
    # 32個のエントリーパターンのうち、高勝率のものを自動検出してボーナス点を与える。
    # 勝率68%以上の「優秀なパターン」が揃っているほど高スコアになる。
    #
    # 実装パターン:
    #   A-09: EMAリボン完全整列（74%勝率） → +8点
    #   A-05: ブレイクアウト後リテスト（73%勝率） → +7点
    #   B-04: フェアバリューギャップ（FVG）穴埋め（70%勝率） → +5点
    #   A-03: 出来高急増ブレイクアウト（69%勝率） → +5点
    score_l, detail_l = _score_v5_patterns(df_primary, entry_price, atr, direction)
    details["L_v5パターン"] = {"点数": score_l, "理由": detail_l}
    score += score_l  # スキップなし（ボーナスのみ）

    # ── M: ICTキルゾーン（London/NY時間帯ボーナス）（最大3点）────
    # v6.0 SCALP_5M_CONFIG: ロンドン・NY時間帯は流動性と方向性が高く
    # ブレイクアウト・リバーサルの精度が大幅に上がる。
    # プロのICTトレーダーが最も集中するこの時間帯に加点する。
    score_m, detail_m = _score_killzone(direction, fear_greed)
    details["M_キルゾーン"] = {"点数": score_m, "理由": detail_m}
    score += score_m  # スキップなし（ボーナスのみ）

    # ── N: v6.1 BTC大局トレンド整合ボーナス（+5点）＋デッドキャットバウンスボーナス（+3点）──
    # BTCトレンド方向とエントリー方向が一致 = 「大局の風向き」に乗る最強の追い風。
    # SHORT + BTC下落中: 市場全体が下落しているときにSHORT = 追い風最大 → +5点
    # LONG  + BTC上昇中: 市場全体が上昇しているときにLONG  = 追い風最大 → +5点
    # 【v7.3追加】デッドキャットバウンスSHORT特例:
    #   SHORT + BTC上昇中 + F&G≤25 = Extreme Fear中の一時反発SHORT = 部分ボーナス → +3点
    #   理由: BTC一時回復中のSHORTは「逆張り」に見えるが、実際はExtreme Fear底圏での
    #         デッドキャットバウンス頂上SHORT = F&G≤25では期待値プラスの戦略。
    #         完全な整合ボーナス(+5)は与えないが、一部ボーナス(+3)で報酬を与える。
    _btc_align_bonus = 0
    if (direction == "short" and btc_trend == "down") or (direction == "long" and btc_trend == "up"):
        _btc_align_bonus = 5
        details["N_BTC整合"] = {
            "点数": 5,
            "理由": f"BTC{btc_trend}方向と完全一致 → +5点ボーナス"
        }
    elif direction == "short" and btc_trend == "up" and fear_greed <= 25:
        # デッドキャットバウンスSHORT: BTC一時上昇 + Extreme Fear = 部分ボーナス
        _btc_align_bonus = 3
        details["N_BTC整合"] = {
            "点数": 3,
            "理由": f"デッドキャットバウンスSHORT(F&G={fear_greed}≤25+BTC短期反発) → +3点部分ボーナス"
        }
    else:
        details["N_BTC整合"] = {
            "点数": 0,
            "理由": f"BTC{btc_trend}・方向不一致またはneutral → ボーナスなし"
        }
    score += _btc_align_bonus

    # ── O: デッドキャットバウンスモメンタム補正（Extreme Fear SHORT専用）──────
    # 問題: デッドキャットバウンス時は直近5本が全て陽線 → I_モメンタムが0点になる。
    #      しかし「RSI65+以上 + F&G≤25 + BTC回復中」のSHORTこそ
    #      バウンス頂上でのSHORTであり、「上がりきった後の反転」が目的。
    # 解決: F&G≤25 + direction=short + btc_trend=up の場合、
    #      I_モメンタム0点に対して補正ボーナス+5点を付与する。
    #      これにより「価格が上がって入るSHORT」という逆張り的な見た目を補正する。
    if direction == "short" and btc_trend == "up" and fear_greed <= 25:
        _dce_bonus = 5  # Dead Cat Bounce Entry bonus
        score += _dce_bonus
        details["O_DCEモメンタム補正"] = {
            "点数": _dce_bonus,
            "理由": f"Extreme Fear(F&G={fear_greed})+BTC反発中SHORT: I_モメンタム0点補正 → +5点"
        }

    final = min(100, score)
    if final >= 80:
        verdict = f"✅ {final}点 フルサイズエントリー可"
    elif final >= 70:
        verdict = f"🟡 {final}点 ハーフサイズエントリー可"
    else:
        verdict = f"❌ {final}点 エントリー禁止（70点未満）"
    details["判定"] = verdict

    return final, details


# ════════════════════════════════════════════════════
# A: 上位足トレンド確認（20点）
# ════════════════════════════════════════════════════

def _score_trend(df: pd.DataFrame, direction: str) -> tuple[int, str]:
    """
    上位足（15m）のEMAで大きなトレンドの方向を確認する。
    EMA20 > EMA50 > EMA200 と整列していれば本物の上昇トレンド。
    逆ならトレンドなし → 0点でスキップ。
    """
    if df.empty or len(df) < 10:
        return 0, "データ不足"

    close = df["close"]
    ema20 = close.ewm(span=20, min_periods=5).mean()
    ema50 = close.ewm(span=50, min_periods=10).mean()

    e20_now  = ema20.iloc[-1]
    e50_now  = ema50.iloc[-1]
    e20_prev = ema20.iloc[-3] if len(df) >= 3 else e20_now

    # EMA200 は足数が少ない場合はスキップ
    if len(df) >= 200:
        ema200   = close.ewm(span=200, min_periods=50).mean()
        e200_now = ema200.iloc[-1]
        has_200  = True
    else:
        e200_now = None
        has_200  = False

    if direction == "long":
        if has_200 and e20_now > e50_now > e200_now:
            return 20, "EMA完全整列↑(20>50>200)"
        # クロス直後（直近3本前は下にあったのが今は上）
        prev50 = ema50.iloc[-3] if len(df) >= 3 else e50_now
        if e20_now > e50_now and e20_prev <= prev50:
            return 15, "EMAゴールデンクロス直後↑"
        if e20_now > e50_now:
            return 12, "EMA部分整列↑(20>50)"
        # EMA中立（差0.5%以内）→ レンジでのLONG = 弱い根拠
        if e50_now > 0 and abs(e20_now - e50_now) / e50_now < 0.005:
            return 5, "EMA中立（neutral）: LONGは弱い優位性"
        return 0, "下降トレンド → スキップ"

    else:  # short
        if has_200 and e20_now < e50_now < e200_now:
            return 20, "EMA完全整列↓(20<50<200)"
        prev50 = ema50.iloc[-3] if len(df) >= 3 else e50_now
        if e20_now < e50_now and e20_prev >= prev50:
            return 15, "EMAデッドクロス直後↓"
        if e20_now < e50_now:
            return 12, "EMA部分整列↓(20<50)"
        # EMA中立（差0.5%以内）→ レンジでのSHORT = 弱い根拠（弱気相場では有効）
        if e50_now > 0 and abs(e20_now - e50_now) / e50_now < 0.005:
            return 8, "EMA中立（neutral）: SHORTは弱い優位性（弱気相場対応）"
        return 0, "上昇トレンド → スキップ"


# ════════════════════════════════════════════════════
# B: キーレベルとの一致（20点）
# ════════════════════════════════════════════════════

def _score_key_level(
    df: pd.DataFrame, entry_price: float, atr: float, direction: str
) -> tuple[int, str]:
    """
    現在の価格が重要な価格帯（サポート・レジスタンス・心理的節目）に
    近いほど高スコア。離れていると0点でスキップ。

    ATR（今の相場の「1分あたりの揺れ幅」）を基準に距離を測る。
    0.3ATR以内 = 直接タッチ、0.8ATR以上 = 遠すぎてスキップ。
    """
    if atr <= 0 or df.empty:
        return 5, "ATR不明 → 保守的5点（不明な状況ではリスクを取らない）"

    levels: list[tuple[str, float]] = []

    # 直近20本の高値・安値
    if len(df) >= 20:
        recent_highs = df["high"].tail(20)
        recent_lows  = df["low"].tail(20)
        prev_high    = recent_highs.max()
        prev_low     = recent_lows.min()
        levels.append(("直近高値", prev_high))
        levels.append(("直近安値", prev_low))

        # スイング高値・安値（5本ローソク内の局所的な高値・安値）
        for i in range(2, min(18, len(df) - 2)):
            if (df["high"].iloc[-i] > df["high"].iloc[-i-1] and
                    df["high"].iloc[-i] > df["high"].iloc[-i+1]):
                levels.append((f"スイング高値", df["high"].iloc[-i]))
            if (df["low"].iloc[-i] < df["low"].iloc[-i-1] and
                    df["low"].iloc[-i] < df["low"].iloc[-i+1]):
                levels.append((f"スイング安値", df["low"].iloc[-i]))

    # 心理的節目（価格に応じて刻みを変える）
    magnitude = 10 ** (len(str(int(entry_price))) - 2)  # 例: 95000 → 1000
    magnitude = max(magnitude, 1)
    round_level = round(entry_price / magnitude) * magnitude
    levels.append(("心理的節目", round_level))

    if not levels:
        return 10, "レベルデータなし → デフォルト"

    # ロングはサポート（安値側）、ショートはレジスタンス（高値側）に近い方が良い
    if direction == "long":
        relevant = [(name, lv) for name, lv in levels if lv <= entry_price * 1.002]
    else:
        relevant = [(name, lv) for name, lv in levels if lv >= entry_price * 0.998]

    if not relevant:
        # 関連レベルがないときは全体から最近のものを使う
        relevant = levels

    min_dist_atr = min(abs(entry_price - lv) / atr for _, lv in relevant)
    closest_name = min(relevant, key=lambda x: abs(entry_price - x[1]))[0]

    # 変更: 距離閾値を拡大（0.8→1.5 ATR）
    # 理由: トレンドフォロー型では「ブレイクアウト後」にいることが多く、
    # キーレベルから少し離れていることが正常。より広い範囲でポイントを付与する。
    if min_dist_atr <= 0.5:
        return 20, f"{closest_name}に直接タッチ (距離{min_dist_atr:.2f}ATR)"
    elif min_dist_atr <= 1.5:
        return 10, f"{closest_name}に近接 (距離{min_dist_atr:.2f}ATR)"
    elif min_dist_atr <= 2.5:
        return 5, f"{closest_name}からやや離れている (距離{min_dist_atr:.2f}ATR)"
    else:
        return 0, f"キーレベルから{min_dist_atr:.2f}ATR離れている → 0点"


# ════════════════════════════════════════════════════
# C: ローソク足パターン（15点）
# ════════════════════════════════════════════════════

def _score_candlestick(df: pd.DataFrame, direction: str) -> tuple[int, str]:
    """
    直近のローソク足の形を見て、反転・継続を示すパターンを評価する。
    ピンバー（長いヒゲ）や包み足（エンゲルフィング）は強いシグナル。
    """
    if len(df) < 2:
        return 2, "データ不足 → 保守的2点"

    c1 = df.iloc[-1]   # 最新足
    c2 = df.iloc[-2]   # 1本前

    o, h, l, c = c1["open"], c1["high"], c1["low"], c1["close"]
    body  = abs(c - o)
    upper = h - max(c, o)
    lower = min(c, o) - l
    rng   = h - l

    if rng < 1e-10:
        return 3, "ほぼ動いていない足"

    body_ratio  = body / rng
    upper_ratio = upper / rng
    lower_ratio = lower / rng

    if direction == "long":
        # ピンバー（下ヒゲがボディの2倍以上で上ヒゲより長い）
        if lower >= body * 2 and lower >= upper * 1.5 and lower_ratio >= 0.4:
            return 15, f"ピンバー(下ヒゲ={lower_ratio:.0%}) ↑"
        # 強気エンゲルフィング（現在足が前足を完全に包む）
        if (c > o and c > c2["high"] and o < c2["low"]):
            return 15, "強気エンゲルフィング ↑"
        # ハンマー
        if lower >= body * 1.5 and c >= o and lower_ratio >= 0.35:
            return 10, "ハンマー足 ↑"
        # ドジ足（始値≒終値）からの反発
        if body_ratio < 0.1 and lower_ratio >= 0.3:
            return 8, "ドジ足(下ヒゲ) ↑"
        # 普通の陽線
        if c > o:
            return 6, f"陽線 (ボディ{body_ratio:.0%})"
        return 3, "パターンなし"

    else:  # short
        # ピンバー（上ヒゲがボディの2倍以上）
        if upper >= body * 2 and upper >= lower * 1.5 and upper_ratio >= 0.4:
            return 15, f"ピンバー(上ヒゲ={upper_ratio:.0%}) ↓"
        # 弱気エンゲルフィング
        if (c < o and c < c2["low"] and o > c2["high"]):
            return 15, "弱気エンゲルフィング ↓"
        # シューティングスター
        if upper >= body * 1.5 and c <= o and upper_ratio >= 0.35:
            return 10, "シューティングスター ↓"
        # ドジ足
        if body_ratio < 0.1 and upper_ratio >= 0.3:
            return 8, "ドジ足(上ヒゲ) ↓"
        # 普通の陰線
        if c < o:
            return 6, f"陰線 (ボディ{body_ratio:.0%})"
        return 3, "パターンなし"


# ════════════════════════════════════════════════════
# D: 出来高確認（10点）
# ════════════════════════════════════════════════════

def _score_volume(df: pd.DataFrame) -> tuple[int, str]:
    """
    出来高（取引量）が平均より多いかどうかを確認する。
    出来高が増えている方向への動きは信頼性が高い。
    薄商い（少量）のときはシグナルが「偽物」の可能性が高い。
    """
    if "volume" not in df.columns or len(df) < 20:
        return 2, "出来高データ不足 → 保守的2点"

    avg_vol  = df["volume"].tail(20).mean()
    last_vol = df["volume"].iloc[-1]

    if avg_vol <= 0:
        return 2, "平均出来高ゼロ"

    ratio = last_vol / avg_vol

    if ratio >= 2.0:
        return 10, f"出来高 {ratio:.1f}x 増加（大幅増）"
    elif ratio >= 1.5:
        return 8, f"出来高 {ratio:.1f}x 増加"
    elif ratio >= 1.2:
        return 6, f"出来高 {ratio:.1f}x（やや増加）"
    elif ratio >= 1.0:
        return 4, f"出来高 {ratio:.1f}x（普通）"
    elif ratio >= 0.7:
        return 2, f"出来高 {ratio:.1f}x（やや薄い）"
    else:
        return 0, f"出来高 {ratio:.1f}x（薄商い禁止）"


# ════════════════════════════════════════════════════
# E: RSIモメンタム（10点）
# ════════════════════════════════════════════════════

def _score_rsi(df: pd.DataFrame, direction: str) -> tuple[int, str]:
    """
    RSIは「買われすぎ・売られすぎ」の指標。
    ロングは「RSIが上向きで中立付近」が最高のタイミング。
    ショートは「RSIが下向きで中立付近」が最高。

    【改善点】RSIの「水準」だけでなく「向き（上昇中か下降中か）」も見る。
    例: RSI=50でも下落中なら弱い。RSI=45でも上昇中なら強い。
    """
    if "rsi" not in df.columns or df.empty or len(df) < 3:
        return 2, "RSIデータなし → 保守的2点"

    rsi = df["rsi"].iloc[-1]
    if pd.isna(rsi):
        return 2, "RSI計算中 → 保守的2点"

    rsi = float(rsi)

    # RSIが上向きか下向きか（直近3本の平均と比較）
    rsi_prev = float(df["rsi"].iloc[-3]) if not pd.isna(df["rsi"].iloc[-3]) else rsi
    rsi_rising  = rsi > rsi_prev + 0.5   # 0.5以上上昇
    rsi_falling = rsi < rsi_prev - 0.5   # 0.5以上下落

    if direction == "long":
        if rsi > 80:
            return 0, f"RSI{rsi:.0f}(買われすぎ禁止 ≥80)"
        if rsi <= 25 and rsi_rising:
            return 10, f"RSI{rsi:.0f}↑(極度売られすぎから反発 = 最強LONG機会)"
        if 25 < rsi <= 35 and rsi_rising:
            return 10, f"RSI{rsi:.0f}↑(売られすぎから急反発中 = 最理想)"
        if 35 < rsi <= 45 and rsi_rising:
            return 9, f"RSI{rsi:.0f}↑(売られすぎ圏から回復中)"
        if 45 < rsi <= 60 and rsi_rising:
            return 8, f"RSI{rsi:.0f}↑(中立から上昇中)"
        if 45 <= rsi <= 60:
            return 7, f"RSI{rsi:.0f}→(中立圏)"
        if rsi <= 25:
            return 7, f"RSI{rsi:.0f}(極度売られすぎ・方向確認待ち)"
        if 25 < rsi <= 35:
            return 6, f"RSI{rsi:.0f}(売られすぎ圏・方向待ち)"
        if 60 < rsi <= 70 and rsi_rising:
            return 5, f"RSI{rsi:.0f}↑(やや過熱気味)"
        if 35 < rsi <= 45:
            return 5, f"RSI{rsi:.0f}(やや低め・方向待ち)"
        if 60 < rsi <= 80 and rsi_falling:
            return 2, f"RSI{rsi:.0f}↓(高値圏で失速・LONG不利)"
        return 3, f"RSI{rsi:.0f}"

    else:  # short
        if rsi < 20:
            return 0, f"RSI{rsi:.0f}(極度売られすぎ禁止 ≤20)"
        if rsi >= 70 and rsi_falling:
            return 10, f"RSI{rsi:.0f}↓(極度買われすぎから反落 = 最強SHORT機会)"
        if 60 < rsi < 70 and rsi_falling:
            return 9, f"RSI{rsi:.0f}↓(買われすぎから反落中)"
        if 40 <= rsi <= 60 and rsi_falling:
            return 8, f"RSI{rsi:.0f}↓(中立から下落中)"
        if 40 <= rsi <= 60:
            return 7, f"RSI{rsi:.0f}→(中立圏)"
        if rsi >= 70:
            return 6, f"RSI{rsi:.0f}(高値圏・ピーク確認待ち)"
        if 60 < rsi < 70:
            return 5, f"RSI{rsi:.0f}(やや高め・方向待ち)"
        if 20 <= rsi < 30 and rsi_rising:
            return 2, f"RSI{rsi:.0f}↑(安値圏で反発中・SHORT不利)"
        if 30 <= rsi < 40 and rsi_falling:
            return 5, f"RSI{rsi:.0f}↓(低め・下落継続確認)"
        if 30 <= rsi < 40:
            return 4, f"RSI{rsi:.0f}(低め・方向待ち)"
        return 3, f"RSI{rsi:.0f}"


# ════════════════════════════════════════════════════
# F: MACDシグナル（10点）
# ════════════════════════════════════════════════════

def _score_macd(df: pd.DataFrame, direction: str) -> tuple[int, str]:
    """
    MACDは「勢いの加速・減速」を教えてくれる指標。
    クロス直後（信号線を超えた瞬間）が最も信頼性の高いシグナル。
    """
    if len(df) < 3:
        return 2, "データ不足 → 保守的2点"

    # 指標計算済みか確認
    if "macd" not in df.columns or "macd_signal" not in df.columns:
        # MACDを自前で計算
        close  = df["close"]
        ema12  = close.ewm(span=12, min_periods=3).mean()
        ema26  = close.ewm(span=26, min_periods=5).mean()
        macd   = ema12 - ema26
        signal = macd.ewm(span=9, min_periods=3).mean()
    else:
        macd   = df["macd"]
        signal = df["macd_signal"]

    m_now  = float(macd.iloc[-1])
    s_now  = float(signal.iloc[-1])
    m_prev = float(macd.iloc[-2])
    s_prev = float(signal.iloc[-2])

    if pd.isna(m_now) or pd.isna(s_now):
        return 5, "MACD計算中 → デフォルト"

    hist_now  = m_now  - s_now
    hist_prev = m_prev - s_prev

    if direction == "long":
        # ゴールデンクロス直後（前本では下にあったMACDが今本では上に）
        if m_now > s_now and m_prev <= s_prev:
            return 10, "MACDゴールデンクロス直後 ↑"
        # デッドクロス（逆方向）
        if m_now < s_now and m_prev >= s_prev:
            return 0, "MACDデッドクロス → ロング禁止"
        # ゼロライン上 + ヒストグラム拡大
        if m_now > 0 and m_now > s_now and hist_now > hist_prev:
            return 8, "MACDゼロ上+ヒストグラム拡大 ↑"
        if m_now > 0 and m_now > s_now:
            return 7, "MACDゼロライン上 ↑"
        return 2, "MACD中立（方向感なし）"

    else:  # short
        if m_now < s_now and m_prev >= s_prev:
            return 10, "MACDデッドクロス直後 ↓"
        if m_now > s_now and m_prev <= s_prev:
            return 0, "MACDゴールデンクロス → ショート禁止"
        if m_now < 0 and m_now < s_now and hist_now < hist_prev:
            return 8, "MACDゼロ下+ヒストグラム拡大 ↓"
        if m_now < 0 and m_now < s_now:
            return 7, "MACDゼロライン下 ↓"
        return 2, "MACD中立（方向感なし）"


# ════════════════════════════════════════════════════
# G: リスクリワード比（10点）★0点で即スキップ
# ════════════════════════════════════════════════════

def _score_rr(entry: float, tp: float, sl: float) -> tuple[int, str]:
    """
    RR比（リスクリワード比）= 「狙える利益 ÷ 最大損失」。
    v7.0: tp_atr_mult=2.5 / sl_atr_mult=1.75 → RR=1.43 が現在の標準。
    最低閾値を1.4に引き下げ（SHORTの勝率60%を前提とした収益最適化）。
    根拠: SHORT勝率60% × RR1.43 → EV = 0.6×1.43 - 0.4×1.0 = +0.458（プラス）
    2.0以上は素晴らしいが、1.4〜2.0も十分収益になる設定に変更。
    """
    if sl <= 0 or tp <= 0 or entry <= 0:
        return 0, "価格データ不正 → 禁止"

    tp_dist = abs(tp - entry)
    sl_dist = abs(sl - entry)

    if sl_dist < 1e-10:
        return 0, "SL幅がほぼゼロ → 禁止"

    rr = tp_dist / sl_dist

    if rr >= 4.0:
        return 10, f"RR {rr:.1f}:1（最高水準）"
    elif rr >= 3.0:
        return 9, f"RR {rr:.1f}:1（優秀）"
    elif rr >= 2.5:
        return 8, f"RR {rr:.1f}:1（良好）"
    elif rr >= 2.0:
        return 6, f"RR {rr:.1f}:1（標準）"
    elif rr >= 1.6:
        return 5, f"RR {rr:.1f}:1（v7.0基準: SHORT60%勝率でEV+0.46）"
    elif rr >= 1.4:
        return 4, f"RR {rr:.1f}:1（最低基準: SHORT60%で損益分岐点超え）"
    else:
        return 0, f"RR {rr:.1f}:1 < 1.4 → エントリー禁止"


# ════════════════════════════════════════════════════
# I: 価格モメンタム確認（10点）
# ════════════════════════════════════════════════════

def _score_price_momentum(df: pd.DataFrame, direction: str) -> tuple[int, str]:
    """
    直近5本のローソク足が実際にエントリー方向に動いているかを確認する。

    「EMAが上向き」だけではダメ。実際の価格が動いていることを確認する。
    例: ロングなら最近5本のうち3本以上が陽線 = 実際に上昇している証拠。

    また「加速しているか」も見る。最後の3本が連続して上昇 → 勢いがある。
    """
    if len(df) < 6:
        return 3, "データ不足"

    closes = df["close"].tail(6).values
    opens  = df["open"].tail(6).values if "open" in df.columns else closes

    if direction == "long":
        # 直近5本のうち何本が陽線か（終値 > 始値）
        bullish_bars = sum(1 for i in range(1, 6) if closes[i] > opens[i])
        # 直近3本の価格が右肩上がりか
        trending_up = closes[-1] > closes[-3] > closes[-5]
        # 最新足が明確な陽線か（ボディが範囲の30%以上）
        last_body = abs(closes[-1] - opens[-1])
        last_range = max(df["high"].iloc[-1] - df["low"].iloc[-1], 1e-10)
        strong_bar = last_body / last_range >= 0.3 and closes[-1] > opens[-1]

        if bullish_bars >= 4 and trending_up and strong_bar:
            return 10, f"強い上昇モメンタム({bullish_bars}/5陽線+右肩上がり)"
        if bullish_bars >= 3 and trending_up:
            return 8, f"上昇モメンタム({bullish_bars}/5陽線)"
        if bullish_bars >= 3:
            return 5, f"やや上昇傾向({bullish_bars}/5陽線)"
        if bullish_bars == 2:
            return 3, f"弱い({bullish_bars}/5陽線)"
        return 0, f"下落中({bullish_bars}/5陽線) → モメンタム不一致"

    else:  # short
        # 直近5本のうち何本が陰線か
        bearish_bars = sum(1 for i in range(1, 6) if closes[i] < opens[i])
        trending_down = closes[-1] < closes[-3] < closes[-5]
        last_body = abs(closes[-1] - opens[-1])
        last_range = max(df["high"].iloc[-1] - df["low"].iloc[-1], 1e-10)
        strong_bar = last_body / last_range >= 0.3 and closes[-1] < opens[-1]

        if bearish_bars >= 4 and trending_down and strong_bar:
            return 10, f"強い下落モメンタム({bearish_bars}/5陰線+右肩下がり)"
        if bearish_bars >= 3 and trending_down:
            return 8, f"下落モメンタム({bearish_bars}/5陰線)"
        if bearish_bars >= 3:
            return 5, f"やや下落傾向({bearish_bars}/5陰線)"
        if bearish_bars == 2:
            return 3, f"弱い({bearish_bars}/5陰線)"
        return 0, f"上昇中({bearish_bars}/5陰線) → モメンタム不一致"


# ════════════════════════════════════════════════════
# H: 市場環境フィルター（5点）
# ════════════════════════════════════════════════════

def _score_market_env(df: pd.DataFrame, atr: float) -> tuple[int, str]:
    """
    市場全体の状態が取引に適しているか確認する。
    急騰落の直後やスクイーズ（動きが止まった状態）はリスクが高い。
    -1点 = 「禁止」を意味する特別な値（呼び出し側が 即スキップ する）。
    """
    # 急騰落チェック（直近足で±4%以上の急変動）
    if len(df) >= 2:
        prev_c = df["close"].iloc[-2]
        last_c = df["close"].iloc[-1]
        if prev_c > 0:
            change_pct = abs(last_c / prev_c - 1)
            if change_pct >= 0.04:
                return -1, f"急騰落 {change_pct*100:.1f}% → 即禁止"

    # ボリンジャーバンドスクイーズチェック
    if all(col in df.columns for col in ["bb_upper", "bb_lower", "bb_mid"]):
        bb_u   = df["bb_upper"].iloc[-1]
        bb_l   = df["bb_lower"].iloc[-1]
        bb_m   = df["bb_mid"].iloc[-1]
        if bb_m > 0 and not pd.isna(bb_u) and not pd.isna(bb_l):
            bb_width_pct = (bb_u - bb_l) / bb_m
            if bb_width_pct < 0.002:  # バンドが非常に狭い（スクイーズ）
                return 0, f"BBスクイーズ中(幅{bb_width_pct*100:.3f}%)"

    # ── Feature 3: セッションバイアス（強化版）────────────────
    # 取引所や機関投資家が最も活発に動く時間帯を正確に分類する。
    # なぜ重要か: 「誰もが取引している時間帯」はブレイクアウトが本物になりやすい。
    # 逆に「薄商い時間帯」は小さな取引で価格が大きく動き「ダマシ」が多い。
    hour_utc = time.gmtime().tm_hour
    if hour_utc in (7, 8, 9):
        # ロンドン市場の開場直後: ヨーロッパの大口プレイヤーが動き出す
        # この時間帯は前夜のレンジをブレイクアウトする「本物の動き」が多い
        session_score = 5
        session_note  = "London Open（ブレイクアウト好機）"
    elif hour_utc in (13, 14, 15):
        # NY市場の開場直後: ロンドン×NYの双方が動いている「最高の流動性」時間帯
        # 既存のトレンドが継続・加速する傾向が強い
        session_score = 5
        session_note  = "NY Open（トレンド継続好機）"
    elif hour_utc in (10, 11, 12, 16, 17, 18, 19, 20, 21):
        # ロンドン or NY の通常営業時間帯: 流動性が高く安定した動き
        session_score = 3
        session_note  = f"流動性良好（UTC {hour_utc}時）"
    elif hour_utc in (0, 1, 2, 3, 4, 5, 6):
        # アジアセッション（東京・シンガポール・香港）: 流動性は低めだが安定している
        # 暗号通貨はアジア市場でも一定の取引量があるため禁止はしない
        session_score = 1
        session_note  = f"アジアセッション（UTC {hour_utc}時）"
    else:
        # 薄商い時間帯: スプレッドが広がりシグナルが不安定になりやすい
        session_score = 0
        session_note  = f"薄商い時間帯（UTC {hour_utc}時）"
    return session_score, session_note


# ════════════════════════════════════════════════════
# Feature 4: K: 出来高プロファイル キーレベル検出（8点）
# ════════════════════════════════════════════════════

def _score_volume_profile(
    df: pd.DataFrame, entry_price: float, atr: float, direction: str
) -> tuple[int, str]:
    """
    出来高プロファイル分析でキーレベルを検出する。

    出来高プロファイルとは:
      「どの価格帯で最も多く取引されたか」を棒グラフのように並べたもの。
      プロのトレーダーが重要視する分析手法。

    HVN（High Volume Node = 高出来高帯）:
      多くの取引が集中した価格帯 = 強いサポート/レジスタンスになりやすい。
      なぜなら多くの人が「その価格で買った/売った」記憶があるから。

    LVN（Low Volume Node = 低出来高帯）:
      取引が少ない価格帯 = 価格が素早く通過しやすい（摩擦が少ない）。
      ブレイクアウト後の価格がLVN内にいると、その先まで一気に動く傾向がある。

    戻り値:
      8点: HVN付近（重要なサポート/レジスタンスレベル確認済み）
      5点: LVN内（価格が速く動く帯域 = トレンドが加速しやすい）
      0点: 特定の重要レベルなし
    """
    if df is None or df.empty or len(df) < 20 or atr <= 0 or entry_price <= 0:
        return 0, "データ不足（出来高プロファイル計算不可）"

    if "volume" not in df.columns or "high" not in df.columns or "low" not in df.columns:
        return 0, "出来高/高値/安値データなし"

    try:
        # 直近100本のローソク足でプロファイルを作成
        df_recent = df.tail(100).copy()
        total_range_high = float(df_recent["high"].max())
        total_range_low  = float(df_recent["low"].min())
        price_range = total_range_high - total_range_low

        if price_range <= 0:
            return 0, "価格レンジがゼロ（計算不可）"

        # ── 20バケツのボリュームヒストグラムを作成 ──
        # 価格範囲を20等分し、各バケツに対応する出来高を集計する
        n_buckets = 20
        bucket_size = price_range / n_buckets
        bucket_volume = np.zeros(n_buckets)

        for _, row in df_recent.iterrows():
            candle_low  = float(row["low"])
            candle_high = float(row["high"])
            vol         = float(row["volume"]) if not pd.isna(row["volume"]) else 0.0

            if vol <= 0 or candle_high <= candle_low:
                continue

            # このローソク足の価格範囲が重なるバケツに出来高を分配する
            candle_range = candle_high - candle_low
            for b in range(n_buckets):
                bucket_low  = total_range_low + b * bucket_size
                bucket_high = bucket_low + bucket_size

                # ローソク足とバケツの重なり部分を計算
                overlap_low  = max(candle_low, bucket_low)
                overlap_high = min(candle_high, bucket_high)

                if overlap_high > overlap_low:
                    overlap_ratio = (overlap_high - overlap_low) / candle_range
                    bucket_volume[b] += vol * overlap_ratio

        # ── HVN / LVN の判定 ─────────────────────────
        # 上位30%の出来高バケツ = HVN（重要な価格帯）
        # 下位30%の出来高バケツ = LVN（価格が速く動く価格帯）
        sorted_volumes  = np.sort(bucket_volume)
        hvn_threshold   = sorted_volumes[int(n_buckets * 0.70)]  # 上位30%の閾値
        lvn_threshold   = sorted_volumes[int(n_buckets * 0.30)]  # 下位30%の閾値

        # エントリー価格がどのバケツに属するか確認
        entry_bucket = min(
            int((entry_price - total_range_low) / bucket_size),
            n_buckets - 1
        )
        entry_bucket = max(0, entry_bucket)

        # エントリー価格の周辺（±0.5ATR）のバケツも確認する
        atr_buckets = max(1, int(atr / bucket_size))  # ATR幅に相当するバケツ数
        check_range = range(
            max(0, entry_bucket - atr_buckets),
            min(n_buckets, entry_bucket + atr_buckets + 1)
        )

        is_near_hvn = any(bucket_volume[b] >= hvn_threshold for b in check_range)
        is_in_lvn   = (bucket_volume[entry_bucket] <= lvn_threshold)

        if is_near_hvn:
            # エントリー価格付近に「重要な価格帯」がある
            # ロングならサポートになる、ショートならレジスタンスになる
            return 8, "HVN付近（重要サポ/レジ確認済）"
        elif is_in_lvn:
            # エントリー価格が「薄い価格帯」にある = 価格が一気に動きやすい
            return 5, "LVN（価格が速く動く帯域）"
        else:
            return 0, "特定の重要レベルなし"

    except Exception as e:
        logger.debug(f"出来高プロファイル計算エラー（0点返却）: {e}")
        return 0, "計算エラー"


# ════════════════════════════════════════════════════
# J: 市場センチメント整合（±10点）
# ════════════════════════════════════════════════════

def _score_sentiment_alignment(direction: str, fear_greed: int) -> tuple[int, str]:
    """
    Fear & Greed 指数と取引方向の整合性を評価する。

    「市場の大きな流れ」に乗る取引はボーナス（+点）。
    「市場の大きな流れ」に逆らう取引はペナルティ（-点）。

    例:
    - F&G=10（Extreme Fear）でSHORT → +10点（流れに乗る）
    - F&G=10（Extreme Fear）でLONG  → -10点（流れに逆らう）
    - F&G=90（Extreme Greed）でLONG  → +10点（流れに乗る）
    - F&G=90（Extreme Greed）でSHORT → -10点（流れに逆らう）
    - F&G=50（中立）             → 0点（どちらでも中立）
    """
    fg = fear_greed

    if direction == "long":
        if fg <= 20:
            return -10, f"F&G={fg}(Extreme Fear): LONGは市場逆風 → -10点ペナルティ"
        elif fg <= 30:
            return -7, f"F&G={fg}(Fear): LONGはやや逆風 → -7点ペナルティ"
        elif fg <= 40:
            return -3, f"F&G={fg}(Fearish): LONGはやや不利 → -3点"
        elif fg <= 60:
            return 0, f"F&G={fg}(中立): ボーナス/ペナルティなし"
        elif fg <= 75:
            return 5, f"F&G={fg}(Greed): LONGは市場追い風 → +5点ボーナス"
        else:
            return 8, f"F&G={fg}(Extreme Greed): LONGは市場の流れ → +8点ボーナス"

    else:  # short
        if fg >= 80:
            return -10, f"F&G={fg}(Extreme Greed): SHORTは市場逆風 → -10点ペナルティ"
        elif fg >= 70:
            return -7, f"F&G={fg}(Greed): SHORTはやや逆風 → -7点ペナルティ"
        elif fg >= 60:
            return -3, f"F&G={fg}(Greedish): SHORTはやや不利 → -3点"
        elif fg >= 40:
            return 0, f"F&G={fg}(中立): ボーナス/ペナルティなし"
        elif fg >= 25:
            return 5, f"F&G={fg}(Fear): SHORTは市場追い風 → +5点ボーナス"
        else:
            return 10, f"F&G={fg}(Extreme Fear): SHORTは市場の流れ → +10点ボーナス"


# ════════════════════════════════════════════════════
# L: v5.0 エントリーパターン一致ボーナス（最大15点）
# ════════════════════════════════════════════════════

def _score_v5_patterns(
    df: pd.DataFrame, entry_price: float, atr: float, direction: str
) -> tuple[int, str]:
    """
    仮想通貨5分足スキャルピングBot v5.0 の高勝率エントリーパターンを検出する。

    実装パターン（勝率順）:
      A-09: EMAリボン完全整列    → +8点（74%勝率）
      A-05: ブレイクアウト後リテスト → +7点（73%勝率）
      B-04: FVG（フェアバリューギャップ）穴埋め → +5点（70%勝率）
      A-03: 出来高急増ブレイクアウト → +5点（69%勝率）

    最大15点（複数パターンが重なった場合はボーナスが積み重なる）
    """
    if df is None or df.empty or len(df) < 20 or atr <= 0 or entry_price <= 0:
        return 0, "データ不足"

    total_bonus = 0
    detected    = []

    try:
        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        # ── A-09: EMAリボン完全整列（+8点）─────────────────────────────
        # EMA9 > EMA21 > EMA50（ロング）または EMA9 < EMA21 < EMA50（ショート）
        # かつ価格が最上位EMA側にある。全EMАが「一方向に整列している」=最強パターンの一つ。
        # なぜ高勝率か: 短・中・長期全ての参加者が同じ方向に動いている証拠。
        ema9  = close.ewm(span=9,  min_periods=3).mean()
        ema21 = close.ewm(span=21, min_periods=5).mean()
        ema50 = close.ewm(span=50, min_periods=10).mean()

        e9  = float(ema9.iloc[-1])
        e21 = float(ema21.iloc[-1])
        e50 = float(ema50.iloc[-1])

        # EMAが「きちんと間隔を空けて」整列しているか（0.1%以上の差）
        gap_threshold = entry_price * 0.001

        if direction == "long":
            if (e9 > e21 + gap_threshold and
                    e21 > e50 + gap_threshold and
                    entry_price > e9 * 0.998):  # 価格がEMA9の上（またはわずか下）
                # EMA9が上向き（勢いがある）かも確認
                e9_prev = float(ema9.iloc[-3]) if len(df) >= 3 else e9
                if e9 > e9_prev:
                    total_bonus += 8
                    detected.append("A09:EMAリボン↑(74%)")
                else:
                    total_bonus += 4  # 整列しているが勢いが弱い
                    detected.append("A09:EMAリボン整列↑(勢い弱)")
        else:  # short
            if (e9 < e21 - gap_threshold and
                    e21 < e50 - gap_threshold and
                    entry_price < e9 * 1.002):  # 価格がEMA9の下（またはわずか上）
                e9_prev = float(ema9.iloc[-3]) if len(df) >= 3 else e9
                if e9 < e9_prev:
                    total_bonus += 8
                    detected.append("A09:EMAリボン↓(74%)")
                else:
                    total_bonus += 4
                    detected.append("A09:EMAリボン整列↓(勢い弱)")

        # ── A-05: ブレイクアウト後のリテスト（+7点）─────────────────────
        # 「重要な価格帯をブレイクして、一度戻ってきてから再度進む」パターン。
        # なぜ高勝率か: ブレイクアウト確認後の押し目 = 遅れてきた参加者も参加する。
        # 検出方法:
        #   LONG: 直近10本の最高値付近（±1ATR）にいるが、直近3本は価格が下がってきた
        #   SHORT: 直近10本の最安値付近（±1ATR）にいるが、直近3本は価格が上がってきた
        if len(df) >= 12:
            recent_high = float(high.tail(12).iloc[:-2].max())  # 最新2本を除いた直近10本の高値
            recent_low  = float(low.tail(12).iloc[:-2].min())   # 最新2本を除いた直近10本の安値
            # 直近3本の価格変化（リテストしているか）
            c_now  = float(close.iloc[-1])
            c_prev = float(close.iloc[-4]) if len(df) >= 4 else float(close.iloc[0])

            if direction == "long":
                # 価格が直近高値付近にある（±1ATR）AND 直近3本は下がってきた（押し目）
                near_high = abs(entry_price - recent_high) <= atr * 1.2
                pulled_back = c_now < c_prev * 0.9993  # -0.07%以上の押し目
                if near_high and pulled_back:
                    # さらに確認: 押し目が浅い（-2ATR以内）
                    pullback_depth = abs(c_now - recent_high) / atr
                    if pullback_depth <= 2.0:
                        total_bonus += 7
                        detected.append(f"A05:リテスト↑({pullback_depth:.1f}ATR戻り)")
            else:  # short
                near_low  = abs(entry_price - recent_low) <= atr * 1.2
                bounced   = c_now > c_prev * 1.0007   # +0.07%以上の戻り
                if near_low and bounced:
                    bounce_depth = abs(c_now - recent_low) / atr
                    if bounce_depth <= 2.0:
                        total_bonus += 7
                        detected.append(f"A05:リテスト↓({bounce_depth:.1f}ATR戻り)")

        # ── B-04: FVG（フェアバリューギャップ）穴埋め（+5点）───────────
        # FVGとは:「3本足のうち1本目の高値 < 3本目の安値」の空隙のこと。
        # 「急激な動き」で生まれた「取引が薄い価格帯」で、価格が戻ってくる傾向がある。
        # なぜ有効か: 機関投資家は「不均衡な価格帯」を解消しようとするため。
        if len(df) >= 5:
            for i in range(2, min(6, len(df))):  # 直近2〜5本前のFVGを検出
                c_minus2 = df.iloc[-(i+1)]  # 2本前
                c_minus1 = df.iloc[-i]      # 1本前（急激な動きの足）
                c_minus0 = df.iloc[-(i-1)]  # 直後の足

                h_minus2 = float(c_minus2["high"])
                l_minus2 = float(c_minus2["low"])
                h_minus0 = float(c_minus0["high"])
                l_minus0 = float(c_minus0["low"])

                if direction == "long":
                    # 上昇FVG: 2本前の高値 < 直後の安値 (= 上方向に空隙がある)
                    # 今の価格がその空隙に入ってきた = 需要ゾーンに戻ってきた
                    if h_minus2 < l_minus0:  # FVG（ギャップ）の確認
                        fvg_mid = (h_minus2 + l_minus0) / 2
                        # 現在価格がFVG内にいるか
                        if l_minus0 >= entry_price >= h_minus2 * 0.998:
                            total_bonus += 5
                            detected.append(f"B04:FVG上昇ゾーン穴埋め↑(${fvg_mid:.2f})")
                            break
                else:  # short
                    # 下降FVG: 2本前の安値 > 直後の高値 (= 下方向に空隙がある)
                    if l_minus2 > h_minus0:  # 下降FVG
                        fvg_mid = (l_minus2 + h_minus0) / 2
                        # 現在価格がFVG内
                        if h_minus0 <= entry_price <= l_minus2 * 1.002:
                            total_bonus += 5
                            detected.append(f"B04:FVG下降ゾーン穴埋め↓(${fvg_mid:.2f})")
                            break

        # ── A-03: 出来高急増ブレイクアウト（+5点）──────────────────────
        # 「価格が重要レベルを超えたとき、出来高も急増している」 = 本物のブレイクアウト。
        # 出来高が少ないブレイクアウト = 「ダマシ」の可能性が高い。
        # 出来高が2倍以上 = 大口の参加者がブレイクを確認している証拠。
        if "volume" in df.columns and len(df) >= 20:
            vol_now  = float(df["volume"].iloc[-1])
            vol_avg  = float(df["volume"].tail(20).mean())
            c_now    = float(close.iloc[-1])
            c_3ago   = float(close.iloc[-4]) if len(df) >= 4 else float(close.iloc[0])

            if vol_avg > 0:
                vol_ratio = vol_now / vol_avg
                price_move_pct = abs(c_now - c_3ago) / c_3ago if c_3ago > 0 else 0

                if vol_ratio >= 2.0 and price_move_pct >= 0.003:  # 出来高2倍+0.3%以上の動き
                    # 価格方向とシグナル方向が一致しているか
                    if (direction == "long"  and c_now > c_3ago) or \
                       (direction == "short" and c_now < c_3ago):
                        total_bonus += 5
                        detected.append(f"A03:出来高急増ブレイク({vol_ratio:.1f}x,{price_move_pct*100:.1f}%)")

        # ── 上限15点でキャップ ──────────────────────────────────────
        total_bonus = min(15, total_bonus)
        if detected:
            return total_bonus, " + ".join(detected)
        return 0, "v5.0パターン: 該当なし"

    except Exception as e:
        logger.debug(f"v5.0パターン検出エラー（0点返却）: {e}")
        return 0, "計算エラー"


# ════════════════════════════════════════════════════
# M: ICTキルゾーン（London/NY時間帯ボーナス）（最大3点）
# ════════════════════════════════════════════════════

def _score_killzone(direction: str, fear_greed: int) -> tuple[int, str]:
    """
    ICTキルゾーン（機関投資家が最も動く時間帯）ボーナスを付与する。

    v13.0 拡張版: 1日のうち最大流動性セッションを広くカバー
      - London Core:       07:00〜10:00 UTC → +3点
      - London/NY Overlap: 13:00〜17:00 UTC → +5点（最高流動性・最大ボーナス）
      - NY Session:        17:00〜21:00 UTC → +3点
      - アジア深夜:        00:00〜06:00 UTC → +0点（流動性低・banned_hours対象）
      - その他:            +1点（軽微ボーナス）

    なぜ重要か:
      London/NYオーバーラップ（UTC 13-17）は仮想通貨市場の1日の出来高の
      約40〜50%が集中する時間帯。この時間帯はトレンドの方向性が最も明確になる。
    """
    import datetime as _dt

    try:
        utc_hour   = _dt.datetime.utcnow().hour
        utc_minute = _dt.datetime.utcnow().minute

        # ── アジア深夜（流動性最低）──────────────────────────────
        if 0 <= utc_hour < 6:
            return 0, f"アジア深夜(UTC {utc_hour}:{utc_minute:02d}): +0点（流動性低）"

        # ── London Core: 07:00〜10:00 UTC ─────────────────────────
        if 7 <= utc_hour < 10:
            return 3, f"Londonコアセッション(UTC {utc_hour}:{utc_minute:02d}): +3点"

        # ── London/NY Overlap（最高流動性）: 13:00〜17:00 UTC ─────
        if 13 <= utc_hour < 17:
            return 5, f"London/NYオーバーラップ(UTC {utc_hour}:{utc_minute:02d}): +5点【最高流動性】"

        # ── NY Session: 17:00〜21:00 UTC ──────────────────────────
        if 17 <= utc_hour < 21:
            return 3, f"NYセッション(UTC {utc_hour}:{utc_minute:02d}): +3点"

        # ── その他（アジア昼間・欧州プレ）────────────────────────
        return 1, f"通常時間帯(UTC {utc_hour}:{utc_minute:02d}): +1点"

    except Exception as e:
        logger.debug(f"キルゾーンスコアエラー: {e}")
        return 0, "計算エラー"
