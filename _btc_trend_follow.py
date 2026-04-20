"""
BTCトレンドフォロー（最もシンプルな戦略）
- BTC が日足 EMA200 より上 → BTC保有
- 下 → 現金
- レバ1倍、月次確認
- 2023・2024両年で検証
"""
import sys, json
from pathlib import Path
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")
import pandas as pd
import numpy as np
from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import validate_ohlcv_data, assert_binance_source

FEE = 0.0006
SLIP = 0.0003


def run_btc_trend(start: str, end: str, initial: float = 10_000.0) -> dict:
    cfg = Config()
    fetcher = DataFetcher(cfg)
    assert_binance_source(fetcher)

    buf_start = (pd.Timestamp(start) - pd.Timedelta(days=250)).strftime("%Y-%m-%d")
    df = fetcher.fetch_historical_ohlcv("BTC/USDT", "1d", buf_start, end)
    validate_ohlcv_data(df, "BTC/USDT", "1d")
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df = df[df.index >= pd.Timestamp(start)]

    cash = initial
    btc_qty = 0.0
    in_btc = False
    trades = []
    equity = []

    for date, row in df.iterrows():
        price = row["close"]
        if in_btc:
            current_equity = btc_qty * price
        else:
            current_equity = cash
        equity.append(current_equity)

        want_in = row["close"] > row["ema200"]
        if want_in and not in_btc:
            # 買い: スリッページ+手数料
            entry_price = price * (1 + SLIP)
            btc_qty = cash * (1 - FEE) / entry_price
            trades.append({"ts": str(date.date()), "action": "buy",
                           "price": round(entry_price, 2), "qty": round(btc_qty, 6)})
            cash = 0.0
            in_btc = True
        elif not want_in and in_btc:
            exit_price = price * (1 - SLIP)
            cash = btc_qty * exit_price * (1 - FEE)
            trades.append({"ts": str(date.date()), "action": "sell",
                           "price": round(exit_price, 2), "cash": round(cash, 2)})
            btc_qty = 0.0
            in_btc = False

    # 最終清算
    if in_btc:
        final = btc_qty * df["close"].iloc[-1] * (1 - SLIP) * (1 - FEE)
    else:
        final = cash

    eq_s = pd.Series(equity, index=df.index)
    monthly = eq_s.resample("M").last().pct_change().dropna() * 100
    peak = eq_s.cummax()
    max_dd = ((peak - eq_s) / peak).max() * 100

    return {
        "start": start, "end": end,
        "initial": initial, "final": round(final, 2),
        "total_return_pct": round((final - initial) / initial * 100, 2),
        "monthly_avg": round(monthly.mean(), 2) if len(monthly) else 0,
        "monthly_std": round(monthly.std(ddof=0), 2) if len(monthly) else 0,
        "win_rate_pct": round((monthly > 0).sum() / max(len(monthly), 1) * 100, 1),
        "positive_months": int((monthly > 0).sum()),
        "total_months": len(monthly),
        "max_dd_pct": round(max_dd, 2),
        "n_trades": len(trades),
        "trades": trades,
    }


if __name__ == "__main__":
    print("🐂 BTC単体トレンドフォロー検証 (EMA200ベース)")
    print("=" * 70)
    for start, end in [("2023-01-01", "2023-12-31"),
                       ("2024-01-01", "2024-12-31"),
                       ("2023-01-01", "2024-12-31")]:
        r = run_btc_trend(start, end)
        # Buy&Hold比較
        fetcher = DataFetcher(Config())
        df = fetcher.fetch_historical_ohlcv("BTC/USDT", "1d", start, end)
        bh = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        print(f"\n  期間 {start} 〜 {end}")
        print(f"    BTC Trend Follow: {r['total_return_pct']:+7.2f}%  "
              f"月平均 {r['monthly_avg']:+.2f}%  勝率 {r['win_rate_pct']}%  "
              f"DD {r['max_dd_pct']:.2f}%  取引 {r['n_trades']}件")
        print(f"    BTC Buy & Hold:   {bh:+7.2f}%  "
              f"(差 {r['total_return_pct']-bh:+.2f}pp)")
