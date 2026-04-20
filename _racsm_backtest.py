"""
_racsm_backtest.py — RACSM戦略のポートフォリオ型バックテスター
==============================================================
本番Binanceデータのみ使用（合成データ混入時RuntimeError）
ハルシネーション対策のデータ健全性検証を全OHLCVに適用。

コスト反映（修正済みロジック）:
- 手数料: notional × fee_rate × 2 (往復)
- スリッページ: エントリー/決済両方 × slippage_rate
- Funding: notional × funding_rate_per_hour × hold_hours
"""

from __future__ import annotations

import sys
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import numpy as np

from config import Config
from data_fetcher import DataFetcher
from strategy_racsm import (
    UNIVERSE, LEVERAGE, TOP_N,
    check_btc_regime, compute_momentum_scores, apply_absolute_momentum,
    select_top_n, compute_inverse_vol_weights, check_portfolio_stop,
    Position,
)

logging.getLogger("data_fetcher").setLevel(logging.WARNING)

FEE_RATE   = 0.0006      # Binance Futures taker 0.06%
SLIPPAGE   = 0.0003      # 想定スリッページ 0.03%
FUNDING_PH = 0.0000125   # Funding 0.00125%/h ≒ 0.03%/日


# ═══ ハルシネーション対策: データ健全性検証 ═══
def validate_ohlcv_data(df: pd.DataFrame, symbol: str, timeframe: str = "1d") -> None:
    """
    データの健全性を検証。異常検出時は RuntimeError で即停止。
    """
    if df is None or df.empty:
        raise RuntimeError(f"データ健全性NG: {symbol} - 空DataFrame")

    ohlc_cols = ["open", "high", "low", "close"]
    if (df[ohlc_cols] <= 0).any().any():
        bad = df[(df[ohlc_cols] <= 0).any(axis=1)].head(3)
        raise RuntimeError(f"データ健全性NG: {symbol} - 価格≤0 検出\n{bad}")

    if df[ohlc_cols].isna().any().any():
        raise RuntimeError(f"データ健全性NG: {symbol} - NaN検出")

    if timeframe == "1d":
        diffs = df.index.to_series().diff().dropna()
        expected = pd.Timedelta(days=1)
        weird = (diffs != expected).sum()
        if weird / max(len(diffs), 1) > 0.02:
            raise RuntimeError(
                f"データ健全性NG: {symbol} - タイムスタンプ欠損 {weird}件"
            )

    zero_vol = (df["volume"] <= 0).sum()
    if zero_vol / len(df) > 0.05:
        raise RuntimeError(
            f"データ健全性NG: {symbol} - ゼロ出来高 {zero_vol}/{len(df)}"
        )

    if df["close"].max() / df["close"].min() > 1000:
        raise RuntimeError(
            f"データ健全性NG: {symbol} - 異常な価格変動(最大/最小>1000倍)"
        )


def assert_binance_source(fetcher: DataFetcher) -> None:
    """
    Binance以外のデータソースを即拒否。
    """
    exch_id = getattr(fetcher._exchange, "id", None)
    if exch_id != "binance":
        raise RuntimeError(
            f"本番データ以外は使用禁止: exchange={exch_id}"
        )


# ═══ データ一括取得 ═══
def fetch_universe_data(fetcher: DataFetcher,
                         start: str, end: str,
                         timeframe: str = "1d",
                         buffer_days: int = 120) -> dict[str, pd.DataFrame]:
    """
    UNIVERSE の全銘柄の日足OHLCVを取得。ルックバック用に buffer_days 分 余分に取る。
    """
    assert_binance_source(fetcher)
    buf_start = (datetime.fromisoformat(start) - timedelta(days=buffer_days)).strftime("%Y-%m-%d")

    data = {}
    skipped = []
    for sym in UNIVERSE:
        try:
            df = fetcher.fetch_historical_ohlcv(sym, timeframe, buf_start, end)
            if df.empty:
                skipped.append(sym)
                continue
            validate_ohlcv_data(df, sym, timeframe)
            data[sym] = df
        except RuntimeError as e:
            print(f"  ⚠️ {sym}: {e}")
            skipped.append(sym)
        except Exception as e:
            print(f"  ⚠️ {sym}: 取得エラー {e}")
            skipped.append(sym)

    if skipped:
        print(f"  📋 スキップ銘柄: {len(skipped)} / {len(UNIVERSE)}")
    if len(data) < 3:
        raise RuntimeError(f"取得銘柄が少なすぎます: {len(data)}銘柄のみ")
    return data


# ═══ バックテスト本体 ═══
def run_racsm_backtest(start: str, end: str,
                       initial_capital: float = 10_000.0,
                       rebalance_days: int = 7) -> dict:
    """
    RACSM のバックテストを実行。
    """
    cfg = Config()
    fetcher = DataFetcher(cfg)
    assert_binance_source(fetcher)

    print(f"\n🔬 RACSM バックテスト: {start} 〜 {end}")
    print(f"   初期資金 ${initial_capital:,.0f}, レバ{LEVERAGE}x, Top{TOP_N}銘柄, "
          f"リバランス{rebalance_days}日毎")
    print(f"   手数料 {FEE_RATE*100:.3f}% / スリッページ {SLIPPAGE*100:.3f}% / "
          f"Funding {FUNDING_PH*24*100:.3f}%/日")

    ohlcv = fetch_universe_data(fetcher, start, end)
    if "BTC/USDT" not in ohlcv:
        raise RuntimeError("BTC/USDTが取得できません。レジーム判定不可")
    btc_df = ohlcv["BTC/USDT"]

    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)
    all_dates = sorted(set.intersection(
        *[set(df.index.tolist()) for df in ohlcv.values()]
    ))
    all_dates = [d for d in all_dates if start_ts <= d <= end_ts]
    if not all_dates:
        raise RuntimeError("検証期間内のデータがありません")

    capital = initial_capital
    positions: dict[str, Position] = {}
    equity_curve = [initial_capital]
    equity_dates = [all_dates[0]]
    trades = []
    last_rebalance: Optional[pd.Timestamp] = None
    stopped_out = False
    stop_cooldown_days = 0
    peak_since_entry = initial_capital  # リセット可能なピーク

    for i, date in enumerate(all_dates):
        # 1. 現在のmark-to-market評価額を計算
        mtm_unrealized = 0.0
        for sym, pos in positions.items():
            if date in ohlcv[sym].index:
                price = ohlcv[sym].loc[date, "close"]
                mtm_unrealized += pos.pnl_at(price)
        current_equity = capital + mtm_unrealized
        equity_curve.append(current_equity)
        equity_dates.append(date)

        # 2. 撤退チェック（保有中のみ・peak_since_entry 基準でDD計測）
        if positions:
            peak_since_entry = max(peak_since_entry, current_equity)
            dd_from_peak = (peak_since_entry - current_equity) / peak_since_entry if peak_since_entry > 0 else 0
            should_stop = False
            reason = ""
            if dd_from_peak >= 0.10:
                should_stop = True
                reason = f"portfolio_dd_{dd_from_peak*100:.1f}pct"
            else:
                btc_view = btc_df[btc_df.index <= date]
                if len(btc_view) >= 205:
                    ema200 = btc_view["close"].ewm(span=200, adjust=False).mean().iloc[-1]
                    if btc_view["close"].iloc[-1] < ema200:
                        should_stop = True
                        reason = "btc_below_ema200"
            if should_stop:
                print(f"  🛑 {date.date()}: 撤退 ({reason})")
                capital = _close_all_positions(positions, ohlcv, date, capital, trades)
                positions = {}
                stopped_out = True
                stop_cooldown_days = 14
                peak_since_entry = capital  # 次エントリー時点をピーク初期値に

        if stop_cooldown_days > 0:
            stop_cooldown_days -= 1
            continue

        # 3. リバランス判定
        need_rebalance = False
        if last_rebalance is None:
            need_rebalance = True
        elif (date - last_rebalance).days >= rebalance_days:
            need_rebalance = True

        if not need_rebalance:
            continue

        # 4. BTCレジーム確認
        if not check_btc_regime(btc_df, date):
            if positions:
                capital = _close_all_positions(positions, ohlcv, date, capital, trades)
                positions = {}
            last_rebalance = date
            continue

        # 5. モメンタムスコア計算
        scores = compute_momentum_scores(ohlcv, date)
        if scores.empty:
            last_rebalance = date
            continue
        scores = apply_absolute_momentum(scores, ohlcv, date)
        if scores.empty:
            if positions:
                capital = _close_all_positions(positions, ohlcv, date, capital, trades)
                positions = {}
            last_rebalance = date
            continue

        target_syms = select_top_n(scores, TOP_N)
        weights = compute_inverse_vol_weights(ohlcv, target_syms, date)
        if not weights:
            last_rebalance = date
            continue

        # 6. 既存ポジション全クローズ → 新配分でエントリー
        if positions:
            capital = _close_all_positions(positions, ohlcv, date, capital, trades)
            positions = {}

        for sym, w in weights.items():
            if sym not in ohlcv or date not in ohlcv[sym].index:
                continue
            price_close = ohlcv[sym].loc[date, "close"]
            entry_with_slip = price_close * (1 + SLIPPAGE)
            alloc = capital * w
            notional = alloc * LEVERAGE
            qty = notional / entry_with_slip / LEVERAGE  # qty = alloc / entry
            # 手数料: 入口分
            capital -= notional * FEE_RATE
            positions[sym] = Position(
                symbol=sym, entry_price=entry_with_slip,
                qty=qty, entry_ts=date, target_weight=w,
            )

        last_rebalance = date
        # 新規エントリー時: peak をその時点の equity にリセット
        unreal = sum(
            p.pnl_at(ohlcv[s].loc[date, "close"])
            for s, p in positions.items()
            if date in ohlcv[s].index
        )
        peak_since_entry = capital + unreal

    # 最終クローズ
    final_date = all_dates[-1]
    if positions:
        capital = _close_all_positions(positions, ohlcv, final_date, capital, trades)

    final_equity = capital
    total_return_pct = (final_equity - initial_capital) / initial_capital * 100

    # 月次リターン計算
    eq_series = pd.Series(equity_curve, index=equity_dates)
    monthly = eq_series.resample("M").last().pct_change().dropna() * 100
    max_dd = _calc_max_dd(equity_curve)

    result = {
        "start": start, "end": end,
        "initial": initial_capital,
        "final":   round(final_equity, 2),
        "total_return_pct": round(total_return_pct, 2),
        "monthly_returns": {str(k.date()): round(v, 2) for k, v in monthly.items()},
        "monthly_avg": round(monthly.mean(), 2) if len(monthly) else 0.0,
        "monthly_median": round(monthly.median(), 2) if len(monthly) else 0.0,
        "monthly_std": round(monthly.std(ddof=0), 2) if len(monthly) else 0.0,
        "positive_months": int((monthly > 0).sum()),
        "total_months": len(monthly),
        "win_rate_pct": round((monthly > 0).sum() / max(len(monthly), 1) * 100, 1),
        "max_dd_pct": round(max_dd, 2),
        "n_trades": len(trades),
        "stopped_out_times": sum(1 for t in trades if t.get("reason") == "stop"),
    }
    return result


def _close_all_positions(positions: dict[str, Position],
                          ohlcv: dict[str, pd.DataFrame],
                          date: pd.Timestamp,
                          capital: float,
                          trades: list) -> float:
    """全ポジションをクローズして現金化、capitalを返す"""
    for sym, pos in positions.items():
        if sym not in ohlcv or date not in ohlcv[sym].index:
            continue
        price_close = ohlcv[sym].loc[date, "close"]
        exit_with_slip = price_close * (1 - SLIPPAGE)
        pnl_gross = pos.pnl_at(exit_with_slip)
        notional = pos.qty * exit_with_slip * LEVERAGE
        # 決済手数料
        pnl_gross -= notional * FEE_RATE
        # Funding
        hold_hours = (date - pos.entry_ts).total_seconds() / 3600.0
        pnl_gross -= notional * FUNDING_PH * hold_hours
        capital += pnl_gross
        trades.append({
            "symbol": sym,
            "entry": pos.entry_price,
            "exit":  exit_with_slip,
            "qty":   pos.qty,
            "pnl":   round(pnl_gross, 2),
            "hold_days": round(hold_hours / 24, 1),
            "entry_ts": str(pos.entry_ts.date()),
            "exit_ts":  str(date.date()),
        })
    return capital


def _calc_max_dd(equity_curve: list[float]) -> float:
    """最大ドローダウン (%) を計算"""
    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return max_dd


# ═══ エントリーポイント ═══
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end",   default="2024-12-31")
    ap.add_argument("--rebalance", type=int, default=7, help="リバランス間隔(日)")
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--out", default=None, help="結果JSON保存先")
    args = ap.parse_args()

    result = run_racsm_backtest(
        args.start, args.end,
        initial_capital=args.capital,
        rebalance_days=args.rebalance,
    )

    print("\n" + "═" * 70)
    print(f"📊 RACSM 結果 ({args.start} 〜 {args.end}, リバランス{args.rebalance}日)")
    print("═" * 70)
    print(f"   初期 ${result['initial']:,.0f} → 最終 ${result['final']:,.2f}  "
          f"({result['total_return_pct']:+.2f}%)")
    print(f"   月次平均: {result['monthly_avg']:+.2f}%  中央値: {result['monthly_median']:+.2f}%  "
          f"標準偏差: {result['monthly_std']:.2f}%")
    print(f"   プラス月: {result['positive_months']}/{result['total_months']}  "
          f"勝率 {result['win_rate_pct']}%")
    print(f"   最大DD: {result['max_dd_pct']:.2f}%  取引数: {result['n_trades']}件")
    print("═" * 70)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"\n💾 結果を保存: {args.out}")


if __name__ == "__main__":
    main()
