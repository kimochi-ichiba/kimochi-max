"""
反復V2: 前回の失敗対策
=====================
❌ 2023-2024 全滅 → SHORT がベア相場初期の反発で死
✅ 対策:
   - SHORT追加条件: BTCが直近14日高値を更新してない (上昇中なら短絡しない)
   - SHORT追加条件: BTC ADX >= 25 (明確なベアトレンドのみ)
   - SHORT完全無効化の選択肢も追加
"""
from __future__ import annotations
import sys, json, time
from pathlib import Path
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

import pandas as pd
import numpy as np
from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import assert_binance_source
from _multipos_backtest import UNIVERSE_50
from _rsi_short_backtest import fetch_with_rsi

FEE        = 0.0006
SLIP       = 0.0003
FUNDING_PH = 0.0000125


def run_v3(all_data, start, end, config, initial=10_000.0):
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    syms = list(all_data.keys())

    cash = initial
    positions = {}
    equity_curve = []
    trades = []

    for date in dates:
        if date not in btc_df.index:
            continue
        btc_row = btc_df.loc[date]
        btc_hist = btc_df[btc_df.index <= date]
        today_rows = {}
        for sym in syms:
            df = all_data[sym]
            if date in df.index:
                r = df.loc[date]
                if not pd.isna(r.get("ema200")) and not pd.isna(r.get("adx")):
                    today_rows[sym] = r

        # ━ BTCレジーム判定（超厳格） ━
        btc_p = btc_row["close"]
        btc_e50 = btc_row["ema50"]
        btc_e200 = btc_row["ema200"]
        btc_adx = btc_row.get("adx", 0)

        regime = "neutral"
        if not pd.isna(btc_e200):
            if btc_p > btc_e200 and btc_e50 > btc_e200:
                regime = "bull"
            elif btc_p < btc_e200:
                # 新条件: SHORTを許可するには以下も必要
                # (a) BTC ADX >= 25 (明確なベアトレンド)
                # (b) BTC が 14日高値を更新していない (反発中でない)
                if len(btc_hist) >= 14:
                    recent_14_high = btc_hist.tail(14)["close"].max()
                    bounced = btc_p >= recent_14_high * 0.98
                    if btc_adx >= config["btc_adx_for_short"] and not bounced:
                        regime = "bear"

        # ボラブレーキ
        btc_atr_pct = (btc_row["atr"] / btc_row["close"] * 100) if not pd.isna(btc_row.get("atr")) else 0
        vol_brake = btc_atr_pct > config["vol_brake_atr_pct"]

        # ━ 決済チェック ━
        for sym in list(positions.keys()):
            if sym not in today_rows: continue
            r = today_rows[sym]
            p = positions[sym]
            cur = r["close"]
            if p["side"] == "long":
                p["peak_price"] = max(p.get("peak_price", p["entry_price"]), cur)
                adverse = (p["entry_price"] - cur) / p["entry_price"]
                favorable = (p["peak_price"] - p["entry_price"]) / p["entry_price"]
            else:
                p["peak_price"] = min(p.get("peak_price", p["entry_price"]), cur)
                adverse = (cur - p["entry_price"]) / p["entry_price"]
                favorable = (p["entry_price"] - p["peak_price"]) / p["entry_price"]

            close_reason = None
            if adverse >= config["stop_loss_pct"]:
                close_reason = "stop_loss"
            elif favorable >= config["trail_activate_pct"]:
                if p["side"] == "long":
                    gb = (p["peak_price"] - cur) / p["peak_price"]
                else:
                    gb = (cur - p["peak_price"]) / p["peak_price"]
                if gb >= config["trail_giveback_pct"]:
                    close_reason = "trail_exit"
            required = "bull" if p["side"] == "long" else "bear"
            if regime != required:
                close_reason = close_reason or "regime"

            if close_reason:
                lev = p["leverage"]
                if p["side"] == "long":
                    exit_p = cur * (1 - SLIP)
                    pnl = p["qty"] * (exit_p - p["entry_price"]) * lev
                else:
                    exit_p = cur * (1 + SLIP)
                    pnl = p["qty"] * (p["entry_price"] - exit_p) * lev
                notional = p["qty"] * exit_p * lev
                pnl -= notional * FEE
                hold_h = (date - p["entry_ts"]).total_seconds() / 3600
                pnl -= notional * FUNDING_PH * hold_h
                cash += p["alloc_usd"] + pnl
                trades.append({"sym": sym, "side": p["side"], "pnl": pnl})
                del positions[sym]

        # エントリー
        if regime in ("bull", "bear") and not vol_brake:
            if regime == "bear" and not config["enable_short"]:
                pass
            else:
                direction = "long" if regime == "bull" else "short"
                slots = config["max_pos"] - len(positions)
                if slots > 0:
                    candidates = []
                    for sym, r in today_rows.items():
                        if sym in positions: continue
                        if r["adx"] < config["adx_min"]: continue
                        price, ema200 = r["close"], r["ema200"]
                        rsi = r.get("rsi", 50)
                        if pd.isna(rsi): continue
                        if direction == "long":
                            if price > ema200:
                                candidates.append((sym, r))
                        else:
                            if price < ema200 and rsi >= config["rsi_short_min"]:
                                candidates.append((sym, r))
                    candidates.sort(key=lambda x: x[1]["adx"], reverse=True)
                    candidates = candidates[:slots]
                    if candidates:
                        per = cash / max(slots, 1) * 0.95
                        for sym, r in candidates:
                            if per < 10: break
                            adx = r["adx"]
                            lev = config["lev_high"] if adx >= config["adx_lev2"] else config["lev_low"]
                            raw = r["close"]
                            ep = raw * (1 + SLIP) if direction == "long" else raw * (1 - SLIP)
                            qty = per / ep
                            notional = per * lev
                            cash -= per + notional * FEE
                            positions[sym] = {
                                "side": direction, "qty": qty, "entry_price": ep,
                                "leverage": lev, "entry_ts": date,
                                "alloc_usd": per, "peak_price": ep,
                            }

        unreal = 0.0
        for sym, p in positions.items():
            if sym in today_rows:
                cur = today_rows[sym]["close"]
                if p["side"] == "long":
                    unreal += p["qty"] * (cur - p["entry_price"]) * p["leverage"]
                else:
                    unreal += p["qty"] * (p["entry_price"] - cur) * p["leverage"]
        equity_curve.append({"ts": date, "equity": max(0, cash + unreal)})

    for sym in list(positions.keys()):
        df = all_data[sym]
        ld = [d for d in df.index if d <= dates[-1]]
        if not ld: continue
        last_row = df.loc[ld[-1]]
        p = positions[sym]
        raw = last_row["close"]
        if p["side"] == "long":
            exit_p = raw * (1 - SLIP)
            pnl = p["qty"] * (exit_p - p["entry_price"]) * p["leverage"]
        else:
            exit_p = raw * (1 + SLIP)
            pnl = p["qty"] * (p["entry_price"] - exit_p) * p["leverage"]
        notional = p["qty"] * exit_p * p["leverage"]
        pnl -= notional * FEE
        cash += p["alloc_usd"] + pnl

    eq_df = pd.DataFrame(equity_curve).set_index("ts")
    eq_df.index = pd.to_datetime(eq_df.index)
    yearly = {}
    for y in range(2020, 2025):
        yr = eq_df[eq_df.index.year == y]["equity"]
        if len(yr) == 0: continue
        s = yr.iloc[0]; e = yr.iloc[-1]
        yearly[y] = round((e/s - 1) * 100, 2) if s > 0 else 0
    peak, max_dd = initial, 0
    for e in eq_df["equity"]:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
    final = max(0, cash)
    total = (final - initial) / initial * 100
    avg_annual = ((final/initial) ** (1/5) - 1) * 100 if final > 0 else -100
    n_long = sum(1 for t in trades if t["side"] == "long")
    n_short = sum(1 for t in trades if t["side"] == "short")
    return {
        "final": final, "total_ret": total,
        "avg_annual_ret": avg_annual,
        "yearly": yearly, "max_dd": max_dd,
        "n_trades": len(trades), "n_long": n_long, "n_short": n_short,
        "all_positive": all(v > 0 for v in yearly.values()),
        "negative_years": sum(1 for v in yearly.values() if v < 0),
    }


if __name__ == "__main__":
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)
    print("📥 データ取得中...")
    t0 = time.time()
    all_data = fetch_with_rsi(fetcher, UNIVERSE_50, "2020-01-01", "2024-12-31")
    print(f"✅ {len(all_data)}銘柄 ({time.time()-t0:.0f}秒)\n")

    base = dict(adx_min=30, adx_lev2=40, lev_low=1.0, lev_high=2.0,
                stop_loss_pct=0.05, trail_activate_pct=0.15, trail_giveback_pct=0.03,
                max_pos=20, rsi_short_min=75, vol_brake_atr_pct=5,
                btc_adx_for_short=25)

    configs = [
        ("W01 SHORT無効 (LONG+ボラブレーキ+SL5%)",
         {**base, "enable_short": False}),
        ("W02 SHORT厳格 (BTC ADX>=30 + 反発なし)",
         {**base, "enable_short": True, "btc_adx_for_short": 30}),
        ("W03 SHORT超厳格 (BTC ADX>=35)",
         {**base, "enable_short": True, "btc_adx_for_short": 35}),
        ("W04 LONG tight SL3%",
         {**base, "enable_short": False, "stop_loss_pct": 0.03, "trail_activate_pct": 0.08, "trail_giveback_pct": 0.02}),
        ("W05 LONG Lev1のみ SL5%",
         {**base, "enable_short": False, "lev_low": 1.0, "lev_high": 1.0}),
        ("W06 LONG+SHORT分離: SHORT Lev1のみ",
         {**base, "enable_short": True, "lev_high": 1.0, "btc_adx_for_short": 30}),
        ("W07 ボラブレーキ3%(超厳格) LONG",
         {**base, "enable_short": False, "vol_brake_atr_pct": 3}),
        ("W08 LONG Lev1-3 積極",
         {**base, "enable_short": False, "lev_high": 3.0, "stop_loss_pct": 0.07}),
    ]

    print(f"{'=' * 140}")
    print(f"🔁 反復V2: 毎年プラス + 年+100%")
    print(f"{'=' * 140}")
    print(f"{'戦略':45s} | {'2020':>7s} {'2021':>7s} {'2022':>7s} {'2023':>7s} {'2024':>7s} | "
          f"{'5年計':>9s} | {'年率':>7s} | {'DD':>5s} | {'L/S':>9s} | 判定")
    print("-" * 140)

    results = {}
    for name, cfg in configs:
        r = run_v3(all_data, "2020-01-01", "2024-12-31", cfg)
        row = f"{name:45s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+6.1f}% "
        row += f"| {r['total_ret']:>+7.1f}% | {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>4.1f}% | "
        row += f"{r['n_long']:>3d}/{r['n_short']:>3d} | "
        tags = []
        if r["all_positive"]: tags.append("🎯毎年+")
        if r["avg_annual_ret"] >= 100: tags.append("🚀+100%")
        elif r["avg_annual_ret"] >= 50: tags.append("⭐+50%")
        if r["max_dd"] < 50: tags.append("🛡DD低")
        row += " ".join(tags) if tags else f"{r['negative_years']}年負"
        print(row)
        results[name] = r

    winners = [(n, r) for n, r in results.items() if r["all_positive"]]
    winners_100 = [(n, r) for n, r in results.items() if r["all_positive"] and r["avg_annual_ret"] >= 100]
    print(f"\n{'=' * 140}")
    if winners_100:
        print(f"🎉 毎年プラス + 年+100% 両達成: {len(winners_100)}戦略")
        for n, r in sorted(winners_100, key=lambda x: -x[1]["avg_annual_ret"]):
            print(f"   ✨ {n}: 年率{r['avg_annual_ret']:+.1f}% / 5年{r['total_ret']:+.1f}% / DD{r['max_dd']:.1f}%")
    elif winners:
        print(f"✅ 毎年プラス達成: {len(winners)}戦略")
        for n, r in sorted(winners, key=lambda x: -x[1]["avg_annual_ret"]):
            print(f"   🎯 {n}: 年率{r['avg_annual_ret']:+.1f}% / DD{r['max_dd']:.1f}%")
    else:
        print(f"⚠️ 毎年プラス達成なし")
    print(f"{'=' * 140}")

    out = Path("/Users/sanosano/projects/kimochi-max/results/iterate_100pct_v2.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {out}")
