"""
スマートLONG/SHORT戦略 バックテスト
===================================
前回の失敗を改善:
  ❌ 旧: 各銘柄独立判定 → 2023年の反発でショート全滅
  ✅ 新: BTCレジームで方向切替・高速撤退

改良ロジック:
  [BTCレジーム判定]
    BULL:  BTC.close > EMA200 & EMA50 > EMA200 → LONG のみ
    BEAR:  BTC.close < EMA200 & EMA50 < EMA200 → SHORT のみ
    NEUTRAL: 混在 → 全決済・現金

  [高速撤退]  ← ここが重要！
    LONG中にBTCがEMA50を下に突破 → 全LONG決済
    SHORT中にBTCがEMA50を上に突破 → 全SHORT決済（2023年の惨劇回避！）
    レジーム切替時も全決済

  [個別銘柄判定]
    LONG 時: 銘柄 close > 銘柄 EMA200 かつ ADX >= 20
    SHORT時: 銘柄 close < 銘柄 EMA200 かつ ADX >= 20
    ADX >= 30 はレバ2倍
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


def btc_regime(btc_row) -> str:
    """BTCのレジーム判定"""
    if pd.isna(btc_row.get("ema200")) or pd.isna(btc_row.get("ema50")):
        return "neutral"
    price = btc_row["close"]
    e50   = btc_row["ema50"]
    e200  = btc_row["ema200"]
    if price > e200 and e50 > e200:
        return "bull"
    if price < e200 and e50 < e200:
        return "bear"
    return "neutral"


def symbol_signal(row, regime: str) -> tuple[str, float]:
    """
    BTCレジーム + 個別銘柄条件で方向とレバレッジを判定
    """
    if pd.isna(row.get("ema200")) or pd.isna(row.get("adx")):
        return ("none", 0.0)
    price = row["close"]
    ema200 = row["ema200"]
    adx    = row["adx"]
    if adx < 20:
        return ("none", 0.0)
    lev = 2.0 if adx >= 30 else 1.0

    if regime == "bull" and price > ema200:
        return ("long", lev)
    if regime == "bear" and price < ema200:
        return ("short", lev)
    return ("none", 0.0)


def fast_exit_signal(btc_row, current_direction: str) -> bool:
    """
    BTCがEMA50を逆方向に突破したら全決済（高速撤退）
    """
    if pd.isna(btc_row.get("ema50")):
        return False
    if current_direction == "long" and btc_row["close"] < btc_row["ema50"]:
        return True
    if current_direction == "short" and btc_row["close"] > btc_row["ema50"]:
        return True
    return False


def run_backtest(all_data, start, end, max_positions, initial=10_000.0,
                 enable_short=True):
    """スマートLONG/SHORTバックテスト"""
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)
    syms = list(all_data.keys())
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    cash = initial
    positions = {}  # sym -> {side, qty, entry_price, leverage, entry_ts, alloc_usd}
    current_direction = "none"  # ポートフォリオの現在の方向
    equity_curve = []
    trades = []

    def close_all(date, today_rows, reason=""):
        nonlocal cash
        closed_count = 0
        for sym in list(positions.keys()):
            if sym not in today_rows:
                continue
            r = today_rows[sym]
            p = positions[sym]
            raw = r["close"]
            if p["side"] == "long":
                exit_price = raw * (1 - SLIP)
                pnl = p["qty"] * (exit_price - p["entry_price"]) * p["leverage"]
            else:
                exit_price = raw * (1 + SLIP)
                pnl = p["qty"] * (p["entry_price"] - exit_price) * p["leverage"]
            notional = p["qty"] * exit_price * p["leverage"]
            pnl -= notional * FEE
            hold_h = (date - p["entry_ts"]).total_seconds() / 3600
            pnl -= notional * FUNDING_PH * hold_h
            cash += p["alloc_usd"] + pnl
            trades.append({
                "symbol": sym, "side": p["side"],
                "pnl": pnl, "hold_h": hold_h, "reason": reason,
            })
            del positions[sym]
            closed_count += 1
        return closed_count

    for date in dates:
        if date not in btc_df.index:
            continue
        btc_row = btc_df.loc[date]

        # 当日の全銘柄データ
        today_rows = {}
        for sym in syms:
            df = all_data[sym]
            if date in df.index:
                r = df.loc[date]
                if not pd.isna(r.get("ema200")) and not pd.isna(r.get("adx")):
                    today_rows[sym] = r

        regime = btc_regime(btc_row)

        # ━━ 1. 高速撤退チェック ━━
        if positions and current_direction != "none":
            if fast_exit_signal(btc_row, current_direction):
                close_all(date, today_rows, reason="fast_exit_ema50")
                current_direction = "none"

        # ━━ 2. レジーム変更チェック ━━
        if positions:
            target_direction = "long" if regime == "bull" else ("short" if regime == "bear" else "none")
            if target_direction != current_direction:
                close_all(date, today_rows, reason="regime_change")
                current_direction = "none"

        # ━━ 3. 個別銘柄の方向維持チェック ━━
        for sym in list(positions.keys()):
            if sym not in today_rows:
                continue
            direction, lev = symbol_signal(today_rows[sym], regime)
            p = positions[sym]
            if direction != p["side"] or lev == 0:
                # この1銘柄だけ決済
                r = today_rows[sym]
                raw = r["close"]
                if p["side"] == "long":
                    exit_price = raw * (1 - SLIP)
                    pnl = p["qty"] * (exit_price - p["entry_price"]) * p["leverage"]
                else:
                    exit_price = raw * (1 + SLIP)
                    pnl = p["qty"] * (p["entry_price"] - exit_price) * p["leverage"]
                notional = p["qty"] * exit_price * p["leverage"]
                pnl -= notional * FEE
                hold_h = (date - p["entry_ts"]).total_seconds() / 3600
                pnl -= notional * FUNDING_PH * hold_h
                cash += p["alloc_usd"] + pnl
                trades.append({"symbol": sym, "side": p["side"], "pnl": pnl, "hold_h": hold_h})
                del positions[sym]

        # ━━ 4. 新規エントリー ━━
        if regime in ("bull", "bear"):
            if regime == "bear" and not enable_short:
                pass  # SHORT無効時はベアでエントリーしない
            else:
                allowed_dir = "long" if regime == "bull" else "short"
                open_slots = max_positions - len(positions)
                if open_slots > 0:
                    candidates = []
                    for sym, r in today_rows.items():
                        if sym in positions:
                            continue
                        direction, lev = symbol_signal(r, regime)
                        if direction == allowed_dir and lev > 0:
                            candidates.append((sym, r, direction, lev))
                    candidates.sort(key=lambda x: x[1]["adx"], reverse=True)
                    candidates = candidates[:open_slots]
                    if candidates:
                        per_slot = cash / max(open_slots, 1)
                        for sym, r, direction, lev in candidates:
                            if per_slot < 10:
                                break
                            raw = r["close"]
                            if direction == "long":
                                entry_price = raw * (1 + SLIP)
                            else:
                                entry_price = raw * (1 - SLIP)
                            qty = per_slot / entry_price
                            notional = per_slot * lev
                            cash -= per_slot + notional * FEE
                            positions[sym] = {
                                "side": direction,
                                "qty": qty, "entry_price": entry_price,
                                "leverage": lev, "entry_ts": date,
                                "alloc_usd": per_slot,
                            }
                        current_direction = allowed_dir

        # ━━ 5. mark-to-market ━━
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
    final_rows = {}
    for sym in positions.keys():
        df = all_data[sym]
        ld = [d for d in df.index if d <= final_date]
        if ld:
            final_rows[sym] = df.loc[ld[-1]]
    close_all(final_date, final_rows, reason="final")

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

    long_trades  = [t for t in trades if t.get("side") == "long"]
    short_trades = [t for t in trades if t.get("side") == "short"]
    return {
        "final": final,
        "total_ret": total_ret,
        "monthly_avg": monthly.mean() if len(monthly) else 0,
        "max_dd": max_dd,
        "n_trades": len(trades),
        "n_long": len(long_trades),
        "n_short": len(short_trades),
        "long_pnl":  sum(t["pnl"] for t in long_trades),
        "short_pnl": sum(t["pnl"] for t in short_trades),
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
        ("2021-01-01", "2021-12-31", "2021 暴落"),
        ("2022-01-01", "2022-12-31", "2022 ベア"),
        ("2023-01-01", "2023-12-31", "2023 回復"),
        ("2024-01-01", "2024-12-31", "2024 新高値"),
    ]

    max_pos_configs = [20, 50]

    print(f"\n{'=' * 120}")
    print(f"🧪 スマートLONG/SHORT戦略（BTCレジーム切替 + 高速撤退）")
    print(f"{'=' * 120}")

    results = {}
    for start, end, label in periods:
        print(f"\n▶ {label} ({start} 〜 {end})")
        print(f"  {'保有':>5s} {'戦略':>10s} | {'最終':>10s} | {'リターン':>8s} | "
              f"{'月':>6s} | {'DD':>6s} | {'勝率':>5s} | {'L':>3s}/{'S':>3s} | "
              f"{'LONG$':>9s} | {'SHORT$':>9s}")
        print("  " + "-" * 115)
        for mp in max_pos_configs:
            # LONG only
            r = run_backtest(all_data, start, end, mp, enable_short=False)
            print(f"  {mp:>4d}件 {'LONGのみ':>9s} | ${r['final']:>8,.0f} | {r['total_ret']:+7.1f}% | "
                  f"{r['monthly_avg']:+5.2f}% | {r['max_dd']:>4.1f}% | "
                  f"{r['win_rate']:>4.1f}% | {r['n_long']:>3d}/{r['n_short']:>3d} | "
                  f"${r['long_pnl']:+7.0f} | ${r['short_pnl']:+7.0f}")
            results[f"{label}_mp{mp}_L"] = r

            # LONG + SHORT (smart)
            r = run_backtest(all_data, start, end, mp, enable_short=True)
            print(f"  {mp:>4d}件 {'スマートL+S':>9s} | ${r['final']:>8,.0f} | {r['total_ret']:+7.1f}% | "
                  f"{r['monthly_avg']:+5.2f}% | {r['max_dd']:>4.1f}% | "
                  f"{r['win_rate']:>4.1f}% | {r['n_long']:>3d}/{r['n_short']:>3d} | "
                  f"${r['long_pnl']:+7.0f} | ${r['short_pnl']:+7.0f}")
            results[f"{label}_mp{mp}_LS"] = r
            print("  " + "-" * 115)

    out = Path("/Users/sanosano/projects/kimochi-max/results/smart_longshort.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
