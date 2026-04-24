"""
清算モデル付き現実的バックテスト
=================================
現実の取引所で発動する清算ロジックを追加:
  - 2x: 逆行 -50% で清算（元本ゼロ）
  - 3x: 逆行 -33% で清算
  - 4x: 逆行 -25% で清算
  - 5x: 逆行 -20% で清算
  - 清算発動時: ポジション全損 + 清算手数料 0.2%

これにより、DD 100%超のシミュ結果が現実的に調整される。
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
from _beat_1271_backtest import btc_regime, adx_to_lev, FEE, SLIP, FUNDING_PH

# 清算設定
LIQ_FEE = 0.002  # 清算手数料 0.2%


def liquidation_threshold(leverage: float) -> float:
    """レバレッジから清算閾値(%)を返す（安全マージン考慮）"""
    # 取引所は 80% 維持証拠金率が標準
    # 実効的な清算距離: 1/lev * 0.85 くらい
    return (1.0 / leverage) * 0.85  # 2x→42.5%, 3x→28.3%, 4x→21.3%, 5x→17%


def run_with_liquidation(all_data, start, end, max_pos, lev_mode="max2x",
                          weight_by_adx=False, enable_pyramid=False,
                          fast_regime=False, initial=10_000.0):
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    syms = list(all_data.keys())

    cash = initial
    positions = {}
    equity_curve = []
    n_liquidations = 0
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

        # ━ 清算チェック（高値・安値でのintrabar判定）━
        for sym in list(positions.keys()):
            if sym not in today_rows:
                continue
            r = today_rows[sym]
            p = positions[sym]
            liq_thr = liquidation_threshold(p["leverage"])
            # ロングの場合: 安値が entry × (1 - liq_thr) を割ったら清算
            intrabar_low = r["low"] if "low" in r else r["close"]
            adverse_move = (p["entry_price"] - intrabar_low) / p["entry_price"]
            if adverse_move >= liq_thr:
                # 清算発動
                n_liquidations += 1
                # 元本全損 + 清算手数料
                fee = p["alloc_usd"] * LIQ_FEE
                cash -= fee  # 元本は既に差引済みなのでfeeのみ追加損失
                trades.append({"sym": sym, "pnl": -p["alloc_usd"], "liquidated": True})
                del positions[sym]

        # レジーム外で全決済
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

        # 個別シグナル消失
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
                trades.append({"sym": sym, "pnl": pnl})
                del positions[sym]

        # 新規エントリー
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

        # ピラミ
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

        # MTM (清算考慮: equity 下限 0)
        unreal = 0.0
        for sym, p in positions.items():
            if sym in today_rows:
                unreal += p["qty"] * (today_rows[sym]["close"] - p["entry_price"]) * p["leverage"]
        eq = max(0, cash + unreal)
        equity_curve.append({"ts": date, "equity": eq})

        # 残高ゼロなら停止
        if eq < 100:
            break

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

    final = max(0, cash)
    total_ret = (final - initial) / initial * 100
    eq_df = pd.DataFrame(equity_curve).set_index("ts")
    eq_df.index = pd.to_datetime(eq_df.index)
    peak = initial
    max_dd = 0
    for e in eq_df["equity"]:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return {
        "final": final, "total_ret": total_ret,
        "max_dd": max_dd, "n_trades": len(trades),
        "n_liquidations": n_liquidations,
    }


if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)

    print("📥 50銘柄×5年データ取得中...")
    t0 = time.time()
    all_data = fetch_all_data(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    print(f"✅ {len(all_data)}銘柄 ({time.time()-t0:.0f}秒)\n")

    strategies = [
        ("🥇 全部盛り MAX 3x (清算付)",       dict(lev_mode="max3x", weight_by_adx=True, fast_regime=True)),
        ("🥈 究極盛り MAX 3x (清算付)",       dict(lev_mode="max3x", weight_by_adx=True, enable_pyramid=True, fast_regime=True)),
        ("🥉 高速レジーム Max 2x (清算付)",    dict(lev_mode="max2x", fast_regime=True)),
        ("4. ピラミ Max 2x (清算付)",         dict(lev_mode="max2x", enable_pyramid=True)),
        ("5. 基準 LONG Max 2x (清算付)",     dict(lev_mode="max2x")),
        ("6. 高速+ピラミ Max 2x (清算付)",    dict(lev_mode="max2x", fast_regime=True, enable_pyramid=True)),
    ]

    print(f"{'=' * 120}")
    print(f"⚠️ 清算モデル付き現実的バックテスト (2020-2024)")
    print(f"{'=' * 120}")
    print(f"{'戦略':40s} | {'最終':>12s} | {'総リターン':>9s} | {'DD':>6s} | {'取引':>5s} | {'清算':>5s}")
    print("-" * 120)

    results = {}
    for name, kwargs in strategies:
        r = run_with_liquidation(all_data, "2020-01-01", "2024-12-31", 50, **kwargs)
        tag = "🚀" if r["total_ret"] > 500 else ("⭐" if r["total_ret"] > 100 else ("💀" if r["total_ret"] < -50 else ""))
        print(f"{name:40s} | ${r['final']:>10,.0f} | {r['total_ret']:+7.1f}% | "
              f"{r['max_dd']:>4.1f}% | {r['n_trades']:>4d} | {r['n_liquidations']:>4d} {tag}")
        results[name] = r

    out = (Path(__file__).resolve().parent / "results" / "liquidation_backtest.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")

    # 比較表
    print(f"\n{'=' * 120}")
    print(f"📊 清算あり vs なしの比較")
    print(f"{'=' * 120}")
    print(f"{'戦略':40s} | {'清算なし':>12s} | {'清算あり':>12s} | {'影響':>10s}")
    print("-" * 100)
    # 前回の結果と比較
    prev = {
        "🥇 全部盛り MAX 3x (清算付)":       3183.4,
        "🥈 究極盛り MAX 3x (清算付)":       2864.1,
        "🥉 高速レジーム Max 2x (清算付)":    2096.1,
        "4. ピラミ Max 2x (清算付)":         1893.4,
        "5. 基準 LONG Max 2x (清算付)":     468.1,
        "6. 高速+ピラミ Max 2x (清算付)":    3438.8,
    }
    for name, r in results.items():
        p = prev.get(name, 0)
        change = r["total_ret"] - p
        print(f"{name:40s} | {p:+9.1f}% | {r['total_ret']:+9.1f}% | {change:+9.1f}pp")
