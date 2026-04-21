"""iter49 最優秀設定でのスリッページ影響検証 (Phase0再適用)"""
from __future__ import annotations
import sys, json
from pathlib import Path
from datetime import datetime, timezone

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
RESULTS_DIR = PROJECT / "results"
IN_JSON = RESULTS_DIR / "iter49_rigorous.json"
OUT_JSON = RESULTS_DIR / "iter49_slippage.json"

SCENARIOS = [
    {"name": "楽観", "label": "Optimistic", "slippage": 0.0,    "fee": 0.0,    "emoji": "🟢"},
    {"name": "標準", "label": "Standard",   "slippage": 0.0005, "fee": 0.0010, "emoji": "🟡"},
    {"name": "慎重", "label": "Conservative","slippage": 0.0015,"fee": 0.0015, "emoji": "🟠"},
    {"name": "悲観", "label": "Pessimistic","slippage": 0.0030, "fee": 0.0020, "emoji": "🔴"},
]

FAIL_THRESHOLD = 30.0
WARN_THRESHOLD = 15.0


def main():
    if not IN_JSON.exists():
        print(f"❌ {IN_JSON} が無い")
        sys.exit(1)

    d = json.loads(IN_JSON.read_text())
    initial = 10000
    results = d["results"]

    # 全Aブロック対象 (true winner を slippage 後で決定)
    targets = [r for r in results if r["block"].startswith("A.")]

    out_patterns = []
    for r in targets:
        final = r["final"]
        n_trades = r["n_trades"]
        total_ret = r["total_ret"]

        scens = []
        for s in SCENARIOS:
            cost = s["slippage"] + s["fee"]
            shrink = (1.0 - cost) ** n_trades
            adj_final = initial + (final - initial) * shrink
            adj_ret = (adj_final / initial - 1.0) * 100
            scens.append({
                "name": s["name"], "emoji": s["emoji"],
                "slippage_pct": s["slippage"] * 100,
                "fee_pct": s["fee"] * 100,
                "adjusted_final": round(adj_final, 2),
                "adjusted_total_ret_pct": round(adj_ret, 2),
                "return_loss_pct": round(total_ret - adj_ret, 2),
            })
        pessimistic = scens[3]
        dev = total_ret - pessimistic["adjusted_total_ret_pct"]
        dev_rel = (dev / total_ret * 100) if total_ret > 0 else 0
        if dev_rel >= FAIL_THRESHOLD:
            verdict = "FAIL"
        elif dev_rel >= WARN_THRESHOLD:
            verdict = "WARN"
        else:
            verdict = "PASS"

        out_patterns.append({
            "id": r["id"], "block": r["block"],
            "top_n": r["top_n"], "rebalance": r["rebalance"],
            "lookback": r["lookback"],
            "n_trades": n_trades,
            "theoretical_final": final,
            "theoretical_total_ret_pct": total_ret,
            "scenarios": scens,
            "pessimistic_deviation_relative_pct": round(dev_rel, 2),
            "verdict": verdict,
        })
        print(f"[{verdict}] {r['id']}: 理論 {total_ret:+.1f}% → 悲観 "
              f"{pessimistic['adjusted_total_ret_pct']:+.1f}% (乖離 {dev_rel:.1f}%)")
        for s in scens:
            print(f"   {s['emoji']} {s['name']:4s}: ${s['adjusted_final']:>11,.0f} "
                  f"({s['adjusted_total_ret_pct']:+7.1f}%)")

    verdicts = [p["verdict"] for p in out_patterns]
    overall = "FAIL" if "FAIL" in verdicts else ("WARN" if "WARN" in verdicts else "PASS")

    # Standardシナリオでの真の勝者を決定
    std_ranking = sorted(
        out_patterns,
        key=lambda p: p["scenarios"][1]["adjusted_total_ret_pct"],  # Standard
        reverse=True,
    )
    print("\n" + "=" * 60)
    print("🏆 Standardシナリオ（市場注文現実的）での真の勝者 Top10")
    print("=" * 60)
    for i, p in enumerate(std_ranking[:10], 1):
        s = p["scenarios"][1]
        print(f"  {i:2d}. {p['id']:15s} Top{p['top_n']}/{p['rebalance']:8s}/"
              f"LB{p['lookback']:3d} | "
              f"取引{p['n_trades']:>5d} | "
              f"理論 {p['theoretical_total_ret_pct']:>+8.1f}% → "
              f"Std {s['adjusted_total_ret_pct']:>+7.1f}%")

    out = {
        "script": "_verify_iter49_slippage.py",
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "input_file": str(IN_JSON),
        "verdict": overall,
        "net_winner_standard": {
            "id": std_ranking[0]["id"],
            "top_n": std_ranking[0]["top_n"],
            "rebalance": std_ranking[0]["rebalance"],
            "lookback": std_ranking[0]["lookback"],
            "theoretical_ret": std_ranking[0]["theoretical_total_ret_pct"],
            "standard_ret": std_ranking[0]["scenarios"][1]["adjusted_total_ret_pct"],
            "n_trades": std_ranking[0]["n_trades"],
        },
        "net_ranking_top10": [
            {
                "rank": i + 1,
                "id": p["id"],
                "top_n": p["top_n"],
                "rebalance": p["rebalance"],
                "lookback": p["lookback"],
                "n_trades": p["n_trades"],
                "theoretical_ret": p["theoretical_total_ret_pct"],
                "optimistic_ret": p["scenarios"][0]["adjusted_total_ret_pct"],
                "standard_ret": p["scenarios"][1]["adjusted_total_ret_pct"],
                "conservative_ret": p["scenarios"][2]["adjusted_total_ret_pct"],
                "pessimistic_ret": p["scenarios"][3]["adjusted_total_ret_pct"],
            }
            for i, p in enumerate(std_ranking[:10])
        ],
        "patterns": out_patterns,
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\n💾 {OUT_JSON}")
    print(f"総合判定: {overall}")


if __name__ == "__main__":
    main()
