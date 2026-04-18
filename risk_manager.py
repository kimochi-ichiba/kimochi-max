"""
risk_manager.py — リスク管理モジュール
========================================
「絶対に大損しない」ための鉄壁のリスク管理を担当する。

主な機能:
  1. ポジションサイズ自動計算（残高・レバレッジ・SL幅から1%リスクルールで計算）
  2. 動的レバレッジ（シグナルスコアに応じて2〜5倍に調整）
  3. ドローダウン監視（-5%で全決済+24h停止）
  4. 連続損失カウンター（3回連続損失で1h停止）
  5. 1日の損失上限管理（-3%で本日取引停止）
  6. ケリー基準による最適なポジションサイズ推薦
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional

from config import Config
from utils import setup_logger, calc_kelly_fraction, safe_div

logger = setup_logger("risk_manager")


# ════════════════════════════════════════════════════
# 取引実績の記録
# ════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    """1件の取引結果を記録するデータクラス"""
    symbol: str
    side: str           # "long" または "short"
    entry_price: float
    exit_price: float
    size_usd: float     # ポジションサイズ（USD建て）
    pnl: float          # 損益（USD）
    pnl_pct: float      # 損益率（%）
    leverage: float
    won: bool
    entry_time: float
    exit_time: float
    exit_reason: str    # "tp", "sl", "trailing", "timeout", "force"
    # v23.0: エントリー時のコンテキスト（分析用）
    entry_score: float = 0.0     # エントリースコア（100点満点）
    entry_fg: int      = 0       # エントリー時のFear & Greedスコア
    entry_btc_trend: str = ""    # エントリー時のBTCトレンド（"up"/"down"/"range"）


# ════════════════════════════════════════════════════
# リスクマネージャー本体
# ════════════════════════════════════════════════════

class RiskManager:
    """
    全リスク管理を一元化したクラス。
    TradingBotクラスがこれを使って「今買っていいか？」「何口買うか？」を判断する。
    """

    def __init__(self, config: Config, initial_balance: float):
        self.config          = config
        self.balance         = initial_balance
        self.initial_balance = initial_balance
        self.peak_balance    = initial_balance

        # 連続損失・連勝カウンター（v6.0 RULE-04/09 Anti-Martingale用）
        self._consecutive_losses: int = 0
        self._consecutive_wins:   int = 0      # 連勝カウンター
        self._cooldown_until:     float = 0.0  # このUNIX時刻まで取引停止

        # 1日の損益管理
        self._day_start_balance: float = initial_balance
        self._day_start_time:    float = time.time()

        # 取引履歴（ケリー基準の計算に使う）
        self.trade_history: list[TradeRecord] = []

        # 24時間停止フラグ（最大ドローダウン到達時）
        self._halt_until: float = 0.0

        # 1日の損失5%超過カウンター（デモモード用）
        self._daily_limit_breach_count: int = 0
        self._daily_limit_breached_today: bool = False

        logger.info(
            f"リスクマネージャー初期化: 残高 ${initial_balance:,.2f} | "
            f"1トレードリスク上限 {config.max_risk_per_trade*100:.1f}% | "
            f"最大DD {config.max_drawdown*100:.1f}%"
        )

    # ── 残高管理 ────────────────────────────────────
    def update_balance(self, new_balance: float):
        """残高を更新し、ピーク残高も更新する"""
        self.balance = new_balance
        if new_balance > self.peak_balance:
            self.peak_balance = new_balance

    # ── 取引可否チェック ─────────────────────────────
    def can_trade(self) -> tuple[bool, str]:
        """
        連続損失クールダウンと全停止フラグをチェックする。
        """
        now = time.time()

        # 全停止フラグ（最大ドローダウン到達時）
        if now < self._halt_until:
            remaining_h = (self._halt_until - now) / 3600
            return False, f"全停止中（あと{remaining_h:.1f}時間）"

        # 連続損失クールダウン（無効化中 - ユーザー指示）
        # if now < self._cooldown_until:
        #     remaining_m = (self._cooldown_until - now) / 60
        #     return False, f"クールダウン中（あと{remaining_m:.0f}分）"

        return True, "ok"

    def _check_day_reset(self):
        """日付が変わったら1日の損益カウンターをリセットする"""
        now = time.time()
        if now - self._day_start_time >= 86400:  # 24時間経過
            self._day_start_balance = self.balance
            self._day_start_time    = now
            self._daily_limit_breached_today = False  # 日次リセット
            logger.info("📅 日次リセット: 1日の損益カウンターをリセットしました")

    # ── ポジションサイズ計算 ─────────────────────────
    def calc_position_size(self, entry_price: float, sl_price: float,
                            leverage: float, signal_score: float = 0.0) -> float:
        """
        「1トレードの最大リスク=残高の1%」を守るために
        最適なポジションサイズ（購入数量）を計算する。

        計算式（正しい版）:
          最大損失額 = 残高 × max_risk_per_trade（例: 1%）
          SL幅（価格差） = |entry_price - sl_price|
          数量 = 最大損失額 ÷ SL幅
          → SLに当たったとき: 数量 × SL幅 = 最大損失額  ✓

          ※ レバレッジはポジションを増やすのではなく「証拠金を少なくする」ために使う
            数量は同じで、実際に使う資金が leverage分の1 になるだけ

        v52.0 確信度連動サイジング:
          - score ≥ 85: ×1.25（高確信シグナルは少し大きく取る）
          - score ≥ 75: ×1.10
          - score < 70: ×0.90（低確信シグナルは少し小さく）

        安全上限:
          1ポジションのnotional = 残高 × 40% 以内に制限（v56.0: 旧max_positionsベース廃止）
          さらに MaxLossControlSizer(v53.0) で SL損失 ≤ 残高×1%($100) を保証

        引数:
            entry_price:  エントリー価格
            sl_price:     ストップロス価格
            leverage:     レバレッジ倍率
            signal_score: エントリースコア（0〜100点）— v52.0

        戻り値:
            購入数量（例: BTC換算の枚数）
        """
        if sl_price <= 0 or entry_price <= 0:
            return 0.0

        max_loss_usd = self.balance * self.config.max_risk_per_trade
        sl_distance  = abs(entry_price - sl_price)

        if sl_distance == 0:
            return 0.0

        # ── v7.3 RULE-04: Anti-Martingale連敗サイズ縮小（段階的・完全停止なし）────────────
        # 「負け後に同サイズ以上は絶対禁止」（RULE-05）の実践。
        # 連敗するほどポジションを小さくして「生き残る」ことを最優先する。
        # v7.3改善: 7連敗=0%（完全停止）→25%（最低25%維持）に変更。
        # ユーザー指示: 「取引が一時的に停止される仕組みを排除」に基づく修正。
        # 理由: 完全停止は「チャンスがあっても取引できない」状況を作り、
        #       相場が好転しても自動復帰できない問題があった。
        # 1連敗→90% / 2連敗→75% / 3連敗→50% / 5連敗→35% / 7連敗以上→25%（最低ライン）
        consec = self._consecutive_losses
        if consec >= 7:
            size_mult = 0.25  # 7連敗以上: 最低25%（完全停止から変更）
        elif consec >= 5:
            size_mult = 0.35  # 5〜6連敗: 35%（25%から引き上げ）
        elif consec >= 3:
            size_mult = 0.50  # 3〜4連敗: 50%
        elif consec >= 2:
            size_mult = 0.75  # 2連敗: 75%
        elif consec >= 1:
            size_mult = 0.90  # 1連敗: 90%（最初の縮小は軽微）
        else:
            size_mult = 1.0   # 0連敗: 通常サイズ
        if size_mult < 1.0:
            logger.info(
                f"⚠️ v6.1 RULE-04: 連敗{consec}回 → "
                f"ポジションサイズ×{size_mult*100:.0f}% (段階的Anti-Martingale)"
            )
        max_loss_usd = max_loss_usd * size_mult

        # ── v52.0: 確信度連動サイジング ──────────────────────────────
        # 高スコアのシグナルほど根拠が強い → サイズを少し大きく取る。
        # 低スコア（ぎりぎり合格）は小さく取って「ゴミエントリーのリスク」を抑える。
        # 上限 1.25倍（過信リスク防止）/ 下限 0.90倍
        if signal_score >= 85:
            score_mult = 1.25  # 85点以上: 高確信 → 25%増し
        elif signal_score >= 75:
            score_mult = 1.10  # 75〜84点: やや高確信 → 10%増し
        elif signal_score >= 65:
            score_mult = 1.00  # 65〜74点: 標準
        else:
            score_mult = 0.90  # 65点未満（理論上ここには来ないが保険）
        if score_mult != 1.0:
            logger.debug(
                f"v52.0 確信度サイジング: score={signal_score:.0f} → ×{score_mult*100:.0f}%"
            )
        max_loss_usd = max_loss_usd * score_mult

        # ── v6.0 RULE-09: Anti-Martingale連勝サイズ拡張 ────────────
        # 「勝ち続けているとき」は少しだけサイズを増やす（最大130%）。
        # 連勝はトレンドが良い証拠 → チャンスを少し大きく取りに行く。
        consec_wins = getattr(self, '_consecutive_wins', 0)
        if consec_wins >= 5:
            win_mult = 1.15  # v69.0: 1.30→1.15（過大ポジション防止）
            # 根拠: 1.30×1.25(score_mult)=1.625倍→最大損失$162で大損の原因だった
            # 1.15×1.25=1.4375倍→最大損失$143（11%削減）でリスク管理が改善
        elif consec_wins >= 3:
            win_mult = 1.10  # 3〜4連勝: 110%
        else:
            win_mult = 1.0   # 0〜2連勝: 通常
        if win_mult > 1.0:
            logger.debug(f"🚀 v6.0 RULE-09: 連勝{consec_wins}回 → サイズ×{win_mult*100:.0f}%")
        max_loss_usd = max_loss_usd * win_mult

        # ── 1%リスクルール ───────────────────────────────────
        # quantity × sl_distance = max_loss_usd を解く
        # 例: 残高10,000 → max_loss=100, SL幅=0.01 → 数量=10,000
        quantity = max_loss_usd / sl_distance

        # ── v56.0: ポジションサイズ上限の最適化 ─────────────────
        # 旧: max_positions(=20)ベースのソフトキャップ → 実効リスクが$7-15（本来$100の7-15%）
        # 問題: 「残高÷20×3==$1504」キャップが1%リスクルールより先に発動し、
        #       ctx_mult(0.675)×counter_trend(0.75)と合わせて実効リスクが激減していた。
        #       例: COMP LONG → 本来204枚→キャップ61枚→×0.756=15枚 → SL損失$7.5のみ
        # 修正: ポジション数ベースのソフトキャップを廃止。
        #       1%リスクルール(qty=max_loss/sl_distance)が直接サイジングを決める。
        #       MaxLossControlSizer(v53.0, $100)が安全弁として機能。
        #       結果: 実効リスク$30-65/トレードに改善（約4倍）

        # ── 絶対上限: 残高の40%を超えるポジション（notional）は禁止 ──
        # v56.0: 25%→40%に拡大（ソフトキャップ廃止に伴う上限引き上げ）
        # 残高$10,000 × 40% = $4,000がnotional上限（1ポジション）
        # 安全性: MaxLossControlSizer(v53.0)がSL損失を$100以内に保証するため安全。
        # 例: COMP LONG → 本来204枚→ハード上限101枚→×0.506=51枚 → SL損失$25（改善！）
        hard_notional_cap = self.balance * 0.40
        hard_qty_cap      = hard_notional_cap / entry_price
        if quantity > hard_qty_cap:
            logger.debug(
                f"ハードキャップ適用: notional上限 ${hard_notional_cap:.0f} "
                f"(残高{self.balance:.0f}×40%) → 数量 {quantity:.4f}→{hard_qty_cap:.4f}"
            )
            quantity = hard_qty_cap

        logger.debug(
            f"ポジションサイズ計算: entry={entry_price:.4g} sl={sl_price:.4g} "
            f"lev={leverage}x → 数量={quantity:.6f} (リスクベース={max_loss_usd/sl_distance:.2f} "
            f"ハード上限={hard_qty_cap:.2f})"
        )
        return quantity

    # ── 動的レバレッジ計算 ───────────────────────────
    def calc_leverage(self, signal_score: float,
                      fear_greed: int = 50, btc_trend: str = "neutral") -> float:
        """
        シグナルスコア（0〜1）に基づいてレバレッジを動的に計算する。

        スコアが低い（弱シグナル） → 最小レバレッジ（2倍）
        スコアが高い（強シグナル） → 最大レバレッジ（5倍）

        【v7.0改善】BEAR相場（F&G≤25 + BTC下降中）はレバレッジを最大2xにキャップ。
        根拠: v6.0プロンプト BEAR regime "leverage_mult: 0.5"
        理由: 弱気相場でのトレードは方向判断が難しく、レバレッジを抑えることで
             一回のミスによる損失を最小化する（「死なないこと」が最優先）。
        """
        score     = max(0.0, min(1.0, signal_score))
        lev_range = self.config.max_leverage - self.config.min_leverage
        raw_lev   = self.config.min_leverage + score * lev_range

        # ステップ単位に丸める（例: 1.0単位 → 2, 3, 4, 5）
        step    = self.config.leverage_step
        rounded = round(raw_lev / step) * step
        result  = max(self.config.min_leverage, min(self.config.max_leverage, rounded))

        # ── BEAR相場レバレッジキャップ（v7.0 v6.0プロンプト対応）──
        # F&G≤25 + BTC下降中 = BEAR相場 → 最大2x（leverage_mult: 0.5 適用）
        if fear_greed <= 25 and btc_trend == "down":
            result = min(result, 2.0)
        # F&G 26-35 + BTC下降中 = Fear相場 → 最大3x
        elif fear_greed <= 35 and btc_trend == "down":
            result = min(result, 3.0)

        return result

    # ── TP/SL価格計算 ─────────────────────────────
    def calc_tp_sl(self, entry_price: float, atr: float,
                    side: str) -> tuple[float, float]:
        """
        ATRを使って動的にTP/SL価格を計算する。

        TP = entry ± ATR × tp_atr_mult（デフォルト3倍）
        SL = entry ∓ ATR × sl_atr_mult（デフォルト1倍）
        → リスクリワード比 = tp_atr_mult / sl_atr_mult = 3:1

        ATRが大きいとき（荒れ相場）→ TP/SLともに広くなる
        ATRが小さいとき（穏やか）  → TP/SLともに狭くなる
        これにより相場状況に適したTP/SLが自動設定される。

        引数:
            side: "long" または "short"
        """
        # SL最低距離: v5.0 ノイズ耐性重視
        # sl_atr_mult=1.5 × 5分足ATR(0.3%)=0.45% が標準。最低0.3%確保。
        # tp_atr_mult=3.0 × 5分足ATR(0.3%)=0.9% が標準。最低0.6%確保。
        # 最低値を守ることで極小ATR銘柄でも手数料負けしない。
        min_sl_pct = 0.003   # 最低0.3%（5分足ノイズ幅 + 余裕）
        min_tp_pct = 0.006   # 最低0.6%（手数料0.08%×7.5倍 = 十分な利益幅）

        if atr <= 0 or entry_price <= 0:
            # ATR不明の場合はデフォルト値（5分足スキャルピング向け）
            sl_pct = min_sl_pct   # 0.2%
            tp_pct = sl_pct * self.config.min_rr_ratio  # 0.2% × 1.5 = 0.3%
            if side == "long":
                return entry_price * (1 + tp_pct), entry_price * (1 - sl_pct)
            else:
                return entry_price * (1 - tp_pct), entry_price * (1 + sl_pct)

        tp_dist = atr * self.config.tp_atr_mult
        sl_dist = atr * self.config.sl_atr_mult

        # 最低距離を保証（極小ATRコインでSLが狭くなりすぎるのを防ぐ）
        sl_dist = max(sl_dist, entry_price * min_sl_pct)
        tp_dist = max(tp_dist, entry_price * min_tp_pct)

        if side == "long":
            tp = entry_price + tp_dist
            sl = entry_price - sl_dist
        else:
            tp = entry_price - tp_dist
            sl = entry_price + sl_dist

        # RR比チェック
        rr = tp_dist / sl_dist if sl_dist > 0 else 0
        if rr < self.config.min_rr_ratio:
            logger.debug(f"RR比{rr:.2f}が最低ライン{self.config.min_rr_ratio}未満。TP幅を調整します。")
            tp_dist = sl_dist * self.config.min_rr_ratio
            tp = entry_price + tp_dist if side == "long" else entry_price - tp_dist

        return tp, sl

    # ── 取引結果の記録 ─────────────────────────────
    def record_trade(self, trade: TradeRecord):
        """取引結果を記録し、連続損失カウンターを更新する"""
        self.trade_history.append(trade)
        self.update_balance(self.balance + trade.pnl)

        if trade.won:
            # 勝ちトレード → 連続損失リセット・連勝カウント増加
            self._consecutive_losses = 0
            self._consecutive_wins = getattr(self, '_consecutive_wins', 0) + 1
            logger.info(
                f"✅ 利確: {trade.symbol} +${trade.pnl:.2f} ({trade.pnl_pct:+.2f}%) "
                f"[連勝{self._consecutive_wins}]"
            )
        else:
            # 負けトレード → 連続損失増加・連勝リセット
            self._consecutive_losses += 1
            self._consecutive_wins = 0
            consec = self._consecutive_losses
            # v6.0 RULE-04: Anti-Martingaleサイズ縮小の通知
            if consec >= 7:
                logger.warning(f"🚨 v6.0 RULE-04: 7連敗到達 → 次トレードのサイズ完全停止(0%)")
            elif consec >= 5:
                logger.warning(f"⚠️ v6.0 RULE-04: {consec}連敗 → 次トレードのサイズ25%に縮小")
            elif consec >= 3:
                logger.warning(f"⚠️ v6.0 RULE-04: {consec}連敗 → 次トレードのサイズ50%に縮小")
            logger.warning(f"🛑 損切り: {trade.symbol} ${trade.pnl:.2f} ({trade.pnl_pct:+.2f}%) [連敗{consec}]")

            # 連続損失クールダウン発動（無効化中 - ユーザー指示）
            # if self._consecutive_losses >= self.config.max_consecutive_losses:
            #     cooldown_s = self.config.cooldown_after_losses_h * 3600
            #     self._cooldown_until = time.time() + cooldown_s
            #     logger.warning(f"⚠️ 連続損失{self._consecutive_losses}回...")

    # ── ケリー基準でポジションサイズを推薦 ─────────
    def calc_kelly_position_pct(self) -> float:
        """
        過去の取引実績からケリー基準で最適なポジションサイズを計算する。

        ケリー基準とは: 数学的に最適な「賭け金の割合」を計算する公式。
        実際には「ハーフケリー（計算値の50%）」を使うことで安全性を高める。

        取引実績が少ない場合（30件未満）はデフォルト値（1%）を返す。
        """
        if len(self.trade_history) < 30:
            return self.config.max_risk_per_trade  # デフォルト 1%

        wins    = [t for t in self.trade_history if t.won]
        losses  = [t for t in self.trade_history if not t.won]
        win_rate = safe_div(len(wins), len(self.trade_history))
        avg_win  = safe_div(sum(t.pnl_pct for t in wins), len(wins)) if wins else 0
        avg_loss = safe_div(sum(abs(t.pnl_pct) for t in losses), len(losses)) if losses else 1

        kelly_full = calc_kelly_fraction(win_rate, avg_win, avg_loss)
        kelly_half = kelly_full * self.config.kelly_fraction

        # 安全のため1%〜5%に制限
        return max(0.005, min(0.05, kelly_half / 100))

    # ── ポートフォリオ全体リスク計算（100点システム対応）──
    def calc_total_risk_pct(self, positions: dict) -> float:
        """
        現在オープン中の全ポジションのリスク額合計 ÷ 残高 × 100 を返す。

        リスク額 = 「SLに当たったときに失う金額」
          = |エントリー価格 - SL価格| × 数量

        例: 残高10,000ドルで、合計1,000ドル分のリスクを抱えていれば10%

        総リスク上限（TOTAL_RISK_LIMIT_PCT=10%）を超えたら新規エントリーを控える。
        """
        total_risk_usd = 0.0
        for sym, pos in positions.items():
            if not hasattr(pos, "sl_price") or not hasattr(pos, "entry_price"):
                continue
            sl_dist    = abs(pos.entry_price - pos.sl_price)
            risk_usd   = sl_dist * pos.quantity
            total_risk_usd += risk_usd

        if self.balance > 0:
            return total_risk_usd / self.balance * 100
        return 0.0

    def get_portfolio_state(self, positions: dict) -> str:
        """
        ポートフォリオの状態をA〜Eの5段階で返す。

        v54.0: max_positions=20, risk=1% に合わせて閾値を更新。
        状態A【通常】  総リスク  0〜8%   → 全条件クリアでエントリー可（0〜8ポジション）
        状態B【注意】  総リスク  8〜12%  → 高スコア（85点以上）のみエントリー可
        状態C【警戒】  総リスク 12〜16%  → 新規エントリー禁止
        状態D【緊急】  総リスク 16%以上  → アラート・ポジション縮小検討
        状態E【停止】  本日損失 3%以上   → 全新規エントリー禁止
        """
        # 本日の損益を確認
        self._check_day_reset()
        day_pnl_pct = safe_div(
            self.balance - self._day_start_balance, self._day_start_balance
        ) * 100

        if day_pnl_pct <= -(self.config.daily_loss_limit * 100):
            return "E"   # 日次損失上限超過 = 完全停止（v6.0 RULE-03: 3%デフォルト）

        # v54.0: max_positions=20, risk_per_trade=1% に連動した動的閾値
        total_risk = self.calc_total_risk_pct(positions)
        _r = 100 * self.config.max_risk_per_trade  # 1.0%

        if total_risk >= _r * 16:    # 16%（16ポジション分）
            return "D"
        if total_risk >= _r * 12:    # 12%（12ポジション分）
            return "C"
        if total_risk >= _r * 8:     # 8%（8ポジション分）
            return "B"
        return "A"

    def get_emergency_level(self, positions: dict) -> int:
        """
        緊急レベルを 0〜4 で返す（高いほど危険）。

        v54.0: 閾値を max_positions × max_risk_per_trade に連動させて更新。
          max_positions=20, max_risk=1% → 最大露出=20%
          従来の7%/10%は max_positions=8 時代の設定で時代遅れ。

        Lv.0: 通常
        Lv.1: 注意（3連続損切り OR 総リスク12%超 = 12ポジション相当）
        Lv.2: 警戒（6連続損切り OR 本日損失3%超）
        Lv.3: 緊急（本日損失5%超 OR 総リスク18%超 = 18ポジション相当）
        Lv.4: 全クローズ（本日損失8%超）
        """
        self._check_day_reset()
        day_pnl_pct = safe_div(
            self.balance - self._day_start_balance, self._day_start_balance
        ) * 100
        total_risk  = self.calc_total_risk_pct(positions)
        c_losses    = self._consecutive_losses

        # v54.0: スケールに応じた閾値（max_positions=20, risk=1% 準拠）
        _risk_warn  = 100 * self.config.max_risk_per_trade * 12   # 12ポジション分 = 12%
        _risk_halt  = 100 * self.config.max_risk_per_trade * 18   # 18ポジション分 = 18%

        if day_pnl_pct <= -8.0:
            return 4
        if day_pnl_pct <= -5.0 or total_risk >= _risk_halt:
            return 3
        if c_losses >= 6 or day_pnl_pct <= -3.0:
            return 2
        if c_losses >= 3 or total_risk >= _risk_warn:
            return 1
        return 0

    # ── Feature 8: 機関投資家レベルのパフォーマンス指標 ──────────
    def calc_performance_metrics(self) -> dict:
        """
        機関投資家レベルのパフォーマンス指標を計算する。

        各指標の意味:
          Sharpe比 = 超過リターン ÷ リターンの標準偏差
            → 「リスク1単位あたりの収益」。2.0以上が優秀。
          Sortino比 = 超過リターン ÷ 下方リスクの標準偏差
            → Sharpeの改善版。「損失のリスクだけ」で測る。より公平な指標。
          Calmar比 = 年間リターン ÷ 最大ドローダウン
            → 「最大の下落幅と比べてどれだけ稼いだか」。高いほど良い。
          プロフィットファクター = 総利益 ÷ 総損失
            → 2.0以上が優秀（勝ちの合計が負けの2倍以上）。
        """
        import math

        result = {
            "sharpe":               0.0,
            "sortino":              0.0,
            "calmar":               0.0,
            "profit_factor":        0.0,
            "avg_hold_minutes":     0.0,
            "best_trade_pct":       0.0,
            "worst_trade_pct":      0.0,
            "win_streak":           0,
            "lose_streak":          0,
            "current_streak_type":  "none",
        }

        if not self.trade_history:
            return result

        trades = self.trade_history

        # ── 基本統計 ──────────────────────────────────
        pnl_pcts = [t.pnl_pct for t in trades]
        wins     = [t for t in trades if t.won]
        losses   = [t for t in trades if not t.won]

        # ── プロフィットファクター ─────────────────────
        total_profit = sum(t.pnl for t in wins) if wins else 0.0
        total_loss   = abs(sum(t.pnl for t in losses)) if losses else 0.0
        if total_loss > 0:
            result["profit_factor"] = round(total_profit / total_loss, 3)
        elif total_profit > 0:
            result["profit_factor"] = 999.0  # 損失ゼロなので無限大相当

        # ── 最良/最悪トレード ─────────────────────────
        result["best_trade_pct"]  = round(max(pnl_pcts), 2) if pnl_pcts else 0.0
        result["worst_trade_pct"] = round(min(pnl_pcts), 2) if pnl_pcts else 0.0

        # ── 平均保有時間（分）──────────────────────────
        hold_minutes = [
            (t.exit_time - t.entry_time) / 60.0
            for t in trades
            if t.exit_time > t.entry_time
        ]
        result["avg_hold_minutes"] = round(
            sum(hold_minutes) / len(hold_minutes), 1
        ) if hold_minutes else 0.0

        # ── 連勝・連敗記録 ────────────────────────────
        max_win_streak  = 0
        max_lose_streak = 0
        cur_streak      = 0
        cur_type        = "none"

        for t in trades:
            if t.won:
                if cur_type == "win":
                    cur_streak += 1
                else:
                    cur_streak = 1
                    cur_type = "win"
                max_win_streak = max(max_win_streak, cur_streak)
            else:
                if cur_type == "lose":
                    cur_streak += 1
                else:
                    cur_streak = 1
                    cur_type = "lose"
                max_lose_streak = max(max_lose_streak, cur_streak)

        result["win_streak"]          = max_win_streak
        result["lose_streak"]         = max_lose_streak
        result["current_streak_type"] = cur_type

        # ── Sharpe比とSortino比の計算 ─────────────────
        # 少なくとも5件のトレードがないと計算が不安定
        if len(pnl_pcts) < 5:
            return result

        import statistics as _stats
        mean_return = _stats.mean(pnl_pcts)
        std_return  = _stats.stdev(pnl_pcts) if len(pnl_pcts) >= 2 else 0.0

        # Sharpe比: 平均リターン ÷ 全体の標準偏差（リスクフリーレート≈0と仮定）
        if std_return > 0:
            result["sharpe"] = round(mean_return / std_return, 3)

        # Sortino比: 平均リターン ÷ 下方リスクのみの標準偏差
        # 「プラス方向のリターンのブレ」は問題ではないため、マイナスのみで計算
        downside_returns = [r for r in pnl_pcts if r < 0]
        if downside_returns and len(downside_returns) >= 2:
            downside_std = _stats.stdev(downside_returns)
            if downside_std > 0:
                result["sortino"] = round(mean_return / downside_std, 3)

        # ── Calmar比の計算 ────────────────────────────
        # 年間換算リターン ÷ 最大ドローダウン
        # 年間換算: 取引期間を日数で計算して1年（365日）に換算
        if len(trades) >= 2:
            first_trade_time = min(t.entry_time for t in trades)
            last_trade_time  = max(t.exit_time  for t in trades)
            elapsed_days = (last_trade_time - first_trade_time) / 86400.0

            if elapsed_days > 0:
                # 累積リターン（全取引のPnL%合計）
                total_return_pct = sum(pnl_pcts)
                # 年間換算リターン
                annual_return_pct = total_return_pct * (365.0 / elapsed_days)

                # 最大ドローダウン（ピーク残高からの最大下落率）
                dd_pct = safe_div(
                    self.peak_balance - self.balance, self.peak_balance
                ) * 100

                if dd_pct > 0:
                    result["calmar"] = round(annual_return_pct / dd_pct, 3)

        return result

    # ── サマリー ────────────────────────────────────
    def reset_cooldown(self):
        """連続損失クールダウンを手動で解除する（再開ボタン用）"""
        self._cooldown_until = 0.0
        self._consecutive_losses = 0
        logger.info("🔄 クールダウンを手動解除しました。取引を再開します。")

    def get_summary(self) -> dict:
        """リスク管理状態のサマリーを辞書で返す（ダッシュボード表示用）"""
        now         = time.time()
        dd          = safe_div(self.peak_balance - self.balance, self.peak_balance)
        total_pnl   = self.balance - self.initial_balance
        wins        = [t for t in self.trade_history if t.won]
        win_rate    = safe_div(len(wins), len(self.trade_history))

        self._check_day_reset()
        today_pnl = round(self.balance - self._day_start_balance, 2)

        # Feature 8: 機関投資家レベルのパフォーマンス指標を計算して追加
        perf_metrics = self.calc_performance_metrics()

        return {
            "balance":             round(self.balance, 2),
            "initial_balance":     self.initial_balance,
            "peak_balance":        round(self.peak_balance, 2),
            "total_pnl":           round(total_pnl, 2),
            "total_pnl_pct":       round(safe_div(total_pnl, self.initial_balance) * 100, 2),
            "drawdown_pct":        round(dd * 100, 2),
            "closed_trades":       len(self.trade_history),
            "won_count":           len(wins),
            "lost_count":          len(self.trade_history) - len(wins),
            "win_rate":            round(win_rate * 100, 1),
            "today_pnl":           today_pnl,
            "today_pnl_pct":       round(safe_div(today_pnl, self._day_start_balance) * 100, 2),
            "consecutive_losses":  self._consecutive_losses,
            "is_cooling_down":     now < self._cooldown_until,
            "is_halted":           now < self._halt_until,
            "cooldown_remaining_m": max(0, (self._cooldown_until - now) / 60),
            "halt_remaining_h":          max(0, (self._halt_until - now) / 3600),
            "daily_limit_breach_count":  self._daily_limit_breach_count,
            # Feature 8: Sharpe/Sortino/Calmar/プロフィットファクター等
            "sharpe":              perf_metrics["sharpe"],
            "sortino":             perf_metrics["sortino"],
            "calmar":              perf_metrics["calmar"],
            "profit_factor":       perf_metrics["profit_factor"],
            "avg_hold_minutes":    perf_metrics["avg_hold_minutes"],
            "best_trade_pct":      perf_metrics["best_trade_pct"],
            "worst_trade_pct":     perf_metrics["worst_trade_pct"],
            "win_streak":          perf_metrics["win_streak"],
            "lose_streak":         perf_metrics["lose_streak"],
            "current_streak_type": perf_metrics["current_streak_type"],
        }
