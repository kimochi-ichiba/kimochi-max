"""A/B/C/D/C+D の比較バックテスト (PR #8 既定値で走らせる)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wf_validate_v24 import (
    WINDOWS,
    load_cache,
    make_universe,
    run_bt_v24,
)

# 比較する 5 パターン (PR #8 既定値 0.60/0.30/0.20 を基準)
VARIANTS = [
    {"id": "A", "name": "そのまま (PR #8 既定値)",
     "kwargs": {}},
    {"id": "B", "name": "少し調整 (trail_ach 0.30→0.25)",
     "kwargs": {"trail_stop_ach_override": 0.25}},
    {"id": "C", "name": "殺り合い相場対策 (chop filter ON)",
     "kwargs": {"chop_filter": True}},
    {"id": "D", "name": "半分だけ売る (partial_exit 50%)",
     "kwargs": {"partial_exit_ratio": 0.5}},
    {"id": "C+D", "name": "C と D 両方 ON",
     "kwargs": {"chop_filter": True, "partial_exit_ratio": 0.5}},
]


def compound(vals_pct: list[float]) -> float:
    r = 1.0
    for v in vals_pct:
        r *= 1 + v / 100
    return (r - 1) * 100


def main() -> None:
    all_data = load_cache()
    universe = make_universe(all_data)
    print(f"🌐 ユニバース: {len(universe)} 銘柄\n")

    results: dict[str, dict] = {}

    for v in VARIANTS:
        print(f"\n=== {v['id']}. {v['name']} ===")
        trail_ach = v["kwargs"].pop("trail_stop_ach_override", 0.30)
        per_window: dict[str, float] = {}
        per_window_dd: dict[str, float] = {}
        for win in WINDOWS:
            r = run_bt_v24(
                all_data, universe,
                win["oos_start"], win["oos_end"],
                bull_ach_weight=0.60,
                trail_stop_ach=trail_ach,
                trail_stop_btc=0.20,
                **v["kwargs"],
            )
            per_window[win["id"]] = r.total_ret
            per_window_dd[win["id"]] = r.max_dd
            print(f"  {win['id']} ({win['oos_start'][:7]}〜{win['oos_end'][:7]}): "
                  f"総リターン {r.total_ret:+6.1f}% / DD {r.max_dd:5.1f}% / "
                  f"trail_ach {r.n_trail_ach} / trail_btc {r.n_trail_btc} / bear {r.n_bear_exits}")
        total = compound([per_window[w] for w in ("W1", "W2", "W3", "W4")])
        max_dd = max(per_window_dd.values())
        final_jpy = int(100_000 * (1 + total / 100))
        print(f"  複利合計 (3.3 年): {total:+.0f}% / 最悪 DD {max_dd:.1f}%")
        print(f"  10 万円 → {final_jpy:,} 円")
        results[v["id"]] = {
            "name": v["name"],
            "per_window": per_window,
            "per_window_dd": per_window_dd,
            "total": total,
            "max_dd": max_dd,
            "final_jpy": final_jpy,
        }

    # 比較表
    print("\n" + "=" * 78)
    print("📊 比較表 (10 万円を 2022-2025 Q1 まで 3.3 年運用)")
    print("=" * 78)
    print(f"{'id':<5} {'2022':>8} {'2023':>8} {'2024':>8} {'2025Q1':>8} {'複利合計':>10} {'最悪DD':>8} {'10万→':>12}")
    print("-" * 78)
    for v_id in ["A", "B", "C", "D", "C+D"]:
        r = results[v_id]
        print(f"{v_id:<5} "
              f"{r['per_window']['W1']:>+7.1f}% "
              f"{r['per_window']['W2']:>+7.1f}% "
              f"{r['per_window']['W3']:>+7.1f}% "
              f"{r['per_window']['W4']:>+7.1f}% "
              f"{r['total']:>+9.0f}% "
              f"{r['max_dd']:>7.1f}% "
              f"{r['final_jpy']:>11,}円")

    # Markdown レポート保存
    lines = [
        "# iter71b: A/B/C/D/C+D 比較バックテスト結果",
        "",
        "PR #8 既定値 (bull_ach_weight=0.60, trail_stop_ach=0.30, trail_stop_btc=0.20) を基準に、",
        "C (chop filter) / D (partial exit 50%) / C+D の効果を実測。",
        "",
        "## 10 万円を 2022-2025 Q1 まで 3.3 年運用した結果",
        "",
        "| id | 案 | 2022 | 2023 | 2024 | 2025 Q1 | **複利合計** | 最悪 DD | **10 万円 →** |",
        "|----|-----|------|------|------|---------|------------|---------|-------------|",
    ]
    for v_id in ["A", "B", "C", "D", "C+D"]:
        r = results[v_id]
        lines.append(
            f"| {v_id} | {r['name']} | "
            f"{r['per_window']['W1']:+.1f}% | "
            f"{r['per_window']['W2']:+.1f}% | "
            f"{r['per_window']['W3']:+.1f}% | "
            f"{r['per_window']['W4']:+.1f}% | "
            f"**{r['total']:+.0f}%** | "
            f"{r['max_dd']:.1f}% | "
            f"**{r['final_jpy']:,} 円** |"
        )

    out = Path(__file__).resolve().parent / "results" / "wf_validate_v24" / "abcd_compare.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n💾 保存: {out}")


if __name__ == "__main__":
    main()
