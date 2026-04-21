"""
iter53: ADX閾値 × BTC比率 × Corr 0.80 統合検証
==============================================
iter52b で win-win 確定した Corr 0.80 をベースに:

  ADX 閾値: 15 / 20 (現行) / 25
  BTC 比率: 30% / 40% (現行) / 50%

3×3 = 9パターン検証。ベースはすべて T3/LB25/週次/Corr 0.80
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

import pandas as pd
import _iter43_rethink as R43

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
RESULTS_DIR = PROJECT / "results"
CACHE_PATH = RESULTS_DIR / "_cache_alldata.pkl"
OUT_JSON = RESULTS_DIR / "iter53_adx_weight.json"

FEE = 0.0010
SLIP = 0.0005
UNIVERSE_REMOVE = ["MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"]
CORR_THRESHOLD = 0.80
CORR_CANDIDATE_N = 10
CORR_LOOKBACK = 60


def _rebalance_key(date, days):
    doy = date.dayofyear + (date.year - 2020) * 366
    return doy // days


def select_top_corr(all_data, universe, date, top_n, lookback, adx_min):
    scores = []
    for sym in universe:
        if sym not in all_data: continue
        df = all_data[sym]
        if date not in df.index: continue
        past = df.index[(df.index < date) & (df.index >= date - pd.Timedelta(days=lookback))]
        if len(past) < 20: continue
        p_now = df.loc[date, "close"]; p_past = df.loc[past[0], "close"]
        ret = p_now / p_past - 1
        adx = df.loc[date].get("adx", 0)
        if pd.isna(adx) or adx < adx_min: continue
        scores.append((sym, ret))
    scores.sort(key=lambda x: x[1], reverse=True)
    cands = scores[:CORR_CANDIDATE_N]
    if len(cands) <= top_n: return cands[:top_n]

    corr_start = date - pd.Timedelta(days=CORR_LOOKBACK)
    rets_df = pd.DataFrame()
    for sym, _ in cands:
        df = all_data[sym]
        s = df.loc[(df.index > corr_start) & (df.index < date), "close"]
        if len(s) < 10: continue
        rets_df[sym] = s.pct_change().dropna()
    if rets_df.empty: return cands[:top_n]
    corr = rets_df.corr()
    selected = []
    for sym, ret in cands:
        if sym not in corr.columns:
            if len(selected) < top_n: selected.append((sym, ret))
            continue
        ok = True
        for s_sym, _ in selected:
            if s_sym in corr.columns:
                c = corr.loc[sym, s_sym]
                if not pd.isna(c) and abs(c) >= CORR_THRESHOLD:
                    ok = False; break
        if ok:
            selected.append((sym, ret))
            if len(selected) >= top_n: break
    while len(selected) < top_n and len(selected) < len(cands):
        for sym, ret in cands:
            if not any(s == sym for s, _ in selected):
                selected.append((sym, ret)); break
    return selected[:top_n]


def run_bt(all_data, universe, start, end, adx_min, btc_w, ach_w, usdt_w,
           top_n=3, lookback=25, rebalance_days=7, initial=10000.0):
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

        # BTC枠
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

        # ACH rebalance
        cur_key = _rebalance_key(date, rebalance_days)
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
                sel = select_top_corr(all_data, universe, date, top_n, lookback, adx_min)
                if sel:
                    w = 1.0 / len(sel)
                    for sym, _ in sel:
                        df = all_data[sym]
                        p_buy = df.loc[date, "close"] * (1 + SLIP)
                        cost = ach_cash * w
                        if cost > 0:
                            qty = cost / p_buy * (1 - FEE)
                            positions[sym] = qty
                            ach_cash -= cost; n_trades += 1
                last_key = cur_key

        # 評価
        ach_value = ach_cash
        for sym, qty in positions.items():
            df = all_data[sym]
            if date in df.index:
                ach_value += qty * df.loc[date, "close"]
        total = btc_cash + btc_qty * price + ach_value + usdt_cash
        equity_curve.append({"ts": date, "equity": total})

    return R43.summarize(equity_curve, initial, n_trades=n_trades)


def main():
    print("=" * 70)
    print("🔬 iter53 ADX × BTC比率 × Corr0.80 統合検証")
    print("=" * 70)

    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    universe = sorted([s for s in all_data.keys() if s not in UNIVERSE_REMOVE])

    patterns = []
    for adx in [15, 20, 25]:
        for btc in [0.30, 0.40, 0.50]:
            patterns.append({
                "id": f"ADX{adx}_BTC{int(btc*100)}",
                "adx": adx, "btc_w": btc, "ach_w": btc, "usdt_w": 1-2*btc,
            })
    # 実際は btc+ach+usdt=1.0 で USDT が動的に
    for p in patterns:
        p["usdt_w"] = max(0.0, 1.0 - p["btc_w"] - p["ach_w"])

    # v2 ベースライン (ADX20, BTC40%/ACH40%/USDT20%, Corr OFF) も参考に
    results = []
    t_start = time.time()

    # ベースライン (現行 v2 = Corr OFF)
    print("\n[0/10] BASELINE (現行v2, Corr OFF, ADX20, 40/40/20)")
    from _iter49_rigorous import run_h11_pure
    base = run_h11_pure(all_data, universe, "2020-01-01", "2024-12-31",
                         top_n=3, lookback=25, rebalance="weekly")
    base.update({"id": "BASELINE", "label": "現行v2", "adx": 20,
                 "btc_w": 0.4, "ach_w": 0.4, "usdt_w": 0.2})
    results.append(base)
    print(f"   ret {base['total_ret']:+.1f}% / DD {base['max_dd']:.1f}%")

    for i, p in enumerate(patterns, 1):
        print(f"\n[{i}/{len(patterns)}] {p['id']}: ADX≥{p['adx']} / BTC{p['btc_w']:.0%}/ACH{p['ach_w']:.0%}/USDT{p['usdt_w']:.0%} + Corr0.80")
        t0 = time.time()
        r = run_bt(all_data, universe, "2020-01-01", "2024-12-31",
                   adx_min=p["adx"], btc_w=p["btc_w"], ach_w=p["ach_w"], usdt_w=p["usdt_w"])
        elapsed = time.time() - t0
        r.update({**p, "label": p["id"], "elapsed": round(elapsed, 2)})
        results.append(r)
        print(f"   {elapsed:.1f}s | ret {r['total_ret']:+.1f}% / DD {r['max_dd']:.1f}% / 取引{r['n_trades']}")

    b_ret = base["total_ret"]
    b_dd = base["max_dd"]

    print("\n" + "=" * 70)
    print(f"📊 ベースライン v2: ret {b_ret:+.1f}% / DD {b_dd:.1f}%")
    print("=" * 70)

    # ランク
    ranked = sorted(results, key=lambda r: r["total_ret"] / max(r["max_dd"], 1),
                    reverse=True)
    print("\n🏆 リスク調整後リターン ランキング")
    print("-" * 70)
    for i, r in enumerate(ranked, 1):
        d_ret = r["total_ret"] - b_ret
        d_dd = r["max_dd"] - b_dd
        ratio = r["total_ret"] / max(r["max_dd"], 1)
        print(f"  {i:2d}. {r['id']:18s}: ret {r['total_ret']:+7.1f}% ({d_ret:+.0f}) / "
              f"DD {r['max_dd']:5.1f}% ({d_dd:+.1f}) / 比率 {ratio:.2f}")

    best = ranked[0]
    print(f"\n🏅 最優秀: {best['id']}")
    print(f"   ret {best['total_ret']:+.1f}% (v2比 {best['total_ret']-b_ret:+.1f}%)")
    print(f"   DD {best['max_dd']:.1f}% (v2比 {best['max_dd']-b_dd:+.1f}pt)")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(universe),
        "baseline": base,
        "recommended": best,
        "all_results": results,
        "total_elapsed_sec": round(time.time() - t_start, 2),
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")


if __name__ == "__main__":
    main()
