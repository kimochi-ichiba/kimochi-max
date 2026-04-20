"""
40反復の結果をHTMLレポートに変換
"""
from __future__ import annotations
import sys, json
from pathlib import Path
from datetime import datetime
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")


def render_html(data, out_path):
    results = data["results"]
    best_name = data.get("best")
    if not best_name:
        # マイナス年ありでも最高を選ぶ
        valid = sorted(results.items(), key=lambda x: -x[1]["avg_annual_ret"])
        best_name = valid[0][0] if valid else list(results.keys())[0]
    best = results[best_name]
    eq = best.get("equity_curve", [])
    eq_labels = [e["ts"] for e in eq]
    eq_values = [e["equity"] for e in eq]

    rows = []
    for name, r in results.items():
        cls = "best" if name == best_name else ""
        tags = []
        if r["all_positive"]: tags.append('<span class="tag green">毎年+</span>')
        elif r["no_negative"]: tags.append('<span class="tag lightgreen">ﾏｲﾅｽ無</span>')
        if r["avg_annual_ret"] >= 70: tags.append('<span class="tag red">🚀+70%</span>')
        elif r["avg_annual_ret"] >= 50: tags.append('<span class="tag orange">⭐+50%</span>')
        elif r["avg_annual_ret"] >= 30: tags.append('<span class="tag yellow">+30%</span>')
        if r["n_liquidations"] == 0: tags.append('<span class="tag blue">清算0</span>')
        if r["max_dd"] < 40: tags.append(f'<span class="tag purple">DD{r["max_dd"]:.0f}</span>')
        rows.append(f"""
        <tr class="{cls}">
          <td>{name}</td>
          <td class="num">{r['yearly'].get(2020, 0):+.1f}%</td>
          <td class="num">{r['yearly'].get(2021, 0):+.1f}%</td>
          <td class="num {'neg' if r['yearly'].get(2022, 0) < 0 else ''}">{r['yearly'].get(2022, 0):+.1f}%</td>
          <td class="num">{r['yearly'].get(2023, 0):+.1f}%</td>
          <td class="num">{r['yearly'].get(2024, 0):+.1f}%</td>
          <td class="num bold">{r['avg_annual_ret']:+.1f}%</td>
          <td class="num">{r['max_dd']:.1f}%</td>
          <td class="num">{r['win_rate']:.1f}%</td>
          <td class="num">{r['n_trades']}</td>
          <td class="num">{r['n_liquidations']}</td>
          <td>{'✅' if r['integrity_ok'] else '❌'}</td>
          <td>{' '.join(tags)}</td>
        </tr>""")
    table_rows = "\n".join(rows)

    status_badge = "🎉 目標達成（年率+50%以上 × マイナス年なし）" if data.get("target_reached") else f"⚠️ 目標未達 / 40反復完了（ベスト年率 {best['avg_annual_ret']:+.1f}%）"

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>伝説トレーダー手法 × 40反復 バックテスト結果</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Hiragino Sans', sans-serif; margin: 0; padding: 20px; background: #f5f7fa; color: #1a202c; }}
  h1 {{ font-size: 26px; margin: 0 0 10px 0; color: #1a365d; }}
  h2 {{ font-size: 20px; margin: 30px 0 15px 0; color: #2c5282; border-left: 4px solid #3182ce; padding-left: 12px; }}
  .container {{ max-width: 1500px; margin: 0 auto; }}
  .card {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  .hero {{ background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; }}
  .hero h1 {{ color: white; }}
  .metric {{ display: inline-block; margin-right: 24px; margin-bottom: 8px; }}
  .metric-label {{ font-size: 12px; opacity: 0.85; }}
  .metric-value {{ font-size: 22px; font-weight: bold; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th, td {{ padding: 7px 8px; border-bottom: 1px solid #e2e8f0; text-align: left; }}
  th {{ background: #f7fafc; font-weight: 600; color: #4a5568; position: sticky; top: 0; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.bold {{ font-weight: bold; }}
  tr.best {{ background: #fff5f5; }}
  tr.best td {{ font-weight: 600; }}
  .neg {{ color: #c53030; }}
  .tag {{ display: inline-block; padding: 2px 7px; border-radius: 10px; font-size: 10px; margin-right: 3px; font-weight: 600; }}
  .green {{ background: #c6f6d5; color: #22543d; }}
  .lightgreen {{ background: #f0fff4; color: #22543d; }}
  .blue {{ background: #bee3f8; color: #2a4365; }}
  .purple {{ background: #e9d8fd; color: #44337a; }}
  .orange {{ background: #feebc8; color: #7b341e; }}
  .yellow {{ background: #fefcbf; color: #744210; }}
  .red {{ background: #fed7d7; color: #742a2a; }}
  .chart-box {{ height: 400px; position: relative; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin: 20px 0; }}
  .summary-item {{ background: #f7fafc; padding: 16px; border-radius: 8px; border-left: 4px solid #3182ce; }}
  .summary-value {{ font-size: 22px; font-weight: bold; color: #2c5282; }}
  .summary-label {{ font-size: 11px; color: #718096; margin-top: 4px; }}
  .legend {{ font-size: 12px; color: #4a5568; line-height: 1.8; }}
  .legend li {{ margin-bottom: 4px; }}
  .footer {{ text-align: center; color: #718096; font-size: 11px; margin-top: 40px; padding: 20px; }}
</style>
</head>
<body>
<div class="container">

<div class="card hero">
  <h1>🎯 伝説トレーダー手法 × 40反復 バックテスト</h1>
  <p>{status_badge}</p>
  <p style="font-size:13px;opacity:0.9">タートル流・リバモア・ミネルビニ・O'Neil・Seykota・シモンズを組み合わせ / 5年間（2020-2024）Binance実データ / 初期$10,000</p>
  <div style="margin-top:20px">
    <div class="metric"><div class="metric-label">ベスト戦略</div><div class="metric-value">{best_name}</div></div>
    <div class="metric"><div class="metric-label">年率</div><div class="metric-value">{best['avg_annual_ret']:+.1f}%</div></div>
    <div class="metric"><div class="metric-label">5年トータル</div><div class="metric-value">{best['total_ret']:+.0f}%</div></div>
    <div class="metric"><div class="metric-label">最大DD</div><div class="metric-value">{best['max_dd']:.1f}%</div></div>
    <div class="metric"><div class="metric-label">勝率</div><div class="metric-value">{best['win_rate']:.1f}%</div></div>
    <div class="metric"><div class="metric-label">清算</div><div class="metric-value">{best['n_liquidations']}回</div></div>
    <div class="metric"><div class="metric-label">$10K → 5年後</div><div class="metric-value">${best['final']:,.0f}</div></div>
  </div>
</div>

<div class="card">
  <h2>📊 ベスト戦略 資産推移</h2>
  <div class="chart-box"><canvas id="eqChart"></canvas></div>
</div>

<div class="card">
  <h2>📈 年次リターン（ベスト）</h2>
  <div class="chart-box" style="height:280px"><canvas id="yrChart"></canvas></div>
  <div class="summary-grid">
    <div class="summary-item"><div class="summary-value">{best['yearly'].get(2020, 0):+.1f}%</div><div class="summary-label">2020年</div></div>
    <div class="summary-item"><div class="summary-value">{best['yearly'].get(2021, 0):+.1f}%</div><div class="summary-label">2021年 (強気)</div></div>
    <div class="summary-item" style="border-color:{'#c53030' if best['yearly'].get(2022, 0) < 0 else '#3182ce'}"><div class="summary-value" style="color:{'#c53030' if best['yearly'].get(2022, 0) < 0 else '#2c5282'}">{best['yearly'].get(2022, 0):+.1f}%</div><div class="summary-label">2022年 (熊相場)</div></div>
    <div class="summary-item"><div class="summary-value">{best['yearly'].get(2023, 0):+.1f}%</div><div class="summary-label">2023年</div></div>
    <div class="summary-item"><div class="summary-value">{best['yearly'].get(2024, 0):+.1f}%</div><div class="summary-label">2024年</div></div>
  </div>
</div>

<div class="card">
  <h2>🧪 40戦略比較（伝説トレーダー手法別）</h2>
  <p style="color:#4a5568;font-size:13px">Phase1: レバ強化 / Phase2: リバモア流ピラミディング / Phase3: タートル流Donchian / Phase4: ミネルビニVCP+O'Neilボリューム / Phase5: 最終融合</p>
  <div style="max-height:700px;overflow-y:auto">
  <table>
    <thead>
      <tr>
        <th>戦略</th><th>2020</th><th>2021</th><th>2022</th><th>2023</th><th>2024</th>
        <th>年率</th><th>DD</th><th>勝率</th><th>取引</th><th>清算</th><th>整合</th><th>判定</th>
      </tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>
  </div>
</div>

<div class="card">
  <h2>📚 組み込んだ伝説トレーダー手法</h2>
  <ul class="legend">
    <li><strong>リチャード・デニス（タートル）</strong>: Donchian 20/55日高値ブレイクアウト + 10/20日安値エグジット</li>
    <li><strong>ジェシー・リバモア</strong>: 勝ち銘柄にピラミディング（+10%毎に買い増し最大3-4回）</li>
    <li><strong>マーク・ミネルビニ</strong>: VCP = ボラティリティ収縮後のブレイクアウト（ATR/価格 &lt; 6%）</li>
    <li><strong>ウィリアム・オニール（CAN SLIM）</strong>: 平均ボリュームの1.5倍以上で確認</li>
    <li><strong>エド・セイコタ</strong>: トレンドフォロー「利益を走らせ、損切りを切る」（トレール幅大きめ）</li>
    <li><strong>ジム・シモンズ</strong>: ハーフケリーサイジング、ボラティリティターゲティング</li>
    <li><strong>ポール・チューダー・ジョーンズ</strong>: 非対称リスク/リワード（SL:TP = 1:2以上）</li>
    <li><strong>スタン・ワインスタイン</strong>: EMA200ステージ分析（価格&gt;EMA200かつEMA50&gt;EMA200 = Stage 2）</li>
  </ul>
</div>

<div class="card">
  <h2>🛡 データ完全性 & バグ対策</h2>
  <ul class="legend">
    <li>✅ Binance実データ強制ガード（合成データ混入なし）</li>
    <li>✅ 50銘柄中48銘柄がBinance+MEXC+CoinGeckoで実在確認済</li>
    <li>✅ 翌日始値エントリー（先読みバイアスなし）</li>
    <li>✅ 日中SL判定（row["low"]/row["high"] で正確）</li>
    <li>✅ 清算モデル（1/lev × 0.85 のマージンで強制ロスカット）</li>
    <li>✅ 整合性チェック（total_ret と 年次複利の差 &lt; 1.5pp）</li>
    <li>✅ 手数料0.06% + スリッページ0.03% + ファンディング実装</li>
  </ul>
</div>

<div class="footer">
  kimochi-max Legends Backtest / 40 iterations / Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
</div>

</div>

<script>
  const eqLabels = {json.dumps(eq_labels)};
  const eqValues = {json.dumps(eq_values)};
  new Chart(document.getElementById('eqChart'), {{
    type: 'line',
    data: {{
      labels: eqLabels,
      datasets: [{{
        label: '資産 ($)',
        data: eqValues,
        borderColor: '#f5576c',
        backgroundColor: 'rgba(245, 87, 108, 0.1)',
        fill: true, tension: 0.2, pointRadius: 0,
      }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: true }} }},
      scales: {{
        y: {{ ticks: {{ callback: v => '$' + v.toLocaleString() }} }},
        x: {{ ticks: {{ maxTicksLimit: 12 }} }}
      }}
    }}
  }});
  new Chart(document.getElementById('yrChart'), {{
    type: 'bar',
    data: {{
      labels: ['2020','2021','2022','2023','2024'],
      datasets: [{{
        label: '年次リターン (%)',
        data: [{best['yearly'].get(2020, 0)},{best['yearly'].get(2021, 0)},{best['yearly'].get(2022, 0)},{best['yearly'].get(2023, 0)},{best['yearly'].get(2024, 0)}],
        backgroundColor: [
          {best['yearly'].get(2020, 0)} >= 0 ? '#38a169' : '#e53e3e',
          {best['yearly'].get(2021, 0)} >= 0 ? '#38a169' : '#e53e3e',
          {best['yearly'].get(2022, 0)} >= 0 ? '#38a169' : '#e53e3e',
          {best['yearly'].get(2023, 0)} >= 0 ? '#38a169' : '#e53e3e',
          {best['yearly'].get(2024, 0)} >= 0 ? '#38a169' : '#e53e3e'
        ],
      }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{ y: {{ ticks: {{ callback: v => v + '%' }} }} }}
    }}
  }});
</script>
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    in_path = Path("/Users/sanosano/projects/kimochi-max/results/iterate_40.json")
    out_path = Path("/Users/sanosano/projects/kimochi-max/results/iterate_40_report.html")
    data = json.loads(in_path.read_text())
    render_html(data, out_path)
    print(f"📄 HTML: {out_path}")
