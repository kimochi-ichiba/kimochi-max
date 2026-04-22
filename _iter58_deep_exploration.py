"""
iter58: 深掘り多角検証（未試行アイデア総動員）
=================================================
ユーザー要求: 「低リスク+今のリターン維持」のスイートスポット探索

検証する新アイデア:
  A. ACH即時ベア退避: BTC<EMA200 なら ACH も即売却（現状はrebalance待ち）
  B. EMA50 高速退避: BTC exit を EMA200 → EMA50 に短縮
  C. 小額ACH: 5%/10%/15% で BTC 主導を維持
  D. ポートフォリオ DD 検知: 40%/50% で緊急撤退 + EMA回復で再エントリー
  E. 組合せ: 上記を順列で総当たり

判定:
  - 年率 ≥ 35% (年々着実成長)
  - DD < 45% ($100 → $55以上維持)
  - 最悪年 ≥ -10% (毎年ほぼプラス)
  - スイートスポット= 3条件すべて満たすもの
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

import pandas as pd
import numpy as np
import _iter43_rethink as R43

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
RESULTS_DIR = PROJECT / "results"
CACHE_PATH = RESULTS_DIR / "_cache_alldata.pkl"
OUT_JSON = RESULTS_DIR / "iter58_deep_exploration.json"

FEE = 0.0010
SLIP = 0.0005
UNIVERSE_REMOVE = ["MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"]


def _reb_key(date, days):
    doy = date.dayofyear + (date.year - 2020) * 366
    return doy // days


def select_top_simple(all_data, universe, date, top_n, lookback, adx_min=15):
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
    return scores[:top_n]


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


def run_bt_v58(all_data, universe, start, end,
                btc_w=0.40, ach_w=0.10, usdt_w=0.50,
                top_n=3, lookback=25, rebalance_days=7,
                btc_exit_ema="ema200",  # "ema200" or "ema50"
                ach_bear_immediate_sell=True,  # A: 即時ベア退避
                port_dd_trigger=0.0,    # D: ポートフォリオ DD検知 (0=OFF, 0.40 = 40%)
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
    n_bear_exits = 0  # ACH bear 即時退避回数
    n_port_stops = 0  # ポートフォリオ DD 停止回数
    last_key = None
    peak_equity = initial
    port_stop_active = False

    for date in dates:
        btc_r = btc_df.loc[date]
        price = btc_r["close"]
        ema_target = btc_r.get(btc_exit_ema)
        if pd.isna(ema_target):
            ema_target = btc_r.get("ema200")  # fallback

        # --- BTC EMA 戦略 ---
        btc_bullish = not pd.isna(ema_target) and price > ema_target

        if btc_qty == 0 and btc_bullish and not port_stop_active:
            buy_p = price * (1 + SLIP)
            btc_qty = btc_cash / buy_p * (1 - FEE)
            btc_cash = 0; n_trades += 1
        elif btc_qty > 0 and not btc_bullish:
            sell_p = price * (1 - SLIP)
            btc_cash += btc_qty * sell_p * (1 - FEE)
            btc_qty = 0; n_trades += 1

        # --- A: ACH 即時ベア退避 ---
        if ach_bear_immediate_sell and not btc_bullish and positions:
            for sym in list(positions.keys()):
                df = all_data[sym]
                if date in df.index:
                    p = df.loc[date, "close"] * (1 - SLIP)
                    ach_cash += positions[sym] * p * (1 - FEE)
                    n_trades += 1; positions.pop(sym)
                    n_bear_exits += 1

        if btc_qty == 0: btc_cash *= (1 + 0.03/365)
        usdt_cash *= (1 + 0.03/365)

        # --- D: ポートフォリオ DD 検知 ---
        if port_dd_trigger > 0:
            temp_ach = ach_cash + sum(pos * all_data[sym].loc[date, "close"]
                                        for sym, pos in positions.items()
                                        if date in all_data[sym].index)
            temp_total = btc_cash + btc_qty * price + temp_ach + usdt_cash
            dd_now = (peak_equity - temp_total) / peak_equity if peak_equity > 0 else 0
            if not port_stop_active and dd_now >= port_dd_trigger:
                # 全リスク資産 → USDT 退避
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
                usdt_cash += btc_cash + ach_cash
                btc_cash = 0; ach_cash = 0
                port_stop_active = True
                n_port_stops += 1
            elif port_stop_active and btc_bullish:
                # BTC が EMA 上に回復 → 再エントリー
                total = usdt_cash
                btc_cash = total * btc_w
                ach_cash = total * ach_w
                usdt_cash = total * usdt_w
                port_stop_active = False

        # --- ACH リバランス ---
        if ach_w > 0 and not port_stop_active:
            cur_key = _reb_key(date, rebalance_days)
            if cur_key != last_key:
                # 既存position を全決済
                for sym in list(positions.keys()):
                    df = all_data[sym]
                    if date in df.index:
                        p = df.loc[date, "close"] * (1 - SLIP)
                        ach_cash += positions[sym] * p * (1 - FEE)
                        n_trades += 1; positions.pop(sym)

                if not btc_bullish:
                    # bear regime: 買わない
                    last_key = cur_key
                else:
                    sel = select_top_simple(all_data, universe, date, top_n, lookback, 15)
                    if sel:
                        w = 1.0 / len(sel)
                        for sym, _ in sel:
                            df = all_data[sym]
                            p_buy = df.loc[date, "close"] * (1 + SLIP)
                            cost = ach_cash * w
                            if cost > 0:
                                qty = cost / p_buy * (1 - FEE)
                                positions[sym] = qty
                                n_trades += 1
                        used = ach_cash * len(sel) * w
                        ach_cash -= used
                    last_key = cur_key

        # --- 時価評価 ---
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
    r["yearly"] = calc_yearly_rets(equity_curve, initial)
    r["n_bear_exits"] = n_bear_exits
    r["n_port_stops"] = n_port_stops
    return r


def main():
    print("=" * 70)
    print("🔬 iter58 深掘り多角検証 (25パターン)")
    print("=" * 70)

    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    universe = sorted([s for s in all_data.keys() if s not in UNIVERSE_REMOVE])

    tests = []

    # 【基準】現行 v2.1 相当
    tests.append({"id": "V21_REF", "label": "v2.1 参考 (BTC35/A35/U30)",
                  "btc_w": 0.35, "ach_w": 0.35, "usdt_w": 0.30,
                  "ach_bear_immediate": False, "btc_exit": "ema200"})

    # 【A系】ACH 即時ベア退避 (現行設定との差分)
    tests.append({"id": "A_V21_BEAR", "label": "v2.1 + ACH即時退避",
                  "btc_w": 0.35, "ach_w": 0.35, "usdt_w": 0.30,
                  "ach_bear_immediate": True, "btc_exit": "ema200"})

    # 【C系】小額ACH (BTC+大USDT, +少ACH)
    for btc, ach, usdt in [(0.40, 0.05, 0.55), (0.50, 0.05, 0.45), (0.30, 0.05, 0.65),
                              (0.40, 0.10, 0.50), (0.50, 0.10, 0.40), (0.30, 0.10, 0.60),
                              (0.40, 0.15, 0.45), (0.35, 0.15, 0.50)]:
        label = f"BTC{int(btc*100)}/A{int(ach*100)}/U{int(usdt*100)} +bear退避"
        tests.append({"id": f"S_B{int(btc*100)}A{int(ach*100)}U{int(usdt*100)}",
                       "label": label,
                       "btc_w": btc, "ach_w": ach, "usdt_w": usdt,
                       "ach_bear_immediate": True, "btc_exit": "ema200"})

    # 【B系】EMA50 高速退避
    tests.append({"id": "BTC50U50_EMA50", "label": "BTC50/U50 + EMA50退避",
                  "btc_w": 0.50, "ach_w": 0.0, "usdt_w": 0.50,
                  "ach_bear_immediate": False, "btc_exit": "ema50"})
    tests.append({"id": "BTC40U60_EMA50", "label": "BTC40/U60 + EMA50退避",
                  "btc_w": 0.40, "ach_w": 0.0, "usdt_w": 0.60,
                  "ach_bear_immediate": False, "btc_exit": "ema50"})
    # EMA50 + 小額ACH
    tests.append({"id": "B40A10U50_EMA50", "label": "BTC40/A10/U50 +EMA50+bear退避",
                  "btc_w": 0.40, "ach_w": 0.10, "usdt_w": 0.50,
                  "ach_bear_immediate": True, "btc_exit": "ema50"})
    tests.append({"id": "B50A10U40_EMA50", "label": "BTC50/A10/U40 +EMA50+bear退避",
                  "btc_w": 0.50, "ach_w": 0.10, "usdt_w": 0.40,
                  "ach_bear_immediate": True, "btc_exit": "ema50"})

    # 【D系】ポートフォリオ DD トリガー
    tests.append({"id": "PORT_DD40", "label": "B50/A10/U40 +PortDD 40%",
                  "btc_w": 0.50, "ach_w": 0.10, "usdt_w": 0.40,
                  "ach_bear_immediate": True, "btc_exit": "ema200",
                  "port_dd": 0.40})
    tests.append({"id": "PORT_DD50", "label": "B50/A10/U40 +PortDD 50%",
                  "btc_w": 0.50, "ach_w": 0.10, "usdt_w": 0.40,
                  "ach_bear_immediate": True, "btc_exit": "ema200",
                  "port_dd": 0.50})

    # 【E系】三重防御
    tests.append({"id": "TRIPLE", "label": "EMA50+bear退避+PortDD 40%",
                  "btc_w": 0.40, "ach_w": 0.10, "usdt_w": 0.50,
                  "ach_bear_immediate": True, "btc_exit": "ema50",
                  "port_dd": 0.40})
    tests.append({"id": "TRIPLE_BIG", "label": "B50+EMA50+bear退避+PortDD 40%",
                  "btc_w": 0.50, "ach_w": 0.10, "usdt_w": 0.40,
                  "ach_bear_immediate": True, "btc_exit": "ema50",
                  "port_dd": 0.40})

    # 【F系】BTC100% EMA50 (参考)
    tests.append({"id": "BTC100_EMA200", "label": "BTC100% (EMA200) 参考",
                  "btc_w": 1.00, "ach_w": 0.0, "usdt_w": 0.0,
                  "ach_bear_immediate": False, "btc_exit": "ema200"})
    tests.append({"id": "BTC100_EMA50", "label": "BTC100% (EMA50) 参考",
                  "btc_w": 1.00, "ach_w": 0.0, "usdt_w": 0.0,
                  "ach_bear_immediate": False, "btc_exit": "ema50"})

    # 【G系】ACH 0% (BTC only) 各比率
    for btc in [0.70, 0.80, 0.90]:
        tests.append({"id": f"BTC{int(btc*100)}_U{int((1-btc)*100)}",
                       "label": f"BTC{int(btc*100)}/U{int((1-btc)*100)} (EMA200)",
                       "btc_w": btc, "ach_w": 0.0, "usdt_w": 1-btc,
                       "ach_bear_immediate": False, "btc_exit": "ema200"})

    # 実行
    results = []
    t_start = time.time()
    for i, t in enumerate(tests, 1):
        print(f"[{i:2d}/{len(tests)}] {t['id']}: {t['label']}")
        t0 = time.time()
        r = run_bt_v58(
            all_data, universe, "2020-01-01", "2024-12-31",
            btc_w=t["btc_w"], ach_w=t["ach_w"], usdt_w=t["usdt_w"],
            ach_bear_immediate_sell=t.get("ach_bear_immediate", False),
            btc_exit_ema=t.get("btc_exit", "ema200"),
            port_dd_trigger=t.get("port_dd", 0.0),
        )
        elapsed = time.time() - t0
        r.update({**t, "elapsed": round(elapsed, 2)})
        results.append(r)
        yearly_min = min(r["yearly"].values()) if r.get("yearly") else 0
        cagr = ((r["final"] / 10000) ** (1/5) - 1) * 100
        print(f"   {elapsed:.1f}s | ret {r['total_ret']:+7.0f}% (CAGR {cagr:+5.1f}%) / "
              f"DD {r['max_dd']:5.1f}% / 最悪年 {yearly_min:+5.1f}%")

    # 評価: 3条件 (CAGR≥35%, DD<45%, 最悪年≥-10%)
    candidates = []
    for r in results:
        cagr = ((r["final"] / 10000) ** (1/5) - 1) * 100
        r["cagr"] = round(cagr, 2)
        yearly_min = min(r["yearly"].values()) if r.get("yearly") else 0
        r["yearly_min"] = yearly_min
        if r["id"] == "V21_REF": continue
        if cagr >= 35 and r["max_dd"] < 45 and yearly_min > -10:
            candidates.append(r)

    # 分野別ソート
    print("\n" + "=" * 70)
    print("🏆 CAGR年率ランキング")
    print("-" * 70)
    ranked_cagr = sorted(results, key=lambda r: r["cagr"], reverse=True)
    for i, r in enumerate(ranked_cagr[:15], 1):
        ok3 = "✅" if (r["cagr"] >= 35 and r["max_dd"] < 45 and r["yearly_min"] > -10) else "  "
        print(f"  {i:2d}. {ok3}{r['id']:20s}: CAGR {r['cagr']:+6.1f}% / "
              f"ret {r['total_ret']:+7.0f}% / DD {r['max_dd']:5.1f}% / 最悪年 {r['yearly_min']:+.1f}%")

    print("\n🛡️ DD低い順ランキング (全体)")
    print("-" * 70)
    ranked_dd = sorted(results, key=lambda r: r["max_dd"])
    for i, r in enumerate(ranked_dd[:15], 1):
        ok3 = "✅" if (r["cagr"] >= 35 and r["max_dd"] < 45 and r["yearly_min"] > -10) else "  "
        print(f"  {i:2d}. {ok3}{r['id']:20s}: DD {r['max_dd']:5.1f}% / "
              f"CAGR {r['cagr']:+6.1f}% / 最悪年 {r['yearly_min']:+.1f}%")

    print("\n" + "=" * 70)
    print(f"✨ 3条件満たすスイートスポット (CAGR≥35%, DD<45%, 最悪年≥-10%): {len(candidates)}件")
    print("-" * 70)
    for r in sorted(candidates, key=lambda x: x["cagr"], reverse=True):
        yearly_str = " / ".join([f"{y}:{v:+.0f}%" for y, v in sorted(r["yearly"].items())[1:]])
        print(f"  {r['id']:20s}: CAGR {r['cagr']:+5.1f}% / DD {r['max_dd']:4.1f}% / 最悪年 {r['yearly_min']:+.1f}%")
        print(f"     年別: {yearly_str}")

    if candidates:
        best = max(candidates, key=lambda r: r["cagr"])
        print(f"\n🏅 推奨: {best['id']} ({best['label']})")
    else:
        print("\n⚠️ 3条件満たすなし。2条件 (CAGR≥25%, DD<50%) で再検索")
        candidates2 = [r for r in results if r["id"] != "V21_REF"
                        and r["cagr"] >= 25 and r["max_dd"] < 50 and r["yearly_min"] > -10]
        for r in sorted(candidates2, key=lambda x: x["cagr"], reverse=True)[:5]:
            print(f"  {r['id']:20s}: CAGR {r['cagr']:+5.1f}% / DD {r['max_dd']:4.1f}% / 最悪年 {r['yearly_min']:+.1f}%")
        best = max(candidates2, key=lambda r: r["cagr"]) if candidates2 else results[0]

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(universe),
        "criteria": {"cagr_min": 35, "dd_max": 45, "worst_year_min": -10},
        "total_patterns": len(tests),
        "sweet_spot_candidates": candidates,
        "recommended": best,
        "all_results": results,
        "total_elapsed_sec": round(time.time() - t_start, 2),
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")
    print(f"⏱️ 合計 {out['total_elapsed_sec']}秒")


if __name__ == "__main__":
    main()
