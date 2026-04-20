"""
月+13%を狙う上位変種 - Dynamic Leverageをベースに5手法
1. DL on ETH (ETHの方がボラ高く上昇余地大)
2. DL on SOL (最も高ボラ)
3. BTC MAX 4x Ultra
4. BTC + ETH Dual DL (両方それぞれ動的レバ)
5. DL + 相対強度選択（BTC/ETH/SOLから強い方にDL適用）
"""
import sys, json
from pathlib import Path
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")
import pandas as pd
import numpy as np
from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import validate_ohlcv_data, assert_binance_source
from _quest_for_10pct import _prepare_df, _stats, FEE, SLIP, FUNDING_PH


def dynamic_leverage_on(fetcher, symbol, start, end, levels, initial=10_000.0):
    """任意銘柄へ Dynamic Leverage を適用"""
    df = _prepare_df(fetcher, symbol, start, end)
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
        adx  = row.get("adx14", np.nan)
        if pd.isna(adx):
            continue

        target_lev = 0.0
        if bull:
            for thr, l in levels:
                if adx >= thr:
                    target_lev = l

        if target_lev != current_lev:
            if qty > 0:
                close_pos(ts, price)
            if target_lev > 0:
                open_pos(ts, price, target_lev)

    if qty > 0:
        close_pos(df.index[-1], df["close"].iloc[-1])
    equity.append(cash)
    return _stats(equity, initial)


def dual_dl_btc_eth(fetcher, start, end, levels, initial=10_000.0):
    """
    BTC と ETH に50/50で資金配分し、それぞれ独立に Dynamic Leverage を適用
    """
    dfb = _prepare_df(fetcher, "BTC/USDT", start, end)
    dfe = _prepare_df(fetcher, "ETH/USDT", start, end)
    common = sorted(set(dfb.index) & set(dfe.index))

    cash_b = cash_e = initial / 2
    qty_b = qty_e = 0.0
    lev_b = lev_e = 0.0
    ep_b = ep_e = 0.0
    ts_b = ts_e = None
    equity = []

    def close_side(which, date, price):
        nonlocal cash_b, cash_e, qty_b, qty_e, lev_b, lev_e, ep_b, ep_e, ts_b, ts_e
        if which == "B":
            if qty_b == 0: return
            exit_price = price * (1 - SLIP)
            pnl = qty_b * (exit_price - ep_b) * lev_b
            notional_exit = qty_b * exit_price * lev_b
            pnl -= notional_exit * FEE
            hold_h = (date - ts_b).total_seconds() / 3600
            pnl -= notional_exit * FUNDING_PH * hold_h
            cash_b += pnl
            qty_b = 0.0
            lev_b = 0.0
        else:
            if qty_e == 0: return
            exit_price = price * (1 - SLIP)
            pnl = qty_e * (exit_price - ep_e) * lev_e
            notional_exit = qty_e * exit_price * lev_e
            pnl -= notional_exit * FEE
            hold_h = (date - ts_e).total_seconds() / 3600
            pnl -= notional_exit * FUNDING_PH * hold_h
            cash_e += pnl
            qty_e = 0.0
            lev_e = 0.0

    def open_side(which, date, price, lev):
        nonlocal cash_b, cash_e, qty_b, qty_e, lev_b, lev_e, ep_b, ep_e, ts_b, ts_e
        if which == "B":
            ep_b = price * (1 + SLIP)
            qty_b = cash_b / ep_b
            cash_b -= cash_b * lev * FEE
            lev_b = lev
            ts_b  = date
        else:
            ep_e = price * (1 + SLIP)
            qty_e = cash_e / ep_e
            cash_e -= cash_e * lev * FEE
            lev_e = lev
            ts_e  = date

    for ts in common:
        pb = dfb.loc[ts, "close"]
        pe = dfe.loc[ts, "close"]
        eq_b = cash_b + qty_b * (pb - ep_b) * lev_b if qty_b > 0 else cash_b
        eq_e = cash_e + qty_e * (pe - ep_e) * lev_e if qty_e > 0 else cash_e
        equity.append(eq_b + eq_e)

        # BTC判定
        rb = dfb.loc[ts]
        bull_b = pb > rb["ema200"] and rb["ema50"] > rb["ema200"]
        adx_b  = rb.get("adx14", np.nan)
        if not pd.isna(adx_b):
            target_b = 0.0
            if bull_b:
                for thr, l in levels:
                    if adx_b >= thr:
                        target_b = l
            if target_b != lev_b:
                if qty_b > 0: close_side("B", ts, pb)
                if target_b > 0: open_side("B", ts, pb, target_b)

        # ETH判定
        re = dfe.loc[ts]
        bull_e = pe > re["ema200"] and re["ema50"] > re["ema200"]
        adx_e  = re.get("adx14", np.nan)
        if not pd.isna(adx_e):
            target_e = 0.0
            if bull_e:
                for thr, l in levels:
                    if adx_e >= thr:
                        target_e = l
            if target_e != lev_e:
                if qty_e > 0: close_side("E", ts, pe)
                if target_e > 0: open_side("E", ts, pe, target_e)

    # 清算
    if qty_b > 0: close_side("B", common[-1], dfb["close"].iloc[-1])
    if qty_e > 0: close_side("E", common[-1], dfe["close"].iloc[-1])
    equity.append(cash_b + cash_e)
    return _stats(equity, initial)


def strongest_dl(fetcher, start, end, levels, initial=10_000.0):
    """
    BTC/ETH/SOLから過去30日で最強の1銘柄にDynamic Leverageを適用
    リバランスは2週間毎
    """
    syms = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    dfs = {s: _prepare_df(fetcher, s, start, end) for s in syms}
    common = sorted(set.intersection(*[set(df.index) for df in dfs.values()]))

    cash = initial
    qty = 0.0
    current_sym = None
    current_lev = 0.0
    ep = 0.0
    e_ts = None
    last_rebalance = None
    equity = []

    def close_p(date, price):
        nonlocal cash, qty, current_sym, current_lev, ep, e_ts
        if qty == 0: return
        exit_price = price * (1 - SLIP)
        pnl = qty * (exit_price - ep) * current_lev
        notional_exit = qty * exit_price * current_lev
        pnl -= notional_exit * FEE
        hold_h = (date - e_ts).total_seconds() / 3600
        pnl -= notional_exit * FUNDING_PH * hold_h
        cash += pnl
        qty = 0.0
        current_sym = None
        current_lev = 0.0

    def open_p(date, price, sym, lev):
        nonlocal cash, qty, current_sym, current_lev, ep, e_ts
        ep = price * (1 + SLIP)
        qty = cash / ep
        cash -= cash * lev * FEE
        current_sym = sym
        current_lev = lev
        e_ts = date

    for i, ts in enumerate(common):
        if i < 30:
            equity.append(cash)
            continue

        price_now = dfs[current_sym].loc[ts, "close"] if current_sym else 0
        if qty > 0:
            eq = cash + qty * (price_now - ep) * current_lev
        else:
            eq = cash
        equity.append(eq)

        # 2週間毎
        if last_rebalance is not None and (ts - last_rebalance).days < 14:
            continue

        # BTC がレジーム ON か？
        rb = dfs["BTC/USDT"].loc[ts]
        btc_bull = rb["close"] > rb["ema200"] and rb["ema50"] > rb["ema200"]

        if not btc_bull:
            if qty > 0: close_p(ts, price_now)
            last_rebalance = ts
            continue

        # 各銘柄の30日リターンとADX
        best_sym, best_ret, best_lev = None, -999, 0
        past_ts = common[i - 30]
        for s in syms:
            ret = dfs[s].loc[ts, "close"] / dfs[s].loc[past_ts, "close"] - 1
            adx = dfs[s].loc[ts].get("adx14", np.nan)
            if pd.isna(adx): continue
            lev = 0.0
            for thr, l in levels:
                if adx >= thr:
                    lev = l
            if lev == 0 or ret <= 0:
                continue
            if ret > best_ret:
                best_ret, best_sym, best_lev = ret, s, lev

        if best_sym is None:
            if qty > 0: close_p(ts, price_now)
            last_rebalance = ts
            continue

        # リバランス
        if best_sym != current_sym or best_lev != current_lev:
            if qty > 0: close_p(ts, price_now)
            entry_price_new = dfs[best_sym].loc[ts, "close"]
            open_p(ts, entry_price_new, best_sym, best_lev)

        last_rebalance = ts

    # 最終清算
    if qty > 0 and current_sym:
        fp = dfs[current_sym]["close"].iloc[-1]
        close_p(common[-1], fp)
    equity.append(cash)
    return _stats(equity, initial)


if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)

    STANDARD_LEVELS = [(20, 1.0), (30, 2.0), (40, 3.0)]
    ULTRA_LEVELS    = [(20, 1.0), (30, 2.0), (40, 3.0), (50, 4.0)]

    tests = [
        ("DL on ETH (MAX 3x)",
         lambda s, e: dynamic_leverage_on(fetcher, "ETH/USDT", s, e, STANDARD_LEVELS)),
        ("DL on SOL (MAX 3x)",
         lambda s, e: dynamic_leverage_on(fetcher, "SOL/USDT", s, e, STANDARD_LEVELS)),
        ("DL on BTC MAX 4x Ultra",
         lambda s, e: dynamic_leverage_on(fetcher, "BTC/USDT", s, e, ULTRA_LEVELS)),
        ("Dual DL (BTC 50 + ETH 50)",
         lambda s, e: dual_dl_btc_eth(fetcher, s, e, STANDARD_LEVELS)),
        ("Strongest DL (BTC/ETH/SOL)",
         lambda s, e: strongest_dl(fetcher, s, e, STANDARD_LEVELS)),
    ]

    print(f"\n{'=' * 110}")
    print(f"🚀 月+13%を狙う上位変種（Binance実データ 2023-2024）")
    print(f"{'=' * 110}")
    print(f"{'戦略':35s} | {'23年':>8s} {'23月':>7s} {'23DD':>6s} | {'24年':>8s} {'24月':>7s} {'24DD':>6s} | 平均月")
    print(f"{'-' * 110}")

    all_results = []
    for name, fn in tests:
        try:
            r23 = fn("2023-01-01", "2023-12-31")
            r24 = fn("2024-01-01", "2024-12-31")
            avg = (r23["monthly_avg"] + r24["monthly_avg"]) / 2
            tag = "🎯13%+" if avg >= 13 else ("🥇10%+" if avg >= 10 else "⭐5%+" if avg >= 5 else "")
            dd_max = max(r23["max_dd_pct"], r24["max_dd_pct"])
            safe = "🛡" if dd_max <= 50 else "⚠️" if dd_max <= 75 else "🔥"
            print(f"{name:35s} | "
                  f"{r23['total_return_pct']:+7.2f}% {r23['monthly_avg']:+6.2f}% {r23['max_dd_pct']:>5.1f}% | "
                  f"{r24['total_return_pct']:+7.2f}% {r24['monthly_avg']:+6.2f}% {r24['max_dd_pct']:>5.1f}% | "
                  f"{avg:+.2f}% {tag}{safe}")
            all_results.append({"name": name, "r23": r23, "r24": r24, "avg_month": avg})
        except Exception as e:
            print(f"{name:35s} | ERROR: {e}")

    # ソート結果
    print(f"{'-' * 110}")
    sorted_r = sorted(all_results, key=lambda x: x["avg_month"], reverse=True)
    print(f"\n🏆 ランキング（両年平均月）:")
    for i, r in enumerate(sorted_r, 1):
        print(f"  {i}. {r['name']:35s}  月 {r['avg_month']:+6.2f}%  "
              f"DD最大 {max(r['r23']['max_dd_pct'], r['r24']['max_dd_pct']):.1f}%")

    out = Path("/Users/sanosano/projects/kimochi-max/results/quest_for_13pct.json")
    out.write_text(json.dumps(all_results, indent=2, ensure_ascii=False))
    print(f"\n💾 {out}")
