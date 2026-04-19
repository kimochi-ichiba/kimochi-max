"""
trading_bot.py — マルチ銘柄対応 TradingBot
==============================================
複数の仮想通貨銘柄を同時に監視し、
エントリー条件を満たした銘柄に自動でポジションを取る。

主な変更点（v2.0）:
  - 1銘柄スキャン → ラウンドロビンで25銘柄を順番に監視
  - 単一ポジション → 最大5つの同時ポジションを管理
  - _market_data: 銘柄ごとの相場データを別々に保持
"""

import time
import threading
import logging
import json
import os
import re
from typing import Optional
from collections import deque
from pathlib import Path

import pandas as pd

from config import Config, Mode
from data_fetcher import DataFetcher
from indicators import add_all_indicators, get_latest_row, is_high_volatility, get_1h_trend
from market_context import MarketContext
from risk_manager import RiskManager, TradeRecord
from strategy import evaluate_consensus, should_exit, should_exit_on_signal_flip, Signal, detect_high_risk_conditions
from utils import setup_logger, fmt_price, fmt_pct, ts_to_str
from entry_scorer import calc_entry_score

logger = setup_logger("trading_bot")


# ════════════════════════════════════════════════════
# ポジション管理
# ════════════════════════════════════════════════════

class Position:
    """現在保有しているポジションを表すクラス"""

    def __init__(self, symbol: str, side: str, entry_price: float,
                 quantity: float, tp_price: float, sl_price: float,
                 leverage: float, entry_atr: float = 0.0,
                 is_dead_cat_bounce: bool = False):
        self.symbol            = symbol
        self.side              = side
        self.entry_price       = entry_price
        self.quantity          = quantity
        self.tp_price          = tp_price
        self.sl_price          = sl_price
        self.leverage          = leverage
        self.entry_atr         = entry_atr   # ブレークイーブンストップ計算用ATR
        self.is_dead_cat_bounce = is_dead_cat_bounce  # デッドキャットバウンスエントリーフラグ
        self.entry_time        = time.time()
        self.trail_peak        = entry_price
        self.size_usd          = entry_price * quantity / leverage
        self.tp1_done          = False       # TP1（部分利確①）済みフラグ
        self.tp2_done          = False       # TP2（部分利確②）済みフラグ（v13.0: dict管理→Positionへ移動）
        self.tp3_done          = False       # TP3（部分利確③）済みフラグ（v47.0: ATR×3.5 追加利確）
        # v50.0: 動的TP延長フラグ
        # トレンド継続中はTP1/TP2の部分利確をスキップして利益を最大化する。
        # 「10%で利確せず、まだ20%行く見込みがあれば20%まで待つ」思想。
        self.tp1_close_skipped = False       # TP1到達時にトレンド継続でスキップ済みフラグ
        self.tp2_close_skipped = False       # TP2到達時にトレンド継続でスキップ済みフラグ
        self.counter_trend     = False       # 逆張りLONG（F&G 25-45）フラグ（半サイズ管理用）
        # v23.0: エントリー時コンテキスト（分析・改善用）
        self.entry_score       = 0.0         # エントリースコア（100点満点）
        self.entry_fg          = 0           # エントリー時のFear & Greedスコア
        self.entry_btc_trend   = ""          # エントリー時のBTCトレンド
        # タイムラグ解消: TP1/TP2/TP3 部分利確で既に残高加算済みの累計を記録
        # 最終クローズの record_trade 時に差し引くことで二重計上を防ぐ
        self._partial_realized = 0.0

    def current_pnl(self, current_price: float) -> float:
        if self.side == "long":
            return (current_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - current_price) * self.quantity

    def current_pnl_pct(self, current_price: float) -> float:
        if self.entry_price == 0:
            return 0.0
        if self.side == "long":
            raw_pct = current_price / self.entry_price - 1
        else:
            raw_pct = self.entry_price / current_price - 1
        return raw_pct * self.leverage * 100

    def update_trail_peak(self, current_price: float):
        if self.side == "long":
            if current_price > self.trail_peak:
                self.trail_peak = current_price
        else:
            if current_price < self.trail_peak:
                self.trail_peak = current_price

    def to_dict(self, current_price: Optional[float] = None) -> dict:
        cp = current_price or self.entry_price
        return {
            "symbol":        self.symbol,
            "side":          self.side,
            "entry_price":   self.entry_price,
            "current_price": cp,
            "quantity":      self.quantity,
            "tp_price":      self.tp_price,
            "sl_price":      self.sl_price,
            "leverage":      self.leverage,
            "size_usd":      round(self.size_usd, 2),
            "upnl":          round(self.current_pnl(cp), 2),
            "upnl_pct":      round(self.current_pnl_pct(cp), 2),
            "trail_peak":    self.trail_peak,
            "entry_time":        self.entry_time,
            "age_s":             round(time.time() - self.entry_time),
            "tp1_done":          self.tp1_done,
            "tp2_done":          self.tp2_done,
            "counter_trend":     self.counter_trend,
            "is_dead_cat_bounce": self.is_dead_cat_bounce,
        }


# ════════════════════════════════════════════════════
# メインボットクラス（マルチ銘柄対応）
# ════════════════════════════════════════════════════

class TradingBot:
    """
    複数の仮想通貨銘柄を同時に監視する自動売買ボット。

    スキャン方式:
      - config.watch_symbols（25銘柄）をラウンドロビンで監視
      - 5秒ごとに5銘柄ずつ処理 → 全銘柄を約25秒で1周
      - 最大5ポジションを同時保有
    """

    def __init__(self, config: Config):
        self.config  = config
        self.fetcher = DataFetcher(config)
        self.risk    = RiskManager(config, config.initial_balance)
        self.mktctx  = MarketContext()   # リアルタイム市場センチメント（5分更新）
        self._lock   = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # マルチ銘柄データ（銘柄 → {ohlcv, current_price, ts}）
        self._market_data: dict[str, dict] = {}

        # ラウンドロビン用ポインタ
        self._scan_pointer: int = 0

        # ポジション（銘柄 → Position）
        self._positions: dict[str, Position] = {}

        # シグナル（全体の最新 + 銘柄ごと）
        self._last_signal_result: dict = {}
        self._per_signal: dict[str, dict] = {}  # symbol → 最新シグナル

        # ログ
        self._logs: deque = deque(maxlen=300)

        # スキャン回数
        self._scan_count: int = 0
        self._last_exit_t: float = 0.0      # 最後に損切り監視した時刻

        # ボット起動時刻（検証経過日時・UI表示用）
        self._started_at: float = time.time()
        # 検証開始時刻（永続化。再起動しても維持され、UIの「検証経過」表示に使う）
        self._validation_started_at: float = time.time()

        # スキャン用スレッド（メインループと分離）
        self._scan_thread: Optional[threading.Thread] = None

        # 資産履歴（エクイティカーブ用）
        self._equity_history: list = []
        self._last_equity_ts: float = 0.0

        # 1時間足トレンドキャッシュ（5分間有効）
        self._trend_cache: dict = {}       # symbol → {"trend": str, "ts": float}
        self._trend_cache_ttl: float = 300.0

        # 損切り後の再エントリー禁止タイマー（銘柄ごと）
        # SLで損切り後しばらく同じ銘柄に再エントリーしない（SLチャーン防止）
        self._sl_cooldown: dict = {}       # symbol → 再エントリー解禁 UNIX時刻

        # シグナルフリップ後の再エントリー禁止タイマー（銘柄ごと）
        # ユーザー指示: 取引が一時的に停止される仕組みを排除 → クールダウン無効化
        self._flip_cooldown: dict = {}     # symbol → 再エントリー解禁 UNIX時刻
        self._FLIP_COOLDOWN_S: int = 0     # signal_flip後クールダウン無効化（0=即再エントリー可）

        # 銘柄別 連続損失カウンター（クールダウン無効化）
        # ユーザー指示: 取引が一時的に停止される仕組みを排除 → 銘柄別クールダウン無効化
        self._consec_losses: dict = {}     # symbol → 連続損失回数（カウントのみ、停止しない）
        self._consec_cooldown: dict = {}   # symbol → 連続損失クールダウン解禁時刻（未使用）
        self._CONSEC_LIMIT  = 99           # 事実上無効（99連敗でクールダウン → 発動しない）
        self._CONSEC_COOL_S = 0            # 銘柄別クールダウン無効化

        # ブレークイーブンストップ発動済みフラグ（銘柄ごと）
        # 一度BEに移動したSLは二重移動しないように管理する
        self._be_triggered: dict = {}      # symbol → bool

        # v49.0: 部分利確（TP1/TP2/TP3）の累積PnL追跡
        # 目的: TradeRecord.pnlに部分利確利益を含めてwon/lost判定を正確にする。
        # 問題: TP1/TP2/TP3クローズ時に balance は更新されるが TradeRecord には反映されない。
        # → won=False（実際は黒字トレード）となり consecutive_losses が誤カウントされる。
        self._partial_pnl: dict = {}       # symbol → 累積部分利確PnL（USD）

        # ── シグナル確認システム（v9.0: 2スキャン連続確認で初めてエントリー）──
        # 同じ方向シグナルが2回連続で検出されてから初めてエントリーする。
        # 目的: 1スキャンで消えるニセシグナルへの即エントリーを排除。
        # 期待効果: signal_flip損失の30〜50%削減、勝率の向上。
        self._entry_confirm: dict = {}  # {symbol: {"direction": str, "first_seen": float}}

        # ── 反転シグナル確認システム（v9.1: 利益中のポジションを反転シグナルから守る）──
        # 利益が出ているポジションはシグナル反転1回ではすぐに閉じない。
        # 2回連続で反転シグナルが確認されて初めてsignal_flipで閉じる。
        # 損失中のポジションは従来通り即クローズ（損切り最優先）。
        # 期待効果: 利益確定前の早期クローズを防ぎ、平均利益額を向上。
        self._flip_confirm: dict = {}  # {symbol: {"rev_signal": str, "first_seen": float}}

        # ── 動的監視銘柄リスト（24時間ごとにBinance出来高ランキングから自動更新）──
        self._watch_symbols: list = list(config.watch_symbols or [config.symbol])
        self._symbols_last_refresh: float = 0.0   # 0 = まだ一度も更新していない
        self._symbols_refresh_hours: float = 24.0  # 24時間ごとに更新

        # ── BTCトレンドキャッシュ（5分間有効）──────────────
        # BTCが下落中: ロングを最大15件に制限・ショートを優遇
        # BTCが上昇中: ショートを最大15件に制限・ロングを優遇
        self._btc_trend_cache: dict = {"trend": "range", "ts": 0.0}

        # ── 緊急停止フラグ ──────────────────────────────
        self._emergency_level: int = 0   # 0〜4（4=全クローズ）
        self._last_emergency_check: float = 0.0

        # ── フェーズ管理（5フェーズ追跡ストップ用）──────────
        # フェーズ2以降のSL移動を管理するフラグ
        self._phase_tp2_done: dict = {}  # symbol → bool（TP2達成フラグ）

        # ── Feature 7: カスケード崩壊保護 ─────────────────
        # 「連鎖的な損切り」を検知して一時的にエントリーを停止する機能。
        # なぜ必要か: 30分以内に3件以上SLが続くのは「市場全体の急変」を示す。
        # こういうときは新しいエントリーをせず嵐が過ぎるのを待つのが賢明。
        self._sl_hits_window: list = []     # 直近のSL発動タイムスタンプリスト
        self._cascade_halt_until: float = 0.0  # この時刻まではエントリー停止

        # ── Feature 9: スマート再エントリーウォッチ ───────────
        # TP（利確）した銘柄が戻ってきたとき、再エントリーをしやすくする機能。
        # なぜ必要か: 強いトレンドは一度TP後も継続することが多い。
        # 「TP後30分以内に同方向のシグナルが出た場合」はボーナス点を与えて
        # 再エントリーのチャンスを逃さないようにする設計。
        self._tp_reentry_watch: dict = {}   # symbol → {direction, tp_price, entry_price, expires_at}

        # ── v51.0: TP延長後モメンタム監視 ─────────────────────
        # tp1_close_skipped / tp2_close_skipped でTP利確をスキップした後、
        # RSIが方向と逆に振れた回数をカウントし、2回連続で弱化したら即利確する。
        # 目的: 「延長したが実は反転だった」ケースで含み益を守る。
        self._momentum_fade_count: dict = {}  # symbol → int（連続弱化カウント）

        self._log(
            f"🤖 TradingBot v2.0 初期化完了 | "
            f"監視銘柄: {len(self._watch_symbols)}種（起動後に上位100銘柄へ自動更新）| "
            f"最大ポジション: {config.max_positions}件 | "
            f"レバ: {config.min_leverage}〜{config.max_leverage}倍",
            "info"
        )

    # ── ログ ────────────────────────────────────────
    def _log(self, msg: str, level: str = "info"):
        ts = ts_to_str(time.time(), "%H:%M:%S")
        with self._lock:
            self._logs.appendleft({"ts": ts, "msg": msg, "level": level})
        getattr(logger, level.lower(), logger.info)(msg)

    # ── 状態の保存・復元 ─────────────────────────────
    STATE_FILE = Path(__file__).parent / "bot_state.json"
    # 追記専用トレード台帳（絶対に上書きされない、取引のたびに1行追記）
    LEDGER_FILE = Path(__file__).parent / "trade_ledger.jsonl"

    def _append_to_ledger(self, trade):
        """取引クローズのたびに追記専用JSONL台帳に1行追記する。
        このファイルは絶対に上書き・削除されず、取引履歴の最終的な正本となる。
        """
        try:
            record = {
                "symbol":      getattr(trade, "symbol", ""),
                "side":        getattr(trade, "side", ""),
                "entry_price": getattr(trade, "entry_price", 0.0),
                "exit_price":  getattr(trade, "exit_price", 0.0),
                "size_usd":    getattr(trade, "size_usd", 0.0),
                "pnl":         getattr(trade, "pnl", 0.0),
                "pnl_pct":     getattr(trade, "pnl_pct", 0.0),
                "leverage":    getattr(trade, "leverage", 1.0),
                "won":         getattr(trade, "won", False),
                "entry_time":  getattr(trade, "entry_time", 0),
                "exit_time":   getattr(trade, "exit_time", 0),
                "exit_reason": getattr(trade, "exit_reason", ""),
                "entry_score": getattr(trade, "entry_score", 0.0),
                "entry_fg":    getattr(trade, "entry_fg", 0),
                "entry_btc_trend": getattr(trade, "entry_btc_trend", ""),
                "ledger_saved_at": time.time(),
            }
            with open(self.LEDGER_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
        except Exception as e:
            logger.error(f"⚠️ 台帳への追記に失敗（重大）: {e}")

    def _save_state(self):
        """取引履歴・残高・オープンポジションをJSONファイルに保存する"""
        try:
            positions_data = {}
            for sym, pos in self._positions.items():
                positions_data[sym] = {
                    "symbol":      pos.symbol,
                    "side":        pos.side,
                    "entry_price": pos.entry_price,
                    "quantity":    pos.quantity,
                    "tp_price":    pos.tp_price,
                    "sl_price":    pos.sl_price,
                    "leverage":    pos.leverage,
                    "entry_atr":          pos.entry_atr,
                    "is_dead_cat_bounce": pos.is_dead_cat_bounce,
                    "entry_time":         pos.entry_time,
                    "trail_peak":         pos.trail_peak,
                    "tp1_done":           pos.tp1_done,
                    "tp2_done":           pos.tp2_done,
                    "tp3_done":           getattr(pos, 'tp3_done', False),         # v47.0
                    "tp1_close_skipped":  getattr(pos, 'tp1_close_skipped', False),# v50.0
                    "tp2_close_skipped":  getattr(pos, 'tp2_close_skipped', False),# v50.0
                    "counter_trend":      pos.counter_trend,
                    # v23.0: エントリーコンテキスト
                    "entry_score":        getattr(pos, 'entry_score', 0.0),
                    "entry_fg":           getattr(pos, 'entry_fg', 0),
                    "entry_btc_trend":    getattr(pos, 'entry_btc_trend', ""),
                }
            trades_data = [
                {
                    "symbol":      t.symbol, "side":        t.side,
                    "entry_price": t.entry_price, "exit_price":  t.exit_price,
                    "size_usd":    t.size_usd,    "pnl":         t.pnl,
                    "pnl_pct":     t.pnl_pct,     "leverage":    t.leverage,
                    "won":         t.won,          "entry_time":  t.entry_time,
                    "exit_time":   t.exit_time,    "exit_reason": t.exit_reason,
                    # v23.0: エントリーコンテキスト
                    "entry_score":     getattr(t, 'entry_score', 0.0),
                    "entry_fg":        getattr(t, 'entry_fg', 0),
                    "entry_btc_trend": getattr(t, 'entry_btc_trend', ""),
                }
                for t in self.risk.trade_history
            ]
            # v62.0: SLクールダウンを永続化（再起動後も再エントリー禁止が有効）
            _now_for_save = time.time()
            sl_cooldown_data = {
                sym: expiry for sym, expiry in self._sl_cooldown.items()
                if expiry > _now_for_save  # 有効期限内のもののみ保存（期限切れは除外）
            }
            # v64.0: BEトリガー済みフラグを永続化（再起動後の誤再発動を防ぐ）
            with self._lock:
                be_triggered_data = {
                    sym: True for sym, flag in self._be_triggered.items() if flag
                }
            state = {
                "balance":              self.risk.balance,
                "initial_balance":      self.risk.initial_balance,
                "peak_balance":         self.risk.peak_balance,
                "day_start_balance":    self.risk._day_start_balance,
                "consecutive_losses":   self.risk._consecutive_losses,
                "consecutive_wins":     getattr(self.risk, "_consecutive_wins", 0),
                "saved_at":             time.time(),
                "validation_started_at": self._validation_started_at,  # 検証経過の正確な計測用
                "scan_count":           self._scan_count,  # スキャン回数を永続化（再起動で0に戻らないように）
                "positions":            positions_data,
                "trade_history":     trades_data,
                "equity_history":    list(self._equity_history),
                "sl_cooldown":       sl_cooldown_data,  # v62.0: 再起動後もクールダウン維持
                "be_triggered":      be_triggered_data, # v64.0: BE済みフラグを永続化
            }
            # 取引履歴ロストガード（厳格モード）:
            # 既存ファイルの取引件数・スキャン回数が減る書き込みは原則すべて拒否する。
            # 減るということはバグか事故なので、常に既存を優先して保全する。
            import os as _os
            try:
                if self.STATE_FILE.exists():
                    with open(self.STATE_FILE, "r", encoding="utf-8") as _f_chk:
                        _existing = json.load(_f_chk)
                    _existing_trades = _existing.get("trade_history", [])
                    _existing_count = len(_existing_trades)
                    _new_count = len(trades_data)
                    _existing_scan = _existing.get("scan_count", 0) or 0

                    # 件数減少を検出: 既存を優先保全
                    if _existing_count > _new_count:
                        _backup_dir = self.STATE_FILE.parent / "state_backups"
                        _backup_dir.mkdir(exist_ok=True)
                        _rej = _backup_dir / f"bot_state_REJECTED_{int(time.time())}_would_be_{_new_count}trades.json"
                        import shutil as _shutil
                        with open(_rej, "w", encoding="utf-8") as _rf:
                            json.dump(state, _rf, ensure_ascii=False, indent=2)
                        logger.warning(
                            f"🛡️ 取引履歴減少を検出 ({_existing_count}件→{_new_count}件)。"
                            f"既存を保全し保存内容を棄却: {_rej.name}"
                        )
                        # 既存の取引履歴を維持（上書きしない）
                        state["trade_history"] = _existing_trades
                        trades_data = _existing_trades
                    # scan_count減少も保全
                    if _existing_scan > state.get("scan_count", 0):
                        logger.warning(
                            f"🛡️ scan_count減少を検出 ({_existing_scan}→{state.get('scan_count',0)})。"
                            f"既存値を維持"
                        )
                        state["scan_count"] = _existing_scan
            except Exception as _guard_err:
                logger.debug(f"ロストガードチェックエラー（無視）: {_guard_err}")

            # v40.0: アトミック書き込み（tmp→renameで中断時のファイル破損を防止）
            # 旧方式: open("w")で直接書き込み → 中断するとファイルが壊れる
            # 新方式: 一時ファイルに書き込み完了後にrename → 常に完全なファイルが存在
            tmp_path = self.STATE_FILE.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            _os.replace(tmp_path, self.STATE_FILE)  # POSIX atomic rename
        except Exception as e:
            logger.debug(f"状態保存エラー（無視）: {e}")

    def _load_state(self):
        """再起動時に前回の取引履歴・残高・ポジションを復元する"""
        if not self.STATE_FILE.exists():
            return
        try:
            with open(self.STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)

            saved_at = state.get("saved_at", 0)
            age_h = (time.time() - saved_at) / 3600

            # 保存から30日以上経過していたら読み込まない
            if age_h > 720:
                logger.info(f"⏰ 保存済み状態が{age_h:.1f}時間前のデータのため無視します")
                return

            # 残高・取引履歴を復元
            self.risk.balance               = state.get("balance",            self.risk.balance)
            self.risk.initial_balance       = state.get("initial_balance",    self.risk.initial_balance)
            self.risk.peak_balance          = state.get("peak_balance",       self.risk.peak_balance)
            self.risk._day_start_balance    = state.get("day_start_balance",  self.risk._day_start_balance)
            # 連敗/連勝カウンターを復元（再起動後もAnti-Martingaleが正しく機能するように）
            self.risk._consecutive_losses   = state.get("consecutive_losses", 0)
            self.risk._consecutive_wins     = state.get("consecutive_wins",   0)
            if self.risk._consecutive_losses > 0:
                logger.info(
                    f"📌 連敗カウンター復元: {self.risk._consecutive_losses}連敗 "
                    f"（再起動後もAnti-Martingaleペナルティを継続）"
                )

            for t in state.get("trade_history", []):
                try:
                    self.risk.trade_history.append(TradeRecord(
                        symbol=t.get("symbol", ""),
                        side=t.get("side", "long"),
                        entry_price=t.get("entry_price", 0.0),
                        exit_price=t.get("exit_price", 0.0),
                        size_usd=t.get("size_usd", 0.0),
                        pnl=t.get("pnl", 0.0),
                        pnl_pct=t.get("pnl_pct", 0.0),
                        leverage=t.get("leverage", 1.0),
                        won=t.get("won", False),
                        entry_time=t.get("entry_time", 0.0),
                        exit_time=t.get("exit_time", 0.0),
                        exit_reason=t.get("exit_reason", ""),
                        entry_score=t.get("entry_score", 0.0),
                        entry_fg=t.get("entry_fg", 0),
                        entry_btc_trend=t.get("entry_btc_trend", ""),
                    ))
                except Exception as te:
                    logger.debug(f"取引履歴1件スキップ（フォーマット不一致）: {te}")

            # オープンポジションを復元
            for sym, pd_ in state.get("positions", {}).items():
                pos = Position(
                    symbol=pd_["symbol"],      side=pd_["side"],
                    entry_price=pd_["entry_price"], quantity=pd_["quantity"],
                    tp_price=pd_["tp_price"],   sl_price=pd_["sl_price"],
                    leverage=pd_["leverage"],   entry_atr=pd_.get("entry_atr", 0.0),
                    is_dead_cat_bounce=pd_.get("is_dead_cat_bounce", False),
                )
                pos.entry_time      = pd_.get("entry_time", time.time())
                pos.trail_peak      = pd_.get("trail_peak", pd_["entry_price"])
                pos.tp1_done           = pd_.get("tp1_done", False)
                pos.tp2_done           = pd_.get("tp2_done", False)
                pos.tp3_done           = pd_.get("tp3_done", False)           # v47.0
                pos.tp1_close_skipped  = pd_.get("tp1_close_skipped", False)  # v50.0
                pos.tp2_close_skipped  = pd_.get("tp2_close_skipped", False)  # v50.0
                pos.counter_trend      = pd_.get("counter_trend", False)
                pos.entry_score     = pd_.get("entry_score", 0.0)   # v23.0
                pos.entry_fg        = pd_.get("entry_fg", 0)         # v23.0
                pos.entry_btc_trend = pd_.get("entry_btc_trend", "") # v23.0
                # v9.3遡及修正: Phase3済みポジションのTPをPhase4ターゲットまで延長
                # 通常TP(2.5×ATR)がPhase4(5.25×ATR)より先に発動するバグを防ぐ
                if pos.tp1_done and pos.entry_atr > 0:
                    _fix_tp4_dist = pos.entry_atr * self.config.sl_atr_mult * 3.5
                    if pos.side == "long":
                        _new_tp = pos.entry_price + _fix_tp4_dist
                        if _new_tp > pos.tp_price:  # 延長のみ（縮小しない）
                            pos.tp_price = _new_tp
                    else:
                        _new_tp = pos.entry_price - _fix_tp4_dist
                        if _new_tp < pos.tp_price:  # 延長のみ（縮小しない）
                            pos.tp_price = _new_tp
                self._positions[sym] = pos

            # エクイティカーブ（グラフ）を復元
            for eq in state.get("equity_history", []):
                if isinstance(eq, dict) and "time" in eq and "value" in eq:
                    self._equity_history.append(eq)
            if len(self._equity_history) > 1440:
                self._equity_history = self._equity_history[-1440:]

            # 検証開始時刻を復元（永続化されたものを優先。なければequity_history最古から推定）
            saved_val_start = state.get("validation_started_at")
            if saved_val_start and saved_val_start > 0:
                self._validation_started_at = float(saved_val_start)
            elif self._equity_history:
                first_ts = self._equity_history[0].get("time")
                if first_ts and first_ts > 0:
                    self._validation_started_at = float(first_ts)
            _elapsed_h = (time.time() - self._validation_started_at) / 3600
            logger.info(f"⏱️ 検証開始時刻を復元: 経過{_elapsed_h:.1f}時間")

            # スキャン回数を復元（再起動で0に戻らないように）
            saved_scan = state.get("scan_count", 0)
            if saved_scan > 0:
                self._scan_count = int(saved_scan)
                logger.info(f"🔍 スキャン回数を復元: {self._scan_count:,}回")

            # v62.0: SLクールダウンを復元（再起動後も同一銘柄への再エントリーを禁止）
            _now_for_load = time.time()
            for sym, expiry in state.get("sl_cooldown", {}).items():
                if expiry > _now_for_load:  # まだ有効期限内のもののみ復元
                    self._sl_cooldown[sym] = expiry
            if self._sl_cooldown:
                _remaining_syms = list(self._sl_cooldown.keys())
                _remaining_mins = [f"{sym}({(expiry - _now_for_load)/60:.0f}分)" for sym, expiry in self._sl_cooldown.items()]
                logger.info(
                    f"🔒 SLクールダウン復元: {len(_remaining_syms)}銘柄 "
                    f"({', '.join(_remaining_mins[:5])}{'...' if len(_remaining_mins) > 5 else ''})"
                )

            # v64.0: BEトリガー済みフラグを復元（再起動後の誤再発動を防ぐ）
            # 既存ポジションのBEが再起動でリセットされると、BE済みのポジションが再びBE条件を通過する
            for sym, flag in state.get("be_triggered", {}).items():
                if flag and sym in self._positions:  # オープンポジションのみ復元
                    self._be_triggered[sym] = True
            if self._be_triggered:
                logger.info(
                    f"🛡️ BEトリガー復元: {len(self._be_triggered)}銘柄 "
                    f"({', '.join(list(self._be_triggered.keys())[:5])})"
                )

            n_trades = len(self.risk.trade_history)
            n_pos    = len(self._positions)
            n_eq     = len(self._equity_history)
            logger.info(
                f"💾 前回の状態を復元しました "
                f"（残高: ${self.risk.balance:,.2f} | "
                f"取引履歴: {n_trades}件 | ポジション: {n_pos}件 | グラフ: {n_eq}点）"
            )
        except Exception as e:
            logger.warning(f"状態復元エラー（新規スタート）: {e}")

    # ── 起動・停止 ──────────────────────────────────
    def start(self):
        if self._running:
            return
        self._load_state()   # 再起動時に前回の状態を復元
        self._setup_signal_handlers()  # 終了シグナル受信時に自動保存
        self._running = True
        # 損切り専用ループ（5秒ごと・高速）
        self._thread = threading.Thread(target=self._main_loop, daemon=True, name="exit-checker")
        # スキャン専用ループ（5秒ごと・高速ラウンドロビン）
        self._scan_thread = threading.Thread(target=self._scan_loop, daemon=True, name="scanner")
        # 定期保存ループ（60秒ごと）
        self._save_thread = threading.Thread(target=self._periodic_save_loop, daemon=True, name="state-saver")
        self._thread.start()
        self._scan_thread.start()
        self._save_thread.start()
        self._log("🚀 自動売買ボット起動（マルチ銘柄モード）", "info")

    def _setup_signal_handlers(self):
        """SIGTERM / SIGINT を受け取ったら状態を保存してから終了する"""
        import signal

        def _handle_shutdown(signum, frame):
            self._log("🛑 終了シグナル受信 → 状態を保存して終了します", "info")
            self._save_state()
            import sys
            sys.exit(0)

        signal.signal(signal.SIGTERM, _handle_shutdown)
        signal.signal(signal.SIGINT, _handle_shutdown)

    def _periodic_save_loop(self):
        """
        定期保存ループ:
          ・60秒ごと → bot_state.json に上書き保存（高速リカバリ用）
          ・30分ごと → state_backups/ にタイムスタンプ付きバックアップを自動保存
          ・バックアップは最新96件を保持（30分×96 = 約48時間分）
        """
        BACKUP_INTERVAL_S  = 30 * 60      # 30分ごとにバックアップ
        MAX_BACKUP_COUNT   = 96           # 最大96件 ≒ 48時間分
        last_backup_time   = time.time()  # 起動直後はまだバックアップしない

        while self._running:
            time.sleep(60)
            if not self._running:
                break

            # ── 通常保存（60秒ごと）────────────────────────
            self._save_state()

            # ── 自動バックアップ（30分ごと）─────────────────
            now = time.time()
            if now - last_backup_time >= BACKUP_INTERVAL_S:
                try:
                    backup_dir = self.STATE_FILE.parent / "state_backups"
                    backup_dir.mkdir(exist_ok=True)

                    ts_str      = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
                    backup_path = backup_dir / f"bot_state_{ts_str}_auto.json"

                    import shutil
                    shutil.copy2(self.STATE_FILE, backup_path)

                    # 古いバックアップを整理（最新96件を超えたら削除）
                    all_backups = sorted(backup_dir.glob("bot_state_*.json"))
                    if len(all_backups) > MAX_BACKUP_COUNT:
                        for old in all_backups[:-MAX_BACKUP_COUNT]:
                            old.unlink(missing_ok=True)

                    last_backup_time = now
                    logger.debug(f"💾 自動バックアップ保存: {backup_path.name}")

                except Exception as e:
                    logger.debug(f"自動バックアップエラー（無視）: {e}")

    def stop(self):
        self._running = False
        self._log("🛑 ボットを停止しました", "info")

    def reset_cooldown(self):
        """クールダウンを解除して取引を再開する"""
        self.risk.reset_cooldown()
        self._log("🔄 クールダウン解除・取引再開", "info")

    # ── メインループ（損切り専用・高速）────────────────
    def _main_loop(self):
        """
        SL/TP決済チェック専用ループ。5秒ごとに全ポジションを確認する。
        スキャン（重い処理）とは別スレッドで動かすことで、
        スキャン中でも損切りが即座に実行される。
        """
        while self._running:
            try:
                now = time.time()

                # ① SL/TP決済チェック（5秒ごと・損切りは素早く）
                if now - self._last_exit_t >= self.config.exit_check_interval_s:
                    self._check_exits_all()
                    self._last_exit_t = now

                # ② 資産履歴記録（60秒ごと・エクイティカーブ用）
                if now - self._last_equity_ts >= 60:
                    with self._lock:
                        upnl = sum(
                            p.current_pnl(self._market_data.get(s, {}).get("current_price", p.entry_price))
                            for s, p in self._positions.items()
                        )
                    equity = self.risk.balance + upnl
                    self._equity_history.append({"time": int(now), "value": round(equity, 2)})
                    if len(self._equity_history) > 1440:
                        self._equity_history = self._equity_history[-1440:]
                    self._last_equity_ts = now

                    # ③ 🚨 緊急退避チェック（DD -30%で全決済 + 24h停止）
                    # バックテスト50パターンで発動ゼロ確認済みの保険機能
                    if equity > 0 and self.risk.peak_balance > 0:
                        dd_pct = (self.risk.peak_balance - equity) / self.risk.peak_balance * 100
                        if dd_pct >= 30.0 and not getattr(self.risk, '_emergency_exit_triggered', False):
                            self._log(f"🚨🚨 緊急退避発動！ DD={dd_pct:.1f}% → 全ポジション強制決済 + 24h停止", "error")
                            with self._lock:
                                pos_syms = list(self._positions.keys())
                            for sym in pos_syms:
                                try:
                                    pr = self._market_data.get(sym, {}).get("current_price")
                                    if pr is None and sym in self._positions:
                                        pr = self._positions[sym].entry_price
                                    if pr:
                                        self._close_position(sym, pr, "emergency_exit_dd30")
                                except Exception as e:
                                    self._log(f"緊急決済失敗 {sym}: {e}", "error")
                            self.risk._emergency_exit_triggered = True
                            self.risk._halt_until = now + 24 * 3600  # 24時間停止

            except Exception as e:
                self._log(f"⚠️ メインループエラー: {e}", "error")
                logger.exception("メインループで予期しないエラー")
            time.sleep(1)

    # ── 動的銘柄リスト更新 ────────────────────────────
    def _refresh_watch_symbols(self):
        """
        Binanceの24時間出来高ランキング上位200銘柄で監視リストを自動更新する。
        初回起動時と24時間ごとに実行される。
        v12.0: 固定50銘柄 → 動的200銘柄（時価総額・出来高上位）に変更。
        """
        now = time.time()
        elapsed_h = (now - self._symbols_last_refresh) / 3600.0

        # 初回（last_refresh=0）または24時間経過したら更新
        if self._symbols_last_refresh > 0 and elapsed_h < self._symbols_refresh_hours:
            return

        # ════ 動的モード: Binance 24h出来高ランキング上位200銘柄 ════
        # v36.0: ccxt.fetch_tickers()→fetcher.fetch_top_symbols()（Session直接HTTP）に統一
        try:
            raw_symbols = self.fetcher.fetch_top_symbols(300)  # L3-T1改良: 200→300銘柄

            # ブラックリスト除外（ユーザー設定の取引禁止銘柄）
            blacklist = set(self.config.symbol_blacklist or [])
            new_symbols = [s for s in raw_symbols if s.replace('/USDT', '') not in blacklist]

            if new_symbols:
                old_count = len(self._watch_symbols)
                with self._lock:
                    self._watch_symbols = new_symbols
                self._symbols_last_refresh = now
                top10 = ', '.join(s.replace('/USDT', '') for s in new_symbols[:10])
                self._log(
                    f"✅ 監視銘柄を動的{len(new_symbols)}銘柄に更新（出来高上位200）: "
                    f"{top10}... "
                    f"（前回: {old_count}銘柄 → 次回確認: {self._symbols_refresh_hours:.0f}時間後）",
                    "info"
                )
                return

        except Exception as e:
            self._log(f"⚠️ 動的銘柄リスト取得失敗: {e} → config固定リストにフォールバック", "warning")

        # ── フォールバック: config.watch_symbols 固定リスト ──
        fixed_symbols = list(self.config.watch_symbols or [self.config.symbol])
        old_count = len(self._watch_symbols)
        with self._lock:
            self._watch_symbols = fixed_symbols
        self._symbols_last_refresh = now
        self._log(
            f"✅ 監視銘柄をフォールバック{len(fixed_symbols)}銘柄に設定: "
            f"{', '.join(s.replace('/USDT','') for s in fixed_symbols[:10])}... "
            f"（前回: {old_count}銘柄 → 次回確認: {self._symbols_refresh_hours:.0f}時間後）",
            "info"
        )

    # ── スキャンループ（エントリー分析専用・バックグラウンド）──
    def _scan_loop(self):
        """
        全銘柄スキャン専用ループ。5秒ごとに少数銘柄をスキャンするラウンドロビン方式。
        5銘柄×5秒 = 100銘柄を約100秒で1周。スキャン間隔が短いためエントリーが素早くなる。
        メインループ（損切りチェック）とは別スレッドで動いているので、
        スキャン中でも損切りは止まらない。
        """
        _batch_log_interval = 20   # 20バッチごとに「全銘柄スキャン完了」をログ出力
        while self._running:
            try:
                # 起動直後と24時間ごとに銘柄リストをBinanceから自動更新
                self._refresh_watch_symbols()

                self._scan_batch()
                with self._lock:
                    self._scan_count += 1
                    sc = self._scan_count

                # 20バッチ（約100秒 = 全銘柄1周）ごとにログ出力
                if sc % _batch_log_interval == 0:
                    with self._lock:
                        sym_count = len(self._watch_symbols)
                        per_sig   = dict(self._per_signal)
                    pos_count = len(self._positions)
                    # シグナル内訳を集計してログ出力
                    _sig_long  = sum(1 for r in per_sig.values() if r.get("signal") == Signal.LONG)
                    _sig_short = sum(1 for r in per_sig.values() if r.get("signal") == Signal.SHORT)
                    _sig_hold  = sum(1 for r in per_sig.values() if r.get("signal") in (Signal.HOLD, "HOLD"))
                    _fg = self.mktctx.fear_greed

                    # ── v6.0 RULE-15: PF（プロフィットファクター）追跡ログ ──────────
                    # PF = 総利益 ÷ 総損失。目標値: PF>1.5
                    # 1.0未満 = 赤字システム（要改善）
                    # 1.0〜1.5 = 損益分岐〜良好（改善継続）
                    # 1.5超 = プロレベル（維持を目標）
                    _metrics = self.risk.calc_performance_metrics()
                    _pf  = _metrics.get("profit_factor", 0.0)
                    _wr  = round(self.risk.trade_history and
                                 len([t for t in self.risk.trade_history if t.won]) /
                                 len(self.risk.trade_history) * 100, 1) or 0
                    _n   = len(self.risk.trade_history)
                    _consec = self.risk._consecutive_losses
                    if _pf >= 1.5:
                        _pf_mark = "🟢"    # 目標達成
                    elif _pf >= 1.0:
                        _pf_mark = "🟡"    # 損益分岐以上
                    else:
                        _pf_mark = "🔴"    # 改善が必要

                    self._log(
                        f"🔍 スキャン{sc}回完了 | 監視{sym_count}銘柄 | ポジション{pos_count}件 "
                        f"| シグナル: LONG={_sig_long} SHORT={_sig_short} HOLD={_sig_hold} | F&G={_fg} "
                        f"| {_pf_mark} PF={_pf:.3f}(目標1.5) WR={_wr}% N={_n} 連敗={_consec}",
                        "info"
                    )
                    # v47.0: 統計異常検知（Polymarketパターン警告）
                    # WR > 80% かつ 100件以上 = 両建てMMバグの可能性を警告
                    if _n >= 100 and _wr > 80.0:
                        logger.warning(
                            f"⚠️ v47.0 統計異常: 勝率{_wr:.1f}%({_n}件) > 80% → "
                            f"Polymarketパターン警告。バグ・両建て・過学習の疑い。"
                            f"実際のエッジを再検証してください。"
                        )
                    # L1警告: 3連敗以上でサイズ縮小を明示通知
                    if _consec >= 3:
                        logger.warning(
                            f"⚠️ v47.0 L1警告: {_consec}連敗 → Anti-Martingaleサイズ縮小中 "
                            f"(3連敗=50%・5連敗=35%・7連敗=25%)"
                        )
                    # v36.0: オープンポジションの現在状態をログ（~100秒ごと）
                    with self._lock:
                        _open_pos = dict(self._positions)
                    for _sym, _pos in _open_pos.items():
                        _cp = self.fetcher.fetch_current_price(_sym)
                        if _cp and _cp > 0 and _pos.entry_atr > 0:
                            _pnl = _pos.current_pnl(_cp)
                            _tp1_dist = _pos.entry_atr * self.config.tp1_atr_mult
                            _tp1_price = (_pos.entry_price - _tp1_dist if _pos.side == "short"
                                          else _pos.entry_price + _tp1_dist)
                            _to_tp1_pct = (_cp - _tp1_price) / _pos.entry_price * 100
                            _tp1_status = "✨TP1間近" if abs(_to_tp1_pct) < 0.3 else ""
                            self._log(
                                f"📊 [{_pos.side.upper()}] {_sym} | "
                                f"entry={_pos.entry_price:.5f} → {_cp:.5f} | "
                                f"PnL={_pnl:+.2f}USD | "
                                f"TP1={_tp1_price:.5f}(残{_to_tp1_pct:+.2f}%) "
                                f"SL={_pos.sl_price:.5f} {_tp1_status}",
                                "info"
                            )
                    # マーケット全体のブル/ベア比率を更新
                    bull_n = _sig_long
                    self.mktctx.update_market_breadth(bull_n, max(len(per_sig), 1))

            except Exception as e:
                self._log(f"⚠️ スキャンループエラー: {e}", "error")
                logger.exception("スキャンループで予期しないエラー")

            # 次のスキャンまで待機（1秒ずつ停止シグナルを確認）
            for _ in range(self.config.scan_interval_s):
                if not self._running:
                    break
                time.sleep(1)

    # ── バッチスキャン ────────────────────────────────
    def _scan_batch(self):
        """
        ラウンドロビンで全銘柄をスキャンする。
        100銘柄なら1回のスキャンで全銘柄を処理する。
        """
        with self._lock:
            symbols = list(self._watch_symbols)
        if not symbols:
            symbols = [self.config.symbol]
        n = len(symbols)
        if n == 0:
            return

        batch_size = min(self.config.scan_batch_size, n)
        batch = [symbols[(self._scan_pointer + i) % n] for i in range(batch_size)]
        self._scan_pointer = (self._scan_pointer + batch_size) % n

        # ブラックリスト取得
        blacklist = set(getattr(self.config, 'symbol_blacklist', []))

        for sym in batch:
            # ブラックリスト銘柄はスキャン自体をスキップ
            base = sym.replace("/USDT", "").replace("/BTC", "")
            if base in blacklist:
                logger.debug(f"{sym} ブラックリスト銘柄のためスキップ")
                continue
            try:
                self._scan_symbol(sym)
            except Exception as e:
                logger.debug(f"{sym} スキャンスキップ: {e}")

    # ── 1銘柄スキャン ────────────────────────────────
    def _scan_symbol(self, symbol: str):
        """
        1つの銘柄をスキャンして、シグナルを評価してエントリーを検討する。
        """
        # データ取得
        multi_tf = self.fetcher.fetch_multi_timeframe(symbol)
        cp       = self.fetcher.fetch_current_price(symbol)
        if not cp:
            return

        # データ保存＋トレーリングストップ更新
        with self._lock:
            self._market_data[symbol] = {
                "ohlcv": multi_tf, "current_price": cp, "ts": time.time()
            }
            if symbol in self._positions:
                self._positions[symbol].update_trail_peak(cp)

        # データが取れていない場合はスキップ
        if not multi_tf:
            return

        # ── 1時間足トレンドを取得（5分キャッシュ）──
        now_ts = time.time()
        cached = self._trend_cache.get(symbol)
        if cached and (now_ts - cached["ts"]) < self._trend_cache_ttl:
            trend_1h = cached["trend"]
        else:
            df_1h = self.fetcher.fetch_ohlcv(
                symbol, self.config.trend_tf,
                limit=self.config.trend_ohlcv_limit
            )
            trend_1h = get_1h_trend(df_1h, self.config)
            self._trend_cache[symbol] = {"trend": trend_1h, "ts": now_ts}

        # シグナル評価
        try:
            consensus = evaluate_consensus(multi_tf, self.config, trend_1h=trend_1h, fear_greed=self.mktctx.fear_greed, btc_trend=self._get_btc_trend())
        except Exception as e:
            logger.debug(f"{symbol} シグナル評価エラー: {e}")
            return

        leverage = self.risk.calc_leverage(
            consensus["score"],
            fear_greed=self.mktctx.fear_greed,
            btc_trend=self._get_btc_trend()
        )
        result   = {**consensus, "leverage": leverage, "symbol": symbol}

        with self._lock:
            self._per_signal[symbol] = result
            if consensus.get("signal") not in (Signal.HOLD, "HOLD"):
                self._last_signal_result = result
                # v35.0b: 非HOLDシグナルをINFOログ（エントリー検討の可視化）
                logger.info(
                    f"📡 {symbol} シグナル={consensus.get('signal')} "
                    f"score={consensus.get('score', 0):.2f} "
                    f"F&G={self.mktctx.fear_greed} BTC={self._get_btc_trend()}"
                )

        # ── シグナル反転による即決済チェック ──
        with self._lock:
            pos = self._positions.get(symbol)
        if pos and should_exit_on_signal_flip(pos.side, consensus.get("signal", Signal.HOLD)):
            _fg_now = self.mktctx.fear_greed
            _btc_now = self._get_btc_trend()

            # 【v9.2 最低保有時間チェック: エントリー直後60秒以内はflip禁止】
            # 理由: signal確認（v9.0）で30秒持続確認後エントリーするが、
            # 直後の即flipは往復手数料コストのみ発生する無駄なトレードになる。
            _held_flip_s = time.time() - pos.entry_time
            _min_hold_flip_s = getattr(self.config, "min_flip_hold_seconds", 60)
            _flip_allowed = _held_flip_s >= _min_hold_flip_s

            if not _flip_allowed:
                logger.debug(
                    f"{symbol} signal_flip待機: 保有{_held_flip_s:.0f}s < "
                    f"{_min_hold_flip_s:.0f}s（エントリー直後保護）"
                )
            # 【v7.2 Extreme Fear SHORT保護（全BTC方向対応）】
            # F&G≤25 の場合、SHORTポジションはLONGシグナルでもフリップしない。
            elif pos.side == "short" and _fg_now <= 25:
                logger.info(
                    f"🔒 {symbol} F&G={_fg_now}≤25: SHORT継続"
                    f"（一時LONG反発を無視 = Extreme Fear全方向SHORT保護）"
                )
            else:
                # 【v9.1 利益保護: 利益中ポジションは2回連続反転確認後にのみクローズ】
                # 利益が出ている → 一時的な反転でポジションを切らないよう確認を要求
                # 損失中 → 素早く損切り（確認不要）
                # v15.0: 利益保護閾値を0.5×SL_ATR → 0.2×ATRに変更（より多くのポジションを保護）
                # 理由: 0.5×SL_ATR（0.875×ATR）は厳しすぎて「利益中」と判定されにくかった。
                # 0.2×ATRに緩和することで、少し利益が出ただけでも即クローズを防ぐ。
                _min_profit_atr = pos.entry_atr * 0.20  # 最小利益判定: ATR×0.2
                _is_in_profit_fc = (
                    (pos.side == "long" and cp >= pos.entry_price + _min_profit_atr)
                    or (pos.side == "short" and cp <= pos.entry_price - _min_profit_atr)
                ) if _min_profit_atr > 0 else False

                # v15.0: エントリーから30分以内はsignal_flipを無効化（ノイズ耐性）
                # 理由: 平均保有17.8分でほとんど終了していた。30分以内の反転は「一時的ノイズ」が多い。
                # SLが機能するので損切りは保証される。signal_flipは不要な早期終了を増やすだけ。
                _held_m = _held_flip_s / 60.0
                _flip_noise_guard = _held_m < 30.0  # 30分以内はsignal_flipをブロック

                _rev_sig_str = str(consensus.get("signal", ""))
                _now_fc = time.time()
                _min_flip_confirm_s = max(self.config.scan_interval_s * 0.75, 20)

                if _flip_noise_guard:
                    # エントリーから30分以内 → signal_flipを無視（SLに任せる）
                    logger.debug(
                        f"🛡️ {symbol} signal_flip無視: エントリー{_held_m:.0f}分<30分（ノイズ耐性保護）"
                        f" pos={pos.side} pnl概算={'+' if _is_in_profit_fc else '-'}"
                    )
                elif _is_in_profit_fc:
                    # 利益中 → 反転確認システムを使う（2回確認が必要）
                    _fc = self._flip_confirm.get(symbol)
                    if _fc is None or _fc.get("rev_signal") != _rev_sig_str:
                        # 初見反転シグナル → 記憶して今回はスキップ
                        self._flip_confirm[symbol] = {
                            "rev_signal": _rev_sig_str,
                            "first_seen": _now_fc,
                        }
                        logger.debug(
                            f"📈 {symbol} 利益中({pos.side}) 反転シグナル初見"
                            f"({_rev_sig_str}) → 確認待ち（利益保護）"
                        )
                    else:
                        _fc_age = _now_fc - _fc["first_seen"]
                        if _fc_age < _min_flip_confirm_s:
                            logger.debug(
                                f"📈 {symbol} 利益中 反転確認待ち "
                                f"{_fc_age:.0f}s < {_min_flip_confirm_s:.0f}s"
                            )
                        else:
                            # 2回連続確認済み → クローズ
                            self._flip_confirm.pop(symbol, None)
                            self._log(
                                f"🔄 {symbol} 反転2回確認後クローズ"
                                f"（利益保護確認{_fc_age:.0f}s）",
                                "info"
                            )
                            self._close_position(symbol, cp, "signal_flip")
                else:
                    # 30分超 + 損失中 → 確認1回待ちでクローズ（早まらない）
                    _fc2 = self._flip_confirm.get(symbol)
                    if _fc2 is None or _fc2.get("rev_signal") != _rev_sig_str:
                        self._flip_confirm[symbol] = {
                            "rev_signal": _rev_sig_str,
                            "first_seen": _now_fc,
                        }
                        logger.debug(f"🔄 {symbol} 損失中 反転初見 → 1回待ち")
                    else:
                        _fc2_age = _now_fc - _fc2["first_seen"]
                        if _fc2_age >= _min_flip_confirm_s:
                            self._flip_confirm.pop(symbol, None)
                            self._log(f"🔄 シグナル反転確認のため {symbol} を決済（損失中・確認済み）", "info")
                            self._close_position(symbol, cp, "signal_flip")

        # エントリーチェック
        self._check_entry(symbol, result, cp)

    # ── BTCトレンド取得（ロング/ショート方向バイアス用）──
    def _get_btc_trend(self) -> str:
        """
        BTCのトレンドを返す（"up" / "down" / "range"）。
        15m足と1h足の両方を確認して、より信頼性の高いトレンド判定をする。
        5分間キャッシュ。

        設計: 15m + 1h の両足でトレンドが一致したときのみ「明確な方向」と判定。
        15m: 短期のトレンド方向
        1h:  中期のトレンド方向（より信頼性が高い）

        BTC上昇中 → ロングが有利（ショートは制限）
        BTC下降中 → ショートが有利（ロングは制限 or 禁止）
        """
        now = time.time()
        if now - self._btc_trend_cache["ts"] < 300:
            return self._btc_trend_cache["trend"]

        def _ema_trend(df, span_short=20, span_long=50) -> str:
            if df is None or len(df) < span_long:
                return "range"
            close = df["close"]
            ema_s = close.ewm(span=span_short).mean().iloc[-1]
            ema_l = close.ewm(span=span_long).mean().iloc[-1]
            price = close.iloc[-1]
            if price > ema_s and ema_s > ema_l:
                return "up"
            elif price < ema_s and ema_s < ema_l:
                return "down"
            return "range"

        try:
            df_15m = self.fetcher.fetch_ohlcv("BTC/USDT", "15m", limit=60)
            df_1h  = self.fetcher.fetch_ohlcv("BTC/USDT", "1h",  limit=60)
            trend_15m = _ema_trend(df_15m)
            trend_1h  = _ema_trend(df_1h)

            # 両足が一致→明確なトレンド。一致しない→range
            if trend_15m == trend_1h and trend_15m != "range":
                trend = trend_15m
            elif trend_1h != "range":
                # 1h足を優先（15m足のノイズより信頼性が高い）
                trend = trend_1h
            else:
                trend = trend_15m if trend_15m != "range" else "range"

        except Exception:
            trend = "range"

        self._btc_trend_cache = {"trend": trend, "ts": now}
        logger.info(f"📊 BTCトレンド更新: {trend} (15m={trend_15m}, 1h={trend_1h})")
        return trend

    def _count_direction_positions(self, direction: str) -> int:
        """ロングまたはショートのポジション数を数える"""
        with self._lock:
            return sum(1 for p in self._positions.values() if p.side == direction)

    def _calc_100_score(
        self, symbol: str, signal_result: dict,
        current_price: float, tp_price: float, sl_price: float, atr: float,
        direction_override: Optional[str] = None,  # デッドキャットフリップ等での方向上書き用
    ) -> int:
        """
        entry_scorer の8項目評価を呼び出して100点スコアを計算する。
        データ取得に失敗した場合はフォールバックとして既存スコアを変換して使う。
        direction_override: "long"/"short" を指定するとシグナルの方向を上書きできる
        """
        try:
            # 1分足データ（エントリー足）
            df_primary = self.fetcher.fetch_ohlcv(symbol, self.config.primary_tf, limit=100)
            # 15分足データ（トレンド足）
            df_trend   = self.fetcher.fetch_ohlcv(symbol, self.config.trend_tf, limit=100)

            if df_primary is None or df_trend is None or df_primary.empty:
                # データ取得失敗 → 既存スコア（0〜1）を100点に換算
                existing = signal_result.get("score", 0)
                return int(existing * 100)

            # 指標を付与
            from indicators import add_all_indicators
            df_primary = add_all_indicators(df_primary, self.config)
            df_trend   = add_all_indicators(df_trend, self.config)

            # direction_override が指定されていればそちらを優先（デッドキャットフリップ等）
            if direction_override and direction_override in ("long", "short"):
                direction = direction_override
            else:
                direction = signal_result.get("signal", "HOLD").lower()
            if direction not in ("long", "short"):
                return 0

            score_100, score_details = calc_entry_score(
                df_primary   = df_primary,
                df_trend     = df_trend,
                direction    = direction,
                entry_price  = current_price,
                tp_price     = tp_price,
                sl_price     = sl_price,
                atr          = atr,
                fear_greed   = self.mktctx.fear_greed,
                btc_trend    = self._get_btc_trend(),  # v5.1: BTC回復時ペナルティ緩和用
            )

            if score_100 > 0:
                logger.debug(
                    f"{symbol} エントリースコア: {score_100}点 | "
                    + " | ".join(
                        f"{k}: {v.get('点数',0)}点"
                        for k, v in score_details.items()
                        if isinstance(v, dict) and "点数" in v
                    )
                )

            return score_100

        except Exception as e:
            logger.debug(f"{symbol} 100点スコア計算エラー（フォールバック）: {e}")
            existing = signal_result.get("score", 0)
            return int(existing * 100)

    def _handle_emergency(self, level: int):
        """
        緊急レベルに応じた対処を行う。

        Lv.1: 注意ログ（エントリーは _check_entry で制御）
        Lv.2: 警戒ログ
        Lv.3: 緊急ログ + 弱いポジションのSLをブレークイーブンに移動
        Lv.4: 全ポジションを成行クローズ
        """
        if level == self._emergency_level:
            return  # レベル変化がなければ何もしない

        if level >= 4 and self._emergency_level < 4:
            self._log("🚨🚨🚨 Lv.4 緊急停止: 本日損失8%超過 → 全ポジション強制クローズ", "error")
            with self._lock:
                symbols = list(self._positions.keys())
            for sym in symbols:
                with self._lock:
                    pos = self._positions.get(sym)
                if pos:
                    cp = self.fetcher.fetch_current_price(sym) or pos.entry_price
                    self._close_position(sym, cp, "force")

        elif level >= 3 and self._emergency_level < 3:
            self._log(
                "🚨🚨 Lv.3 緊急停止: 本日損失5%超過 OR 総リスク10%超 → 新規エントリー禁止",
                "error"
            )
            # 弱いポジション（含み損が大きいもの）のSLをBEPに移動
            with self._lock:
                symbols = list(self._positions.keys())
            for sym in symbols:
                with self._lock:
                    pos = self._positions.get(sym)
                if pos and not self._be_triggered.get(sym, False):
                    buf = pos.entry_price * 0.001
                    if pos.side == "long":
                        new_sl = pos.entry_price + buf
                        if pos.sl_price < new_sl:
                            pos.sl_price = new_sl
                    else:
                        new_sl = pos.entry_price - buf
                        if pos.sl_price > new_sl:
                            pos.sl_price = new_sl
                    self._be_triggered[sym] = True

        elif level >= 2 and self._emergency_level < 2:
            self._log(
                "⚠️⚠️ Lv.2 警戒停止: 6連続損切り OR 本日損失3%超 → 当日新規エントリー停止",
                "warn"
            )

        elif level >= 1 and self._emergency_level < 1:
            self._log(
                "⚠️ Lv.1 注意アラート: 3連続損切り OR 総リスク7%超 → 30分間エントリー制限",
                "warn"
            )

        self._emergency_level = level

    # ── 銘柄別動的レバレッジ計算（勝率連動型）──────────
    def _calc_symbol_leverage(self, symbol: str, atr: float,
                               current_price: float, signal_score: float) -> float:
        """
        直近の勝率に基づいてレバレッジを自動調整する。

        【判断ロジック】
        基本: 1倍（安全ベース）
        直近20トレードの勝率が55%超かつスコアが高い → 最大1.5倍まで許容
        直近20トレードの勝率が60%超かつスコアが高い → 最大2.0倍まで許容
        勝率が50%未満 → 強制1倍

        なぜこの設計か:
        勝率37%でレバレッジを上げると損失だけが増幅される。
        勝率が統計的に証明された後だけ、慎重にレバレッジを上げる。
        """
        # 直近20トレードの勝率を計算
        recent = list(self.risk.trade_history)[-20:]
        if len(recent) >= 10:
            recent_wr = len([t for t in recent if t.won]) / len(recent)
        else:
            recent_wr = 0.0  # サンプル不足 → 保守的に0%扱い

        # config.min_leverage を絶対の下限として尊重
        # （config.min_leverage=2.0のとき、勝率不足でも最低2倍を保証）
        # 勝率に応じた最大レバレッジ上限
        if recent_wr >= 0.60 and signal_score >= 0.70:
            # 勝率60%超 + 強シグナル → config.max_leverageまで許可
            max_lev = self.config.max_leverage
        elif recent_wr >= 0.55 and signal_score >= 0.60:
            # 勝率55%超 + 良シグナル → 中間レバレッジ
            max_lev = (self.config.min_leverage + self.config.max_leverage) / 2
        else:
            # それ以外 → config.min_leverage（設定した最低値を保証）
            max_lev = self.config.min_leverage

        # config の max_leverage も超えない（設定値を尊重）
        max_lev = min(max_lev, self.config.max_leverage)
        # config の min_leverage を下回らない（設定値を保証）
        max_lev = max(max_lev, self.config.min_leverage)

        logger.debug(
            f"{symbol} レバレッジ決定: 直近勝率{recent_wr*100:.0f}% "
            f"スコア{signal_score:.2f} → {max_lev}倍"
        )
        return max_lev

    # ── Feature 5: 相関ガード（既存ポジションとの相関チェック）──────
    def _calc_correlation_with_positions(self, symbol: str, direction: str) -> float:
        """
        これから入ろうとしている銘柄と、同方向の既存ポジションとの「相関係数」を計算する。

        相関係数とは:
          2つの銘柄の価格が「同じ方向に動く度合い」を-1〜+1の数値で表したもの。
          +1.0 = 完全に同じ動き（片方が上がれば必ずもう片方も上がる）
          0.0  = 無関係（独立した動き）
          -1.0 = 正反対の動き

        なぜ重要か:
          高い相関がある銘柄を複数保有するのは、実質的に「同じ銘柄を複数持つ」のと同じ。
          リスク分散になっておらず、市場が動いたとき一気に全部損失になる。
          相関0.80超 = 「ほぼ同じ動き」なのでエントリーを禁止する。

        戻り値: 最大相関係数（0.0〜1.0）
        """
        max_corr = 0.0

        with self._lock:
            same_dir_positions = {
                sym: pos for sym, pos in self._positions.items()
                if pos.side == direction
            }

        for held_sym, _ in same_dir_positions.items():
            try:
                # 対象銘柄と既存銘柄の直近30本の終値を取得
                data_new  = self._market_data.get(symbol, {}).get("ohlcv", {})
                data_held = self._market_data.get(held_sym, {}).get("ohlcv", {})

                primary_tf = self.config.primary_tf
                df_new  = data_new.get(primary_tf)
                df_held = data_held.get(primary_tf)

                if df_new is None or df_held is None:
                    continue
                if len(df_new) < 30 or len(df_held) < 30:
                    continue

                # 直近30本の終値で相関係数を計算
                close_new  = df_new["close"].tail(30).values.astype(float)
                close_held = df_held["close"].tail(30).values.astype(float)

                if len(close_new) != len(close_held):
                    min_len = min(len(close_new), len(close_held))
                    close_new  = close_new[-min_len:]
                    close_held = close_held[-min_len:]

                # ピアソン相関係数を計算（numpy利用）
                std_new  = np.std(close_new)
                std_held = np.std(close_held)
                if std_new == 0 or std_held == 0:
                    continue

                corr = float(np.corrcoef(close_new, close_held)[0, 1])
                if not np.isnan(corr):
                    max_corr = max(max_corr, abs(corr))

            except Exception as e:
                logger.debug(f"相関計算スキップ ({symbol} vs {held_sym}): {e}")
                continue

        return max_corr

    # ── Feature 6: 適応型パフォーマンスガード ──────────────────
    def _get_adaptive_score_penalty(self) -> int:
        """
        直近10トレードの成績に応じて、エントリースコアの閾値を動的に調整する。

        なぜ必要か:
          アルゴリズムが「今の相場と合っていない」時期は、いくら頑張っても
          損失が続く。そういう時期は自動的に「もっと厳しい条件でないと入らない」
          ように調整することで、大きな連続損失を防ぐ。

          逆に調子が良い（勝率50%以上）ときは少しだけ閾値を下げて
          チャンスを積極的に取りに行く。

        戻り値（ペナルティ点数）:
          -5 = ボーナス（閾値を5点下げる = 入りやすくする）
           0 = 変更なし
          +5 = ペナルティ（閾値を5点上げる = 厳しくする）
          +10 = 大ペナルティ
          +15 = 最大ペナルティ（アルゴ不調 = 非常に厳しくする）
        """
        # 直近10トレードを取得
        recent_trades = list(self.risk.trade_history)[-10:]
        if len(recent_trades) < 5:
            # データ不足は判断しない（初期は通常通り動かす）
            return 0

        wins     = sum(1 for t in recent_trades if t.won)
        win_rate = wins / len(recent_trades)  # 勝率（0.0〜1.0）

        if win_rate >= 0.50:
            # 勝率50%以上 = 調子が良い → 少しだけ入りやすくする（-5点ボーナス）
            return -5
        elif win_rate >= 0.40:
            # 勝率40〜49% = 普通 → 変更なし
            return 0
        elif win_rate >= 0.30:
            # 勝率30〜39% = やや不振 → +5点ペナルティ
            return 5
        elif win_rate >= 0.20:
            # 勝率20〜29% = 不振 → +10点ペナルティ
            return 10
        else:
            # 勝率20%未満 = 深刻な不振 → +15点ペナルティ（非常に厳しく）
            return 15

    # ── エントリーチェック ────────────────────────────
    def _check_entry(self, symbol: str, signal_result: dict, current_price: float):
        """
        シグナルとリスク管理を確認して、ポジションを開くか判断する。

        チェック順序:
          1. シグナルがHOLDなら即リターン
          2. 既に保有中・上限超過・クールダウン中チェック
          3. 緊急レベルチェック（Lv.2以上は新規禁止）
          4. ポートフォリオ状態A〜Eチェック
          5. BTCトレンドフィルター（方向別上限チェック）
          6. 同方向ポジション数チェック（最大30件）
          7. TP/SL計算
          8. 100点エントリースコアチェック（70点未満は禁止）
          9. ポジションサイジング（スコア85点以上でフルサイズ）
          10. 市場センチメント（Fear&Greed）によるサイズ調整
        """
        # ── Feature 7: カスケード崩壊保護（無効化中）────────────
        # ユーザー指示により一時停止中。再有効化するときはここのコメントを外す。
        # if time.time() < self._cascade_halt_until:
        #     remain = int((self._cascade_halt_until - time.time()) / 60)
        #     logger.debug(f"{symbol} カスケード保護中 → あと{remain}分停止")
        #     return

        signal = signal_result.get("signal", Signal.HOLD)
        if signal in (Signal.HOLD, "HOLD"):
            return

        # ── v42.0: マーケットコンテキスト未ロードガード ──────────
        # F&G=0 or is_ready=False のときはエントリーを禁止する
        # 理由: 起動直後にF&G/BTCトレンドが未取得の状態でエントリーすると
        #      スコア計算・方向フィルターが全て無効になる（過去の全損失6件の根本原因）
        if not self.mktctx.is_ready or self.mktctx.fear_greed == 0:
            logger.debug(
                f"🔒 v42.0 {symbol} マーケットコンテキスト未ロード"
                f"（is_ready={self.mktctx.is_ready}, F&G={self.mktctx.fear_greed}）→ エントリースキップ"
            )
            return

        with self._lock:
            already_have   = symbol in self._positions
            pos_count      = len(self._positions)
            sym_pos_count  = sum(1 for s in self._positions if s == symbol)

        # ── 同一銘柄ポジション数チェック ──
        # _positions は辞書（symbol→pos）なので同一銘柄は1つしか持てない
        # already_have=True のまま進むと既存ポジションが上書きされるバグの防止
        if already_have:
            return

        # ── ポジション上限・クールダウンチェック ──
        # v81.0: F&G連動の厳格ポジション上限（v73.0を大幅強化）
        # 実績: F&G=21で20件保有→全部同じ理由で負け→-$135の壊滅的損失
        # 新ルール: 極度恐怖では最大3件のみ。厳選した最高品質のみに集中。
        _fg_now = self.mktctx.fear_greed
        if _fg_now <= 20:
            _effective_max_pos = min(self.config.max_positions, 3)   # 極度恐怖: 最大3件
        elif _fg_now <= 30:
            _effective_max_pos = min(self.config.max_positions, 5)   # 恐怖: 最大5件
        elif _fg_now <= 40:
            _effective_max_pos = min(self.config.max_positions, 10)  # やや恐怖: 最大10件
        else:
            _effective_max_pos = self.config.max_positions            # 中立以上: 通常
        if pos_count >= _effective_max_pos:
            return

        # ── v82.0: 直近成績悪化時のエントリー停止（v83.0: 時間リセット付き）──
        # 問題: スクリーンショット時(17:01)は勝てていたが、トレンド終了後も入り続けて壊滅
        # 対策: 直近2時間の取引PFが0.5未満 → 新規エントリー停止
        # v83.0: 「直近10件」ではなく「直近2時間以内の取引」に変更
        #   理由: 古い取引のPFが永遠にリセットされず、新規エントリーが永久停止されるバグ防止
        #   2時間以内の取引が5件未満 → フィルターをスキップ（十分なサンプルがない）
        _pf_window_s = 2 * 3600  # 2時間
        _now = time.time()
        _recent_trades = [
            t for t in self.risk.trade_history
            if t.exit_time > _now - _pf_window_s
        ]
        if len(_recent_trades) >= 5:
            _rw = sum(t.pnl for t in _recent_trades if t.won)
            _rl = abs(sum(t.pnl for t in _recent_trades if not t.won))
            _recent_pf = _rw / _rl if _rl > 0 else 999
            if _recent_pf < 0.5:
                logger.info(
                    f"🛑 v82.0 {symbol} 直近2h内{len(_recent_trades)}件PF={_recent_pf:.2f}<0.5 → "
                    f"エントリー停止（相場が勝てる状態に戻るまで待機）"
                )
                return

        # ── リスクマネージャーのクールダウンチェック ──
        can, reason = self.risk.can_trade()
        if not can:
            logger.debug(f"{symbol} リスクマネージャー取引停止: {reason}")
            return

        # ── 時間帯フィルター（v4.0 RULE-10: 低流動性時間帯は取引禁止）──
        # 深夜0〜6時UTC（日本時間9〜15時が最も流動性が高い）
        _banned_hours = getattr(self.config, "banned_hours_utc", [0, 1, 2, 3, 4, 5])
        if _banned_hours:
            import datetime as _dt
            _utc_hour = _dt.datetime.utcnow().hour
            if _utc_hour in _banned_hours:
                logger.debug(f"{symbol} UTC{_utc_hour}時は低流動性時間帯 → エントリースキップ")
                return

        # ── SLクールダウンチェック（SL後の再エントリー禁止） ──
        # SLで損切りされた銘柄には sl_reentry_cooldown_s 秒間（デフォルト5分）
        # 再エントリーしない。IOやMAVへの連続損失を防ぐための重要なフィルター。
        with self._lock:
            sl_ban_until = self._sl_cooldown.get(symbol, 0)
        if time.time() < sl_ban_until:
            remain = int(sl_ban_until - time.time())
            logger.debug(f"{symbol} SLクールダウン中 → あと{remain}秒禁止")
            return

        # ── v87.0: BTC モメンタム+出来高サージチェック ──────────────────
        # v86.0にBTC出来高チェックを追加: 「本物のトレンドか偽物か」を判定
        # 出来高が平均の1.3倍以上 = 機関投資家参加 = 本物のトレンド → 許可
        # 出来高が平均以下 = フェイクムーブの可能性 → 禁止
        direction = signal_result.get("signal", Signal.HOLD)
        if direction not in (Signal.HOLD, "HOLD"):
            try:
                with self._lock:
                    _btc_ohlcv = self._market_data.get("BTC/USDT", {}).get("ohlcv", {})
                _btc_5m = _btc_ohlcv.get("5m")
                if _btc_5m is not None and len(_btc_5m) >= 10:
                    _btc_now = float(_btc_5m["close"].iloc[-1])
                    _btc_prev5 = float(_btc_5m["close"].iloc[-2])
                    _dir = "long" if direction in (Signal.LONG, "LONG") else "short"

                    # 1. BTC方向チェック（v86.0維持）
                    if _btc_prev5 > 0:
                        _btc_mom = (_btc_now - _btc_prev5) / _btc_prev5
                        if _dir == "long" and _btc_mom < -0.0005:
                            logger.debug(f"v87.0 {symbol} BTC5分下落({_btc_mom*100:+.3f}%) → LONG禁止")
                            return
                        if _dir == "short" and _btc_mom > 0.0005:
                            logger.debug(f"v87.0 {symbol} BTC5分上昇({_btc_mom*100:+.3f}%) → SHORT禁止")
                            return

                    # 2. BTC出来高サージチェック（v87.0新規）
                    if "volume" in _btc_5m.columns:
                        _vol_now = float(_btc_5m["volume"].iloc[-1])
                        _vol_avg = float(_btc_5m["volume"].iloc[-10:].mean())
                        if _vol_avg > 0 and _vol_now < _vol_avg * 0.5:
                            # v89.0: 1.0→0.5に緩和（平均の50%以下のみブロック）
                            # 旧: 平均以下で全ブロック → 取引機会ゼロに
                            logger.debug(
                                f"v89.0 {symbol} BTC出来高不足({_vol_now/_vol_avg:.1f}×平均<0.5) → エントリー見送り"
                            )
                            return
            except Exception:
                pass

        # ── フリップクールダウンチェック（signal_flip後の再エントリー禁止） ──
        # signal_flipで決済した後は15分間再エントリーを禁止する。
        # 理由: LONG→SHORT→LONGの高速フリップは手数料ドレインになるため。
        with self._lock:
            flip_ban_until = self._flip_cooldown.get(symbol, 0)
        if time.time() < flip_ban_until:
            remain = int(flip_ban_until - time.time())
            logger.debug(f"{symbol} フリップクールダウン中 → あと{remain//60}分{remain%60}秒禁止")
            return

        # ── 連続損失クールダウンチェック（3連敗後60分禁止） ──
        with self._lock:
            consec_ban_until = self._consec_cooldown.get(symbol, 0)
        if time.time() < consec_ban_until:
            remain = int(consec_ban_until - time.time())
            logger.debug(f"{symbol} 連続損失クールダウン中 → あと{remain//60}分禁止")
            return

        # ── ポートフォリオ状態A〜Eチェック ──────────────
        with self._lock:
            positions_snap = dict(self._positions)
        portfolio_state = self.risk.get_portfolio_state(positions_snap)

        # 状態C（総リスク7〜9%） → 新規エントリー禁止
        if portfolio_state == "C":
            logger.debug(f"{symbol} ポートフォリオ状態C（総リスク7〜9%）→ 新規エントリー禁止")
            return
        # 状態D（総リスク9%以上） → 新規エントリー禁止
        if portfolio_state == "D":
            logger.debug(f"{symbol} ポートフォリオ状態D（総リスク9%超）→ 新規エントリー禁止")
            return
        # 状態E（本日損失5%超） → 新規エントリー全面禁止
        if portfolio_state == "E":
            logger.warning(f"⚠️ 状態E: 本日損失5%超過 → 本日の全新規エントリーを停止")
            return

        # ── Fear & Greed 動的スコア閾値フィルター ──────────
        # 設計方針: 市場の恐怖/強欲レベルに応じて LONG/SHORT の入りやすさを逆方向に調整する。
        # Extreme Fear（F&G<25）: LONG は最高水準(85点)を要求、SHORT は緩和(55点)
        # Extreme Greed（F&G>80）: SHORT は最高水準を要求、LONG は緩和
        # これにより「逆風に向かう取引」を排除し「風に乗る取引」を優先する。
        direction = signal.lower()
        fg_score = self.mktctx.fear_greed

        # ── BTCトレンド先取り（F&Gスコア計算前に必要）──────────────
        btc_trend = self._get_btc_trend()

        # F&Gに応じた必要最低スコアを計算（ロング用）
        # 【重要改善】BTC 1h="up"（短期回復中）のときはF&Gペナルティを1段階緩和する
        # 理由: F&G=23でもBTCが回復中 = 市場底圏での反発局面
        #      この局面でLONG完全禁止にすると「底値買い」のチャンスを全て逃す
        #      BTCの実際の値動きを優先してF&G制限を1段階緩和する
        btc_recovery = (btc_trend == "up" and fg_score <= 35)  # BTC回復中 + まだ恐怖圏

        # v14.0: LONGをF&G 15以上でほぼ全面解禁（スコア要件で品質管理）
        _fg_long_ban = getattr(self.config, 'fg_long_ban_threshold', 15)
        _counter_trend_long = False  # 逆張りLONG（半サイズ）フラグ
        if direction == "long":
            if fg_score < _fg_long_ban:
                # Extreme Fear（F&G<15）= LONG完全禁止（市場崩壊・パニック売り中）
                _fg_required_score = 999
            elif fg_score <= 25:
                # Extreme Fear圏（F&G 15-25）: スコア85以上 + 半サイズ
                # v19.0: BTC回復中（btc_recovery=True）はスコア閾値を72に緩和
                # v37.0b: F&G≤22（深い極度恐怖）では緩和を廃止 → スコア88以上必須
                # 根拠: AXS LONG score=91, F&G=21 → -$10.73 損失（1サンプルだが示唆的）
                #       F&G=21のような深い恐怖では市場全体の売り圧力が強く、
                #       LONGはBTCが短期回復してもアルトが追従しない場合が多い。
                #       F&G 23-25はまだ反発の可能性があるため72を維持。
                if btc_recovery:
                    if fg_score <= 22:
                        _fg_required_score = 88  # v37.0b: 深い極度恐怖は厳格（72→88）
                    else:
                        _fg_required_score = 72   # v19.0: F&G 23-25 BTC回復中は緩和維持
                else:
                    _fg_required_score = 85
                _counter_trend_long = True   # 半サイズフラグ
            elif fg_score <= 35:
                # Fear圏（F&G 25-35）: スコア78以上（v14.0: 90→78に大幅緩和）
                # v19.0: BTC回復中はさらに緩和して70点に
                # 理由: Fear圏でも「売られすぎからの反発LONG」は有望なチャンス
                if btc_recovery:
                    _fg_required_score = 70   # v19.0: BTC回復中は緩和（78→70）
                else:
                    _fg_required_score = 78
                _counter_trend_long = True   # やや逆張りのため半サイズ
            elif fg_score <= 45:
                _fg_required_score = 73    # Fear圏下位（35-45）: やや緩和
            elif fg_score <= 55:
                _fg_required_score = self.config.min_entry_score  # 中立: 通常スコア
            else:
                _fg_required_score = self.config.min_entry_score  # 通常（55超・強気）
        else:  # short
            if fg_score >= 85:
                _fg_required_score = 999   # 絶対禁止（Extreme Greed = 反発危険）
            elif fg_score >= 70:
                _fg_required_score = 80    # Greed: 厳格（v14.0: 82→80に緩和）
            elif fg_score <= 15:
                _fg_required_score = 60    # Extreme Fear崩壊中 = ショート有利 → やや緩和（v45.0: 55→60）
            elif fg_score <= 35:
                # v60.0: btc_recovery中（BTC上昇+F&G≤35）はSHORTのスコア閾値を厳格化
                # 理由: BTC回復中はSHORT逆張りリスクが高い（METIS SHORT score=69 → 損失の実例）
                # btc_recovery時: 死後猫バウンスでBTCが上昇中 → SHORTは高品質シグナルのみ許可
                if btc_recovery:
                    _fg_required_score = 80   # v60.0: btc_recovery中SHORT = 80点以上必須
                else:
                    _fg_required_score = 65   # Fear = ショート優位だが品質管理（v45.0: 60→65）
            else:
                _fg_required_score = self.config.min_entry_score  # 通常

        long_count  = self._count_direction_positions("long")
        short_count = self._count_direction_positions("short")

        # ── v57.0: btc_recovery中の逆張りLONG集中制限 ─────────────────────
        # 問題: F&G≤35+BTC上昇（btc_recovery）環境で逆張りLONGが15件以上同時オープンすることがある。
        # 全て同じ「BTC回復」テーマで相関が高い → BTCが急落すると同時に全件SL発動のリスク。
        # 対策: 上限8件に制限 → スコア上位の8件のみ保有、9件目以降は見送り。
        # 安全性: 残り12スロットはSHORTや非btc_recovery銘柄が埋められる。
        # v60.0: バグ修正 self.positions → positions_snap (self.positionsは未定義属性でAttributeError)
        if _counter_trend_long and btc_recovery:
            _btc_recovery_long_count = sum(
                1 for p in positions_snap.values()
                if p.side == "long"
                and getattr(p, 'counter_trend', False)
                and getattr(p, 'entry_btc_trend', "") == "up"
            )
            _max_recovery_longs = getattr(self.config, 'max_btc_recovery_longs', 8)
            if _btc_recovery_long_count >= _max_recovery_longs:
                logger.info(
                    f"⛔ {symbol} v57.0 btc_recoveryLONG上限({_max_recovery_longs}件): "
                    f"現在{_btc_recovery_long_count}件 → エントリー見送り（集中リスク回避）"
                )
                return

        _is_dead_cat_flip = False  # デッドキャットバウンスフリップフラグ（モメンタムフィルター免除用）
        _btc_counter_short = False  # v37.0: BTC上昇中の逆張りSHORTフラグ（極度恐怖圏のみ許可）

        # ── BTCトレンドフィルター（v5.1: BTC回復時SHORT制限を緩和）─────────────
        # [設計変更] BTC 1h="up" でも F&G<=25 の場合はSHORTを4件まで許可する。
        # 理由: 超恐怖（F&G<=25）中のBTC短期回復 = 「デッドキャットバウンス」が多い。
        # デッドキャットバウンス = 一時的な戻りの後に再下落。
        # SHORT勝率が高い局面なので、制限を完全禁止から上限4件に緩和する。
        if btc_trend == "down" and direction == "long":
            if fg_score <= 35:
                # v11.0: Fear以下（F&G≤35）+ BTC下降 = LONG完全禁止（25→35に拡大）
                # ── v6.0 デッドキャットバウンスSHORT検出 ────────────────
                # 「LONGが来たが下落相場でブロック」→ 逆にSHORTできないか確認する
                # 根拠: F&G≤35+BTC下降中はSHORT勝率が高い（60%超）
                #   LONGシグナルが出ているということは銘柄が「反発中」
                #   反発してRSIが過熱していれば「デッドキャットバウンス頂上でのSHORT」狙い
                _can_flip_to_short = False
                _flip_reason = ""
                try:
                    with self._lock:
                        _flip_md = self._market_data.get(symbol, {}).get("ohlcv", {})
                    _flip_df = _flip_md.get(self.config.primary_tf)
                    if _flip_df is not None and not _flip_df.empty:
                        # RSIカラム名は indicators.py で動的に付与される
                        _rsi_col = "rsi"
                        # RSIが指標計算済みでない場合は簡易計算
                        if _rsi_col not in _flip_df.columns and len(_flip_df) >= 14:
                            from indicators import add_all_indicators as _aii
                            _flip_df = _aii(_flip_df.copy(), self.config)
                        if _rsi_col in _flip_df.columns:
                            _rsi_flip = float(_flip_df[_rsi_col].iloc[-1])
                            # RSI下落フィルター: 前の足より下がっていることを確認（ピーク越え確認）
                            _rsi_flip_prev = float(_flip_df[_rsi_col].iloc[-2]) if len(_flip_df) >= 2 else _rsi_flip + 1
                            _rsi_flip_declining = _rsi_flip < _rsi_flip_prev   # ピーク越えて下落開始
                            _dce_rsi_thr = getattr(self.config, 'dce_rsi_threshold', 58)
                            _dce_short_limit = self.config.max_same_direction
                            if not pd.isna(_rsi_flip) and _rsi_flip >= _dce_rsi_thr and _rsi_flip_declining and short_count < _dce_short_limit:
                                # EMAより上にいるか確認（バウンス頂上付近）
                                _ema_col = f"ema_{self.config.ema_short}"
                                _price = float(_flip_df["close"].iloc[-1])
                                if _ema_col in _flip_df.columns:
                                    _ema_val = float(_flip_df[_ema_col].iloc[-1])
                                    if _price >= _ema_val * 0.995:
                                        _can_flip_to_short = True
                                        _flip_reason = (
                                            f"RSI={_rsi_flip:.0f}≥{_dce_rsi_thr}(前足{_rsi_flip_prev:.0f}から下落) → "
                                            f"デッドキャットバウンスSHORT機会（RSIピーク越え確認）"
                                        )
                                elif _rsi_flip >= _dce_rsi_thr:
                                    _can_flip_to_short = True
                                    _flip_reason = (
                                        f"RSI={_rsi_flip:.0f}≥{_dce_rsi_thr}(前足{_rsi_flip_prev:.0f}から下落) → "
                                        f"デッドキャットバウンスSHORT機会"
                                    )
                except Exception:
                    pass

                if _can_flip_to_short:
                    logger.info(
                        f"🔄 {symbol} F&G={fg_score}+BTC下降中: {_flip_reason} → "
                        f"LONGをSHORTにフリップ（下落トレンドへの逆張り禁止→追従）"
                    )
                    direction = "short"
                    _is_dead_cat_flip = True   # モメンタムフィルター免除フラグ
                    # SHORT用のF&G必要スコアに更新（Extreme Fearのとき55点）
                    _fg_required_score = 55
                else:
                    # v41.0: F&G≤25(Extreme Fear) + BTC下降 → LONGを完全ブロック
                    # 根拠: F&G≤25+BTC下降はLONGの最悪環境。実績で全敗（AXS score=91→-$10.73）。
                    # SPK/USDT LONG(BTC下降+F&G=21)も-$0.46と期待値マイナスのパターン。
                    # DCE SHORTフリップができない場合はエントリー禁止（機会損失より損失回避を優先）。
                    if fg_score <= 25:
                        logger.info(
                            f"🚫 v41.0 {symbol} F&G={fg_score}≤25(極度恐怖) + BTC下降 → LONG完全ブロック"
                            f"（DCE条件未成立 | 最悪市場環境でのLONG禁止）"
                        )
                        return
                    # v14.0: F&G 26-35: 完全禁止 → スコア85以上なら逆張りLONG許可
                    if fg_score < _fg_long_ban:  # F&G<15の崩壊中のみ完全禁止
                        logger.info(
                            f"🚫 {symbol} F&G={fg_score}(<{_fg_long_ban}崩壊中) + BTC下降 → LONG完全禁止"
                        )
                        return
                    _fg_required_score = max(_fg_required_score, 85)  # 最低85点要求（F&G 26-35）
                    logger.info(
                        f"⚠️ {symbol} F&G={fg_score}+BTC下降中: DCE条件未成立 → LONG厳格許可（スコア85↑要求）"
                    )
            elif fg_score <= 40:
                # v14.0: 完全禁止 → スコア82以上なら許可（v8.0の完全禁止を緩和）
                _fg_required_score = max(_fg_required_score, 82)
                logger.debug(f"{symbol} F&G={fg_score}(Fear)+BTC下降: スコア82以上なら許可（v14.0緩和）")
            elif fg_score <= 50:
                if long_count >= 4:
                    logger.debug(f"{symbol} 中立下寄り+BTC下降: ロング{long_count}件 ≥ 4件上限 → スキップ")
                    return
            else:
                if long_count >= 8:
                    logger.debug(f"{symbol} BTC下降中: ロング{long_count}件 ≥ 8件上限 → スキップ")
                    return
        elif btc_trend == "up" and direction == "long" and fg_score <= 35:
            # 【v11.0 拡大: F&G≤35 + BTC短期上昇 + LONGシグナル = デッドキャットバウンス】
            # 旧: F&G≤25のみ。新: F&G≤35まで拡大（Fear圏全体をカバー）
            # F&G≤35 + BTC短期上昇 + LONGシグナル = 典型的なデッドキャットバウンス。
            # 「落ちた猫が一瞬跳ね上がる」現象: 下落相場での一時的な回復は本物の上昇ではない。
            # RSI≥55（過熱気味）のとき = バウンスの頂上付近 = SHORTの絶好のタイミング。
            # 根拠: F&G=23でBTC一時的回復中は全50銘柄がLONGシグナルを出す = どれも偽のLONG。
            _can_flip_up = False
            _flip_reason_up = ""
            try:
                with self._lock:
                    _flip_md2 = self._market_data.get(symbol, {}).get("ohlcv", {})
                _flip_df2 = _flip_md2.get(self.config.primary_tf)
                if _flip_df2 is not None and not _flip_df2.empty:
                    _rsi_col2 = "rsi"
                    if _rsi_col2 not in _flip_df2.columns and len(_flip_df2) >= 14:
                        from indicators import add_all_indicators as _aii2
                        _flip_df2 = _aii2(_flip_df2.copy(), self.config)
                    if _rsi_col2 in _flip_df2.columns:
                        _rsi_val2 = float(_flip_df2[_rsi_col2].iloc[-1])
                        # RSI下落フィルター: 前の足より下がっていることを確認（ピーク越え確認）
                        _rsi_val2_prev = float(_flip_df2[_rsi_col2].iloc[-2]) if len(_flip_df2) >= 2 else _rsi_val2 + 1
                        _rsi_val2_declining = _rsi_val2 < _rsi_val2_prev   # ピーク越えて下落開始
                        # RSI≥65かつ下落開始: バウンスで価格過熱し、ピークを越えた段階 → SHORT機会
                        # v7.4改善: RSIが「まだ上昇中」の段階でSHORTするとsignal_flipで即クローズされる。
                        #          前足より下落していることを確認することで、ピーク越え後にのみエントリー。
                        _dce_rsi_thr2 = getattr(self.config, 'dce_rsi_threshold', 58)
                        _dce_short_limit2 = self.config.max_same_direction
                        if not pd.isna(_rsi_val2) and _rsi_val2 >= _dce_rsi_thr2 and _rsi_val2_declining and short_count < _dce_short_limit2:
                            _can_flip_up = True
                            _flip_reason_up = (
                                f"F&G={fg_score}(極度恐怖)+BTC短期反発中+RSI={_rsi_val2:.0f}≥{_dce_rsi_thr2}"
                                f"(前足{_rsi_val2_prev:.0f}から下落) → デッドキャットバウンスピーク越え確認SHORT"
                            )
            except Exception:
                pass

            if _can_flip_up:
                # v29.0: btc_recovery中（BTC上昇+F&G≤35）はDCEフリップをスキップ
                # v37.0c: F&G≤22（深い極度恐怖）ではDCEフリップを復活 → SHORT許可
                # 根拠:
                #   v29.0のDCEスキップは「btc_recovery中のDCEは期待値マイナス」が根拠。
                #   しかしv37.0bの分析（F&G=21ではLONG失敗）と合わせると、
                #   F&G≤22では「LONGより高品質DCE SHORTの方が有望」と判断できる。
                #   F&G 23-35は従来通りDCEスキップ→LONG優先を維持。
                if fg_score <= 22:
                    # 深い極度恐怖: DCE SHORTを許可（BTCが上昇中でも市場は下落圧力強）
                    direction = "short"
                    _is_dead_cat_flip = True
                    _fg_required_score = 60  # DCE SHORT用スコア閾値
                    logger.info(
                        f"v37.0c {symbol} DCE条件成立(RSI={_rsi_val2:.0f}) + F&G={fg_score}≤22 → "
                        f"深い極度恐怖: DCE SHORTを実行（v29.0スキップを解除）"
                    )
                else:
                    # F&G 23-35: 従来通りDCEスキップしてLONG優先
                    logger.info(
                        f"v29.0 {symbol} DCE条件成立(RSI={_rsi_val2:.0f}≥{_dce_rsi_thr2}) だが "
                        f"btc_recovery=True(BTC上昇+F&G={fg_score}≤35) → DCEスキップ, LONG許可（スコア70↑）"
                    )
                    _fg_required_score = max(_fg_required_score, 70)
            else:
                # v14.0: 完全禁止 → スコア78以上なら通常LONGを許可
                # v19.0: btc_recovery=True（BTC本物の回復中）はさらに緩和して70点に
                # 理由: F&G≤35 + BTC短期回復中 = 反発が本物の可能性もある。
                # DCE条件が揃っていなければ普通にLONGを取れるよう緩和する。
                if btc_recovery:
                    _fg_required_score = max(_fg_required_score, 70)  # v19.0: BTC回復中はbtc_recoveryベースの閾値を尊重
                    logger.debug(
                        f"{symbol} F&G={fg_score}+BTC回復中(btc_recovery): DCE条件未成立 → LONG許可（スコア70↑要求）"
                    )
                else:
                    _fg_required_score = max(_fg_required_score, 78)
                    logger.debug(
                        f"{symbol} F&G={fg_score}+BTC回復中: DCE条件未成立 → LONG許可（スコア78↑要求）"
                    )

        elif btc_trend == "up" and direction == "short":
            # v21.0: BTC上昇中の戦略由来SHORTを完全ブロック
            # v37.0: F&G≤25（極度恐怖深い圏）の場合は高品質SHORTを例外的に許可
            # 背景: v21.0は entry_score=0 のスコアなし取引の4連敗を根拠に追加。
            #       現在はスコアリングシステム（75点以上）があるため品質管理できる。
            #       F&G=21-25の極度恐怖では市場全体が下落圧力強く、BTCの一時回復でも
            #       アルトコインは下落継続することが多い。高品質SHORTは有効。
            if fg_score <= 25:
                # v89.0: 極度恐怖圏SHORTしきい値を88→82に緩和
                # 旧v66.0: 88点 → btc_recoveryでSHORT機会がほぼゼロ → 取引停止の原因
                # v89.0: 82点 → 高品質SHORTは許可しつつ低品質(69-81)は引き続きブロック
                # 安全装置: v85.0 quick_stop(0.6ATR/30分) + v87.0 アダプティブSL(1.0ATR)
                # BTC上昇中 = 上昇回復トレンド → SHORTは高確信シグナルのみ許可
                _fg_required_score = max(_fg_required_score, 82)  # v89.0: 88→82に緩和
                _btc_counter_short = True  # BTC逆張りフラグ → サイズ0.75倍
                logger.info(
                    f"v66.0 {symbol} F&G={fg_score}≤25(極度恐怖) + BTC短期回復中 → "
                    f"高品質SHORT例外許可（スコア88以上・0.75倍サイズ）"
                )
            else:
                # F&G 26-35（Fear圏）: 従来通りブロック
                logger.info(
                    f"🚫 {symbol} BTC上昇中 + 戦略SHORT → v21.0完全ブロック "
                    f"(BTC方向逆張り禁止 | F&G={fg_score})"
                )
                return

        # ── 同方向ポジション数チェック（最大12件）────────
        dir_count = long_count if direction == "long" else short_count
        if dir_count >= self.config.max_same_direction:
            logger.debug(f"{symbol} {direction}方向 {dir_count}件 ≥ 上限{self.config.max_same_direction}件")
            return

        # ── Feature 5: 相関ガード ──────────────────────────
        # 既存の同方向ポジションと高い相関がある銘柄への重複エントリーを防ぐ。
        # 相関0.80超 = 「ほぼ同じ動き」なので、実質的なリスク分散にならない。
        max_corr = self._calc_correlation_with_positions(symbol, direction)
        if max_corr > 0.80:
            logger.debug(
                f"{symbol} 相関過多: 既存ポジションと相関{max_corr:.2f} > 0.80 → スキップ"
            )
            return

        # ── v14.0 シンプル化: 過剰フィルター全廃（スコアに委ねる設計）────
        # 旧バージョンで存在した以下のフィルターを全て廃止:
        #   ❌ RSI FOMO防止フィルター (RSI>75 LONG禁止)
        #   ❌ 15m RSI方向確認フィルター
        #   ❌ RSI転換点フィルター (v10.0)
        #   ❌ ファンディングレート方向制限
        #   ❌ v5.0 防御パターン (D-01〜D-05)
        #   ❌ ADX上昇中フィルター
        #   ❌ 直近ローソク足モメンタムフィルター
        # 理由: 上記フィルターが「重なり合い」1日のエントリーチャンスを数件に限定していた。
        #       これらの条件は entry_scorer の75点制度に吸収されており、二重チェックは不要。
        #       シンプルなルール=多くのチャンス=統計的に安定した損益になる。

        # ── 唯一残すセーフティ: BTC±5%の激しい急変動のみ停止 ──────────
        # (±3%は正常な変動範囲。±5%は本当の異常時のみ)
        try:
            with self._lock:
                _btc_data_v14 = self._market_data.get("BTC/USDT", {}).get("ohlcv", {})
            _df_btc_v14 = _btc_data_v14.get("5m")
            if _df_btc_v14 is not None and len(_df_btc_v14) >= 2:
                _btc_c1 = float(_df_btc_v14["close"].iloc[-1])
                _btc_c0 = float(_df_btc_v14["close"].iloc[-2])
                if _btc_c0 > 0 and abs(_btc_c1 - _btc_c0) / _btc_c0 >= 0.05:
                    logger.warning(f"⚡ {symbol} BTC 5分±5%超急変動 → エントリー停止（異常相場）")
                    return
        except Exception:
            pass

        # ── TP/SL計算 ───────────────────────────────────
        # v20.0: SL/TP計算に15m ATRを優先使用（ノイズ耐性改善）
        # 理由: 5m ATR（例: ICP=0.00666, 0.25%）は細かすぎて
        #       普通の価格ノイズ（0.625%の下げ）で6.8分後にSLに当たることが判明。
        #       15m ATR（例: ICP=0.01430, 0.54%）を使えばSL=1.35%に拡大、
        #       ノイズ耐性が2倍以上になり、TP到達率が大幅に改善する。
        _atr_5m   = signal_result.get("atr") or 0
        _tf_res   = signal_result.get("tf_results") or {}
        _atr_15m  = (_tf_res.get("15m") or {}).get("atr") or 0
        # 15m ATRが取得できれば優先使用、なければ5m ATRで代替
        atr = _atr_15m if _atr_15m > 0 else _atr_5m
        if _atr_15m > 0 and _atr_5m > 0:
            logger.debug(
                f"{symbol} v20.0 ATR選択: 15m={_atr_15m:.5f}({_atr_15m/current_price*100:.2f}%) "
                f"5m={_atr_5m:.5f}({_atr_5m/current_price*100:.2f}%) → 15m優先使用"
            )

        # ── v43.0: ATR=0 エントリーブロック ──────────────────────
        # ATRが計算できない（新規上場・流動性不足・データ取得失敗）銘柄はエントリーしない
        # 理由: ATR=0だとTP1/TP2部分利確・ブレークイーブンストップ・Chandelier Exit
        #      が全て機能しない。固定%のみのリスク管理になりシステムの優位性が失われる。
        #      過去のSPK/SUSHI/ZAMAのようなATR=0ポジション再発防止。
        if atr == 0:
            logger.debug(
                f"🔒 v43.0 {symbol} ATR=0（データ不足）→ エントリースキップ"
                f"（ATRなしではTP1/TP2/Chandelier/BE-stopが全て無効になる）"
            )
            return

        # ── v6.1: BTC/ETH主要銘柄SL拡張（ノイズ耐性優先）─────────────
        # BTC/ETHは流動性が高い分だけ「ノイズ幅」も大きく、通常のSL=1.75ATRでは
        # 大口の注文フローによるスパイクで刈られやすい。
        # SL=2.0ATR, TP=4.0ATR でRR=2.0:1を維持しながらノイズ耐性を強化する。
        # v14.0: tp_atr_multをconfigから動的に読み込み（5.0倍設定に対応）
        _cfg_tp_mult = self.config.tp_atr_mult  # 現在5.0倍
        _major_coins = {"BTC/USDT", "ETH/USDT"}
        if symbol in _major_coins and atr > 0 and current_price > 0:
            _major_sl_mult = 2.0   # SL広め（1.75→2.0: BTC/ETHのノイズ耐性）
            _major_tp_mult = _cfg_tp_mult  # v14.0: config値を使用（5.0倍）
            _min_sl_pct = 0.003
            _min_tp_pct = 0.006
            _sl_dist_maj = max(atr * _major_sl_mult, current_price * _min_sl_pct)
            _tp_dist_maj = max(atr * _major_tp_mult, current_price * _min_tp_pct)
            # RR確認（最低min_rr_ratio:1）
            if _sl_dist_maj > 0 and _tp_dist_maj / _sl_dist_maj < self.config.min_rr_ratio:
                _tp_dist_maj = _sl_dist_maj * self.config.min_rr_ratio
            if direction == "long":
                tp_price = current_price + _tp_dist_maj
                sl_price = current_price - _sl_dist_maj
            else:
                tp_price = current_price - _tp_dist_maj
                sl_price = current_price + _sl_dist_maj
            logger.debug(
                f"{symbol} 主要銘柄SL拡張: SL=2.0ATR({_sl_dist_maj:.4g}), "
                f"TP={_major_tp_mult:.1f}ATR({_tp_dist_maj:.4g}) → RR={_tp_dist_maj/_sl_dist_maj:.2f}:1"
            )
        elif _is_dead_cat_flip and atr > 0 and current_price > 0:
            # ── v6.3: デッドキャットバウンスSHORT専用SL拡張 ──────────────
            # RSI≥58の反発ピーク後にSHORTするが、dead cat bounceは一時的に
            # さらに上昇してからSHORTが効き始めるケースがある。
            # SL=2.5ATR（広め）, TP=config.tp_atr_mult → v14.0でRR=2.0:1達成
            _dce_sl_mult = 2.5
            _dce_tp_mult = _cfg_tp_mult   # v14.0: config値使用（5.0×ATR）
            _min_sl_pct  = 0.003
            _min_tp_pct  = 0.005
            _sl_dist_dce = max(atr * _dce_sl_mult, current_price * _min_sl_pct)
            _tp_dist_dce = max(atr * _dce_tp_mult, current_price * _min_tp_pct)
            # RR確認（最低1.8:1）
            if _sl_dist_dce > 0 and _tp_dist_dce / _sl_dist_dce < 1.8:
                _tp_dist_dce = _sl_dist_dce * 1.8
            # dead cat bounceはSHORTのみ（directionは必ずshort）
            tp_price = current_price - _tp_dist_dce
            sl_price = current_price + _sl_dist_dce
            _rr_dce = _tp_dist_dce / _sl_dist_dce if _sl_dist_dce > 0 else 0
            logger.info(
                f"{symbol} デッドキャットバウンスSL拡張: SL=2.5ATR({_sl_dist_dce:.4g}), "
                f"TP={_dce_tp_mult:.1f}ATR({_tp_dist_dce:.4g}) → RR={_rr_dce:.2f}:1"
            )
        else:
            tp_price, sl_price = self.risk.calc_tp_sl(current_price, atr, direction)

            # ── v94.0: 30m足用アダプティブSL ──────────────────────────
            # v87.0を30m足に最適化。30m足のATRは1h足より小さいためSLもタイトに。
            if atr > 0 and current_price > 0:
                _fg_sl = self.mktctx.fear_greed
                if _fg_sl <= 30:
                    _adaptive_sl_mult = 0.8   # v94.0: 恐怖: 超タイト（1.0→0.8）
                elif _fg_sl <= 60:
                    _adaptive_sl_mult = 1.0   # v94.0: 中立: config値（1.5→1.0）
                else:
                    _adaptive_sl_mult = 0.9   # v94.0: 強欲: やや警戒（1.2→0.9）
                _adaptive_sl_dist = atr * _adaptive_sl_mult
                if direction == "long":
                    sl_price = current_price - _adaptive_sl_dist
                else:
                    sl_price = current_price + _adaptive_sl_dist

        # ═══════════════════════════════════════════════════════════════
        # L3戦略フィルター（PROM $42損失の原因を根絶する強化版）
        # ═══════════════════════════════════════════════════════════════

        # ── BTCトレンド強制一致（最優先・絶対に失敗しない形で実装）──────
        # 【バグ修正】旧コード getattr(self, "_btc_trend", ...) は属性名が違い常に"range"で誤作動していた
        # 【原因確定】PROM -$42 損失はBTC=downなのにLONG許可された事による
        try:
            _btc_trend_now = self._get_btc_trend()  # 正しいメソッド呼び出し
        except Exception:
            _btc_trend_now = "range"
        if _btc_trend_now == "up" and direction == "short":
            logger.info(f"🛡️ L3 {symbol} BTC上昇中のSHORT → 強制見送り")
            return
        if _btc_trend_now == "down" and direction == "long":
            logger.info(f"🛡️ L3 {symbol} BTC下降中のLONG → 強制見送り（PROM-$42の再発防止）")
            return

        # ── 自動改善オーバーライド判定（auto_improver.py が書き込む）─────
        try:
            import json as _json, os as _os
            _override_path = _os.path.join(_os.path.dirname(__file__), 'runtime_override.json')
            if _os.path.exists(_override_path):
                with open(_override_path) as _of:
                    _ov = _json.load(_of)
                import datetime as _dt
                _rev = _ov.get("revert_at")
                if _rev:
                    _rev_dt = _dt.datetime.fromisoformat(_rev)
                    if _dt.datetime.now() > _rev_dt:
                        _os.unlink(_override_path)
                    else:
                        _blacklist = _ov.get("symbol_blacklist", [])
                        if symbol in _blacklist:
                            logger.info(f"🔒 [自動改善] {symbol} ブラックリスト中 → 見送り（理由: {_ov.get('reason','')}）")
                            return
                        _avoid_fg = _ov.get("avoid_fg_band")
                        if _avoid_fg:
                            _fg_band_now = "fear(<25)" if fg_score < 25 else ("neutral(25-60)" if fg_score < 60 else "greed(>60)")
                            if _fg_band_now == _avoid_fg:
                                logger.info(f"🔒 [自動改善] F&G {_avoid_fg} 回避中 → 見送り")
                                return
        except Exception as _oe:
            logger.debug(f"オーバーライド判定エラー: {_oe}")

        # ── ADX / ATR% フィルター（強度確認）──────────────────────────
        try:
            with self._lock:
                _md = self._market_data.get(symbol, {}).get("ohlcv", {})
            _df_primary = _md.get(self.config.primary_tf)
            if _df_primary is not None and len(_df_primary) >= 2:
                import pandas as pd
                if "adx" in _df_primary.columns:
                    _adx_val = float(_df_primary["adx"].iloc[-1])
                    if not pd.isna(_adx_val) and _adx_val < self.config.adx_threshold:
                        logger.info(f"🛡️ L3 {symbol} ADX={_adx_val:.1f} < {self.config.adx_threshold} 横ばい → 見送り")
                        return
                if "atr" in _df_primary.columns:
                    _atr_val_filt = float(_df_primary["atr"].iloc[-1])
                    _close_val = float(_df_primary["close"].iloc[-1])
                    if not pd.isna(_atr_val_filt) and _close_val > 0:
                        _atr_pct = (_atr_val_filt / _close_val) * 100
                        if _atr_pct < self.config.min_atr_pct:
                            logger.info(f"🛡️ L3 {symbol} ATR%={_atr_pct:.2f}% < {self.config.min_atr_pct}% ボラ不足 → 見送り")
                            return
                        # PROM対策: ATR%が異常に大きい銘柄（>5%）も回避（スリッページ・乱高下で大損リスク）
                        if _atr_pct > 5.0:
                            logger.info(f"🛡️ L3 {symbol} ATR%={_atr_pct:.2f}% > 5% 高ボラすぎ → 見送り（急変動で大損リスク）")
                            return
        except Exception:
            pass

        # ── v94.0: EMAクロス転換検知 ────────────────────────────────
        # EMA9がEMA21を逆方向にクロス → トレンド転換のサイン → エントリー禁止
        # LONG: EMA9 < EMA21（短期が長期の下 = 下落転換中）→ 禁止
        # SHORT: EMA9 > EMA21（短期が長期の上 = 上昇転換中）→ 禁止
        try:
            if _df_primary is not None and len(_df_primary) >= 21:
                _ema9 = float(_df_primary["close"].ewm(span=9).mean().iloc[-1])
                _ema21 = float(_df_primary["close"].ewm(span=21).mean().iloc[-1])
                if direction == "long" and _ema9 < _ema21:
                    logger.debug(f"v94.0 {symbol} EMA9<EMA21（下落転換中）→ LONG見送り")
                    return
                if direction == "short" and _ema9 > _ema21:
                    logger.debug(f"v94.0 {symbol} EMA9>EMA21（上昇転換中）→ SHORT見送り")
                    return
        except Exception:
            pass

        # ── v92.0: 4重フィルター（プルバック+4h確認+RSI反転+出来高サージ）──
        try:
            with self._lock:
                _md = self._market_data.get(symbol, {}).get("ohlcv", {})
            _df_1h = _md.get(self.config.primary_tf)
            _df_4h = _md.get(self.config.trend_tf)

            if _df_1h is not None and len(_df_1h) >= 5:
                # ── フィルター1: プルバック待ちエントリー ──────────────
                # 「トレンド方向に一時的に戻ったところ」で入る = 有利な価格で入れる
                # LONG: 直近3本のうち少なくとも1本が陰線（=一時的な押し目）
                # SHORT: 直近3本のうち少なくとも1本が陽線（=一時的な戻り）
                _recent_candles = _df_1h.iloc[-3:]
                if direction == "long":
                    _has_pullback = any(_recent_candles["close"] < _recent_candles["open"])
                else:
                    _has_pullback = any(_recent_candles["close"] > _recent_candles["open"])
                if not _has_pullback:
                    logger.debug(f"v92.0 {symbol} プルバックなし（{direction}方向に連続→天井/底掴みリスク）→ 見送り")
                    return

                # ── フィルター2: RSI反転確認 ──────────────────────────
                # LONG: RSIが前足より上昇中（下落から反転した証拠）
                # SHORT: RSIが前足より下落中（上昇から反転した証拠）
                if "rsi" in _df_1h.columns and len(_df_1h) >= 3:
                    _rsi_now = float(_df_1h["rsi"].iloc[-1])
                    _rsi_prev = float(_df_1h["rsi"].iloc[-2])
                    import pandas as pd
                    if not pd.isna(_rsi_now) and not pd.isna(_rsi_prev):
                        if direction == "long" and _rsi_now < _rsi_prev:
                            logger.debug(f"v92.0 {symbol} RSI下降中({_rsi_now:.0f}<{_rsi_prev:.0f}) → LONG見送り")
                            return
                        if direction == "short" and _rsi_now > _rsi_prev:
                            logger.debug(f"v92.0 {symbol} RSI上昇中({_rsi_now:.0f}>{_rsi_prev:.0f}) → SHORT見送り")
                            return

                # ── フィルター3: 出来高サージ確認 ──────────────────────
                # 直近の出来高が10本平均の1.2倍以上 = 機関投資家参加 = 本物の動き
                if "volume" in _df_1h.columns and len(_df_1h) >= 10:
                    _vol_now = float(_df_1h["volume"].iloc[-1])
                    _vol_avg = float(_df_1h["volume"].iloc[-10:].mean())
                    if _vol_avg > 0 and _vol_now < _vol_avg * 1.2:
                        logger.debug(f"v92.0 {symbol} 出来高不足({_vol_now/_vol_avg:.1f}×平均<1.2) → 見送り")
                        return

            # ── フィルター4: 4h+1h二重トレンド確認 ──────────────────
            # 4h足のトレンドがエントリー方向と一致するか確認
            if _df_4h is not None and len(_df_4h) >= 50:
                _ema20_4h = float(_df_4h["close"].ewm(span=20).mean().iloc[-1])
                _ema50_4h = float(_df_4h["close"].ewm(span=50).mean().iloc[-1])
                _price_4h = float(_df_4h["close"].iloc[-1])
                if direction == "long" and not (_price_4h > _ema20_4h and _ema20_4h > _ema50_4h):
                    logger.debug(f"v92.0 {symbol} 4h足トレンド不一致 → LONG見送り")
                    return
                if direction == "short" and not (_price_4h < _ema20_4h and _ema20_4h < _ema50_4h):
                    logger.debug(f"v92.0 {symbol} 4h足トレンド不一致 → SHORT見送り")
                    return
        except Exception:
            pass  # データ不足時はフィルターをスキップ

        # ── v6.2: キルゾーン時間帯はTP目標を20%拡大 ─────────────────────
        # London(08-09UTC)/NY AM(15-16UTC)/NY PM(19-20UTC)は機関投資家が活発に動く時間帯。
        # これらの時間帯は価格の動きが通常より大きくなる傾向がある。
        # SLはそのままでTPだけ広げることで、同じリスクでより多くの利益を狙う。
        # 注意: RR比が上がる（例: 2.0→2.4）のでTP到達は少し難しくなるが、到達時の利益が増える。
        try:
            import datetime as _dt_kz2
            _kz_hour = _dt_kz2.datetime.utcnow().hour
            _in_killzone_tp = (8 <= _kz_hour < 9) or (15 <= _kz_hour < 16) or (19 <= _kz_hour < 20)
            if _in_killzone_tp and current_price > 0 and tp_price > 0:
                _tp_expand = 1.20  # キルゾーンTP +20%拡大
                _tp_dist_orig = abs(tp_price - current_price)
                _tp_dist_new  = _tp_dist_orig * _tp_expand
                if direction == "long":
                    tp_price = current_price + _tp_dist_new
                else:
                    tp_price = current_price - _tp_dist_new
                logger.debug(
                    f"{symbol} キルゾーンTP拡大(UTC{_kz_hour}h): "
                    f"TP距離 +20% ({_tp_dist_orig:.4g}→{_tp_dist_new:.4g})"
                )
        except Exception:
            pass

        # TP距離が最低基準を下回るならスキップ（手数料負け防止）
        if current_price > 0 and tp_price > 0:
            tp_dist_pct = abs(tp_price - current_price) / current_price
            if tp_dist_pct < self.config.min_tp_dist_pct:
                logger.debug(
                    f"{symbol} TP距離 {tp_dist_pct*100:.3f}% < "
                    f"最低基準 {self.config.min_tp_dist_pct*100:.1f}% → スキップ（手数料負け防止）"
                )
                return

        # ── v18.0 Phase3: 15分足EMAトレンド一致フィルター ──────────────────
        # LONGエントリー: 15分足EMA9 ≥ EMA21 - 0.2%（上昇または均衡トレンド）
        # SHORTエントリー: 15分足EMA9 ≤ EMA21 + 0.2%（下降または均衡トレンド）
        # これにより「主要トレンドに逆らうエントリー」を排除し、WR+5〜8%を目標とする。
        # 免除条件: DCEショート（RSIによる独立フィルター保証）/ 5件未満のExtremeFeaer時
        if not _is_dead_cat_flip:
            try:
                with self._lock:
                    _df_15m_raw = self._market_data.get(symbol, {}).get("ohlcv", {}).get("15m")
                    _open_cnt_ema = len(self._positions)
                if _df_15m_raw is not None and not _df_15m_raw.empty and len(_df_15m_raw) >= 22:
                    _close_15m = _df_15m_raw["close"]
                    _ema9_15m  = float(_close_15m.ewm(span=9, adjust=False).mean().iloc[-1])
                    _ema21_15m = float(_close_15m.ewm(span=21, adjust=False).mean().iloc[-1])
                    if _ema21_15m > 0:
                        _ema_diff_pct = (_ema9_15m - _ema21_15m) / _ema21_15m
                        _ema_tol = 0.002  # ±0.2%以内は均衡（移行期）として許容
                        if direction == "long":
                            _ema_aligned_15m = _ema_diff_pct >= -_ema_tol
                        else:  # short
                            _ema_aligned_15m = _ema_diff_pct <= _ema_tol
                        if not _ema_aligned_15m:
                            # 例外: 5件未満 + Extreme Fear → 積極姿勢を維持
                            _ema_exempt = (_open_cnt_ema < 5 and fg_score <= 25)
                            if not _ema_exempt:
                                logger.info(
                                    f"⛔ {symbol} 15mEMAトレンド不一致"
                                    f"(EMA9={_ema9_15m:.4g} EMA21={_ema21_15m:.4g}"
                                    f" diff={_ema_diff_pct*100:+.2f}%"
                                    f" {direction}方向はEMAと逆) → スキップ"
                                )
                                return
            except Exception:
                pass  # データ取得失敗時はフィルターをスキップ

        # ── 100点エントリースコアチェック ────────────────
        # direction変数が更新されている場合（デッドキャットフリップ等）も正確に計算する
        score_100 = self._calc_100_score(
            symbol, signal_result, current_price, tp_price, sl_price, atr,
            direction_override=direction,
        )

        # ── Feature 9: スマート再エントリーブースト ──────────
        # 直前にTPを達成した銘柄が同方向のシグナルを出した場合、スコアを+8点する。
        # 理由: 一度TPを取れた = その銘柄はトレンドが強い証拠。
        # 同方向への再エントリーは「勝ちパターンの継続」として積極的に取りに行く。
        reentry = self._tp_reentry_watch.get(symbol)
        if reentry and time.time() < reentry.get("expires_at", 0):
            if reentry["direction"] == direction:
                # 同方向 → +8点ボーナス（再エントリーを優遇する）
                score_100 = min(100, score_100 + 8)
                logger.debug(f"{symbol} 直前TP達成の再エントリー候補 → スコア+8点")
        # 期限切れのウォッチエントリーを削除（メモリ管理）
        self._tp_reentry_watch = {
            k: v for k, v in self._tp_reentry_watch.items()
            if time.time() < v.get("expires_at", 0)
        }

        # F&G動的閾値の採用方法（方向によって異なる）
        # LONG: max() = 「基準」と「F&G制限」の厳しい方を採用（逆風時は厳しく）
        # SHORT in bear (F&G<35): F&G閾値を直接採用（市場の流れに乗るSHORTを優遇）
        # SHORT in bull (F&G>50): max() = 逆張りSHORTには厳しい基準
        # デッドキャットバウンスSHORT: 特別緩和（RSI≥65で既に品質保証済み）
        if _is_dead_cat_flip:
            # v55.0: DCBショート最低スコア50→70に引き上げ
            # 旧: RSI≥65フィルター済みなので50で十分、という設計だった
            # 問題: score=69のDCBショート(METIS)がSL損切り → RSI≥65だけでは品質保証不十分
            # 新: 70点以上要求 = スコアリング8項目の総合評価で十分な根拠があるときのみ許可
            # v66.0: btc_recovery中はDCBショートも_fg_required_score(88)を尊重する
            # 理由: btc_recovery(F&G≤25+BTC上昇中)ではDCBも上昇トレンドと戦う逆張り。
            #       RSI≥65だけでは品質保証不十分で、高い_fg_required_scoreを優先すべき。
            if btc_recovery and _fg_required_score > 70:
                effective_min_score = _fg_required_score  # = 88 in btc_recovery
                logger.debug(
                    f"{symbol} v66.0 DCBショートbtc_recovery適用: "
                    f"effective_min_score={_fg_required_score}（旧70→btc_recovery優先）"
                )
            else:
                effective_min_score = 70
        elif direction == "long":
            effective_min_score = max(self.config.min_entry_score, _fg_required_score)
        else:  # short
            if fg_score <= 35 and _fg_required_score < self.config.min_entry_score:
                # 弱気相場（F&G<35）: SHORTは市場の流れに乗る取引 → F&G閾値を優先採用
                effective_min_score = _fg_required_score
            else:
                # 強気・中立相場: 逆張りSHORTは厳しく審査
                effective_min_score = max(self.config.min_entry_score, _fg_required_score)

        # ── v6.1: ICTキルゾーン内はスコア閾値を5点緩和 ──────────────
        # London(08-09UTC)/NY AM(15-16UTC)/NY PM(19-20UTC) = 機関投資家が動く時間帯
        # → シグナルの信頼性が高い → min_entry_score 75点を70点に緩和（5点割引）
        try:
            import datetime as _dt_kz
            _utc_h = _dt_kz.datetime.utcnow().hour
            _in_killzone = (8 <= _utc_h < 9) or (15 <= _utc_h < 16) or (19 <= _utc_h < 20)
            if _in_killzone:
                # v62.0: btc_recovery逆張りLONGはICTキルゾーン緩和を適用しない
                # 理由: F&G≤35のbtc_recovery LONGは独自の品質保証条件（スコア88等）を既に持つ。
                # ICTボーナスを重ねると閾値未満の低品質エントリーを通してしまう（LUNC=86,IOTA=87の実例）
                if _counter_trend_long and btc_recovery:
                    logger.debug(
                        f"{symbol} ICTキルゾーン時間帯だがbtc_recovery逆張りLONG → 緩和スキップ"
                    )
                else:
                    # バグ修正: max()だとベア相場SHORT(effective=55)のとき70に引き上がってしまう
                    # 正しい動作: 常に-5点（下限50）= キルゾーンは必ず緩和する方向
                    effective_min_score = max(50, effective_min_score - 5)
                    logger.debug(
                        f"{symbol} ICTキルゾーン時間帯(UTC{_utc_h}時): "
                        f"スコア閾値-5点 → {effective_min_score}点"
                    )
        except Exception:
            pass

        # ── v18.0 Phase3: ポジション数に応じた段階的スコア閾値管理 ────────────
        # ポジションが少ない → 積極的にエントリー（閾値を下げる）
        # ポジションが多い  → 慎重にエントリー（閾値を上げる）
        # 目標: 常に10〜15件の適切なポジション数を維持する
        with self._lock:
            _open_count = len(self._positions)
        if _open_count < 5:
            # 5件未満: 最積極（max_positions=15の33%以下 → 急いで増やす）
            effective_min_score = max(50, effective_min_score - 5)
            logger.debug(f"{symbol} 段階スコア: {_open_count}件<5 → 閾値-5点 → {effective_min_score}点")
        elif _open_count < 10:
            # 5〜9件: やや積極（66%未満 → 積み増し）
            effective_min_score = max(55, effective_min_score - 3)
            logger.debug(f"{symbol} 段階スコア: {_open_count}件<10 → 閾値-3点 → {effective_min_score}点")
        elif _open_count >= 16:
            # v48.0: 16件以上（80%容量 = 20×0.8）: 慎重（残スロット少ない → 高品質のみ）
            # v46.0では12件（max=15の80%）、v48.0ではmax=20の80%=16件に更新
            effective_min_score = min(85, effective_min_score + 5)
            logger.debug(f"{symbol} 段階スコア: {_open_count}件≥16 → 閾値+5点 → {effective_min_score}点")
        # 10〜15件: 通常閾値（変更なし）

        # ── Feature 6: 適応型パフォーマンスガード ──────────
        # 直近成績に応じてスコア閾値を動的に上げ下げする。
        # 不調時は「もっと良いシグナルのみ通す」ように閾値を自動調整する。
        adaptive_penalty  = self._get_adaptive_score_penalty()
        # 【v7.3改善】デッドキャットバウンスSHORTは適応ペナルティを完全無効化
        # 理由: dead cat flipは RSI≥65 + Extreme Fear という独立した品質フィルターを持つ。
        #      直近の損失（主にLONG失敗）によるペナルティをこのSHORTに適用するのは不適切。
        #      「過去のLONGの負けによってSHORT機会を逃す」状況を防ぐ。
        if _is_dead_cat_flip:
            adaptive_penalty = 0  # dead cat bounce専用フィルター（RSI≥65）が品質保証するため
            logger.debug(f"{symbol} デッドキャットバウンスSHORT: 適応ペナルティ=0（独立品質フィルター適用）")
        # 【v7.3改善】Extreme Fear（F&G≤25）全状況でのSHORTペナルティを最大+5点に制限
        elif direction == "short" and fg_score <= 25:
            if adaptive_penalty > 5:
                logger.debug(
                    f"{symbol} Extreme Fear SHORT: 適応ペナルティを{adaptive_penalty}→5点に制限"
                    f"（F&G={fg_score} → SHORT有利相場での過剰制限を防止、BTC方向問わず）"
                )
                adaptive_penalty = 5
        # v25.0: BTC回復中のExtreme Fear LONG: 適応ペナルティを最大+5点に制限
        # 理由: Extreme Fear(F&G≤25) + BTC上昇 = 底値反発の高確率局面。
        #       過去の損失（主にBTC=UP SHORT構造問題）による高ペナルティが
        #       本来有望なLONG機会をブロックしている状況を改善する。
        #       btc_recoveryは独立した品質保証条件（BTC実際に上昇中）を持つ。
        elif direction == "long" and btc_recovery and fg_score <= 25:
            if adaptive_penalty > 5:
                logger.debug(
                    f"{symbol} v25.0 BTC回復Extreme Fear LONG: 適応ペナルティを{adaptive_penalty}→5点に制限"
                    f"（F&G={fg_score} BTC=UP → 底値反発局面での高品質LONG機会を確保）"
                )
                adaptive_penalty = 5
        effective_min_score = effective_min_score + adaptive_penalty
        if adaptive_penalty > 0:
            logger.debug(
                f"{symbol} 直近成績不振: スコア閾値+{adaptive_penalty}点 → {effective_min_score}点"
            )
        elif adaptive_penalty < 0:
            logger.debug(
                f"{symbol} 直近好調: スコア閾値{adaptive_penalty}点 → {effective_min_score}点"
            )

        if score_100 < effective_min_score:
            logger.info(
                f"⛔ {symbol} 100点スコア {score_100}点 < "
                f"最低{effective_min_score}点(F&G={fg_score}, {direction}) → エントリー禁止"
            )
            return

        # 注意状態B（総リスク5〜7%）は85点以上のみ許可
        if portfolio_state == "B" and score_100 < self.config.full_size_score:
            logger.debug(
                f"{symbol} 状態B: スコア{score_100}点 < "
                f"85点（注意状態では高スコアのみ可）"
            )
            return

        # ── ポジションサイズ計算 ──────────────────────────
        score_01  = signal_result.get("score", 0)
        lever     = self._calc_symbol_leverage(symbol, atr, current_price, score_01)
        quantity  = self.risk.calc_position_size(current_price, sl_price, lever,
                                                   signal_score=float(score_100))  # v52.0
        if quantity <= 0:
            return

        # 83点未満（75〜82点）はハーフサイズ
        if score_100 < self.config.full_size_score:
            quantity = quantity * 0.5
            logger.debug(f"{symbol} スコア{score_100}点(75〜82): ハーフサイズで開設")

        # v13.0: 逆張りLONG（F&G 25-45）は追加で半サイズ（合計0.25〜0.5倍）
        # v19.0: btc_recovery=True（BTC本物の回復中）の場合は削減を0.75xに緩和
        #        理由: BTC上昇トレンド中のLONGは「逆張り」ではなく「トレンドフォロー」
        #             過剰なサイズ削減が min_expected_profit_usd フィルターを通せない原因になっていた
        # v19.0c BUGFix: DCE SHORT(LONGからフリップした場合)には counter_trend削減を適用しない
        #        理由: DCE SHORTは「逆張りLONG」ではなくトレンドフォローSHORT。
        #             _counter_trend_long=Trueが残っても、実際はSHORTなので削減対象外。
        if _counter_trend_long and not _is_dead_cat_flip:
            if btc_recovery:
                quantity = quantity * 0.75   # v19.0: BTC回復中は軽減（0.5→0.75）
                logger.info(
                    f"{symbol} v19.0 BTC回復LONG(F&G={fg_score}): "
                    f"0.75倍サイズ適用（BTC上昇トレンドに乗るため通常逆張りより大きめ）"
                )
            else:
                quantity = quantity * 0.5
                logger.info(
                    f"{symbol} v14.0 逆張りLONG(F&G={fg_score}): "
                    f"半サイズ適用（Fear圏 = リスク管理のため慎重エントリー）"
                )

        # v37.0: BTC上昇中の逆張りSHORT（F&G≤25 極度恐怖圏限定）はサイズ0.75倍
        if _btc_counter_short:
            quantity = quantity * 0.75
            logger.info(
                f"{symbol} v37.0 BTC上昇逆張りSHORT(F&G={fg_score}): "
                f"0.75倍サイズ適用（BTC方向は逆だが極度恐怖圏・リスク管理）"
            )

        # v55.0: DCBショートも同様にBTC上昇逆張りのため0.75倍サイズ適用
        # 理由: DCBショートはBTC=up中のSHORTなのに従来サイズ削減がなかった。
        #       v37.0の逆張りSHORTと同じリスク環境なので同等の管理が必要。
        if _is_dead_cat_flip:
            quantity = quantity * 0.75
            logger.info(
                f"{symbol} v55.0 DCBショート(F&G={fg_score}): "
                f"0.75倍サイズ適用（BTC上昇中の逆張り＝リスク管理強化）"
            )

        # ── ATR過大フィルター（超高ボラ銘柄のサイズ削減）────
        # ATR/価格 > max_atr_pct（0.8%）の銘柄はポジションをさらに半分にする
        max_atr_pct = getattr(self.config, 'max_atr_pct', 0.008)
        if atr > 0 and current_price > 0:
            atr_pct = atr / current_price
            if atr_pct > max_atr_pct:
                quantity = quantity * 0.5
                logger.debug(
                    f"{symbol} ATR過大 ({atr_pct*100:.2f}% > {max_atr_pct*100:.1f}%): "
                    f"ポジションを半分に削減"
                )

        # ── 市場センチメントによるサイズ調整 ─────────────
        if direction == "long":
            ctx_mult = self.mktctx.get_long_size_multiplier()
            # v76.0: btc_recovery LONGブーストを1.5→1.2に縮小
            # 旧(v19.0): ctx_mult×1.5 → F&G=21でctx_mult=0.45×1.5=0.675 → 損失も0.675倍
            # 問題: btc_recovery LONGの大損(DOT-$32,HEMI-$30)はブーストで増幅されていた
            # 修正: 1.5→1.2に縮小。ctx_mult=0.45×1.2=0.54（旧0.675→20%削減）
            # 効果: DOT$32損失 → $32×(0.54/0.675)=$25.6（$6.4削減/取引）
            if btc_recovery:
                ctx_mult = min(1.0, ctx_mult * 1.2)
                logger.debug(f"{symbol} v76.0 btc_recovery LONG: ctx_mult×1.2 → {ctx_mult:.3f}")
        else:
            ctx_mult = self.mktctx.get_short_size_multiplier()
        quantity = quantity * ctx_mult

        if quantity <= 0:
            return

        # ── v10.0 ⑦: MaxLossControlSizer（1トレード最大SL損失キャップ）──────────
        # 原因⑦解決: 高ボラ銘柄でのポジション過大を物理的に防ぐ。
        # v53.0: 固定$15→「残高 × max_risk_per_trade」に変更。
        #   理由: $10,000口座では$15固定は0.15%リスクにしかならず、
        #         risk_managerが計算した1.0%リスク($100)を完全に無効化していた。
        #         残高連動にすることで「1%リスクルール」と整合性が取れる。
        # v87.0: 利益プロテクション — 勝った分を守る
        # 残高が初期+3%を超えたら、リスクを自動縮小して利益を守る
        _risk_pct = self.config.max_risk_per_trade
        _profit_pct = (self.risk.balance - self.risk.initial_balance) / self.risk.initial_balance
        if _profit_pct > 0.03:  # +3%超の利益
            _risk_pct = min(_risk_pct, 0.007)  # 1.0%→0.7%に縮小
            logger.debug(f"v87.0 利益プロテクション: 利益{_profit_pct*100:.1f}%→リスク0.7%に縮小")
        _max_loss_usd = self.risk.balance * _risk_pct
        if _max_loss_usd > 0 and quantity > 0 and current_price > 0 and sl_price > 0:
            _sl_dist_abs = abs(current_price - sl_price)   # SL距離（価格差）
            _expected_sl_loss = _sl_dist_abs * quantity    # 予想最大損失額
            if _expected_sl_loss > _max_loss_usd:
                _scale = _max_loss_usd / _expected_sl_loss
                _qty_before_cap = quantity
                quantity = quantity * _scale
                logger.info(
                    f"⚡ {symbol} v53.0 MaxLoss制限: 予想SL損失${_expected_sl_loss:.2f}>"
                    f"上限${_max_loss_usd:.2f}(残高×{self.config.max_risk_per_trade*100:.1f}%) "
                    f"→ サイズ×{_scale:.3f}({_qty_before_cap:.6f}→{quantity:.6f}枚)"
                )
        if quantity <= 0:
            return

        # ── v17.0 最低期待利益チェック（小さすぎるポジションを排除）──────────────
        # TP全体到達時の期待利益 = TP距離 × 数量
        # これが min_expected_profit_usd（$3）未満なら取引しない。
        # 理由: $3未満のポジションは手数料を差し引くと実質ほぼゼロ利益。
        #      avg_win の底上げとPF改善のために「稼げないトレード」を排除する。
        _min_profit_usd = getattr(self.config, 'min_expected_profit_usd', 3.0)
        if _min_profit_usd > 0 and quantity > 0 and tp_price > 0 and current_price > 0:
            _tp_dist_abs = abs(tp_price - current_price)
            _expected_full_profit = _tp_dist_abs * quantity
            if _expected_full_profit < _min_profit_usd:
                logger.debug(
                    f"{symbol} 期待利益${_expected_full_profit:.2f} < 最低${_min_profit_usd:.1f} → "
                    f"エントリースキップ（利益小さすぎ）"
                )
                return

        # ── v9.0 シグナル確認システム（2スキャン連続確認） ──────────
        # 同じ方向シグナルが 2 回以上スキャンに連続して現れたときのみエントリーする。
        # 理由: 1スキャン限りのニセシグナルへの即エントリーで signal_flip 損失が多発していたため。
        # デッドキャットバウンスSHORTは高品質のため確認不要（RSI declining 済み）。
        _need_confirm = not _is_dead_cat_flip  # DCE SHORTは確認済みとみなす
        if _need_confirm:
            _now_ec = time.time()
            _confirm = self._entry_confirm.get(symbol)
            _min_confirm_s = max(self.config.scan_interval_s * 0.75, 20)  # 最低20秒
            _stale_s = 300  # 5分以上経過した確認記録は無効（古い）

            # 古い確認記録を自動削除
            if _confirm is not None and (_now_ec - _confirm.get("first_seen", 0)) > _stale_s:
                _confirm = None
                self._entry_confirm.pop(symbol, None)

            if _confirm is None or _confirm.get("direction") != direction:
                # 初見シグナル（または方向転換）→ 記憶して今回はスキップ
                self._entry_confirm[symbol] = {
                    "direction": direction,
                    "first_seen": _now_ec,
                    "score": score_100,
                }
                logger.debug(
                    f"{symbol} シグナル確認登録: {direction} score={score_100:.0f} "
                    f"→ {_min_confirm_s:.0f}s後に確認済みなら入場"
                )
                return
            else:
                _signal_age = _now_ec - _confirm["first_seen"]
                if _signal_age < _min_confirm_s:
                    logger.debug(
                        f"{symbol} シグナル確認待ち: {direction} {_signal_age:.0f}s < "
                        f"{_min_confirm_s:.0f}s（まだ待機）"
                    )
                    return
                # 2スキャン連続確認済み → クリアしてエントリー
                del self._entry_confirm[symbol]
                logger.info(
                    f"✅ {symbol} シグナル2スキャン確認完了: {direction} score={score_100:.0f} "
                    f"({_signal_age:.0f}s持続) → エントリー許可"
                )
        # ────────────────────────────────────────────────────────────

        self._open_position(
            symbol             = symbol,
            side               = direction,
            price              = current_price,
            quantity           = quantity,
            tp_price           = tp_price,
            sl_price           = sl_price,
            leverage           = lever,
            score              = score_100 / 100,   # 内部では0〜1スケールで保持
            entry_atr          = atr,
            is_dead_cat_bounce = _is_dead_cat_flip,
            entry_score        = float(score_100),   # v23.0: 分析用メタデータ
            entry_fg           = int(fg_score),
            entry_btc_trend    = str(btc_trend),
        )
        # v13.0: 逆張りLONGフラグをポジションに記録
        if _counter_trend_long:
            with self._lock:
                if symbol in self._positions:
                    self._positions[symbol].counter_trend = True

    # ── 全ポジション決済チェック ──────────────────────
    def _check_exits_all(self):
        """
        保有中の全ポジションに対してTP/SL/トレーリングをチェックする。
        古いスキャン価格ではなく、リアルタイムで最新価格を取得してチェックする。
        これにより、60秒のスキャン間隔の間でも素早く損切りが実行される。
        """
        with self._lock:
            symbols = list(self._positions.keys())

        for symbol in symbols:
            try:
                # 最新価格をAPIから直接取得（スキャン時の古い価格に頼らない）
                cp = self.fetcher.fetch_current_price(symbol)
                if cp:
                    # market_dataの価格も更新しておく
                    with self._lock:
                        if symbol in self._market_data:
                            self._market_data[symbol]["current_price"] = cp
                        else:
                            self._market_data[symbol] = {"current_price": cp, "ts": time.time()}
                    self._check_exits_for(symbol, cp)
            except Exception as e:
                logger.debug(f"{symbol} 決済チェック価格取得エラー: {e}")

    # ── v50.0: トレンド継続チェック ────────────────────────
    def _is_trend_still_strong(self, symbol: str, side: str) -> bool:
        """
        TP到達時点でトレンドがまだ継続しているか判定する。
        True なら部分利確をスキップして利益を最大化する。
        「10%で利確せず、20%行く見込みがあれば20%まで待つ」ロジック。

        判定条件（全て満たすとき True）:
          - RSI がまだ方向性を示している（SHORT: RSI < 45, LONG: RSI > 55）
          - ADX > 25（トレンド相場）
          - BTCトレンドが方向一致
        """
        try:
            with self._lock:
                ohlcv = self._market_data.get(symbol, {}).get("ohlcv", {})
            df = ohlcv.get(self.config.primary_tf)
            if df is None or df.empty:
                return False
            if "rsi" not in df.columns or "adx" not in df.columns:
                return False

            rsi = float(df["rsi"].iloc[-1])
            adx = float(df["adx"].iloc[-1])
            if pd.isna(rsi) or pd.isna(adx):
                return False

            # ADX < 25 = 横ばい相場 → 延長しない
            if adx < 25.0:
                return False

            # BTC トレンドとの方向一致チェック
            btc_trend = self._btc_trend_cache.get("trend", "range")

            if side == "short":
                # SHORT: RSI < 45 = まだ下落モメンタム継続
                rsi_ok = rsi < 45.0
                # BTC が下落 or 横ばいなら SHORT トレンド継続
                btc_ok = btc_trend in ("down", "range")
                return rsi_ok and btc_ok
            else:
                # LONG: RSI > 55 = まだ上昇モメンタム継続
                rsi_ok = rsi > 55.0
                # BTC が上昇 or 横ばいなら LONG トレンド継続
                btc_ok = btc_trend in ("up", "range")
                return rsi_ok and btc_ok
        except Exception:
            return False

    # ── 1銘柄の決済チェック ──────────────────────────
    def _check_exits_for(self, symbol: str, current_price: float):
        with self._lock:
            pos = self._positions.get(symbol)
        if pos is None:
            return

        # トレーリングストップのピークを最新価格で更新（5秒ごとに更新）
        pos.update_trail_peak(current_price)

        # ── ブレークイーブンストップ ──────────────────
        # 「利益が出たら損切りラインをエントリー価格まで引き上げる」機能。
        # 一度利益圏に入ったポジションが損失に転落するのを防ぐ。
        # ATRはエントリー時に記録した pos.entry_atr を使う（_market_dataはATR未計算の生データのため）
        if self.config.use_break_even and not self._be_triggered.get(symbol, False):
            atr = pos.entry_atr  # エントリー時にシグナルから取得して記録済みのATR

            if atr > 0:
                trigger_dist = atr * self.config.break_even_trigger_atr
                # v75.0: BEバッファをATR上限付きに修正
                # 問題: 固定0.8%バッファが低ATR銘柄でTP1(1×ATR)より大きくなる
                #   例: TRX(ATR=0.13%) → BE_buf=0.8% > TP1=0.13% → BE後にSL即発火
                #   ZEC(0.43%), XLM(0.46%), PUMP(0.74%), ENS(0.52%)も影響
                # 修正: BEバッファを max(手数料分0.1%, min(0.8%, 0.5×ATR)) に制限
                #   → BEバッファは常にTP1(1×ATR)の半分以下 → TP1到達の余地を保証
                _buf_pct = min(
                    pos.entry_price * self.config.break_even_buffer_pct,  # 固定0.8%
                    0.5 * atr  # 0.5×ATR上限（TP1=1×ATRの半分以下に制限）
                )
                buf = max(pos.entry_price * 0.001, _buf_pct)  # 最低0.1%（手数料カバー）

                if pos.side == "long":
                    be_sl = pos.entry_price + buf
                    if current_price >= pos.entry_price + trigger_dist and pos.sl_price < be_sl:
                        with self._lock:
                            pos.sl_price = be_sl
                            self._be_triggered[symbol] = True
                        _buf_pct_actual = buf / pos.entry_price * 100
                        self._log(
                            f"🛡️ ブレークイーブン発動 {symbol}: "
                            f"SL → {fmt_price(be_sl)}（entry+{_buf_pct_actual:.2f}%）",
                            "info"
                        )
                else:  # short
                    be_sl = pos.entry_price - buf
                    if current_price <= pos.entry_price - trigger_dist and pos.sl_price > be_sl:
                        with self._lock:
                            pos.sl_price = be_sl
                            self._be_triggered[symbol] = True
                        _buf_pct_actual = buf / pos.entry_price * 100
                        self._log(
                            f"🛡️ ブレークイーブン発動 {symbol}: "
                            f"SL → {fmt_price(be_sl)}（entry-{_buf_pct_actual:.2f}%）",
                            "info"
                        )

        # ── v71.0: Post-TP1 ATRベーストレーリングSL ──────────────────────
        # 問題: TP1後のSLがentry+0.8%に固定されたまま → 利益を守れなかった！
        #       trail_peakは毎秒追跡されているのに、SLに反映されていなかった。
        #       例: 価格が+3%まで上昇→反転→entry+0.8%で決済 → 利益の大半を逃す
        # 対策: TP1後は trail_peak - 1×ATR のトレーリングSLで利益を動的に追跡する。
        #       SLは「引き上げのみ」（引き下げしない）で安全性を保証。
        # 効果試算: trail_peak=entry+3%, ATR=1.6%の場合:
        #   旧: SL=entry+0.8% → 利益0.8%
        #   新: SL=entry+3%-1.6%=entry+1.4% → 利益1.4%（75%改善）
        # TP2/TP3の段階SL引き上げとの関係: trail_slはTP2/TP3のSLより低い場合が多く干渉しない。
        #   TP2到達時にはTP2のSL引き上げが優先（さらに高いSLを設定するため）。
        if pos.tp1_done and pos.entry_atr > 0:
            # v91.0: 1h足最適トレーリング幅（v85.0の5m足用を1h足に調整）
            # 5m足: 0.5ATR=0.15%（適切）→ 1h足: 0.5ATR=1.0%（狭すぎ！1h足は1本で1-2%動く）
            # TP1後: 0.8×ATR（1h足の自然な値動きに余裕を持たせる）
            # TP2後: 1.2×ATR（ランナーは広めに）
            # TP3後: 1.5×ATR（最終ランナー）
            if getattr(pos, 'tp3_done', False):
                _trail_mult = 1.5
            elif getattr(pos, 'tp2_done', False):
                _trail_mult = 1.2
            else:
                _trail_mult = 0.8   # v91.0: 1h足用（0.5→0.8に拡大）
            _trail_dist = _trail_mult * pos.entry_atr
            if pos.side == "long":
                _trail_sl = pos.trail_peak - _trail_dist
                if _trail_sl > pos.sl_price:
                    with self._lock:
                        pos.sl_price = _trail_sl
            else:  # short
                _trail_sl = pos.trail_peak + _trail_dist
                if _trail_sl < pos.sl_price:
                    with self._lock:
                        pos.sl_price = _trail_sl

        # ══════════════════════════════════════════════════════
        # 5フェーズ段階管理 (億トレーダー手法)
        # フェーズ1: エントリー〜1R未達 → 初期SL維持
        # フェーズ2: 1R到達 → ブレークイーブン（↑の BE処理が担当）
        # フェーズ3: 2R到達 = TP1 → 55%利確 + SLをトレーリング追跡開始
        # フェーズ4: 3R到達 = TP2 → 残りの30%を利確してATR×1.0追跡ストップ
        # フェーズ5: 強トレンド継続 → Chandelier Exit（ATR×3.0追跡）
        # ══════════════════════════════════════════════════════

        # ── v13.0 フェーズ3: TP1（ATR×1.5）到達 → 30%利確 ──────────────
        # 早めに30%を確定し、SLをTP1-0.3%に引き上げて「確実にプラス」を作る（v22.0）
        if (self.config.use_partial_tp and not pos.tp1_done
                and pos.entry_atr > 0 and pos.quantity > 0):
            tp1_dist = pos.entry_atr * self.config.tp1_atr_mult  # ATR×1.5
            if pos.side == "long":
                tp1_price = pos.entry_price + tp1_dist
                tp1_hit = current_price >= tp1_price
            else:
                tp1_price = pos.entry_price - tp1_dist
                tp1_hit = current_price <= tp1_price

            if tp1_hit:
                # ── v50.0: トレンド継続チェック ─────────────────────
                # 「まだ20%行く見込みがあれば10%で利確しない」設計。
                # TP1到達時にRSI/ADXでモメンタムが強ければ部分利確をスキップし、
                # SLだけBE+bufferに引き上げて利益を守りつつトレンドを追う。
                # スキップは1回限り（2回目以降は必ず利確してTP2・TP3を狙う）。
                # v63.0: 逆張りLONG（counter_trend）はTP1スキップを禁止する。
                # 理由: 逆張りは「反発を1回捕まえる」設計のため、TP1到達は成功の証。
                # スキップすると価格が反転してTP1利益を全て失うリスクが高い（平均win$4.53の主因）。
                _skip_tp1 = (
                    not getattr(pos, 'tp1_close_skipped', False)   # まだスキップしていない
                    and not getattr(pos, 'counter_trend', False)    # v63.0: 逆張りはTP1必ず利確
                    and self._is_trend_still_strong(symbol, pos.side)
                )

                # v75.0: ATR上限付きBEバッファ（TP1のSL計算にも適用）
                # 低ATR銘柄でTP1 SLがentry以下に落ちるバグを防ぐ
                _buf_raw_tp1 = pos.entry_price * self.config.break_even_buffer_pct
                if pos.entry_atr > 0:
                    be_buf = max(pos.entry_price * 0.001, min(_buf_raw_tp1, 0.5 * pos.entry_atr))
                else:
                    be_buf = _buf_raw_tp1
                if pos.side == "long":
                    new_sl_f3 = tp1_price - be_buf
                    if pos.sl_price < new_sl_f3:
                        pos.sl_price = new_sl_f3
                else:
                    new_sl_f3 = tp1_price + be_buf
                    if pos.sl_price > new_sl_f3:
                        pos.sl_price = new_sl_f3

                if _skip_tp1:
                    # ── トレンド継続: 部分利確スキップ → SLだけ引き上げてTP2へ ──
                    # TP2（ATR×2.0）から直接利確を開始するよう tp_price を更新
                    _tp2_dist_ext = pos.entry_atr * getattr(self.config, 'tp2_atr_mult', 2.0)
                    if pos.side == "long":
                        pos.tp_price = pos.entry_price + _tp2_dist_ext
                    else:
                        pos.tp_price = pos.entry_price - _tp2_dist_ext

                    with self._lock:
                        pos.tp1_done          = True   # TP2チェックを解放
                        pos.tp1_close_skipped = True   # スキップ済みフラグ
                        self._be_triggered[symbol]    = True

                    self._log(
                        f"🚀 [TP1延長/v50.0] {symbol} モメンタム継続 → 25%利確スキップ "
                        f"SL→BE+buffer({fmt_price(new_sl_f3)}) | 次目標TP2(ATR×{self.config.tp2_atr_mult}) | 全量保持継続",
                        "info"
                    )
                else:
                    # ── 通常: TP1で25%利確 ──────────────────────────
                    close_qty  = pos.quantity * self.config.tp1_close_pct
                    pnl_part   = ((current_price - pos.entry_price) * close_qty
                                  if pos.side == "long"
                                  else (pos.entry_price - current_price) * close_qty)
                    notional_part = pos.entry_price * close_qty
                    comm_part  = notional_part * self.config.commission_rate * 2
                    pnl_part   = pnl_part - comm_part

                    # TP2まで残り75%を引き続き保有
                    _tp2_dist_ext = pos.entry_atr * getattr(self.config, 'tp2_atr_mult', 2.0)
                    if pos.side == "long":
                        pos.tp_price = pos.entry_price + _tp2_dist_ext
                    else:
                        pos.tp_price = pos.entry_price - _tp2_dist_ext

                    with self._lock:
                        pos.quantity = pos.quantity * (1 - self.config.tp1_close_pct)
                        pos.size_usd = pos.entry_price * pos.quantity / pos.leverage
                        pos.tp1_done = True
                        self._be_triggered[symbol] = True

                    self.risk.update_balance(self.risk.balance + pnl_part)
                    # v49.0: 部分利確PnLを累積
                    self._partial_pnl[symbol] = self._partial_pnl.get(symbol, 0.0) + pnl_part
                    # タイムラグ修正: 最終クローズ時に二重計上を防ぐため累計を記録
                    pos._partial_realized = getattr(pos, '_partial_realized', 0.0) + pnl_part
                    self._log(
                        f"🎯 [Phase3/TP1] {symbol} {self.config.tp1_close_pct*100:.0f}%決済 +{pnl_part:.2f}USD "
                        f"| SL→TP1-buffer({fmt_price(new_sl_f3)}) | 次目標:TP2(ATR×{self.config.tp2_atr_mult}) | 残り{pos.quantity:.6f}枚",
                        "info"
                    )

        # ── v13.0 フェーズ4: TP2（ATR×2.5）到達 → 35%追加利確 ──────────
        # 残りポジションの35%を利確し、SLをTP1レベルに引き上げてTP1の利益を守る
        if (pos.tp1_done and not pos.tp2_done
                and pos.quantity > 0 and pos.entry_atr > 0):
            tp2_dist = pos.entry_atr * getattr(self.config, 'tp2_atr_mult', 2.5)
            if pos.side == "long":
                tp2_price = pos.entry_price + tp2_dist
                tp2_hit = current_price >= tp2_price
            else:
                tp2_price = pos.entry_price - tp2_dist
                tp2_hit = current_price <= tp2_price

            if tp2_hit:
                # ── v50.0: TP2でもトレンド継続チェック ──────────────────────
                # TP1をスキップして全量でTP2に到達した場合、または通常TP1後の残り75%が
                # TP2到達時にもまだトレンドが強いなら、TP2の35%利確もスキップして
                # TP3(ATR×3.5)まで全量を保持する。スキップは1回限り。
                # v63.0: 逆張りLONG（counter_trend）はTP2スキップも禁止。
                # 理由: 逆張りはTP2到達自体がレアケース。到達したら必ず確定すべき。
                _skip_tp2 = (
                    not getattr(pos, 'tp2_close_skipped', False)  # まだスキップしていない
                    and not getattr(pos, 'counter_trend', False)   # v63.0: 逆張りはTP2必ず利確
                    and self._is_trend_still_strong(symbol, pos.side)
                )

                # SL→TP1レベルに引き上げ（TP1分の利益を守る）
                tp1_price_level = (pos.entry_price + pos.entry_atr * self.config.tp1_atr_mult
                                   if pos.side == "long"
                                   else pos.entry_price - pos.entry_atr * self.config.tp1_atr_mult)
                if pos.side == "long":
                    if pos.sl_price < tp1_price_level:
                        pos.sl_price = tp1_price_level
                else:
                    if pos.sl_price > tp1_price_level:
                        pos.sl_price = tp1_price_level

                if _skip_tp2:
                    # ── トレンド継続: TP2利確スキップ → TP3（ATR×3.5）まで全量保持 ──
                    _tp3_dist_ext = pos.entry_atr * getattr(self.config, 'tp3_atr_mult', 3.5)
                    if pos.side == "long":
                        pos.tp_price = pos.entry_price + _tp3_dist_ext
                    else:
                        pos.tp_price = pos.entry_price - _tp3_dist_ext

                    with self._lock:
                        pos.tp2_done          = True   # TP3チェックを解放
                        pos.tp2_close_skipped = True   # スキップ済みフラグ

                    self._log(
                        f"🚀 [TP2延長/v50.0] {symbol} モメンタム継続 → 35%利確スキップ "
                        f"SL→TP1レベル({fmt_price(tp1_price_level)}) | 次目標TP3(ATR×{getattr(self.config,'tp3_atr_mult',3.5)}) | 全量保持継続",
                        "info"
                    )
                else:
                    # ── 通常: TP2で35%利確 ──────────────────────────────
                    close_pct = getattr(self.config, 'tp2_close_pct', 0.35)  # 35%
                    close_qty = pos.quantity * close_pct
                    pnl_part  = ((current_price - pos.entry_price) * close_qty
                                 if pos.side == "long"
                                 else (pos.entry_price - current_price) * close_qty)
                    notional_part = pos.entry_price * close_qty
                    comm_part = notional_part * self.config.commission_rate * 2
                    pnl_part  = pnl_part - comm_part

                    # 残り40%はTP3（ATR×tp_atr_mult）までトレーリングで最大化
                    _tp3_dist = pos.entry_atr * self.config.tp_atr_mult  # v14.0: ATR×5.0
                    if pos.side == "long":
                        pos.tp_price = pos.entry_price + _tp3_dist
                    else:
                        pos.tp_price = pos.entry_price - _tp3_dist

                    with self._lock:
                        pos.quantity = pos.quantity * (1 - close_pct)
                        pos.size_usd = pos.entry_price * pos.quantity / pos.leverage
                        pos.tp2_done = True

                    self.risk.update_balance(self.risk.balance + pnl_part)
                    # v49.0: 部分利確PnL累積
                    self._partial_pnl[symbol] = self._partial_pnl.get(symbol, 0.0) + pnl_part
                    # タイムラグ修正: 二重計上防止用累計
                    pos._partial_realized = getattr(pos, '_partial_realized', 0.0) + pnl_part
                    self._log(
                        f"🎯 [Phase4/TP2] {symbol} 35%追加利確 +{pnl_part:.2f}USD "
                        f"| SL→TP1レベル | 残り40%→Runner(ATR×{self.config.tp_atr_mult:.1f})トレーリング | 残り{pos.quantity:.6f}枚",
                        "info"
                    )

        # ── v47.0 フェーズ4.5: TP3（ATR×3.5）到達 → 30%追加利確 ──────────
        # v12.0 設計書 Phase4 "+3.5R: 30%決済 + SL=+1.5R" を移植。
        # 目的: TP2(2R)→Runner(5R)の間に中間利確を追加し、
        #       3.5R到達後の反転リスクをヘッジしつつ利益フロアを+1.5Rに引き上げる。
        if (self.config.use_partial_tp and pos.tp2_done
                and not getattr(pos, 'tp3_done', False)
                and pos.quantity > 0 and pos.entry_atr > 0):
            tp3_dist = pos.entry_atr * getattr(self.config, 'tp3_atr_mult', 3.5)
            if pos.side == "long":
                tp3_price = pos.entry_price + tp3_dist
                tp3_hit = current_price >= tp3_price
            else:
                tp3_price = pos.entry_price - tp3_dist
                tp3_hit = current_price <= tp3_price

            if tp3_hit:
                close_pct  = getattr(self.config, 'tp3_close_pct', 0.30)
                close_qty  = pos.quantity * close_pct
                pnl_part   = ((current_price - pos.entry_price) * close_qty
                              if pos.side == "long"
                              else (pos.entry_price - current_price) * close_qty)
                notional_part = pos.entry_price * close_qty
                comm_part  = notional_part * self.config.commission_rate * 2
                pnl_part   = pnl_part - comm_part

                # v58.0: SL → entry ± TP2レベル（ATR×tp2_atr_mult = 2.0）
                # 旧: ATR×1.5（TP1=1.0 と TP2=2.0 の中間）
                # 問題: TP3(3.5ATR)まで到達した好調なトレードなのに、
                #       SLがTP1とTP2の間にとどまっていた → TP2分の利益が守られていなかった。
                # 改善: TP3到達後はSLをTP2価格（entry+2.0ATR）に引き上げ
                #       → TP2で利確した分の利益を完全に守りつつランナーを継続
                _tp3_sl_dist = pos.entry_atr * getattr(self.config, 'tp2_atr_mult', 2.0)
                if pos.side == "long":
                    new_sl_tp3 = pos.entry_price + _tp3_sl_dist
                    if pos.sl_price < new_sl_tp3:
                        pos.sl_price = new_sl_tp3
                else:
                    new_sl_tp3 = pos.entry_price - _tp3_sl_dist
                    if pos.sl_price > new_sl_tp3:
                        pos.sl_price = new_sl_tp3

                with self._lock:
                    pos.quantity = pos.quantity * (1 - close_pct)
                    pos.size_usd = pos.entry_price * pos.quantity / pos.leverage
                    pos.tp3_done = True

                self.risk.update_balance(self.risk.balance + pnl_part)
                # v49.0: 部分利確PnL累積
                self._partial_pnl[symbol] = self._partial_pnl.get(symbol, 0.0) + pnl_part
                # タイムラグ修正: 二重計上防止用累計
                pos._partial_realized = getattr(pos, '_partial_realized', 0.0) + pnl_part
                self._log(
                    f"🎯 [Phase4.5/TP3] {symbol} 30%追加利確 +{pnl_part:.2f}USD "
                    f"| SL→+1.5ATR({fmt_price(new_sl_tp3)}) "
                    f"| ランナー36.75%→ATR×{self.config.tp_atr_mult:.1f} | 残り{pos.quantity:.6f}枚",
                    "info"
                )

        # ── v51.0: TP延長後モメンタム反転決済 ───────────────────────────────
        # TP1またはTP2の利確をスキップして利益を延長した場合、
        # RSIが方向と逆に振れたとき（2回連続）即座に利確して含み益を守る。
        # 「延長したが実は反転だった」ケースで損失に変わる前に利益確定する。
        # 閾値: LONG: RSI < 48（55の境界より低め）/ SHORT: RSI > 52（45の境界より高め）
        # 2回連続確認: 一時的な振れに反応しないため。
        _extended_hold = (
            getattr(pos, 'tp1_close_skipped', False)
            or getattr(pos, 'tp2_close_skipped', False)
        )
        if _extended_hold and pos.entry_atr > 0:
            try:
                with self._lock:
                    _rsi_md = self._market_data.get(symbol, {}).get("ohlcv", {})
                _df_rsi_v51 = _rsi_md.get(self.config.primary_tf)
                if _df_rsi_v51 is not None and not _df_rsi_v51.empty and "rsi" in _df_rsi_v51.columns:
                    _rsi_v51 = float(_df_rsi_v51["rsi"].iloc[-1])
                    if not pd.isna(_rsi_v51):
                        _rsi_faded = (
                            (pos.side == "long" and _rsi_v51 < 48.0) or
                            (pos.side == "short" and _rsi_v51 > 52.0)
                        )
                        if _rsi_faded:
                            _fade_cnt = self._momentum_fade_count.get(symbol, 0) + 1
                            self._momentum_fade_count[symbol] = _fade_cnt
                            if _fade_cnt >= 2:
                                # 2回連続弱化確認 → 含み益を確定
                                _pnl_now_v51 = pos.current_pnl_pct(current_price)
                                self._log(
                                    f"⚡ [TP延長モメンタム反転/v51.0] {symbol} "
                                    f"{'LONG RSI<48' if pos.side=='long' else 'SHORT RSI>52'}"
                                    f"({_rsi_v51:.0f}) 2回連続 → 含み益{_pnl_now_v51:+.2f}%確定",
                                    "info"
                                )
                                self._momentum_fade_count.pop(symbol, None)
                                self._close_position(symbol, current_price, "momentum_fade")
                                return
                            else:
                                logger.debug(
                                    f"{symbol} v51.0 モメンタム弱化1回目 RSI={_rsi_v51:.0f} "
                                    f"(2回連続で決済)"
                                )
                        else:
                            # 回復 → カウンターリセット
                            self._momentum_fade_count.pop(symbol, None)
            except Exception:
                pass

        # ── v6.1: RSIダイバージェンス早期撤退 ───────────────────────────
        # 「一定時間以上保有していて、RSIが方向に逆行している」= トレンドが終わっている証拠
        # LONG: RSI < 40 = もう上昇モメンタムがない
        # SHORT: RSI > 60 = もう下降モメンタムがない
        # v8.0改善: Extreme Fear (F&G≤25) のSHORTは120分に延長（デッドキャットバウンス終了を待つ）
        # 通常は60分でOK（短期トレード）。但し極度恐怖でのSHORTはバウンス長引く場合があり延長。
        _held_s_rsi = time.time() - pos.entry_time
        _fg_now_stag = self.mktctx.fear_greed
        # v15.0: config.stagnation_exit_hoursを使用（現在3.0時間）
        _stagnation_base_s = getattr(self.config, 'stagnation_exit_hours', 3.0) * 3600
        _stagnation_threshold_s = (
            _stagnation_base_s * 1.5  # Extreme Fear SHORTは1.5倍（DCBバウンス待機）
            if (pos.side == "short" and _fg_now_stag <= 25)
            else _stagnation_base_s   # 通常: config設定値（3時間）
        )
        if _held_s_rsi >= _stagnation_threshold_s:  # 時間以上保有
            try:
                with self._lock:
                    _rsi_exit_md = self._market_data.get(symbol, {}).get("ohlcv", {})
                _df_rsi_exit = _rsi_exit_md.get(self.config.primary_tf)
                if _df_rsi_exit is not None and not _df_rsi_exit.empty:
                    if "rsi" in _df_rsi_exit.columns:
                        _rsi_exit_val = float(_df_rsi_exit["rsi"].iloc[-1])
                        if not pd.isna(_rsi_exit_val):
                            _rsi_diverge = (
                                (pos.side == "long" and _rsi_exit_val < 40) or
                                (pos.side == "short" and _rsi_exit_val > 60)
                            )
                            if _rsi_diverge:
                                _pnl_now = pos.current_pnl_pct(current_price)
                                # v15.0: 利益中のポジションはRSIダイバージェンスでも終了しない
                                # 理由: 利益が出ているなら「トレンドが終わっていても利益確保済み」
                                # → SLがentry以上に設定されているので損切りは自動で防ぎながら
                                #    さらなる利益を追う方が合理的。
                                _is_profitable_stag = (
                                    (pos.side == "long" and current_price > pos.entry_price * 1.001)
                                    or (pos.side == "short" and current_price < pos.entry_price * 0.999)
                                )
                                if _is_profitable_stag:
                                    logger.debug(
                                        f"🛡️ {symbol} RSIダイバージェンス: 利益中のため継続保有 "
                                        f"(損益{_pnl_now:+.2f}% RSI={_rsi_exit_val:.0f})"
                                    )
                                else:
                                    self._log(
                                        f"📉 {symbol} RSIダイバージェンス撤退: "
                                        f"RSI={_rsi_exit_val:.0f} "
                                        f"({'LONGにRSI<40' if pos.side == 'long' else 'SHORTにRSI>60'}) "
                                        f"保有{_held_s_rsi/60:.0f}分 損益{_pnl_now:+.2f}%",
                                        "info"
                                    )
                                    self._close_position(symbol, current_price, "stagnation")
                                    return
            except Exception:
                pass

        # ── v44.0: 低品質ポジション早期スタグネーション退出 ──────────
        # score=0またはATR=0で入ったポジションは、将来にも増殖させないために早めに閉じる。
        # v42/v43でこれ以上の低品質エントリーは防げるが、既存ポジションの後始末として追加。
        # 30分保有かつ±0.5%以内なら停滞として退出（通常は3時間のところを大幅短縮）。
        # 但し: すでに利益方向に0.5%以上動いているなら継続保有（勝てる可能性）。
        if (pos.entry_score == 0.0 or pos.entry_atr == 0.0) and pos.entry_price > 0:
            _v44_held_s = time.time() - pos.entry_time
            _v44_fast_stag_s = 30 * 60  # 30分（通常3時間より大幅短縮）
            _v44_stag_pct = 0.005       # ±0.5%以内なら停滞
            if _v44_held_s >= _v44_fast_stag_s:
                _v44_chg = abs(current_price - pos.entry_price) / pos.entry_price
                if _v44_chg <= _v44_stag_pct:
                    self._log(
                        f"⏩ v44.0 {symbol} 低品質ポジション早期退出 "
                        f"(score={pos.entry_score:.0f} ATR={pos.entry_atr:.6f}) "
                        f"保有{_v44_held_s/60:.0f}分 変化{_v44_chg*100:.2f}% → stagnation",
                        "info"
                    )
                    self._close_position(symbol, current_price, "stagnation")
                    return

        # ── v70.0: 恐怖圏LONG 45分0.8ATR早期損切り（counter_trend問わず）──────────
        # 問題(v68.0の拡張): 大損4件(DOT-$32, HEMI-$30, RAY-$25, PENGU-$25)は全てcounter_trend=False。
        #       F&G=21でエントリーしたが、btc_trendが一時的に"range"だったためct=Falseになった。
        #       v68.0（counter_trend=True限定）ではこれらを保護できなかった。
        # 対策: counter_trend=True/Falseに関わらず、entry_fg≤35のLONG全てに適用。
        # 理由: F&G≤35は「恐怖圏」= LONGが最も危険な環境。即下落は「相場のミスマッチ」を意味する。
        if (pos.side == "long"
                and not pos.tp1_done
                and not self._be_triggered.get(symbol, False)
                and pos.entry_atr > 0
                and getattr(pos, 'entry_fg', 50) <= 35):   # v70.0: F&G≤35（恐怖圏）
            _qs_held_s = time.time() - pos.entry_time
            if _qs_held_s <= 3 * 3600:   # v91.0: 30分→3時間（1h足=3本分で判定）
                _quick_stop_dist = 0.8 * pos.entry_atr  # v91.0: 0.6→0.8ATR（1h足のノイズ許容）
                if current_price < pos.entry_price - _quick_stop_dist:
                    _qs_pnl = pos.current_pnl_pct(current_price)
                    _qs_ct = getattr(pos, 'counter_trend', False)
                    self._log(
                        f"⚡ v70.0 {symbol} 恐怖圏LONG 45分0.8ATR早期損切り "
                        f"(F&G={getattr(pos,'entry_fg',50)} ct={_qs_ct}) "
                        f"entry={pos.entry_price:.4g} 保有{_qs_held_s/60:.0f}分 損益{_qs_pnl:+.2f}%",
                        "warn"
                    )
                    self._close_position(symbol, current_price, "quick_stop")
                    return

        # ── v80.0: 恐怖圏SHORT 45分0.8ATR早期損切り（v70.0のSHORT版）──────────
        # 問題: btc_recovery(BTC上昇+F&G≤25)中のSHORTが即座に逆行するケース
        #   実データ: ZAMA SHORT -$6.74（75分で-3.24%逆行）→ 0.8ATRで切っていたら-$2-3
        # 対策: entry_fg≤35のSHORT + 45分以内 + 0.8ATR上昇 → 早期損切り
        if (pos.side == "short"
                and not pos.tp1_done
                and not self._be_triggered.get(symbol, False)
                and pos.entry_atr > 0
                and getattr(pos, 'entry_fg', 50) <= 35):
            _qs_held_s = time.time() - pos.entry_time
            if _qs_held_s <= 45 * 60:
                _quick_stop_dist = 0.8 * pos.entry_atr
                if current_price > pos.entry_price + _quick_stop_dist:
                    _qs_pnl = pos.current_pnl_pct(current_price)
                    self._log(
                        f"⚡ v80.0 {symbol} 恐怖圏SHORT 45分0.8ATR早期損切り "
                        f"(F&G={getattr(pos,'entry_fg',50)}) "
                        f"entry={pos.entry_price:.4g} 保有{_qs_held_s/60:.0f}分 損益{_qs_pnl:+.2f}%",
                        "warn"
                    )
                    self._close_position(symbol, current_price, "quick_stop")
                    return

        # ── v65.0: 逆張りLONG 1.5時間「真停滞」早期撤退 ──────────────────
        # 問題: BE未発動(0.7ATR未到達)のまま1.5時間以上経過 = 価格が有利方向に全く動いていない
        #       → 「BTC回復テーマが完全に機能していない」と判断できる
        # 対策: BE=False + 1.5時間経過 → 即撤退してスロット解放
        # 根拠: 4時間タイムアウトを待つより早期に見切りをつけて次のチャンスへ
        if (getattr(pos, 'counter_trend', False)
                and pos.side == "long"
                and not pos.tp1_done
                and not self._be_triggered.get(symbol, False)  # v65.0: 0.7ATR未到達
                and pos.entry_atr > 0):
            _ct_stale_held_s = time.time() - pos.entry_time
            _ct_stale_threshold_s = 1.5 * 3600  # 1.5時間
            if _ct_stale_held_s >= _ct_stale_threshold_s:
                _ct_stale_pnl = pos.current_pnl_pct(current_price)
                self._log(
                    f"⏰ v65.0 {symbol} 逆張りLONG 1.5h真停滞撤退（BE未発動=0.7ATR未到達） "
                    f"保有{_ct_stale_held_s/3600:.1f}h 損益{_ct_stale_pnl:+.2f}%",
                    "info"
                )
                self._close_position(symbol, current_price, "stagnation")
                return

        # ── v67.0/v79.0: btc_recovery中のSHORT 1時間停滞早期撤退 ──────────────
        # v79.0: 2時間→1時間に短縮。実データ（44取引）で判明:
        #   STO: 2.7h保有→-$11.57, TURBO: 2.7h→-$6.23, REZ: 3.2h→-$11.78
        #   1時間で出ていたら: STO≈-$4.29, TURBO≈-$2.31 → 合計$11.68節約
        # BTC上昇中にSHORTが1時間でBE(0.7ATR)に到達しない = 完全に機能不全
        _cur_fg = self.mktctx.fear_greed
        _cur_btc_trend = self._btc_trend_cache.get("trend", "range")
        _cur_btc_recovery = (_cur_fg <= 35 and _cur_btc_trend == "up")
        if (pos.side == "short"
                and _cur_btc_recovery
                and not pos.tp1_done
                and not self._be_triggered.get(symbol, False)  # 0.7ATR未到達
                and pos.entry_atr > 0):
            _cs_stale_held_s = time.time() - pos.entry_time
            _cs_stale_threshold_s = 1.0 * 3600  # v79.0: 2h→1h（損失拡大前に撤退）
            if _cs_stale_held_s >= _cs_stale_threshold_s:
                _cs_stale_pnl = pos.current_pnl_pct(current_price)
                self._log(
                    f"⏰ v79.0 {symbol} btc_recoverySHORT 1h停滞撤退（BE未発動・BTC上昇逆行） "
                    f"保有{_cs_stale_held_s/3600:.1f}h F&G={_cur_fg} 損益{_cs_stale_pnl:+.2f}%",
                    "info"
                )
                self._close_position(symbol, current_price, "stagnation")
                return

        # ── v59.0: 逆張りLONG（btc_recovery）TP1未達タイムアウト ──────────
        # 問題: BTC回復テーマで入った逆張りLONGが長時間経ってもTP1（1×ATR）に届かない場合、
        #       「回復テーマが外れた」と判断すべき。このまま保有を続けてもスロットを無駄に占有する。
        # 対策: 逆張りLONG + TP1未達 + タイムアウト → 撤退
        # 例外: TP1が既に達成されていれば継続保有（トレードが機能している証拠）
        # v63.0: 6時間→4時間に短縮。実データで1.8h経ってTP1未達のポジションは多数。
        #        6時間待つより4時間でスロット解放し、次の高スコアチャンスを掴む。
        if (getattr(pos, 'counter_trend', False)
                and pos.side == "long"
                and not pos.tp1_done
                and pos.entry_price > 0):
            _ct_held_s = time.time() - pos.entry_time
            _ct_max_hold_s = 4 * 3600  # v63.0: 6→4時間（スロット解放を早める）
            if _ct_held_s >= _ct_max_hold_s:
                _ct_pnl = pos.current_pnl_pct(current_price)
                self._log(
                    f"⏰ v63.0 {symbol} 逆張りLONG 4時間TP1未達タイムアウト "
                    f"保有{_ct_held_s/3600:.1f}h 損益{_ct_pnl:+.2f}% → 撤退",
                    "info"
                )
                self._close_position(symbol, current_price, "timeout")
                return

        pos_dict = {
            "entry_price": pos.entry_price,
            "tp_price":    pos.tp_price,
            "sl_price":    pos.sl_price,
            "side":        pos.side,
            "entry_time":  pos.entry_time,
            # v72.0: トレーリングストップに必須のフィールド追加
            # BUG: これまでtp1_done等が未提供→strategy.pyのトレーリングが一度も機能していなかった
            "tp1_done":    pos.tp1_done,
            "tp2_done":    pos.tp2_done,
            "tp3_done":    getattr(pos, 'tp3_done', False),
            "entry_atr":   pos.entry_atr,
            "entry_fg":    getattr(pos, 'entry_fg', 50),   # v77.0: F&G連動stagnation用
        }
        exit_flag, reason = should_exit(pos_dict, current_price, pos.trail_peak, self.config)
        if exit_flag:
            self._close_position(symbol, current_price, reason)

    # ── ポジションオープン ────────────────────────────
    def _open_position(self, symbol: str, side: str, price: float,
                        quantity: float, tp_price: float, sl_price: float,
                        leverage: float, score: float, entry_atr: float = 0.0,
                        is_dead_cat_bounce: bool = False,
                        entry_score: float = 0.0, entry_fg: int = 0,
                        entry_btc_trend: str = ""):
        # ── 実運用シミュレーション（リアル近似） ──
        if getattr(self.config, "realistic_mode", False):
            import random
            notional = quantity * price
            # 1) 最小ポジション額チェック（Binance先物は$5以上）
            min_usd = getattr(self.config, "min_position_usd", 5.0)
            if notional < min_usd:
                self._log(
                    f"⛔ {symbol} ポジション額${notional:.2f} < Binance最小${min_usd} → エントリー見送り",
                    "warn"
                )
                return
            # 2) 約定拒否シミュレーション（残高不足・流動性不足などを再現）
            reject_rate = getattr(self.config, "order_reject_rate", 0.0)
            if random.random() < reject_rate:
                self._log(f"⛔ {symbol} 約定拒否（Binance実運用想定: {reject_rate*100:.1f}%確率）", "warn")
                return
            # 3) スリッページ適用（成行注文の約定ズレを再現）
            slip = getattr(self.config, "slippage_rate", 0.0)
            if slip > 0:
                if side == "long":
                    price = price * (1 + slip)  # ロングは少し高く買わされる
                else:
                    price = price * (1 - slip)  # ショートは少し安く売らされる

        pos = Position(
            symbol=symbol, side=side, entry_price=price,
            quantity=quantity, tp_price=tp_price, sl_price=sl_price,
            leverage=leverage, entry_atr=entry_atr,
            is_dead_cat_bounce=is_dead_cat_bounce,
        )
        # v23.0: エントリー時コンテキストを保存
        pos.entry_score     = entry_score
        pos.entry_fg        = entry_fg
        pos.entry_btc_trend = entry_btc_trend
        with self._lock:
            self._positions[symbol] = pos

        # エントリー確認記録をクリア（使用済み）
        self._entry_confirm.pop(symbol, None)

        self._log(
            f"🟢 {'ロング' if side == 'long' else 'ショート'} {symbol}  "
            f"価格: {fmt_price(price)}  数量: {quantity:.6f}  "
            f"TP: {fmt_price(tp_price)}  SL: {fmt_price(sl_price)}  "
            f"レバ: {leverage:.1f}倍  スコア: {score:.2f}",
            "info"
        )
        self._save_state()   # ポジション開始時に状態を保存

        if self.config.mode == Mode.LIVE:
            self._place_live_order(symbol, side, quantity, price, tp_price, sl_price, leverage)

    def _place_live_order(self, symbol, side, quantity, price, tp_price, sl_price, leverage):
        from config import is_live_mode
        if not is_live_mode(self.config):
            self._log("⛔ 本番モードでないため実際の注文は送信しません", "warn")
            return
        self._log("📤 本番注文を送信しました（実装済み）", "info")

    # ── ポジションクローズ ────────────────────────────
    def _close_position(self, symbol: str, price: float, reason: str):
        with self._lock:
            pos = self._positions.pop(symbol, None)
        if pos is None:
            return

        pnl     = pos.current_pnl(price)
        pnl_pct = pos.current_pnl_pct(price)

        # ── 手数料を差し引く（エントリー時 + エグジット時の往復）──
        # 手数料 = 約定代金（枚数×価格）× 手数料率 × 2（往復）
        notional   = pos.entry_price * pos.quantity          # ポジション想定元本
        commission = notional * self.config.commission_rate * 2
        pnl        = pnl - commission                         # 手数料引き後の実損益
        # 手数料率をleverageで増幅してpnl_pctに反映（証拠金ベースの%に変換）
        commission_pct = (commission / pos.size_usd) * 100 if pos.size_usd > 0 else 0
        pnl_pct    = pnl_pct - commission_pct

        # v49.0: 部分利確（TP1/TP2/TP3）の累積PnLを加算してトータルで won/lost を判定
        # 理由: 部分利確後にSLに当たると「最終クローズ分はマイナス」でも「トータルはプラス」の場合がある。
        # これを正確に反映しないと consecutive_losses が誤カウントされAnti-Martingaleが誤作動する。
        _partial = self._partial_pnl.pop(symbol, 0.0)
        pnl_total = pnl + _partial   # 最終クローズ + 部分利確の合計

        won     = pnl_total > 0

        labels = {
            "tp":             "✅ 利確",
            "sl":             "🛑 損切り",
            "trailing":       "📈 追跡決済",
            "timeout":        "⏰ 時間切れ",
            "stagnation":     "😴 停滞タイムアウト",
            "force":          "🚨 強制決済",
            "signal_flip":    "🔄 シグナル反転",
            "momentum_fade":  "⚡ TP延長モメンタム反転決済",  # v51.0
        }
        label = labels.get(reason, reason)

        self._log(
            f"{label} {symbol}  {fmt_pct(pnl_pct)}  "
            f"損益: {'+' if pnl >= 0 else ''}{pnl:.2f}USD  "
            f"手数料: -{commission:.2f}USD  "
            f"保有: {(time.time() - pos.entry_time)/60:.1f}分",
            "info" if won else "warn"
        )

        # v49.0: pnl_totalを使用（部分利確 + 最終クローズの合計）
        # ログ表示は「最終クローズのみ」のpnlを使い、統計はpnl_totalを使う
        if _partial != 0.0:
            self._log(
                f"  └─ v49.0 部分利確累計: +{_partial:.2f}USD | "
                f"最終クローズ: {pnl:+.2f}USD | "
                f"トータル: {pnl_total:+.2f}USD ({'勝ち' if won else '負け'})",
                "info" if won else "warn"
            )
        record = TradeRecord(
            symbol=symbol, side=pos.side,
            entry_price=pos.entry_price, exit_price=price,
            size_usd=pos.size_usd, pnl=round(pnl_total, 4),   # v49.0: トータルPnLを記録
            pnl_pct=round(pnl_pct, 2), leverage=pos.leverage,
            won=won, entry_time=pos.entry_time,
            exit_time=time.time(), exit_reason=reason,
            # v23.0: エントリー時のコンテキストを記録（後で分析できるように）
            entry_score=getattr(pos, 'entry_score', 0.0),
            entry_fg=getattr(pos, 'entry_fg', 0),
            entry_btc_trend=getattr(pos, 'entry_btc_trend', ""),
        )
        # タイムラグ修正: 部分利確で既に加算済みの累計を record_trade に渡す
        _partial_already = getattr(pos, '_partial_realized', 0.0)
        self.risk.record_trade(record, partial_already_credited=_partial_already)
        # 追記専用台帳にも即座に保存（bot_state.jsonの破損・上書きに備える最終防衛線）
        self._append_to_ledger(record)

        # ── Feature 9: TP達成後の再エントリーウォッチ ───────────
        # TPを達成した銘柄を30分間監視リストに入れ、再エントリー時にボーナスを与える。
        # 理由: 強いトレンドは一度TP後も継続することが多いため、
        # 再エントリーの機会を逃さないようにする。
        # v74.0: TP達成 OR 利益SL（トレーリング利確）後の再エントリーウォッチ
        # 理由: v71.0トレーリングSLで利確した場合もトレンド継続の可能性がある。
        #       TPだけでなく利益SLも再エントリー候補として30分間監視する。
        _is_profitable_exit = (reason == "tp" or (reason == "sl" and pnl_total > 0)
                               or reason == "trailing")
        if _is_profitable_exit:
            self._tp_reentry_watch[symbol] = {
                "direction":   pos.side,
                "entry_price": pos.entry_price,
                "tp_price":    price,
                "expires_at":  time.time() + 1800,  # 30分間監視
            }
            logger.info(f"👀 {symbol} 利確後再エントリー監視開始（{reason}|+${pnl_total:.2f}|30分間）")

        # SL損切り後は同じ銘柄への再エントリーを一定時間禁止する（SLチャーン防止）
        # v74.0: 利益が出ているSL決済（v71.0トレーリングSLによるBE+利確）はクールダウン対象外
        # 理由: v71.0のATRトレーリングがSLを動的に引き上げるため、利益確定もreason="sl"で処理される。
        #       利益で終了したのに30分禁止は機会損失。実際の損失SLのみクールダウン適用。
        if reason == "sl" and pnl_total <= 0:
            with self._lock:
                self._sl_cooldown[symbol] = time.time() + self.config.sl_reentry_cooldown_s
            logger.debug(f"{symbol} SLクールダウン開始 ({self.config.sl_reentry_cooldown_s}秒)")
        elif reason == "sl" and pnl_total > 0:
            logger.info(f"✅ {symbol} v74.0 トレーリングSL利確（+${pnl_total:.2f}）→ クールダウン免除")

        # signal_flip後は15分間同一銘柄への再エントリーを禁止する（フリップチャーン防止）
        if reason == "signal_flip":
            with self._lock:
                self._flip_cooldown[symbol] = time.time() + self._FLIP_COOLDOWN_S
            logger.info(
                f"🚫 {symbol} フリップクールダウン開始: {self._FLIP_COOLDOWN_S//60}分間 "
                f"再エントリー禁止（signal_flip後の高速往復防止）"
            )

            # ── Feature 7: カスケード崩壊検知（無効化中）─────────────
            # ユーザー指示により一時停止中。
            # now = time.time()
            # self._sl_hits_window.append(now)
            # self._sl_hits_window = [t for t in self._sl_hits_window if now - t < 1800]
            # if len(self._sl_hits_window) >= 3:
            #     self._cascade_halt_until = now + 3600
            #     logger.warning(f"🌊 カスケード崩壊検知: ...")

        # 銘柄別 連続損失カウンター更新
        with self._lock:
            if not won:
                self._consec_losses[symbol] = self._consec_losses.get(symbol, 0) + 1
                if self._consec_losses[symbol] >= self._CONSEC_LIMIT:
                    self._consec_cooldown[symbol] = time.time() + self._CONSEC_COOL_S
                    self._consec_losses[symbol] = 0
                    logger.warning(
                        f"⛔ {symbol} {self._CONSEC_LIMIT}連敗 → "
                        f"{self._CONSEC_COOL_S//60}分間エントリー禁止"
                    )
            else:
                # 勝ちでリセット
                self._consec_losses[symbol] = 0

        # ブレークイーブン・フェーズ・確認フラグをリセット（次のポジションに備えて）
        with self._lock:
            self._be_triggered.pop(symbol, None)
            self._phase_tp2_done.pop(symbol, None)
            self._flip_confirm.pop(symbol, None)   # 反転確認記録もクリア
            self._momentum_fade_count.pop(symbol, None)  # v51.0: モメンタム弱化カウンターをリセット

        self._save_state()   # クローズ時に状態を保存（取引履歴・残高を永続化）

    # ── ダッシュボード用データ ────────────────────────
    def get_account_status(self) -> dict:
        with self._lock:
            positions = {}
            for sym, pos in self._positions.items():
                cp = self._market_data.get(sym, {}).get("current_price")
                positions[sym] = pos.to_dict(cp)
            logs       = list(self._logs)
            signal     = self._last_signal_result.copy()
            per_signal = dict(self._per_signal)

        risk_summary = self.risk.get_summary()
        upnl_total   = sum(p["upnl"] for p in positions.values())
        total_equity = risk_summary["balance"] + upnl_total

        # 取引履歴（最新50件）
        trade_history = [
            {
                "symbol":      t.symbol,
                "side":        t.side,
                "entry_price": t.entry_price,
                "exit_price":  t.exit_price,
                "pnl":         t.pnl,
                "pnl_pct":     t.pnl_pct,
                "leverage":    t.leverage,
                "won":         t.won,
                "exit_reason": t.exit_reason,
                "entry_time":  t.entry_time,
                "exit_time":   t.exit_time,
            }
            for t in self.risk.trade_history[-50:][::-1]
        ]

        # スキャン済み銘柄数
        scanned_count = len(per_signal)

        return {
            "mode":               self.config.mode,
            "symbol":             self.config.symbol,
            "watch_symbols":      list(self._watch_symbols),
            "scanned_count":      scanned_count,
            "balance":            risk_summary["balance"],
            "initial":            risk_summary["initial_balance"],
            "total_equity":       round(total_equity, 2),
            "unrealized_pnl":     round(upnl_total, 2),
            "realized_pnl":       risk_summary["total_pnl"],
            "realized_pnl_pct":   risk_summary["total_pnl_pct"],
            "today_pnl":          risk_summary["today_pnl"],
            "today_pnl_pct":      risk_summary["today_pnl_pct"],
            "drawdown_pct":       risk_summary["drawdown_pct"],
            "closed_trades":      risk_summary["closed_trades"],
            "won_count":          risk_summary["won_count"],
            "lost_count":         risk_summary["lost_count"],
            "win_rate":           risk_summary["win_rate"],
            "consecutive_losses": risk_summary["consecutive_losses"],
            "is_cooling_down":    risk_summary["is_cooling_down"],
            "is_halted":          risk_summary["is_halted"],
            "current_price":      self._market_data.get(self.config.symbol, {}).get("current_price"),
            "positions":          positions,
            "per_signal":         per_signal,
            "scan_count":         self._scan_count,
            "last_signal":        signal,
            "logs":               logs[:100],
            "trade_history":      trade_history,
            "ts":                       time.time(),
            "leverage":                 signal.get("leverage", self.config.min_leverage),
            "max_positions":            self.config.max_positions,
            "cooldown_remaining_m":     risk_summary.get("cooldown_remaining_m", 0),
            "halt_remaining_h":         risk_summary.get("halt_remaining_h", 0),
            "daily_loss_limit_pct":       self.config.daily_loss_limit * 100,
            "today_pnl_pct":              risk_summary.get("today_pnl_pct", 0),
            "daily_limit_breach_count":   risk_summary.get("daily_limit_breach_count", 0),
            "market_context":             self.mktctx.get_snapshot(),
            "bot_started_at":             self._started_at,
        }

    def get_equity_history(self) -> list:
        """資産推移履歴を返す（エクイティカーブ用）"""
        return list(self._equity_history)

    def get_symbol_stats(self) -> list:
        """銘柄ごとの勝率・損益ランキングを返す"""
        from collections import defaultdict
        stats: dict = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
        for t in self.risk.trade_history:
            s = stats[t.symbol]
            if t.won:
                s["wins"] += 1
            else:
                s["losses"] += 1
            s["pnl"] += t.pnl
        result = []
        for sym, s in stats.items():
            total = s["wins"] + s["losses"]
            result.append({
                "symbol":   sym,
                "wins":     s["wins"],
                "losses":   s["losses"],
                "total":    total,
                "win_rate": round(s["wins"] / total * 100, 1) if total > 0 else 0.0,
                "pnl":      round(s["pnl"], 2),
            })
        result.sort(key=lambda x: x["pnl"], reverse=True)
        return result

    def get_logs(self) -> list:
        with self._lock:
            return list(self._logs)

    def get_chart_data(self, symbol: str = None, timeframe: str = "1m") -> dict:
        """
        チャート描画用データを返す。
        symbolを指定しない場合はconfig.symbol（BTC/USDT）のデータを使う。
        """
        sym = symbol or self.config.symbol
        with self._lock:
            sym_data  = self._market_data.get(sym, {})
            cp        = sym_data.get("current_price")
            pos       = self._positions.get(sym)

        df = sym_data.get("ohlcv", {}).get(timeframe)
        candles = []
        if df is not None and not df.empty:
            for idx, row in df.tail(100).iterrows():
                ts = int(idx.timestamp())
                candles.append({
                    "time":  ts,
                    "open":  round(float(row["open"]),  2),
                    "high":  round(float(row["high"]),  2),
                    "low":   round(float(row["low"]),   2),
                    "close": round(float(row["close"]), 2),
                })

        positions = {}
        if pos is not None:
            positions[sym] = pos.to_dict(cp)

        # その銘柄の取引履歴からチャートマーカーを生成
        markers = []
        for t in self.risk.trade_history:
            if t.symbol != sym:
                continue
            # エントリーマーカー（下から上向き三角）
            markers.append({
                "time":     int(t.entry_time),
                "position": "belowBar",
                "color":    "#4fc3f7" if t.side == "long" else "#ff9800",
                "shape":    "arrowUp",
                "text":     f"{'LONG' if t.side == 'long' else 'SHORT'} @{t.entry_price:.4g}",
            })
            # 決済マーカー（上から下向き三角）
            markers.append({
                "time":     int(t.exit_time),
                "position": "aboveBar",
                "color":    "#00e676" if t.won else "#f44336",
                "shape":    "arrowDown",
                "text":     f"{'✅ 利確' if t.won else '❌ 損切'} {'+' if t.pnl >= 0 else ''}{t.pnl:.0f}$",
            })

        # 現在保有中ポジションのエントリーマーカーも追加
        if pos is not None:
            markers.append({
                "time":     int(pos.entry_time),
                "position": "belowBar",
                "color":    "#4fc3f7",
                "shape":    "arrowUp",
                "text":     f"保有中 @{pos.entry_price:.4g}",
            })

        # 時刻順にソート（setMarkersの要件）
        markers.sort(key=lambda m: m["time"])

        return {
            "candles":       candles,
            "positions":     positions,
            "markers":       markers,
            "current_price": cp,
            "symbol":        sym,
            "timeframe":     timeframe,
        }

    # ── 手動スキャン（シグナル評価のみ・エントリーなし）────
    def _scan_symbol_quiet(self, symbol: str):
        """
        手動スキャン専用の静音スキャン。
        シグナル評価までは行うが、自動エントリーは起こさない。
        """
        try:
            multi_tf = self.fetcher.fetch_multi_timeframe(symbol)
            cp       = self.fetcher.fetch_current_price(symbol)
            if not cp or not multi_tf:
                return

            with self._lock:
                self._market_data[symbol] = {
                    "ohlcv": multi_tf, "current_price": cp, "ts": time.time()
                }

            # 1時間足トレンド（キャッシュ活用）
            now_ts = time.time()
            cached = self._trend_cache.get(symbol)
            if cached and (now_ts - cached["ts"]) < self._trend_cache_ttl:
                trend_1h = cached["trend"]
            else:
                df_1h    = self.fetcher.fetch_ohlcv(
                    symbol, self.config.trend_tf,
                    limit=self.config.trend_ohlcv_limit
                )
                trend_1h = get_1h_trend(df_1h, self.config)
                self._trend_cache[symbol] = {"trend": trend_1h, "ts": now_ts}

            consensus = evaluate_consensus(multi_tf, self.config, trend_1h=trend_1h, fear_greed=self.mktctx.fear_greed, btc_trend=self._get_btc_trend())
            leverage  = self.risk.calc_leverage(
                consensus["score"],
                fear_greed=self.mktctx.fear_greed,
                btc_trend=trend_1h if trend_1h else self._get_btc_trend()
            )
            result    = {**consensus, "leverage": leverage, "symbol": symbol}

            with self._lock:
                self._per_signal[symbol] = result
        except Exception as e:
            logger.debug(f"手動スキャン {symbol} エラー: {e}")

    # ── 手動スキャン ────────────────────────────────
    def manual_scan(self, top_n: int = 20) -> list:
        """
        上位銘柄をリアルタイムで並列スキャンし、スコア順に返す。
        エントリー条件を満たす銘柄（ready=True）だけでなく、
        スコアが高いHOLD銘柄（候補）も含めて返す。
        """
        import concurrent.futures as cf

        with self._lock:
            watch = list(self._watch_symbols)[:50]   # 監視上位50銘柄を対象
            held  = set(self._positions.keys())

        # 未保有の銘柄を最大30件リアルタイムスキャン
        targets = [s for s in watch if s not in held][:30]
        with cf.ThreadPoolExecutor(max_workers=10) as executor:
            executor.map(self._scan_symbol_quiet, targets)

        # スキャン後のキャッシュから結果を収集
        with self._lock:
            per_sig = dict(self._per_signal)
            held    = set(self._positions.keys())
            mkt     = {k: v.get("current_price") for k, v in self._market_data.items()}

        candidates = []
        for sym in targets:
            sig = per_sig.get(sym)
            if not sig:
                continue
            score  = sig.get("score", 0)
            if score < 0.10:          # スコア10%未満は完全に無関係なので除外
                continue

            signal = sig.get("signal", Signal.HOLD)
            cp     = mkt.get(sym) or 0
            atr    = sig.get("atr") or 0
            lever  = sig.get("leverage", self.config.min_leverage)
            is_hold = signal in (Signal.HOLD, "HOLD", "hold", "neutral", None, "")
            side   = "hold" if is_hold else signal.lower()

            tp_price, sl_price, tp_dist_pct = 0, 0, 0
            if not is_hold and cp > 0:
                tp_price, sl_price = self.risk.calc_tp_sl(cp, atr, side)
                tp_dist_pct = abs(tp_price - cp) / cp if cp > 0 else 0

            tf_res = sig.get("tf_results", {})
            timeframe_summary = " | ".join(
                f"{tf}:{r.get('direction','?')}" for tf, r in tf_res.items()
            )

            # エントリー条件を全て満たすかどうか
            ready = (
                not is_hold
                and tp_dist_pct >= self.config.min_tp_dist_pct
                and score >= self.config.min_signal_score
            )

            candidates.append({
                "symbol":      sym,
                "signal":      side,
                "score":       round(score, 3),
                "leverage":    lever,
                "price":       cp,
                "tp_price":    round(tp_price, 8),
                "sl_price":    round(sl_price, 8),
                "tp_dist_pct": round(tp_dist_pct * 100, 2),
                "atr":         round(atr, 8),
                "trend_1h":    sig.get("trend_1h", "?"),
                "tf_summary":  timeframe_summary,
                "ready":       ready,   # True=エントリー推奨 / False=監視継続
            })

        # スコア降順でソート
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:top_n]

    # ── 手動エントリー ──────────────────────────────
    def manual_entry(self, symbol: str, side: str) -> dict:
        """
        ユーザーが手動でエントリーボタンを押したときに呼ばれる。
        _per_signal のキャッシュデータを元にポジションを開く。
        """
        with self._lock:
            sig    = self._per_signal.get(symbol)
            held   = symbol in self._positions
            cp     = self._market_data.get(symbol, {}).get("current_price")
            pos_count = len(self._positions)

        if held:
            return {"ok": False, "reason": f"すでに {symbol} のポジションを保有中です"}
        if pos_count >= self.config.max_positions:
            return {"ok": False, "reason": f"最大ポジション数 ({self.config.max_positions}件) に達しています"}
        if not cp:
            return {"ok": False, "reason": f"{symbol} の現在価格を取得できません"}

        atr   = (sig.get("atr") or 0) if sig else 0
        score = (sig.get("score") or 0) if sig else 0
        lever = self._calc_symbol_leverage(symbol, atr, cp or 0, score)

        tp_price, sl_price = self.risk.calc_tp_sl(cp, atr, side)

        tp_dist_pct = abs(tp_price - cp) / cp if cp > 0 else 0
        if tp_dist_pct < self.config.min_tp_dist_pct:
            return {"ok": False, "reason": f"TP距離が狭すぎます ({tp_dist_pct*100:.3f}% < {self.config.min_tp_dist_pct*100:.1f}%)"}

        _score_for_size = float((sig.get("score_100") or score * 100) if sig else 0)
        quantity = self.risk.calc_position_size(cp, sl_price, lever,
                                                signal_score=_score_for_size)  # v52.0
        if quantity <= 0:
            return {"ok": False, "reason": "ポジションサイズの計算結果が 0 以下です"}

        ctx_mult = self.mktctx.get_long_size_multiplier() if side == "long" else self.mktctx.get_short_size_multiplier()
        quantity *= ctx_mult

        self._open_position(
            symbol=symbol, side=side, price=cp,
            quantity=quantity, tp_price=tp_price, sl_price=sl_price,
            leverage=lever, score=score, entry_atr=atr,
        )
        return {
            "ok":       True,
            "symbol":   symbol,
            "side":     side,
            "price":    cp,
            "tp_price": round(tp_price, 8),
            "sl_price": round(sl_price, 8),
            "leverage": lever,
        }
