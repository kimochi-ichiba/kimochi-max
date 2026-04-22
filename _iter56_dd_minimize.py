"""
iter56: DD最小化最優先の包括検証
=====================================
iter55 で B40_A15_C80_MW が +10,205% / DD 71% と判明。
リターンを維持しつつ、DD を 60%以下 → 理想は 50%以下 に削減したい。

検証する DD 削減手法:
  A. USDT クッション増強 (20%→30%→40%)
  B. ポートフォリオ全体のトレーリング (-15% / -20% / -25% ピークから)
  C. DD-CB 緩和閾値 (40% / 50% / 60%)
  D. BTC レジーム強化 (EMA200 下 ∩ 下降傾向なら ACH も全現金化)
  E. 複合組合せ

ベース設定: B40_A15_C80_MW (BTC40%/ACH40%/USDT20%, ADX15, Corr0.80, Mom加重)

判定:
  - return ≥ 現行 v2 (+4,575%) の維持
  - DD ≤ 60% に改善
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
OUT_JSON = RESULTS_DIR / "iter56_dd_minimize.json"

FEE = 0.0010
SLIP = 0.0005
UNIVERSE_REMOVE = ["MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"]
CORR_THRESHOLD = 0.80


def _reb_key(date, days):
    doy = date.dayofyear + (date.year - 2020) * 366
    return doy // days


def run_bt_defense(all_data, universe, start, end,
                    btc_w=0.40, ach_w=0.40, usdt_w=0.20,
                    adx_min=15, top_n=3, lookback=25, rebalance_days=7,
                    weight_method="momentum",
                    # 新規防御機能
                    port_trailing_pct=0.0,  # 0 = OFF, 0.20 = ピークから-20%でリスク資産全売却
                    dd_cb_threshold=0.0,    # 0 = OFF, 0.50 = DD 50%で ACH 比率を半減
                    btc_regime_strict=False, # True: EMA200下&5日下降なら ACH も現金化
                    initial=10000.0):
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
    peak_equity = initial
    port_trail_active = False
    dd_cb_active = False

    # 5日BTCトレンド (レジーム判定用)
    btc_5d_trend = pd.Series(dtype=float)
    if btc_regime_strict:
        btc_5d_trend = btc_df["close"].pct_change(5)

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

        # ━ Portfolio Trailing Stop ━
        if port_trailing_pct > 0 and peak_equity > 0:
            # 現在の仮想総資産
            temp_ach = ach_cash + sum(
                pos * all_data[sym].loc[date, "close"]
                for sym, pos in positions.items() if date in all_data[sym].index)
            temp_total = btc_cash + btc_qty * price + temp_ach + usdt_cash
            dd_now = (peak_equity - temp_total) / peak_equity
            if not port_trail_active and dd_now >= port_trailing_pct:
                # 全リスク資産 → USDT
                if btc_qty > 0:
                    sell_p = price * (1 - SLIP)
                    btc_cash += btc_qty * sell_p * (1 - FEE)
                    btc_qty = 0; n_trades += 1
                for sym in list(positions.keys()):
                    df = all_data[sym]
                    if date in df.index:
                        p = df.loc[date, "close"] * (1 - SLIP)
                        ach_cash += positions[sym] * p * (1 - FEE)
                        n_trades += 1; positions.pop(sym)
                # 現金すべてを USDT cushion 扱いに (金利のみ獲得)
                usdt_cash += btc_cash + ach_cash
                btc_cash = 0
                ach_cash = 0
                port_trail_active = True
            elif port_trail_active and dd_now < port_trailing_pct * 0.5:
                # 回復したら解除 (初期配分に戻す)
                total = btc_cash + ach_cash + usdt_cash
                btc_cash = total * btc_w
                ach_cash = total * ach_w
                usdt_cash = total * usdt_w
                port_trail_active = False

        # ━ DD サーキットブレーカー ━
        if dd_cb_threshold > 0 and peak_equity > 0:
            temp_ach = ach_cash + sum(
                pos * all_data[sym].loc[date, "close"]
                for sym, pos in positions.items() if date in all_data[sym].index)
            temp_total = btc_cash + btc_qty * price + temp_ach + usdt_cash
            dd_now = (peak_equity - temp_total) / peak_equity
            if not dd_cb_active and dd_now >= dd_cb_threshold:
                # ACH 比率を半減 → USDT に移動
                half_ach = ach_cash * 0.5
                ach_cash -= half_ach
                usdt_cash += half_ach
                dd_cb_active = True
            elif dd_cb_active and dd_now < dd_cb_threshold * 0.5:
                dd_cb_active = False  # 単純解除 (配分は自然に戻らないが許容)

        # ━ BTC厳格レジーム ━
        btc_bear = False
        if not pd.isna(ema200) and price < ema200:
            if btc_regime_strict:
                try:
                    trend_5d = btc_5d_trend.loc[date]
                    if not pd.isna(trend_5d) and trend_5d < 0:
                        btc_bear = True
                except KeyError:
                    pass

        # ━ ACH リバランス ━
        cur_key = _reb_key(date, rebalance_days)
        if cur_key != last_key and not port_trail_active:
            for sym in list(positions.keys()):
                df = all_data[sym]
                if date in df.index:
                    p = df.loc[date, "close"] * (1 - SLIP)
                    ach_cash += positions[sym] * p * (1 - FEE)
                    n_trades += 1; positions.pop(sym)

            is_bear = (not pd.isna(ema200) and price < ema200) or btc_bear
            if is_bear:
                last_key = cur_key
            else:
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

        # 評価
        ach_value = ach_cash
        for sym, qty in positions.items():
            df = all_data[sym]
            if date in df.index:
                ach_value += qty * df.loc[date, "close"]
        total = btc_cash + btc_qty * price + ach_value + usdt_cash
        equity_curve.append({"ts": date, "equity": total})
        if total > peak_equity:
            peak_equity = total

    r = R43.summarize(equity_curve, initial, n_trades=n_trades)
    return r


def main():
    print("=" * 70)
    print("🛡️ iter56 DD最小化最優先 包括検証")
    print("=" * 70)

    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    universe = sorted([s for s in all_data.keys() if s not in UNIVERSE_REMOVE])

    # ベース設定 (iter55 winner)
    base_params = dict(
        btc_w=0.40, ach_w=0.40, usdt_w=0.20,
        adx_min=15, top_n=3, lookback=25, rebalance_days=7,
        weight_method="momentum",
    )

    tests = [
        # 現行 v2 (比較用)
        {"id": "V2_CURRENT", "label": "現行v2 (ADX20,CorrOFF)",
         "override": dict(adx_min=20, weight_method="equal")},
        # iter55 winner
        {"id": "ITER55_BASE", "label": "iter55勝者 (防御なし)",
         "override": {}},
        # A. USDT 増強
        {"id": "USDT30", "label": "USDT30% (BTC35/ACH35)",
         "override": dict(btc_w=0.35, ach_w=0.35, usdt_w=0.30)},
        {"id": "USDT40", "label": "USDT40% (BTC30/ACH30)",
         "override": dict(btc_w=0.30, ach_w=0.30, usdt_w=0.40)},
        {"id": "USDT50", "label": "USDT50% (BTC25/ACH25)",
         "override": dict(btc_w=0.25, ach_w=0.25, usdt_w=0.50)},
        # B. Portfolio trailing
        {"id": "PT20", "label": "ポートトレーリング-20%",
         "override": {}, "port_trailing": 0.20},
        {"id": "PT25", "label": "ポートトレーリング-25%",
         "override": {}, "port_trailing": 0.25},
        {"id": "PT30", "label": "ポートトレーリング-30%",
         "override": {}, "port_trailing": 0.30},
        # C. DD-CB 緩和
        {"id": "DDCB40", "label": "DD-CB 40%閾値",
         "override": {}, "dd_cb": 0.40},
        {"id": "DDCB50", "label": "DD-CB 50%閾値",
         "override": {}, "dd_cb": 0.50},
        {"id": "DDCB60", "label": "DD-CB 60%閾値",
         "override": {}, "dd_cb": 0.60},
        # D. BTC 厳格
        {"id": "BTC_STRICT", "label": "BTC厳格レジーム",
         "override": {}, "btc_strict": True},
        # E. 複合
        {"id": "USDT30_PT25", "label": "USDT30% + ポートトレ-25%",
         "override": dict(btc_w=0.35, ach_w=0.35, usdt_w=0.30),
         "port_trailing": 0.25},
        {"id": "USDT30_DDCB50", "label": "USDT30% + DD-CB 50%",
         "override": dict(btc_w=0.35, ach_w=0.35, usdt_w=0.30),
         "dd_cb": 0.50},
        {"id": "TRIPLE_DEF", "label": "三重防御(USDT30/PT25/DDCB50)",
         "override": dict(btc_w=0.35, ach_w=0.35, usdt_w=0.30),
         "port_trailing": 0.25, "dd_cb": 0.50},
        {"id": "PT25_STRICT", "label": "ポートトレ-25%+BTC厳格",
         "override": {}, "port_trailing": 0.25, "btc_strict": True},
    ]

    import _iter54_comprehensive as M
    M.CORR_THRESHOLD = CORR_THRESHOLD

    results = []
    t_start = time.time()
    for i, t in enumerate(tests, 1):
        # V2_CURRENT の場合は Corr OFF
        if t["id"] == "V2_CURRENT":
            M.CORR_THRESHOLD = 1.1
        else:
            M.CORR_THRESHOLD = CORR_THRESHOLD

        params = dict(base_params)
        params.update(t.get("override", {}))

        print(f"[{i}/{len(tests)}] {t['id']}: {t['label']}")
        t0 = time.time()
        r = run_bt_defense(
            all_data, universe, "2020-01-01", "2024-12-31",
            **params,
            port_trailing_pct=t.get("port_trailing", 0.0),
            dd_cb_threshold=t.get("dd_cb", 0.0),
            btc_regime_strict=t.get("btc_strict", False),
        )
        elapsed = time.time() - t0
        r.update({**t, "elapsed": round(elapsed, 2)})
        results.append(r)
        print(f"   {elapsed:.1f}s | ret {r['total_ret']:+.0f}% / DD {r['max_dd']:.1f}% / 取引{r['n_trades']}")

    # ベースライン (現行 v2)
    v2 = next(r for r in results if r["id"] == "V2_CURRENT")
    v2_ret = v2["total_ret"]
    v2_dd = v2["max_dd"]

    print("\n" + "=" * 70)
    print(f"📊 現行 v2: ret {v2_ret:+.0f}% / DD {v2_dd:.1f}%")
    print("=" * 70)

    # DD優先ランキング
    print("\n🛡️ DD低い順ランキング")
    print("-" * 70)
    ranked_dd = sorted(results, key=lambda r: r["max_dd"])
    for i, r in enumerate(ranked_dd, 1):
        d_ret = r["total_ret"] - v2_ret
        d_dd = r["max_dd"] - v2_dd
        ratio = r["total_ret"] / max(r["max_dd"], 1)
        icon = "✅" if r["total_ret"] >= v2_ret and r["max_dd"] < v2_dd else "  "
        print(f"  {i:2d}. {icon}{r['id']:15s} ({r['label']:25s}): "
              f"ret {r['total_ret']:+7.0f}% ({d_ret:+6.0f}) / DD {r['max_dd']:5.1f}% ({d_dd:+5.1f})")

    # 採用条件: ret ≥ v2 の ret AND DD < v2 の DD (両方改善)
    both_better = [r for r in results
                   if r["id"] != "V2_CURRENT"
                   and r["total_ret"] >= v2_ret
                   and r["max_dd"] < v2_dd]

    print("\n" + "=" * 70)
    print(f"✅ 両方改善 (return≥v2 AND DD<v2): {len(both_better)}件")
    if both_better:
        # 最優秀 = DD最低のもの
        best_dd = min(both_better, key=lambda r: r["max_dd"])
        best_ret = max(both_better, key=lambda r: r["total_ret"])
        print(f"  DD最小: {best_dd['id']} ({best_dd['label']}): "
              f"ret {best_dd['total_ret']:+.0f}% / DD {best_dd['max_dd']:.1f}%")
        print(f"  Ret最大: {best_ret['id']} ({best_ret['label']}): "
              f"ret {best_ret['total_ret']:+.0f}% / DD {best_ret['max_dd']:.1f}%")

        # リスク調整最優秀
        best_ratio = max(both_better, key=lambda r: r["total_ret"] / max(r["max_dd"], 1))
        print(f"  リスク調整: {best_ratio['id']} ({best_ratio['label']}): "
              f"ret {best_ratio['total_ret']:+.0f}% / DD {best_ratio['max_dd']:.1f}% / "
              f"ratio {best_ratio['total_ret']/max(best_ratio['max_dd'],1):.1f}")

        recommended = best_ratio
    else:
        recommended = v2

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(universe),
        "v2_baseline": v2,
        "recommended": recommended,
        "both_better_candidates": both_better,
        "all_results": results,
        "total_elapsed_sec": round(time.time() - t_start, 2),
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")
    print(f"⏱️ 合計 {out['total_elapsed_sec']}秒")


if __name__ == "__main__":
    main()
