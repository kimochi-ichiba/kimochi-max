"""
iter55: バランス型最適解の確定
================================
iter54 で発覚した問題:
- BTC50%/ACH50%/USDT0% は DD 75% (資産1/4に)
- 旧バックテストはキャッシュ配分にバグ（過小配分で DD が低く見えていた）

本検証:
- 正しい配分ロジック (iter54 の fixed) を使用
- BTC40%/ACH40%/USDT20% (USDT cushion 維持)
- ADX 15/20/25 × 重み等分/モメンタム加重 × Corr 0.80

安全第一で、DD 60% 以下を目指す。
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _iter54_comprehensive import run_bt, UNIVERSE_REMOVE

PROJECT = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT / "results"
CACHE_PATH = RESULTS_DIR / "_cache_alldata.pkl"
OUT_JSON = RESULTS_DIR / "iter55_balanced.json"


def main():
    print("=" * 70)
    print("🛡️ iter55 バランス型最適解")
    print("=" * 70)

    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    universe = sorted([s for s in all_data.keys() if s not in UNIVERSE_REMOVE])

    # 主要設定を網羅
    tests = [
        # ベースライン: 現行 v2 相当 (BTC40/ACH40/USDT20, ADX20, Corr OFF)
        {"id": "V2_CURRENT", "label": "現行v2(ADX20,CorrOFF)", "btc_w": 0.4, "ach_w": 0.4, "usdt_w": 0.2,
         "adx_min": 20, "corr_on": False, "weight_method": "equal"},

        # BTC40 シリーズ (USDT20% cushion)
        {"id": "B40_A15_C80", "label": "BTC40/ADX15/Corr80", "btc_w": 0.4, "ach_w": 0.4, "usdt_w": 0.2,
         "adx_min": 15, "corr_on": True, "weight_method": "equal"},
        {"id": "B40_A20_C80", "label": "BTC40/ADX20/Corr80", "btc_w": 0.4, "ach_w": 0.4, "usdt_w": 0.2,
         "adx_min": 20, "corr_on": True, "weight_method": "equal"},
        {"id": "B40_A15_C80_MW", "label": "BTC40/ADX15/Corr80/MomW", "btc_w": 0.4, "ach_w": 0.4, "usdt_w": 0.2,
         "adx_min": 15, "corr_on": True, "weight_method": "momentum"},
        {"id": "B40_A20_C80_MW", "label": "BTC40/ADX20/Corr80/MomW", "btc_w": 0.4, "ach_w": 0.4, "usdt_w": 0.2,
         "adx_min": 20, "corr_on": True, "weight_method": "momentum"},

        # BTC50 系 (USDT 0%, より攻撃的)
        {"id": "B50_A15_C80", "label": "BTC50/ADX15/Corr80", "btc_w": 0.5, "ach_w": 0.5, "usdt_w": 0.0,
         "adx_min": 15, "corr_on": True, "weight_method": "equal"},
        {"id": "B50_A15_C80_MW", "label": "BTC50/ADX15/Corr80/MomW", "btc_w": 0.5, "ach_w": 0.5, "usdt_w": 0.0,
         "adx_min": 15, "corr_on": True, "weight_method": "momentum"},

        # BTC30 系 (USDT40%, 保守的)
        {"id": "B30_A15_C80", "label": "BTC30/ADX15/Corr80", "btc_w": 0.3, "ach_w": 0.3, "usdt_w": 0.4,
         "adx_min": 15, "corr_on": True, "weight_method": "equal"},

        # BTC45/ACH45/USDT10 (中間)
        {"id": "B45_A15_C80_MW", "label": "BTC45/ADX15/Corr80/MomW", "btc_w": 0.45, "ach_w": 0.45, "usdt_w": 0.10,
         "adx_min": 15, "corr_on": True, "weight_method": "momentum"},
    ]

    # corr_on=False のときは Corr 0.80 の代わりに高閾値で実質無効化
    # (現実装で corr_on フラグがないので、CORR_THRESHOLD を動的に変更)
    import _iter54_comprehensive as M

    results = []
    t_start = time.time()
    for i, t in enumerate(tests, 1):
        # corr 有効/無効切替 (閾値を 0.80 or 1.1 に)
        M.CORR_THRESHOLD = 0.80 if t["corr_on"] else 1.1

        print(f"[{i}/{len(tests)}] {t['id']}: {t['label']}")
        t0 = time.time()
        r = run_bt(all_data, universe, "2020-01-01", "2024-12-31",
                   top_n=3, lookback=25, rebalance_days=7,
                   adx_min=t["adx_min"],
                   btc_w=t["btc_w"], ach_w=t["ach_w"], usdt_w=t["usdt_w"],
                   weight_method=t["weight_method"])
        elapsed = time.time() - t0
        r.update({**t, "elapsed": round(elapsed, 2)})
        results.append(r)
        print(f"   {elapsed:.1f}s | ret {r['total_ret']:+.1f}% / DD {r['max_dd']:.1f}% / 取引{r['n_trades']}")

    # 現行 v2 がベースライン
    baseline = next(r for r in results if r["id"] == "V2_CURRENT")
    b_ret = baseline["total_ret"]
    b_dd = baseline["max_dd"]

    print("\n" + "=" * 70)
    print(f"📊 ベースライン (現行 v2): ret {b_ret:+.1f}% / DD {b_dd:.1f}%")
    print("=" * 70)

    # リターン優先ランキング
    print("\n📈 リターン優先ランキング")
    print("-" * 70)
    ranked_ret = sorted(results, key=lambda r: r["total_ret"], reverse=True)
    for i, r in enumerate(ranked_ret, 1):
        d_ret = r["total_ret"] - b_ret
        d_dd = r["max_dd"] - b_dd
        print(f"  {i:2d}. {r['id']:20s}: ret {r['total_ret']:+7.0f}% ({d_ret:+6.0f}) / DD {r['max_dd']:5.1f}% ({d_dd:+4.1f})")

    # リスク調整ランキング (ret/DD)
    print("\n🛡️ リスク調整後ランキング (ret/DD)")
    print("-" * 70)
    ranked_adj = sorted(results, key=lambda r: r["total_ret"] / max(r["max_dd"], 1), reverse=True)
    for i, r in enumerate(ranked_adj, 1):
        ratio = r["total_ret"] / max(r["max_dd"], 1)
        d_ret = r["total_ret"] - b_ret
        d_dd = r["max_dd"] - b_dd
        print(f"  {i:2d}. {r['id']:20s}: ratio {ratio:6.1f} (ret {r['total_ret']:+.0f}% / DD {r['max_dd']:.1f}%)")

    # 採用候補: v2 より return 同等以上 かつ DD 同等以下
    acceptable = [r for r in results
                  if r["id"] != "V2_CURRENT"
                  and r["total_ret"] >= b_ret
                  and r["max_dd"] <= b_dd + 2.0]  # DD +2pt 以内は許容

    print("\n" + "=" * 70)
    print(f"✅ 採用候補 (v2以上のret AND DD +2pt以内): {len(acceptable)}件")
    for r in acceptable:
        print(f"  - {r['id']} ({r['label']}): ret {r['total_ret']:+.0f}% / DD {r['max_dd']:.1f}%")

    if acceptable:
        # Best = return 最大 のもの
        best = max(acceptable, key=lambda r: r["total_ret"])
        print(f"\n🏅 推奨: {best['id']} ({best['label']})")
        print(f"   ret {best['total_ret']:+.0f}% (v2比 {best['total_ret']-b_ret:+.0f}%pt)")
        print(f"   DD {best['max_dd']:.1f}% (v2比 {best['max_dd']-b_dd:+.1f}pt)")
    else:
        best = baseline
        print("\n⚠️ 採用候補なし。現行 v2 維持推奨")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(universe),
        "baseline": baseline,
        "recommended": best,
        "acceptable": acceptable,
        "all_results": results,
        "total_elapsed_sec": round(time.time() - t_start, 2),
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")


if __name__ == "__main__":
    main()
