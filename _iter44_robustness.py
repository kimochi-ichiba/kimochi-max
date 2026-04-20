"""
Iter44 Step3: ロバストネステスト (20回反復)
===============================================
目的:
  特定の期間や銘柄構成に「たまたま」勝っただけでないか検証する。

実施内容:
  パターンA (8回): 期間シフト
    - 2020〜2024 (基準)
    - 2019半ば〜2023末、2020半ば〜2024半ば、2021〜2024末、など
  パターンB (6回): パラメータ微変動
    - モメンタム Top3: lookback 60/90/120、top_n 2/3/4/5
    - BTCマイルド: EMA期間 100/150/200/250
  パターンC (6回): 銘柄シャッフル
    - UNIVERSE_50 から 40/35/30 銘柄ランダム抽出（seed固定）

ベースライン (R05 モメンタムTop3, R04b BTCマイルド, R10 ハイブリッド)を各バリアントで比較
"""
from __future__ import annotations
import sys, json, time, pickle, random
from pathlib import Path
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

import pandas as pd
import numpy as np

import _iter43_rethink as R43

CACHE_PATH = Path("/Users/sanosano/projects/kimochi-max/results/_cache_alldata.pkl")
OUT_PATH = Path("/Users/sanosano/projects/kimochi-max/results/iter44_robustness.json")


def load_data():
    with open(CACHE_PATH, "rb") as f:
        return pickle.load(f)


def sample_universe(all_data, n, seed):
    """UNIVERSE_50 から n銘柄サンプリング (BTC/ETH は必ず含める)"""
    random.seed(seed)
    must_have = ["BTC/USDT", "ETH/USDT"]
    others = [s for s in all_data.keys() if s not in must_have]
    sampled = must_have + random.sample(others, min(n - 2, len(others)))
    return {k: all_data[k] for k in sampled}


def fmt_result(r):
    """1行サマリ"""
    return (f"年率 {r['avg_annual_ret']:+6.1f}% | "
            f"DD {r['max_dd']:>5.1f}% | "
            f"Sharpe {r['sharpe']:>4.2f} | "
            f"最終 ${r['final']:>10,.0f} | "
            f"清算 {r.get('n_liquidations',0):>3d} | "
            f"ﾏｲﾅｽ年 {r['negative_years']}")


def main():
    print("=" * 130)
    print("🧪 Iter44 Step3: ロバストネステスト (20パターン反復)")
    print("=" * 130)
    all_data = load_data()

    results = []

    # ━━━ パターンA: 期間シフト (7回) ━━━
    print("\n━━━━━━━━━━━━━ パターンA: 期間シフト ━━━━━━━━━━━━━")
    periods = [
        ("A1 2020-2024 (基準)",    "2020-01-01", "2024-12-31"),
        ("A2 2020-2023 (4年)",     "2020-01-01", "2023-12-31"),
        ("A3 2021-2024 (4年)",     "2021-01-01", "2024-12-31"),
        ("A4 2020H2-2024H1 (4年)", "2020-07-01", "2024-06-30"),
        ("A5 2021-2023 (3年)",     "2021-01-01", "2023-12-31"),
        ("A6 2022-2024 (3年)",     "2022-01-01", "2024-12-31"),
        ("A7 2020-2022 (クマ年含)", "2020-01-01", "2022-12-31"),
    ]
    for label, start, end in periods:
        print(f"\n📅 {label} ({start} 〜 {end})")
        # モメンタムTop3
        r_mom = R43.run_momentum(all_data, start, end, top_n=3, lookback_days=90)
        # BTCマイルド+金利
        r_btc = R43.run_btc_mild(all_data, start, end, cash_rate=0.03)
        # ハイブリッド 50/50
        r_hyb = R43.run_hybrid(all_data, start, end, btc_weight=0.5)
        print(f"   R05 モメンタム  : {fmt_result(r_mom)}")
        print(f"   R04b BTCマイルド : {fmt_result(r_btc)}")
        print(f"   R10 ハイブリッド  : {fmt_result(r_hyb)}")
        results.append({"label": label, "period": [start, end],
                        "mom": r_mom, "btc_mild": r_btc, "hybrid": r_hyb})

    # ━━━ パターンB: パラメータ変動 (6回) ━━━
    print("\n━━━━━━━━━━━━━ パターンB: パラメータ変動 ━━━━━━━━━━━━━")
    param_variants = [
        ("B1 モメンタム Top2 lookback=90",    {"top_n": 2, "lookback_days": 90}),
        ("B2 モメンタム Top4 lookback=90",    {"top_n": 4, "lookback_days": 90}),
        ("B3 モメンタム Top5 lookback=90",    {"top_n": 5, "lookback_days": 90}),
        ("B4 モメンタム Top3 lookback=60",    {"top_n": 3, "lookback_days": 60}),
        ("B5 モメンタム Top3 lookback=120",   {"top_n": 3, "lookback_days": 120}),
        ("B6 モメンタム Top3 週次リバランス",    {"top_n": 3, "lookback_days": 90, "rebalance_freq": "W"}),
    ]
    for label, params in param_variants:
        print(f"\n⚙️  {label}")
        r = R43.run_momentum(all_data, "2020-01-01", "2024-12-31", **params)
        print(f"   → {fmt_result(r)}")
        results.append({"label": label, "variant": params, "mom": r})

    # ━━━ パターンC: 銘柄サンプリング (5回) ━━━
    print("\n━━━━━━━━━━━━━ パターンC: 銘柄サンプリング ━━━━━━━━━━━━━")
    sample_variants = [
        ("C1 35銘柄ランダム seed=1", 35, 1),
        ("C2 35銘柄ランダム seed=2", 35, 2),
        ("C3 30銘柄ランダム seed=3", 30, 3),
        ("C4 25銘柄ランダム seed=4", 25, 4),
        ("C5 40銘柄ランダム seed=5", 40, 5),
    ]
    for label, n, seed in sample_variants:
        print(f"\n🎲 {label}")
        sampled_data = sample_universe(all_data, n, seed)
        r_mom = R43.run_momentum(sampled_data, "2020-01-01", "2024-12-31", top_n=3, lookback_days=90)
        r_btc = R43.run_btc_mild(sampled_data, "2020-01-01", "2024-12-31", cash_rate=0.03)
        print(f"   R05 モメンタム  : {fmt_result(r_mom)}")
        print(f"   R04b BTCマイルド : {fmt_result(r_btc)}")
        results.append({"label": label, "sample_size": n, "seed": seed,
                        "symbols_sampled": list(sampled_data.keys()),
                        "mom": r_mom, "btc_mild": r_btc})

    # ━━━ 集計 ━━━
    print(f"\n{'=' * 130}")
    print("📊 ロバストネス統計まとめ")
    print(f"{'=' * 130}")

    # モメンタムの年率分布
    mom_annuals = [r["mom"]["avg_annual_ret"] for r in results if "mom" in r]
    btc_annuals = [r["btc_mild"]["avg_annual_ret"] for r in results if "btc_mild" in r]
    hyb_annuals = [r["hybrid"]["avg_annual_ret"] for r in results if "hybrid" in r]

    def stats(arr, label):
        if not arr: return f"  {label}: データなし"
        return (f"  {label:20s}: n={len(arr):>2d} | "
                f"平均 {np.mean(arr):+6.1f}% | "
                f"中央 {np.median(arr):+6.1f}% | "
                f"最小 {np.min(arr):+6.1f}% | "
                f"最大 {np.max(arr):+6.1f}% | "
                f"標準偏差 {np.std(arr):5.1f}")
    print(stats(mom_annuals, "モメンタムTop3"))
    print(stats(btc_annuals, "BTCマイルド+金利"))
    print(stats(hyb_annuals, "ハイブリッド 50/50"))

    # 勝ちパターン率
    mom_positive = sum(1 for x in mom_annuals if x > 0)
    btc_positive = sum(1 for x in btc_annuals if x > 0)
    hyb_positive = sum(1 for x in hyb_annuals if x > 0)
    print(f"\n  プラス年率で終わった割合:")
    print(f"    モメンタムTop3 : {mom_positive}/{len(mom_annuals)} = {mom_positive/max(len(mom_annuals),1)*100:.0f}%")
    print(f"    BTCマイルド    : {btc_positive}/{len(btc_annuals)} = {btc_positive/max(len(btc_annuals),1)*100:.0f}%")
    print(f"    ハイブリッド   : {hyb_positive}/{len(hyb_annuals)} = {hyb_positive/max(len(hyb_annuals),1)*100:.0f}%")

    out = {
        "results": results,
        "summary": {
            "mom_annuals": mom_annuals,
            "btc_annuals": btc_annuals,
            "hyb_annuals": hyb_annuals,
            "mom_stats": {"n": len(mom_annuals),
                          "mean": round(float(np.mean(mom_annuals)), 2) if mom_annuals else None,
                          "median": round(float(np.median(mom_annuals)), 2) if mom_annuals else None,
                          "min": round(float(np.min(mom_annuals)), 2) if mom_annuals else None,
                          "max": round(float(np.max(mom_annuals)), 2) if mom_annuals else None,
                          "std": round(float(np.std(mom_annuals)), 2) if mom_annuals else None},
            "btc_stats": {"n": len(btc_annuals),
                          "mean": round(float(np.mean(btc_annuals)), 2) if btc_annuals else None,
                          "median": round(float(np.median(btc_annuals)), 2) if btc_annuals else None,
                          "min": round(float(np.min(btc_annuals)), 2) if btc_annuals else None,
                          "max": round(float(np.max(btc_annuals)), 2) if btc_annuals else None,
                          "std": round(float(np.std(btc_annuals)), 2) if btc_annuals else None},
            "hyb_stats": {"n": len(hyb_annuals),
                          "mean": round(float(np.mean(hyb_annuals)), 2) if hyb_annuals else None,
                          "median": round(float(np.median(hyb_annuals)), 2) if hyb_annuals else None,
                          "min": round(float(np.min(hyb_annuals)), 2) if hyb_annuals else None,
                          "max": round(float(np.max(hyb_annuals)), 2) if hyb_annuals else None,
                          "std": round(float(np.std(hyb_annuals)), 2) if hyb_annuals else None},
            "mom_positive_rate": round(mom_positive / max(len(mom_annuals), 1) * 100, 1),
            "btc_positive_rate": round(btc_positive / max(len(btc_annuals), 1) * 100, 1),
            "hyb_positive_rate": round(hyb_positive / max(len(hyb_annuals), 1) * 100, 1),
        },
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_PATH}")


if __name__ == "__main__":
    main()
