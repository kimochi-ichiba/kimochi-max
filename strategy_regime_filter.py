"""
strategy_regime_filter.py
=========================
市場環境フィルター版: v95.0 + BTC Trend Regime Filter + 10通貨

アプローチ:
- 各ウィンドウで、BTCの4h足 ADX と 日足EMA状態を評価
- BTC ADX >= 25 AND 日足トレンド明確 の場合のみ v95.0 取引を許可
- それ以外のウィンドウは「取引なし(0%)」として扱う
- これによって「負ける月を回避」する効果を測定

実装方式:
- 既存の v95.0 Backtester を流用
- ウィンドウごとに BTC データから regime を判定
- regime OK → バックテスト実行、regime NG → スキップ(0%)
- 既存の10通貨バックテスト結果と比較
"""

from __future__ import annotations

from pathlib import Path
import sys
import logging
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config
from backtester import Backtester
from data_fetcher import DataFetcher

logging.getLogger("data_fetcher").setLevel(logging.WARNING)
logging.getLogger("backtester").setLevel(logging.WARNING)

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "ADA/USDT",
    "LINK/USDT", "AVAX/USDT", "DOT/USDT", "UNI/USDT", "NEAR/USDT"
]
WINDOW_DAYS = 30
STEP_DAYS = 15
HISTORY_DAYS = 365
INITIAL_BALANCE = 10_000.0

ADX_PERIOD = 14
ADX_THRESHOLD = 25.0
EMA_SHORT = 50
EMA_LONG = 200


def compute_btc_regime(btc_4h: pd.DataFrame, btc_1d: pd.DataFrame,
                        window_start: pd.Timestamp, window_end: pd.Timestamp) -> dict:
    """BTCの4h ADX と 日足EMA から window開始時点の regime を判定"""
    # ウィンドウ開始直前のBTC状態を評価
    close_4h = btc_4h["close"]
    high_4h, low_4h = btc_4h["high"], btc_4h["low"]

    tr = pd.concat([
        high_4h - low_4h,
        (high_4h - close_4h.shift()).abs(),
        (low_4h - close_4h.shift()).abs(),
    ], axis=1).max(axis=1)

    up = high_4h.diff()
    down = -low_4h.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr_adx = tr.rolling(ADX_PERIOD).mean()
    plus_di = 100 * pd.Series(plus_dm, index=btc_4h.index).rolling(ADX_PERIOD).mean() / atr_adx
    minus_di = 100 * pd.Series(minus_dm, index=btc_4h.index).rolling(ADX_PERIOD).mean() / atr_adx
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.rolling(ADX_PERIOD).mean()

    # window_start直前のADX値
    pre_window = adx[adx.index < window_start]
    if pre_window.empty:
        adx_val = np.nan
    else:
        adx_val = pre_window.iloc[-1]

    # 日足EMA状態
    ema_s = btc_1d["close"].ewm(span=EMA_SHORT, adjust=False).mean()
    ema_l = btc_1d["close"].ewm(span=EMA_LONG, adjust=False).mean()
    pre_daily = btc_1d[btc_1d.index < window_start]
    if pre_daily.empty:
        trend_val = 0
    else:
        last_idx = pre_daily.index[-1]
        s = ema_s.loc[last_idx]
        l = ema_l.loc[last_idx]
        if pd.isna(s) or pd.isna(l):
            trend_val = 0
        elif s > l * 1.02:
            trend_val = 1   # 強い上昇トレンド
        elif s < l * 0.98:
            trend_val = -1  # 強い下降トレンド
        else:
            trend_val = 0

    regime_ok = (not pd.isna(adx_val) and adx_val >= ADX_THRESHOLD and trend_val != 0)
    return {
        "adx": adx_val if not pd.isna(adx_val) else 0,
        "trend": trend_val,
        "regime_ok": regime_ok,
    }


def run_symbol(symbol: str, start: str, end: str) -> dict:
    cfg = Config()
    bt = Backtester(cfg)
    r = bt.run(symbol, start, end, timeframe="1h", initial_balance=INITIAL_BALANCE)
    if not r.trades:
        return {"pnl": 0.0, "final": INITIAL_BALANCE, "pnl_pct": 0.0, "trades": 0}
    return {
        "pnl": r.final - r.initial,
        "final": r.final,
        "pnl_pct": (r.final / r.initial - 1) * 100,
        "trades": len(r.trades),
    }


def main():
    end_date = datetime(2026, 4, 18)
    buffer_start = end_date - timedelta(days=HISTORY_DAYS + 90)
    analysis_start = end_date - timedelta(days=HISTORY_DAYS)

    print(f"\n🔬 市場環境フィルター版 — 10通貨・12ヶ月検証")
    print(f"{'='*100}")
    print(f"期間: {analysis_start.strftime('%Y-%m-%d')} 〜 {end_date.strftime('%Y-%m-%d')}")
    print(f"フィルター条件: BTC 4h ADX >= {ADX_THRESHOLD} かつ 日足EMA{EMA_SHORT}/{EMA_LONG}で方向明確")
    print(f"{'='*100}")

    # BTCデータを先に取得（regime判定用）
    cfg = Config()
    fetcher = DataFetcher(cfg)
    print(f"📥 BTC regime判定データ取得中...")
    btc_4h = fetcher.fetch_historical_ohlcv(
        "BTC/USDT", "4h",
        buffer_start.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
    )
    btc_1d = fetcher.fetch_historical_ohlcv(
        "BTC/USDT", "1d",
        buffer_start.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
    )

    # ウィンドウ生成
    windows = []
    cursor = analysis_start
    while cursor + timedelta(days=WINDOW_DAYS) <= end_date:
        w_s = cursor
        w_e = cursor + timedelta(days=WINDOW_DAYS)
        windows.append((w_s, w_e))
        cursor += timedelta(days=STEP_DAYS)

    print(f"\n  {'Win#':4s} {'期間':30s} {'ADX':>5s} {'日足':>5s} {'Regime':>8s} {'リターン':>10s} {'取引数':>7s}")
    print(f"  {'-'*85}")

    filtered_returns = []
    all_returns = []
    skipped = 0

    for i, (w_s, w_e) in enumerate(windows, 1):
        regime = compute_btc_regime(btc_4h, btc_1d,
                                     pd.Timestamp(w_s), pd.Timestamp(w_e))
        start_str = w_s.strftime("%Y-%m-%d")
        end_str = w_e.strftime("%Y-%m-%d")
        regime_str = "✅ OK" if regime["regime_ok"] else "❌ NG"
        trend_str = {1: "Bull", -1: "Bear", 0: "Chop"}[regime["trend"]]

        if regime["regime_ok"]:
            # 10通貨でバックテスト実行
            per_sym = {}
            for sym in SYMBOLS:
                per_sym[sym] = run_symbol(sym, start_str, end_str)
            total_pnl = sum(r["pnl"] for r in per_sym.values())
            port_ret = total_pnl / (INITIAL_BALANCE * len(SYMBOLS)) * 100
            total_trades = sum(r["trades"] for r in per_sym.values())
            filtered_returns.append(port_ret)
            all_returns.append(port_ret)
            print(f"  [{i:2d}/{len(windows)}] {start_str} 〜 {end_str}  "
                  f"{regime['adx']:5.1f} {trend_str:>5s} {regime_str:>8s} "
                  f"{port_ret:+8.2f}% {total_trades:6d}")
        else:
            all_returns.append(0.0)  # スキップ = 0%
            skipped += 1
            print(f"  [{i:2d}/{len(windows)}] {start_str} 〜 {end_str}  "
                  f"{regime['adx']:5.1f} {trend_str:>5s} {regime_str:>8s} "
                  f"{'スキップ':>9s} {'-':>6s}")

    print(f"\n{'='*100}")
    print(f"  📊 市場環境フィルター版 集計結果")
    print(f"{'='*100}")
    print(f"  総ウィンドウ数             : {len(windows)}")
    print(f"  稼働月                     : {len(filtered_returns)} ({len(filtered_returns)/len(windows)*100:.0f}%)")
    print(f"  スキップ月                 : {skipped} ({skipped/len(windows)*100:.0f}%)")
    if filtered_returns:
        filt = np.array(filtered_returns)
        print(f"  稼働月のみ平均リターン     : {np.mean(filt):+.2f}%")
        print(f"  稼働月の勝率               : {sum(1 for r in filt if r > 0)/len(filt)*100:.0f}%")
        print(f"  稼働月の最高               : {np.max(filt):+.2f}%")
        print(f"  稼働月の最低               : {np.min(filt):+.2f}%")
    all_arr = np.array(all_returns)
    print(f"  全期間平均(スキップ=0%)    : {np.mean(all_arr):+.2f}%")

    # 複利シミュレーション
    balance = 100000.0
    for r in all_arr:
        balance *= (1 + r / 100)
    annual_compound = (balance / 100000 - 1) * 100
    months = len(all_arr) / 2.0  # 15日刻みなので月換算は半分
    monthly_comp = ((balance / 100000) ** (1 / months) - 1) * 100 if balance > 0 else -100

    print(f"\n  💰 複利シミュレーション ($100,000スタート)")
    print(f"  {'-'*60}")
    print(f"  最終残高                   : ${balance:,.0f}")
    print(f"  総リターン (1年相当)        : {annual_compound:+.2f}%")
    print(f"  月次平均 (複利)             : {monthly_comp:+.2f}%")

    # 比較
    print(f"\n  🏆 全戦略比較 (平均月次)")
    print(f"  {'='*60}")
    print(f"  {'Buy&Hold':<25s}: {'+0.41%':>8s}  勝率 57%")
    print(f"  {'v95.0 (現行)':<25s}: {'-11.92%':>8s}  勝率 22%")
    print(f"  {'v95.0 10通貨版':<25s}: {'-9.70%':>8s}  勝率 30%")
    print(f"  {'v95.0 + Regime Filter':<25s}: {f'{np.mean(all_arr):+.2f}%':>8s}  "
          f"稼働率 {len(filtered_returns)/len(windows)*100:.0f}%")
    print(f"  {'='*60}\n")


if __name__ == "__main__":
    main()
