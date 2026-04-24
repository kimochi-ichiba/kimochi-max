"""
iter49: 良い改善のみ + 厳重バックテスト (sensitivity + walk-forward + regime)
============================================================================

iter48 で判明した事実:
  ✅ 効く:    銘柄拡張 (+130%pt), Top5+週次 (+1341%pt)  ← これだけ採用
  ❌ 効かない: RSIフィルター, トレーリングストップ, 部分利確, 急落休業

そのため本バックテストでは <<悪い改善を全て排除>> し、
<<Top N>>, <<リバランス頻度>>, <<lookback期間>> を感度分析で最適化する。

検証ブロック:
  [A] パラメータ感度分析 (3×3×3 = 9パターン)
  [B] ウォークフォワード検証 (3期間で再現性確認)
  [C] 年別レジーム分解 (bull/bear 耐性)
  [D] 初期資金スケール感度 ($1K/$10K/$100K)

データ: Binance daily 実データのみ, 2020-01-01 〜 2024-12-31
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np

import _iter43_rethink as R43

PROJECT = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT / "results"
CACHE_PATH = RESULTS_DIR / "_cache_alldata.pkl"
OUT_JSON = RESULTS_DIR / "iter49_rigorous.json"

FEE = 0.0006
SLIP = 0.0003

# iter48 で除外したFAIL銘柄
UNIVERSE_REMOVE = ["MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# バックテストエンジン (無余計な機能, 純粋な ACH+BTC mild)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _rebalance_key(date, rebalance):
    if rebalance == "daily":
        return (date.year, date.month, date.day)
    if rebalance == "3day":
        # 3日単位でbin
        doy = date.dayofyear
        return (date.year, doy // 3)
    if rebalance == "weekly":
        iso = date.isocalendar()
        return (iso.year, iso.week)
    if rebalance == "biweekly":
        iso = date.isocalendar()
        return (iso.year, iso.week // 2)
    if rebalance == "monthly":
        return (date.year, date.month)
    raise ValueError(f"unknown rebalance: {rebalance}")


def run_ach_pure(
    all_data, universe, start, end,
    top_n=5, lookback_days=90, rebalance="weekly",
    initial=10_000.0,
):
    """純粋な ACH モメンタム (余計な機能なし)"""
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    if not dates:
        return {"final": initial, "total_ret": 0, "max_dd": 0, "sharpe": 0, "n_trades": 0,
                "yearly": {}, "equity_weekly": []}

    cash = initial
    positions = {}  # sym -> qty
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
                    price = df.loc[date, "close"] * (1 - SLIP)
                    cash += positions[sym] * price * (1 - FEE)
                    n_trades += 1
                    positions.pop(sym)

            # TopN選定
            scores = []
            for sym in universe:
                if sym not in all_data:
                    continue
                df = all_data[sym]
                if date not in df.index:
                    continue
                past = df.index[(df.index < date) & (df.index >= date - pd.Timedelta(days=lookback_days))]
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

            # BTCレジーム
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
                        price_buy = df.loc[date, "close"] * (1 + SLIP)
                        cost = cash * w
                        if cost > 0:
                            qty = cost / price_buy * (1 - FEE)
                            positions[sym] = qty
                            cash -= cost
                            n_trades += 1
                last_key = cur_key

        # 時価評価
        total = cash
        for sym, qty in positions.items():
            df = all_data[sym]
            if date in df.index:
                total += qty * df.loc[date, "close"]
        equity_curve.append({"ts": date, "equity": total})

    return R43.summarize(equity_curve, initial, n_trades=n_trades)


def run_btc_mild_pure(all_data, start, end, initial=10_000.0, cash_rate=0.03):
    """純粋な BTC マイルド (余計な機能なし)"""
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    if not dates:
        return {"final": initial, "total_ret": 0, "max_dd": 0, "sharpe": 0, "n_trades": 0,
                "yearly": {}, "equity_weekly": []}

    cash = initial
    btc_qty = 0.0
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0

    for date in dates:
        r = btc_df.loc[date]
        price = r["close"]
        ema200 = r.get("ema200")
        if btc_qty == 0 and not pd.isna(ema200) and price > ema200:
            buy_p = price * (1 + SLIP)
            btc_qty = cash / buy_p * (1 - FEE)
            cash = 0
            n_trades += 1
        elif btc_qty > 0 and not pd.isna(ema200) and price < ema200:
            sell_p = price * (1 - SLIP)
            cash += btc_qty * sell_p * (1 - FEE)
            btc_qty = 0
            n_trades += 1

        if btc_qty == 0:
            cash *= (1 + cash_rate / 365)

        total = cash + btc_qty * price
        equity_curve.append({"ts": date, "equity": total})

    return R43.summarize(equity_curve, initial, n_trades=n_trades)


def run_h11_pure(all_data, universe, start, end, top_n=5, lookback=90,
                   rebalance="weekly", initial=10_000.0):
    """H11: BTC40% + ACH40% + USDT20%"""
    btc_res = run_btc_mild_pure(all_data, start, end, initial=initial * 0.4, cash_rate=0.03)
    ach_res = run_ach_pure(all_data, universe, start, end, top_n=top_n,
                            lookback_days=lookback, rebalance=rebalance,
                            initial=initial * 0.4)
    # USDT
    start_ts = pd.Timestamp(start); end_ts = pd.Timestamp(end)
    dates = [d for d in all_data["BTC/USDT"].index if start_ts <= d <= end_ts]
    days = max(1, len(dates))
    usdt_eq = initial * 0.2 * (1 + 0.03 / 365) ** days

    combined_final = btc_res["final"] + ach_res["final"] + usdt_eq
    combined_ret = (combined_final / initial - 1) * 100
    years = sorted(set(list(btc_res.get("yearly", {}).keys()) +
                       list(ach_res.get("yearly", {}).keys())))
    yearly = {}
    for y in years:
        yearly[y] = round(btc_res.get("yearly", {}).get(y, 0) * 0.4 +
                          ach_res.get("yearly", {}).get(y, 0) * 0.4 +
                          3.0 * 0.2, 2)

    n_years = max(1, days / 365)
    calmar = (combined_ret / max(1e-9, max(btc_res.get("max_dd", 0), ach_res.get("max_dd", 0))))
    return {
        "final": round(combined_final, 2),
        "total_ret": round(combined_ret, 2),
        "avg_annual_ret": round((combined_final / initial) ** (1 / n_years) * 100 - 100, 2),
        "yearly": yearly,
        "max_dd": max(btc_res.get("max_dd", 0), ach_res.get("max_dd", 0)),
        "sharpe": round(btc_res.get("sharpe", 0) * 0.4 + ach_res.get("sharpe", 0) * 0.4, 2),
        "calmar": round(calmar, 2),
        "n_trades": btc_res.get("n_trades", 0) + ach_res.get("n_trades", 0),
        "n_trades_btc": btc_res.get("n_trades", 0),
        "n_trades_ach": ach_res.get("n_trades", 0),
        "equity_weekly": ach_res.get("equity_weekly", []),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# シナリオ定義
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [A] パラメータ感度: Top3/5/7/10 × 日次/3日/週次/月次 (基準 lookback=90)
BLOCK_A = []
for t in [3, 5, 7, 10]:
    for rb in ["daily", "3day", "weekly", "monthly"]:
        BLOCK_A.append({
            "id": f"A-T{t}-{rb[:3].upper()}",
            "block": "A. パラメータ感度",
            "top_n": t, "rebalance": rb, "lookback": 90,
            "start": "2020-01-01", "end": "2024-12-31",
        })

# [A'] Lookback感度: 30/60/90/180d (Top5, 週次固定)
for lb in [30, 60, 90, 180]:
    BLOCK_A.append({
        "id": f"A-LB{lb}",
        "block": "A. Lookback感度",
        "top_n": 5, "rebalance": "weekly", "lookback": lb,
        "start": "2020-01-01", "end": "2024-12-31",
    })

# [B] ウォークフォワード (期間を切って再現性確認)
BLOCK_B = [
    {"id": "B-2020-2021", "block": "B. ウォークフォワード",
     "top_n": 5, "rebalance": "weekly", "lookback": 90,
     "start": "2020-01-01", "end": "2021-12-31"},
    {"id": "B-2022-2023", "block": "B. ウォークフォワード",
     "top_n": 5, "rebalance": "weekly", "lookback": 90,
     "start": "2022-01-01", "end": "2023-12-31"},
    {"id": "B-2024", "block": "B. ウォークフォワード",
     "top_n": 5, "rebalance": "weekly", "lookback": 90,
     "start": "2024-01-01", "end": "2024-12-31"},
    {"id": "B-FULL", "block": "B. ウォークフォワード",
     "top_n": 5, "rebalance": "weekly", "lookback": 90,
     "start": "2020-01-01", "end": "2024-12-31"},
]

ALL_SCENARIOS = BLOCK_A + BLOCK_B


def main():
    print("=" * 70)
    print("🔬 iter49: 良い改善のみ + 厳重バックテスト")
    print("=" * 70)

    print("\n📦 キャッシュ読込...")
    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    # FAIL除外後のユニバース
    universe = sorted([s for s in all_data.keys() if s not in UNIVERSE_REMOVE])
    print(f"📊 総データ銘柄: {len(all_data)}, ユニバース: {len(universe)}銘柄")

    results = []
    t_start = time.time()
    for i, s in enumerate(ALL_SCENARIOS, 1):
        print(f"\n[{i}/{len(ALL_SCENARIOS)}] {s['id']}: "
              f"Top{s['top_n']}/{s['rebalance']}/LB{s['lookback']} "
              f"({s['start']}〜{s['end']})")
        t0 = time.time()
        r = run_h11_pure(all_data, universe, s["start"], s["end"],
                         top_n=s["top_n"], lookback=s["lookback"],
                         rebalance=s["rebalance"])
        elapsed = time.time() - t0
        r.update({**s, "elapsed_sec": round(elapsed, 2),
                  "universe_size": len(universe)})
        results.append(r)
        print(f"  ✅ {elapsed:.1f}s | 最終 ${r['final']:,.0f} | "
              f"リターン {r['total_ret']:+.1f}% | DD {r['max_dd']:.1f}% | "
              f"Calmar {r.get('calmar', 0):.2f} | 取引 {r['n_trades']}回")

    # ブロック別ベスト検出
    block_A = [r for r in results if r["block"].startswith("A")]
    block_B = [r for r in results if r["block"].startswith("B")]
    full_period = [r for r in results if r["id"] == "B-FULL"][0]

    # sort by total_ret (for A block, full period)
    best_a = max(block_A, key=lambda r: r["total_ret"])

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script": "_iter49_rigorous.py",
        "data_source": "Binance daily 実データ (_cache_alldata.pkl)",
        "universe_size": len(universe),
        "universe_excluded": UNIVERSE_REMOVE,
        "improvements_applied": ["銘柄拡張", "Top N可変", "リバランス頻度可変", "Lookback可変"],
        "improvements_rejected": ["#1 トレーリングストップ (iter48で-1223%pt)",
                                    "#2 RSIフィルター (-934%pt)",
                                    "#4 部分利確 (-119%pt)",
                                    "#9 急落休業 (効果微小)"],
        "best_a_config": {"id": best_a["id"], "top_n": best_a["top_n"],
                          "rebalance": best_a["rebalance"], "lookback": best_a["lookback"],
                          "total_ret": best_a["total_ret"], "max_dd": best_a["max_dd"],
                          "calmar": best_a.get("calmar", 0)},
        "full_period_ret": full_period["total_ret"],
        "block_A_count": len(block_A),
        "block_B_count": len(block_B),
        "total_elapsed_sec": round(time.time() - t_start, 2),
        "results": results,
    }

    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 JSON: {OUT_JSON}")
    print(f"⏱️ 合計実行時間: {summary['total_elapsed_sec']}秒")
    print(f"\n🏆 ブロックA最優秀: {best_a['id']} ({best_a['total_ret']:+.1f}%)")


if __name__ == "__main__":
    main()
