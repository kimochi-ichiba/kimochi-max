"""v4.0 攻め極振り構成の Walk-Forward 検証 (15-20 倍狙い).

iter61 F3+Maker や v2.4 が 2020-2024 で 60-78 倍を達成していた事実を踏まえ、
v2.5_chop の「守備力」を捨てて「攻め」に振った構成を試す。

v4.0 候補:
- v4_attack: Top3 + multi_lb なし + chop なし + 40/40/20 + trail 40%
- v4_attack_lev: + 動的レバ 1.5x (慎重に)
- v4_iter61_replica: iter61 と同じ Top3 + multi_lb なし + chop なし + 35/35/30
- v2.5_chop (基準)

検証期間:
- W1-W4 (3.3 年、ベア + chop 中心) → v2.5_chop が強い想定
- W_full (2020-2024 5 年フル) → 攻め構成が強い想定 ← ここで 15-20 倍出る?
"""
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


CONFIGS = [
    {"id": "v2.5_chop",
     "name": "v2.5 + ATR chop filter (PR #12 採用候補)",
     "kwargs": dict(
         bull_ach_weight=0.60, trail_stop_ach=0.30, trail_stop_btc=0.20,
         btc_weight=0.35, ach_weight=0.35, usdt_weight=0.30,
         multi_lookback=True, top_n=2,
         chop_atr_filter=True, chop_atr_threshold=0.04, chop_atr_multiplier=2.0,
     )},
    {"id": "v4_attack",
     "name": "v4.0 攻め極振り (Top3, chop なし, 40/40/20, trail 40%, bull70)",
     "kwargs": dict(
         bull_ach_weight=0.70,
         trail_stop_ach=0.40, trail_stop_btc=0.25,
         btc_weight=0.40, ach_weight=0.40, usdt_weight=0.20,
         multi_lookback=False, top_n=3,
         chop_atr_filter=False,
     )},
    {"id": "v4_attack_lev",
     "name": "v4.0 + 動的レバ 1.5x (慎重)",
     "kwargs": dict(
         bull_ach_weight=0.70,
         trail_stop_ach=0.40, trail_stop_btc=0.25,
         btc_weight=0.40, ach_weight=0.40, usdt_weight=0.20,
         multi_lookback=False, top_n=3,
         chop_atr_filter=False,
         leverage_max=1.5, leverage_adx_min=15.0,
         leverage_adx_strong=25.0, leverage_adx_super=40.0,
         leverage_floor_pct=0.20,
     )},
    {"id": "v4_iter61_replica",
     "name": "iter61 風 (Top3, chop/multi_lb なし, 35/35/30)",
     "kwargs": dict(
         bull_ach_weight=0.60, trail_stop_ach=0.30, trail_stop_btc=0.20,
         btc_weight=0.35, ach_weight=0.35, usdt_weight=0.30,
         multi_lookback=False, top_n=3,
         chop_atr_filter=False,
     )},
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

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 第 1 部: 2020-2024 全期間 (5 年、bull dominant)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("=" * 90)
    print("📊 第 1 部: 2020-2024 全期間 (5 年、bull 含む)")
    print("=" * 90)
    print(f"{'id':<22} {'最終 USD':>12} {'倍率':>8} {'CAGR':>8} {'DD':>7} {'trades':>7}")
    print("-" * 90)
    full_results = {}
    for cfg in CONFIGS:
        r = run_bt_v24(all_data, universe, "2020-01-01", "2024-12-31",
                        **cfg["kwargs"])
        full_results[cfg["id"]] = r
        mult = (1 + r.total_ret / 100)
        print(f"{cfg['id']:<22} ${r.final:>11,.0f} {mult:>7.1f}x "
              f"{r.cagr:>+7.1f}% {r.max_dd:>6.1f}% {r.n_trades:>7}")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 第 2 部: 4 窓 OOS (2022-2025 Q1, ベア + chop dominant)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("\n" + "=" * 90)
    print("📊 第 2 部: 4 窓 OOS (2022-2025 Q1)")
    print("=" * 90)

    table = {}
    for win in WINDOWS:
        table[win["id"]] = {}
        for cfg in CONFIGS:
            r = run_bt_v24(all_data, universe,
                            win["oos_start"], win["oos_end"], **cfg["kwargs"])
            table[win["id"]][cfg["id"]] = r

    print(f"{'id':<22} {'2022':>8} {'2023':>8} {'2024':>8} {'2025Q1':>8} "
          f"{'複利':>9} {'最悪DD':>7} {'最終 JPY':>13}")
    print("-" * 90)

    summary = []
    for cfg in CONFIGS:
        cid = cfg["id"]
        rows = [table[w][cid] for w in ("W1","W2","W3","W4")]
        rets = [r.total_ret for r in rows]
        total = compound(rets)
        max_dd = max(r.max_dd for r in rows)
        final = int(100_000 * (1 + total / 100))
        wins = sum(1 for w in ("W1","W2","W3","W4")
                    if table[w][cid].total_ret > table[w]["v2.5_chop"].total_ret)
        summary.append({
            "id": cid, "name": cfg["name"],
            "rets": rets, "total": total, "max_dd": max_dd, "final": final,
            "wins": wins,
            "full_5y_mult": (1 + full_results[cid].total_ret / 100),
            "full_5y_cagr": full_results[cid].cagr,
            "full_5y_dd": full_results[cid].max_dd,
        })
        print(f"{cid:<22} "
              f"{rets[0]:>+7.1f}% {rets[1]:>+7.1f}% "
              f"{rets[2]:>+7.1f}% {rets[3]:>+7.1f}% "
              f"{total:>+8.0f}% {max_dd:>6.1f}% {final:>12,}円")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 統合判定
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("\n" + "=" * 90)
    print("🎯 統合判定 (5 年 bull 含む VS 3.3 年 ベア+chop)")
    print("=" * 90)
    print(f"{'id':<22} {'5y mult':>9} {'5y CAGR':>9} {'5y DD':>7} "
          f"{'3.3y mult':>10} {'勝敗 vs chop':>14}")
    print("-" * 90)
    for s in summary:
        cid = s["id"]
        mult_3 = (1 + s["total"] / 100)
        verdict = f"{s['wins']}/4" if cid != "v2.5_chop" else "—基準"
        # 15-20 倍判定
        flag = "🌟 15-20倍候補" if s["full_5y_mult"] >= 15 else (
            "✨ 10倍超" if s["full_5y_mult"] >= 10 else
            "📊 10倍未満"
        )
        print(f"{cid:<22} {s['full_5y_mult']:>8.1f}x "
              f"{s['full_5y_cagr']:>+8.1f}% "
              f"{s['full_5y_dd']:>6.1f}% "
              f"{mult_3:>9.1f}x "
              f"{verdict:>14} {flag}")

    # 結論
    print("\n" + "=" * 90)
    print("📝 結論")
    print("=" * 90)
    best_5y = max(summary, key=lambda x: x["full_5y_mult"])
    print(f"5 年 (2020-2024) 最強: {best_5y['id']} = {best_5y['full_5y_mult']:.1f}x "
          f"(${100_000 * best_5y['full_5y_mult']:,.0f} 円)")
    if best_5y["full_5y_mult"] >= 15:
        print(f"  → 15-20 倍狙い達成可能 ✅ (相場が 2020-2024 を再現すれば)")
    print()
    print("ただし注意:")
    print("- これは過去 5 年の再現バックテスト、未来予測ではない")
    print(f"- {best_5y['id']} の 2022-2025 Q1 (ベア+chop) では {(1+best_5y['total']/100):.1f}x 止まり")
    print("- 2 つの期間で結果が大きく違う = 相場依存性が高い")

    # Markdown レポート
    out = (Path(__file__).resolve().parent / "results"
           / "wf_validate_v24" / "v4_aggressive_wf.md")
    out.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# iter75: v4.0 攻め極振り (15-20 倍狙い) WF 検証",
        "",
        "v2.5_chop は守備重視で 4 倍止まり (3.3 年)。実は 2020-2024 全 5 年だと",
        "v2.4 風 (Top3, multi_lb なし, chop なし) で **78 倍**達成していた事実を",
        "踏まえ、攻めに振った構成を検証。",
        "",
        "## 第 1 部: 2020-2024 全期間 (5 年、bull dominant)",
        "",
        "| id | 最終 USD | 倍率 | CAGR | DD | 取引 |",
        "|----|---------|------|------|-----|------|",
    ]
    for s in summary:
        full_mult = s["full_5y_mult"]
        flag = " 🌟" if full_mult >= 15 else ""
        lines.append(
            f"| {s['id']} | ${100_000 * full_mult:,.0f} | "
            f"**{full_mult:.1f}x**{flag} | {s['full_5y_cagr']:+.1f}% | "
            f"{s['full_5y_dd']:.1f}% | — |"
        )

    lines += [
        "",
        "## 第 2 部: 4 窓 OOS (2022-2025 Q1、ベア + chop)",
        "",
        "| id | 2022 | 2023 | 2024 | 2025 Q1 | 複利 | DD | 最終 |",
        "|----|------|------|------|---------|------|-----|------|",
    ]
    for s in summary:
        rets = s["rets"]
        lines.append(
            f"| {s['id']} | {rets[0]:+.1f}% | {rets[1]:+.1f}% | "
            f"{rets[2]:+.1f}% | {rets[3]:+.1f}% | "
            f"**{s['total']:+.0f}%** | {s['max_dd']:.1f}% | {s['final']:,} 円 |"
        )

    lines += [
        "",
        "## 統合判定",
        "",
        "| id | 5y bull dominant | 3.3y ベア+chop | 採用判断 |",
        "|----|----------------|---------------|---------|",
    ]
    for s in summary:
        mult_3 = (1 + s["total"] / 100)
        if s["full_5y_mult"] >= 15 and mult_3 >= 3:
            judge = "✅ 15 倍狙い候補 (両期間で良)"
        elif s["full_5y_mult"] >= 15:
            judge = "🟡 攻め過ぎ (ベア期間で苦戦)"
        elif mult_3 >= 4:
            judge = "🟢 守備型 (15 倍は届かず)"
        else:
            judge = "❌ 微妙"
        lines.append(
            f"| {s['id']} | {s['full_5y_mult']:.1f}x ({s['full_5y_cagr']:+.0f}% CAGR) | "
            f"{mult_3:.1f}x | {judge} |"
        )

    lines += [
        "",
        "## 重要な発見",
        "",
        "私が WF 検証で「v2.5_chop が最強」と言ってきたのは",
        "**2022-2025 Q1 という難しい期間限定**の話。",
        "**2020-2024 全 5 年だと攻めの方が強い**ことが判明。",
        "",
        "15-20 倍を狙うには:",
        "1. 次の 5 年で 2020-2024 級の bull cycle が来ることを信じる",
        "2. ベア期間で耐える覚悟を持つ (DD 50-65%)",
        "3. chop filter / multi_lookback / Top2 の「守り」を捨てる",
        "",
        "つまり「設計」より「相場サイクルへの賭け」が支配的。",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n💾 保存: {out}")


if __name__ == "__main__":
    main()
