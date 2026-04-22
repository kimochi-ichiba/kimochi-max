"""
Iter46: ハイブリッド戦略(BTC + AC)の徹底最適化
====================================================
目的: ハイブリッド50/50の良さ（年率+74% / DD 48%）を土台に、
      DDをもっと下げながらリターンも確保する最適版を探す

10パターン:
  H01 50/50 (ベースライン)                 = R10と同じ
  H02 60/40 BTC多め
  H03 70/30 BTC重め
  H04 40/60 AC多め（攻め）
  H05 50/50 + USDT10%バッファ (合計110%)→40/40/20
  H06 30/30/40 BTC/AC/USDT (現金厚め)
  H07 40/20/40 BTC/AC/USDT
  H08 50/30/20 BTC/AC/USDT
  H09 BTC50% + ACH50% (ACをACHに置換、安全版)
  H10 BTC40% + ACH30% + USDT30%
  H11 モメンタム入り: BTC30% + モメ20% + USDT50%
  H12 BTC40% + ACH30% + モメ15% + USDT15%
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np
import _iter43_rethink as R43

CACHE_PATH = (Path(__file__).resolve().parent / "results" / "_cache_alldata.pkl")
OUT_PATH = (Path(__file__).resolve().parent / "results" / "iter46_hybrid.json")


def load_data():
    with open(CACHE_PATH, "rb") as f:
        return pickle.load(f)


def run_multi_portfolio(all_data, start, end, parts, initial=10_000.0):
    """複数戦略を合成する"""
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]

    part_eqs = []
    total_trades = 0
    total_liq = 0
    for p in parts:
        part_init = initial * p["weight"]
        if p.get("fn") is None:
            rate = p.get("usdt_rate", 0.03)
            eq_list = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": part_init}]
            cash = part_init
            prev = dates[0]
            for d in dates:
                days = (d - prev).days
                cash *= (1 + rate) ** (days / 365)
                prev = d
                eq_list.append({"ts": d, "equity": cash})
            part_eqs.append(eq_list)
        else:
            r = p["fn"](all_data, start, end, initial=part_init, **p.get("kwargs", {}))
            eq_weekly = r["equity_weekly"]
            part_eqs.append([{"ts": pd.Timestamp(e["ts"]), "equity": e["equity"]} for e in eq_weekly])
            total_trades += r.get("n_trades", 0)
            total_liq += r.get("n_liquidations", 0)

    all_ts = sorted(set(ts for part in part_eqs for ts in (e["ts"] for e in part)))
    last_vals = [initial * p["weight"] for p in parts]
    idxs = [0] * len(parts)
    combined = []
    for ts in all_ts:
        for i, part in enumerate(part_eqs):
            while idxs[i] < len(part) - 1 and pd.Timestamp(part[idxs[i] + 1]["ts"]) <= ts:
                idxs[i] += 1
            if idxs[i] < len(part):
                last_vals[i] = part[idxs[i]]["equity"]
        combined.append({"ts": ts, "equity": round(sum(last_vals), 2)})

    r = R43.summarize(combined, initial, n_trades=total_trades, n_liq=total_liq)
    return r


def run_ac(all_data, start, end, initial=10_000.0):
    """Iter41 AC戦略 (BTC EMA50フィルタ + ピラミ2)"""
    import _iter42_improve as iter42
    b = dict(
        risk_per_trade_pct=0.02, max_pos=20, stop_loss_pct=0.22,
        tp1_pct=0.10, tp1_fraction=0.25, tp2_pct=0.30, tp2_fraction=0.35,
        trail_activate_pct=0.50, trail_giveback_pct=0.15,
        adx_min=50, adx_lev2=60, adx_lev3=70,
        lev_low=2.5, lev_mid=2.5, lev_high=2.5,
        breakout_pct=0.05, rsi_long_min=50, rsi_long_max=75, rsi_short_min=85,
        enable_short=False, year_profit_lock=True,
        profit_lock_pct=0.25, btc_adx_for_short=40,
        max_margin_per_pos_pct=0.10,
        max_pos_override=12,
        pyramid_enabled=True, pyramid_max=2,
        pyramid_trigger_pct=0.10, pyramid_size_pct=0.5,
        btc_ema50_filter=True,
    )
    b["max_pos"] = 12
    r = iter42.run_iter42(all_data, start, end, b, initial=initial)
    return r


def run_ach(all_data, start, end, initial=10_000.0):
    """ACH: AC + 動的レバ (ACより安全)"""
    import _iter42_improve as iter42
    b = dict(
        risk_per_trade_pct=0.02, max_pos=12, stop_loss_pct=0.22,
        tp1_pct=0.10, tp1_fraction=0.25, tp2_pct=0.30, tp2_fraction=0.35,
        trail_activate_pct=0.50, trail_giveback_pct=0.15,
        adx_min=50, adx_lev2=60, adx_lev3=70,
        lev_low=2.5, lev_mid=2.5, lev_high=2.5,
        breakout_pct=0.05, rsi_long_min=50, rsi_long_max=75, rsi_short_min=85,
        enable_short=False, year_profit_lock=True,
        profit_lock_pct=0.25, btc_adx_for_short=40,
        max_margin_per_pos_pct=0.10,
        pyramid_enabled=True, pyramid_max=2,
        pyramid_trigger_pct=0.10, pyramid_size_pct=0.5,
        btc_ema50_filter=True,
        dynamic_leverage=True,
    )
    r = iter42.run_iter42(all_data, start, end, b, initial=initial)
    return r


def tag(r):
    t = []
    if r["all_positive"]: t.append("🎯毎年+")
    elif r["no_negative"]: t.append("🟢ﾏｲﾅｽ無")
    if r["max_dd"] < 25: t.append("🛡🛡DD<25%")
    elif r["max_dd"] < 35: t.append("🛡DD<35%")
    elif r["max_dd"] < 45: t.append("◯DD<45%")
    if r["avg_annual_ret"] >= 70: t.append("🚀+70%")
    elif r["avg_annual_ret"] >= 50: t.append("⭐+50%")
    elif r["avg_annual_ret"] >= 30: t.append("💪+30%")
    if r["sharpe"] >= 1.5: t.append("⚡Sharpe優")
    elif r["sharpe"] >= 1.0: t.append("◯Sharpe良")
    return " ".join(t)


def main():
    print("=" * 145)
    print("🔀 Iter46: ハイブリッド戦略の徹底最適化 (DD下げつつ年率確保)")
    print("=" * 145)
    all_data = load_data()
    start, end = "2020-01-01", "2024-12-31"

    runs = [
        ("H01 50/50 (R10ベースライン)",
         [{"fn": R43.run_btc_mild, "weight": 0.50, "kwargs":{"cash_rate":0.03}},
          {"fn": run_ac,            "weight": 0.50}]),
        ("H02 BTC60%+AC40%",
         [{"fn": R43.run_btc_mild, "weight": 0.60, "kwargs":{"cash_rate":0.03}},
          {"fn": run_ac,            "weight": 0.40}]),
        ("H03 BTC70%+AC30%",
         [{"fn": R43.run_btc_mild, "weight": 0.70, "kwargs":{"cash_rate":0.03}},
          {"fn": run_ac,            "weight": 0.30}]),
        ("H04 BTC40%+AC60% (攻め)",
         [{"fn": R43.run_btc_mild, "weight": 0.40, "kwargs":{"cash_rate":0.03}},
          {"fn": run_ac,            "weight": 0.60}]),
        ("H05 BTC40%+AC40%+USDT20%",
         [{"fn": R43.run_btc_mild, "weight": 0.40, "kwargs":{"cash_rate":0.03}},
          {"fn": run_ac,            "weight": 0.40},
          {"weight": 0.20, "usdt_rate": 0.03}]),
        ("H06 BTC30%+AC30%+USDT40% (現金厚)",
         [{"fn": R43.run_btc_mild, "weight": 0.30, "kwargs":{"cash_rate":0.03}},
          {"fn": run_ac,            "weight": 0.30},
          {"weight": 0.40, "usdt_rate": 0.03}]),
        ("H07 BTC50%+AC30%+USDT20%",
         [{"fn": R43.run_btc_mild, "weight": 0.50, "kwargs":{"cash_rate":0.03}},
          {"fn": run_ac,            "weight": 0.30},
          {"weight": 0.20, "usdt_rate": 0.03}]),
        ("H08 BTC50%+ACH50% (ACをACHに, 安全)",
         [{"fn": R43.run_btc_mild, "weight": 0.50, "kwargs":{"cash_rate":0.03}},
          {"fn": run_ach,           "weight": 0.50}]),
        ("H09 BTC40%+ACH30%+USDT30% (安全)",
         [{"fn": R43.run_btc_mild, "weight": 0.40, "kwargs":{"cash_rate":0.03}},
          {"fn": run_ach,           "weight": 0.30},
          {"weight": 0.30, "usdt_rate": 0.03}]),
        ("H10 BTC40%+ACH30%+モメ15%+USDT15%",
         [{"fn": R43.run_btc_mild, "weight": 0.40, "kwargs":{"cash_rate":0.03}},
          {"fn": run_ach,           "weight": 0.30},
          {"fn": R43.run_momentum, "weight": 0.15, "kwargs":{"top_n":3,"lookback_days":90}},
          {"weight": 0.15, "usdt_rate": 0.03}]),
        ("H11 BTC40%+ACH40%+USDT20% (バランス安全)",
         [{"fn": R43.run_btc_mild, "weight": 0.40, "kwargs":{"cash_rate":0.03}},
          {"fn": run_ach,           "weight": 0.40},
          {"weight": 0.20, "usdt_rate": 0.03}]),
        ("H12 BTC60%+ACH20%+USDT20% (保守バランス)",
         [{"fn": R43.run_btc_mild, "weight": 0.60, "kwargs":{"cash_rate":0.03}},
          {"fn": run_ach,           "weight": 0.20},
          {"weight": 0.20, "usdt_rate": 0.03}]),
    ]

    print(f"\n{'No':4s} | {'戦略':40s} | {'20':>5s} {'21':>6s} {'22':>5s} {'23':>5s} {'24':>5s} | "
          f"{'年率':>6s} | {'★DD':>6s} | {'Sh':>4s} | 清算 | 最終       | 判定")
    print("-" * 165)

    results = {}
    for name, parts in runs:
        r = run_multi_portfolio(all_data, start, end, parts)
        row = f"{name[:4]} | {name:40s} | "
        for y in range(2020, 2025):
            v = r["yearly"].get(y, 0)
            row += f"{v:>+4.1f}% "[:7]
        row += f"| {r['avg_annual_ret']:>+5.1f}% | {r['max_dd']:>5.1f}% | "
        row += f"{r['sharpe']:>4.2f} | {r.get('n_liquidations',0):>3d} | "
        row += f"${r['final']:>8,.0f} | "
        row += tag(r)
        print(row, flush=True)
        results[name] = r

    # ベスト選定
    # DD別ベスト
    best_by_dd = {}
    for ddc in [25, 30, 35, 40, 45, 50]:
        low = [(n, r) for n, r in results.items() if r["max_dd"] < ddc]
        if low:
            best = max(low, key=lambda x: x[1]["avg_annual_ret"])
            best_by_dd[f"DD<{ddc}%"] = {"name": best[0], "annual": best[1]["avg_annual_ret"],
                                         "dd": best[1]["max_dd"], "sharpe": best[1]["sharpe"],
                                         "final": best[1]["final"]}

    # 毎年プラス
    pos = [(n, r) for n, r in results.items() if r["no_negative"]]
    best_pos = max(pos, key=lambda x: x[1]["avg_annual_ret"]) if pos else None

    # Sharpe最大
    best_sharpe = max(results.items(), key=lambda x: x[1]["sharpe"])

    print("\n" + "=" * 145)
    print("🏆 DD別ベスト:")
    for ddc, b in best_by_dd.items():
        print(f"   {ddc:8s} 最良: {b['name']:40s} 年率{b['annual']:+.1f}% DD{b['dd']:.1f}% "
              f"Sharpe{b['sharpe']:.2f} 最終${b['final']:,.0f}")
    if best_pos:
        n, r = best_pos
        print(f"🎯 毎年プラス達成: {n} 年率{r['avg_annual_ret']:+.1f}% DD{r['max_dd']:.1f}%")
    n, r = best_sharpe
    print(f"⚡ Sharpe最良  : {n} Sharpe{r['sharpe']:.2f} 年率{r['avg_annual_ret']:+.1f}% DD{r['max_dd']:.1f}%")
    print("=" * 145)

    out = {
        "results": results,
        "best_by_dd": best_by_dd,
        "best_positive": best_pos[0] if best_pos else None,
        "best_sharpe": best_sharpe[0],
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_PATH}")


if __name__ == "__main__":
    main()
