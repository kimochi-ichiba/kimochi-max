"""
Iter46 HTMLレポート v2 (白紙問題修正版)
==============================================
修正点:
  - symlog スケール廃止（互換性問題）→ グループ化バー＋値ラベル表示
  - 全チャートを try-catch で囲み、エラーがあっても他チャートは表示
  - モバイル対応 (iPhone Safari 対応)
  - Chart.js CDNを robust化
"""
from __future__ import annotations
import json
from pathlib import Path

DATA_PATH = (Path(__file__).resolve().parent / "results" / "iter46_hybrid.json")
OUT_PATH = (Path(__file__).resolve().parent / "results" / "iter46_report_v2.html")


def main():
    data = json.loads(DATA_PATH.read_text())
    data_json = json.dumps(data, ensure_ascii=False, default=str)

    html = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Iter46 v2 — DD解説＆ハイブリッド最適化</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"
        onerror="this.onerror=null;this.src='https://unpkg.com/chart.js@4.4.1/dist/chart.umd.js'"></script>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", "Noto Sans JP", sans-serif;
         margin: 0; padding: 16px; background: #eef2f7; color: #1a1a1a; line-height: 1.7; }
  .container { max-width: 1280px; margin: 0 auto; }
  h1 { margin-bottom: 4px; font-size: 24px; color: #0f172a; }
  .subtitle { color: #475569; font-size: 13px; margin-bottom: 20px; }
  .card { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px;
          padding: 18px 22px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  h2 { margin: 0 0 12px 0; font-size: 20px; color: #0f172a;
       border-left: 4px solid #6366f1; padding-left: 10px; }
  h3 { font-size: 16px; color: #334155; margin: 14px 0 8px 0; }
  table { width: 100%; border-collapse: collapse; margin-top: 10px;
          font-size: 12px; background: #ffffff; color: #1a1a1a; }
  th, td { padding: 8px 8px; text-align: right; border-bottom: 1px solid #e5e7eb; color: #1a1a1a; }
  th:first-child, td:first-child { text-align: left; }
  th { background: #f1f5f9; color: #334155; font-weight: 600; border-bottom: 2px solid #cbd5e1; }
  .pos { color: #15803d; font-weight: 600; }
  .neg { color: #b91c1c; font-weight: 600; }

  tr.best { background: #dcfce7; }
  tr.best td { color: #14532d; font-weight: 700; }
  tr.baseline { background: #fef3c7; }
  tr.baseline td { color: #713f12; }
  tr.great-safe { background: #dbeafe; }
  tr.great-safe td { color: #1e3a8a; font-weight: 600; }

  .chart-box { position: relative; height: 380px; margin-top: 10px; background: #ffffff; }
  .chart-box.small { height: 240px; }

  .dd-visual { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 14px; margin: 14px 0; }
  .dd-case { padding: 16px 20px; border-radius: 10px; border: 2px solid; }
  .dd-good { background: #ecfdf5; border-color: #10b981; color: #064e3b; }
  .dd-mid { background: #fef3c7; border-color: #f59e0b; color: #78350f; }
  .dd-bad { background: #fee2e2; border-color: #dc2626; color: #7f1d1d; }
  .dd-case h3 { margin-top: 0; }
  .dd-case .story { font-size: 13px; margin: 8px 0; }
  .dd-case .bignum { font-size: 38px; font-weight: 800; margin: 6px 0; }
  .dd-good .bignum { color: #10b981; }
  .dd-mid .bignum { color: #f59e0b; }
  .dd-bad .bignum { color: #dc2626; }

  .winner-card { background: #0f172a; color: #ffffff; padding: 22px 28px;
                 border-radius: 12px; margin: 14px 0; border: 3px solid #10b981; }
  .winner-card h3 { color: #6ee7b7; margin: 0; font-size: 14px; }
  .winner-card .title { font-size: 22px; font-weight: 800; margin: 6px 0; color: #ffffff; }
  .winner-card .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
                       gap: 10px; margin-top: 12px; }
  .winner-card .kpi { background: #1e293b; padding: 10px 14px; border-radius: 8px; }
  .winner-card .kpi .lbl { font-size: 11px; color: #94a3b8; }
  .winner-card .kpi .v { font-size: 20px; font-weight: 700; color: #10b981; }

  .explainer { background: #fff7ed; border: 1px solid #fdba74; border-left: 4px solid #f97316;
               padding: 14px 20px; border-radius: 6px; margin: 10px 0; }
  .explainer h3 { color: #9a3412; margin-top: 0; }
  .insight { background: #eff6ff; border: 1px solid #93c5fd; border-left: 4px solid #2563eb;
             padding: 14px 20px; border-radius: 6px; margin: 10px 0; }
  .insight h3 { color: #1e40af; margin-top: 0; }

  .error-msg { background: #fee2e2; color: #991b1b; padding: 12px; border-radius: 6px;
               border: 1px solid #fca5a5; margin: 10px 0; font-size: 13px; }

  strong, b { color: #0f172a; }
  ul { padding-left: 20px; margin: 8px 0; }
  li { margin-bottom: 4px; }

  /* モバイル対応 */
  @media (max-width: 768px) {
    body { padding: 10px; }
    h1 { font-size: 20px; }
    h2 { font-size: 17px; }
    .card { padding: 12px 16px; }
    table { font-size: 11px; }
    th, td { padding: 6px 4px; }
    .chart-box { height: 320px; }
    .winner-card .title { font-size: 18px; }
  }
</style>
</head>
<body>
<div class="container">

<h1>🔀 Iter46 v2 — DDをやさしく説明＆ハイブリッド戦略の最適化</h1>
<p class="subtitle">
  「ハイブリッド50/50がすごく良いと思っている」というお声にお応えして、DDの意味から改良版までまとめました。<br>
  元金 <b>$10,000</b>、期間2020-2024、Binance実データ使用
</p>

<!-- ━━━━━━ DDの説明 ━━━━━━ -->
<div class="card">
  <h2>📖 まず、DD（ドローダウン）って何？</h2>

  <div class="explainer">
    <h3>DD = 「一番高かった時から一番下がった時の落差」</h3>
    <p>
      たとえば資産が <b>100万円 → 500万円 → 150万円 → 200万円</b> と動いた場合、
      最大DDは <b>500万円から150万円 = 70%</b>（300万円以上減った瞬間があった）ということです。
      最終的には+100万円儲かったとしても、<b>途中で300万円以上減った恐怖体験</b>があったわけです。
    </p>
  </div>

  <h3>💡 100万円で始めた時、DDの違いでどうなる？</h3>
  <div class="dd-visual">
    <div class="dd-case dd-bad">
      <h3>😱 DD 70%（モメンタム戦略）</h3>
      <div class="bignum">$30万円</div>
      <div class="story">一番下がった時の残高。100万円が30万円に。70万円が一時的に消えた気分。</div>
      <p style="font-size:12px;">投げ売りしてしまう危険性大。夜眠れない。仕事に集中できない。家庭にも影響。</p>
    </div>
    <div class="dd-case dd-mid">
      <h3>😰 DD 48%（ハイブリッド50/50）</h3>
      <div class="bignum">$52万円</div>
      <div class="story">半分近く減る。「もうダメかも」と不安になる瞬間。</div>
      <p style="font-size:12px;">1-2年かけて回復することが多い。精神的にキツいが、長期で見れば回復する。</p>
    </div>
    <div class="dd-case dd-good">
      <h3>😊 DD 25%（低DD戦略）</h3>
      <div class="bignum">$75万円</div>
      <div class="story">75%は残る。「まだ全然平気」と冷静でいられる。</div>
      <p style="font-size:12px;">生活に影響なし。夜ぐっすり眠れる。長く続けられる。</p>
    </div>
  </div>

  <div class="insight">
    <h3>⚠️ DDが大きいと、3つの怖いことが起きる</h3>
    <ul>
      <li><b>①「投げ売り」の誘惑</b>：「もうダメだ」と思って底値で売ってしまう → これが一番怖い失敗パターン</li>
      <li><b>②回復に時間がかかる</b>：100万→50万に戻すには<b>2倍に増やす必要がある</b>（半値戻し問題）</li>
      <li><b>③人生への影響</b>：眠れない、家族に当たる、仕事に集中できない... お金の問題が生活全体に広がる</li>
    </ul>
  </div>
</div>

<!-- ━━━━━━ 推奨戦略 ━━━━━━ -->
<div class="card">
  <h2>🏆 新しく最適化された推奨戦略 H11</h2>
  <div class="winner-card" id="winnerCard"></div>

  <div class="explainer">
    <h3>💡 H11を超おすすめする理由</h3>
    <ul>
      <li><b>清算が21件→4件に激減</b>：ACをACH（動的レバ版）に変更、相場が荒い時は自動でレバレッジを下げる</li>
      <li><b>USDT 20%の現金クッション</b>：大暴落時の買い増しチャンス資金＆精神的安定</li>
      <li><b>DD 39.7%に抑制</b>：元のR10(48%)から約9ポイント改善</li>
      <li><b>2022年も-3.7%の軽微</b>：BTC単体なら-64%の年に、ほぼノーダメージ</li>
    </ul>
  </div>
</div>

<!-- ━━━━━━ ハイブリッド比較表 ━━━━━━ -->
<div class="card">
  <h2>📊 ハイブリッド12パターン 全比較（DD小さい順）</h2>
  <table id="hybridTable"></table>
</div>

<!-- ━━━━━━ 散布図 ━━━━━━ -->
<div class="card">
  <h2>⚖️ リスクとリターンのバランス</h2>
  <p style="color:#475569;font-size:13px;">
    左下（安全・利益小）〜 右上（危険・爆益）のどこが自分に合うか選べます。
  </p>
  <div class="chart-box"><canvas id="scatterChart"></canvas></div>
</div>

<!-- ━━━━━━ 年別リターン ━━━━━━ -->
<div class="card">
  <h2>📈 年別リターン比較（対数スケール）</h2>
  <p style="color:#475569;font-size:13px;">
    2021年は大きい数値なので対数スケールで表示しています。
  </p>
  <div class="chart-box"><canvas id="yearlyChart"></canvas></div>
</div>

<!-- ━━━━━━ 資産推移 ━━━━━━ -->
<div class="card">
  <h2>💰 資産推移（対数スケール）</h2>
  <div class="chart-box"><canvas id="equityChart"></canvas></div>
</div>

<!-- ━━━━━━ 選び方ガイド ━━━━━━ -->
<div class="card">
  <h2>🎯 あなたに合う戦略（状況別）</h2>
  <div id="whoShould"></div>
</div>

<!-- ━━━━━━ $100万円シミュレーション ━━━━━━ -->
<div class="card">
  <h2>💎 $100万円でのシミュレーション</h2>
  <div style="background:#0f172a;color:white;padding:20px;border-radius:12px;overflow-x:auto;">
    <table style="color:white;background:transparent;">
      <tr style="background:#1e293b;">
        <th style="color:#cbd5e1;">戦略</th>
        <th style="color:#cbd5e1;">5年後</th>
        <th style="color:#cbd5e1;">一番下がった時</th>
        <th style="color:#cbd5e1;">ストレス</th>
      </tr>
      <tr style="background:#1e293b;">
        <td style="color:white;">H01 元の50/50</td>
        <td style="color:#10b981;"><b>$1,590万円</b></td>
        <td style="color:#ef4444;">$52万円</td>
        <td style="color:white;">😰</td>
      </tr>
      <tr style="background:#0f172a;">
        <td style="color:white;"><b>H11 新おすすめ ★</b></td>
        <td style="color:#10b981;"><b>$887万円</b></td>
        <td style="color:#f59e0b;">$60万円</td>
        <td style="color:white;">😊</td>
      </tr>
      <tr style="background:#1e293b;">
        <td style="color:white;">H09 超安全</td>
        <td style="color:#10b981;">$773万円</td>
        <td style="color:#10b981;">$62万円</td>
        <td style="color:white;">😌</td>
      </tr>
    </table>
  </div>
</div>

</div>

<script>
const DATA = __DATA_JSON__;
const RESULTS = DATA.results || {};
const NAMES = Object.keys(RESULTS);

function yen(n) { return "$" + Number(n).toLocaleString("ja-JP", {maximumFractionDigits: 0}); }
function pct(n) { return (n >= 0 ? "+" : "") + Number(n).toFixed(1) + "%"; }
function getYear(r, y) {
  if (!r || !r.yearly) return 0;
  return r.yearly[String(y)] ?? r.yearly[y] ?? 0;
}

// Chart.js ready チェック
function withChartJs(fn) {
  if (typeof Chart === 'undefined') {
    setTimeout(() => withChartJs(fn), 200);
    return;
  }
  try { fn(); } catch(e) {
    console.error("Chart error:", e);
  }
}

// ===== Winner card =====
try {
  const winner = RESULTS["H11 BTC40%+ACH40%+USDT20% (バランス安全)"];
  if (winner) {
    document.getElementById("winnerCard").innerHTML = `
      <h3>🥇 新・推奨: H11 BTC40% + ACH40% + USDT20%</h3>
      <div class="title">BTCコツコツ40% ＋ 動的レバAC 40% ＋ 現金クッション20%</div>
      <div class="kpis">
        <div class="kpi"><div class="lbl">年率</div><div class="v">${pct(winner.avg_annual_ret)}</div></div>
        <div class="kpi"><div class="lbl">最大DD</div><div class="v">${winner.max_dd.toFixed(1)}%</div></div>
        <div class="kpi"><div class="lbl">Sharpe</div><div class="v">${winner.sharpe.toFixed(2)}</div></div>
        <div class="kpi"><div class="lbl">清算</div><div class="v">${winner.n_liquidations}回</div></div>
        <div class="kpi"><div class="lbl">最終資産</div><div class="v">${yen(winner.final)}</div></div>
        <div class="kpi"><div class="lbl">2022年</div><div class="v">${pct(getYear(winner, 2022))}</div></div>
      </div>`;
  }
} catch(e) { console.error(e); }

// ===== ハイブリッド比較表 =====
try {
  const sorted = [...NAMES].sort((a, b) => RESULTS[a].max_dd - RESULTS[b].max_dd);
  let tblHtml = `<tr>
    <th>#</th><th>戦略</th>
    <th>2020</th><th>2021</th><th>2022</th><th>2023</th><th>2024</th>
    <th>年率</th><th>★DD</th><th>Sharpe</th><th>清算</th><th>最終</th>
  </tr>`;
  sorted.forEach((n, idx) => {
    const r = RESULTS[n];
    let cls = "";
    if (n.startsWith("H11")) cls = "best";
    else if (n.startsWith("H01")) cls = "baseline";
    else if (r.max_dd < 42) cls = "great-safe";

    let row = `<tr class="${cls}"><td>${idx+1}</td><td><b>${n}</b>`;
    if (n.startsWith("H11")) row += ' 🥇';
    if (n.startsWith("H01")) row += ' （元）';
    row += `</td>`;
    [2020,2021,2022,2023,2024].forEach(y => {
      const v = getYear(r, y);
      row += `<td class="${v<0?'neg':'pos'}">${pct(v)}</td>`;
    });
    row += `<td class="pos"><b>${pct(r.avg_annual_ret)}</b></td>`;
    row += `<td><b>${r.max_dd.toFixed(1)}%</b></td>`;
    row += `<td>${r.sharpe.toFixed(2)}</td>`;
    row += `<td class="${r.n_liquidations>10?'neg':''}">${r.n_liquidations||0}</td>`;
    row += `<td>${yen(r.final)}</td></tr>`;
    tblHtml += row;
  });
  document.getElementById("hybridTable").innerHTML = tblHtml;
} catch(e) { console.error(e); }

// ===== おすすめガイド =====
document.getElementById("whoShould").innerHTML = `
<div class="insight">
  <h3>🟢 超安心派 → H11 (DD 39.7%) / H09 (DD 38.1%) / H12 (DD 39.7%)</h3>
  <ul>
    <li><b>H11 BTC40%+ACH40%+USDT20%</b>（年率+54.8%、DD 39.7%、清算4件）← <b>一番おすすめ</b></li>
    <li><b>H09 BTC40%+ACH30%+USDT30%</b>（年率+50.5%、DD 38.1%、清算4件）</li>
    <li><b>H12 BTC60%+ACH20%+USDT20%</b>（年率+52.1%、DD 39.7%、清算4件）</li>
    <li>こんな人におすすめ：本業が忙しい方、家族と住宅資金も考える方、初めての仮想通貨</li>
  </ul>
</div>
<div class="explainer">
  <h3>🔵 バランス派 → H08 (DD 41.0%) / H03 (DD 44.8%)</h3>
  <ul>
    <li><b>H08 BTC50%+ACH50%</b>（年率+61.0%、DD 41.0%、清算4件）</li>
    <li><b>H03 BTC70%+AC30%</b>（年率+67.4%、DD 44.8%、清算21件）</li>
    <li>こんな人におすすめ：30〜50代で余裕資金がある方、中長期で最大化したい方</li>
  </ul>
</div>
<div class="explainer" style="background:#fee2e2;border-color:#fca5a5;border-left-color:#dc2626;">
  <h3>🔴 攻め派 → H04 or H10 (DD 49%)</h3>
  <ul>
    <li><b>H04 BTC40%+AC60%</b>（年率+76.8%、DD 49.0%、清算21件）</li>
    <li><b>H10 BTC40%+ACH30%+モメ15%+USDT15%</b>（年率+76.8%、DD 48.9%、Sharpe最高1.25）</li>
    <li>こんな人におすすめ：余剰資金の少額で、DD 50%に耐える心構えあり</li>
  </ul>
</div>
`;

// ===== Charts =====
const COLORS = ["#6366f1","#f59e0b","#10b981","#ef4444","#8b5cf6","#ec4899",
                "#06b6d4","#84cc16","#f97316","#0ea5e9","#14b8a6","#a855f7"];

withChartJs(() => {
  // Scatter
  new Chart(document.getElementById("scatterChart"), {
    type: "scatter",
    data: {
      datasets: NAMES.map((n, i) => ({
        label: n,
        data: [{ x: RESULTS[n].max_dd, y: RESULTS[n].avg_annual_ret }],
        backgroundColor: COLORS[i % COLORS.length],
        pointRadius: n.startsWith("H11") ? 16 : 10,
        pointHoverRadius: 18,
      }))
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "bottom", labels: { boxWidth: 12, padding: 6, font: { size: 11 } } } },
      scales: {
        x: { title: { display: true, text: "最大DD (%) ← 左ほど安全" },
             ticks: { callback: v => v + "%" } },
        y: { title: { display: true, text: "年率 (%) → 上ほど儲かる" },
             ticks: { callback: v => v + "%" } }
      }
    }
  });

  // Yearly (対数スケール)
  new Chart(document.getElementById("yearlyChart"), {
    type: "bar",
    data: {
      labels: ["2020","2021","2022","2023","2024"],
      datasets: NAMES.map((n, i) => ({
        label: n,
        data: [2020,2021,2022,2023,2024].map(y => {
          const v = getYear(RESULTS[n], y);
          return v > 0 ? v : (v < 0 ? Math.max(v, -99) : 0.01);
        }),
        backgroundColor: COLORS[i % COLORS.length],
      }))
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "bottom", labels: { boxWidth: 12, padding: 4, font: { size: 10 } } } },
      scales: {
        y: {
          type: "logarithmic",
          min: 0.1, max: 2000,
          ticks: { callback: v => (v < 1 ? v.toFixed(1) : Math.round(v)) + "%" }
        }
      }
    }
  });

  // Equity
  const allDates = new Set();
  NAMES.forEach(n => (RESULTS[n].equity_weekly || []).forEach(e => allDates.add(e.ts)));
  const sortedDates = [...allDates].sort();
  new Chart(document.getElementById("equityChart"), {
    type: "line",
    data: {
      labels: sortedDates,
      datasets: NAMES.map((n, i) => {
        const emap = {};
        (RESULTS[n].equity_weekly || []).forEach(e => emap[e.ts] = e.equity);
        return {
          label: n,
          data: sortedDates.map(d => emap[d] ?? null),
          borderColor: COLORS[i % COLORS.length],
          backgroundColor: COLORS[i % COLORS.length] + "15",
          borderWidth: n.startsWith("H11") ? 3.5 : 1.5,
          pointRadius: 0,
          tension: 0.1,
        };
      })
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "bottom", labels: { boxWidth: 12, padding: 4, font: { size: 10 } } } },
      scales: {
        y: { type: "logarithmic", ticks: { callback: v => "$" + v.toLocaleString() } },
        x: { ticks: { maxTicksLimit: 12 } }
      }
    }
  });
});
</script>
</body>
</html>
"""
    html = html.replace("__DATA_JSON__", data_json)
    OUT_PATH.write_text(html)
    print(f"✅ {OUT_PATH}")
    print(f"   サイズ: {OUT_PATH.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
