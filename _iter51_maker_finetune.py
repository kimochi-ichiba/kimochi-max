"""
iter51: Maker手数料 + 超細粒度チューニング
==========================================
Phase 3 (指値注文化) 導入時の効果を実データで検証。

検証する fee シナリオ:
  - Taker (現行市場注文):  slip 0.05% + fee 0.10% = 片道0.15%
  - Zero (指値約定・手数料なし): slip 0.05% + fee 0.00% = 片道0.05%
  - Maker (bitbank/GMO想定): slip 0.05% + fee -0.02% = 片道0.03%
  - Maker Rebate Pro: slip 0.02% + fee -0.02% = 片道0%
  (※ fee=-0.02% は「払うのではなく貰える」意味)

チューニング次元:
  - Top N: 2, 3, 4, 5
  - Lookback: 20, 25, 30, 35, 40, 45, 50, 60
  - Rebalance: 7d, 10d, 14d, 21d, 30d

組合せ: 8 × 5 × 4 = 160パターン (多すぎるので代表72パターンに絞る)
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

PROJECT = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT / "results"
CACHE_PATH = RESULTS_DIR / "_cache_alldata.pkl"
OUT_JSON = RESULTS_DIR / "iter51_maker_finetune.json"

UNIVERSE_REMOVE = ["MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"]

# 手数料シナリオ
FEE_SCENARIOS = {
    "taker":       {"slip": 0.0005, "fee": 0.0010, "label": "市場注文(Taker)", "emoji": "🔴"},
    "zero":        {"slip": 0.0005, "fee": 0.0000, "label": "指値Zero",        "emoji": "🟡"},
    "maker":       {"slip": 0.0005, "fee": -0.0002, "label": "指値Maker",       "emoji": "🟢"},
    "maker_pro":   {"slip": 0.0002, "fee": -0.0002, "label": "指値Pro(低slip)", "emoji": "🏆"},
}

# _iter49_rigorous.py の run_h11_pure をインポート + カスタムfee対応版を作成
from _iter49_rigorous import _rebalance_key
import _iter43_rethink as R43


def run_ach_custom_fee(all_data, universe, start, end, top_n, lookback,
                       rebalance, slip, fee, initial=10_000.0):
    """カスタム fee/slip 対応 ACH"""
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    if not dates:
        return {"final": initial, "total_ret": 0, "max_dd": 0, "sharpe": 0,
                "n_trades": 0, "yearly": {}, "equity_weekly": []}

    cash = initial
    positions = {}
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0
    last_key = None

    for date in dates:
        cur_key = _rebalance_key(date, rebalance)
        do_reb = (last_key is None) or (cur_key != last_key)

        if do_reb:
            # 全決済
            for sym in list(positions.keys()):
                df = all_data[sym]
                if date in df.index:
                    price = df.loc[date, "close"] * (1 - slip)
                    cash += positions[sym] * price * (1 - fee)
                    n_trades += 1
                    positions.pop(sym)

            # TopN 選定
            scores = []
            for sym in universe:
                if sym not in all_data:
                    continue
                df = all_data[sym]
                if date not in df.index:
                    continue
                past = df.index[(df.index < date) & (df.index >= date - pd.Timedelta(days=lookback))]
                if len(past) < 20:
                    continue
                price_now = df.loc[date, "close"]
                price_past = df.loc[past[0], "close"]
                ret = price_now / price_past - 1
                adx = df.loc[date].get("adx", 0)
                if pd.isna(adx) or adx < 20:
                    continue
                scores.append((sym, ret))
            scores.sort(key=lambda x: x[1], reverse=True)

            btc_r = btc_df.loc[date]
            btc_ema200 = btc_r.get("ema200")
            if not pd.isna(btc_ema200) and btc_r["close"] < btc_ema200:
                last_key = cur_key
            else:
                sel = scores[:top_n]
                if sel:
                    w = 1.0 / len(sel)
                    for sym, _ in sel:
                        df = all_data[sym]
                        price_buy = df.loc[date, "close"] * (1 + slip)
                        cost = cash * w
                        if cost > 0:
                            qty = cost / price_buy * (1 - fee)
                            positions[sym] = qty
                            cash -= cost
                            n_trades += 1
                last_key = cur_key

        total = cash
        for sym, qty in positions.items():
            df = all_data[sym]
            if date in df.index:
                total += qty * df.loc[date, "close"]
        equity_curve.append({"ts": date, "equity": total})

    return R43.summarize(equity_curve, initial, n_trades=n_trades)


def run_btc_mild_custom_fee(all_data, start, end, slip, fee,
                               initial=10_000.0, cash_rate=0.03):
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    if not dates:
        return {"final": initial, "total_ret": 0, "max_dd": 0, "sharpe": 0,
                "n_trades": 0, "yearly": {}, "equity_weekly": []}

    cash = initial
    btc_qty = 0.0
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0

    for date in dates:
        r = btc_df.loc[date]
        price = r["close"]
        ema200 = r.get("ema200")
        if btc_qty == 0 and not pd.isna(ema200) and price > ema200:
            buy_p = price * (1 + slip)
            btc_qty = cash / buy_p * (1 - fee)
            cash = 0
            n_trades += 1
        elif btc_qty > 0 and not pd.isna(ema200) and price < ema200:
            sell_p = price * (1 - slip)
            cash += btc_qty * sell_p * (1 - fee)
            btc_qty = 0
            n_trades += 1

        if btc_qty == 0:
            cash *= (1 + cash_rate / 365)

        total = cash + btc_qty * price
        equity_curve.append({"ts": date, "equity": total})

    return R43.summarize(equity_curve, initial, n_trades=n_trades)


def run_h11_custom(all_data, universe, start, end, top_n, lookback,
                    rebalance, slip, fee, initial=10_000.0):
    btc_res = run_btc_mild_custom_fee(all_data, start, end, slip, fee,
                                         initial=initial * 0.4, cash_rate=0.03)
    ach_res = run_ach_custom_fee(all_data, universe, start, end,
                                    top_n=top_n, lookback=lookback,
                                    rebalance=rebalance, slip=slip, fee=fee,
                                    initial=initial * 0.4)
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    dates = [d for d in all_data["BTC/USDT"].index if start_ts <= d <= end_ts]
    days = max(1, len(dates))
    usdt_eq = initial * 0.2 * (1 + 0.03 / 365) ** days

    combined_final = btc_res["final"] + ach_res["final"] + usdt_eq
    combined_ret = (combined_final / initial - 1) * 100
    n_years = max(1, days / 365)
    calmar = (combined_ret / max(1e-9, max(btc_res.get("max_dd", 0), ach_res.get("max_dd", 0))))

    return {
        "final": round(combined_final, 2),
        "total_ret": round(combined_ret, 2),
        "avg_annual_ret": round((combined_final / initial) ** (1 / n_years) * 100 - 100, 2),
        "max_dd": max(btc_res.get("max_dd", 0), ach_res.get("max_dd", 0)),
        "sharpe": round(btc_res.get("sharpe", 0) * 0.4 + ach_res.get("sharpe", 0) * 0.4, 2),
        "calmar": round(calmar, 2),
        "n_trades": btc_res.get("n_trades", 0) + ach_res.get("n_trades", 0),
    }


# iter49 の rebalance keyは 'weekly' 'biweekly' 'monthly' しかサポートしていないので
# 拡張: 日数を受ける
def custom_rebalance_key(date, days: int):
    """汎用: 指定日数ごとにbin"""
    doy = date.dayofyear + (date.year - 2020) * 366
    return doy // days


# 追加: 日数指定rebalance対応版 run_ach
def run_ach_days(all_data, universe, start, end, top_n, lookback,
                  rebalance_days: int, slip, fee, initial=10_000.0):
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    if not dates:
        return {"final": initial, "total_ret": 0, "max_dd": 0, "sharpe": 0,
                "n_trades": 0, "yearly": {}, "equity_weekly": []}

    cash = initial
    positions = {}
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0
    last_key = None

    for date in dates:
        cur_key = custom_rebalance_key(date, rebalance_days)
        do_reb = (last_key is None) or (cur_key != last_key)

        if do_reb:
            for sym in list(positions.keys()):
                df = all_data[sym]
                if date in df.index:
                    price = df.loc[date, "close"] * (1 - slip)
                    cash += positions[sym] * price * (1 - fee)
                    n_trades += 1
                    positions.pop(sym)

            scores = []
            for sym in universe:
                if sym not in all_data:
                    continue
                df = all_data[sym]
                if date not in df.index:
                    continue
                past = df.index[(df.index < date) & (df.index >= date - pd.Timedelta(days=lookback))]
                if len(past) < 20:
                    continue
                price_now = df.loc[date, "close"]
                price_past = df.loc[past[0], "close"]
                ret = price_now / price_past - 1
                adx = df.loc[date].get("adx", 0)
                if pd.isna(adx) or adx < 20:
                    continue
                scores.append((sym, ret))
            scores.sort(key=lambda x: x[1], reverse=True)

            btc_r = btc_df.loc[date]
            btc_ema200 = btc_r.get("ema200")
            if not pd.isna(btc_ema200) and btc_r["close"] < btc_ema200:
                last_key = cur_key
            else:
                sel = scores[:top_n]
                if sel:
                    w = 1.0 / len(sel)
                    for sym, _ in sel:
                        df = all_data[sym]
                        price_buy = df.loc[date, "close"] * (1 + slip)
                        cost = cash * w
                        if cost > 0:
                            qty = cost / price_buy * (1 - fee)
                            positions[sym] = qty
                            cash -= cost
                            n_trades += 1
                last_key = cur_key

        total = cash
        for sym, qty in positions.items():
            df = all_data[sym]
            if date in df.index:
                total += qty * df.loc[date, "close"]
        equity_curve.append({"ts": date, "equity": total})

    return R43.summarize(equity_curve, initial, n_trades=n_trades)


def run_h11_days(all_data, universe, start, end, top_n, lookback,
                   rebalance_days, slip, fee, initial=10_000.0):
    btc_res = run_btc_mild_custom_fee(all_data, start, end, slip, fee,
                                         initial=initial * 0.4, cash_rate=0.03)
    ach_res = run_ach_days(all_data, universe, start, end,
                             top_n=top_n, lookback=lookback,
                             rebalance_days=rebalance_days,
                             slip=slip, fee=fee, initial=initial * 0.4)
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    dates = [d for d in all_data["BTC/USDT"].index if start_ts <= d <= end_ts]
    days = max(1, len(dates))
    usdt_eq = initial * 0.2 * (1 + 0.03 / 365) ** days

    combined_final = btc_res["final"] + ach_res["final"] + usdt_eq
    combined_ret = (combined_final / initial - 1) * 100
    n_years = max(1, days / 365)
    calmar = (combined_ret / max(1e-9, max(btc_res.get("max_dd", 0), ach_res.get("max_dd", 0))))

    return {
        "final": round(combined_final, 2),
        "total_ret": round(combined_ret, 2),
        "avg_annual_ret": round((combined_final / initial) ** (1 / n_years) * 100 - 100, 2),
        "max_dd": max(btc_res.get("max_dd", 0), ach_res.get("max_dd", 0)),
        "sharpe": round(btc_res.get("sharpe", 0) * 0.4 + ach_res.get("sharpe", 0) * 0.4, 2),
        "calmar": round(calmar, 2),
        "n_trades": btc_res.get("n_trades", 0) + ach_res.get("n_trades", 0),
    }


# 3D グリッド (選別)
TOPS = [2, 3, 4, 5]
LOOKBACKS = [25, 35, 45, 55]
REBALANCE_DAYS = [7, 14, 21, 30]  # 週次〜月次


def main():
    print("=" * 70)
    print("🔬 iter51 Maker fee + 超細粒度チューニング")
    print("=" * 70)

    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    universe = sorted([s for s in all_data.keys() if s not in UNIVERSE_REMOVE])
    print(f"ユニバース: {len(universe)}銘柄")

    # 全パターン構築
    patterns = []
    for tn in TOPS:
        for lb in LOOKBACKS:
            for rb in REBALANCE_DAYS:
                for fee_key, f in FEE_SCENARIOS.items():
                    patterns.append({
                        "id": f"T{tn}_LB{lb}_R{rb}_{fee_key}",
                        "top_n": tn, "lookback": lb, "rebalance_days": rb,
                        "fee_scenario": fee_key, "slip": f["slip"], "fee": f["fee"],
                        "fee_label": f["label"],
                    })
    print(f"総パターン数: {len(patterns)} (4×4×4×4)")

    results = []
    t_start = time.time()
    for i, p in enumerate(patterns, 1):
        if i % 20 == 0:
            elapsed = time.time() - t_start
            print(f"  ... {i}/{len(patterns)} ({elapsed:.0f}s)")
        t0 = time.time()
        r = run_h11_days(all_data, universe, "2020-01-01", "2024-12-31",
                          top_n=p["top_n"], lookback=p["lookback"],
                          rebalance_days=p["rebalance_days"],
                          slip=p["slip"], fee=p["fee"])
        r.update({**p, "elapsed": round(time.time() - t0, 2)})
        results.append(r)

    total_time = time.time() - t_start
    print(f"\n✅ 完了 ({total_time:.0f}秒)")

    # fee scenario別ランキング
    print("\n" + "=" * 70)
    print("🏆 fee シナリオ別 Top5")
    print("=" * 70)
    for fee_key in FEE_SCENARIOS.keys():
        f = FEE_SCENARIOS[fee_key]
        subset = [r for r in results if r["fee_scenario"] == fee_key]
        subset.sort(key=lambda r: r["total_ret"], reverse=True)
        print(f"\n{f['emoji']} {f['label']} (slip={f['slip']*100:.2f}%, fee={f['fee']*100:+.2f}%)")
        for i, r in enumerate(subset[:5], 1):
            print(f"  {i}. T{r['top_n']}/LB{r['lookback']}/R{r['rebalance_days']}d: "
                  f"{r['total_ret']:+.1f}% (DD {r['max_dd']:.1f}%, {r['n_trades']}取引)")

    # 現行設定 (T3/LB45/R30, taker) との差分
    current_cfg = next((r for r in results
                        if r["top_n"] == 3 and r["lookback"] == 45
                        and r["rebalance_days"] == 30 and r["fee_scenario"] == "taker"), None)
    # Phase 3 reality: maker シナリオでの最大
    maker_winner = max([r for r in results if r["fee_scenario"] == "maker"],
                       key=lambda r: r["total_ret"])

    print("\n" + "=" * 70)
    print("📊 Phase 3 (指値Maker) 導入時の期待効果")
    print("=" * 70)
    if current_cfg:
        print(f"現行 (Taker, T3/LB45/月次): {current_cfg['total_ret']:+.1f}% / DD {current_cfg['max_dd']:.1f}%")
    print(f"Phase 3 最強 (Maker, T{maker_winner['top_n']}/LB{maker_winner['lookback']}/"
          f"R{maker_winner['rebalance_days']}d): {maker_winner['total_ret']:+.1f}% / "
          f"DD {maker_winner['max_dd']:.1f}%")
    if current_cfg:
        improvement = maker_winner['total_ret'] - current_cfg['total_ret']
        print(f"改善幅: {improvement:+.1f}%pt")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script": "_iter51_maker_finetune.py",
        "universe_size": len(universe),
        "data_source": "Binance daily 実データ (62銘柄・FAIL除外後)",
        "fee_scenarios": {k: {"slip_pct": v["slip"]*100, "fee_pct": v["fee"]*100,
                              "label": v["label"]}
                          for k, v in FEE_SCENARIOS.items()},
        "patterns_tested": len(patterns),
        "total_elapsed_sec": round(total_time, 2),
        "current_setting": current_cfg,
        "maker_winner": maker_winner,
        "all_results": results,
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")


if __name__ == "__main__":
    main()
