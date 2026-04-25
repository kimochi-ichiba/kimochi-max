"""v5.0「ユーザー予想最適化」厳密 WF 検証.

ユーザー予想 (BTC 5x、主要アルト 10x) に最適化された v5.0 (BTC 30/ACH 50/
USDT 20、bull_ach 0.65、multi_lookback + chop_filter) が、iter71e と同じ
4 窓 OOS で 3/4 以上勝てるか検証。

加えて:
- パラメータ感度 (配分 ±5%): ロバスト性確認
- 期間別期待値: ユーザー予想シナリオに最適か

採用基準: 4 窓中 3/4 以上で v2.5_chop を超えれば採用候補。

Usage:
    PYTHONIOENCODING=utf-8 python _wf_validate_v5_0.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wf_validate_v24 import (
    WINDOWS,
    load_cache,
    make_universe,
    run_bt_v24,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主要 5 構成
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MAIN_CONFIGS = [
    {"id": "v2.5_chop",
     "name": "v2.5_chop (PR #12 採用候補、基準)",
     "kwargs": dict(
         bull_ach_weight=0.60, trail_stop_ach=0.30, trail_stop_btc=0.20,
         btc_weight=0.35, ach_weight=0.35, usdt_weight=0.30,
         multi_lookback=True, top_n=2,
         chop_atr_filter=True, chop_atr_threshold=0.04, chop_atr_multiplier=2.0,
     )},
    {"id": "v4_attack",
     "name": "v4_attack (PR #15 攻め極振り)",
     "kwargs": dict(
         bull_ach_weight=0.70, trail_stop_ach=0.40, trail_stop_btc=0.25,
         btc_weight=0.40, ach_weight=0.40, usdt_weight=0.20,
         multi_lookback=False, top_n=3, chop_atr_filter=False,
     )},
    {"id": "v5.0_user",
     "name": "v5.0_user (BTC 30/ACH 50/USDT 20、ユーザー予想最適)",
     "kwargs": dict(
         bull_ach_weight=0.65, trail_stop_ach=0.30, trail_stop_btc=0.20,
         btc_weight=0.30, ach_weight=0.50, usdt_weight=0.20,
         multi_lookback=True, top_n=2,
         chop_atr_filter=True, chop_atr_threshold=0.04, chop_atr_multiplier=2.0,
     )},
    {"id": "v5.0_balanced",
     "name": "v5.0_balanced (35/45/20、調整版)",
     "kwargs": dict(
         bull_ach_weight=0.65, trail_stop_ach=0.30, trail_stop_btc=0.20,
         btc_weight=0.35, ach_weight=0.45, usdt_weight=0.20,
         multi_lookback=True, top_n=2,
         chop_atr_filter=True, chop_atr_threshold=0.04, chop_atr_multiplier=2.0,
     )},
    {"id": "v5.0_alt_heavy",
     "name": "v5.0_alt_heavy (25/55/20、極アルト)",
     "kwargs": dict(
         bull_ach_weight=0.65, trail_stop_ach=0.30, trail_stop_btc=0.20,
         btc_weight=0.25, ach_weight=0.55, usdt_weight=0.20,
         multi_lookback=True, top_n=2,
         chop_atr_filter=True, chop_atr_threshold=0.04, chop_atr_multiplier=2.0,
     )},
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# パラメータ感度 (3 × 3 = 9 セル)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SENSITIVITY_GRID = []
for bull_w in (0.60, 0.65, 0.70):
    for ach_w in (0.45, 0.50, 0.55):
        usdt_w = 0.20
        btc_w = 1.0 - ach_w - usdt_w
        SENSITIVITY_GRID.append({
            "id": f"sens_{int(bull_w*100)}_{int(ach_w*100)}",
            "bull_ach": bull_w,
            "ach_w": ach_w,
            "btc_w": btc_w,
            "kwargs": dict(
                bull_ach_weight=bull_w, trail_stop_ach=0.30, trail_stop_btc=0.20,
                btc_weight=btc_w, ach_weight=ach_w, usdt_weight=usdt_w,
                multi_lookback=True, top_n=2,
                chop_atr_filter=True, chop_atr_threshold=0.04, chop_atr_multiplier=2.0,
            ),
        })


def compound(vals: list[float]) -> float:
    r = 1.0
    for v in vals:
        r *= 1 + v / 100
    return (r - 1) * 100


def main() -> None:
    all_data = load_cache()
    universe = make_universe(all_data)
    print(f"🌐 ユニバース: {len(universe)} 銘柄\n")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 第 1 部: 4 窓 OOS で主要 5 構成
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("=" * 100)
    print("📊 第 1 部: 4 窓 OOS 検証 (iter71e 基準)")
    print("=" * 100)

    table_4w = {}
    for win in WINDOWS:
        table_4w[win["id"]] = {}
        for cfg in MAIN_CONFIGS:
            r = run_bt_v24(all_data, universe,
                            win["oos_start"], win["oos_end"], **cfg["kwargs"])
            table_4w[win["id"]][cfg["id"]] = r
            print(f"  {win['id']} {cfg['id']:<18}: "
                  f"OOS {r.total_ret:+8.1f}% / DD {r.max_dd:5.1f}% / "
                  f"final ${r.final:,.0f}")
        print()

    # 集計
    print("\n" + "=" * 100)
    print("📋 4 窓 OOS 比較表")
    print("=" * 100)
    print(f"{'id':<18} {'2022':>8} {'2023':>8} {'2024':>8} {'2025Q1':>8} "
          f"{'複利':>9} {'最悪DD':>7} {'勝敗':>10}")
    print("-" * 100)
    summary_4w = []
    for cfg in MAIN_CONFIGS:
        cid = cfg["id"]
        rows = [table_4w[w][cid] for w in ("W1","W2","W3","W4")]
        rets = [r.total_ret for r in rows]
        total = compound(rets)
        max_dd = max(r.max_dd for r in rows)
        final = int(100_000 * (1 + total / 100))
        wins = sum(1 for w in ("W1","W2","W3","W4")
                    if table_4w[w][cid].total_ret > table_4w[w]["v2.5_chop"].total_ret)
        summary_4w.append({
            "id": cid, "name": cfg["name"], "rets": rets,
            "total": total, "max_dd": max_dd, "final": final, "wins": wins,
        })
        verdict = f"{wins}/4" if cid != "v2.5_chop" else "—基準"
        print(f"{cid:<18} {rets[0]:>+7.1f}% {rets[1]:>+7.1f}% "
              f"{rets[2]:>+7.1f}% {rets[3]:>+7.1f}% "
              f"{total:>+8.0f}% {max_dd:>6.1f}% {verdict:>10}")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 第 2 部: 期間別期待値 (5 シナリオ)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("\n" + "=" * 100)
    print("📊 第 2 部: 期間別期待値 (5 シナリオ)")
    print("=" * 100)

    scenarios = [
        ("full_2020_2024", "2020-01-01", "2024-12-31", "5 年フル (極端 bull)"),
        ("2024_only",       "2024-01-01", "2024-12-31", "2024 単独 (穏やか bull、ユーザー予想 近似)"),
        ("2023_2024",       "2023-01-01", "2024-12-31", "2 年中庸 bull"),
        ("2022_only",       "2022-01-01", "2022-12-31", "ベア年単独"),
        ("hard_3_3yr",      "2022-01-01", "2025-04-19", "ベア + chop (3.3 年)"),
    ]
    table_scenarios = {}
    for sid, start, end, label in scenarios:
        table_scenarios[sid] = {}
        print(f"\n--- {label} ({start} 〜 {end}) ---")
        for cfg in MAIN_CONFIGS:
            r = run_bt_v24(all_data, universe, start, end, **cfg["kwargs"])
            table_scenarios[sid][cfg["id"]] = r
            mult = (1 + r.total_ret / 100)
            print(f"  {cfg['id']:<18}: ${r.final:>10,.0f} ({mult:>5.1f}x) "
                  f"CAGR {r.cagr:>+6.1f}% DD {r.max_dd:>5.1f}%")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 第 3 部: パラメータ感度 (9 セル × 4 窓 = 36 BT)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("\n" + "=" * 100)
    print("📊 第 3 部: パラメータ感度 (bull_ach × ach_weight)")
    print("=" * 100)

    table_sens = {}
    for cfg in SENSITIVITY_GRID:
        sid = cfg["id"]
        rows = []
        for win in WINDOWS:
            r = run_bt_v24(all_data, universe,
                            win["oos_start"], win["oos_end"], **cfg["kwargs"])
            rows.append(r.total_ret)
        total = compound(rows)
        final = int(100_000 * (1 + total / 100))
        table_sens[sid] = {
            "bull_ach": cfg["bull_ach"], "ach_w": cfg["ach_w"],
            "btc_w": cfg["btc_w"], "total": total, "final": final,
        }

    print(f"{'bull_ach':>10} | {'ach=0.45':>14} {'ach=0.50':>14} {'ach=0.55':>14}")
    print("-" * 60)
    for bull_w in (0.60, 0.65, 0.70):
        cells = []
        for ach_w in (0.45, 0.50, 0.55):
            sid = f"sens_{int(bull_w*100)}_{int(ach_w*100)}"
            d = table_sens[sid]
            cells.append(f"{d['final']:>13,}円")
        print(f"  {bull_w:.2f}    | {cells[0]:>14} {cells[1]:>14} {cells[2]:>14}")

    sens_finals = [d["final"] for d in table_sens.values()]
    sens_min = min(sens_finals)
    sens_max = max(sens_finals)
    sens_range = (sens_max - sens_min) / sens_min * 100 if sens_min else 0
    print(f"\n感度幅: ${sens_min:,} 〜 ${sens_max:,} (差 {sens_range:.1f}%)")
    if sens_range < 30:
        print("→ ✅ ロバスト (差 30% 未満)")
    elif sens_range < 60:
        print("→ 🟡 やや過敏 (差 30-60%)")
    else:
        print("→ 🔴 過敏 (差 60% 超、過学習疑い)")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 第 4 部: 採用判定
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print("\n" + "=" * 100)
    print("🎯 第 4 部: 採用判定")
    print("=" * 100)

    for s in summary_4w:
        if s["id"] == "v2.5_chop":
            continue
        wins = s["wins"]
        if wins == 4:
            verdict = "✅ 採用候補 (4/4 勝ち、90 点級)"
        elif wins == 3:
            verdict = "🟢 ほぼ採用 (3/4 勝ち、75 点級)"
        elif wins == 2:
            verdict = "🟡 条件付き (2/4 のみ、SIM 観察必須)"
        else:
            verdict = "❌ 不採用 (1/4 以下)"
        print(f"  {s['id']:<18} ({s['name']})")
        print(f"    {wins}/4 勝ち、複利 {s['total']:+.0f}%、最悪 DD {s['max_dd']:.1f}%")
        print(f"    → {verdict}")
        print()

    # 期待値計算 (ユーザー予想シナリオ重み付き)
    print("\n" + "=" * 100)
    print("🎲 ユーザー予想シナリオでの期待値 (5x BTC, 10x alt 想定)")
    print("=" * 100)
    weights = {
        "full_2020_2024": 0.10,    # 極端 bull の確率
        "2023_2024": 0.30,         # 中庸 bull
        "2024_only": 0.20,         # 穏やか bull
        "2022_only": 0.10,         # ベア
        "hard_3_3yr": 0.30,        # ベア + chop
    }
    # 簡易: 各シナリオの倍率を確率重み付け平均
    print(f"{'config':<18} {'極端bull':>10} {'中庸bull':>10} {'穏やかbull':>12} "
          f"{'ベア':>8} {'難相場':>10} {'期待値':>10}")
    print("-" * 100)
    for cfg in MAIN_CONFIGS:
        cid = cfg["id"]
        ev = 0
        cells = []
        for sid, w in weights.items():
            r = table_scenarios[sid][cid]
            mult = (1 + r.total_ret / 100)
            cells.append(f"{mult:>9.1f}x")
            ev += mult * w
        print(f"  {cid:<18} {cells[0]:>10} {cells[1]:>10} {cells[2]:>12} "
              f"{cells[3]:>8} {cells[4]:>10} {ev:>9.1f}x")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # JSON / Markdown 出力
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    out_dir = Path(__file__).resolve().parent / "results"
    out_md = out_dir / "wf_validate_v24" / "v5_robust_wf.md"
    out_json = out_dir / "v5_robust_wf.json"
    out_md.parent.mkdir(parents=True, exist_ok=True)

    # JSON
    json_data = {
        "title": "v5.0 厳密 WF 検証",
        "configs": {c["id"]: c["name"] for c in MAIN_CONFIGS},
        "wf_4_windows": {
            w_id: {cid: {"total_ret": r.total_ret, "max_dd": r.max_dd,
                          "final": r.final, "cagr": r.cagr,
                          "n_trades": r.n_trades}
                    for cid, r in cells.items()}
            for w_id, cells in table_4w.items()
        },
        "scenarios": {
            sid: {cid: {"total_ret": r.total_ret, "max_dd": r.max_dd,
                         "final": r.final, "cagr": r.cagr,
                         "label": next(s[3] for s in scenarios if s[0] == sid)}
                   for cid, r in cells.items()}
            for sid, cells in table_scenarios.items()
        },
        "sensitivity": table_sens,
        "summary": summary_4w,
    }
    out_json.write_text(json.dumps(json_data, indent=2, ensure_ascii=False, default=str),
                         encoding="utf-8")

    # Markdown
    lines = [
        "# iter76: v5.0 厳密 Walk-Forward 検証",
        "",
        "ユーザー予想 (BTC 5x、主要アルト 10x) に最適化した v5.0 を、",
        "iter71e と同じ 4 窓 OOS + パラメータ感度 + 期間別期待値で検証。",
        "",
        "## 第 1 部: 4 窓 OOS 比較",
        "",
        "| id | 案 | 2022 | 2023 | 2024 | 2025 Q1 | 複利 | DD | 勝敗 |",
        "|----|----|------|------|------|---------|------|-----|------|",
    ]
    for s in summary_4w:
        rets = s["rets"]
        verdict = f"{s['wins']}/4" if s["id"] != "v2.5_chop" else "基準"
        lines.append(
            f"| {s['id']} | {s['name']} | "
            f"{rets[0]:+.1f}% | {rets[1]:+.1f}% | "
            f"{rets[2]:+.1f}% | {rets[3]:+.1f}% | "
            f"**{s['total']:+.0f}%** | {s['max_dd']:.1f}% | {verdict} |"
        )

    lines += [
        "",
        "## 第 2 部: 期間別期待値",
        "",
        "| config | 5 年 bull | 中庸 bull | 穏やか bull | ベア | 難相場 |",
        "|--------|-----------|-----------|------------|------|--------|",
    ]
    for cfg in MAIN_CONFIGS:
        cid = cfg["id"]
        cells = []
        for sid, w in weights.items():
            r = table_scenarios[sid][cid]
            mult = (1 + r.total_ret / 100)
            cells.append(f"{mult:.1f}x")
        lines.append(f"| {cid} | {cells[0]} | {cells[1]} | {cells[2]} | "
                      f"{cells[3]} | {cells[4]} |")

    lines += [
        "",
        "## 第 3 部: パラメータ感度",
        "",
        f"v5.0_user 周辺 (bull_ach: 0.60-0.70, ach_w: 0.45-0.55) の 9 セル:",
        f"- 最終金額の幅: ${sens_min:,} 〜 ${sens_max:,}",
        f"- 差: {sens_range:.1f}%",
    ]
    if sens_range < 30:
        lines.append("- 判定: ✅ ロバスト")
    elif sens_range < 60:
        lines.append("- 判定: 🟡 やや過敏")
    else:
        lines.append("- 判定: 🔴 過敏 (過学習疑い)")

    lines += [
        "",
        "## 採用判定",
        "",
    ]
    for s in summary_4w:
        if s["id"] == "v2.5_chop":
            continue
        wins = s["wins"]
        if wins == 4:
            verdict = "✅ 採用候補"
        elif wins == 3:
            verdict = "🟢 ほぼ採用"
        elif wins == 2:
            verdict = "🟡 条件付き"
        else:
            verdict = "❌ 不採用"
        lines.append(f"- **{s['id']}** ({wins}/4): {verdict}")

    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n💾 保存: {out_md}")
    print(f"💾 保存: {out_json}")


if __name__ == "__main__":
    main()
