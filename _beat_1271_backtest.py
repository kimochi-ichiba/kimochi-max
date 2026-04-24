"""
+1,271%を超える戦略を探す
==========================
候補4手法:
  A. 高レバ版: ADX>=20→1x, >=30→2x, >=40→3x, >=50→4x
  B. ADX加重: ポジション配分をADX強度に比例
  C. ピラミッディング: 勝ってる銘柄に+10%上昇ごとに追加
  D. 高速レジーム: BTC>EMA50なら「強気」として早めエントリー
"""
from __future__ import annotations
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np
from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import assert_binance_source, validate_ohlcv_data
from _multipos_backtest import UNIVERSE_50, compute_indicators, fetch_all_data

FEE        = 0.0006
SLIP       = 0.0003
FUNDING_PH = 0.0000125


def btc_regime(btc_row, fast: bool = False) -> str:
    """BTCレジーム. fast=True なら EMA50 ベース(早期判定)"""
    if pd.isna(btc_row.get("ema200")) or pd.isna(btc_row.get("ema50")):
        return "neutral"
    price = btc_row["close"]
    e50 = btc_row["ema50"]
    e200 = btc_row["ema200"]
    if fast:
        return "bull" if price > e50 else "bear"
    if price > e200 and e50 > e200: return "bull"
    if price < e200 and e50 < e200: return "bear"
    return "neutral"


def adx_to_lev(adx: float, mode: str) -> float:
    """モード別にADX→レバレッジ変換"""
    if pd.isna(adx) or adx < 20: return 0.0
    if mode == "max2x":
        return 2.0 if adx >= 30 else 1.0
    if mode == "max3x":
        if adx >= 40: return 3.0
        if adx >= 30: return 2.0
        return 1.0
    if mode == "max4x":
        if adx >= 50: return 4.0
        if adx >= 40: return 3.0
        if adx >= 30: return 2.0
        return 1.0
    if mode == "max5x":
        if adx >= 50: return 5.0
        if adx >= 40: return 3.0
        if adx >= 30: return 2.0
        return 1.0
    return 0.0


def run_strategy(all_data, start, end, max_pos, lev_mode="max2x",
                 weight_by_adx=False, enable_pyramid=False, fast_regime=False,
                 initial=10_000.0):
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    syms = list(all_data.keys())

    cash = initial
    positions = {}  # sym -> {qty, entry_price, lev, entry_ts, alloc_usd, adds}
    equity_curve = []
    trades = []

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

        # ベア or 中立で全決済
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
                    trades.append({"sym": sym, "pnl": pnl})
                    del positions[sym]

        # 個別シグナル消失で決済
        for sym in list(positions.keys()):
            if sym not in today_rows:
                continue
            r = today_rows[sym]
            lev = adx_to_lev(r["adx"], lev_mode)
            # ブル相場でも銘柄が EMA200 割れたら決済
            if lev == 0 or r["close"] <= r["ema200"]:
                p = positions[sym]
                exit_p = r["close"] * (1 - SLIP)
                pnl = p["qty"] * (exit_p - p["entry_price"]) * p["leverage"]
                notional = p["qty"] * exit_p * p["leverage"]
                pnl -= notional * FEE
                hold_h = (date - p["entry_ts"]).total_seconds() / 3600
                pnl -= notional * FUNDING_PH * hold_h
                cash += p["alloc_usd"] + pnl
                trades.append({"sym": sym, "pnl": pnl})
                del positions[sym]

        # 新規エントリー（ブル時のみ）
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
                    # 配分計算
                    if weight_by_adx:
                        total_adx = sum(c[1]["adx"] for c in candidates)
                        weights = {c[0]: c[1]["adx"] / total_adx for c in candidates}
                    else:
                        eq_w = 1.0 / len(candidates)
                        weights = {c[0]: eq_w for c in candidates}

                    for sym, r, lev in candidates:
                        alloc = cash * weights[sym] * 0.95  # 95%まで使う
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

        # ピラミッディング（勝ち銘柄に追加）
        if enable_pyramid and regime == "bull":
            for sym, p in list(positions.items()):
                if sym not in today_rows or p["adds"] >= 3:
                    continue
                cur = today_rows[sym]["close"]
                if cur >= p["last_add_price"] * 1.10:  # +10%上昇で追加
                    add_alloc = p["alloc_usd"] * 0.5
                    if cash >= add_alloc:
                        new_ep = cur * (1 + SLIP)
                        new_qty = add_alloc / new_ep
                        notional = add_alloc * p["leverage"]
                        # 平均建値
                        total_qty = p["qty"] + new_qty
                        avg_ep = (p["qty"] * p["entry_price"] + new_qty * new_ep) / total_qty
                        p["qty"] = total_qty
                        p["entry_price"] = avg_ep
                        p["alloc_usd"] += add_alloc
                        p["adds"] += 1
                        p["last_add_price"] = cur
                        cash -= add_alloc + notional * FEE

        # MTM
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
        trades.append({"sym": sym, "pnl": pnl})

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
    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    return {
        "final": final, "total_ret": total_ret,
        "monthly_avg": monthly.mean() if len(monthly) else 0,
        "max_dd": max_dd,
        "n_trades": len(trades),
        "win_rate": wins / max(len(trades), 1) * 100,
    }


if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)

    print("📥 50銘柄×5年データ取得中...")
    t0 = time.time()
    all_data = fetch_all_data(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    print(f"✅ {len(all_data)}銘柄 ({time.time()-t0:.0f}秒)")

    # 固定: 50件保有, 5年通期
    max_pos = 50

    tests = [
        ("【基準】LONG MAX 2x",                     dict(lev_mode="max2x")),
        ("A1. MAX 3x (ADX>=40で3倍)",               dict(lev_mode="max3x")),
        ("A2. MAX 4x (ADX>=50で4倍)",               dict(lev_mode="max4x")),
        ("A3. MAX 5x (挑戦)",                       dict(lev_mode="max5x")),
        ("B. ADX加重配分 (強いほど多く) Max2x",      dict(lev_mode="max2x", weight_by_adx=True)),
        ("B+A. ADX加重 x MAX 3x",                  dict(lev_mode="max3x", weight_by_adx=True)),
        ("C. ピラミッディング Max2x (+10%毎追加)",   dict(lev_mode="max2x", enable_pyramid=True)),
        ("C+A. ピラミッディング Max 3x",            dict(lev_mode="max3x", enable_pyramid=True)),
        ("D. 高速レジーム(EMA50) Max2x",            dict(lev_mode="max2x", fast_regime=True)),
        ("全部盛り (D+B+A MAX 3x)",                  dict(lev_mode="max3x", weight_by_adx=True, fast_regime=True)),
        ("究極盛り (D+B+C MAX 3x)",                  dict(lev_mode="max3x", weight_by_adx=True, enable_pyramid=True, fast_regime=True)),
    ]

    print(f"\n{'=' * 125}")
    print(f"🧪 +1,271%を超える戦略探し - 保有50件固定, 2020-2024 通期")
    print(f"{'=' * 125}")
    print(f"{'戦略':45s} | {'最終':>12s} | {'総リターン':>9s} | {'月平均':>7s} | "
          f"{'DD':>6s} | {'取引':>5s} | {'勝率':>5s}")
    print("-" * 125)

    results = {}
    for name, kwargs in tests:
        try:
            r = run_strategy(all_data, "2020-01-01", "2024-12-31", max_pos, **kwargs)
            beat = "🚀" if r["total_ret"] > 1271 else ("⭐" if r["total_ret"] > 1000 else "")
            print(f"{name:45s} | ${r['final']:>10,.0f} | {r['total_ret']:+7.1f}% | "
                  f"{r['monthly_avg']:+5.2f}% | {r['max_dd']:>4.1f}% | {r['n_trades']:>4d} | "
                  f"{r['win_rate']:>4.1f}% {beat}")
            results[name] = r
        except Exception as e:
            print(f"{name:45s} | ERROR: {e}")

    # ソート
    print(f"\n{'=' * 125}")
    print(f"🏆 ランキング（5年総リターン順）")
    print(f"{'=' * 125}")
    sorted_r = sorted(results.items(), key=lambda x: x[1]["total_ret"], reverse=True)
    for i, (name, r) in enumerate(sorted_r, 1):
        tag = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else "  "))
        print(f"  {tag} {i}. {name:45s}  {r['total_ret']:+8.1f}%  DD {r['max_dd']:>5.1f}%  "
              f"月{r['monthly_avg']:+5.2f}%")

    out = (Path(__file__).resolve().parent / "results" / "beat_1271.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
