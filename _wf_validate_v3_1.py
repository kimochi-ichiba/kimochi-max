"""v3.1 攻め版 (改良レバ + スナイパー) の Walk-Forward 検証.

ベースライン: v2.5 + ATR chop filter (PR #12 で唯一勝った構成)
v3.1 候補:
  A: + 改良レバ (ADX>15 で 1.5x、>25 で 2x、>40 で 3x、清算ストップ 10%)
  B: + スナイパー (90% メイン + 10% 新規上場)
  C: 全部入り (改良レバ + スナイパー)

Usage:
    PYTHONIOENCODING=utf-8 python _wf_validate_v3_1.py
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


# ベースライン: v2.5 + ATR chop filter (採用候補)
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

# v3.1 改良レバ (3 段階 ADX、清算 10%)
V31_LEV_KW = {**V25_CHOP_KW,
              "leverage_max": 2.0,
              "leverage_adx_min": 15.0,
              "leverage_adx_strong": 25.0,
              "leverage_adx_super": 40.0,
              "leverage_floor_pct": 0.10}


def compound(vals: list[float]) -> float:
    r = 1.0
    for v in vals:
        r *= 1 + v / 100
    return (r - 1) * 100


def run_main_plus_sniper(
    all_data, universe, start, end, *,
    main_kwargs: dict,
    main_alloc: float = 0.90,
    sniper_alloc: float = 0.10,
    initial: float = 10_000.0,
):
    """メイン戦略 (90%) + スナイパー (10%) を別管理して合算."""
    # メイン (initial × 0.90)
    main_initial = initial * main_alloc
    main_kwargs2 = dict(main_kwargs)
    main_kwargs2["initial"] = main_initial
    main_r = run_bt_v24(all_data, universe, start, end, **main_kwargs2)

    # スナイパー (initial × 0.10)
    snipe_initial = initial * sniper_alloc
    snipe_r = simulate_sniper(all_data, universe, start, end,
                                initial=snipe_initial,
                                listing_days=30, tp_multiple=5.0,
                                sl_pct=0.50, alloc_per_trade_pct=0.20)

    # 合算
    final = main_r.final + snipe_r.final
    total_ret = (final - initial) / initial * 100
    return {
        "final": final,
        "total_ret": total_ret,
        "main_final": main_r.final, "main_ret": main_r.total_ret,
        "snipe_final": snipe_r.final, "snipe_ret": snipe_r.total_ret,
        "snipe_trades": snipe_r.n_trades,
        "snipe_tp": snipe_r.n_tp, "snipe_sl": snipe_r.n_sl,
        "max_dd": main_r.max_dd,  # スナイパー側 DD は別計算なので簡易にメインのみ
    }


def main() -> None:
    all_data = load_cache()
    universe = make_universe(all_data)
    print(f"🌐 ユニバース: {len(universe)} 銘柄\n")

    table: dict = {}
    for win in WINDOWS:
        table[win["id"]] = {}

        # baseline: v2.5_chop
        r = run_bt_v24(all_data, universe, win["oos_start"], win["oos_end"],
                        **V25_CHOP_KW)
        table[win["id"]]["v2.5_chop"] = {"total_ret": r.total_ret,
                                           "max_dd": r.max_dd,
                                           "final": r.final}

        # v3.1 A: 改良レバ
        r = run_bt_v24(all_data, universe, win["oos_start"], win["oos_end"],
                        **V31_LEV_KW)
        table[win["id"]]["v3.1_lev"] = {"total_ret": r.total_ret,
                                          "max_dd": r.max_dd,
                                          "final": r.final}

        # v3.1 B: スナイパー (90% chop + 10% sniper)
        sb = run_main_plus_sniper(all_data, universe,
                                    win["oos_start"], win["oos_end"],
                                    main_kwargs=V25_CHOP_KW)
        table[win["id"]]["v3.1_snipe"] = sb

        # v3.1 C: 全部入り (改良レバ + スナイパー)
        sbc = run_main_plus_sniper(all_data, universe,
                                     win["oos_start"], win["oos_end"],
                                     main_kwargs=V31_LEV_KW)
        table[win["id"]]["v3.1_full"] = sbc

        for cid in ("v2.5_chop", "v3.1_lev", "v3.1_snipe", "v3.1_full"):
            r = table[win["id"]][cid]
            print(f"  {win['id']} {cid:<14}: "
                  f"OOS {r['total_ret']:+8.1f}% / DD {r.get('max_dd', 0):5.1f}% / "
                  f"final ${r['final']:,.0f}")
        print()

    # === 集計 ===
    print("=" * 100)
    print("📊 v3.1 候補 (10 万円 → 3.3 年運用)")
    print("=" * 100)
    print(f"{'id':<14} {'2022':>8} {'2023':>8} {'2024':>8} {'2025Q1':>8} "
          f"{'複利':>9} {'最悪DD':>7} {'最終 JPY':>13} {'勝敗 vs chop':>14}")
    print("-" * 100)

    base_results = {w: table[w]["v2.5_chop"] for w in ("W1","W2","W3","W4")}
    base_total = compound([base_results[w]["total_ret"] for w in ("W1","W2","W3","W4")])
    base_dd = max(r["max_dd"] for r in base_results.values())
    base_final = int(100_000 * (1 + base_total / 100))

    summary = []
    for cid, name in [
        ("v2.5_chop",  "v2.5 + ATR chop (採用候補)"),
        ("v3.1_lev",   "v3.1A 改良レバ (ADX>15で 1.5x〜)"),
        ("v3.1_snipe", "v3.1B + スナイパー 10% (90% メイン)"),
        ("v3.1_full",  "v3.1C 全部入り (レバ + スナイパー)"),
    ]:
        rows = [table[w][cid] for w in ("W1","W2","W3","W4")]
        rets = [r["total_ret"] for r in rows]
        total = compound(rets)
        max_dd = max(r.get("max_dd", 0) for r in rows)
        final = int(100_000 * (1 + total / 100))
        wins = sum(1 for w in ("W1","W2","W3","W4")
                    if table[w][cid]["total_ret"] > base_results[w]["total_ret"])
        summary.append({"id": cid, "name": name, "rets": rets,
                        "total": total, "max_dd": max_dd,
                        "final": final, "wins": wins})
        verdict = f"🏆{wins}/4" if cid != "v2.5_chop" else "—基準"
        print(f"{cid:<14} "
              f"{rets[0]:>+7.1f}% {rets[1]:>+7.1f}% "
              f"{rets[2]:>+7.1f}% {rets[3]:>+7.1f}% "
              f"{total:>+8.0f}% {max_dd:>6.1f}% {final:>12,}円 "
              f"{verdict:>14}")

    print("\n🏆 最終金額ランキング")
    print("-" * 100)
    for s in sorted(summary, key=lambda x: x["final"], reverse=True):
        diff = s["final"] - base_final
        diff_dd = s["max_dd"] - base_dd
        mark = ""
        if s["id"] != "v2.5_chop":
            if s["wins"] >= 3 and diff > 0:
                mark = "✅ 採用候補"
            elif s["wins"] >= 2:
                mark = "🟡 部分的"
            else:
                mark = "❌ 不採用"
        print(f"  {s['id']:<14} {s['name']:<40} "
              f"最終 {s['final']:>11,}円 ({diff:+,}円 / DD {diff_dd:+.1f}pt) {mark}")

    # スナイパー詳細
    print("\n🔫 スナイパー別期間トレード詳細 (90% メイン + 10% スナイパー)")
    print("-" * 80)
    for w in ("W1","W2","W3","W4"):
        sb = table[w]["v3.1_snipe"]
        print(f"  {w}: snipe_final ${sb['snipe_final']:>7,.0f} / "
              f"trades {sb['snipe_trades']:>2} (TP={sb['snipe_tp']}, SL={sb['snipe_sl']})")

    out = (Path(__file__).resolve().parent / "results"
           / "wf_validate_v24" / "v3_1_aggressive_wf.md")
    out.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# iter73: v3.1 攻め版 (改良レバ + スナイパー) Walk-Forward 検証",
        "",
        "PR #12 で唯一勝った v2.5 + ATR chop filter をベースラインに、",
        "「攻め」の追加機能を試す:",
        "- A: 改良レバ (ADX 閾値を 15/25/40 に下げる、清算ストップ 10%)",
        "- B: スナイパー (90% メイン + 10% 新規上場 5x TP / -50% SL)",
        "- C: 全部入り (レバ + スナイパー)",
        "",
        "## 結果 (10 万円 → 3.3 年運用)",
        "",
        "| id | 案 | 2022 | 2023 | 2024 | 2025 Q1 | 複利 | DD | 最終 | vs chop |",
        "|----|----|------|------|------|---------|------|-----|------|---------|",
    ]
    for s in summary:
        rets = s["rets"]
        verdict = f"{s['wins']}/4" if s["id"] != "v2.5_chop" else "基準"
        lines.append(
            f"| {s['id']} | {s['name']} | "
            f"{rets[0]:+.1f}% | {rets[1]:+.1f}% | "
            f"{rets[2]:+.1f}% | {rets[3]:+.1f}% | "
            f"**{s['total']:+.0f}%** | {s['max_dd']:.1f}% | "
            f"**{s['final']:,} 円** | {verdict} |"
        )

    lines += [
        "",
        "## スナイパー詳細",
        "",
        "| 窓 | スナイパー final | trades | TP | SL |",
        "|----|----------------|--------|-----|-----|",
    ]
    for w in ("W1","W2","W3","W4"):
        sb = table[w]["v3.1_snipe"]
        lines.append(f"| {w} | ${sb['snipe_final']:,.0f} | "
                      f"{sb['snipe_trades']} | {sb['snipe_tp']} | {sb['snipe_sl']} |")

    lines += [
        "",
        "## 注意事項",
        "",
        "- スナイパーは **survivorship bias** あり: キャッシュには Binance で",
        "  生き残った銘柄のみ含まれる。実 memecoin (Pump.fun) は 99% rug pull",
        "- レバレッジは簡易モデル。清算ストップで equity の 10% 残高ロック",
        "- 採用判断: 4 窓中 3 勝以上で「採用候補」",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n💾 保存: {out}")


if __name__ == "__main__":
    main()
