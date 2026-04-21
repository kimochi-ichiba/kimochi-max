"""
iter52b: リスク管理パラメータ緩和チューニング
==============================================
iter52 で「厳しすぎる設定」が返って不利益だったと判明。
より緩いパラメータで再探索:

  ATR multiplier: 2.0 (厳) / 3.0 (中) / 4.0 (緩)
  DD threshold:   30% (厳) / 40% (中) / 50% (緩)
  Corr threshold: 0.70 (厳) / 0.80 (中) / 0.90 (緩)

単独 + 2機能組合せ で調査。Time 機能は iter52 で効果ゼロのため除外。

目標:
  - リターン ≥ ベースラインの 90% 維持 (+1980%)
  - DD ≤ 50% に緩和
  - 両方満たす設定を採用
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

from _iter52_risk_mgmt import run_v21_backtest, UNIVERSE_REMOVE, _rebalance_key
import _iter52_risk_mgmt as M

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
RESULTS_DIR = PROJECT / "results"
CACHE_PATH = RESULTS_DIR / "_cache_alldata.pkl"
OUT_JSON = RESULTS_DIR / "iter52b_tuning.json"


def main():
    print("=" * 70)
    print("🔧 iter52b リスク管理緩和チューニング")
    print("=" * 70)

    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    universe = sorted([s for s in all_data.keys() if s not in UNIVERSE_REMOVE])

    # 実験: 単独機能の緩和設定
    tests = []

    # ATR 単独: 2.0 / 3.0 / 4.0
    for atr in [2.0, 3.0, 4.0]:
        tests.append({
            "id": f"ATR{atr}",
            "label": f"ATR×{atr}",
            "atr_mult": atr, "dd_thresh": 0.30, "corr_thresh": 0.70,
            "f1": True, "f2": False, "f3": False, "f4": False,
        })

    # DD-CB 単独: 30% / 40% / 50%
    for dd in [0.30, 0.40, 0.50]:
        tests.append({
            "id": f"DD{int(dd*100)}",
            "label": f"DD-CB閾値{int(dd*100)}%",
            "atr_mult": 2.0, "dd_thresh": dd, "corr_thresh": 0.70,
            "f1": False, "f2": True, "f3": False, "f4": False,
        })

    # Corr 単独: 0.70 / 0.80 / 0.90
    for c in [0.70, 0.80, 0.90]:
        tests.append({
            "id": f"Corr{int(c*100)}",
            "label": f"Corr閾値{c}",
            "atr_mult": 2.0, "dd_thresh": 0.30, "corr_thresh": c,
            "f1": False, "f2": False, "f3": True, "f4": False,
        })

    # ATR×4 + DD50% 組合せ (最も緩い)
    tests.append({
        "id": "ATR4_DD50",
        "label": "ATR×4 + DD-CB 50%",
        "atr_mult": 4.0, "dd_thresh": 0.50, "corr_thresh": 0.70,
        "f1": True, "f2": True, "f3": False, "f4": False,
    })
    # Corr 0.80 のみ
    tests.append({
        "id": "Corr80_only",
        "label": "Corr 0.80 のみ",
        "atr_mult": 2.0, "dd_thresh": 0.30, "corr_thresh": 0.80,
        "f1": False, "f2": False, "f3": True, "f4": False,
    })
    # ATR×3 + Corr 0.80
    tests.append({
        "id": "ATR3_Corr80",
        "label": "ATR×3 + Corr 0.80",
        "atr_mult": 3.0, "dd_thresh": 0.30, "corr_thresh": 0.80,
        "f1": True, "f2": False, "f3": True, "f4": False,
    })

    # ベースライン
    tests.insert(0, {
        "id": "BASELINE",
        "label": "ベースライン (現行v2)",
        "atr_mult": 2.0, "dd_thresh": 0.30, "corr_thresh": 0.70,
        "f1": False, "f2": False, "f3": False, "f4": False,
    })

    results = []
    t_start = time.time()
    for i, t in enumerate(tests, 1):
        # モジュール定数を一時的に書換
        M.ATR_STOP_MULT = t["atr_mult"]
        M.DD_CIRCUIT_THRESHOLD = t["dd_thresh"]
        M.CORR_THRESHOLD = t["corr_thresh"]

        print(f"[{i:2d}/{len(tests)}] {t['id']}: {t['label']}")
        t0 = time.time()
        r = run_v21_backtest(
            all_data, universe, "2020-01-01", "2024-12-31",
            top_n=3, lookback=25, rebalance_days=7,
            f1_atr_stop=t["f1"], f2_dd_circuit=t["f2"],
            f3_corr_aware=t["f3"], f4_time_exit=t["f4"],
        )
        elapsed = time.time() - t0
        r.update({**t, "elapsed": round(elapsed, 2)})
        results.append(r)
        print(f"    {elapsed:.1f}s | 最終 ${r['final']:>10,.0f} | "
              f"ret {r['total_ret']:+7.1f}% | DD {r['max_dd']:5.1f}% | "
              f"ATR停止{r['n_atr_stops']:3d} | CB発動{r['n_circuit_activations']}")

    # ベースライン
    baseline = next(r for r in results if r["id"] == "BASELINE")
    b_ret = baseline["total_ret"]
    b_dd = baseline["max_dd"]

    print("\n" + "=" * 70)
    print(f"📊 ベースライン: {b_ret:+.1f}% / DD {b_dd:.1f}%")
    print(f"判定: ret ≥ {b_ret*0.9:.1f}% AND DD ≤ 50% → 採用候補")
    print("=" * 70)

    # リスク調整後リターン (Return/DD比率) でソート
    ranked = sorted(results, key=lambda r: r["total_ret"] / max(r["max_dd"], 1),
                    reverse=True)
    print("\n🏆 リスク調整後リターン (Ret/DD) ランキング")
    print("-" * 70)
    for i, r in enumerate(ranked, 1):
        ok = (r["total_ret"] >= b_ret * 0.9 and r["max_dd"] <= 50)
        icon = "✅" if ok else "⚠️"
        ratio = r["total_ret"] / max(r["max_dd"], 1)
        dd_improve = b_dd - r["max_dd"]
        ret_loss_pct = (b_ret - r["total_ret"]) / b_ret * 100 if b_ret else 0
        print(f"  {i:2d}. {icon} {r['id']:15s} ({r['label']:25s}): "
              f"ret{r['total_ret']:+7.1f}% ({-ret_loss_pct:+5.1f}%) / "
              f"DD{r['max_dd']:5.1f}% ({-dd_improve:+5.1f}pt) / "
              f"比率 {ratio:.2f}")

    # 採用候補
    acceptable = [r for r in results
                  if r["id"] != "BASELINE"
                  and r["total_ret"] >= b_ret * 0.9
                  and r["max_dd"] <= 50]

    print("\n" + "=" * 70)
    print(f"✅ 採用候補 (ret≥{b_ret*0.9:.0f}% & DD≤50%): {len(acceptable)}件")
    for r in acceptable:
        print(f"  - {r['id']} ({r['label']}): "
              f"ret {r['total_ret']:+.1f}% / DD {r['max_dd']:.1f}%")

    if acceptable:
        best = max(acceptable, key=lambda r: r["total_ret"] / max(r["max_dd"], 1))
        print(f"\n🏅 推奨: {best['id']} ({best['label']})")
        print(f"   リターン {best['total_ret']:+.1f}% / DD {best['max_dd']:.1f}%")
        print(f"   ベースライン比: ret {(best['total_ret']-b_ret):+.1f}%pt, "
              f"DD {(best['max_dd']-b_dd):+.1f}pt")
    else:
        best = baseline
        print(f"\n⚠️ 適合する設定なし。現行 v2 維持を推奨。")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script": "_iter52b_tuning.py",
        "universe_size": len(universe),
        "patterns_tested": len(tests),
        "baseline": baseline,
        "acceptable_candidates": acceptable,
        "recommended": best,
        "all_results": results,
        "total_elapsed_sec": round(time.time() - t_start, 2),
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")


if __name__ == "__main__":
    main()
