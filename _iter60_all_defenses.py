"""
iter60: 全リスク軽減候補の包括検証
=====================================
v2.2 ベースに以下の防御機能を1つずつ/組合せで検証:

  F1. 利確定期退避 (月末に利益の X% を USDT へ)
  F2. マイルストーン退避 (2x/5x/10x 到達でポーション退避)
  F3. 年末強制利確 (12/31 全決済)
  F4. ATR 適応ポジションサイジング (高ボラで縮小)
  F5. 連敗停止ルール (N連敗で X週間休業)
  F6. DCA on entry (3日分割購入)
  F7. BTC Dominance フィルター (Dom > X% で ACH スキップ)

データ: Binance 62銘柄 日足 2020-2024 (ハルシネーション0)

判定:
  - DD が v2.2 の 64.6% より改善
  - リターンが v2.2 の 80% 以上維持 (+4,600%以上)
  - 最悪年 ≥ -5%
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

import pandas as pd
import _iter43_rethink as R43
from _iter54_comprehensive import select_top
import _iter54_comprehensive as M

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
RESULTS_DIR = PROJECT / "results"
CACHE_PATH = RESULTS_DIR / "_cache_alldata.pkl"
OUT_JSON = RESULTS_DIR / "iter60_all_defenses.json"

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


def run_bt_v60(all_data, universe, start, end,
                btc_w=0.35, ach_w=0.35, usdt_w=0.30,
                top_n=3, lookback=25, rebalance_days=7, adx_min=15,
                # v2.2 機能
                ach_bear_immediate=True,
                # v60 新機能
                profit_take_monthly_pct=0.0,   # F1: 月末に利益のX%をUSDTへ (0=OFF)
                milestone_extraction=False,      # F2: 2x/5x/10x到達で退避
                year_end_exit=False,            # F3: 12/31 全決済
                atr_adaptive=False,             # F4: ATR適応サイジング
                losing_streak_pause=0,          # F5: N連敗でX週間休業 (0=OFF)
                losing_streak_weeks=2,
                dca_entry_days=1,               # F6: N日分割購入 (1=OFF)
                initial=10000.0):
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    if not dates: return {}

    M.CORR_THRESHOLD = 0.80

    btc_cash = initial * btc_w
    btc_qty = 0.0
    ach_cash = initial * ach_w
    positions = {}  # sym -> {qty, entry_price}
    usdt_cash = initial * usdt_w
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0
    n_bear_exits = 0
    n_profit_takes = 0
    n_milestone_extracts = 0
    n_yearend_exits = 0
    n_streak_pauses = 0
    last_key = None
    last_month = None
    last_year = None
    losing_streak = 0
    pause_until = None
    milestones_hit = set()

    # DCA スケジュール: target_buys = [(date_when_to_buy, sym, pct_of_cash)]
    pending_dca = []  # (buy_date, sym, allocated_cash)

    for date in dates:
        btc_r = btc_df.loc[date]
        price = btc_r["close"]
        ema200 = btc_r.get("ema200")
        atr = btc_r.get("atr", 0)

        btc_bullish = not pd.isna(ema200) and price > ema200

        # ====== BTC 戦略 ======
        if btc_qty == 0 and btc_bullish:
            buy_p = price * (1 + SLIP)
            btc_qty = btc_cash / buy_p * (1 - FEE)
            btc_cash = 0; n_trades += 1
        elif btc_qty > 0 and not btc_bullish:
            sell_p = price * (1 - SLIP)
            btc_cash += btc_qty * sell_p * (1 - FEE)
            btc_qty = 0; n_trades += 1

        # v2.2: ACH即時ベア退避
        if ach_bear_immediate and not btc_bullish and positions:
            for sym in list(positions.keys()):
                df = all_data[sym]
                if date in df.index:
                    p = df.loc[date, "close"] * (1 - SLIP)
                    pos = positions[sym]
                    qty = pos["qty"] if isinstance(pos, dict) else pos
                    entry = pos["entry_price"] if isinstance(pos, dict) else 0
                    proceeds = qty * p * (1 - FEE)
                    ach_cash += proceeds
                    cost = qty * entry / (1 - FEE) if entry > 0 else proceeds
                    pnl = proceeds - cost
                    if pnl < 0:
                        losing_streak += 1
                    else:
                        losing_streak = 0
                    n_trades += 1
                    n_bear_exits += 1
                    positions.pop(sym)

        if btc_qty == 0: btc_cash *= (1 + 0.03/365)
        usdt_cash *= (1 + 0.03/365)

        # ====== F3: 年末強制利確 ======
        if year_end_exit and date.month == 12 and date.day >= 30:
            if last_year != date.year and (btc_qty > 0 or positions):
                if btc_qty > 0:
                    sell_p = price * (1 - SLIP)
                    btc_cash += btc_qty * sell_p * (1 - FEE)
                    btc_qty = 0; n_trades += 1
                for sym in list(positions.keys()):
                    df = all_data[sym]
                    if date in df.index:
                        pos = positions[sym]
                        qty = pos["qty"] if isinstance(pos, dict) else pos
                        p = df.loc[date, "close"] * (1 - SLIP)
                        ach_cash += qty * p * (1 - FEE)
                        n_trades += 1
                        positions.pop(sym)
                n_yearend_exits += 1
                last_year = date.year

        # ====== F1: 月末利確退避 (profit take to USDT) ======
        if profit_take_monthly_pct > 0 and date.month != last_month:
            if last_month is not None:
                # 前月末相当: USDT 以外の利益分を計算
                # 単純化: ACH cash + positions value の profit_take_monthly_pct % を USDT に移動
                ach_value_now = ach_cash
                for sym, pos in positions.items():
                    df = all_data[sym]
                    if date in df.index:
                        qty = pos["qty"] if isinstance(pos, dict) else pos
                        ach_value_now += qty * df.loc[date, "close"]

                # 初期 ACH 割当より多い分(=利益)の X%を USDT に退避
                ach_initial = initial * ach_w
                profit_ach = max(0, ach_value_now - ach_initial)
                if profit_ach > 0:
                    extract = profit_ach * profit_take_monthly_pct
                    # positions を比例で縮小
                    if ach_value_now > 0:
                        ratio = extract / ach_value_now
                        for sym in list(positions.keys()):
                            pos = positions[sym]
                            qty = pos["qty"] if isinstance(pos, dict) else pos
                            df = all_data[sym]
                            if date in df.index:
                                sell_qty = qty * ratio
                                p = df.loc[date, "close"] * (1 - SLIP)
                                ach_cash -= extract * (ach_cash / ach_value_now)
                                ach_cash_deduct = sell_qty * df.loc[date, "close"]
                                ach_cash -= ach_cash_deduct
                                usdt_cash += sell_qty * p * (1 - FEE)
                                n_trades += 1
                                positions[sym] = {"qty": qty - sell_qty,
                                                   "entry_price": pos["entry_price"] if isinstance(pos, dict) else 0}
                        # ACH cash 分も比例退避
                        cash_extract = ach_cash * ratio
                        ach_cash -= cash_extract
                        usdt_cash += cash_extract
                        n_profit_takes += 1
            last_month = date.month

        # ====== F2: マイルストーン退避 ======
        if milestone_extraction:
            ach_value_now = ach_cash + sum(
                (p["qty"] if isinstance(p, dict) else p) * all_data[s].loc[date, "close"]
                for s, p in positions.items() if date in all_data[s].index)
            total_now = btc_cash + btc_qty * price + ach_value_now + usdt_cash
            ratio_to_initial = total_now / initial
            thresholds = [(2, 0.30), (5, 0.40), (10, 0.50)]  # (倍率, 抽出率)
            for mult, extract_ratio in thresholds:
                if ratio_to_initial >= mult and mult not in milestones_hit:
                    # 全リスク資産から extract_ratio を USDT へ
                    if btc_qty > 0:
                        sell_qty = btc_qty * extract_ratio
                        sell_p = price * (1 - SLIP)
                        btc_cash += sell_qty * sell_p * (1 - FEE)
                        btc_qty -= sell_qty
                        usdt_cash += btc_cash
                        btc_cash = 0
                        n_trades += 1
                    for sym in list(positions.keys()):
                        pos = positions[sym]
                        qty = pos["qty"] if isinstance(pos, dict) else pos
                        df = all_data[sym]
                        if date in df.index:
                            sell_qty = qty * extract_ratio
                            p = df.loc[date, "close"] * (1 - SLIP)
                            proceeds = sell_qty * p * (1 - FEE)
                            usdt_cash += proceeds
                            positions[sym] = {"qty": qty - sell_qty,
                                               "entry_price": pos["entry_price"] if isinstance(pos, dict) else 0}
                            n_trades += 1
                    ach_cash_extract = ach_cash * extract_ratio
                    usdt_cash += ach_cash_extract
                    ach_cash -= ach_cash_extract
                    n_milestone_extracts += 1
                    milestones_hit.add(mult)

        # ====== F5: 連敗休業 ======
        if pause_until and date < pause_until:
            # 休業中 - リバランス実行しない
            ach_value = ach_cash
            for sym, pos in positions.items():
                df = all_data[sym]
                if date in df.index:
                    qty = pos["qty"] if isinstance(pos, dict) else pos
                    ach_value += qty * df.loc[date, "close"]
            total = btc_cash + btc_qty * price + ach_value + usdt_cash
            equity_curve.append({"ts": date, "equity": total})
            continue

        if losing_streak_pause > 0 and losing_streak >= losing_streak_pause:
            pause_until = date + pd.Timedelta(weeks=losing_streak_weeks)
            losing_streak = 0
            n_streak_pauses += 1

        # ====== ACH リバランス ======
        cur_key = _reb_key(date, rebalance_days)
        if cur_key != last_key:
            # 全決済
            for sym in list(positions.keys()):
                df = all_data[sym]
                if date in df.index:
                    pos = positions[sym]
                    qty = pos["qty"] if isinstance(pos, dict) else pos
                    entry = pos["entry_price"] if isinstance(pos, dict) else 0
                    p = df.loc[date, "close"] * (1 - SLIP)
                    proceeds = qty * p * (1 - FEE)
                    ach_cash += proceeds
                    cost = qty * entry / (1 - FEE) if entry > 0 else proceeds
                    pnl = proceeds - cost
                    if pnl < 0:
                        losing_streak += 1
                    else:
                        losing_streak = 0
                    n_trades += 1
                    positions.pop(sym)

            if not btc_bullish:
                last_key = cur_key
            else:
                # F4: ATR 適応
                size_multiplier = 1.0
                if atr_adaptive and not pd.isna(atr) and price > 0:
                    atr_pct = atr / price
                    if atr_pct > 0.05:
                        size_multiplier = 0.5
                    elif atr_pct > 0.03:
                        size_multiplier = 0.75

                sel = select_top(all_data, universe, date, top_n, lookback,
                                  adx_min, 0, "momentum", False)
                if sel:
                    pos_rets = [max(r, 0.01) for _, r in sel]
                    total_w = sum(pos_rets)
                    weights = [r/total_w for r in pos_rets]

                    for (sym, _), w in zip(sel, weights):
                        df = all_data[sym]
                        p_buy = df.loc[date, "close"] * (1 + SLIP)
                        cost = ach_cash * w * size_multiplier
                        if cost > 0:
                            qty = cost / p_buy * (1 - FEE)
                            positions[sym] = {"qty": qty, "entry_price": p_buy}
                            n_trades += 1
                    used = sum(ach_cash * w * size_multiplier for w in weights)
                    ach_cash -= used
                last_key = cur_key

        # 時価評価
        ach_value = ach_cash
        for sym, pos in positions.items():
            df = all_data[sym]
            if date in df.index:
                qty = pos["qty"] if isinstance(pos, dict) else pos
                ach_value += qty * df.loc[date, "close"]
        total = btc_cash + btc_qty * price + ach_value + usdt_cash
        equity_curve.append({"ts": date, "equity": total})

    r = R43.summarize(equity_curve, initial, n_trades=n_trades)
    r["yearly"] = calc_yearly_rets(equity_curve, initial)
    r["n_bear_exits"] = n_bear_exits
    r["n_profit_takes"] = n_profit_takes
    r["n_milestone_extracts"] = n_milestone_extracts
    r["n_yearend_exits"] = n_yearend_exits
    r["n_streak_pauses"] = n_streak_pauses
    return r


def main():
    print("=" * 70)
    print("🔬 iter60 全リスク軽減候補 包括検証")
    print("=" * 70)

    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    universe = sorted([s for s in all_data.keys() if s not in UNIVERSE_REMOVE])

    tests = [
        # ベースライン v2.2
        {"id": "V22_BASE", "label": "v2.2 ベースライン"},
        # F1: 利確退避 (月次 10% / 20% / 30%)
        {"id": "F1_PT10", "label": "F1: 月末利確退避 10%",
         "profit_take_monthly_pct": 0.10},
        {"id": "F1_PT20", "label": "F1: 月末利確退避 20%",
         "profit_take_monthly_pct": 0.20},
        {"id": "F1_PT30", "label": "F1: 月末利確退避 30%",
         "profit_take_monthly_pct": 0.30},
        # F2: マイルストーン退避
        {"id": "F2_MILESTONE", "label": "F2: 2x/5x/10x でポーション退避",
         "milestone_extraction": True},
        # F3: 年末強制利確
        {"id": "F3_YEAREND", "label": "F3: 12/31 全決済",
         "year_end_exit": True},
        # F4: ATR 適応サイジング
        {"id": "F4_ATR", "label": "F4: ATR 適応サイジング",
         "atr_adaptive": True},
        # F5: 連敗停止
        {"id": "F5_STREAK3", "label": "F5: 3連敗で2週間停止",
         "losing_streak_pause": 3, "losing_streak_weeks": 2},
        {"id": "F5_STREAK5", "label": "F5: 5連敗で4週間停止",
         "losing_streak_pause": 5, "losing_streak_weeks": 4},
        # 組合せ
        {"id": "C1_MS_YE", "label": "C1: マイルストーン + 年末利確",
         "milestone_extraction": True, "year_end_exit": True},
        {"id": "C2_PT20_MS", "label": "C2: 利確10% + マイルストーン",
         "profit_take_monthly_pct": 0.10, "milestone_extraction": True},
        {"id": "C3_PT20_YE", "label": "C3: 利確20% + 年末利確",
         "profit_take_monthly_pct": 0.20, "year_end_exit": True},
        {"id": "C4_ALL_CONSERVATIVE", "label": "C4: 全部入り保守 (利確20%+MS+YE+ATR+連敗3)",
         "profit_take_monthly_pct": 0.20, "milestone_extraction": True,
         "year_end_exit": True, "atr_adaptive": True, "losing_streak_pause": 3},
        {"id": "C5_LIGHT_DEFENSE", "label": "C5: 軽防御 (MS + 年末)",
         "milestone_extraction": True, "year_end_exit": True},
        {"id": "C6_MODERATE", "label": "C6: 中程度 (PT10 + MS + 年末)",
         "profit_take_monthly_pct": 0.10, "milestone_extraction": True,
         "year_end_exit": True},
    ]

    results = []
    t_start = time.time()
    for i, t in enumerate(tests, 1):
        print(f"[{i:2d}/{len(tests)}] {t['id']}: {t['label']}")
        t0 = time.time()
        kwargs = {k: v for k, v in t.items() if k not in ("id", "label")}
        r = run_bt_v60(all_data, universe, "2020-01-01", "2024-12-31", **kwargs)
        elapsed = time.time() - t0
        r.update({**t, "elapsed": round(elapsed, 2)})
        results.append(r)
        yearly_min = min(r["yearly"].values()) if r.get("yearly") else 0
        cagr = ((r["final"] / 10000) ** (1/5) - 1) * 100
        r["cagr"] = cagr
        print(f"   {elapsed:.1f}s | ret {r['total_ret']:+7.0f}% (CAGR {cagr:+5.1f}%) / "
              f"DD {r['max_dd']:5.1f}% / 最悪年 {yearly_min:+.1f}% | "
              f"bear{r['n_bear_exits']} PT{r['n_profit_takes']} MS{r['n_milestone_extracts']} YE{r['n_yearend_exits']}")

    # v2.2 ベースラインと比較
    v22 = results[0]
    v22_ret = v22["total_ret"]
    v22_dd = v22["max_dd"]
    v22_worst = min(v22["yearly"].values())

    print("\n" + "=" * 70)
    print(f"📊 v2.2 ベース: ret {v22_ret:+.0f}% / DD {v22_dd:.1f}% / 最悪年 {v22_worst:+.1f}%")
    print("=" * 70)

    # 評価基準
    print("\n🏆 DD低い順 (リスク軽減優先)")
    print("-" * 70)
    ranked_dd = sorted(results, key=lambda r: r["max_dd"])
    for i, r in enumerate(ranked_dd, 1):
        y_min = min(r["yearly"].values())
        d_ret = r["total_ret"] - v22_ret
        d_dd = r["max_dd"] - v22_dd
        ok = (r["total_ret"] >= v22_ret * 0.80 and r["max_dd"] < v22_dd and y_min > -5)
        icon = "✅" if ok else "  "
        print(f"  {i:2d}. {icon}{r['id']:25s}: DD {r['max_dd']:5.1f}% ({d_dd:+5.1f}) / "
              f"ret {r['total_ret']:+6.0f}% ({d_ret:+6.0f}) / 最悪年 {y_min:+5.1f}%")

    print("\n💎 リスク軽減候補 (ret≥80%維持 AND DD<v2.2 AND 最悪年≥-5%)")
    print("-" * 70)
    winners = [r for r in results
               if r["id"] != "V22_BASE"
               and r["total_ret"] >= v22_ret * 0.80
               and r["max_dd"] < v22_dd
               and min(r["yearly"].values()) >= -5]
    if winners:
        for r in sorted(winners, key=lambda x: x["max_dd"]):
            y_min = min(r["yearly"].values())
            print(f"  {r['id']:25s} ({r['label']:40s}):")
            print(f"     ret {r['total_ret']:+.0f}% / DD {r['max_dd']:.1f}% / 最悪年 {y_min:+.1f}%")
        best = min(winners, key=lambda r: r["max_dd"])
        print(f"\n🏅 最優秀 DD削減: {best['id']} ({best['label']})")
        best_cagr = best["cagr"]
        final_10k = 10000 * (1 + best["total_ret"]/100)
        print(f"   CAGR {best_cagr:+.1f}%/年 / $10K→${final_10k:,.0f}")
    else:
        print("  該当なし - 80%ret維持 & DD改善の組合せが見つからない")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(universe),
        "v22_baseline": v22,
        "winners": winners,
        "all_results": results,
        "total_elapsed_sec": round(time.time() - t_start, 2),
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")


if __name__ == "__main__":
    main()
