"""v3.0 (15-20 倍狙い) の Walk-Forward 検証.

v2.5 vs v3.0 構成 (動的レバ + Top1 + ATR chop filter) を 4 窓 OOS で対決。
3/4 勝てば SIM 採用候補、ダメなら却下。

Usage:
    PYTHONIOENCODING=utf-8 python _wf_validate_v3.py
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

# v2.5 base (PR #10 と同等)
V25_KW = dict(
    bull_ach_weight=0.60,
    trail_stop_ach=0.30,
    trail_stop_btc=0.20,
    btc_weight=0.35, ach_weight=0.35, usdt_weight=0.30,
    multi_lookback=True,
    top_n=2,
)

# v3.0 配置 (各機能を単独 + 全部入り)
CONFIGS = [
    {"id": "v2.5",       "name": "v2.5 baseline (multi_lb + Top2)",
     "kwargs": V25_KW},

    {"id": "v3.0_top1",  "name": "v3.0 ② のみ: Top1 集中",
     "kwargs": {**V25_KW, "top_n": 1}},

    {"id": "v3.0_chop",  "name": "v3.0 ④ のみ: ATR chop filter",
     "kwargs": {**V25_KW, "chop_atr_filter": True,
                "chop_atr_threshold": 0.04, "chop_atr_multiplier": 2.0}},

    {"id": "v3.0_lev2",  "name": "v3.0 ① のみ: 動的レバ (2x max)",
     "kwargs": {**V25_KW, "leverage_max": 2.0}},

    {"id": "v3.0_lev3",  "name": "v3.0 ① のみ: 動的レバ (3x max)",
     "kwargs": {**V25_KW, "leverage_max": 3.0}},

    {"id": "v3.0_124",   "name": "v3.0 ①②④ 全部入り (2x レバ)",
     "kwargs": {**V25_KW, "top_n": 1, "leverage_max": 2.0,
                "chop_atr_filter": True,
                "chop_atr_threshold": 0.04, "chop_atr_multiplier": 2.0}},

    {"id": "v3.0_124_3x", "name": "v3.0 ①②④ 全部入り (3x レバ)",
     "kwargs": {**V25_KW, "top_n": 1, "leverage_max": 3.0,
                "chop_atr_filter": True,
                "chop_atr_threshold": 0.04, "chop_atr_multiplier": 2.0}},
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

    table: dict = {}
    for win in WINDOWS:
        table[win["id"]] = {}
        for cfg in CONFIGS:
            r = run_bt_v24(all_data, universe,
                            win["oos_start"], win["oos_end"], **cfg["kwargs"])
            table[win["id"]][cfg["id"]] = r
            print(f"  {win['id']} {cfg['id']:<14}: "
                  f"OOS {r.total_ret:+8.1f}% / DD {r.max_dd:5.1f}% / "
                  f"Sharpe {r.sharpe:5.2f}")
        print()

    # === 集計 ===
    print("=" * 100)
    print("📊 v3.0 候補の OOS 比較 (10 万円 → 3.3 年運用)")
    print("=" * 100)
    print(f"{'id':<14} {'2022':>8} {'2023':>8} {'2024':>8} {'2025Q1':>8} "
          f"{'複利':>9} {'最悪DD':>7} {'最終 JPY':>13} {'勝敗 vs v2.5':>14}")
    print("-" * 100)

    base_results = {}
    for win in WINDOWS:
        base_results[win["id"]] = table[win["id"]]["v2.5"]
    base_total = compound([base_results[w].total_ret
                            for w in ("W1", "W2", "W3", "W4")])
    base_dd = max(r.max_dd for r in base_results.values())
    base_final = int(100_000 * (1 + base_total / 100))

    summary = []
    for cfg in CONFIGS:
        cid = cfg["id"]
        rows = [table[w][cid] for w in ("W1", "W2", "W3", "W4")]
        total = compound([r.total_ret for r in rows])
        max_dd = max(r.max_dd for r in rows)
        final = int(100_000 * (1 + total / 100))
        # 各窓で v2.5 を上回るかカウント
        wins = sum(
            1 for w in ("W1", "W2", "W3", "W4")
            if table[w][cid].total_ret > table[w]["v2.5"].total_ret
        )
        summary.append({
            "id": cid, "name": cfg["name"],
            "rets": [r.total_ret for r in rows],
            "total": total, "max_dd": max_dd, "final": final, "wins": wins,
        })
        rets = [r.total_ret for r in rows]
        verdict = f"🏆{wins}/4" if cid != "v2.5" else "—基準"
        print(f"{cid:<14} "
              f"{rets[0]:>+7.1f}% {rets[1]:>+7.1f}% "
              f"{rets[2]:>+7.1f}% {rets[3]:>+7.1f}% "
              f"{total:>+8.0f}% {max_dd:>6.1f}% {final:>12,}円 "
              f"{verdict:>14}")

    # ランキング
    print("\n🏆 最終金額ランキング")
    print("-" * 100)
    for s in sorted(summary, key=lambda x: x["final"], reverse=True):
        diff = s["final"] - base_final
        diff_dd = s["max_dd"] - base_dd
        mark = ""
        if s["id"] != "v2.5":
            if s["wins"] >= 3 and diff > 0:
                mark = "✅ 採用候補"
            elif s["wins"] >= 2:
                mark = "🟡 部分的"
            else:
                mark = "❌ 不採用"
        print(f"  {s['id']:<14} {s['name']:<40} "
              f"最終 {s['final']:>11,}円 (A 比 {diff:+,}円 / DD {diff_dd:+.1f}pt) {mark}")

    # 結論
    full_v3 = next((s for s in summary if s["id"] == "v3.0_124"), None)
    full_v3_3x = next((s for s in summary if s["id"] == "v3.0_124_3x"), None)
    print("\n" + "=" * 100)
    print("🎯 結論")
    print("=" * 100)
    for s in (full_v3, full_v3_3x):
        if s is None: continue
        mult = s["final"] / 100_000
        print(f"  {s['id']}: 10 万円 → {s['final']:,}円 ({mult:.2f} 倍) / "
              f"v2.5 比 {s['wins']}/4 窓勝ち / 最悪 DD {s['max_dd']:.1f}%")
    if full_v3 and full_v3["wins"] >= 3:
        print("  → v3.0 ①②④ (2x レバ) は採用候補。SIM で 3 ヶ月以上検証推奨")
    elif full_v3:
        print(f"  → v3.0 ①②④ (2x レバ) は {full_v3['wins']}/4 のみ。慎重判断")

    # Markdown レポート
    out = (Path(__file__).resolve().parent / "results"
           / "wf_validate_v24" / "v3_wf_validate.md")
    out.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# iter72: v3.0 (15-20 倍狙い) Walk-Forward 検証",
        "",
        "v3.0 候補 (動的レバ + Top1 + ATR chop filter) が v2.5 を上回るかを",
        "4 窓 OOS で対決。レバレッジは BTC bullish + ADX で動的調整、",
        "funding cost 0.025%/日 控除。",
        "",
        "## 結果",
        "",
        "| id | 案 | 2022 | 2023 | 2024 | 2025 Q1 | 複利 | 最悪 DD | 最終金額 | v2.5 比勝ち |",
        "|----|----|------|------|------|---------|------|---------|---------|----------|",
    ]
    for s in summary:
        rets = s["rets"]
        verdict = f"{s['wins']}/4" if s["id"] != "v2.5" else "基準"
        lines.append(
            f"| {s['id']} | {s['name']} | "
            f"{rets[0]:+.1f}% | {rets[1]:+.1f}% | "
            f"{rets[2]:+.1f}% | {rets[3]:+.1f}% | "
            f"**{s['total']:+.0f}%** | {s['max_dd']:.1f}% | "
            f"**{s['final']:,} 円** | {verdict} |"
        )

    lines += [
        "",
        "## 判定ルール",
        "",
        "- 4/4 勝ち: ✅ 採用候補 (SIM 3 ヶ月検証推奨)",
        "- 3/4 勝ち: 🟢 ほぼ採用候補",
        "- 2/4 勝ち: 🟡 部分的、慎重判断",
        "- 0-1/4 勝ち: ❌ 不採用",
        "",
        "## 注意事項",
        "",
        "- レバレッジは簡易モデル (日次 % リターン × leverage、funding cost 控除)",
        "- 完全な perpetual contract 再現ではないが方向性を捉える",
        "- 実 SIM ではロスカット・スリッページが想定より大きくなる可能性あり",
        "- メモコイン/新規上場スナイパー (③) は別 Bot として要設計",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n💾 保存: {out}")


if __name__ == "__main__":
    main()
