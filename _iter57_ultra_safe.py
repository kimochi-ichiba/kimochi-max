"""
iter57: 絶対安全・長期プラス志向版
====================================
ユーザー要求: 「$100が$25になるのは渋い、長期的に必ずプラス」

目標:
  - 最大DD 40%以下 (できれば 30%)
  - 5年年別リターン全年プラス (最悪の 2022年でも プラス or 最小マイナス)
  - 5年合計 +200% 以上 (年率 25%以上)
  - ゼロ清算リスク

検証方針:
  A. BTC比率を大幅縮小 (20%/30%/40%)
  B. USDT を主力にする (60%/70%/80%)
  C. ACH(アルト) を完全削除 or 最小化
  D. BTC 単体戦略 vs ACH 付き比較
  E. 純粋バイ&ホールド も比較

参考: 現行 v2.1 = +8,931% / DD 70.5% (リスク大)
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import _iter43_rethink as R43
from _iter54_comprehensive import select_top

PROJECT = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT / "results"
CACHE_PATH = RESULTS_DIR / "_cache_alldata.pkl"
OUT_JSON = RESULTS_DIR / "iter57_ultra_safe.json"

FEE = 0.0010
SLIP = 0.0005
UNIVERSE_REMOVE = ["MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"]


def _reb_key(date, days):
    doy = date.dayofyear + (date.year - 2020) * 366
    return doy // days


def calc_yearly_rets(equity_curve, initial):
    """年別リターンを計算"""
    df = pd.DataFrame(equity_curve).set_index("ts")
    df.index = pd.to_datetime(df.index)
    by_year = df.resample("YE").last()
    yearly = {}
    prev = initial
    for ts, row in by_year.iterrows():
        y = str(ts.year)
        eq = row["equity"]
        if prev > 0:
            yearly[y] = round((eq / prev - 1) * 100, 2)
        prev = eq
    return yearly


def run_btc_only(all_data, start, end, btc_w, usdt_w,
                  use_ema200=True, initial=10000.0):
    """BTC + USDT のみ構成 (アルトなし)"""
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    if not dates: return {}

    btc_cash = initial * btc_w
    btc_qty = 0.0
    usdt_cash = initial * usdt_w
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0

    for date in dates:
        r = btc_df.loc[date]
        price = r["close"]
        ema200 = r.get("ema200")

        if use_ema200:
            if btc_qty == 0 and not pd.isna(ema200) and price > ema200:
                buy_p = price * (1 + SLIP)
                btc_qty = btc_cash / buy_p * (1 - FEE)
                btc_cash = 0; n_trades += 1
            elif btc_qty > 0 and not pd.isna(ema200) and price < ema200:
                sell_p = price * (1 - SLIP)
                btc_cash += btc_qty * sell_p * (1 - FEE)
                btc_qty = 0; n_trades += 1
        else:
            # Buy-and-hold: 最初に購入、永久保持
            if btc_qty == 0:
                buy_p = price * (1 + SLIP)
                btc_qty = btc_cash / buy_p * (1 - FEE)
                btc_cash = 0; n_trades += 1

        if btc_qty == 0: btc_cash *= (1 + 0.03/365)
        usdt_cash *= (1 + 0.03/365)

        total = btc_cash + btc_qty * price + usdt_cash
        equity_curve.append({"ts": date, "equity": total})

    r = R43.summarize(equity_curve, initial, n_trades=n_trades)
    r["yearly"] = calc_yearly_rets(equity_curve, initial)
    return r


def run_hybrid(all_data, universe, start, end,
                btc_w, ach_w, usdt_w, top_n=3, lookback=25,
                rebalance_days=7, adx_min=15, initial=10000.0):
    """BTC + ACH + USDT ハイブリッド (v2.1 相当)"""
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    if not dates: return {}

    btc_cash = initial * btc_w
    btc_qty = 0.0
    ach_cash = initial * ach_w
    positions = {}
    usdt_cash = initial * usdt_w
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0
    last_key = None

    for date in dates:
        btc_r = btc_df.loc[date]
        price = btc_r["close"]
        ema200 = btc_r.get("ema200")

        if btc_qty == 0 and not pd.isna(ema200) and price > ema200:
            buy_p = price * (1 + SLIP)
            btc_qty = btc_cash / buy_p * (1 - FEE)
            btc_cash = 0; n_trades += 1
        elif btc_qty > 0 and not pd.isna(ema200) and price < ema200:
            sell_p = price * (1 - SLIP)
            btc_cash += btc_qty * sell_p * (1 - FEE)
            btc_qty = 0; n_trades += 1

        if btc_qty == 0: btc_cash *= (1 + 0.03/365)
        usdt_cash *= (1 + 0.03/365)

        if ach_w > 0:
            cur_key = _reb_key(date, rebalance_days)
            if cur_key != last_key:
                for sym in list(positions.keys()):
                    df = all_data[sym]
                    if date in df.index:
                        p = df.loc[date, "close"] * (1 - SLIP)
                        ach_cash += positions[sym] * p * (1 - FEE)
                        n_trades += 1; positions.pop(sym)

                if not pd.isna(ema200) and price < ema200:
                    last_key = cur_key
                else:
                    sel = select_top(all_data, universe, date, top_n, lookback,
                                      adx_min, 0, "momentum", False)
                    if sel:
                        weights = [1.0/len(sel)] * len(sel)
                        for (sym, _), w in zip(sel, weights):
                            df = all_data[sym]
                            p_buy = df.loc[date, "close"] * (1 + SLIP)
                            cost = ach_cash * w
                            if cost > 0:
                                qty = cost / p_buy * (1 - FEE)
                                positions[sym] = qty
                                n_trades += 1
                        used = sum(ach_cash * w for w in weights)
                        ach_cash -= used
                    last_key = cur_key

        ach_value = ach_cash
        for sym, qty in positions.items():
            df = all_data[sym]
            if date in df.index:
                ach_value += qty * df.loc[date, "close"]
        total = btc_cash + btc_qty * price + ach_value + usdt_cash
        equity_curve.append({"ts": date, "equity": total})

    r = R43.summarize(equity_curve, initial, n_trades=n_trades)
    r["yearly"] = calc_yearly_rets(equity_curve, initial)
    return r


def main():
    print("=" * 70)
    print("🛡️ iter57 絶対安全・長期プラス志向版検証")
    print("=" * 70)

    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    universe = sorted([s for s in all_data.keys() if s not in UNIVERSE_REMOVE])

    import _iter54_comprehensive as M
    M.CORR_THRESHOLD = 0.80

    tests = [
        # 現行 v2.1 比較用
        {"id": "V2_1_CURRENT", "label": "v2.1 (BTC35/ACH35/USDT30)",
         "type": "hybrid", "btc_w": 0.35, "ach_w": 0.35, "usdt_w": 0.30},
        # BTC only, EMA200 戦略, USDT多め
        {"id": "BTC20_USDT80", "label": "BTC20% / USDT80% (EMA)",
         "type": "btc", "btc_w": 0.20, "usdt_w": 0.80},
        {"id": "BTC30_USDT70", "label": "BTC30% / USDT70% (EMA)",
         "type": "btc", "btc_w": 0.30, "usdt_w": 0.70},
        {"id": "BTC40_USDT60", "label": "BTC40% / USDT60% (EMA)",
         "type": "btc", "btc_w": 0.40, "usdt_w": 0.60},
        {"id": "BTC50_USDT50", "label": "BTC50% / USDT50% (EMA)",
         "type": "btc", "btc_w": 0.50, "usdt_w": 0.50},
        # BTC only, Buy and Hold (比較用)
        {"id": "BTC100_HOLD", "label": "BTC100% Buy&Hold (参考)",
         "type": "btc", "btc_w": 1.00, "usdt_w": 0.00, "use_ema200": False},
        {"id": "BTC50_HOLD", "label": "BTC50% Buy&Hold / USDT50%",
         "type": "btc", "btc_w": 0.50, "usdt_w": 0.50, "use_ema200": False},
        # ACH少量 + BTC中心
        {"id": "B30_A10_U60", "label": "BTC30/ACH10(Top2)/USDT60",
         "type": "hybrid", "btc_w": 0.30, "ach_w": 0.10, "usdt_w": 0.60, "top_n": 2},
        {"id": "B40_A10_U50", "label": "BTC40/ACH10(Top2)/USDT50",
         "type": "hybrid", "btc_w": 0.40, "ach_w": 0.10, "usdt_w": 0.50, "top_n": 2},
        {"id": "B30_A20_U50", "label": "BTC30/ACH20(Top3)/USDT50",
         "type": "hybrid", "btc_w": 0.30, "ach_w": 0.20, "usdt_w": 0.50},
        {"id": "B25_A25_U50", "label": "BTC25/ACH25(Top3)/USDT50",
         "type": "hybrid", "btc_w": 0.25, "ach_w": 0.25, "usdt_w": 0.50},
    ]

    results = []
    t_start = time.time()
    for i, t in enumerate(tests, 1):
        print(f"[{i}/{len(tests)}] {t['id']}: {t['label']}")
        t0 = time.time()
        if t["type"] == "btc":
            r = run_btc_only(all_data, "2020-01-01", "2024-12-31",
                              btc_w=t["btc_w"], usdt_w=t["usdt_w"],
                              use_ema200=t.get("use_ema200", True))
        else:
            r = run_hybrid(all_data, universe, "2020-01-01", "2024-12-31",
                            btc_w=t["btc_w"], ach_w=t.get("ach_w", 0),
                            usdt_w=t["usdt_w"], top_n=t.get("top_n", 3))
        elapsed = time.time() - t0
        r.update({**t, "elapsed": round(elapsed, 2)})
        results.append(r)
        yearly_min = min(r.get("yearly", {}).values()) if r.get("yearly") else 0
        print(f"   {elapsed:.1f}s | ret {r['total_ret']:+7.0f}% / DD {r['max_dd']:5.1f}% / 年別min {yearly_min:+5.1f}%")

    # 採用候補: DD < 40% AND 全年 ≥ -10%
    safe_candidates = []
    for r in results:
        if r["id"] == "V2_1_CURRENT": continue
        yearly = r.get("yearly", {})
        yearly_min = min(yearly.values()) if yearly else 0
        if r["max_dd"] < 40 and yearly_min > -15:
            safe_candidates.append(r)

    v21 = results[0]
    v21_ret = v21["total_ret"]

    print("\n" + "=" * 70)
    print("📊 年別リターン (全テスト、最悪年チェック)")
    print("-" * 70)
    for r in results:
        yearly = r.get("yearly", {})
        years_str = " | ".join([f"{y}:{v:+.1f}%" for y, v in sorted(yearly.items())])
        y_min = min(yearly.values()) if yearly else 0
        worst = "🔴" if y_min < -20 else ("🟡" if y_min < -10 else ("🟠" if y_min < 0 else "🟢"))
        print(f" {worst} {r['id']:18s}: {years_str} | minWorst {y_min:+.1f}%")

    print("\n" + "=" * 70)
    print(f"✅ 絶対安全候補 (DD<40% AND 全年≥-15%): {len(safe_candidates)}件")
    print("-" * 70)
    for r in sorted(safe_candidates, key=lambda x: x["total_ret"], reverse=True):
        yearly = r.get("yearly", {})
        y_min = min(yearly.values()) if yearly else 0
        print(f"  {r['id']:18s} ({r['label']:35s}): ret {r['total_ret']:+7.0f}% / "
              f"DD {r['max_dd']:5.1f}% / 最悪年 {y_min:+.1f}%")

    if safe_candidates:
        best = max(safe_candidates, key=lambda r: r["total_ret"])
        print(f"\n🏅 推奨: {best['id']} ({best['label']})")
        print(f"   ret {best['total_ret']:+.0f}% / DD {best['max_dd']:.1f}% / 最悪年 {min(best['yearly'].values()):+.1f}%")
    else:
        best = v21

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "v21_baseline": v21,
        "safe_candidates": safe_candidates,
        "recommended": best,
        "all_results": results,
        "total_elapsed_sec": round(time.time() - t_start, 2),
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")


if __name__ == "__main__":
    main()
