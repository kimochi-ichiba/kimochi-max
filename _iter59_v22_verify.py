"""
iter59: v2.2 厳密検証 (3角度)
=============================
v2.2 の実装内容 (ACH即時ベア退避 + Corr 0.80 + Mom加重 + 62銘柄) を
正確に再現したバックテストで検証。

検証角度:
  1. 単純 v2.1 → v2.2 差分 (ACH即時ベア退避の純粋効果)
  2. 期間別検証 (全期間 / 2022年単独 / 2020-2022 / 2023-2024)
  3. bear退避回数・失われた上昇機会カウント
  4. v2, v2.1, v2.2 の年次リターン徹底比較

期待値 (iter58近似結果より):
  v2.1: +8,931% / DD 70.5% / 最悪年 -16.4%
  v2.2: 推定 +5,500〜7,000% / DD 63〜68% / 最悪年 -5前後
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

import pandas as pd
import _iter43_rethink as R43
from _iter54_comprehensive import select_top

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
RESULTS_DIR = PROJECT / "results"
CACHE_PATH = RESULTS_DIR / "_cache_alldata.pkl"
OUT_JSON = RESULTS_DIR / "iter59_v22_verify.json"

FEE = 0.0010
SLIP = 0.0005
UNIVERSE_REMOVE = ["MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"]


def _reb_key(date, days):
    doy = date.dayofyear + (date.year - 2020) * 366
    return doy // days


def calc_yearly_rets(equity_curve, initial):
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


def run_bt_v22_exact(all_data, universe, start, end,
                      btc_w=0.35, ach_w=0.35, usdt_w=0.30,
                      top_n=3, lookback=25, rebalance_days=7,
                      adx_min=15,
                      corr_threshold=0.80,
                      weight_method="momentum",
                      ach_bear_immediate=True,  # v2.2 新機能
                      initial=10000.0):
    """demo_runner.py v2.2 の正確な再現"""
    import _iter54_comprehensive as M
    M.CORR_THRESHOLD = corr_threshold

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
    n_bear_exits = 0
    last_key = None

    for date in dates:
        btc_r = btc_df.loc[date]
        price = btc_r["close"]
        ema200 = btc_r.get("ema200")

        btc_bullish = not pd.isna(ema200) and price > ema200

        # BTC 戦略
        if btc_qty == 0 and btc_bullish:
            buy_p = price * (1 + SLIP)
            btc_qty = btc_cash / buy_p * (1 - FEE)
            btc_cash = 0; n_trades += 1
        elif btc_qty > 0 and not btc_bullish:
            sell_p = price * (1 - SLIP)
            btc_cash += btc_qty * sell_p * (1 - FEE)
            btc_qty = 0; n_trades += 1

        # v2.2 新機能: ACH即時ベア退避 (BTC<EMA200 で即売却)
        if ach_bear_immediate and not btc_bullish and positions:
            for sym in list(positions.keys()):
                df = all_data[sym]
                if date in df.index:
                    p = df.loc[date, "close"] * (1 - SLIP)
                    ach_cash += positions[sym] * p * (1 - FEE)
                    n_trades += 1
                    positions.pop(sym)
                    n_bear_exits += 1

        if btc_qty == 0: btc_cash *= (1 + 0.03/365)
        usdt_cash *= (1 + 0.03/365)

        # ACH リバランス
        cur_key = _reb_key(date, rebalance_days)
        if cur_key != last_key:
            for sym in list(positions.keys()):
                df = all_data[sym]
                if date in df.index:
                    p = df.loc[date, "close"] * (1 - SLIP)
                    ach_cash += positions[sym] * p * (1 - FEE)
                    n_trades += 1; positions.pop(sym)

            if not btc_bullish:
                last_key = cur_key
            else:
                # Corr 0.80 + Mom 加重 (v2.1+)
                sel = select_top(all_data, universe, date, top_n, lookback,
                                  adx_min, 0, "momentum", False)
                if sel:
                    if weight_method == "momentum":
                        pos_rets = [max(r, 0.01) for _, r in sel]
                        total_w = sum(pos_rets)
                        weights = [r/total_w for r in pos_rets]
                    else:
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
    r["n_bear_exits"] = n_bear_exits
    r["final"] = equity_curve[-1]["equity"]
    return r


def main():
    print("=" * 70)
    print("🔍 iter59 v2.2 厳密検証 (3角度)")
    print("=" * 70)

    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    universe = sorted([s for s in all_data.keys() if s not in UNIVERSE_REMOVE])

    print(f"ユニバース: {len(universe)}銘柄")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 角度1: 単純 v2.1 → v2.2 差分比較
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("\n" + "=" * 70)
    print("🔬 角度1: v2.1 vs v2.2 (bear退避のみ差分) 全期間 (2020-2024)")
    print("=" * 70)

    v21 = run_bt_v22_exact(
        all_data, universe, "2020-01-01", "2024-12-31",
        btc_w=0.35, ach_w=0.35, usdt_w=0.30,
        ach_bear_immediate=False,  # v2.1 = bear退避なし
    )
    print(f"\nv2.1 (bear退避OFF): ret {v21['total_ret']:+.0f}% / DD {v21['max_dd']:.1f}% / 取引{v21['n_trades']}")
    for y, v in sorted(v21["yearly"].items()):
        print(f"    {y}: {v:+.1f}%")

    v22 = run_bt_v22_exact(
        all_data, universe, "2020-01-01", "2024-12-31",
        btc_w=0.35, ach_w=0.35, usdt_w=0.30,
        ach_bear_immediate=True,  # v2.2 = bear退避ON
    )
    print(f"\nv2.2 (bear退避ON): ret {v22['total_ret']:+.0f}% / DD {v22['max_dd']:.1f}% / 取引{v22['n_trades']} / bear退避{v22['n_bear_exits']}回")
    for y, v in sorted(v22["yearly"].items()):
        print(f"    {y}: {v:+.1f}%")

    print("\n📊 差分:")
    print(f"  ret: {v22['total_ret']-v21['total_ret']:+.0f}%pt")
    print(f"  DD: {v22['max_dd']-v21['max_dd']:+.1f}pt")
    v21_worst = min(v21["yearly"].values())
    v22_worst = min(v22["yearly"].values())
    print(f"  最悪年: v2.1 {v21_worst:+.1f}% → v2.2 {v22_worst:+.1f}% ({v22_worst-v21_worst:+.1f}pt)")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 角度2: 期間別検証
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("\n" + "=" * 70)
    print("🗓️ 角度2: 期間別検証 (2022年弱気、2020-2022、2023-2024)")
    print("=" * 70)

    periods = [
        ("2020-2022 (コロナ強気 → BTCベア)", "2020-01-01", "2022-12-31"),
        ("2022 単独 (最悪年)", "2022-01-01", "2022-12-31"),
        ("2023-2024 (回復期)", "2023-01-01", "2024-12-31"),
    ]
    period_results = {}
    for label, s, e in periods:
        print(f"\n📍 {label}")
        v21_p = run_bt_v22_exact(all_data, universe, s, e,
                                    btc_w=0.35, ach_w=0.35, usdt_w=0.30,
                                    ach_bear_immediate=False)
        v22_p = run_bt_v22_exact(all_data, universe, s, e,
                                    btc_w=0.35, ach_w=0.35, usdt_w=0.30,
                                    ach_bear_immediate=True)
        print(f"  v2.1: ret {v21_p['total_ret']:+6.1f}% / DD {v21_p['max_dd']:.1f}%")
        print(f"  v2.2: ret {v22_p['total_ret']:+6.1f}% / DD {v22_p['max_dd']:.1f}% / bear退避 {v22_p['n_bear_exits']}回")
        print(f"  効果: ret差 {v22_p['total_ret']-v21_p['total_ret']:+.1f}%pt / DD差 {v22_p['max_dd']-v21_p['max_dd']:+.1f}pt")
        period_results[label] = {"v21": v21_p, "v22": v22_p}

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 角度3: CAGR, 5年予測
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("\n" + "=" * 70)
    print("📈 角度3: CAGR と $10,000 投資時の予測")
    print("=" * 70)

    for label, r in [("v2.1", v21), ("v2.2", v22)]:
        initial = 10000
        final = initial * (1 + r["total_ret"] / 100)
        cagr = ((final / initial) ** (1/5) - 1) * 100
        r["cagr"] = cagr
        r["initial_10k_final"] = final
        worst_y = min(r["yearly"].values())
        worst_y_10k = initial * (1 + worst_y / 100)
        print(f"\n{label}:")
        print(f"  5年総リターン: {r['total_ret']:+.0f}%")
        print(f"  CAGR 年率: {cagr:+.1f}%/年")
        print(f"  $10,000 → 5年後: ${final:,.0f}")
        print(f"  最悪年: {worst_y:+.1f}% (年始$10,000 → 年末 ${worst_y_10k:,.0f})")
        print(f"  最大DD {r['max_dd']:.1f}% (ピークから -{r['max_dd']:.0f}% 一時下落)")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 最終総評
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("\n" + "=" * 70)
    print("🎯 最終検証結果")
    print("=" * 70)
    print(f"v2.2 実装の妥当性: {'✅ 妥当' if v22['total_ret'] > 0 and v22['max_dd'] < v21['max_dd'] else '⚠️ 要確認'}")
    print(f"リターン維持: {'✅ 許容' if v22['total_ret'] >= v21['total_ret'] * 0.6 else '⚠️ リターン過大損失'}")
    print(f"DD改善: {'✅ 改善' if v22['max_dd'] < v21['max_dd'] else '⚠️ 改善せず'}")
    print(f"最悪年改善: {'✅ 改善' if v22_worst > v21_worst else '⚠️ 悪化'}")
    print(f"bear退避発動: {v22['n_bear_exits']}回 ({'✅ 適切' if v22['n_bear_exits'] > 0 else '⚠️ 発動せず'})")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(universe),
        "v21_full_period": v21,
        "v22_full_period": v22,
        "period_results": {k: {"v21": v["v21"], "v22": v["v22"]} for k, v in period_results.items()},
        "verification_passed": bool(v22['total_ret'] > 0 and v22['max_dd'] < v21['max_dd'] and v22_worst > v21_worst),
        "predictions": {
            "v21_10k_5y": v21["initial_10k_final"],
            "v22_10k_5y": v22["initial_10k_final"],
            "v21_cagr": v21["cagr"],
            "v22_cagr": v22["cagr"],
        },
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")


if __name__ == "__main__":
    main()
