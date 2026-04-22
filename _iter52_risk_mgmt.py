"""
iter52: リスク管理 4機能 × 16パターン バックテスト
==================================================
ベースライン (現行 v2 = T3/LB25/週次/62銘柄/Taker) に以下を段階的に追加:

  F1. ATR損切り (entry - 2×ATR で即売却)
  F2. DDサーキットブレーカー (DD≥30% で ACH 40%→20%)
  F3. 相関考慮Top選定 (Top10から低相関Top3、閾値0.7)
  F4. 時間ベース退場 (保有30日経過で含み損なら売却)

2^4 = 16パターンで、どの組合せが DD 改善 vs リターン維持の最適解かを決定。

判定基準:
  - リターン > ベースラインの80% 維持
  - かつ 最大DD < 45%
  - 両方満たす設定を採用候補とする

データ: Binance 日足実データ (2020-01-01 〜 2024-12-31), 62銘柄
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from datetime import datetime, timezone
from itertools import product
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np

import _iter43_rethink as R43

PROJECT = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT / "results"
CACHE_PATH = RESULTS_DIR / "_cache_alldata.pkl"
OUT_JSON = RESULTS_DIR / "iter52_risk_mgmt.json"

FEE = 0.0010   # Taker fee (iter51 Taker と同じ)
SLIP = 0.0005

UNIVERSE_REMOVE = ["MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"]

# 4機能のデフォルトパラメータ
ATR_STOP_MULT = 2.0          # F1: ATR × 2 を損切り距離
DD_CIRCUIT_THRESHOLD = 0.30  # F2: 総資産 DD 30% で発動
DD_CIRCUIT_RELEASE = 0.15    # F2: DD 15% 未満で解除
DD_ACH_REDUCED = 0.20        # F2: 発動時の ACH 割合 (通常 0.40)
CORR_CANDIDATE_N = 10        # F3: Top10 候補から選定
CORR_THRESHOLD = 0.70        # F3: 相関 0.7 以上は除外
CORR_LOOKBACK = 60           # F3: 相関計算の過去日数
TIME_EXIT_DAYS = 30          # F4: 30日経過で検査


def _rebalance_key(date, days: int):
    doy = date.dayofyear + (date.year - 2020) * 366
    return doy // days


def select_top_n_corr_aware(all_data, universe, date, top_n, lookback_days,
                              candidate_n, corr_threshold, corr_lookback):
    """相関考慮 Top N 選定"""
    # 候補 Top N (モメンタム順)
    scores = []
    for sym in universe:
        if sym not in all_data: continue
        df = all_data[sym]
        if date not in df.index: continue
        past = df.index[(df.index < date) & (df.index >= date - pd.Timedelta(days=lookback_days))]
        if len(past) < 20: continue
        price_now = df.loc[date, "close"]
        price_past = df.loc[past[0], "close"]
        ret = price_now / price_past - 1
        adx = df.loc[date].get("adx", 0)
        if pd.isna(adx) or adx < 20: continue
        scores.append((sym, ret))
    scores.sort(key=lambda x: x[1], reverse=True)
    candidates = scores[:candidate_n]
    if len(candidates) <= top_n:
        return candidates[:top_n]

    # 相関行列を作成
    corr_start = date - pd.Timedelta(days=corr_lookback)
    returns_df = pd.DataFrame()
    for sym, _ in candidates:
        df = all_data[sym]
        slice_df = df.loc[(df.index > corr_start) & (df.index < date), "close"]
        if len(slice_df) < 10:
            continue
        returns_df[sym] = slice_df.pct_change().dropna()

    if returns_df.empty:
        return candidates[:top_n]

    corr = returns_df.corr()
    # Greedy: 最もモメンタム高いものから、相関 < 閾値 なら追加
    selected = []
    for sym, ret in candidates:
        if sym not in corr.columns:
            if len(selected) < top_n:
                selected.append((sym, ret))
            continue
        ok = True
        for sel_sym, _ in selected:
            if sel_sym in corr.columns:
                c = corr.loc[sym, sel_sym]
                if not pd.isna(c) and abs(c) >= corr_threshold:
                    ok = False
                    break
        if ok:
            selected.append((sym, ret))
            if len(selected) >= top_n:
                break

    # 足りなければモメンタム順で補完
    while len(selected) < top_n and len(selected) < len(candidates):
        for sym, ret in candidates:
            if not any(s == sym for s, _ in selected):
                selected.append((sym, ret))
                break
    return selected[:top_n]


def run_v21_backtest(all_data, universe, start, end, top_n=3, lookback=25,
                      rebalance_days=7, initial=10_000.0,
                      f1_atr_stop=False, f2_dd_circuit=False,
                      f3_corr_aware=False, f4_time_exit=False):
    """v2.1 バックテスト (4機能 on/off 可)"""
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    if not dates:
        return {"final": initial, "total_ret": 0, "max_dd": 0, "sharpe": 0,
                "n_trades": 0, "yearly": {}}

    btc_weight = 0.40
    ach_weight = 0.40
    usdt_weight = 0.20

    # BTC枠 (既存 EMA200 ロジック)
    btc_cash = initial * btc_weight
    btc_qty = 0.0
    # ACH枠
    ach_cash = initial * ach_weight
    ach_positions = {}  # sym -> {"qty", "entry_price", "entry_date", "atr_stop_price"}
    # USDT枠 (年率3% 金利)
    usdt_cash = initial * usdt_weight

    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0
    n_atr_stops = 0
    n_time_exits = 0
    n_circuit_activations = 0
    circuit_active = False
    peak_equity = initial
    last_reb_key = None

    for date in dates:
        btc_r = btc_df.loc[date]
        btc_price = btc_r["close"]
        btc_ema200 = btc_r.get("ema200")

        # ━ BTC枠 (EMA200 戦略) ━
        if btc_qty == 0 and not pd.isna(btc_ema200) and btc_price > btc_ema200:
            buy_p = btc_price * (1 + SLIP)
            btc_qty = btc_cash / buy_p * (1 - FEE)
            btc_cash = 0
            n_trades += 1
        elif btc_qty > 0 and not pd.isna(btc_ema200) and btc_price < btc_ema200:
            sell_p = btc_price * (1 - SLIP)
            btc_cash += btc_qty * sell_p * (1 - FEE)
            btc_qty = 0
            n_trades += 1

        # USDT枠 金利 (BTC保有してない時の現金扱い)
        if btc_qty == 0:
            btc_cash *= (1 + 0.03 / 365)
        ach_cash *= (1 + 0.03 / 365)  # ACHのキャッシュ分
        usdt_cash *= (1 + 0.03 / 365)

        # ━ F1: ATR 損切りチェック (日足 low で近似) ━
        if f1_atr_stop and ach_positions:
            for sym in list(ach_positions.keys()):
                df = all_data[sym]
                if date not in df.index: continue
                day_low = df.loc[date, "low"]
                stop = ach_positions[sym].get("atr_stop_price", 0)
                if stop > 0 and day_low <= stop:
                    # ストップ発動
                    sell_p = stop * (1 - SLIP)  # ストップ価格で約定
                    ach_cash += ach_positions[sym]["qty"] * sell_p * (1 - FEE)
                    ach_positions.pop(sym)
                    n_trades += 1
                    n_atr_stops += 1

        # ━ F4: 時間ベース退場 ━
        if f4_time_exit and ach_positions:
            for sym in list(ach_positions.keys()):
                df = all_data[sym]
                if date not in df.index: continue
                entry_date = ach_positions[sym].get("entry_date")
                if entry_date and (date - entry_date).days >= TIME_EXIT_DAYS:
                    cur_price = df.loc[date, "close"]
                    entry_price = ach_positions[sym].get("entry_price", cur_price)
                    if cur_price < entry_price:
                        sell_p = cur_price * (1 - SLIP)
                        ach_cash += ach_positions[sym]["qty"] * sell_p * (1 - FEE)
                        ach_positions.pop(sym)
                        n_trades += 1
                        n_time_exits += 1

        # ━ 週次リバランス判定 ━
        cur_key = _rebalance_key(date, rebalance_days)
        do_reb = (last_reb_key is None) or (cur_key != last_reb_key)

        if do_reb:
            # 全決済 (リバランスで Top 更新)
            for sym in list(ach_positions.keys()):
                df = all_data[sym]
                if date in df.index:
                    price = df.loc[date, "close"] * (1 - SLIP)
                    ach_cash += ach_positions[sym]["qty"] * price * (1 - FEE)
                    n_trades += 1
                    ach_positions.pop(sym)

            # Top N 選定 (F3: 相関考慮)
            if f3_corr_aware:
                selected = select_top_n_corr_aware(
                    all_data, universe, date, top_n, lookback,
                    CORR_CANDIDATE_N, CORR_THRESHOLD, CORR_LOOKBACK)
            else:
                scores = []
                for sym in universe:
                    if sym not in all_data: continue
                    df = all_data[sym]
                    if date not in df.index: continue
                    past = df.index[(df.index < date) & (df.index >= date - pd.Timedelta(days=lookback))]
                    if len(past) < 20: continue
                    price_now = df.loc[date, "close"]
                    price_past = df.loc[past[0], "close"]
                    ret = price_now / price_past - 1
                    adx = df.loc[date].get("adx", 0)
                    if pd.isna(adx) or adx < 20: continue
                    scores.append((sym, ret))
                scores.sort(key=lambda x: x[1], reverse=True)
                selected = scores[:top_n]

            # BTCレジームチェック (Bear は買わない)
            if not pd.isna(btc_ema200) and btc_price < btc_ema200:
                last_reb_key = cur_key
            else:
                # F2: サーキットブレーカー発動中は ACH 割合を削減
                current_ach_weight = DD_ACH_REDUCED if circuit_active else ach_weight
                allocation_total = ach_cash  # 現 cash 全額を分配 (簡略化)
                if circuit_active:
                    # キャッシュの半分だけ運用、残りは USDT に退避
                    alloc = allocation_total * 0.5
                    usdt_cash += allocation_total * 0.5
                    ach_cash = alloc
                if selected:
                    w = 1.0 / len(selected)
                    for sym, _ in selected:
                        df = all_data[sym]
                        price_buy = df.loc[date, "close"] * (1 + SLIP)
                        cost = ach_cash * w
                        if cost > 0:
                            qty = cost / price_buy * (1 - FEE)
                            # F1: ATR ストップ価格計算
                            atr_stop_price = 0
                            if f1_atr_stop:
                                atr = df.loc[date].get("atr", 0)
                                if not pd.isna(atr) and atr > 0:
                                    atr_stop_price = df.loc[date, "close"] - ATR_STOP_MULT * atr
                            ach_positions[sym] = {
                                "qty": qty,
                                "entry_price": df.loc[date, "close"],
                                "entry_date": date,
                                "atr_stop_price": atr_stop_price,
                            }
                            ach_cash -= cost
                            n_trades += 1
                last_reb_key = cur_key

        # ━ 時価評価 ━
        ach_value = ach_cash
        for sym, pos in ach_positions.items():
            df = all_data[sym]
            if date in df.index:
                ach_value += pos["qty"] * df.loc[date, "close"]
        btc_value = btc_cash + btc_qty * btc_price
        total = btc_value + ach_value + usdt_cash
        equity_curve.append({"ts": date, "equity": total})

        # ━ F2: DDサーキットブレーカー判定 ━
        if total > peak_equity:
            peak_equity = total
        dd = (peak_equity - total) / peak_equity if peak_equity > 0 else 0
        if f2_dd_circuit:
            if not circuit_active and dd >= DD_CIRCUIT_THRESHOLD:
                circuit_active = True
                n_circuit_activations += 1
            elif circuit_active and dd < DD_CIRCUIT_RELEASE:
                circuit_active = False

    r = R43.summarize(equity_curve, initial, n_trades=n_trades)
    r["n_atr_stops"] = n_atr_stops
    r["n_time_exits"] = n_time_exits
    r["n_circuit_activations"] = n_circuit_activations
    r["final_circuit_active"] = circuit_active
    return r


def main():
    print("=" * 70)
    print("🛡️ iter52 リスク管理 4機能 × 16パターン検証")
    print("=" * 70)

    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    universe = sorted([s for s in all_data.keys() if s not in UNIVERSE_REMOVE])
    print(f"ユニバース: {len(universe)}銘柄\n")

    # 16パターン (2^4)
    patterns = []
    for f1, f2, f3, f4 in product([False, True], repeat=4):
        flags = "".join([("1" if f1 else "0"), ("1" if f2 else "0"),
                         ("1" if f3 else "0"), ("1" if f4 else "0")])
        names = []
        if f1: names.append("ATR")
        if f2: names.append("DD-CB")
        if f3: names.append("Corr")
        if f4: names.append("Time")
        label = "+".join(names) if names else "ベースライン"
        patterns.append({
            "id": f"P{flags}",
            "label": label,
            "f1_atr_stop": f1, "f2_dd_circuit": f2,
            "f3_corr_aware": f3, "f4_time_exit": f4,
        })

    results = []
    t_start = time.time()
    for i, p in enumerate(patterns, 1):
        t0 = time.time()
        print(f"[{i:2d}/16] {p['id']}: {p['label']}")
        r = run_v21_backtest(
            all_data, universe, "2020-01-01", "2024-12-31",
            top_n=3, lookback=25, rebalance_days=7,
            f1_atr_stop=p["f1_atr_stop"],
            f2_dd_circuit=p["f2_dd_circuit"],
            f3_corr_aware=p["f3_corr_aware"],
            f4_time_exit=p["f4_time_exit"],
        )
        elapsed = time.time() - t0
        r.update({**p, "elapsed": round(elapsed, 2)})
        results.append(r)
        print(f"    {elapsed:.1f}s | 最終 ${r['final']:>10,.0f} | "
              f"リターン {r['total_ret']:+7.1f}% | DD {r['max_dd']:5.1f}% | "
              f"取引 {r['n_trades']:>4d} | ATR停止 {r['n_atr_stops']} | "
              f"CB発動 {r['n_circuit_activations']}")

    # ベースライン
    baseline = next(r for r in results if r["id"] == "P0000")
    baseline_ret = baseline["total_ret"]
    baseline_dd = baseline["max_dd"]

    # 判定
    print("\n" + "=" * 70)
    print(f"📊 ベースライン: リターン +{baseline_ret:.1f}%, DD {baseline_dd:.1f}%")
    print(f"判定基準: リターン ≥ {baseline_ret*0.8:.1f}% AND DD ≤ 45%")
    print("=" * 70)

    # Standard ランキング
    ranked = sorted(results, key=lambda r: (r["total_ret"] / max(r["max_dd"], 1)),
                    reverse=True)
    print("\n🏆 Return/DD 比率ランキング (リスク調整後リターン)")
    print("-" * 70)
    for i, r in enumerate(ranked[:16], 1):
        icon = "✅" if (r["total_ret"] >= baseline_ret * 0.8 and r["max_dd"] <= 45) else "⚠️"
        print(f"  {i:2d}. {icon} {r['id']} ({r['label']:20s}): "
              f"ret {r['total_ret']:+7.1f}% / DD {r['max_dd']:5.1f}% / "
              f"比率 {r['total_ret']/max(r['max_dd'],1):.2f}")

    # 採用候補
    acceptable = [r for r in results
                  if r["total_ret"] >= baseline_ret * 0.8 and r["max_dd"] <= 45]
    print("\n" + "=" * 70)
    print(f"✅ 採用候補 (リターン80%維持 + DD≤45%): {len(acceptable)}件")
    for r in acceptable:
        dd_improve = baseline_dd - r["max_dd"]
        ret_loss = baseline_ret - r["total_ret"]
        print(f"  - {r['id']} ({r['label']}): "
              f"DD {r['max_dd']:.1f}% (-{dd_improve:.1f}%pt), "
              f"リターン {r['total_ret']:+.1f}% ({-ret_loss:+.1f}%pt)")

    if acceptable:
        best = max(acceptable, key=lambda r: r["total_ret"] / max(r["max_dd"], 1))
        print(f"\n🏅 推奨: {best['id']} ({best['label']})")
    else:
        # DD 50% まで緩和して再評価
        acceptable2 = [r for r in results
                       if r["total_ret"] >= baseline_ret * 0.8 and r["max_dd"] <= 50]
        if acceptable2:
            best = max(acceptable2, key=lambda r: r["total_ret"] / max(r["max_dd"], 1))
            print(f"\n⚠️ DD45%基準では採用候補なし。DD50%緩和での推奨: {best['id']} ({best['label']})")
        else:
            best = baseline
            print(f"\n⚠️ 4機能のどれも現行を改善せず。現行 v2 維持を推奨")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script": "_iter52_risk_mgmt.py",
        "data_source": "Binance daily 実データ (62銘柄)",
        "universe_size": len(universe),
        "patterns_tested": len(patterns),
        "total_elapsed_sec": round(time.time() - t_start, 2),
        "baseline": baseline,
        "recommended": best,
        "acceptable": acceptable,
        "all_results": results,
        "thresholds": {
            "atr_stop_mult": ATR_STOP_MULT,
            "dd_circuit": DD_CIRCUIT_THRESHOLD,
            "dd_release": DD_CIRCUIT_RELEASE,
            "dd_ach_reduced": DD_ACH_REDUCED,
            "corr_threshold": CORR_THRESHOLD,
            "corr_lookback": CORR_LOOKBACK,
            "time_exit_days": TIME_EXIT_DAYS,
        },
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")


if __name__ == "__main__":
    main()
