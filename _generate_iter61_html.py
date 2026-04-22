"""
iter61 HTML レポート生成
=============================
F3_YEAREND + Maker指値化 の複合効果をグラフ付きHTMLで可視化。
"""
from __future__ import annotations
import json
from pathlib import Path

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
RESULTS_DIR = PROJECT / "results"
IN_JSON = RESULTS_DIR / "iter61_f3_maker.json"
OUT_HTML = RESULTS_DIR / "iter61_report.html"


def fmt_money(v: float) -> str:
    return f"${v:,.0f}"


def main():
    data = json.loads(IN_JSON.read_text())
    scenarios = data["scenarios"]
    base = scenarios[0]
    chosen_id = "V22_F3_MAKER_REAL"

    # 各シナリオの説明(非エンジニア向け)
    descriptions = {
        "V22_BASE":          "今の気持ちマックス v2.2 そのまま。成行注文で手数料も今のまま。比較の基準",
        "V22_F3":            "F3_YEAREND のみ追加。年末は全決済・休む。手数料は今のまま(成行)",
        "V22_MAKER_REAL":    "手数料を Maker(指値)化 だけ。ただし 60%しか約定せず 40%は成行に戻る想定",
        "V22_F3_MAKER_REAL": "★ 今回採用：F3年末決済 + Maker指値化。現実的な約定率60%を想定",
        "V22_F3_MAKER_BEST": "F3 + Maker指値化。100%約定したらの理論最高値(参考値)",
    }

    # ラベル用の短縮表記
    short_labels = {
        "V22_BASE":          "今のまま (ベース)",
        "V22_F3":            "F3だけ追加",
        "V22_MAKER_REAL":    "Makerだけ (現実)",
        "V22_F3_MAKER_REAL": "F3 + Maker (採用)",
        "V22_F3_MAKER_BEST": "F3 + Maker (理論)",
    }

    # 年利ベストの背景色判定
    def bg_for_delta(delta_pt: float) -> str:
        if delta_pt > 0.3:  return "bg-good"
        if delta_pt < -0.3: return "bg-bad"
        return "bg-neutral"

    # --- サマリーテーブル ---
    rows = []
    for s in scenarios:
        sid = s["id"]
        cagr = s["cagr"]
        dd = s["max_dd"]
        final = s["final"]
        yearly_min = min(s["yearly"].values()) if s.get("yearly") else 0
        cagr_delta = cagr - base["cagr"]
        dd_delta = dd - base["max_dd"]
        final_ratio = final / base["final"]
        is_chosen = sid == chosen_id
        chosen_cls = " chosen-row" if is_chosen else ""
        fee_pct = s["fee_rate"] * 200  # %往復
        rows.append(f"""
      <tr class="scenario-row{chosen_cls}">
        <td><strong>{short_labels[sid]}</strong>{' <span class="badge">採用</span>' if is_chosen else ''}</td>
        <td class="num">{fmt_money(final)}</td>
        <td class="num {bg_for_delta(cagr_delta)}">+{cagr:.2f}%</td>
        <td class="num {bg_for_delta(-dd_delta)}">{dd:.2f}%</td>
        <td class="num">{fee_pct:.2f}%</td>
        <td class="num">{'✔' if s.get('year_end_exit') else '—'}</td>
        <td class="num delta">{cagr_delta:+.2f} pt</td>
        <td class="num delta">{dd_delta:+.2f} pt</td>
        <td class="num delta">×{final_ratio:.3f}</td>
      </tr>""")

    # --- 年別リターン比較 ---
    years = sorted(scenarios[0]["yearly"].keys())
    year_header = "".join(f"<th>{y}年</th>" for y in years if y != "2019")

    yearly_rows = []
    for s in scenarios:
        cells = ""
        for y in years:
            if y == "2019": continue
            v = s["yearly"].get(y, 0) * 1.0
            cls = "positive" if v > 0 else "negative"
            cells += f'<td class="num {cls}">+{v:.1f}%</td>' if v > 0 else f'<td class="num {cls}">{v:.1f}%</td>'
        is_chosen = s["id"] == chosen_id
        chosen_cls = " chosen-row" if is_chosen else ""
        yearly_rows.append(f'<tr class="scenario-row{chosen_cls}"><td><strong>{short_labels[s["id"]]}</strong></td>{cells}</tr>')

    # --- Chart.js 用データ ---
    chart_labels = [short_labels[s["id"]] for s in scenarios]
    chart_cagr = [s["cagr"] for s in scenarios]
    chart_dd = [s["max_dd"] for s in scenarios]
    chart_final = [s["final"] for s in scenarios]
    chosen_colors = ["#8FA7E8" if s["id"] != chosen_id else "#F97316" for s in scenarios]

    chart_labels_json = json.dumps(chart_labels, ensure_ascii=False)
    chart_cagr_json = json.dumps(chart_cagr)
    chart_dd_json = json.dumps(chart_dd)
    chart_final_json = json.dumps(chart_final)
    chosen_colors_json = json.dumps(chosen_colors)

    # --- Equity curve データ (週次) ---
    eq_datasets = []
    eq_dates = None
    for s in scenarios:
        eq = s.get("equity_weekly", [])
        if not eq:
            continue
        if eq_dates is None:
            eq_dates = [e.get("ts", "") for e in eq]
        color = "#F97316" if s["id"] == chosen_id else None
        eq_datasets.append({
            "label": short_labels[s["id"]],
            "data": [round(e.get("equity", 0), 0) for e in eq],
            "color": color,
        })
    eq_dates_json = json.dumps(eq_dates or [])
    eq_datasets_json = json.dumps(eq_datasets, ensure_ascii=False)

    chosen = next(s for s in scenarios if s["id"] == chosen_id)
    chosen_final = fmt_money(chosen["final"])
    chosen_cagr = chosen["cagr"]
    chosen_dd = chosen["max_dd"]
    base_final = fmt_money(base["final"])

    # CAGR delta summary
    delta_cagr = chosen["cagr"] - base["cagr"]
    delta_dd = chosen["max_dd"] - base["max_dd"]
    delta_final = chosen["final"] - base["final"]
    delta_final_str = f"+${delta_final:,.0f}" if delta_final >= 0 else f"-${abs(delta_final):,.0f}"

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iter61 F3_YEAREND + Maker指値化 検証レポート</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Yu Gothic", sans-serif;
    background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
    color: #e2e8f0;
    margin: 0; padding: 24px; line-height: 1.6;
  }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  h1 {{ font-size: 28px; margin: 0 0 8px 0; color: #f97316; }}
  .subtitle {{ color: #94a3b8; margin-bottom: 32px; font-size: 14px; }}
  .hero {{
    background: linear-gradient(135deg, #f97316 0%, #ea580c 100%);
    border-radius: 12px; padding: 24px; margin-bottom: 24px;
    box-shadow: 0 10px 30px rgba(249, 115, 22, 0.3);
  }}
  .hero h2 {{ margin: 0 0 12px 0; font-size: 22px; }}
  .hero-stats {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 16px; margin-top: 16px;
  }}
  .stat {{ background: rgba(0,0,0,0.2); padding: 14px; border-radius: 8px; }}
  .stat-label {{ font-size: 12px; opacity: 0.9; }}
  .stat-value {{ font-size: 22px; font-weight: bold; margin-top: 4px; }}
  .card {{
    background: #1e293b; border-radius: 12px; padding: 24px;
    margin-bottom: 24px; border: 1px solid #334155;
  }}
  .card h3 {{ margin-top: 0; color: #fbbf24; font-size: 18px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
  th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #334155; }}
  th {{ background: #0f172a; color: #94a3b8; font-weight: 600; font-size: 13px; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.delta {{ font-size: 13px; color: #94a3b8; }}
  .chosen-row {{ background: rgba(249, 115, 22, 0.1); }}
  .chosen-row td {{ font-weight: 600; }}
  .badge {{
    background: #f97316; color: #0f172a; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; margin-left: 6px; font-weight: 700;
  }}
  .bg-good {{ color: #4ade80; }}
  .bg-bad {{ color: #f87171; }}
  .bg-neutral {{ color: #e2e8f0; }}
  .positive {{ color: #4ade80; }}
  .negative {{ color: #f87171; }}
  .chart-wrap {{ position: relative; height: 320px; margin-top: 16px; }}
  .note {{
    background: #0f172a; border-left: 4px solid #fbbf24;
    padding: 14px; margin-top: 16px; border-radius: 4px; font-size: 14px;
  }}
  .legend {{ font-size: 13px; color: #94a3b8; margin-top: 12px; }}
</style>
</head>
<body>
  <div class="container">
    <h1>🔬 iter61: F3_YEAREND + Maker指値化 検証レポート</h1>
    <div class="subtitle">
      生成日時: {data["generated_at"]} ・ 期間: {data["period"]} ・
      銘柄数: {data["universe_size"]} ・ 計算時間: {data["total_elapsed_sec"]}秒
    </div>

    <!-- Hero: 採用構成のサマリー -->
    <div class="hero">
      <h2>⭐ 採用構成: F3_YEAREND + Maker指値化 (現実値)</h2>
      <p style="margin: 0; opacity: 0.95;">
        ベースラインと比べて<strong>リターン{delta_cagr:+.2f}pt、最大下落{delta_dd:+.2f}pt</strong>。
        F3による年末リスク回避の分はMakerによる手数料削減が補って、
        <strong>両方改善できた</strong>のがポイントです。
      </p>
      <div class="hero-stats">
        <div class="stat">
          <div class="stat-label">最終資産 (5年)</div>
          <div class="stat-value">{chosen_final}</div>
        </div>
        <div class="stat">
          <div class="stat-label">年平均リターン (CAGR)</div>
          <div class="stat-value">+{chosen_cagr:.2f}%</div>
        </div>
        <div class="stat">
          <div class="stat-label">最大下落 (DD)</div>
          <div class="stat-value">{chosen_dd:.2f}%</div>
        </div>
        <div class="stat">
          <div class="stat-label">ベース比 (最終)</div>
          <div class="stat-value">{delta_final_str}</div>
        </div>
      </div>
    </div>

    <!-- 全シナリオ比較表 -->
    <div class="card">
      <h3>📋 全シナリオ比較 (5年バックテスト, $10,000 スタート)</h3>
      <table>
        <thead>
          <tr>
            <th>シナリオ</th>
            <th style="text-align:right">最終資産</th>
            <th style="text-align:right">CAGR</th>
            <th style="text-align:right">最大DD</th>
            <th style="text-align:right">手数料 (往復)</th>
            <th style="text-align:right">年末決済</th>
            <th style="text-align:right">CAGR差</th>
            <th style="text-align:right">DD差</th>
            <th style="text-align:right">資産倍率</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows)}
        </tbody>
      </table>
      <div class="note">
        💡 <strong>読み方：</strong>「採用」行が今回デプロイした構成です。
        CAGR差・DD差・資産倍率は「今のまま(ベース)」と比べた差分。
        CAGRは高いほど良く、DDは低いほど良い。両方が改善しているのが理想。
      </div>
    </div>

    <!-- 年平均リターン比較 -->
    <div class="card">
      <h3>📊 シナリオ別 年平均リターン (CAGR)</h3>
      <div class="chart-wrap"><canvas id="chartCagr"></canvas></div>
      <div class="legend">オレンジ色が採用構成。高いほど良い。</div>
    </div>

    <!-- 最大DD比較 -->
    <div class="card">
      <h3>📉 シナリオ別 最大下落幅 (Max Drawdown)</h3>
      <div class="chart-wrap"><canvas id="chartDD"></canvas></div>
      <div class="legend">オレンジ色が採用構成。低いほど良い（下落が浅い）。</div>
    </div>

    <!-- 資産推移 -->
    <div class="card">
      <h3>📈 資産推移 (週次) — $10,000 からのスタート</h3>
      <div class="chart-wrap"><canvas id="chartEquity"></canvas></div>
      <div class="legend">採用構成（オレンジ太線）は、上位シナリオと遜色ない成長を達成しつつ、年末前後で保守的に動く。</div>
    </div>

    <!-- 年別リターン -->
    <div class="card">
      <h3>🗓 年別リターン比較</h3>
      <table>
        <thead>
          <tr>
            <th>シナリオ</th>
            {year_header}
          </tr>
        </thead>
        <tbody>
          {''.join(yearly_rows)}
        </tbody>
      </table>
      <div class="note">
        💡 <strong>2022年の冬の時代でも全シナリオがプラス</strong>を維持。
        ACH即時ベア退避（v2.2の核機能）が効いています。
      </div>
    </div>

    <!-- 各シナリオの説明 -->
    <div class="card">
      <h3>📖 シナリオの中身</h3>
      <table>
        <thead><tr><th>ID</th><th>内容</th></tr></thead>
        <tbody>
          {''.join(f'<tr><td><strong>{short_labels[s["id"]]}</strong></td><td>{descriptions[s["id"]]}</td></tr>' for s in scenarios)}
        </tbody>
      </table>
    </div>

    <!-- 重要な但し書き -->
    <div class="card" style="border-left: 4px solid #f87171;">
      <h3 style="color: #f87171;">⚠️ 大事な注意書き（必ず読んでください）</h3>
      <ul style="line-height: 1.9;">
        <li><strong>過去データのバックテスト結果です。</strong>未来の利益を保証するものではありません。</li>
        <li><strong>年利125%は理想値です。</strong>実ライブ運用ではスリッページ、API制限、流動性、過適合で
          <strong>バックテストの30〜50%程度</strong>になるのが一般的。現実的な年利期待値は<strong>30〜60%</strong>。</li>
        <li><strong>最大DD 63-64%</strong> は、一時的に資産が半分近くまで減る期間があるということ。
          $10,000 が $3,600 まで減って、また戻ってくる、という動きに耐える必要がある。</li>
        <li><strong>Maker約定率 60%</strong> は現実想定。板の薄い銘柄・急変時は約定しにくい。</li>
      </ul>
    </div>

    <div style="text-align: center; margin-top: 32px; color: #64748b; font-size: 13px;">
      🤖 Generated by iter61 backtest · kimochi-max v2.2
    </div>
  </div>

<script>
const scenarioLabels = {chart_labels_json};
const cagrData = {chart_cagr_json};
const ddData = {chart_dd_json};
const finalData = {chart_final_json};
const barColors = {chosen_colors_json};

// CAGR bar chart
new Chart(document.getElementById('chartCagr'), {{
  type: 'bar',
  data: {{
    labels: scenarioLabels,
    datasets: [{{
      label: 'CAGR (%)', data: cagrData, backgroundColor: barColors,
      borderRadius: 6,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      y: {{ ticks: {{ color: '#94a3b8', callback: v => '+' + v + '%' }}, grid: {{ color: '#334155' }} }},
      x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ display: false }} }}
    }}
  }}
}});

// Max DD bar chart
new Chart(document.getElementById('chartDD'), {{
  type: 'bar',
  data: {{
    labels: scenarioLabels,
    datasets: [{{
      label: 'Max DD (%)', data: ddData, backgroundColor: barColors,
      borderRadius: 6,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      y: {{ ticks: {{ color: '#94a3b8', callback: v => v + '%' }}, grid: {{ color: '#334155' }} }},
      x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ display: false }} }}
    }}
  }}
}});

// Equity curve
const eqDates = {eq_dates_json};
const eqDatasets = {eq_datasets_json};
const palette = ['#60a5fa', '#a78bfa', '#34d399', '#fbbf24', '#f87171'];
new Chart(document.getElementById('chartEquity'), {{
  type: 'line',
  data: {{
    labels: eqDates,
    datasets: eqDatasets.map((d, i) => ({{
      label: d.label,
      data: d.data,
      borderColor: d.color || palette[i % palette.length],
      backgroundColor: 'transparent',
      tension: 0.2,
      borderWidth: d.color ? 3 : 1.5,
      pointRadius: 0,
    }}))
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ intersect: false, mode: 'index' }},
    plugins: {{
      legend: {{ labels: {{ color: '#e2e8f0' }} }},
      tooltip: {{ callbacks: {{ label: ctx => ctx.dataset.label + ': $' + Math.round(ctx.parsed.y).toLocaleString() }} }}
    }},
    scales: {{
      y: {{
        type: 'logarithmic',
        ticks: {{ color: '#94a3b8', callback: v => '$' + v.toLocaleString() }},
        grid: {{ color: '#334155' }}
      }},
      x: {{
        ticks: {{ color: '#94a3b8', maxTicksLimit: 12 }},
        grid: {{ display: false }}
      }}
    }}
  }}
}});
</script>
</body>
</html>
"""

    OUT_HTML.write_text(html)
    print(f"✅ HTML生成完了: {OUT_HTML}")
    print(f"   ブラウザで表示: http://localhost:8080/iter61_report.html")


if __name__ == "__main__":
    main()
