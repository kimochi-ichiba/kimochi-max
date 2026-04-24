"""①〜⑤ の改善案を A (PR #8 既定値) と比較するバックテスト."""
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

# PR #8 既定値 (bull=0.60, trail_ach=0.30, trail_btc=0.20) を基準に、
# 各改善を単独 ON にした結果を見る
VARIANTS = [
    {"id": "A",   "name": "ベースライン (PR #8 既定値)",
     "kwargs": {}},
    {"id": "①a", "name": "配分 30/30/40 (USDT +10%)",
     "kwargs": {"btc_weight": 0.30, "ach_weight": 0.30, "usdt_weight": 0.40}},
    {"id": "①b", "name": "配分 25/25/50 (USDT +20%)",
     "kwargs": {"btc_weight": 0.25, "ach_weight": 0.25, "usdt_weight": 0.50}},
    {"id": "②a", "name": "Top 3 銘柄",
     "kwargs": {"top_n": 3}},
    {"id": "②b", "name": "Top 5 銘柄",
     "kwargs": {"top_n": 5}},
    {"id": "③a", "name": "リバランス 14 日",
     "kwargs": {"rebalance_days": 14}},
    {"id": "③b", "name": "リバランス 30 日",
     "kwargs": {"rebalance_days": 30}},
    {"id": "④a", "name": "Bear バッファ 3%",
     "kwargs": {"bear_ema_buffer": 0.03}},
    {"id": "④b", "name": "Bear バッファ 5%",
     "kwargs": {"bear_ema_buffer": 0.05}},
    {"id": "⑤",  "name": "multi_lookback (25+45+90 日)",
     "kwargs": {"multi_lookback": True}},
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
        print(f"\n=== {v['id']}. {v['name']} ===")
        per_win: dict[str, float] = {}
        per_win_dd: dict[str, float] = {}
        trail_ach_total = 0
        trail_btc_total = 0
        bear_total = 0
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
            trail_ach_total += r.n_trail_ach
            trail_btc_total += r.n_trail_btc
            bear_total += r.n_bear_exits
            print(f"  {win['id']} ({win['oos_start'][:7]}〜{win['oos_end'][:7]}): "
                  f"総リターン {r.total_ret:+7.1f}% / DD {r.max_dd:5.1f}%")
        total = compound([per_win[w] for w in ("W1", "W2", "W3", "W4")])
        max_dd = max(per_win_dd.values())
        final = int(100_000 * (1 + total / 100))
        print(f"  → 複利合計 {total:+.0f}% / 最悪 DD {max_dd:.1f}% / 10 万 → {final:,} 円")
        print(f"     (trail_ach 計 {trail_ach_total} / trail_btc {trail_btc_total} / bear 退避 {bear_total})")
        results[v["id"]] = {
            "name": v["name"],
            "per_win": per_win,
            "per_win_dd": per_win_dd,
            "total": total,
            "max_dd": max_dd,
            "final": final,
        }

    # コンソール比較表
    print("\n" + "=" * 100)
    print("📊 ①〜⑤ 改善案の比較 (10 万円を 2022-01〜2025-04 の 3.3 年運用)")
    print("=" * 100)
    print(f"{'id':<5} {'案':<35} {'2022':>7} {'2023':>7} {'2024':>7} {'2025Q1':>7} {'複利':>8} {'DD':>7} {'最終 JPY':>13}")
    print("-" * 100)
    base_total = results["A"]["total"]
    base_final = results["A"]["final"]
    base_dd = results["A"]["max_dd"]
    for v_id in [v["id"] for v in VARIANTS]:
        r = results[v_id]
        print(f"{v_id:<5} {r['name']:<35} "
              f"{r['per_win']['W1']:>+6.1f}% "
              f"{r['per_win']['W2']:>+6.1f}% "
              f"{r['per_win']['W3']:>+6.1f}% "
              f"{r['per_win']['W4']:>+6.1f}% "
              f"{r['total']:>+7.0f}% "
              f"{r['max_dd']:>6.1f}% "
              f"{r['final']:>12,}円")

    # A との差分ランキング
    print("\n" + "=" * 100)
    print("🏆 A との差分 (最終金額順)")
    print("=" * 100)
    ranked = sorted(
        [(v_id, results[v_id]) for v_id in results],
        key=lambda x: x[1]["final"], reverse=True,
    )
    for v_id, r in ranked:
        diff_yen = r["final"] - base_final
        diff_pt = r["total"] - base_total
        diff_dd = r["max_dd"] - base_dd
        mark = "⭐" if diff_yen > 0 and diff_dd <= 0 else ("👍" if diff_yen > 0 else ("" if v_id == "A" else "👎"))
        print(f"  {v_id:<5} {r['name']:<35} 最終 {r['final']:>12,}円 (A 比 {diff_yen:+,}円 / リターン {diff_pt:+.0f}pt / DD {diff_dd:+.1f}pt) {mark}")

    # Markdown レポート
    lines = [
        "# iter71c: ①〜⑤ 改善案の比較バックテスト結果",
        "",
        "PR #8 既定値 (bull_ach_weight=0.60, trail_stop_ach=0.30, trail_stop_btc=0.20) を基準に、",
        "①〜⑤ の各改善を単独 ON にして 4 窓で比較。",
        "",
        "## 10 万円を 2022-01〜2025-04 の 3.3 年運用した結果",
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
            f"{r['per_win']['W1']:+.1f}% | "
            f"{r['per_win']['W2']:+.1f}% | "
            f"{r['per_win']['W3']:+.1f}% | "
            f"{r['per_win']['W4']:+.1f}% | "
            f"**{r['total']:+.0f}%** | "
            f"{r['max_dd']:.1f}% | "
            f"**{r['final']:,} 円** | "
            f"{diff_str} |"
        )

    lines += [
        "",
        "## 判定基準",
        "",
        "- ⭐ 最終金額↑ かつ DD↓ (文句なしの改善)",
        "- 👍 最終金額↑ (DD は悪化でも可)",
        "- 👎 最終金額↓ (悪化)",
        "",
    ]

    out = Path(__file__).resolve().parent / "results" / "wf_validate_v24" / "12345_compare.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n💾 保存: {out}")


if __name__ == "__main__":
    main()
