"""
_verify_slippage_impact.py — スリッページ・手数料を加味した現実的リターン再計算

Phase 0-2: iter47_trade_limit.json の理論値に、
  - 楽観   (スリッページ0%   + 手数料0%)
  - 標準   (スリッページ0.05%+ 手数料0.10%)
  - 慎重   (スリッページ0.15%+ 手数料0.15%)
  - 悲観   (スリッページ0.30%+ 手数料0.20%)
の4シナリオを適用して、現実の運用で期待できるリターンを見積もる。

計算方法:
  取引1回あたりのコスト率 = slippage + fee
  総コスト率 = コスト率 × 総取引回数
  悲観期待リターン = 理論リターン × (1 - 総コスト率)
  （ざっくり線形近似。複利効果が大きい場合は過大評価に注意）

判定:
  理論値と悲観値の乖離が 30%超 → FAIL（戦略として採用不可）
  15%以上 30%未満 → WARN
  15%未満 → PASS

出力: results/slippage_impact.{json,html}
"""
from __future__ import annotations
import sys, json
from pathlib import Path
from datetime import datetime, timezone

PROJECT = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT / "results"
IN_JSON = RESULTS_DIR / "iter47_trade_limit.json"
OUT_JSON = RESULTS_DIR / "slippage_impact.json"
OUT_HTML = RESULTS_DIR / "slippage_impact.html"

# スリッページ+手数料シナリオ（片道あたりの%）
SCENARIOS = [
    {"name": "楽観", "label": "Optimistic", "slippage": 0.0,    "fee": 0.0,    "emoji": "🟢"},
    {"name": "標準", "label": "Standard",   "slippage": 0.0005, "fee": 0.0010, "emoji": "🟡"},
    {"name": "慎重", "label": "Conservative","slippage": 0.0015,"fee": 0.0015, "emoji": "🟠"},
    {"name": "悲観", "label": "Pessimistic","slippage": 0.0030, "fee": 0.0020, "emoji": "🔴"},
]

FAIL_THRESHOLD = 30.0  # 理論→悲観 の乖離がこれ以上なら FAIL
WARN_THRESHOLD = 15.0


def apply_cost(final_equity: float, initial: float, n_trades: int,
               cost_per_side: float) -> tuple[float, float, float]:
    """取引1回(片道)あたり cost_per_side% のコストを適用し、最終資産を再計算
    買い+売りで往復コストになるので、n_trades は片道取引回数として扱う
    """
    # 1取引あたり (1-cost) の掛け算が発生
    shrink = (1.0 - cost_per_side) ** n_trades
    adjusted_final = initial + (final_equity - initial) * shrink
    adjusted_total_ret = (adjusted_final / initial - 1.0) * 100.0
    delta_pct = (1.0 - shrink) * 100.0  # 理論値からの削られ率
    return round(adjusted_final, 2), round(adjusted_total_ret, 2), round(delta_pct, 2)


def verify_pattern(pattern: dict, initial: float) -> dict:
    final = pattern["final"]
    n_trades = pattern["n_trades"]
    total_ret_theo = pattern["total_ret"]

    scenarios_out = []
    for s in SCENARIOS:
        cost_per_side = s["slippage"] + s["fee"]
        adj_final, adj_ret, shrink_pct = apply_cost(final, initial, n_trades, cost_per_side)
        scenarios_out.append({
            "name": s["name"],
            "label": s["label"],
            "emoji": s["emoji"],
            "slippage_pct": s["slippage"] * 100,
            "fee_pct": s["fee"] * 100,
            "total_cost_per_side_pct": cost_per_side * 100,
            "adjusted_final": adj_final,
            "adjusted_total_ret_pct": adj_ret,
            "return_loss_pct": round(total_ret_theo - adj_ret, 2),
            "equity_shrinkage_pct": shrink_pct,
        })

    # 悲観シナリオ ( index 3 ) と理論値の乖離
    pessimistic = scenarios_out[3]
    deviation = total_ret_theo - pessimistic["adjusted_total_ret_pct"]
    deviation_rel = (deviation / total_ret_theo * 100) if total_ret_theo > 0 else 0

    if deviation_rel >= FAIL_THRESHOLD:
        verdict = "FAIL"
    elif deviation_rel >= WARN_THRESHOLD:
        verdict = "WARN"
    else:
        verdict = "PASS"

    return {
        "pattern_name": pattern.get("pattern_name", f"max_daily_trades={pattern['max_daily_trades']}"),
        "max_daily_trades": pattern["max_daily_trades"],
        "n_trades": n_trades,
        "theoretical_final": final,
        "theoretical_total_ret_pct": total_ret_theo,
        "scenarios": scenarios_out,
        "pessimistic_deviation_pct_points": round(deviation, 2),
        "pessimistic_deviation_relative_pct": round(deviation_rel, 2),
        "verdict": verdict,
    }


def verify() -> dict:
    if not IN_JSON.exists():
        return {
            "script": "_verify_slippage_impact.py",
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "verdict": "ERROR",
            "error": f"Input not found: {IN_JSON}",
        }

    d = json.loads(IN_JSON.read_text())
    initial = d.get("initial", 10000)
    patterns = d.get("patterns", [])

    pattern_results = [verify_pattern(p, initial) for p in patterns]

    # 全パターンのうち最悪の verdict を全体判定に
    verdicts = [p["verdict"] for p in pattern_results]
    if "FAIL" in verdicts:
        overall = "FAIL"
    elif "WARN" in verdicts:
        overall = "WARN"
    else:
        overall = "PASS"

    return {
        "script": "_verify_slippage_impact.py",
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "input_file": str(IN_JSON),
        "initial_capital": initial,
        "scenarios_def": [
            {"name": s["name"], "slippage_pct": s["slippage"] * 100,
             "fee_pct": s["fee"] * 100} for s in SCENARIOS
        ],
        "fail_threshold_pct": FAIL_THRESHOLD,
        "warn_threshold_pct": WARN_THRESHOLD,
        "patterns": pattern_results,
        "verdict": overall,
    }


def generate_html(result: dict) -> str:
    overall = result["verdict"]
    verdict_color = {"PASS": "#48bb78", "WARN": "#ed8936", "FAIL": "#e53e3e",
                     "ERROR": "#e53e3e"}[overall]
    verdict_label = {
        "PASS": "✅ スリッページ後も戦略は健全",
        "WARN": "⚠️ スリッページで15-30%損なう戦略あり",
        "FAIL": "🔴 実運用で理論値の70%以下になる戦略あり",
        "ERROR": "⚠️ 検証失敗",
    }[overall]

    if overall == "ERROR":
        return f"<html><body><h1>{verdict_label}</h1><p>{result.get('error')}</p></body></html>"

    initial = result["initial_capital"]
    pattern_sections = ""
    for p in result["patterns"]:
        v = p["verdict"]
        vc = {"PASS": "#48bb78", "WARN": "#ed8936", "FAIL": "#e53e3e"}[v]
        scen_rows = ""
        for s in p["scenarios"]:
            scen_rows += (f'<tr>'
                          f'<td>{s["emoji"]} {s["name"]}</td>'
                          f'<td style="text-align:right;">{s["slippage_pct"]:.2f}%</td>'
                          f'<td style="text-align:right;">{s["fee_pct"]:.2f}%</td>'
                          f'<td style="text-align:right;">¥{s["adjusted_final"]:,.0f}</td>'
                          f'<td style="text-align:right;">{s["adjusted_total_ret_pct"]:+.2f}%</td>'
                          f'<td style="text-align:right; color:#c0392b;">'
                          f'-{s["return_loss_pct"]:.2f}%pt</td>'
                          f'</tr>')
        pattern_sections += f'''
<div class="card">
<h2>{p["pattern_name"]}（取引 {p["n_trades"]}回）</h2>
<div style="padding:8px 14px; background:{vc}22; border-left:5px solid {vc};
            border-radius:8px; margin-bottom:14px;">
  <strong style="color:{vc};">判定: {v}</strong> — 悲観シナリオでの理論値からの乖離
  {p["pessimistic_deviation_relative_pct"]:.1f}%
</div>
<table>
<thead><tr><th>シナリオ</th><th>スリッページ</th><th>手数料</th>
<th>調整後最終</th><th>調整後リターン</th><th>理論からの差</th></tr></thead>
<tbody>{scen_rows}</tbody>
</table>
<div class="note">理論値: 最終 ${p["theoretical_final"]:,.0f} / リターン {p["theoretical_total_ret_pct"]:+.2f}%</div>
</div>
'''

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>スリッページ影響検証 | 気持ちマックス</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, "Hiragino Kaku Gothic ProN", sans-serif;
       background: linear-gradient(135deg,#667eea 0%,#764ba2 100%); min-height: 100vh;
       padding: 20px; color: #2c3e50; }}
.container {{ max-width: 1100px; margin: 0 auto; }}
h1 {{ color: white; text-align: center; font-size: 2rem; margin-bottom: 8px;
      text-shadow: 0 2px 10px rgba(0,0,0,0.2); }}
.subtitle {{ color: rgba(255,255,255,0.9); text-align: center; margin-bottom: 24px; }}
.verdict-badge {{ display: inline-block; padding: 12px 24px; border-radius: 999px;
                  font-size: 1.3rem; font-weight: 700; color: white;
                  background: {verdict_color}; margin-bottom: 20px; }}
.verdict-wrap {{ text-align: center; }}
.card {{ background: white; border-radius: 16px; padding: 24px; margin-bottom: 20px;
        box-shadow: 0 10px 40px rgba(0,0,0,0.1); }}
.card h2 {{ font-size: 1.3rem; margin-bottom: 16px; color: #4a5568;
           border-left: 5px solid #667eea; padding-left: 12px; }}
.explain {{ background: #f0f4ff; border-left: 4px solid #667eea; padding: 14px;
          border-radius: 8px; margin: 14px 0; line-height: 1.8; font-size: 0.95rem; }}
.note {{ background: #fffaf0; border-left: 4px solid #ed8936; padding: 10px 14px;
        border-radius: 6px; margin-top: 12px; font-size: 0.9rem; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 0.9rem; }}
th {{ background: #667eea; color: white; padding: 10px 8px; text-align: left; }}
td {{ padding: 8px; border-bottom: 1px solid #e2e8f0; }}
.footer {{ text-align: center; color: rgba(255,255,255,0.8); padding: 20px;
          font-size: 0.85rem; }}
</style></head><body>
<div class="container">
<h1>💸 スリッページ影響検証レポート</h1>
<p class="subtitle">理論値と現実の運用値の乖離を定量化</p>

<div class="verdict-wrap">
  <div class="verdict-badge">{verdict_label}</div>
</div>

<div class="card">
<h2>📖 この検証でわかること</h2>
<div class="explain">
バックテストの「月+30%」は、約定が理論通りに成立した場合の<strong>理論値</strong>です。
現実には、注文するたびに<strong>スリッページ</strong>（注文価格と実際の約定価格のズレ）と
<strong>手数料</strong>が発生します。<br><br>
このツールは iter47 の4パターンを4シナリオ（楽観〜悲観）で再計算し、
<strong>実運用でどこまで目減りするか</strong>を可視化します。<br><br>
<strong>判定基準:</strong><br>
・ 🟢 <strong>PASS</strong>: 悲観でも理論値の85%以上 → 健全<br>
・ 🟡 <strong>WARN</strong>: 悲観で70-85% → 注意（慎重に運用）<br>
・ 🔴 <strong>FAIL</strong>: 悲観で70%未満 → <strong>戦略見直し必要</strong>
</div>
</div>

{pattern_sections}

<div class="footer">生成日時: {result["ran_at"]}<br>🤖 気持ちマックス Phase 0 検証基盤<br>
計算式: 調整後最終 = 初期 + (理論最終 - 初期) × (1 - コスト率)^取引回数</div>
</div></body></html>"""
    return html


def main():
    print("=" * 70)
    print("💸 スリッページ影響検証 (Phase 0-2)")
    print("=" * 70)

    result = verify()
    if result["verdict"] == "ERROR":
        print(f"❌ エラー: {result.get('error')}")
        sys.exit(1)

    print(f"\n判定: {result['verdict']}")
    for p in result["patterns"]:
        print(f"\n  {p['pattern_name']} (取引{p['n_trades']}回, 判定:{p['verdict']})")
        for s in p["scenarios"]:
            print(f"    {s['emoji']} {s['name']:4s}: 最終 ${s['adjusted_final']:>12,.0f} "
                  f"({s['adjusted_total_ret_pct']:>+7.2f}%, "
                  f"理論-{s['return_loss_pct']:>6.2f}%pt)")
        print(f"    悲観乖離: {p['pessimistic_deviation_relative_pct']:.1f}%")

    OUT_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\n💾 JSON: {OUT_JSON}")

    html = generate_html(result)
    OUT_HTML.write_text(html)
    print(f"💾 HTML: {OUT_HTML}")

    if result["verdict"] == "FAIL":
        flag = PROJECT / "HALLUCINATION_DETECTED.flag"
        with open(flag, "a") as f:
            f.write(f"[{result['ran_at']}] slippage_impact FAIL\n")
        print(f"\n🚨 HALLUCINATION_DETECTED.flag 追記: {flag}")
        sys.exit(1)


if __name__ == "__main__":
    main()
