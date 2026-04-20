"""
RSIフィルタ付きショート戦略バックテスト
=========================================
改良ポイント:
  SHORTエントリー条件を厳格化:
    [BTCベア相場] かつ [個別銘柄RSI >= RSI_THRESHOLD]
  → ベア相場での"反発天井"を狙う古典的カウンタートレンド手法

  複数のRSI閾値 (70, 75, 80, 85, 90) で比較
  最適値を発見すれば 2023年の逆噴射を回避できる可能性
"""
from __future__ import annotations
import sys, json, time
from pathlib import Path
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

import pandas as pd
import numpy as np
from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import validate_ohlcv_data, assert_binance_source
from _multipos_backtest import UNIVERSE_50, fetch_all_data

FEE        = 0.0006
SLIP       = 0.0003
FUNDING_PH = 0.0000125


def compute_indicators_with_rsi(df: pd.DataFrame) -> pd.DataFrame:
    """既存の指標に加えてRSIも計算"""
    df = df.copy()
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["ema50"]  = df["close"].ewm(span=50,  adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    up = df["high"] - df["high"].shift()
    dn = df["low"].shift() - df["low"]
    plus_dm  = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    plus_di  = 100 * pd.Series(plus_dm,  index=df.index).rolling(14).mean() / df["atr"]
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(14).mean() / df["atr"]
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
    df["adx"] = dx.rolling(14).mean()

    # RSI (14)
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


def fetch_with_rsi(fetcher, symbols, start, end, buf_days=320):
    assert_binance_source(fetcher)
    buf_start = (pd.Timestamp(start) - pd.Timedelta(days=buf_days)).strftime("%Y-%m-%d")
    data = {}
    for sym in symbols:
        try:
            df = fetcher.fetch_historical_ohlcv(sym, "1d", buf_start, end)
            if df.empty:
                continue
            validate_ohlcv_data(df, sym, "1d")
            data[sym] = compute_indicators_with_rsi(df)
        except Exception:
            pass
    return data


def btc_regime(btc_row) -> str:
    if pd.isna(btc_row.get("ema200")) or pd.isna(btc_row.get("ema50")):
        return "neutral"
    p, e50, e200 = btc_row["close"], btc_row["ema50"], btc_row["ema200"]
    if p > e200 and e50 > e200: return "bull"
    if p < e200 and e50 < e200: return "bear"
    return "neutral"


def fast_exit(btc_row, direction: str) -> bool:
    if pd.isna(btc_row.get("ema50")):
        return False
    if direction == "long"  and btc_row["close"] < btc_row["ema50"]:
        return True
    if direction == "short" and btc_row["close"] > btc_row["ema50"]:
        return True
    return False


def symbol_signal(row, regime: str, rsi_short_threshold: float) -> tuple[str, float]:
    """
    LONG条件は通常通り
    SHORT条件は追加で RSI >= rsi_short_threshold を要求（過買い時のみ）
    """
    if pd.isna(row.get("ema200")) or pd.isna(row.get("adx")) or pd.isna(row.get("rsi")):
        return ("none", 0.0)
    price = row["close"]
    ema200 = row["ema200"]
    adx    = row["adx"]
    rsi    = row["rsi"]
    if adx < 20:
        return ("none", 0.0)
    lev = 2.0 if adx >= 30 else 1.0

    if regime == "bull" and price > ema200:
        return ("long", lev)
    if regime == "bear" and price < ema200:
        # RSIフィルタ: 過買い状態でのみSHORT
        if rsi >= rsi_short_threshold:
            return ("short", lev)
    return ("none", 0.0)


def run(all_data, start, end, max_pos, rsi_short=85.0, enable_short=True,
        initial=10_000.0):
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    syms = list(all_data.keys())

    cash = initial
    positions = {}
    current_dir = "none"
    equity = []
    trades = []

    def close_symbol(sym, date, today_rows, reason=""):
        nonlocal cash
        if sym not in positions or sym not in today_rows:
            return
        p = positions[sym]
        raw = today_rows[sym]["close"]
        if p["side"] == "long":
            exit_p = raw * (1 - SLIP)
            pnl = p["qty"] * (exit_p - p["entry_price"]) * p["leverage"]
        else:
            exit_p = raw * (1 + SLIP)
            pnl = p["qty"] * (p["entry_price"] - exit_p) * p["leverage"]
        notional = p["qty"] * exit_p * p["leverage"]
        pnl -= notional * FEE
        hold_h = (date - p["entry_ts"]).total_seconds() / 3600
        pnl -= notional * FUNDING_PH * hold_h
        cash += p["alloc_usd"] + pnl
        trades.append({"symbol": sym, "side": p["side"], "pnl": pnl, "reason": reason})
        del positions[sym]

    for date in dates:
        if date not in btc_df.index:
            continue
        btc_row = btc_df.loc[date]
        today_rows = {}
        for sym in syms:
            df = all_data[sym]
            if date in df.index:
                r = df.loc[date]
                if not pd.isna(r.get("ema200")) and not pd.isna(r.get("adx")):
                    today_rows[sym] = r
        regime = btc_regime(btc_row)

        # 高速撤退
        if positions and current_dir != "none" and fast_exit(btc_row, current_dir):
            for sym in list(positions.keys()):
                close_symbol(sym, date, today_rows, "fast_exit")
            current_dir = "none"

        # レジーム変更
        if positions:
            target = "long" if regime == "bull" else ("short" if regime == "bear" else "none")
            if target != current_dir:
                for sym in list(positions.keys()):
                    close_symbol(sym, date, today_rows, "regime")
                current_dir = "none"

        # 個別条件維持チェック
        for sym in list(positions.keys()):
            if sym not in today_rows:
                continue
            direction, lev = symbol_signal(today_rows[sym], regime, rsi_short)
            if direction != positions[sym]["side"] or lev == 0:
                close_symbol(sym, date, today_rows, "signal_off")

        # 新規エントリー
        if regime in ("bull", "bear"):
            if regime == "bear" and not enable_short:
                pass
            else:
                allowed = "long" if regime == "bull" else "short"
                slots = max_pos - len(positions)
                if slots > 0:
                    candidates = []
                    for sym, r in today_rows.items():
                        if sym in positions:
                            continue
                        d, lev = symbol_signal(r, regime, rsi_short)
                        if d == allowed and lev > 0:
                            candidates.append((sym, r, d, lev))
                    candidates.sort(key=lambda x: x[1]["adx"], reverse=True)
                    candidates = candidates[:slots]
                    if candidates:
                        per = cash / max(slots, 1)
                        for sym, r, d, lev in candidates:
                            if per < 10: break
                            raw = r["close"]
                            ep = raw * (1 + SLIP) if d == "long" else raw * (1 - SLIP)
                            qty = per / ep
                            notional = per * lev
                            cash -= per + notional * FEE
                            positions[sym] = {
                                "side": d, "qty": qty, "entry_price": ep,
                                "leverage": lev, "entry_ts": date,
                                "alloc_usd": per,
                            }
                        current_dir = allowed

        # MTM
        unreal = 0.0
        for sym, p in positions.items():
            if sym in today_rows:
                cur = today_rows[sym]["close"]
                if p["side"] == "long":
                    unreal += p["qty"] * (cur - p["entry_price"]) * p["leverage"]
                else:
                    unreal += p["qty"] * (p["entry_price"] - cur) * p["leverage"]
        equity.append({"ts": date, "equity": cash + unreal})

    # 最終クローズ
    final_date = dates[-1]
    for sym in list(positions.keys()):
        df = all_data[sym]
        ld = [d for d in df.index if d <= final_date]
        if ld:
            close_symbol(sym, final_date, {sym: df.loc[ld[-1]]}, "final")

    final = cash
    total_ret = (final - initial) / initial * 100
    eq_df = pd.DataFrame(equity).set_index("ts")
    eq_df.index = pd.to_datetime(eq_df.index)
    monthly = eq_df["equity"].resample("ME").last().pct_change().dropna() * 100
    peak = initial
    max_dd = 0
    for e in eq_df["equity"]:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
    long_t  = [t for t in trades if t.get("side") == "long"]
    short_t = [t for t in trades if t.get("side") == "short"]
    return {
        "final": final, "total_ret": total_ret,
        "monthly_avg": monthly.mean() if len(monthly) else 0,
        "max_dd": max_dd,
        "n_trades": len(trades),
        "n_long": len(long_t), "n_short": len(short_t),
        "long_pnl": sum(t["pnl"] for t in long_t),
        "short_pnl": sum(t["pnl"] for t in short_t),
        "win_rate": sum(1 for t in trades if t.get("pnl", 0) > 0) / max(len(trades), 1) * 100,
    }


if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)

    print("📥 50銘柄×5年データ取得 + RSI計算中...")
    t0 = time.time()
    all_data = fetch_with_rsi(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    print(f"✅ {len(all_data)}銘柄 ({time.time()-t0:.0f}秒)")

    rsi_thresholds = [None, 70, 75, 80, 85, 90]  # None = RSIなし(前回版)
    periods = [
        ("2020-01-01", "2024-12-31", "🏆 5年通期"),
        ("2021-01-01", "2021-12-31", "2021 暴落年"),
        ("2022-01-01", "2022-12-31", "🐻 2022 ベア"),
        ("2023-01-01", "2023-12-31", "⚠️ 2023 回復 (鬼門)"),
        ("2024-01-01", "2024-12-31", "2024 新高値"),
    ]

    # 50件保有で固定、RSI閾値を変えて比較
    print(f"\n{'=' * 120}")
    print(f"🧪 RSI閾値別ショートエントリー検証 (保有50件固定)")
    print(f"{'=' * 120}")
    results = {}
    for start, end, label in periods:
        print(f"\n▶ {label}  ({start}〜{end})")
        print(f"  {'戦略':>18s} | {'最終':>10s} | {'リターン':>9s} | {'月平均':>7s} | "
              f"{'DD':>6s} | {'勝率':>5s} | {'L':>4s}/{'S':>4s} | {'LONG$':>8s} / {'SHORT$':>8s}")
        print("  " + "-" * 115)
        # LONG only
        r = run(all_data, start, end, 50, enable_short=False)
        print(f"  {'LONG のみ (参照)':>18s} | ${r['final']:>8,.0f} | {r['total_ret']:+7.1f}% | "
              f"{r['monthly_avg']:+5.2f}% | {r['max_dd']:>4.1f}% | {r['win_rate']:>4.1f}% | "
              f"{r['n_long']:>3d}/{r['n_short']:>3d} | ${r['long_pnl']:+7.0f} / ${r['short_pnl']:+7.0f}")
        results[f"{label}_LONGonly"] = r
        # RSI thresholds
        for thr in rsi_thresholds[1:]:
            r = run(all_data, start, end, 50, rsi_short=thr, enable_short=True)
            tag = f"SHORT RSI>={thr}"
            print(f"  {tag:>18s} | ${r['final']:>8,.0f} | {r['total_ret']:+7.1f}% | "
                  f"{r['monthly_avg']:+5.2f}% | {r['max_dd']:>4.1f}% | {r['win_rate']:>4.1f}% | "
                  f"{r['n_long']:>3d}/{r['n_short']:>3d} | ${r['long_pnl']:+7.0f} / ${r['short_pnl']:+7.0f}")
            results[f"{label}_RSI{thr}"] = r
        print("  " + "-" * 115)

    out = Path("/Users/sanosano/projects/kimochi-max/results/rsi_short_backtest.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
