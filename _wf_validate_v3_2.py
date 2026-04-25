"""v3.2 二段構成 (75% main + 25% sniper) の Walk-Forward 検証.

選択肢 B 案: 法人化 + メイン Bot (v2.5_chop) と sniper Bot を別管理。
資金は最初に 75/25 で分け、以後は独立運用 (相互の資金移動なし)。

3 つの sniper 設定で比較:
- A: default (5% × 最大 5 並列、5x TP / -50% SL)
- B: diversified (2.5% × 最大 10 並列、より分散)
- C: aggressive (8% × 最大 4 並列、上場 14 日内のみ、3x TP)

Usage:
    PYTHONIOENCODING=utf-8 python _wf_validate_v3_2.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sniper_backtest import simulate_sniper
from wf_validate_v24 import (
    WINDOWS,
    load_cache,
    make_universe,
    run_bt_v24,
)


V25_CHOP_KW = dict(
    bull_ach_weight=0.60,
    trail_stop_ach=0.30,
    trail_stop_btc=0.20,
    btc_weight=0.35, ach_weight=0.35, usdt_weight=0.30,
    multi_lookback=True,
    top_n=2,
    chop_atr_filter=True,
    chop_atr_threshold=0.04,
    chop_atr_multiplier=2.0,
)

# 3 つの sniper 設定
SNIPER_CONFIGS = [
    {"id": "default",
     "name": "標準 (5% × 5 並列、5x TP / -50% SL、上場 30 日内)",
     "kwargs": dict(listing_days=30, tp_multiple=5.0, sl_pct=0.50,
                     alloc_per_trade_pct=0.20, max_concurrent=5,
                     timeout_days=180)},
    {"id": "diversified",
     "name": "分散 (2.5% × 10 並列、5x TP / -50% SL)",
     "kwargs": dict(listing_days=30, tp_multiple=5.0, sl_pct=0.50,
                     alloc_per_trade_pct=0.10, max_concurrent=10,
                     timeout_days=180)},
    {"id": "aggressive",
     "name": "速攻 (8% × 4 並列、上場 14 日内、3x TP / -40% SL)",
     "kwargs": dict(listing_days=14, tp_multiple=3.0, sl_pct=0.40,
                     alloc_per_trade_pct=0.32, max_concurrent=4,
                     timeout_days=90)},
]


def compound(vals: list[float]) -> float:
    r = 1.0
    for v in vals:
        r *= 1 + v / 100
    return (r - 1) * 100


def two_tier_run(
    all_data, universe, start, end,
    *,
    main_alloc: float = 0.75,
    sniper_kwargs: dict,
    initial: float = 10_000.0,
):
    """75% メイン + 25% スナイパーを独立運用、合算."""
    main_initial = initial * main_alloc
    sniper_initial = initial * (1.0 - main_alloc)

    main_kwargs = dict(V25_CHOP_KW)
    main_kwargs["initial"] = main_initial
    main_r = run_bt_v24(all_data, universe, start, end, **main_kwargs)

    snipe_r = simulate_sniper(all_data, universe, start, end,
                                initial=sniper_initial, **sniper_kwargs)

    final = main_r.final + snipe_r.final
    total_ret = (final - initial) / initial * 100
    return {
        "final": final,
        "total_ret": total_ret,
        "main_final": main_r.final,
        "main_ret": main_r.total_ret,
        "main_dd": main_r.max_dd,
        "snipe_final": snipe_r.final,
        "snipe_ret": snipe_r.total_ret,
        "snipe_trades": snipe_r.n_trades,
        "snipe_tp": snipe_r.n_tp,
        "snipe_sl": snipe_r.n_sl,
        "snipe_win_rate": snipe_r.win_rate,
        "snipe_avg_mult": snipe_r.avg_multiple,
    }


def main() -> None:
    all_data = load_cache()
    universe = make_universe(all_data)
    print(f"🌐 ユニバース: {len(universe)} 銘柄\n")

    table: dict = {}
    for win in WINDOWS:
        table[win["id"]] = {}

        # baseline: 100% v2.5_chop (二段構成しない)
        kw = dict(V25_CHOP_KW)
        kw["initial"] = 10_000.0
        r = run_bt_v24(all_data, universe,
                        win["oos_start"], win["oos_end"], **kw)
        table[win["id"]]["100%_chop"] = {
            "final": r.final, "total_ret": r.total_ret,
            "main_dd": r.max_dd, "snipe_final": 0,
            "snipe_trades": 0, "snipe_tp": 0, "snipe_sl": 0,
        }

        # 二段: 75% main + 25% sniper × 3 設定
        for cfg in SNIPER_CONFIGS:
            cid = f"75_25_{cfg['id']}"
            r = two_tier_run(all_data, universe,
                              win["oos_start"], win["oos_end"],
                              sniper_kwargs=cfg["kwargs"])
            table[win["id"]][cid] = r

        # 各窓の結果をプリント
        for cid in ("100%_chop", "75_25_default",
                     "75_25_diversified", "75_25_aggressive"):
            r = table[win["id"]][cid]
            print(f"  {win['id']} {cid:<22}: "
                  f"final ${r['final']:>8,.0f} "
                  f"(main ${r.get('main_final', r['final']):,.0f} + "
                  f"snipe ${r['snipe_final']:,.0f}) "
                  f"trades={r.get('snipe_trades', 0)} "
                  f"TP={r.get('snipe_tp', 0)}")
        print()

    # 集計
    print("=" * 110)
    print("📊 二段構成 vs 100% v2.5_chop (10 万円 → 3.3 年運用)")
    print("=" * 110)
    print(f"{'id':<22} {'2022':>8} {'2023':>8} {'2024':>8} {'2025Q1':>8} "
          f"{'複利':>9} {'最終 JPY':>13} {'snipe 寄与':>12} {'勝敗':>8}")
    print("-" * 110)

    base = table  # alias
    base_total = compound([base[w]["100%_chop"]["total_ret"]
                            for w in ("W1","W2","W3","W4")])
    base_final = int(100_000 * (1 + base_total / 100))

    summary = []
    for cid, name in [
        ("100%_chop",          "100% v2.5_chop (PR #12 採用候補)"),
        ("75_25_default",      "75/25: 標準 (5% × 5 並列)"),
        ("75_25_diversified",  "75/25: 分散 (2.5% × 10 並列)"),
        ("75_25_aggressive",   "75/25: 速攻 (上場 14 日 3x TP)"),
    ]:
        rows = [base[w][cid] for w in ("W1","W2","W3","W4")]
        rets = [r["total_ret"] for r in rows]
        total = compound(rets)
        final = int(100_000 * (1 + total / 100))
        # snipe 部分の合計利益
        snipe_total_pl = sum(
            (r.get("snipe_final", 0) - 2500.0) for r in rows
        )  # 2500 円ずつ初期想定 (10 万 × 25% = 2.5 万 = $2,500 起点)
        wins = sum(1 for w in ("W1","W2","W3","W4")
                    if base[w][cid]["total_ret"]
                       > base[w]["100%_chop"]["total_ret"])
        summary.append({
            "id": cid, "name": name,
            "rets": rets, "total": total, "final": final,
            "snipe_total_pl": snipe_total_pl,
            "wins": wins,
        })
        verdict = f"🏆{wins}/4" if cid != "100%_chop" else "—基準"
        snipe_str = f"+${snipe_total_pl:,.0f}" if snipe_total_pl else "$0"
        print(f"{cid:<22} "
              f"{rets[0]:>+7.1f}% {rets[1]:>+7.1f}% "
              f"{rets[2]:>+7.1f}% {rets[3]:>+7.1f}% "
              f"{total:>+8.0f}% {final:>12,}円 "
              f"{snipe_str:>12} "
              f"{verdict:>8}")

    print("\n🏆 最終金額ランキング")
    print("-" * 110)
    for s in sorted(summary, key=lambda x: x["final"], reverse=True):
        diff = s["final"] - base_final
        mark = ""
        if s["id"] != "100%_chop":
            if s["wins"] >= 3 and diff > 0:
                mark = "✅ 採用候補"
            elif s["wins"] >= 2 and diff > 0:
                mark = "🟢 ほぼ良"
            elif diff > 0:
                mark = "🟡 微増"
            else:
                mark = "❌ 悪化"
        print(f"  {s['id']:<22} {s['name']:<45} "
              f"最終 {s['final']:>11,}円 ({diff:+,}円) {mark}")

    # 詳細: スナイパー収支
    print("\n🔫 スナイパー期間別収支 ($2,500 起点 = 25% × $10,000)")
    print("-" * 110)
    print(f"{'config':<22} {'W1':>15} {'W2':>15} {'W3':>15} {'W4':>15}")
    for cfg in SNIPER_CONFIGS:
        cid = f"75_25_{cfg['id']}"
        cells = []
        for w in ("W1","W2","W3","W4"):
            r = base[w][cid]
            sf = r["snipe_final"]
            tp = r["snipe_tp"]
            cells.append(f"${sf:,.0f} (TP {tp})")
        print(f"  {cfg['id']:<22} {cells[0]:>15} {cells[1]:>15} {cells[2]:>15} {cells[3]:>15}")

    # Markdown レポート
    out = (Path(__file__).resolve().parent / "results"
           / "wf_validate_v24" / "v3_2_two_tier_wf.md")
    out.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# iter74: v3.2 二段構成 (75% メイン + 25% スナイパー) WF 検証",
        "",
        "選択肢 B: 法人化 + 二段構成。資金を最初に 75/25 で分割し、",
        "メイン (v2.5_chop) と sniper を独立運用。3 つの sniper 設定で比較。",
        "",
        "## 結果 (10 万円 → 3.3 年)",
        "",
        "| id | 案 | 2022 | 2023 | 2024 | 2025 Q1 | 複利 | 最終金額 | snipe 寄与 | vs 100% |",
        "|----|----|------|------|------|---------|------|---------|----------|---------|",
    ]
    for s in summary:
        rets = s["rets"]
        verdict = f"{s['wins']}/4" if s["id"] != "100%_chop" else "基準"
        snipe_str = f"+${s['snipe_total_pl']:,.0f}" if s["snipe_total_pl"] else "—"
        lines.append(
            f"| {s['id']} | {s['name']} | "
            f"{rets[0]:+.1f}% | {rets[1]:+.1f}% | "
            f"{rets[2]:+.1f}% | {rets[3]:+.1f}% | "
            f"**{s['total']:+.0f}%** | **{s['final']:,} 円** | "
            f"{snipe_str} | {verdict} |"
        )

    lines += [
        "",
        "## スナイパー期間別収支 ($2,500 起点 = 25% × $10,000)",
        "",
        "| config | W1 (2022) | W2 (2023) | W3 (2024) | W4 (2025Q1) |",
        "|--------|-----------|-----------|-----------|-------------|",
    ]
    for cfg in SNIPER_CONFIGS:
        cid = f"75_25_{cfg['id']}"
        cells = []
        for w in ("W1","W2","W3","W4"):
            r = base[w][cid]
            cells.append(f"${r['snipe_final']:,.0f} (TP {r['snipe_tp']}, SL {r['snipe_sl']})")
        lines.append(f"| {cfg['id']} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} |")

    lines += [
        "",
        "## 注意事項",
        "",
        "- スナイパーは **survivorship bias** あり: キャッシュには Binance で",
        "  生き残った銘柄のみ含まれる。実 memecoin は遥かに過酷",
        "- 二段構成: 資金は最初に分割、以後は独立 (相互移動なし)",
        "  → スナイパーが全損してもメインは無傷",
        "- 100% メインに対して 25% を犠牲にする価値があるか、上記表で判断",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n💾 保存: {out}")


if __name__ == "__main__":
    main()
