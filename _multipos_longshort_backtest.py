"""
DL MAX 2x LONG/SHORT両方向版 バックテスト
==============================================
ロジック:
  - 各銘柄独立に方向判定（BTCレジームは廃止、個別銘柄で判定）
  - LONG:  close > EMA200 & EMA50 > EMA200 & ADX >= 20
  - SHORT: close < EMA200 & EMA50 < EMA200 & ADX >= 20
  - ADX >= 30 でレバ2倍、20-30 でレバ1倍
  - ADX < 20 or 方向条件未達 → 決済
  - 空きスロットには ADX 強度順で自動エントリー

2020-2024 実Binanceデータ × 5年検証
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
from _multipos_backtest import UNIVERSE_50, compute_indicators, fetch_all_data

FEE        = 0.0006
SLIP       = 0.0003
FUNDING_PH = 0.0000125


def signal_direction(row) -> tuple[str, float]:
    """
    各銘柄の方向 + レバレッジを判定
    戻り値: ("long" / "short" / "none", leverage)
    """
    if pd.isna(row.get("ema200")) or pd.isna(row.get("adx")):
        return ("none", 0.0)
    price = row["close"]
    ema50  = row["ema50"]
    ema200 = row["ema200"]
    adx    = row["adx"]

    if adx < 20:
        return ("none", 0.0)

    lev = 2.0 if adx >= 30 else 1.0

    # LONG条件
    if price > ema200 and ema50 > ema200:
        return ("long", lev)
    # SHORT条件
    if price < ema200 and ema50 < ema200:
        return ("short", lev)
    return ("none", 0.0)


def run_backtest(all_data, start, end, max_positions, initial=10_000.0,
                 enable_short=True):
    """LONG + SHORT 両方向のマルチポジションバックテスト"""
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)
    syms = list(all_data.keys())
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    cash = initial
    positions = {}  # sym -> {side, qty, entry_price, leverage, entry_ts, alloc_usd}
    equity_curve = []
    trades = []

    for date in dates:
        today_rows = {}
        for sym in syms:
            df = all_data[sym]
            if date in df.index:
                r = df.loc[date]
                if not pd.isna(r.get("ema200")) and not pd.isna(r.get("adx")):
                    today_rows[sym] = r

        # ─ 決済判定 ─
        for sym in list(positions.keys()):
            if sym not in today_rows:
                continue
            r = today_rows[sym]
            direction, lev = signal_direction(r)
            p = positions[sym]
            # シグナル方向が変わった or 消えた → 決済
            if direction != p["side"] or lev == 0:
                exit_price_raw = r["close"]
                if p["side"] == "long":
                    exit_price = exit_price_raw * (1 - SLIP)
                    pnl = p["qty"] * (exit_price - p["entry_price"]) * p["leverage"]
                else:  # short
                    exit_price = exit_price_raw * (1 + SLIP)
                    pnl = p["qty"] * (p["entry_price"] - exit_price) * p["leverage"]
                notional = p["qty"] * exit_price * p["leverage"]
                pnl -= notional * FEE
                hold_h = (date - p["entry_ts"]).total_seconds() / 3600
                pnl -= notional * FUNDING_PH * hold_h
                cash += p["alloc_usd"] + pnl
                trades.append({
                    "symbol": sym, "side": p["side"],
                    "pnl": pnl, "hold_h": hold_h,
                })
                del positions[sym]

        # ─ 新規エントリー ─
        open_slots = max_positions - len(positions)
        if open_slots > 0:
            candidates = []
            for sym, r in today_rows.items():
                if sym in positions:
                    continue
                direction, lev = signal_direction(r)
                if lev == 0:
                    continue
                if not enable_short and direction == "short":
                    continue
                candidates.append((sym, r, direction, lev))

            # ADX降順
            candidates.sort(key=lambda x: x[1]["adx"], reverse=True)
            candidates = candidates[:open_slots]

            if candidates:
                per_slot = cash / max(open_slots, 1)
                for sym, r, direction, lev in candidates:
                    if per_slot < 10:
                        break
                    raw_price = r["close"]
                    if direction == "long":
                        entry_price = raw_price * (1 + SLIP)
                    else:
                        entry_price = raw_price * (1 - SLIP)
                    qty = per_slot / entry_price
                    notional = per_slot * lev
                    cash -= per_slot + notional * FEE
                    positions[sym] = {
                        "side": direction,
                        "qty": qty, "entry_price": entry_price,
                        "leverage": lev, "entry_ts": date,
                        "alloc_usd": per_slot,
                    }

        # ─ mark-to-market ─
        unreal = 0.0
        for sym, p in positions.items():
            if sym in today_rows:
                cur = today_rows[sym]["close"]
                if p["side"] == "long":
                    unreal += p["qty"] * (cur - p["entry_price"]) * p["leverage"]
                else:
                    unreal += p["qty"] * (p["entry_price"] - cur) * p["leverage"]
        equity_curve.append({"ts": date, "equity": cash + unreal})

    # 最終クローズ
    final_date = dates[-1]
    for sym in list(positions.keys()):
        df = all_data[sym]
        last_dates = [d for d in df.index if d <= final_date]
        if not last_dates:
            continue
        last_row = df.loc[last_dates[-1]]
        p = positions[sym]
        raw = last_row["close"]
        if p["side"] == "long":
            exit_price = raw * (1 - SLIP)
            pnl = p["qty"] * (exit_price - p["entry_price"]) * p["leverage"]
        else:
            exit_price = raw * (1 + SLIP)
            pnl = p["qty"] * (p["entry_price"] - exit_price) * p["leverage"]
        notional = p["qty"] * exit_price * p["leverage"]
        pnl -= notional * FEE
        cash += p["alloc_usd"] + pnl
        trades.append({"symbol": sym, "side": p["side"], "pnl": pnl})

    final = cash
    total_ret = (final - initial) / initial * 100
    eq_df = pd.DataFrame(equity_curve).set_index("ts")
    eq_df.index = pd.to_datetime(eq_df.index)
    monthly = eq_df["equity"].resample("ME").last().pct_change().dropna() * 100

    peak = initial
    max_dd = 0
    for e in eq_df["equity"]:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # サイド別集計
    long_trades  = [t for t in trades if t.get("side") == "long"]
    short_trades = [t for t in trades if t.get("side") == "short"]
    long_pnl  = sum(t["pnl"] for t in long_trades)
    short_pnl = sum(t["pnl"] for t in short_trades)

    return {
        "final": final,
        "total_ret": total_ret,
        "monthly_avg": monthly.mean() if len(monthly) else 0,
        "max_dd": max_dd,
        "n_trades": len(trades),
        "n_long": len(long_trades),
        "n_short": len(short_trades),
        "long_pnl": long_pnl,
        "short_pnl": short_pnl,
        "win_rate": sum(1 for t in trades if t.get("pnl", 0) > 0) / max(len(trades), 1) * 100,
    }


if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)

    print("📥 50銘柄×5年データ取得中（Binance実データ・健全性6項目チェック）...")
    t0 = time.time()
    all_data = fetch_all_data(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    print(f"✅ {len(all_data)}銘柄取得 ({time.time()-t0:.1f}秒)")

    periods = [
        ("2020-01-01", "2024-12-31", "5年通期"),
        ("2021-01-01", "2021-12-31", "2021 暴落年"),
        ("2022-01-01", "2022-12-31", "2022 ベア市場"),
        ("2023-01-01", "2023-12-31", "2023 回復"),
        ("2024-01-01", "2024-12-31", "2024 新高値"),
    ]

    max_pos_configs = [10, 20, 50]

    results = {}
    for start, end, label in periods:
        print(f"\n{'=' * 120}")
        print(f"▶ {label} ({start} 〜 {end})")
        print(f"{'=' * 120}")
        print(f"  {'保有数':>5s} {'方向':>8s} | {'最終':>10s} | {'総リターン':>9s} | {'月平均':>7s} | "
              f"{'DD':>6s} | {'取引':>5s} {'L':>4s}/{'S':>4s} | {'LONG$':>9s} | {'SHORT$':>9s}")
        print("  " + "-" * 115)
        for mp in max_pos_configs:
            # LONG only
            r = run_backtest(all_data, start, end, mp, enable_short=False)
            print(f"  {mp:>4d}件 {'LONGのみ':>7s} | ${r['final']:>8,.0f} | {r['total_ret']:+7.1f}% | "
                  f"{r['monthly_avg']:+5.2f}% | {r['max_dd']:>4.1f}% | {r['n_trades']:>4d} {r['n_long']:>3d}/{r['n_short']:>3d} | "
                  f"${r['long_pnl']:+7.0f} | ${r['short_pnl']:+7.0f}")
            results[f"{label}_mp{mp}_longonly"] = r

            # LONG + SHORT
            r = run_backtest(all_data, start, end, mp, enable_short=True)
            print(f"  {mp:>4d}件 {'L+S':>7s} | ${r['final']:>8,.0f} | {r['total_ret']:+7.1f}% | "
                  f"{r['monthly_avg']:+5.2f}% | {r['max_dd']:>4.1f}% | {r['n_trades']:>4d} {r['n_long']:>3d}/{r['n_short']:>3d} | "
                  f"${r['long_pnl']:+7.0f} | ${r['short_pnl']:+7.0f}")
            results[f"{label}_mp{mp}_longshort"] = r
            print("  " + "-" * 115)

    out = Path("/Users/sanosano/projects/kimochi-max/results/longshort_backtest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 保存: {out}")
