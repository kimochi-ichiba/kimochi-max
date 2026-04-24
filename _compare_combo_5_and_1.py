"""⑤ multi_lookback と ①系 配分変更の組み合わせバックテスト."""
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

VARIANTS = [
    {"id": "A",         "name": "ベースライン (PR #8 既定値)",
     "kwargs": {}},
    {"id": "⑤単独",     "name": "⑤ multi_lookback のみ",
     "kwargs": {"multi_lookback": True}},
    {"id": "①b単独",    "name": "①b 配分 25/25/50 のみ",
     "kwargs": {"btc_weight": 0.25, "ach_weight": 0.25, "usdt_weight": 0.50}},
    {"id": "⑤+①a",     "name": "⑤ + 配分 30/30/40",
     "kwargs": {"multi_lookback": True,
                "btc_weight": 0.30, "ach_weight": 0.30, "usdt_weight": 0.40}},
    {"id": "⑤+①b",     "name": "⑤ + 配分 25/25/50 (狙い)",
     "kwargs": {"multi_lookback": True,
                "btc_weight": 0.25, "ach_weight": 0.25, "usdt_weight": 0.50}},
    {"id": "⑤+①c",     "name": "⑤ + 配分 20/20/60 (超保守)",
     "kwargs": {"multi_lookback": True,
                "btc_weight": 0.20, "ach_weight": 0.20, "usdt_weight": 0.60}},
    {"id": "⑤+①d",     "name": "⑤ + 配分 15/15/70 (激保守)",
     "kwargs": {"multi_lookback": True,
                "btc_weight": 0.15, "ach_weight": 0.15, "usdt_weight": 0.70}},
]


def compound(vals: list[float]) -> float:
    r = 1.0
    for v in vals:
        r *= 1 + v / 100
    return (r - 1) * 100


def main() -> None:
    all_data = load_cache()
    universe = make_universe(all_data)
    print(f"🌐 ユニバース: {len(universe)} 銘柄\n")

    results: dict[str, dict] = {}
    for v in VARIANTS:
        print(f"=== {v['id']}. {v['name']} ===")
        per_win: dict[str, float] = {}
        per_win_dd: dict[str, float] = {}
        for win in WINDOWS:
            r = run_bt_v24(
                all_data, universe,
                win["oos_start"], win["oos_end"],
                bull_ach_weight=0.60,
                trail_stop_ach=0.30,
                trail_stop_btc=0.20,
                **v["kwargs"],
            )
            per_win[win["id"]] = r.total_ret
            per_win_dd[win["id"]] = r.max_dd
        total = compound([per_win[w] for w in ("W1", "W2", "W3", "W4")])
        max_dd = max(per_win_dd.values())
        final = int(100_000 * (1 + total / 100))
        print(f"  2022 {per_win['W1']:+5.1f}% / 2023 {per_win['W2']:+5.1f}% / "
              f"2024 {per_win['W3']:+6.1f}% / 2025Q1 {per_win['W4']:+5.1f}%")
        print(f"  → 複利 {total:+.0f}% / DD {max_dd:.1f}% / 10 万 → {final:,} 円\n")
        results[v["id"]] = {
            "name": v["name"],
            "per_win": per_win, "per_win_dd": per_win_dd,
            "total": total, "max_dd": max_dd, "final": final,
        }

    # 比較表
    print("=" * 100)
    print("📊 ⑤ と ①系の組み合わせ比較 (10 万円 → 3.3 年後)")
    print("=" * 100)
    print(f"{'id':<8} {'案':<30} {'2022':>7} {'2023':>7} {'2024':>7} {'2025Q1':>7} {'複利':>7} {'DD':>6} {'最終':>13}")
    print("-" * 100)
    for v_id in [v["id"] for v in VARIANTS]:
        r = results[v_id]
        print(f"{v_id:<8} {r['name']:<30} "
              f"{r['per_win']['W1']:>+6.1f}% {r['per_win']['W2']:>+6.1f}% "
              f"{r['per_win']['W3']:>+6.1f}% {r['per_win']['W4']:>+6.1f}% "
              f"{r['total']:>+6.0f}% {r['max_dd']:>5.1f}% {r['final']:>12,}円")

    print("\n🏆 A との差分 (最終金額順)")
    print("-" * 100)
    base_final = results["A"]["final"]
    base_dd = results["A"]["max_dd"]
    base_total = results["A"]["total"]
    ranked = sorted(
        [(v_id, results[v_id]) for v_id in results],
        key=lambda x: x[1]["final"], reverse=True,
    )
    for v_id, r in ranked:
        diff_yen = r["final"] - base_final
        diff_dd = r["max_dd"] - base_dd
        mark = "⭐" if diff_yen > 0 and diff_dd <= 0 else ("👍" if diff_yen > 0 else ("" if v_id == "A" else "👎"))
        print(f"  {v_id:<8} {r['name']:<30} 最終 {r['final']:>12,}円 "
              f"(A 比 {diff_yen:+,}円 / DD {diff_dd:+.1f}pt) {mark}")

    # Markdown 保存
    lines = [
        "# iter71d: ⑤ multi_lookback + ①系 配分変更の組み合わせバックテスト",
        "",
        "前の iter71c で ⑤ と ①b が A に勝った (他は全敗) ので、組み合わせと",
        "配分の段階的な変更 (20/20/60, 15/15/70) を試す。",
        "",
        "## 結果 (10 万円を 2022-01〜2025-04 の 3.3 年運用)",
        "",
        "| id | 案 | 2022 | 2023 | 2024 | 2025 Q1 | **複利合計** | 最悪 DD | **最終金額** | A との差 |",
        "|----|----|------|------|------|---------|------------|---------|-------------|----------|",
    ]
    for v_id in [v["id"] for v in VARIANTS]:
        r = results[v_id]
        diff_yen = r["final"] - base_final
        diff_dd = r["max_dd"] - base_dd
        diff_str = "—" if v_id == "A" else f"{diff_yen:+,} 円 / DD {diff_dd:+.1f}pt"
        lines.append(
            f"| {v_id} | {r['name']} | "
            f"{r['per_win']['W1']:+.1f}% | {r['per_win']['W2']:+.1f}% | "
            f"{r['per_win']['W3']:+.1f}% | {r['per_win']['W4']:+.1f}% | "
            f"**{r['total']:+.0f}%** | {r['max_dd']:.1f}% | "
            f"**{r['final']:,} 円** | {diff_str} |"
        )

    out = Path(__file__).resolve().parent / "results" / "wf_validate_v24" / "combo_5_and_1.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n💾 保存: {out}")


if __name__ == "__main__":
    main()
