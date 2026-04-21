"""
iter50: iter49優勝設定(Top3/月次/LB90)の周辺を細かく探索
=========================================================
探索次元:
  - Top N: 2, 3, 4 (Top3周辺)
  - Lookback: 45, 60, 75, 90, 120 (90日周辺)
  - Rebalance: 14d, 21d, 30d, 45d (30日周辺)
  - 上記の組合せ + スリッページ補正で真の勝者を決定

データ: Binance daily 実データ (_cache_alldata.pkl, 62銘柄)
スリッページ補正: Standardシナリオ (slip 0.05% + fee 0.10%)
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

from _iter49_rigorous import run_h11_pure, UNIVERSE_REMOVE

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
RESULTS_DIR = PROJECT / "results"
CACHE_PATH = RESULTS_DIR / "_cache_alldata.pkl"
OUT_JSON = RESULTS_DIR / "iter50_finetune.json"

# Standardシナリオ (市場注文想定)
STD_SLIP = 0.0005
STD_FEE = 0.0010


def apply_standard_slippage(theoretical_ret: float, n_trades: int,
                             initial: float = 10000) -> float:
    """理論リターン% と取引数から Standard Scenario での現実リターンを計算"""
    cost = STD_SLIP + STD_FEE
    shrink = (1.0 - cost) ** n_trades
    theo_final = initial * (1 + theoretical_ret / 100)
    adj_final = initial + (theo_final - initial) * shrink
    return (adj_final / initial - 1) * 100


def rebalance_days_to_label(days: int) -> str:
    """日数からラベルへ"""
    if days == 1: return "daily"
    if days == 3: return "3day"
    if days == 7: return "weekly"
    if days == 14: return "biweekly"
    if days == 21: return "triweekly"
    if days == 30: return "monthly"
    if days == 45: return "45day"
    if days == 60: return "bimonthly"
    return f"{days}day"


# 探索パターン
# iter49 は rebalance が 'monthly' or 'weekly' のみサポート。
# iter50 では biweekly/triweekly/45day を追加するため、run_h11_pure の rebalance 引数を
# そのまま使える文字列ラベルに統一する。
# iter49 の _rebalance_key は 'biweekly' を既にサポートしているので、weekly/biweekly/monthly は OK
# triweekly/45day は独自実装が必要なので、ここでは biweekly(14) / weekly(7) / monthly(30) で近似

PATTERNS = []

# 【次元1】Top N sensitivity (monthly, LB90)
for t in [2, 3, 4, 5]:
    PATTERNS.append({
        "id": f"Top{t}_M_LB90",
        "category": "TopN周辺",
        "top_n": t, "rebalance": "monthly", "lookback": 90,
    })

# 【次元2】Lookback sensitivity (Top3, monthly)
for lb in [45, 60, 75, 90, 105, 120]:
    PATTERNS.append({
        "id": f"Top3_M_LB{lb}",
        "category": "Lookback周辺",
        "top_n": 3, "rebalance": "monthly", "lookback": lb,
    })

# 【次元3】Rebalance frequency (Top3, LB90)
for rb in ["weekly", "biweekly", "monthly"]:
    PATTERNS.append({
        "id": f"Top3_{rb[:3].upper()}_LB90",
        "category": "リバランス周辺",
        "top_n": 3, "rebalance": rb, "lookback": 90,
    })

# 【次元4】2D探索: Top × Rebalance
for t in [2, 3, 4]:
    for rb in ["biweekly", "monthly"]:
        PATTERNS.append({
            "id": f"2D_Top{t}_{rb[:3].upper()}",
            "category": "2D探索",
            "top_n": t, "rebalance": rb, "lookback": 90,
        })

# 重複パターン除去 (id unique に)
seen = set()
unique_patterns = []
for p in PATTERNS:
    if p["id"] not in seen:
        seen.add(p["id"])
        unique_patterns.append(p)
PATTERNS = unique_patterns


def main():
    print("=" * 70)
    print("🔬 iter50 ファインチューニング探索")
    print("=" * 70)
    print(f"検証パターン数: {len(PATTERNS)}")

    print("\n📦 キャッシュ読込...")
    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    universe = sorted([s for s in all_data.keys() if s not in UNIVERSE_REMOVE])
    print(f"ユニバース: {len(universe)}銘柄")

    results = []
    t_start = time.time()
    for i, p in enumerate(PATTERNS, 1):
        print(f"\n[{i}/{len(PATTERNS)}] {p['id']}: "
              f"Top{p['top_n']}/{p['rebalance']}/LB{p['lookback']} ({p['category']})")
        t0 = time.time()
        r = run_h11_pure(all_data, universe, "2020-01-01", "2024-12-31",
                         top_n=p["top_n"], lookback=p["lookback"],
                         rebalance=p["rebalance"])
        elapsed = time.time() - t0
        std_ret = apply_standard_slippage(r["total_ret"], r["n_trades"])
        r.update({
            **p, "elapsed_sec": round(elapsed, 2),
            "universe_size": len(universe),
            "standard_ret": round(std_ret, 2),
        })
        results.append(r)
        print(f"  ✅ {elapsed:.1f}s | 理論 {r['total_ret']:+.1f}% → "
              f"Std {std_ret:+.1f}% | DD {r['max_dd']:.1f}% | "
              f"取引 {r['n_trades']} | Calmar {r.get('calmar', 0):.1f}")

    # Standardシナリオでソート
    ranked = sorted(results, key=lambda r: r["standard_ret"], reverse=True)

    print("\n" + "=" * 70)
    print("🏆 Standardシナリオ (現実値) ランキング Top10")
    print("=" * 70)
    for i, r in enumerate(ranked[:10], 1):
        icon = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else f"{i}."))
        print(f"{icon} {r['id']:20s} "
              f"Top{r['top_n']}/{r['rebalance']:8s}/LB{r['lookback']:3d} | "
              f"理論 {r['total_ret']:>+8.1f}% → Std {r['standard_ret']:>+7.1f}% | "
              f"取引 {r['n_trades']:>4d} | DD {r['max_dd']:.1f}%")

    # 現行設定 (Top3/月次/LB90) を基準に差分計算
    current = next((r for r in results if r["id"] == "Top3_M_LB90"), None)
    if current:
        print("\n" + "=" * 70)
        print(f"📊 現行設定との比較 (基準: Top3/月次/LB90 = Std {current['standard_ret']:+.1f}%)")
        print("=" * 70)
        improvements = [r for r in ranked if r["standard_ret"] > current["standard_ret"]]
        if improvements:
            print(f"✅ 現行を上回る設定: {len(improvements)}件")
            for r in improvements:
                diff = r["standard_ret"] - current["standard_ret"]
                print(f"   {r['id']}: Std {r['standard_ret']:+.1f}% (現行比 {diff:+.2f}%pt)")
        else:
            print("🏆 現行設定 (Top3/月次/LB90) が最強!")
            print("   iter50 で検証した全 patterns の中で最高のStd return")

    best = ranked[0]
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script": "_iter50_finetune.py",
        "data_source": "Binance daily 実データ (62銘柄 - FAIL除外後)",
        "universe_size": len(universe),
        "patterns_tested": len(PATTERNS),
        "total_elapsed_sec": round(time.time() - t_start, 2),
        "best_std": {
            "id": best["id"],
            "top_n": best["top_n"],
            "rebalance": best["rebalance"],
            "lookback": best["lookback"],
            "total_ret_theo": best["total_ret"],
            "standard_ret": best["standard_ret"],
            "max_dd": best["max_dd"],
            "n_trades": best["n_trades"],
            "calmar": best.get("calmar", 0),
        },
        "current_setting": {
            "id": current["id"],
            "standard_ret": current["standard_ret"],
        } if current else None,
        "improves_over_current": bool(best["standard_ret"] > (current["standard_ret"] if current else 0)),
        "rankings_top10": [
            {"rank": i + 1, **{k: r[k] for k in ("id", "top_n", "rebalance", "lookback",
                                                  "total_ret", "standard_ret", "max_dd",
                                                  "n_trades")}}
            for i, r in enumerate(ranked[:10])
        ],
        "all_results": results,
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")
    print(f"⏱️ 合計 {summary['total_elapsed_sec']}秒")


if __name__ == "__main__":
    main()
