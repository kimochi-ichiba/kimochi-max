"""
さらなる戦略探索 - 未試行の4手法
1. BTC 2x レバレッジトレンドフォロー（強気時のみ倍賭け）
2. マルチアセット トレンドフォロー（BTC + ETH 各自EMA200判定）
3. プルバックバイヤー（EMA200上昇中のEMA50までの押し目買い）
4. ゴールデン/デッドクロス（EMA50 × EMA200）
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
FUNDING_PH = 0.0000125


def _prepare_df(fetcher, symbol, start, end, buf_days=300):
    buf_start = (pd.Timestamp(start) - pd.Timedelta(days=buf_days)).strftime("%Y-%m-%d")
    df = fetcher.fetch_historical_ohlcv(symbol, "1d", buf_start, end)
    validate_ohlcv_data(df, symbol, "1d")
    df["ema50"]  = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["ema20"]  = df["close"].ewm(span=20, adjust=False).mean()
    return df[df.index >= pd.Timestamp(start)]


def _stats(equity, initial):
    eq_s = pd.Series(equity)
    peak = eq_s.cummax()
    dd = ((peak - eq_s) / peak).max() * 100
    monthly = []
    for i in range(30, len(equity), 30):
        r = equity[i] / equity[i-30] - 1
        monthly.append(r * 100)
    monthly_avg = np.mean(monthly) if monthly else 0
    win = sum(1 for m in monthly if m > 0) / len(monthly) * 100 if monthly else 0
    return {
        "total_return_pct": round((equity[-1] - initial) / initial * 100, 2),
        "monthly_avg": round(monthly_avg, 2),
        "win_rate_pct": round(win, 1),
        "max_dd_pct": round(dd, 2),
    }


# ─── 戦略1: BTC 2x レバレッジトレンドフォロー ───
def btc_leveraged_trend(fetcher, start, end, leverage=2.0, initial=10_000.0):
    df = _prepare_df(fetcher, "BTC/USDT", start, end)
    cash = initial
    qty = 0.0
    in_pos = False
    entry_ts = None
    equity = []

    for ts, row in df.iterrows():
        price = row["close"]
        if in_pos:
            eq = cash + qty * (price - entry_price) * leverage
        else:
            eq = cash
        equity.append(eq)

        bullish = price > row["ema200"] and row["ema50"] > row["ema200"]
        if bullish and not in_pos:
            entry_price = price * (1 + SLIP)
            notional = cash * leverage
            qty = notional / entry_price / leverage  # qty = cash / entry
            cash -= notional * FEE
            entry_ts = ts
            in_pos = True
        elif not bullish and in_pos:
            exit_price = price * (1 - SLIP)
            pnl = qty * (exit_price - entry_price) * leverage
            notional_exit = qty * exit_price * leverage
            pnl -= notional_exit * FEE
            hold_hours = (ts - entry_ts).total_seconds() / 3600
            pnl -= notional_exit * FUNDING_PH * hold_hours
            cash += pnl
            qty = 0.0
            in_pos = False

    if in_pos:
        final_price = df["close"].iloc[-1] * (1 - SLIP)
        pnl = qty * (final_price - entry_price) * leverage
        notional_exit = qty * final_price * leverage
        pnl -= notional_exit * FEE
        hold_hours = (df.index[-1] - entry_ts).total_seconds() / 3600
        pnl -= notional_exit * FUNDING_PH * hold_hours
        cash += pnl
    equity.append(cash)
    return _stats(equity, initial)


# ─── 戦略2: マルチアセット（BTC+ETH）トレンドフォロー ───
def multi_asset_trend(fetcher, start, end, initial=10_000.0):
    symbols = ["BTC/USDT", "ETH/USDT"]
    dfs = {s: _prepare_df(fetcher, s, start, end) for s in symbols}

    # 日付の交集合
    common = sorted(set.intersection(*[set(df.index) for df in dfs.values()]))

    cash = initial
    positions = {}  # sym -> (qty, entry_price, entry_ts)
    equity = []

    for ts in common:
        # equity計算
        eq = cash
        for sym, (qty, ep, _) in positions.items():
            p = dfs[sym].loc[ts, "close"]
            eq += qty * (p - ep)
        equity.append(eq)

        # 各銘柄の判定
        alloc_per = cash / (2 - len(positions)) if len(positions) < 2 else 0
        # 各シンボル独立に判定
        for sym in symbols:
            row = dfs[sym].loc[ts]
            price = row["close"]
            bullish = price > row["ema200"] and row["ema50"] > row["ema200"]
            if bullish and sym not in positions:
                # 未保有銘柄でブル → 残り現金を分割投入
                n_open = 2 - len(positions)
                if n_open <= 0 or cash <= 0:
                    continue
                alloc = cash / n_open
                entry_price = price * (1 + SLIP)
                qty = alloc / entry_price * (1 - FEE)
                cash -= alloc
                positions[sym] = (qty, entry_price, ts)
            elif not bullish and sym in positions:
                qty, ep, e_ts = positions[sym]
                exit_price = price * (1 - SLIP)
                proceeds = qty * exit_price * (1 - FEE)
                cash += proceeds
                del positions[sym]

    # 最終清算
    for sym, (qty, ep, _) in positions.items():
        final = dfs[sym]["close"].iloc[-1] * (1 - SLIP)
        cash += qty * final * (1 - FEE)
    equity.append(cash)
    return _stats(equity, initial)


# ─── 戦略3: プルバックバイヤー ───
def pullback_buyer(fetcher, start, end, initial=10_000.0):
    """
    条件: EMA50 > EMA200 (上昇トレンド) かつ 前日 close > ema50 だったが今日 close <= ema50
    決済: close が ema20 に達する or EMA50 < EMA200
    """
    df = _prepare_df(fetcher, "BTC/USDT", start, end)
    cash = initial
    qty = 0.0
    in_pos = False
    entry_price = 0.0
    entry_ts = None
    equity = []

    prev_above_ema50 = False
    for ts, row in df.iterrows():
        price = row["close"]
        if in_pos:
            eq = cash + qty * (price - entry_price)
        else:
            eq = cash
        equity.append(eq)

        uptrend = row["ema50"] > row["ema200"]
        above_ema50 = price > row["ema50"]
        pullback_entry = (not above_ema50) and prev_above_ema50 and uptrend

        if in_pos:
            # 利確: EMA20タッチ or トレンド終了
            if price >= row["ema20"] * 0.999 or not uptrend:
                exit_price = price * (1 - SLIP)
                proceeds = qty * exit_price * (1 - FEE)
                cash += proceeds
                qty = 0
                in_pos = False
        else:
            if pullback_entry:
                entry_price = price * (1 + SLIP)
                qty = cash / entry_price * (1 - FEE)
                cash = 0
                entry_ts = ts
                in_pos = True

        prev_above_ema50 = above_ema50

    if in_pos:
        final = df["close"].iloc[-1] * (1 - SLIP)
        cash += qty * final * (1 - FEE)
    equity.append(cash)
    return _stats(equity, initial)


# ─── 戦略4: ゴールデンクロス / デッドクロス ───
def golden_cross(fetcher, start, end, initial=10_000.0):
    df = _prepare_df(fetcher, "BTC/USDT", start, end)
    cash = initial
    qty = 0.0
    in_pos = False
    entry_price = 0.0
    equity = []

    prev_golden = None
    for ts, row in df.iterrows():
        price = row["close"]
        if in_pos:
            eq = cash + qty * (price - entry_price)
        else:
            eq = cash
        equity.append(eq)

        golden = row["ema50"] > row["ema200"]
        if prev_golden is None:
            prev_golden = golden
            continue

        if golden and not prev_golden and not in_pos:
            entry_price = price * (1 + SLIP)
            qty = cash / entry_price * (1 - FEE)
            cash = 0
            in_pos = True
        elif not golden and prev_golden and in_pos:
            exit_price = price * (1 - SLIP)
            cash += qty * exit_price * (1 - FEE)
            qty = 0
            in_pos = False
        prev_golden = golden

    if in_pos:
        final = df["close"].iloc[-1] * (1 - SLIP)
        cash += qty * final * (1 - FEE)
    equity.append(cash)
    return _stats(equity, initial)


# ─── 戦略5: BTC 3x 超レバレッジトレンドフォロー（挑戦版） ───
# → 既存の leveraged_trend を3xで呼び出すだけ


# ─── 実行 ───
if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)

    strategies = [
        ("BTC Trend Follow 1x (比較用)", lambda s, e: btc_leveraged_trend(fetcher, s, e, leverage=1.0)),
        ("BTC Trend Follow 2x",          lambda s, e: btc_leveraged_trend(fetcher, s, e, leverage=2.0)),
        ("BTC Trend Follow 3x",          lambda s, e: btc_leveraged_trend(fetcher, s, e, leverage=3.0)),
        ("Multi-Asset (BTC+ETH) TF",     lambda s, e: multi_asset_trend(fetcher, s, e)),
        ("Pullback Buyer (BTC)",         lambda s, e: pullback_buyer(fetcher, s, e)),
        ("Golden Cross (BTC EMA50/200)", lambda s, e: golden_cross(fetcher, s, e)),
    ]

    all_results = []
    for name, fn in strategies:
        row = {"strategy": name}
        for year in [2023, 2024]:
            try:
                r = fn(f"{year}-01-01", f"{year}-12-31")
                row[f"{year}_ret"] = r["total_return_pct"]
                row[f"{year}_mavg"] = r["monthly_avg"]
                row[f"{year}_dd"] = r["max_dd_pct"]
                row[f"{year}_win"] = r["win_rate_pct"]
            except Exception as e:
                row[f"{year}_ret"] = None
                print(f"  ⚠️ {name} {year}: {e}")
        all_results.append(row)

    print(f"\n\n{'=' * 100}")
    print(f"🔬 新戦略の比較（2023-2024 Binance実データ）")
    print(f"{'=' * 100}")
    print(f"{'戦略':35s} | {'23年':>8s} {'23月平均':>8s} {'23DD':>6s} | {'24年':>8s} {'24月平均':>8s} {'24DD':>6s}")
    print(f"{'-' * 100}")
    for r in all_results:
        if r.get("2023_ret") is None or r.get("2024_ret") is None:
            continue
        print(f"{r['strategy']:35s} | "
              f"{r['2023_ret']:+7.2f}% {r['2023_mavg']:+7.2f}% {r['2023_dd']:>5.1f}% | "
              f"{r['2024_ret']:+7.2f}% {r['2024_mavg']:+7.2f}% {r['2024_dd']:>5.1f}%")

    # ベンチ
    from _racsm_backtest import assert_binance_source
    print(f"{'-' * 100}")
    bh23 = (_prepare_df(fetcher, 'BTC/USDT', '2023-01-01', '2023-12-31')['close'].iloc[-1] /
            _prepare_df(fetcher, 'BTC/USDT', '2023-01-01', '2023-12-31')['close'].iloc[0] - 1) * 100
    bh24 = (_prepare_df(fetcher, 'BTC/USDT', '2024-01-01', '2024-12-31')['close'].iloc[-1] /
            _prepare_df(fetcher, 'BTC/USDT', '2024-01-01', '2024-12-31')['close'].iloc[0] - 1) * 100
    print(f"{'BTC Buy&Hold (参考)':35s} | {bh23:+7.2f}% {'':>8s} {'':>6s} | {bh24:+7.2f}% {'':>8s} {'':>6s}")

    # 結果保存
    out = Path("/Users/sanosano/projects/kimochi-max/results/explore_more.json")
    out.write_text(json.dumps(all_results, indent=2, ensure_ascii=False))
    print(f"\n💾 {out}")
