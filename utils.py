"""
utils.py — 共通ユーティリティ
==============================
ロギング設定・数値フォーマット・時間ヘルパーなど
どのモジュールからも使える汎用関数をまとめます。
"""

import logging
import sys
import time
from datetime import datetime, timezone
from typing import Optional


# ════════════════════════════════════════════════════
# ロギング設定
# ════════════════════════════════════════════════════

def setup_logger(name: str = "trading_bot", level: str = "INFO",
                 log_file: Optional[str] = None) -> logging.Logger:
    """
    アプリ全体で使うロガーをセットアップする。
    コンソールとファイルの両方に出力できる。
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger  # 二重設定防止

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # コンソール出力
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # ファイル出力（指定があれば）
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


# ════════════════════════════════════════════════════
# 数値フォーマット
# ════════════════════════════════════════════════════

def fmt_price(price: float, decimals: int = 2) -> str:
    """価格を見やすい形式にフォーマット"""
    if price is None:
        return "—"
    if price >= 1000:
        return f"{price:,.{decimals}f}"
    if price >= 1:
        return f"{price:.{decimals+2}f}"
    return f"{price:.8f}"


def fmt_pct(value: float, decimals: int = 2) -> str:
    """パーセントを符号付きでフォーマット（例: +2.35%）"""
    if value is None:
        return "—"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def fmt_jpy(value: float) -> str:
    """円建て金額をフォーマット（例: ¥1,234,567）"""
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}¥{abs(value):,.0f}"


# ════════════════════════════════════════════════════
# 時間ヘルパー
# ════════════════════════════════════════════════════

def now_ts() -> float:
    """現在時刻のUNIXタイムスタンプ（秒）"""
    return time.time()


def ts_to_str(ts: float, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """UNIXタイムスタンプを文字列に変換"""
    return datetime.fromtimestamp(ts).strftime(fmt)


def timeframe_to_seconds(tf: str) -> int:
    """
    時間軸の文字列を秒数に変換する。
    例）"1m"→60, "5m"→300, "1h"→3600, "1d"→86400
    """
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    num = int("".join(c for c in tf if c.isdigit()))
    unit = "".join(c for c in tf if c.isalpha()).lower()
    return num * units.get(unit, 60)


# ════════════════════════════════════════════════════
# 統計ヘルパー
# ════════════════════════════════════════════════════

def safe_div(a: float, b: float, default: float = 0.0) -> float:
    """ゼロ除算を安全に処理する割り算"""
    return a / b if b != 0 else default


def calc_sharpe_ratio(returns: list, risk_free_rate: float = 0.0,
                       periods_per_year: int = 252) -> float:
    """
    シャープレシオを計算する（リスクあたりのリターンの指標）
    値が高いほど「リスクに対してうまく稼げている」ことを意味する。
    一般的に1.0以上が良好、2.0以上は優秀。
    """
    if len(returns) < 2:
        return 0.0
    import numpy as np
    arr = np.array(returns, dtype=float)
    mean_r  = arr.mean()
    std_r   = arr.std(ddof=1)
    if std_r == 0:
        return 0.0
    return float((mean_r - risk_free_rate) * (periods_per_year ** 0.5) / std_r)


def calc_max_drawdown(equity_curve: list) -> float:
    """
    最大ドローダウンを計算する（ピークからの最大下落率）
    例）100万円 → 90万円になったとき = -10%のドローダウン
    """
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def calc_profit_factor(trades: list) -> float:
    """
    プロフィットファクターを計算する（総利益 ÷ 総損失）
    1.5以上が理想。1.0以下は負け越し。
    """
    gross_profit = sum(t["pnl"] for t in trades if t.get("pnl", 0) > 0)
    gross_loss   = sum(abs(t["pnl"]) for t in trades if t.get("pnl", 0) < 0)
    return safe_div(gross_profit, gross_loss, default=0.0)


def calc_kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    ケリー基準（最適なポジションサイズ）を計算する。
    f* = (win_rate × avg_win - (1-win_rate) × avg_loss) / avg_win
    負の値は「賭けるな」というシグナル。
    実際には half_kelly（f* × 0.5）を使う。
    """
    if avg_win <= 0 or avg_loss <= 0:
        return 0.0
    f = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win
    return max(0.0, f)
