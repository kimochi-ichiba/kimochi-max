"""Dynamic Leverage の MAX 2x 版と別レンジ版を検証"""
import sys
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")
from _quest_for_10pct import _prepare_df, _stats, FEE, SLIP, FUNDING_PH
from config import Config
from data_fetcher import DataFetcher
import pandas as pd
import numpy as np


def dynamic_leverage_custom(fetcher, start, end, levels, initial=10_000.0):
    """
    levels: list of (adx_threshold, leverage) in ascending order
    例: [(20, 1.0), (30, 2.0)] → ADX<20現金, 20-30が1x, 30+が2x
    """
    df = _prepare_df(fetcher, "BTC/USDT", start, end)
    cash = initial
    qty = 0.0
    current_lev = 0.0
    entry_price = 0.0
    entry_ts = None
    equity = []

    def close_pos(date, price):
        nonlocal cash, qty, current_lev, entry_price, entry_ts
        if qty == 0: return
        exit_price = price * (1 - SLIP)
        pnl = qty * (exit_price - entry_price) * current_lev
        notional_exit = qty * exit_price * current_lev
        pnl -= notional_exit * FEE
        hold_h = (date - entry_ts).total_seconds() / 3600
        pnl -= notional_exit * FUNDING_PH * hold_h
        cash += pnl
        qty = 0.0
        current_lev = 0.0

    def open_pos(date, price, lev):
        nonlocal cash, qty, current_lev, entry_price, entry_ts
        entry_price = price * (1 + SLIP)
        qty = cash / entry_price
        notional = cash * lev
        cash -= notional * FEE
        current_lev = lev
        entry_ts = date

    for ts, row in df.iterrows():
        price = row["close"]
        if qty > 0:
            eq = cash + qty * (price - entry_price) * current_lev
        else:
            eq = cash
        equity.append(eq)

        bull = price > row["ema200"] and row["ema50"] > row["ema200"]
        adx = row.get("adx14", np.nan)
        if pd.isna(adx):
            continue

        target_lev = 0.0
        if bull:
            for thr, lev in levels:
                if adx >= thr:
                    target_lev = lev

        if target_lev != current_lev:
            if qty > 0:
                close_pos(ts, price)
            if target_lev > 0:
                open_pos(ts, price, target_lev)

    if qty > 0:
        close_pos(df.index[-1], df["close"].iloc[-1])
    equity.append(cash)
    return _stats(equity, initial)


if __name__ == "__main__":
    fetcher = DataFetcher(Config())

    configs = [
        ("MAX 2x (ADX>20=1x, ADX>30=2x)",
         [(20, 1.0), (30, 2.0)]),
        ("MAX 2x (ADX>25=1x, ADX>35=2x)",  # 保守的
         [(25, 1.0), (35, 2.0)]),
        ("MAX 2.5x (ADX>20=1x, ADX>30=2x, ADX>40=2.5x)",
         [(20, 1.0), (30, 2.0), (40, 2.5)]),
        ("MAX 2x + ADX高い時だけ (ADX>25=1x, ADX>35=1.5x, ADX>45=2x)",
         [(25, 1.0), (35, 1.5), (45, 2.0)]),
        ("Max 3x (オリジナル再現)",
         [(20, 1.0), (30, 2.0), (40, 3.0)]),
    ]

    print(f"\n{'=' * 120}")
    print(f"🎯 Dynamic Leverage チューニング（月+10%とDD低下のバランス）")
    print(f"{'=' * 120}")
    print(f"{'設定':60s} | {'23年':>8s} {'23月':>7s} {'23DD':>6s} | {'24年':>8s} {'24月':>7s} {'24DD':>6s} | 平均月")
    print(f"{'-' * 120}")

    for name, levels in configs:
        try:
            r23 = dynamic_leverage_custom(fetcher, "2023-01-01", "2023-12-31", levels)
            r24 = dynamic_leverage_custom(fetcher, "2024-01-01", "2024-12-31", levels)
            avg = (r23["monthly_avg"] + r24["monthly_avg"]) / 2
            max_dd_both = max(r23["max_dd_pct"], r24["max_dd_pct"])
            tag = ""
            if avg >= 10:
                tag = "🎯10%+"
            elif avg >= 5:
                tag = "⭐5%+"
            safety = "🛡" if max_dd_both <= 50 else ("⚠️" if max_dd_both <= 70 else "🔥")
            print(f"{name:60s} | "
                  f"{r23['total_return_pct']:+7.2f}% {r23['monthly_avg']:+6.2f}% {r23['max_dd_pct']:>5.1f}% | "
                  f"{r24['total_return_pct']:+7.2f}% {r24['monthly_avg']:+6.2f}% {r24['max_dd_pct']:>5.1f}% | "
                  f"{avg:+.2f}% {tag}{safety}")
        except Exception as e:
            print(f"{name:60s} | ERROR: {e}")
