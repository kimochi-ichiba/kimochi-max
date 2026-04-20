"""
backtester.py — バックテストモジュール
========================================
過去データを使って「もし自動取引していたら」を高速検証する。

バックテストとは:
  過去の実際の価格データをロードして、
  今のシグナルロジックをその時間に適用したらどうなっていたかをシミュレーションすること。
  「このパラメータで本当に儲かるのか？」を本番投入前に確認できる重要な機能。

出力メトリクス:
  - 総リターン（%）
  - 最大ドローダウン（%）
  - 勝率（%）
  - プロフィットファクター（総利益 / 総損失）
  - シャープレシオ（リスク調整後リターン）
  - 複利運用シミュレーション（資産成長曲線）
"""

import time
import logging
from typing import Optional
import pandas as pd
import numpy as np

from config import Config, Mode
from data_fetcher import DataFetcher
from indicators import add_all_indicators, get_latest_row, is_high_volatility
from strategy import evaluate_single_timeframe, Signal
from risk_manager import TradeRecord
from utils import (
    setup_logger, safe_div, calc_sharpe_ratio,
    calc_max_drawdown, calc_profit_factor
)

logger = setup_logger("backtester")


class BacktestResult:
    """バックテスト結果をまとめるクラス"""

    def __init__(self):
        self.trades:        list[TradeRecord] = []
        self.equity_curve:  list[float]       = []
        self.timestamps:    list              = []
        self.initial:       float             = 0.0
        self.final:         float             = 0.0

    def summary(self) -> dict:
        """バックテスト結果のサマリーを返す"""
        if not self.trades:
            return {"error": "取引が1件もありませんでした。パラメータを調整してください。"}

        wins   = [t for t in self.trades if t.won]
        losses = [t for t in self.trades if not t.won]

        total_return_pct = safe_div(self.final - self.initial, self.initial) * 100
        win_rate         = safe_div(len(wins), len(self.trades)) * 100
        avg_win          = safe_div(sum(t.pnl_pct for t in wins), len(wins)) if wins else 0
        avg_loss         = safe_div(sum(t.pnl_pct for t in losses), len(losses)) if losses else 0
        profit_factor    = calc_profit_factor([{"pnl": t.pnl} for t in self.trades])
        max_dd           = calc_max_drawdown(self.equity_curve) * 100

        # シャープレシオの計算用: 1取引ごとのリターン率
        returns = [t.pnl_pct for t in self.trades]
        sharpe  = calc_sharpe_ratio(returns, periods_per_year=252)

        # 複利で成長した場合の最終資産
        compound_final = self.initial
        for t in self.trades:
            compound_final *= (1 + t.pnl_pct / 100)

        return {
            "期間":                f"{self.timestamps[0] if self.timestamps else '?'} 〜 {self.timestamps[-1] if self.timestamps else '?'}",
            "初期資金":            f"${self.initial:,.2f}",
            "最終資金":            f"${self.final:,.2f}",
            "複利最終資金":        f"${compound_final:,.2f}",
            "総リターン":          f"{total_return_pct:+.2f}%",
            "最大ドローダウン":    f"{max_dd:.2f}%",
            "総取引数":            len(self.trades),
            "勝率":                f"{win_rate:.1f}% ({len(wins)}勝{len(losses)}敗)",
            "平均利益":            f"{avg_win:+.2f}%",
            "平均損失":            f"{avg_loss:+.2f}%",
            "プロフィットファクター": f"{profit_factor:.2f}",
            "シャープレシオ":      f"{sharpe:.2f}",
            "判定":                _grade(total_return_pct, max_dd, win_rate, profit_factor),
        }

    def print_summary(self):
        """サマリーをコンソールに見やすく表示する"""
        summary = self.summary()
        print("\n" + "=" * 60)
        print("  📊 バックテスト結果")
        print("=" * 60)
        for key, val in summary.items():
            print(f"  {key:20s}: {val}")
        print("=" * 60 + "\n")

    def plot_equity_curve(self, save_path: Optional[str] = None):
        """資産成長曲線をグラフで表示する"""
        try:
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates

            fig, axes = plt.subplots(2, 1, figsize=(14, 8))
            fig.suptitle("バックテスト結果", fontsize=14, fontweight="bold")

            # グラフ1: 資産推移
            ax1 = axes[0]
            ax1.plot(self.equity_curve, color="#00e676", linewidth=1.5, label="資産額")
            ax1.axhline(y=self.initial, color="gray", linestyle="--", alpha=0.5, label="初期資金")
            ax1.fill_between(range(len(self.equity_curve)),
                             self.initial, self.equity_curve,
                             where=[v >= self.initial for v in self.equity_curve],
                             alpha=0.2, color="green", label="利益")
            ax1.fill_between(range(len(self.equity_curve)),
                             self.initial, self.equity_curve,
                             where=[v < self.initial for v in self.equity_curve],
                             alpha=0.2, color="red", label="損失")
            ax1.set_ylabel("資産額 (USD)")
            ax1.legend(loc="upper left")
            ax1.grid(True, alpha=0.3)

            # グラフ2: ドローダウン
            ax2 = axes[1]
            peak   = self.equity_curve[0]
            dds    = []
            for v in self.equity_curve:
                if v > peak:
                    peak = v
                dds.append((peak - v) / peak * 100 if peak > 0 else 0)
            ax2.fill_between(range(len(dds)), 0, [-d for d in dds],
                             alpha=0.5, color="red", label="ドローダウン")
            ax2.set_ylabel("ドローダウン (%)")
            ax2.set_xlabel("取引回数")
            ax2.legend(loc="lower left")
            ax2.grid(True, alpha=0.3)

            plt.tight_layout()
            if save_path:
                plt.savefig(save_path, dpi=150, bbox_inches="tight")
                logger.info(f"グラフを保存しました: {save_path}")
            else:
                plt.show()
            plt.close()

        except ImportError:
            logger.warning("matplotlibがインストールされていません。グラフは表示できません。")


def _grade(total_return: float, max_dd: float, win_rate: float, pf: float) -> str:
    """バックテスト結果を5段階で評価する"""
    if total_return > 50 and max_dd < 10 and win_rate > 55 and pf > 1.5:
        return "⭐⭐⭐⭐⭐ 優秀（本番投入の候補になりえます）"
    if total_return > 20 and max_dd < 15 and pf > 1.3:
        return "⭐⭐⭐⭐ 良好（パラメータ微調整で改善できます）"
    if total_return > 0 and pf > 1.1:
        return "⭐⭐⭐ 普通（さらなる改善が必要です）"
    if total_return > 0:
        return "⭐⭐ 要改善（戦略の見直しを推奨します）"
    return "⭐ 不合格（このパラメータでの本番投入は危険です）"


# ════════════════════════════════════════════════════
# バックテスター本体
# ════════════════════════════════════════════════════

class Backtester:
    """
    過去データでシグナルロジックを高速検証するクラス。

    使い方:
        config = Config(mode=Mode.BACKTEST, initial_balance=10000)
        bt = Backtester(config)
        result = bt.run("BTC/USDT", "2024-01-01", "2024-12-31")
        result.print_summary()
        result.plot_equity_curve()
    """

    def __init__(self, config: Config):
        self.config  = config
        self.fetcher = DataFetcher(config)

    def run(self, symbol: str, start: str, end: str,
            timeframe: str = "1h", initial_balance: float = 10_000.0) -> BacktestResult:
        """
        バックテストを実行する。

        引数:
            symbol:          銘柄（例: "BTC/USDT"）
            start:           開始日 "2024-01-01"
            end:             終了日 "2024-12-31"
            timeframe:       使用する時間軸（高速化のため1hを推奨）
            initial_balance: 初期資金

        戻り値:
            BacktestResult オブジェクト
        """
        logger.info(f"バックテスト開始: {symbol} {start}〜{end} 時間軸:{timeframe} 初期資金:${initial_balance:,.0f}")

        # 過去データを取得
        df = self.fetcher.fetch_historical_ohlcv(symbol, timeframe, start, end)
        if df.empty:
            logger.error("過去データの取得に失敗しました")
            return BacktestResult()

        # 指標を全データに対して一括計算
        df = add_all_indicators(df, self.config)
        logger.info(f"指標計算完了: {len(df)}本のローソク足")

        # バックテスト実行
        result = BacktestResult()
        result.initial   = initial_balance
        balance          = initial_balance
        peak_balance     = initial_balance
        consecutive_loss = 0
        position: Optional[dict] = None  # 現在のポジション
        trail_peak       = 0.0

        for i in range(self.config.ema_trend + 10, len(df)):
            row    = df.iloc[i]
            price  = float(row["close"])
            ts     = df.index[i]

            result.equity_curve.append(balance)
            result.timestamps.append(str(ts)[:10])

            # ── ポジション決済チェック ───────────────
            if position is not None:
                side  = position["side"]
                entry = position["entry"]
                tp    = position["tp"]
                sl    = position["sl"]
                qty   = position["qty"]
                lev   = position["lev"]
                e_t   = position["entry_time"]

                # トレーリングストップのピーク更新
                if side == "long":
                    trail_peak = max(trail_peak, price)
                else:
                    trail_peak = min(trail_peak, price)

                # 決済判定
                exit_reason = None
                if side == "long":
                    if price >= tp:              exit_reason = "tp"
                    elif price <= sl:            exit_reason = "sl"
                    elif trail_peak > 0:
                        peak_pct = trail_peak / entry - 1
                        if peak_pct >= self.config.trailing_stop_activate:
                            if (price / trail_peak - 1) <= -self.config.trailing_stop_pct:
                                exit_reason = "trailing"
                else:  # short
                    if price <= tp:              exit_reason = "tp"
                    elif price >= sl:            exit_reason = "sl"

                if exit_reason:
                    # 決済処理
                    if side == "long":
                        pnl = (price - entry) * qty
                        raw_pct = (price / entry - 1)
                    else:
                        pnl = (entry - price) * qty
                        raw_pct = (entry / price - 1)

                    pnl_pct = raw_pct * lev * 100
                    pnl    *= lev
                    pnl    -= abs(pnl) * self.config.commission_rate * 2  # 往復手数料

                    balance += pnl
                    if balance > peak_balance:
                        peak_balance = balance

                    won = pnl > 0
                    if won:
                        consecutive_loss = 0
                    else:
                        consecutive_loss += 1

                    result.trades.append(TradeRecord(
                        symbol=symbol, side=side,
                        entry_price=entry, exit_price=price,
                        size_usd=position.get("size", 0),
                        pnl=round(pnl, 4),
                        pnl_pct=round(pnl_pct, 2),
                        leverage=lev,
                        won=won,
                        entry_time=e_t,
                        exit_time=float(ts.timestamp()) if hasattr(ts, "timestamp") else 0,
                        exit_reason=exit_reason,
                    ))
                    position   = None
                    trail_peak = 0.0

            # ── エントリーチェック ────────────────────
            if position is not None:
                continue  # 1ポジションのみ

            # ドローダウンチェック
            if peak_balance > 0:
                dd = (peak_balance - balance) / peak_balance
                if dd >= self.config.max_drawdown:
                    continue

            # 連続損失クールダウン（簡易: 3回損失後10本スキップ）
            if consecutive_loss >= self.config.max_consecutive_losses:
                consecutive_loss = 0  # バックテストなので次の機会を待つ
                continue

            # ボラティリティフィルター
            if is_high_volatility(row, self.config):
                continue

            # シグナル評価（バックテストでは1時間軸のみ簡易評価）
            df_slice = df.iloc[max(0, i - self.config.ema_trend - 10): i + 1]
            eval_r   = evaluate_single_timeframe(df_slice, self.config)
            direction = eval_r.get("direction", "neutral")
            score     = max(eval_r.get("long_score", 0), eval_r.get("short_score", 0))

            if direction == "neutral" or score < self.config.min_signal_score:
                continue

            # ATRベースのTP/SL
            atr = row.get("atr", price * 0.01)
            if pd.isna(atr) or atr <= 0:
                atr = price * 0.01

            leverage = self.config.min_leverage + score * (self.config.max_leverage - self.config.min_leverage)
            leverage = max(self.config.min_leverage, min(self.config.max_leverage, leverage))

            if direction == "long":
                tp = price + atr * self.config.tp_atr_mult
                sl = price - atr * self.config.sl_atr_mult
            else:
                tp = price - atr * self.config.tp_atr_mult
                sl = price + atr * self.config.sl_atr_mult

            # リスク1%でポジションサイズ計算（修正版: SL到達時に正確にbalance×max_riskだけ損失）
            # 損失 = size_usd × sl_pct × leverage（PnL計算で×levされるため）
            # balance × max_risk = size_usd × sl_pct × leverage を size_usd について解く
            sl_dist = abs(price - sl)
            sl_pct  = safe_div(sl_dist, price)
            size_usd = safe_div(balance * self.config.max_risk_per_trade, sl_pct * leverage)
            qty     = safe_div(size_usd, price)

            # 手数料考慮のエントリー価格
            entry_price = price * (1 + self.config.commission_rate) if direction == "long" else \
                          price * (1 - self.config.commission_rate)

            position = {
                "side":       direction,
                "entry":      entry_price,
                "tp":         tp,
                "sl":         sl,
                "qty":        qty,
                "lev":        leverage,
                "size":       size_usd,
                "entry_time": float(ts.timestamp()) if hasattr(ts, "timestamp") else 0,
            }
            trail_peak = entry_price

        result.final = balance
        logger.info(
            f"バックテスト完了: {len(result.trades)}件の取引 | "
            f"${initial_balance:,.0f} → ${balance:,.0f} "
            f"({(balance/initial_balance - 1)*100:+.1f}%)"
        )
        return result
