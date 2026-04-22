"""
月+10%の現実解を探す - 4つの未試行戦略
1. Chandelier Exit (ATRトレーリングストップ)
2. BTC/ETH モメンタムローテーション (強い方を保有)
3. Dynamic Leverage (ADXでレバ可変 1-3x)
4. Pyramid Trend (段階的に買い増し、最大3段)
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
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
    # ATR
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()
    df["atr22"] = tr.rolling(22).mean()
    # EMA
    df["ema20"]  = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"]  = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    # ADX（簡易）
    up_move = df["high"] - df["high"].shift()
    dn_move = df["low"].shift() - df["low"]
    plus_dm  = np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0)
    plus_di  = 100 * pd.Series(plus_dm,  index=df.index).rolling(14).mean() / df["atr14"]
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(14).mean() / df["atr14"]
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
    df["adx14"] = dx.rolling(14).mean()
    return df[df.index >= pd.Timestamp(start)]


def _stats(equity, initial):
    eq = pd.Series(equity).clip(lower=0.01)
    peak = eq.cummax()
    dd = ((peak - eq) / peak).max() * 100
    # 30日ごと月次近似
    monthly = []
    for i in range(30, len(eq), 30):
        m = eq.iloc[i] / eq.iloc[i-30] - 1
        monthly.append(m * 100)
    mavg = np.mean(monthly) if monthly else 0
    win  = sum(1 for m in monthly if m > 0) / len(monthly) * 100 if monthly else 0
    return {
        "total_return_pct": round((eq.iloc[-1] - initial) / initial * 100, 2),
        "monthly_avg":      round(mavg, 2),
        "win_rate_pct":     round(win, 1),
        "max_dd_pct":       round(dd, 2),
    }


# ─── 戦略1: Chandelier Exit ───
def chandelier_exit(fetcher, start, end, leverage=2.0, atr_mult=3.0, initial=10_000.0):
    """
    エントリー: close > EMA200 & EMA50 > EMA200
    決済: close < max(過去22日の高値) - atr_mult × ATR22
    """
    df = _prepare_df(fetcher, "BTC/USDT", start, end)
    cash, qty, in_pos = initial, 0.0, False
    entry_price, entry_ts = 0.0, None
    highest_high = 0.0
    equity = []

    for ts, row in df.iterrows():
        price = row["close"]
        if in_pos:
            highest_high = max(highest_high, row["high"])
            eq = cash + qty * (price - entry_price) * leverage
        else:
            eq = cash
        equity.append(eq)

        if not in_pos:
            if price > row["ema200"] and row["ema50"] > row["ema200"]:
                entry_price = price * (1 + SLIP)
                notional = cash * leverage
                qty = cash / entry_price
                cash -= notional * FEE
                entry_ts = ts
                highest_high = row["high"]
                in_pos = True
        else:
            trail_stop = highest_high - atr_mult * row["atr22"]
            if price < trail_stop:
                exit_price = price * (1 - SLIP)
                pnl = qty * (exit_price - entry_price) * leverage
                notional_exit = qty * exit_price * leverage
                pnl -= notional_exit * FEE
                hold_h = (ts - entry_ts).total_seconds() / 3600
                pnl -= notional_exit * FUNDING_PH * hold_h
                cash += pnl
                qty = 0.0
                in_pos = False

    if in_pos:
        fp = df["close"].iloc[-1] * (1 - SLIP)
        pnl = qty * (fp - entry_price) * leverage
        notional_exit = qty * fp * leverage
        pnl -= notional_exit * FEE
        hold_h = (df.index[-1] - entry_ts).total_seconds() / 3600
        pnl -= notional_exit * FUNDING_PH * hold_h
        cash += pnl
    equity.append(cash)
    return _stats(equity, initial)


# ─── 戦略2: BTC/ETH モメンタムローテーション ───
def momentum_rotation(fetcher, start, end, lookback=30, initial=10_000.0):
    """
    毎週、過去lookback日でBTCとETHのリターンを比較。
    勝った方を1x保有、両方マイナスなら現金。
    """
    dfb = _prepare_df(fetcher, "BTC/USDT", start, end)
    dfe = _prepare_df(fetcher, "ETH/USDT", start, end)
    common = sorted(set(dfb.index) & set(dfe.index))

    cash = initial
    qty_b, qty_e = 0.0, 0.0
    entry_price = 0.0
    held = None  # "BTC" / "ETH" / None
    equity = []
    last_rebalance = None

    for i, ts in enumerate(common):
        pb = dfb.loc[ts, "close"]
        pe = dfe.loc[ts, "close"]
        if held == "BTC":
            eq = cash + qty_b * (pb - entry_price)
        elif held == "ETH":
            eq = cash + qty_e * (pe - entry_price)
        else:
            eq = cash
        equity.append(eq)

        if last_rebalance is not None and (ts - last_rebalance).days < 7:
            continue

        # 過去lookbackリターン
        if i < lookback:
            continue
        past_ts = common[i - lookback]
        ret_b = dfb.loc[ts, "close"] / dfb.loc[past_ts, "close"] - 1
        ret_e = dfe.loc[ts, "close"] / dfe.loc[past_ts, "close"] - 1
        # BTCのEMA200レジーム
        btc_bull = pb > dfb.loc[ts, "ema200"]

        want = None
        if btc_bull:
            if ret_b > 0 or ret_e > 0:
                want = "BTC" if ret_b >= ret_e else "ETH"

        if want == held:
            last_rebalance = ts
            continue

        # 現在保有をクローズ
        if held == "BTC":
            exit_price = pb * (1 - SLIP)
            cash += qty_b * exit_price * (1 - FEE)
            qty_b = 0.0
        elif held == "ETH":
            exit_price = pe * (1 - SLIP)
            cash += qty_e * exit_price * (1 - FEE)
            qty_e = 0.0
        held = None

        # 新規エントリー
        if want == "BTC":
            entry_price = pb * (1 + SLIP)
            qty_b = cash * (1 - FEE) / entry_price
            cash = 0.0
            held = "BTC"
        elif want == "ETH":
            entry_price = pe * (1 + SLIP)
            qty_e = cash * (1 - FEE) / entry_price
            cash = 0.0
            held = "ETH"
        last_rebalance = ts

    # 清算
    if held == "BTC":
        cash += qty_b * dfb["close"].iloc[-1] * (1 - SLIP) * (1 - FEE)
    elif held == "ETH":
        cash += qty_e * dfe["close"].iloc[-1] * (1 - SLIP) * (1 - FEE)
    equity.append(cash)
    return _stats(equity, initial)


# ─── 戦略3: Dynamic Leverage (ADXベース) ───
def dynamic_leverage(fetcher, start, end, initial=10_000.0):
    """
    BTC>EMA200 時:
      ADX < 20 → 現金
      20 ≤ ADX < 30 → 1x
      30 ≤ ADX < 40 → 2x
      ADX ≥ 40 → 3x
    レバレッジは変更時のみリポジション（取引コスト抑制）
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
        if qty == 0:
            return
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
            if adx >= 40:
                target_lev = 3.0
            elif adx >= 30:
                target_lev = 2.0
            elif adx >= 20:
                target_lev = 1.0

        if target_lev != current_lev:
            # リポジション
            if qty > 0:
                close_pos(ts, price)
            if target_lev > 0:
                open_pos(ts, price, target_lev)

    # 清算
    if qty > 0:
        close_pos(df.index[-1], df["close"].iloc[-1])
    equity.append(cash)
    return _stats(equity, initial)


# ─── 戦略4: Pyramid Trend (+5%毎に段階追加) ───
def pyramid_trend(fetcher, start, end, max_stacks=3, initial=10_000.0):
    """
    エントリー: BTC > EMA200 & EMA50 > EMA200
    追加: 前回建玉から+5%上昇ごとに1段追加 (最大3段, 各1x相当の1/3資金)
    決済: 全段平均 - 2ATR で trailing stop、または bearish へ変化
    """
    df = _prepare_df(fetcher, "BTC/USDT", start, end)
    cash = initial
    stacks = []  # each: {qty, entry_price, entry_ts}
    equity = []

    for ts, row in df.iterrows():
        price = row["close"]
        total_qty = sum(s["qty"] for s in stacks)
        avg_entry = (sum(s["qty"] * s["entry_price"] for s in stacks) / total_qty) if total_qty > 0 else 0
        if total_qty > 0:
            eq = cash + total_qty * (price - avg_entry)
        else:
            eq = cash
        equity.append(eq)

        bull = price > row["ema200"] and row["ema50"] > row["ema200"]

        if bull and len(stacks) < max_stacks:
            # 最初のエントリー or +5% 上昇での追加
            if not stacks:
                alloc = initial / max_stacks
                ep = price * (1 + SLIP)
                qty = alloc * (1 - FEE) / ep
                stacks.append({"qty": qty, "entry_price": ep, "entry_ts": ts})
                cash -= alloc
            else:
                last_entry = stacks[-1]["entry_price"]
                if price >= last_entry * 1.05:
                    alloc = initial / max_stacks
                    if cash >= alloc:
                        ep = price * (1 + SLIP)
                        qty = alloc * (1 - FEE) / ep
                        stacks.append({"qty": qty, "entry_price": ep, "entry_ts": ts})
                        cash -= alloc

        # 決済判定
        if stacks:
            atr = row["atr14"]
            if pd.isna(atr):
                continue
            trail = avg_entry + (max([s["entry_price"] for s in stacks]) - avg_entry) - 2 * atr
            # シンプル: bearish 転換 or trail 下回り
            if not bull or price < avg_entry - 2 * atr:
                exit_price = price * (1 - SLIP)
                for s in stacks:
                    cash += s["qty"] * exit_price * (1 - FEE)
                stacks = []

    # 清算
    if stacks:
        fp = df["close"].iloc[-1] * (1 - SLIP)
        for s in stacks:
            cash += s["qty"] * fp * (1 - FEE)
    equity.append(cash)
    return _stats(equity, initial)


# ─── 実行 ───
if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)

    strategies = [
        ("Chandelier Exit 1x (ATR3)",  lambda s, e: chandelier_exit(fetcher, s, e, leverage=1.0, atr_mult=3.0)),
        ("Chandelier Exit 2x (ATR3)",  lambda s, e: chandelier_exit(fetcher, s, e, leverage=2.0, atr_mult=3.0)),
        ("Chandelier Exit 2x (ATR4)",  lambda s, e: chandelier_exit(fetcher, s, e, leverage=2.0, atr_mult=4.0)),
        ("BTC/ETH Momentum Rotation",  lambda s, e: momentum_rotation(fetcher, s, e)),
        ("Dynamic Leverage (ADX)",     lambda s, e: dynamic_leverage(fetcher, s, e)),
        ("Pyramid Trend (3段)",         lambda s, e: pyramid_trend(fetcher, s, e)),
    ]

    print(f"\n{'=' * 110}")
    print(f"🔎 月+10% を目指した追加戦略探索（2023-2024 Binance実データ）")
    print(f"{'=' * 110}")
    print(f"{'戦略':30s} | {'23年':>8s} {'23月平均':>8s} {'23DD':>6s} | {'24年':>8s} {'24月平均':>8s} {'24DD':>6s} | 平均月")
    print(f"{'-' * 110}")

    all_results = []
    for name, fn in strategies:
        row = {"strategy": name}
        try:
            r23 = fn("2023-01-01", "2023-12-31")
            r24 = fn("2024-01-01", "2024-12-31")
            avg_monthly = (r23["monthly_avg"] + r24["monthly_avg"]) / 2
            print(f"{name:30s} | "
                  f"{r23['total_return_pct']:+7.2f}% {r23['monthly_avg']:+7.2f}% {r23['max_dd_pct']:>5.1f}% | "
                  f"{r24['total_return_pct']:+7.2f}% {r24['monthly_avg']:+7.2f}% {r24['max_dd_pct']:>5.1f}% | "
                  f"{avg_monthly:+.2f}%")
            row.update({
                "r23": r23, "r24": r24,
                "avg_monthly_both_years": round(avg_monthly, 2),
            })
            all_results.append(row)
        except Exception as e:
            print(f"{name:30s} | ERROR: {e}")

    # ソート
    print(f"{'-' * 110}")
    sorted_r = sorted(all_results, key=lambda x: x.get("avg_monthly_both_years", -999), reverse=True)
    print(f"\n🏆 月平均ランキング（両年平均）:")
    for i, r in enumerate(sorted_r, 1):
        avg = r["avg_monthly_both_years"]
        status = "🎯月10%到達" if avg >= 10 else ("⭐月5%超" if avg >= 5 else "")
        print(f"  {i}. {r['strategy']:30s}  月平均 {avg:+6.2f}%  {status}")

    out = (Path(__file__).resolve().parent / "results" / "quest_for_10pct.json")
    out.write_text(json.dumps(all_results, indent=2, ensure_ascii=False))
    print(f"\n💾 {out}")
