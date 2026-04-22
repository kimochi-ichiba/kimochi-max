"""
iter61: F3_YEAREND + Maker指値化 の複合効果検証
====================================================
2026-04-23 に demo_runner.py/kelly_bot.py へ統合した2つの改善を、
過去5年(2020-2024)の実データでバックテストし効果を定量化する。

検証シナリオ:
  1. V22_BASE            : 現行 v2.2 (F3なし, Taker手数料 0.10%往復)
  2. V22_F3              : F3_YEAREND のみ追加 (Taker手数料)
  3. V22_MAKER_REAL      : Maker指値化のみ (手数料 0.06%往復 = 60%がMakerで40%がTaker想定)
  4. V22_F3_MAKER_REAL   : F3 + Maker現実値 (当方針の採用構成)
  5. V22_F3_MAKER_BEST   : F3 + Maker完璧 (手数料 0.04%往復 = 100%Maker約定想定)

手数料前提:
  Taker往復     = 0.10% (Binance先物 0.05% × 2)
  Maker往復     = 0.04% (Binance先物 0.02% × 2, 100%Maker約定)
  現実Maker想定 = 0.06% (60%がMakerで約定, 40%はタイムアウトで成行フォールバック)
"""
from __future__ import annotations
import sys, json, time, pickle
from pathlib import Path

sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

import _iter60_all_defenses as ITER60

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
RESULTS_DIR = PROJECT / "results"
CACHE_PATH = RESULTS_DIR / "_cache_alldata.pkl"
OUT_JSON = RESULTS_DIR / "iter61_f3_maker.json"


def run_scenario(all_data, universe, label, fee_rate, year_end_exit):
    """FEEを一時的に差し替えてシナリオ実行"""
    original_fee = ITER60.FEE
    ITER60.FEE = fee_rate
    try:
        t0 = time.time()
        r = ITER60.run_bt_v60(
            all_data, universe,
            "2020-01-01", "2024-12-31",
            year_end_exit=year_end_exit,
        )
        elapsed = time.time() - t0
    finally:
        ITER60.FEE = original_fee

    final = r.get("final", 0)
    cagr = ((final / 10000) ** (1/5) - 1) * 100 if final > 0 else 0
    r["cagr"] = round(cagr, 2)
    r["fee_rate"] = fee_rate
    r["year_end_exit"] = year_end_exit
    r["elapsed_sec"] = round(elapsed, 2)
    r["label"] = label
    return r


def main():
    print("=" * 70)
    print("🔬 iter61: F3_YEAREND + Maker指値化 複合効果検証")
    print("=" * 70)

    with open(CACHE_PATH, "rb") as f:
        all_data = pickle.load(f)
    universe = sorted([s for s in all_data.keys() if s not in ITER60.UNIVERSE_REMOVE])
    print(f"📦 universe: {len(universe)} 銘柄")
    print(f"📅 期間: 2020-01-01 〜 2024-12-31 (5年)")
    print()

    scenarios = [
        # (id, label, fee_rate, year_end_exit)
        ("V22_BASE",          "現行 v2.2 (F3なし, Taker手数料)",           0.0010, False),
        ("V22_F3",            "F3_YEAREND のみ追加 (Taker手数料)",          0.0010, True),
        ("V22_MAKER_REAL",    "Maker指値化のみ (現実想定 60%Maker約定)",   0.0006, False),
        ("V22_F3_MAKER_REAL", "F3 + Maker現実値 (採用構成)",               0.0006, True),
        ("V22_F3_MAKER_BEST", "F3 + Maker完璧 (100%Maker約定)",            0.0004, True),
    ]

    results = []
    t_start = time.time()
    for i, (sid, label, fee, ye) in enumerate(scenarios, 1):
        print(f"[{i}/{len(scenarios)}] {sid}: {label}")
        r = run_scenario(all_data, universe, label, fee, ye)
        r["id"] = sid
        yearly_min = min(r["yearly"].values()) if r.get("yearly") else 0
        print(f"   {r['elapsed_sec']:.1f}s | "
              f"最終 ${r['final']:>10,.0f} | "
              f"CAGR {r['cagr']:+6.2f}% | "
              f"DD {r['max_dd']:5.2f}% | "
              f"最悪年 {yearly_min:+6.1f}% | "
              f"YE出{r.get('n_yearend_exits',0)} Bear{r.get('n_bear_exits',0)}")
        results.append(r)

    total = time.time() - t_start
    print()
    print("=" * 70)
    print(f"✅ 完了 ({total:.1f}s)")
    print("=" * 70)

    # サマリー表
    print()
    print(f"{'ID':<22} {'最終資産':>12} {'CAGR':>8} {'最大DD':>8} {'最悪年':>8}")
    print("-" * 70)
    for r in results:
        yearly_min = min(r["yearly"].values()) if r.get("yearly") else 0
        print(f"{r['id']:<22} ${r['final']:>11,.0f} "
              f"{r['cagr']:>+7.2f}% {r['max_dd']:>7.2f}% {yearly_min:>+7.1f}%")

    # 効果比較 (ベースライン比)
    base = results[0]
    print()
    print("📊 ベースライン(V22_BASE)比の効果:")
    for r in results[1:]:
        ret_delta = r["cagr"] - base["cagr"]
        dd_delta = r["max_dd"] - base["max_dd"]
        final_ratio = r["final"] / base["final"]
        print(f"  {r['id']:<22}: CAGR {ret_delta:+6.2f}pt, DD {dd_delta:+6.2f}pt, "
              f"最終資産 x{final_ratio:.3f}")

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "universe_size": len(universe),
        "period": "2020-01-01 〜 2024-12-31",
        "scenarios": results,
        "total_elapsed_sec": round(total, 2),
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")


if __name__ == "__main__":
    main()
