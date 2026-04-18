"""
monitor_kelly_bot.py
====================
Kelly Bot の監視スクリプト (15分毎実行)

機能:
1. ボット状態ファイルの健全性チェック
2. 現在のポジション評価損益を計算
3. 清算リスクの事前警告 (DD 70%超で警告)
4. Binance API接続確認
5. 30日経過ならリバランス自動トリガー
6. エラーログ + macOS通知 (notifier)
7. 日次サマリーレポート

使い方:
    # 手動実行
    python3 monitor_kelly_bot.py

    # Cron自動実行 (15分ごと)
    crontab -e で追加:
    */15 * * * * cd /Users/sanosano/projects/crypto-bot-pro && /usr/bin/python3 monitor_kelly_bot.py >> monitor.log 2>&1
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import ccxt


# ========== 設定 ==========

STATE_FILE = "kelly_bot_state.json"
LOG_FILE = "monitor.log"
ALERT_FILE = "monitor_alerts.log"

# 警告閾値
DD_WARN_PCT = 30      # -30%で警告
DD_CRITICAL_PCT = 50  # -50%で重大警告
DAYS_SINCE_REBAL_WARN = 32  # 32日以上リバランスなしなら警告
MAINTENANCE_MARGIN_RATE = 0.005

# Bot起動パス
BOT_DIR = "/Users/sanosano/projects/crypto-bot-pro"
BOT_CMD = ["python3", "kelly_bot.py", "--mode", "paper", "--capital", "3000"]


# ========== ロガー ==========

def setup_logger():
    logger = logging.getLogger("monitor")
    logger.setLevel(logging.INFO)
    # ファイル
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    # コンソール
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(ch)
    return logger


# ========== macOS通知 ==========

def macos_notify(title: str, message: str, critical: bool = False):
    """macOSの通知センターに表示"""
    try:
        sound = "Basso" if critical else "Glass"
        subprocess.run([
            "osascript", "-e",
            f'display notification "{message}" with title "{title}" sound name "{sound}"'
        ], check=False, timeout=5)
    except Exception:
        pass  # 通知失敗しても続行


def log_alert(message: str, level: str = "WARNING"):
    """アラートを別ファイルに記録"""
    try:
        with open(ALERT_FILE, "a") as f:
            f.write(f"{datetime.now().isoformat()} [{level}] {message}\n")
    except Exception:
        pass


# ========== Bot状態チェック ==========

@dataclass
class HealthCheck:
    ok: bool
    state_exists: bool = False
    positions_count: int = 0
    total_capital: float = 0
    start_capital: float = 0
    total_pnl_pct: float = 0
    days_since_rebal: int = 0
    worst_position_dd: float = 0
    worst_position_name: str = ""
    warnings: list = None
    errors: list = None

    def __post_init__(self):
        if self.warnings is None: self.warnings = []
        if self.errors is None: self.errors = []


def load_bot_state() -> Optional[dict]:
    path = Path(BOT_DIR) / STATE_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        return {"_error": str(e)}


def check_bot_health(logger) -> HealthCheck:
    """ボットの健全性チェック"""
    hc = HealthCheck(ok=False)

    # 状態ファイル確認
    state = load_bot_state()
    if state is None:
        hc.errors.append("状態ファイルが存在しません")
        return hc
    if "_error" in state:
        hc.errors.append(f"状態ファイル読込失敗: {state['_error']}")
        return hc

    hc.state_exists = True
    hc.total_capital = state.get("total_capital", 0)
    hc.start_capital = state.get("start_capital", 0)
    if hc.start_capital > 0:
        hc.total_pnl_pct = (hc.total_capital / hc.start_capital - 1) * 100

    # リバランス経過日数
    last_rebal = state.get("last_rebalance")
    if last_rebal:
        try:
            last_dt = datetime.fromisoformat(last_rebal.split(".")[0])
            hc.days_since_rebal = (datetime.now() - last_dt).days
        except Exception:
            pass

    # ポジション評価
    positions = state.get("positions", {})
    hc.positions_count = len(positions)

    if not positions:
        hc.warnings.append("ポジションなし (Cooldown中 or リバランス待ち)")

    # 価格取得して含み損益計算
    if positions:
        try:
            ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
            for sym, pos in positions.items():
                ticker = ex.fetch_ticker(sym)
                current = float(ticker["last"])
                entry = pos["entry_price"]
                size = pos["size"]
                leverage = pos["leverage"]
                margin = pos.get("initial_margin", 0)

                # レバ込みsizeでPnL (修正版ロジック)
                unrealized = (current - entry) * size
                pnl_pct_on_margin = (unrealized / margin * 100) if margin > 0 else 0

                logger.info(f"📊 {sym}: ${entry:.2f} → ${current:.2f}  "
                             f"未実現 ${unrealized:+.2f} ({pnl_pct_on_margin:+.1f}% of margin)  "
                             f"Kelly {leverage:.2f}x")

                # 清算リスク (現在価格から清算価格までの余裕)
                # 清算条件: equity <= margin_maintenance
                # current_equity = margin + (current - entry) * size
                # 清算価格 = entry - (margin - maintenance) / size
                # 安全余裕 = (current - liq_price) / current
                equity = margin + unrealized
                maintenance = current * size * MAINTENANCE_MARGIN_RATE
                if equity <= maintenance * 1.5:  # マージン1.5倍以内なら危険
                    hc.errors.append(f"⚠️ {sym} が清算閾値に近い! equity=${equity:.0f} vs mm=${maintenance:.0f}")

                # ポジション単位のDD
                if pnl_pct_on_margin < hc.worst_position_dd:
                    hc.worst_position_dd = pnl_pct_on_margin
                    hc.worst_position_name = sym
        except Exception as e:
            hc.errors.append(f"価格取得エラー: {e}")

    # リバランス経過チェック
    if hc.days_since_rebal >= DAYS_SINCE_REBAL_WARN:
        hc.warnings.append(f"🔄 リバランスが {hc.days_since_rebal}日間実行されていません!")

    # DD警告
    if hc.worst_position_dd <= -DD_CRITICAL_PCT:
        hc.errors.append(f"🚨 {hc.worst_position_name} が -{DD_CRITICAL_PCT}% を超えて下落!")
    elif hc.worst_position_dd <= -DD_WARN_PCT:
        hc.warnings.append(f"⚠️ {hc.worst_position_name} が -{DD_WARN_PCT}% を超えて下落")

    # 全体PnL警告
    if hc.total_pnl_pct <= -30:
        hc.warnings.append(f"📉 総資産が初期から {hc.total_pnl_pct:+.1f}% (DD大きい)")

    hc.ok = len(hc.errors) == 0
    return hc


def trigger_rebalance(logger) -> bool:
    """必要ならボットを起動してリバランス実行"""
    try:
        logger.info("🔄 Bot起動中 (リバランス判定込み)...")
        result = subprocess.run(
            BOT_CMD, cwd=BOT_DIR, capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            logger.info("✅ Bot実行成功")
            # 出力の最後の数行をログに
            for line in result.stdout.strip().split("\n")[-5:]:
                logger.info(f"  [Bot] {line}")
            return True
        else:
            logger.error(f"❌ Bot異常終了 (code={result.returncode})")
            logger.error(f"  stderr: {result.stderr[:500]}")
            return False
    except subprocess.TimeoutExpired:
        logger.error("❌ Bot実行タイムアウト (120秒)")
        return False
    except Exception as e:
        logger.error(f"❌ Bot実行失敗: {e}")
        return False


def print_health_report(hc: HealthCheck, logger):
    """健全性レポートを表示"""
    logger.info("=" * 70)
    logger.info(f"🩺 Kelly Bot 健全性レポート ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    logger.info("=" * 70)

    if not hc.state_exists:
        logger.error("❌ ボット状態ファイル未発見")
        for err in hc.errors:
            logger.error(f"   {err}")
        return

    status_emoji = "✅" if hc.ok else "🚨"
    logger.info(f"  {status_emoji} 総合判定: {'健全' if hc.ok else '異常あり'}")
    logger.info(f"  💰 資金: ${hc.total_capital:,.2f} (初期${hc.start_capital:,.0f}, "
                 f"{hc.total_pnl_pct:+.2f}%)")
    logger.info(f"  📊 ポジション数: {hc.positions_count}")
    logger.info(f"  📅 最終リバランスから: {hc.days_since_rebal}日")
    if hc.positions_count > 0:
        logger.info(f"  📉 最悪ポジションDD: {hc.worst_position_dd:+.2f}% ({hc.worst_position_name})")

    if hc.errors:
        logger.error(f"\n  🚨 エラー ({len(hc.errors)}件):")
        for err in hc.errors:
            logger.error(f"    - {err}")

    if hc.warnings:
        logger.warning(f"\n  ⚠️ 警告 ({len(hc.warnings)}件):")
        for w in hc.warnings:
            logger.warning(f"    - {w}")

    logger.info("=" * 70)


# ========== メイン ==========

def main():
    logger = setup_logger()
    logger.info(f"\n{'#'*70}")
    logger.info(f"# Kelly Bot Monitor 実行 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'#'*70}")

    try:
        # 1. 健全性チェック
        hc = check_bot_health(logger)
        print_health_report(hc, logger)

        # 2. 致命的エラー → macOS通知
        if hc.errors:
            critical = any("清算" in e or "CRITICAL" in e.upper() for e in hc.errors)
            msg = "; ".join(hc.errors[:2])
            macos_notify("🚨 Kelly Bot エラー" + (" 重大!" if critical else ""), msg, critical)
            for e in hc.errors:
                log_alert(e, "ERROR")

        # 3. 警告 → ログのみ (通知過多防止)
        if hc.warnings:
            for w in hc.warnings:
                log_alert(w, "WARNING")

        # 4. リバランス必要なら自動トリガー
        if hc.state_exists and hc.days_since_rebal >= 30:
            logger.info("⏰ 30日経過 - リバランストリガー")
            triggered = trigger_rebalance(logger)
            if triggered:
                macos_notify("✅ Kelly Bot", f"リバランス実行完了 ({hc.days_since_rebal}日ぶり)")
            else:
                macos_notify("🚨 Kelly Bot", "リバランス失敗!", critical=True)
                log_alert("リバランス失敗", "ERROR")

        # 5. ボット未起動なら最初の起動
        if not hc.state_exists:
            logger.warning("🚀 ボット未起動 - 初期リバランス実行")
            trigger_rebalance(logger)

        # 6. 定期サマリー (毎日12時に大きなサマリー)
        now = datetime.now()
        if now.hour == 12 and now.minute < 15:
            summary = (f"残高: ${hc.total_capital:,.2f} "
                        f"({hc.total_pnl_pct:+.2f}%)\n"
                        f"ポジション: {hc.positions_count}\n"
                        f"次リバランスまで: {max(0, 30 - hc.days_since_rebal)}日")
            macos_notify("📊 Kelly Bot 日次サマリー", summary)

        logger.info("✅ Monitor 正常終了")
        return 0

    except Exception as e:
        logger.error(f"💥 Monitor 自体が例外: {e}")
        logger.error(traceback.format_exc())
        macos_notify("💥 Monitor エラー", str(e), critical=True)
        log_alert(f"Monitor例外: {e}", "CRITICAL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
