"""
上位戦略の1年単位リターン分析
==================================
5年通期バックテストで連続運用し、年末時点のequityを記録、
年次リターン%を算出。
"""
from __future__ import annotations
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np
from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import assert_binance_source
from _multipos_backtest import UNIVERSE_50, fetch_all_data
from _beat_1271_backtest import run_strategy, btc_regime, adx_to_lev, FEE, SLIP, FUNDING_PH


def run_with_yearly(all_data, start, end, max_pos, lev_mode="max2x",
                    weight_by_adx=False, enable_pyramid=False, fast_regime=False,
                    initial=10_000.0):
    """run_strategyと同じロジックだが、年末時点のequityも記録"""
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    syms = list(all_data.keys())

    cash = initial
    positions = {}
    equity_curve = []

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
        regime = btc_regime(btc_row, fast=fast_regime)

        if regime != "bull" and positions:
            for sym in list(positions.keys()):
                if sym in today_rows:
                    p = positions[sym]
                    exit_p = today_rows[sym]["close"] * (1 - SLIP)
                    pnl = p["qty"] * (exit_p - p["entry_price"]) * p["leverage"]
                    notional = p["qty"] * exit_p * p["leverage"]
                    pnl -= notional * FEE
                    hold_h = (date - p["entry_ts"]).total_seconds() / 3600
                    pnl -= notional * FUNDING_PH * hold_h
                    cash += p["alloc_usd"] + pnl
                    del positions[sym]

        for sym in list(positions.keys()):
            if sym not in today_rows: continue
            r = today_rows[sym]
            lev = adx_to_lev(r["adx"], lev_mode)
            if lev == 0 or r["close"] <= r["ema200"]:
                p = positions[sym]
                exit_p = r["close"] * (1 - SLIP)
                pnl = p["qty"] * (exit_p - p["entry_price"]) * p["leverage"]
                notional = p["qty"] * exit_p * p["leverage"]
                pnl -= notional * FEE
                hold_h = (date - p["entry_ts"]).total_seconds() / 3600
                pnl -= notional * FUNDING_PH * hold_h
                cash += p["alloc_usd"] + pnl
                del positions[sym]

        if regime == "bull":
            open_slots = max_pos - len(positions)
            if open_slots > 0:
                candidates = []
                for sym, r in today_rows.items():
                    if sym in positions: continue
                    lev = adx_to_lev(r["adx"], lev_mode)
                    if lev > 0 and r["close"] > r["ema200"]:
                        candidates.append((sym, r, lev))
                candidates.sort(key=lambda x: x[1]["adx"], reverse=True)
                candidates = candidates[:open_slots]

                if candidates:
                    if weight_by_adx:
                        total_adx = sum(c[1]["adx"] for c in candidates)
                        weights = {c[0]: c[1]["adx"] / total_adx for c in candidates}
                    else:
                        eq_w = 1.0 / len(candidates)
                        weights = {c[0]: eq_w for c in candidates}

                    for sym, r, lev in candidates:
                        alloc = cash * weights[sym] * 0.95
                        if alloc < 10: continue
                        entry_price = r["close"] * (1 + SLIP)
                        qty = alloc / entry_price
                        notional = alloc * lev
                        cash -= alloc + notional * FEE
                        positions[sym] = {
                            "qty": qty, "entry_price": entry_price,
                            "leverage": lev, "entry_ts": date,
                            "alloc_usd": alloc, "adds": 0, "last_add_price": entry_price,
                        }

        if enable_pyramid and regime == "bull":
            for sym, p in list(positions.items()):
                if sym not in today_rows or p["adds"] >= 3: continue
                cur = today_rows[sym]["close"]
                if cur >= p["last_add_price"] * 1.10:
                    add_alloc = p["alloc_usd"] * 0.5
                    if cash >= add_alloc:
                        new_ep = cur * (1 + SLIP)
                        new_qty = add_alloc / new_ep
                        notional = add_alloc * p["leverage"]
                        total_qty = p["qty"] + new_qty
                        avg_ep = (p["qty"] * p["entry_price"] + new_qty * new_ep) / total_qty
                        p["qty"] = total_qty
                        p["entry_price"] = avg_ep
                        p["alloc_usd"] += add_alloc
                        p["adds"] += 1
                        p["last_add_price"] = cur
                        cash -= add_alloc + notional * FEE

        unreal = 0.0
        for sym, p in positions.items():
            if sym in today_rows:
                unreal += p["qty"] * (today_rows[sym]["close"] - p["entry_price"]) * p["leverage"]
        equity_curve.append({"ts": date, "equity": cash + unreal})

    # 最終クローズ
    for sym in list(positions.keys()):
        df = all_data[sym]
        ld = [d for d in df.index if d <= dates[-1]]
        if not ld: continue
        last_row = df.loc[ld[-1]]
        p = positions[sym]
        exit_p = last_row["close"] * (1 - SLIP)
        pnl = p["qty"] * (exit_p - p["entry_price"]) * p["leverage"]
        notional = p["qty"] * exit_p * p["leverage"]
        pnl -= notional * FEE
        cash += p["alloc_usd"] + pnl

    # 年別 equity
    eq_df = pd.DataFrame(equity_curve).set_index("ts")
    eq_df.index = pd.to_datetime(eq_df.index)
    yearly = {}
    for year in range(2020, 2025):
        year_eq = eq_df[eq_df.index.year == year]["equity"]
        if len(year_eq) == 0: continue
        start_eq = year_eq.iloc[0]
        end_eq = year_eq.iloc[-1]
        ret_pct = (end_eq / start_eq - 1) * 100
        peak = year_eq.cummax()
        year_dd = ((peak - year_eq) / peak).max() * 100
        yearly[year] = {
            "start": round(start_eq, 2),
            "end":   round(end_eq, 2),
            "ret":   round(ret_pct, 2),
            "dd":    round(year_dd, 2),
        }

    return {
        "final_equity": cash,
        "total_ret": (cash - initial) / initial * 100,
        "yearly": yearly,
    }


if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)
    print("📥 50銘柄×5年データ取得中...")
    t0 = time.time()
    all_data = fetch_all_data(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    print(f"✅ {len(all_data)}銘柄 ({time.time()-t0:.0f}秒)\n")

    strategies = [
        ("🥇 全部盛り (D+B+A MAX 3x)",      dict(lev_mode="max3x", weight_by_adx=True, fast_regime=True)),
        ("🥈 究極盛り (D+B+C MAX 3x)",      dict(lev_mode="max3x", weight_by_adx=True, enable_pyramid=True, fast_regime=True)),
        ("🥉 高速レジーム(EMA50) Max2x",     dict(lev_mode="max2x", fast_regime=True)),
        ("4. ピラミッディング Max2x",        dict(lev_mode="max2x", enable_pyramid=True)),
        ("📊 基準 LONG MAX 2x",           dict(lev_mode="max2x")),
        ("📊 高速 + ピラミ MAX 2x",        dict(lev_mode="max2x", fast_regime=True, enable_pyramid=True)),
    ]

    print(f"{'=' * 120}")
    print(f"📅 上位戦略の年次リターン詳細")
    print(f"{'=' * 120}")
    print(f"{'戦略':35s} | {'2020':>10s} | {'2021':>10s} | {'2022':>10s} | {'2023':>10s} | {'2024':>10s} | {'5年合計':>10s}")
    print("-" * 120)

    results = {}
    for name, kwargs in strategies:
        r = run_with_yearly(all_data, "2020-01-01", "2024-12-31", 50, **kwargs)
        row = f"{name:35s} | "
        for year in range(2020, 2025):
            y = r["yearly"].get(year, {})
            if y:
                ret = y["ret"]
                row += f"{ret:+8.1f}% | "
            else:
                row += f"{'—':>10s} | "
        row += f"{r['total_ret']:+8.1f}%"
        print(row)
        results[name] = r

    # DD も出力
    print(f"\n{'=' * 120}")
    print(f"📉 年次最大DD詳細")
    print(f"{'=' * 120}")
    print(f"{'戦略':35s} | {'2020 DD':>10s} | {'2021 DD':>10s} | {'2022 DD':>10s} | {'2023 DD':>10s} | {'2024 DD':>10s}")
    print("-" * 120)
    for name, r in results.items():
        row = f"{name:35s} | "
        for year in range(2020, 2025):
            y = r["yearly"].get(year, {})
            dd = y.get("dd", "—")
            if dd == "—":
                row += f"{'—':>10s} | "
            else:
                row += f"{dd:>8.1f}% | "
        print(row)

    # 最終残高推移
    print(f"\n{'=' * 120}")
    print(f"💰 $10,000 スタートの年末残高推移")
    print(f"{'=' * 120}")
    print(f"{'戦略':35s} | {'2020末':>12s} | {'2021末':>12s} | {'2022末':>12s} | {'2023末':>12s} | {'2024末':>12s}")
    print("-" * 120)
    for name, r in results.items():
        row = f"{name:35s} | "
        for year in range(2020, 2025):
            y = r["yearly"].get(year, {})
            if y:
                row += f"${y['end']:>10,.0f} | "
            else:
                row += f"{'—':>12s} | "
        print(row)

    out = (Path(__file__).resolve().parent / "results" / "yearly_breakdown.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
