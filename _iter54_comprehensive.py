"""
iter54: 残り改善アイデア 包括検証
=================================
iter53 で ADX15+BTC50%+Corr0.80 が最優秀と判明。
これをベースにして以下を全て検証:

  A. Volume フィルター: 直近Nday比 1.0x / 1.2x / 1.5x / 2.0x
  B. Sharpe スコア: リターン÷ボラティリティ でランキング
  C. モメンタム加重: 均等配分 vs スコア比例
  D. マルチLookback: 25日 / 25+45 / 25+45+90 合成スコア
  E. レジーム別: Bull時Top5/LB45, Bear時現金
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np
import _iter43_rethink as R43

PROJECT = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT / "results"
CACHE_PATH = RESULTS_DIR / "_cache_alldata.pkl"
OUT_JSON = RESULTS_DIR / "iter54_comprehensive.json"

FEE = 0.0010
SLIP = 0.0005
UNIVERSE_REMOVE = ["MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"]

# iter53 winner をベース
BASE_ADX = 15
BASE_BTC_W = 0.50
BASE_ACH_W = 0.50
BASE_USDT_W = 0.0
CORR_THRESHOLD = 0.80
CORR_CANDIDATE_N = 10
CORR_LOOKBACK = 60


def _reb_key(date, days):
    doy = date.dayofyear + (date.year - 2020) * 366
    return doy // days


def select_top(all_data, universe, date, top_n, lookback, adx_min,
               volume_ratio=0.0, score_method="momentum",
               multi_lookback=False):
    """Top N 選定（複数モード対応）"""
    scores = []
    for sym in universe:
        if sym not in all_data: continue
        df = all_data[sym]
        if date not in df.index: continue
        past = df.index[(df.index < date) & (df.index >= date - pd.Timedelta(days=lookback))]
        if len(past) < 20: continue

        p_now = df.loc[date, "close"]
        p_past = df.loc[past[0], "close"]
        ret = p_now / p_past - 1
        adx = df.loc[date].get("adx", 0)
        if pd.isna(adx) or adx < adx_min: continue

        # Volume filter
        if volume_ratio > 0:
            vol_now = df.loc[date, "volume"]
            vol_avg = df.loc[past, "volume"].mean() if len(past) > 0 else 0
            if vol_avg > 0 and vol_now < vol_avg * volume_ratio:
                continue

        # スコア計算
        if score_method == "sharpe":
            slice_df = df.loc[past, "close"].pct_change().dropna()
            if len(slice_df) < 5:
                continue
            vol = slice_df.std() * (252 ** 0.5)  # 年率ボラ
            if vol == 0: continue
            score = ret / vol
        elif multi_lookback:
            # 25+45+90 の平均リターン
            total = 0; count = 0
            for lb in [25, 45, 90]:
                past_lb = df.index[(df.index < date) & (df.index >= date - pd.Timedelta(days=lb))]
                if len(past_lb) >= 20:
                    r = df.loc[date, "close"] / df.loc[past_lb[0], "close"] - 1
                    total += r; count += 1
            if count == 0: continue
            score = total / count
        else:
            score = ret

        scores.append((sym, score, ret))

    scores.sort(key=lambda x: x[1], reverse=True)
    cands = scores[:CORR_CANDIDATE_N]
    if len(cands) <= top_n:
        return [(s, r) for s, _, r in cands[:top_n]]

    # 相関考慮選定
    corr_start = date - pd.Timedelta(days=CORR_LOOKBACK)
    rets_df = pd.DataFrame()
    for sym, _, _ in cands:
        df = all_data[sym]
        s = df.loc[(df.index > corr_start) & (df.index < date), "close"]
        if len(s) < 10: continue
        rets_df[sym] = s.pct_change().dropna()
    if rets_df.empty:
        return [(s, r) for s, _, r in cands[:top_n]]
    corr = rets_df.corr()
    selected = []
    for sym, _, ret in cands:
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
        for sym, _, ret in cands:
            if not any(s == sym for s, _ in selected):
                selected.append((sym, ret)); break
    return selected[:top_n]


def run_bt(all_data, universe, start, end, top_n=3, lookback=25,
           rebalance_days=7, adx_min=15, btc_w=0.50, ach_w=0.50, usdt_w=0.0,
           volume_ratio=0.0, score_method="momentum", multi_lookback=False,
           weight_method="equal", initial=10000.0):
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    if not dates: return {}

    btc_cash = initial * btc_w
    btc_qty = 0.0
    ach_cash = initial * ach_w
    positions = {}  # sym -> qty
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
                                  adx_min, volume_ratio, score_method, multi_lookback)
                if sel:
                    # 重み付け
                    if weight_method == "momentum":
                        # リターンの絶対値で加重 (正の場合のみ)
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
                    # 配分後の残キャッシュは調整 (重み合計が1.0になるので0近辺)
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

    return R43.summarize(equity_curve, initial, n_trades=n_trades)


def main():
    print("=" * 70)
    print("🔬 iter54 残り改善アイデア 包括検証")
    print("=" * 70)

    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    universe = sorted([s for s in all_data.keys() if s not in UNIVERSE_REMOVE])

    # iter53 winner = BASE
    base_params = dict(top_n=3, lookback=25, rebalance_days=7,
                         adx_min=BASE_ADX, btc_w=BASE_BTC_W, ach_w=BASE_ACH_W, usdt_w=BASE_USDT_W)

    tests = []
    # iter53 winner (baseline for this iter)
    tests.append({"id": "ITER53_WINNER", "label": "iter53優勝(ADX15/BTC50/Corr80)",
                  "volume_ratio": 0, "score_method": "momentum",
                  "multi_lookback": False, "weight_method": "equal"})
    # A. Volume ratio
    for v in [1.2, 1.5, 2.0]:
        tests.append({"id": f"VOL{v}", "label": f"Volume×{v}",
                      "volume_ratio": v, "score_method": "momentum",
                      "multi_lookback": False, "weight_method": "equal"})
    # B. Sharpe score
    tests.append({"id": "SHARPE", "label": "Sharpeスコア選定",
                  "volume_ratio": 0, "score_method": "sharpe",
                  "multi_lookback": False, "weight_method": "equal"})
    # C. Momentum weighted
    tests.append({"id": "MOM_WEIGHT", "label": "モメンタム加重",
                  "volume_ratio": 0, "score_method": "momentum",
                  "multi_lookback": False, "weight_method": "momentum"})
    # D. Multi-lookback
    tests.append({"id": "MULTI_LB", "label": "マルチLookback(25+45+90)",
                  "volume_ratio": 0, "score_method": "momentum",
                  "multi_lookback": True, "weight_method": "equal"})
    # E. Combined: Volume1.2 + Sharpe + Multi-LB
    tests.append({"id": "COMBO_A", "label": "Vol1.2+Sharpe",
                  "volume_ratio": 1.2, "score_method": "sharpe",
                  "multi_lookback": False, "weight_method": "equal"})
    tests.append({"id": "COMBO_B", "label": "Multi-LB+Mom加重",
                  "volume_ratio": 0, "score_method": "momentum",
                  "multi_lookback": True, "weight_method": "momentum"})

    results = []
    t_start = time.time()
    for i, t in enumerate(tests, 1):
        print(f"[{i}/{len(tests)}] {t['id']}: {t['label']}")
        t0 = time.time()
        r = run_bt(all_data, universe, "2020-01-01", "2024-12-31",
                   **base_params,
                   volume_ratio=t["volume_ratio"],
                   score_method=t["score_method"],
                   multi_lookback=t["multi_lookback"],
                   weight_method=t["weight_method"])
        elapsed = time.time() - t0
        r.update({**t, "elapsed": round(elapsed, 2)})
        results.append(r)
        print(f"   {elapsed:.1f}s | ret {r['total_ret']:+.1f}% / DD {r['max_dd']:.1f}% / 取引{r['n_trades']}")

    baseline = results[0]
    b_ret = baseline["total_ret"]
    b_dd = baseline["max_dd"]

    print("\n" + "=" * 70)
    print(f"📊 基準 (iter53勝者): ret {b_ret:+.1f}% / DD {b_dd:.1f}%")
    print("=" * 70)

    ranked = sorted(results, key=lambda r: r["total_ret"] / max(r["max_dd"], 1),
                    reverse=True)
    print("\n🏆 ランキング (ret/DD 比率)")
    print("-" * 70)
    for i, r in enumerate(ranked, 1):
        d_ret = r["total_ret"] - b_ret
        d_dd = r["max_dd"] - b_dd
        ratio = r["total_ret"] / max(r["max_dd"], 1)
        icon = "🏆" if r["total_ret"] > b_ret and r["max_dd"] <= b_dd + 1.0 else "  "
        print(f" {i:2d}. {icon}{r['id']:15s} ({r['label']:30s}): "
              f"ret {r['total_ret']:+7.1f}% ({d_ret:+7.0f}) / DD {r['max_dd']:5.1f}% ({d_dd:+.1f}) / ratio {ratio:.1f}")

    best = ranked[0]
    print(f"\n🏅 最優秀: {best['id']} ({best['label']})")
    print(f"   ret {best['total_ret']:+.1f}% / DD {best['max_dd']:.1f}% / ratio {best['total_ret']/max(best['max_dd'],1):.2f}")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(universe),
        "base_config": base_params,
        "baseline": baseline,
        "recommended": best,
        "all_results": results,
        "total_elapsed_sec": round(time.time() - t_start, 2),
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")


if __name__ == "__main__":
    main()
