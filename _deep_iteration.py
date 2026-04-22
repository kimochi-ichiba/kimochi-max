"""
深掘り反復検証
1. Dynamic Leverage を 5年（2020-2024）で検証 - 2022ベア市場も含む真のストレステスト
2. Ichimoku Cloud (一目均衡表) - 日本発の古典手法
3. SuperTrend - 現代の標準トレンド指標
4. Hull MA - 超高速トレンドMA
5. DLのADXパラメータスイープ（15/25/35/45）
全て実Binanceデータ・合成禁止・データ健全性6項目チェック通過必須。
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
import numpy as np
from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import validate_ohlcv_data, assert_binance_source
from _quest_for_10pct import _prepare_df, _stats, FEE, SLIP, FUNDING_PH


def _prep_with_ichimoku(fetcher, symbol, start, end, buf=400):
    """一目均衡表の指標を追加"""
    df = _prepare_df(fetcher, symbol, start, end, buf_days=buf)
    # 転換線（過去9日の中央値）
    p = df
    tenkan = (p["high"].rolling(9).max() + p["low"].rolling(9).min()) / 2
    kijun  = (p["high"].rolling(26).max() + p["low"].rolling(26).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(26)
    senkou_b = ((p["high"].rolling(52).max() + p["low"].rolling(52).min()) / 2).shift(26)
    chikou   = p["close"].shift(-26)
    df["tenkan"] = tenkan
    df["kijun"]  = kijun
    df["sen_a"]  = senkou_a
    df["sen_b"]  = senkou_b
    return df


# ─── 戦略1: Dynamic Leverage Max 3x（長期5年） ───
def dl_longterm(fetcher, start, end, levels, initial=10_000.0):
    from _dynamic_lev_max2x import dynamic_leverage_custom
    return dynamic_leverage_custom(fetcher, start, end, levels, initial)


# ─── 戦略2: Ichimoku Cloud ───
def ichimoku_cloud(fetcher, start, end, leverage=1.0, initial=10_000.0):
    """
    Ichimoku トレンドフォロー（レバ可変）
    エントリー: close > cloud (max(sen_a, sen_b)) かつ 転換線 > 基準線
    決済: close < cloud (min(sen_a, sen_b)) または 転換線 < 基準線
    """
    df = _prep_with_ichimoku(fetcher, "BTC/USDT", start, end)
    cash = initial
    qty = 0.0
    in_pos = False
    ep = 0.0
    ts_entry = None
    equity = []

    for ts, row in df.iterrows():
        price = row["close"]
        if in_pos:
            eq = cash + qty * (price - ep) * leverage
        else:
            eq = cash
        equity.append(eq)

        if pd.isna(row["sen_a"]) or pd.isna(row["sen_b"]):
            continue
        cloud_top = max(row["sen_a"], row["sen_b"])
        cloud_bot = min(row["sen_a"], row["sen_b"])
        tk_over_kj = row["tenkan"] > row["kijun"]

        if not in_pos and price > cloud_top and tk_over_kj:
            ep = price * (1 + SLIP)
            qty = cash / ep
            cash -= cash * leverage * FEE
            in_pos = True
            ts_entry = ts
        elif in_pos and (price < cloud_bot or row["tenkan"] < row["kijun"]):
            exit_price = price * (1 - SLIP)
            pnl = qty * (exit_price - ep) * leverage
            notional_exit = qty * exit_price * leverage
            pnl -= notional_exit * FEE
            hold_h = (ts - ts_entry).total_seconds() / 3600
            pnl -= notional_exit * FUNDING_PH * hold_h
            cash += pnl
            qty = 0
            in_pos = False

    if in_pos:
        fp = df["close"].iloc[-1] * (1 - SLIP)
        pnl = qty * (fp - ep) * leverage
        notional_exit = qty * fp * leverage
        pnl -= notional_exit * FEE
        hold_h = (df.index[-1] - ts_entry).total_seconds() / 3600
        pnl -= notional_exit * FUNDING_PH * hold_h
        cash += pnl
    equity.append(cash)
    return _stats(equity, initial)


# ─── 戦略3: SuperTrend ───
def supertrend(fetcher, start, end, period=10, multiplier=3.0, leverage=1.0, initial=10_000.0):
    """
    SuperTrend: ATR×Mult を中央値に加減算してトレンド判定
    """
    df = _prepare_df(fetcher, "BTC/USDT", start, end)
    # SuperTrend 計算
    hl2 = (df["high"] + df["low"]) / 2
    atr = df["atr14"]
    up_band = hl2 + multiplier * atr
    dn_band = hl2 - multiplier * atr
    # 初期化
    trend = pd.Series(index=df.index, dtype="float64")
    final_up = up_band.copy()
    final_dn = dn_band.copy()
    direction = 1  # 1=up trend, -1=down
    trend_line = pd.Series(index=df.index, dtype="float64")
    close = df["close"]

    for i, idx in enumerate(df.index):
        if i == 0:
            trend_line.iloc[i] = dn_band.iloc[i]
            continue
        # 更新ルール
        if close.iloc[i] > final_up.iloc[i-1]:
            direction = 1
        elif close.iloc[i] < final_dn.iloc[i-1]:
            direction = -1
        if direction == 1:
            trend_line.iloc[i] = max(dn_band.iloc[i], trend_line.iloc[i-1]) if trend_line.iloc[i-1] > 0 else dn_band.iloc[i]
        else:
            trend_line.iloc[i] = min(up_band.iloc[i], trend_line.iloc[i-1]) if trend_line.iloc[i-1] > 0 else up_band.iloc[i]

    cash = initial
    qty = 0.0
    in_pos = False
    ep = 0.0
    ts_entry = None
    equity = []

    for i, (ts, row) in enumerate(df.iterrows()):
        price = row["close"]
        if in_pos:
            eq = cash + qty * (price - ep) * leverage
        else:
            eq = cash
        equity.append(eq)

        if i < 15 or pd.isna(trend_line.iloc[i]):
            continue
        above_trend = price > trend_line.iloc[i]

        if not in_pos and above_trend:
            ep = price * (1 + SLIP)
            qty = cash / ep
            cash -= cash * leverage * FEE
            in_pos = True
            ts_entry = ts
        elif in_pos and not above_trend:
            exit_price = price * (1 - SLIP)
            pnl = qty * (exit_price - ep) * leverage
            notional_exit = qty * exit_price * leverage
            pnl -= notional_exit * FEE
            hold_h = (ts - ts_entry).total_seconds() / 3600
            pnl -= notional_exit * FUNDING_PH * hold_h
            cash += pnl
            qty = 0
            in_pos = False

    if in_pos:
        fp = df["close"].iloc[-1] * (1 - SLIP)
        pnl = qty * (fp - ep) * leverage
        notional_exit = qty * fp * leverage
        pnl -= notional_exit * FEE
        hold_h = (df.index[-1] - ts_entry).total_seconds() / 3600
        pnl -= notional_exit * FUNDING_PH * hold_h
        cash += pnl
    equity.append(cash)
    return _stats(equity, initial)


# ─── 戦略4: Hull Moving Average ───
def hull_ma(fetcher, start, end, period=20, leverage=1.0, initial=10_000.0):
    """
    HMA = WMA(2 × WMA(n/2) − WMA(n), sqrt(n))
    """
    df = _prepare_df(fetcher, "BTC/USDT", start, end)
    def wma(series, n):
        weights = np.arange(1, n + 1)
        return series.rolling(n).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)
    wma_half = wma(df["close"], period // 2)
    wma_full = wma(df["close"], period)
    hma_src  = 2 * wma_half - wma_full
    df["hma"] = wma(hma_src, int(np.sqrt(period)))
    df["hma_prev"] = df["hma"].shift(1)

    cash = initial
    qty = 0.0
    in_pos = False
    ep = 0.0
    ts_entry = None
    equity = []

    for ts, row in df.iterrows():
        price = row["close"]
        if in_pos:
            eq = cash + qty * (price - ep) * leverage
        else:
            eq = cash
        equity.append(eq)

        if pd.isna(row["hma"]) or pd.isna(row["hma_prev"]):
            continue
        rising = row["hma"] > row["hma_prev"]

        if not in_pos and rising and price > row["ema200"]:
            ep = price * (1 + SLIP)
            qty = cash / ep
            cash -= cash * leverage * FEE
            in_pos = True
            ts_entry = ts
        elif in_pos and (not rising or price < row["ema200"]):
            exit_price = price * (1 - SLIP)
            pnl = qty * (exit_price - ep) * leverage
            notional_exit = qty * exit_price * leverage
            pnl -= notional_exit * FEE
            hold_h = (ts - ts_entry).total_seconds() / 3600
            pnl -= notional_exit * FUNDING_PH * hold_h
            cash += pnl
            qty = 0
            in_pos = False

    if in_pos:
        fp = df["close"].iloc[-1] * (1 - SLIP)
        pnl = qty * (fp - ep) * leverage
        notional_exit = qty * fp * leverage
        pnl -= notional_exit * FEE
        hold_h = (df.index[-1] - ts_entry).total_seconds() / 3600
        pnl -= notional_exit * FUNDING_PH * hold_h
        cash += pnl
    equity.append(cash)
    return _stats(equity, initial)


# ─── 実行 ───
if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)

    # === テスト1: 長期5年検証（2020-2024）===
    print("\n" + "=" * 110)
    print("📅 TEST 1: Dynamic Leverage 5年ストレステスト（2020-2024 / 2022ベア市場含む）")
    print("=" * 110)

    from _dynamic_lev_max2x import dynamic_leverage_custom
    LEVELS_3X = [(20, 1.0), (30, 2.0), (40, 3.0)]
    LEVELS_2X = [(20, 1.0), (30, 2.0)]

    for name, levels in [("DL MAX 2x", LEVELS_2X), ("DL MAX 3x", LEVELS_3X)]:
        for period in [("2020-01-01", "2022-12-31"), ("2020-01-01", "2024-12-31"),
                       ("2022-01-01", "2022-12-31")]:  # 2022 bearだけ
            start, end = period
            try:
                r = dynamic_leverage_custom(fetcher, start, end, levels)
                print(f"  {name:12s} {start} 〜 {end}: "
                      f"{r['total_return_pct']:+9.2f}% / 月平均 {r['monthly_avg']:+6.2f}% / "
                      f"DD {r['max_dd_pct']:5.1f}% / 勝率 {r['win_rate_pct']}%")
            except Exception as e:
                print(f"  {name}: ERROR {e}")

    # === テスト2: 新戦略4つ（1x基準） ===
    print("\n" + "=" * 110)
    print("🧪 TEST 2: 新戦略の比較（BTC/USDT, 1x・両年）")
    print("=" * 110)
    strategies_1x = [
        ("Ichimoku Cloud 1x",     lambda s,e: ichimoku_cloud(fetcher, s, e, leverage=1.0)),
        ("Ichimoku Cloud 2x",     lambda s,e: ichimoku_cloud(fetcher, s, e, leverage=2.0)),
        ("SuperTrend(10,3) 1x",   lambda s,e: supertrend(fetcher, s, e, 10, 3.0, 1.0)),
        ("SuperTrend(10,3) 2x",   lambda s,e: supertrend(fetcher, s, e, 10, 3.0, 2.0)),
        ("SuperTrend(20,2) 1x",   lambda s,e: supertrend(fetcher, s, e, 20, 2.0, 1.0)),
        ("Hull MA(20) 1x",        lambda s,e: hull_ma(fetcher, s, e, 20, 1.0)),
        ("Hull MA(20) 2x",        lambda s,e: hull_ma(fetcher, s, e, 20, 2.0)),
    ]
    print(f"{'戦略':25s} | {'23年':>8s} {'23月':>7s} {'23DD':>6s} | {'24年':>8s} {'24月':>7s} {'24DD':>6s} | 平均月")
    print("-" * 110)
    new_results = []
    for name, fn in strategies_1x:
        try:
            r23 = fn("2023-01-01", "2023-12-31")
            r24 = fn("2024-01-01", "2024-12-31")
            avg = (r23["monthly_avg"] + r24["monthly_avg"]) / 2
            tag = "🎯10%+" if avg >= 10 else ("⭐5%+" if avg >= 5 else "")
            print(f"{name:25s} | "
                  f"{r23['total_return_pct']:+7.2f}% {r23['monthly_avg']:+6.2f}% {r23['max_dd_pct']:>5.1f}% | "
                  f"{r24['total_return_pct']:+7.2f}% {r24['monthly_avg']:+6.2f}% {r24['max_dd_pct']:>5.1f}% | "
                  f"{avg:+.2f}% {tag}")
            new_results.append({"name": name, "r23": r23, "r24": r24, "avg": avg})
        except Exception as e:
            print(f"{name:25s} | ERROR: {e}")

    # === テスト3: DL パラメータスイープ ===
    print("\n" + "=" * 110)
    print("🔧 TEST 3: Dynamic Leverage ADXパラメータスイープ")
    print("=" * 110)
    print(f"{'パラメータ':45s} | {'23月':>7s} {'23DD':>6s} | {'24月':>7s} {'24DD':>6s} | 平均月")
    sweep_configs = [
        ("ADX>15=1x, 25=2x, 35=3x (早入り)",    [(15, 1.0), (25, 2.0), (35, 3.0)]),
        ("ADX>20=1x, 30=2x, 40=3x (標準)",     [(20, 1.0), (30, 2.0), (40, 3.0)]),
        ("ADX>25=1x, 35=2x, 45=3x (遅入り)",    [(25, 1.0), (35, 2.0), (45, 3.0)]),
        ("ADX>30=1x, 40=2x, 50=3x (超遅)",     [(30, 1.0), (40, 2.0), (50, 3.0)]),
        ("ADX>20=1x, 25=2x, 30=3x (素早く)",    [(20, 1.0), (25, 2.0), (30, 3.0)]),
        ("ADX>20=1.5x, 30=2.5x, 40=3.5x",     [(20, 1.5), (30, 2.5), (40, 3.5)]),
    ]
    print("-" * 110)
    for name, lv in sweep_configs:
        try:
            r23 = dynamic_leverage_custom(fetcher, "2023-01-01", "2023-12-31", lv)
            r24 = dynamic_leverage_custom(fetcher, "2024-01-01", "2024-12-31", lv)
            avg = (r23["monthly_avg"] + r24["monthly_avg"]) / 2
            tag = "🎯" if avg >= 10 else ""
            print(f"{name:45s} | {r23['monthly_avg']:+6.2f}% {r23['max_dd_pct']:>5.1f}% | "
                  f"{r24['monthly_avg']:+6.2f}% {r24['max_dd_pct']:>5.1f}% | {avg:+.2f}% {tag}")
        except Exception as e:
            print(f"{name:45s} | ERROR {e}")

    # 結果保存
    out = (Path(__file__).resolve().parent / "results" / "deep_iteration.json")
    out.write_text(json.dumps({"new_strategies": new_results}, indent=2, ensure_ascii=False))
    print(f"\n💾 {out}")
