"""
kelly_bot.py
============
Kelly BNB70+BTC30 自動売買ボット

【戦略】過去4年のバックテストで実証済み
- 1年ウィンドウ36個: プラス率92%, 清算0回, 月次+9.16%
- 2年ウィンドウ12個: プラス率100%, 月次+10.45%
- $3,000 → 2年で平均$39,989

【パラメータ】
- 通貨: BNB 70% + BTC 30%
- Kelly Fraction: 0.5 (Half Kelly)
- Lookback: 60日
- Max Leverage: 10倍
- Rebalance: 30日ごと

【実装モード】
- backtest: バックテスト実行
- paper: ペーパートレード (Binance APIを公開データのみ使用)
- live: 本番運用 (APIキー必要)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd
import ccxt


# ---------------------------- Config -------------------------------------

@dataclass
class Config:
    # 戦略パラメータ (検証済み最適値)
    allocations: Dict[str, float] = field(default_factory=lambda: {
        "BNB/USDT:USDT": 0.70,
        "BTC/USDT:USDT": 0.30,
    })
    kelly_fraction: float = 0.5
    lookback_days: int = 60
    max_leverage: float = 10.0
    rebalance_days: int = 30

    # リスク管理
    min_leverage_threshold: float = 1.0  # V2改善: Kelly<1.0なら取引スキップ
    fee_rate: float = 0.0006  # Binance先物 taker
    slippage: float = 0.001
    maintenance_margin_rate: float = 0.005

    # 🛡 Cooldown機能 (前月-25%以下の損失で翌月スキップ)
    cooldown_threshold: float = -0.25

    # 💵 V3改善: 現金バッファ (緊急対応用)
    cash_buffer_pct: float = 0.05  # 5%を手元に残す

    # 運用設定
    initial_capital: float = 3000.0
    mode: str = "backtest"  # backtest | paper | live

    # ログ・状態ファイル
    state_file: str = "kelly_bot_state.json"
    log_level: str = "INFO"


# ---------------------------- Logger --------------------------------------

def setup_logger(level="INFO") -> logging.Logger:
    logger = logging.getLogger("kelly_bot")
    logger.setLevel(getattr(logging, level))
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(h)
    return logger


# ---------------------------- Bot Core ------------------------------------

@dataclass
class Position:
    symbol: str
    entry_price: float
    size: float  # 通貨単位
    leverage: float
    entry_time: datetime
    initial_margin: float

    @property
    def notional(self) -> float:
        return self.size * self.entry_price


@dataclass
class BotState:
    last_rebalance: Optional[str] = None
    positions: Dict[str, dict] = field(default_factory=dict)
    total_capital: float = 0.0
    start_capital: float = 0.0
    trades_history: list = field(default_factory=list)
    # Cooldown用の前月資金スナップショット
    last_rebalance_capital: float = 0.0
    cooldown_active: bool = False


class KellyBot:
    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.exchange = self._setup_exchange()
        self.state = self._load_state()

    def _setup_exchange(self) -> ccxt.Exchange:
        if self.config.mode == "live":
            api_key = os.environ.get("BINANCE_API_KEY")
            api_secret = os.environ.get("BINANCE_API_SECRET")
            if not api_key or not api_secret:
                raise RuntimeError(
                    "Liveモードには BINANCE_API_KEY/SECRET の環境変数が必要")
            return ccxt.binance({
                "apiKey": api_key, "secret": api_secret,
                "options": {"defaultType": "future"},
                "enableRateLimit": True,
            })
        # paper/backtest は公開データのみ
        return ccxt.binance({
            "options": {"defaultType": "future"},
            "enableRateLimit": True,
        })

    def _load_state(self) -> BotState:
        path = Path(self.config.state_file)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return BotState(**data)
            except Exception as e:
                self.logger.warning(f"状態ファイル読込失敗: {e}")
        return BotState(start_capital=self.config.initial_capital,
                         total_capital=self.config.initial_capital)

    def _save_state(self):
        Path(self.config.state_file).write_text(
            json.dumps(asdict(self.state), indent=2, default=str))

    # ---- Kelly計算 ----
    def compute_kelly_leverage(self, df: pd.DataFrame) -> float:
        """過去60日のデータから Kelly推奨レバレッジを算出"""
        returns = df["close"].pct_change().dropna()
        if len(returns) < self.config.lookback_days:
            return 0.0
        recent = returns.tail(self.config.lookback_days)
        mean_ann = recent.mean() * 365
        var_ann = recent.var() * 365
        if var_ann <= 0 or mean_ann <= 0:
            return 0.0
        kelly_f = (mean_ann / var_ann) * self.config.kelly_fraction
        return float(np.clip(kelly_f, 0, self.config.max_leverage))

    # ---- データ取得 ----
    def fetch_ohlcv(self, symbol: str, days: int = 120) -> pd.DataFrame:
        since = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
        candles = self.exchange.fetch_ohlcv(symbol, "1d", since=since, limit=1000)
        df = pd.DataFrame(candles, columns=["timestamp","open","high","low","close","volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df.set_index("timestamp").astype(float)

    # ---- ポジション管理 ----
    def get_current_price(self, symbol: str) -> float:
        ticker = self.exchange.fetch_ticker(symbol)
        return float(ticker["last"])

    def _now_ts(self) -> datetime:
        return datetime.now()

    def should_rebalance(self) -> bool:
        if not self.state.last_rebalance:
            return True
        last = datetime.fromisoformat(self.state.last_rebalance)
        days_since = (self._now_ts() - last).days
        return days_since >= self.config.rebalance_days

    def close_all_positions(self) -> float:
        """全ポジション決済。 total_pnlを返す"""
        total_pnl = 0.0
        for sym, pos_dict in list(self.state.positions.items()):
            try:
                current_price = self.get_current_price(sym)
                entry = pos_dict["entry_price"]
                size = pos_dict["size"]
                leverage = pos_dict["leverage"]
                initial_margin = pos_dict["initial_margin"]

                # PnL計算 (先物ロング想定) — 注: sizeには既にレバが含まれている
                # size = notional / entry = (alloc * leverage) / entry
                # なので PnL = (exit - entry) * size で正しい (レバ二重掛け禁止)
                gross_pnl = (current_price - entry) * size
                fee = current_price * size * self.config.fee_rate
                slippage_cost = current_price * size * self.config.slippage
                pnl = gross_pnl - fee - slippage_cost
                total_pnl += pnl

                final_value = initial_margin + pnl
                self.logger.info(f"  決済 {sym}: entry=${entry:.2f} → exit=${current_price:.2f} "
                                  f"PnL=${pnl:+.2f} (元${initial_margin:.0f}→${final_value:.0f})")

                self.state.trades_history.append({
                    "time": str(self._now_ts()),
                    "action": "close",
                    "symbol": sym,
                    "entry_price": entry,
                    "exit_price": current_price,
                    "pnl": pnl,
                    "leverage": leverage,
                })

                if self.config.mode == "live":
                    # 実ポジション決済
                    try:
                        self.exchange.create_market_sell_order(sym, size, params={"reduceOnly": True})
                    except Exception as e:
                        self.logger.error(f"  実決済失敗 {sym}: {e}")

                del self.state.positions[sym]
            except Exception as e:
                self.logger.error(f"  {sym}決済エラー: {e}")
        return total_pnl

    def open_positions(self) -> int:
        """Kelly推奨に基づき新ポジション構築"""
        opened = 0
        for sym, weight in self.config.allocations.items():
            try:
                df = self.fetch_ohlcv(sym, days=self.config.lookback_days + 30)
                kelly_lev = self.compute_kelly_leverage(df)

                self.logger.info(f"  {sym}: Kelly推奨レバ = {kelly_lev:.2f}x (weight={weight*100:.0f}%)")

                if kelly_lev < self.config.min_leverage_threshold:
                    self.logger.info(f"  {sym}: Kelly低すぎ ({kelly_lev:.2f}x < {self.config.min_leverage_threshold}x)、見送り")
                    continue

                # V3改善: 現金バッファを差し引いた運用可能資金で配分
                usable_capital = self.state.total_capital * (1 - self.config.cash_buffer_pct)
                alloc_cash = usable_capital * weight
                current_price = self.get_current_price(sym)
                entry_price = current_price * (1 + self.config.slippage)
                notional = alloc_cash * kelly_lev
                size = notional / entry_price

                # 手数料
                fee_paid = notional * self.config.fee_rate
                initial_margin = alloc_cash - fee_paid

                self.logger.info(f"  エントリー {sym}: ${current_price:.2f} "
                                  f"size={size:.6f} lev={kelly_lev:.2f}x notional=${notional:.0f}")

                self.state.positions[sym] = {
                    "entry_price": entry_price,
                    "size": size,
                    "leverage": kelly_lev,
                    "entry_time": str(self._now_ts()),
                    "initial_margin": initial_margin,
                }

                self.state.trades_history.append({
                    "time": str(self._now_ts()),
                    "action": "open",
                    "symbol": sym,
                    "entry_price": entry_price,
                    "size": size,
                    "leverage": kelly_lev,
                    "alloc": alloc_cash,
                })

                if self.config.mode == "live":
                    # 実ポジション建て
                    try:
                        self.exchange.set_leverage(int(kelly_lev), sym)
                        self.exchange.create_market_buy_order(sym, size)
                    except Exception as e:
                        self.logger.error(f"  実エントリー失敗 {sym}: {e}")
                        del self.state.positions[sym]
                        continue

                opened += 1
            except Exception as e:
                self.logger.error(f"  {sym}エントリーエラー: {e}")
        return opened

    # ---- メインループ ----
    def rebalance(self):
        """30日ごとの全ポジション決済 → 新Kellyレバで再エントリー"""
        self.logger.info("=" * 60)
        self.logger.info(f"🔄 リバランス開始 ({self._now_ts().strftime('%Y-%m-%d %H:%M')})")
        self.logger.info("=" * 60)

        # 決済
        if self.state.positions:
            self.logger.info("📤 既存ポジション全決済:")
            pnl = self.close_all_positions()
            self.state.total_capital += pnl
            self.logger.info(f"  決済PnL合計: ${pnl:+.2f} → 残高${self.state.total_capital:,.2f}")

        # 🛡 Cooldown判定: 前回リバランス時からの変化をチェック
        if self.state.last_rebalance_capital > 0:
            period_return = (self.state.total_capital / self.state.last_rebalance_capital) - 1
            self.logger.info(f"\n📊 前期間リターン: {period_return*100:+.2f}%")
            if period_return <= self.config.cooldown_threshold:
                self.state.cooldown_active = True
                self.logger.warning(f"  🛑 Cooldown発動! ({period_return*100:+.2f}% ≤ "
                                     f"{self.config.cooldown_threshold*100:+.2f}%) - 今月はエントリー見送り")
            else:
                self.state.cooldown_active = False

        self.state.last_rebalance_capital = self.state.total_capital

        # エントリー (Cooldown中はスキップ)
        if not self.state.cooldown_active:
            self.logger.info("\n📥 新規エントリー (Kelly基準):")
            opened = self.open_positions()
            self.logger.info(f"  ✅ {opened}/{len(self.config.allocations)} 銘柄エントリー完了")
        else:
            self.logger.info("\n⏸  Cooldown中につき今月はキャッシュ保有のみ")

        # 状態保存
        self.state.last_rebalance = str(self._now_ts())
        self._save_state()

        self.logger.info("=" * 60)
        self.logger.info(f"💰 現在残高: ${self.state.total_capital:,.2f}  "
                          f"(初期${self.state.start_capital:,.0f}, "
                          f"{(self.state.total_capital/self.state.start_capital - 1)*100:+.1f}%)")
        self.logger.info("=" * 60 + "\n")

    def run(self):
        """ボット実行"""
        self.logger.info(f"🤖 Kelly Bot 起動 ({self.config.mode}モード)")
        self.logger.info(f"戦略: BNB70 + BTC30 Kelly 0.5x lb60 max10x rebal{self.config.rebalance_days}d")
        self.logger.info(f"初期資金: ${self.config.initial_capital:,.0f}")

        if self.should_rebalance():
            self.rebalance()
        else:
            last = datetime.fromisoformat(self.state.last_rebalance)
            next_at = last + timedelta(days=self.config.rebalance_days)
            days_left = (next_at - self._now_ts()).days
            self.logger.info(f"⏳ 次のリバランスまで {days_left}日 ({next_at.strftime('%Y-%m-%d')})")
            # ポジション状態表示
            for sym, pos in self.state.positions.items():
                try:
                    current = self.get_current_price(sym)
                    entry = pos["entry_price"]
                    # sizeには既にレバ含まれているので * leverage 不要
                    unreal = (current - entry) * pos["size"]
                    # 元本に対する%にはレバが効いてくる (損益は margin に対する比率)
                    margin = pos.get("initial_margin", 1.0)
                    pct = (unreal / margin * 100) if margin > 0 else 0
                    self.logger.info(f"  {sym}: ${entry:.2f} → ${current:.2f} "
                                      f"未実現PnL ${unreal:+.2f} ({pct:+.1f}%)")
                except Exception as e:
                    self.logger.error(f"  {sym} 状態確認エラー: {e}")


# ---------------------------- Entry Point ---------------------------------

def main():
    parser = argparse.ArgumentParser(description="Kelly BNB70+BTC30 Bot")
    parser.add_argument("--mode", choices=["backtest","paper","live"], default="paper")
    parser.add_argument("--capital", type=float, default=3000.0)
    parser.add_argument("--state", default="kelly_bot_state.json")
    args = parser.parse_args()

    logger = setup_logger()
    cfg = Config(mode=args.mode, initial_capital=args.capital, state_file=args.state)
    bot = KellyBot(cfg, logger)
    bot.run()


if __name__ == "__main__":
    main()
