"""
Iter43 根本見直しHTMLレポート
"""
from __future__ import annotations
import json
from pathlib import Path

DATA_PATH = Path("/Users/sanosano/projects/kimochi-max/results/iter43_rethink.json")
OUT_PATH = Path("/Users/sanosano/projects/kimochi-max/results/iter43_report.html")


def main():
    data = json.loads(DATA_PATH.read_text())
    data_json = json.dumps(data, ensure_ascii=False)

    html = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>Iter43 — 根本見直し 12戦略タイプ比較</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", sans-serif;
         margin: 0; padding: 24px; background: #f5f6f8; color: #222; line-height: 1.6; }
  .container { max-width: 1280px; margin: 0 auto; }
  h1 { margin-bottom: 4px; font-size: 28px; color: #1a1a2e; }
  .subtitle { color: #666; font-size: 14px; margin-bottom: 24px; }
  .card { background: white; border-radius: 10px; padding: 20px 24px; margin-bottom: 18px;
          box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
  h2 { margin-top: 0; font-size: 21px; color: #2a2a4a; border-left: 4px solid #6366f1;
       padding-left: 10px; }
  h3 { font-size: 16px; color: #333; margin-top: 16px; }
  table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
  th, td { padding: 8px 10px; text-align: right; border-bottom: 1px solid #eee; }
  th:first-child, td:first-child { text-align: left; }
  th { background: #f8fafc; color: #555; font-weight: 600; }
  tr.winner { background: linear-gradient(90deg, #fef3c7 0%, #fff 60%); font-weight: 700; }
  tr.safe { background: linear-gradient(90deg, #dbeafe 0%, #fff 60%); font-weight: 600; }
  tr.hybrid { background: linear-gradient(90deg, #ddd6fe 0%, #fff 60%); }
  .neg { color: #dc2626; }
  .good { color: #16a34a; font-weight: 600; }
  .bad { color: #dc2626; font-weight: 600; }
  .chart-box { position: relative; height: 400px; margin-top: 12px; }
  .chart-box.small { height: 260px; }
  .summary-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-top: 14px; }
  .sum-card { padding: 16px 20px; border-radius: 10px; color: white; }
  .sum-card.gold { background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); }
  .sum-card.blue { background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%); }
  .sum-card.purple { background: linear-gradient(135deg, #8b5cf6 0%, #6d28d9 100%); }
  .sum-card h3 { margin: 0 0 6px 0; color: white; }
  .sum-card .big { font-size: 32px; font-weight: 800; }
  .sum-card .sub { opacity: 0.9; font-size: 12px; }
  .insight { background: #fff7ed; border-left: 4px solid #f97316; padding: 16px 22px;
             border-radius: 6px; margin: 12px 0; }
  .critical { background: #fef2f2; border-left: 4px solid #dc2626; padding: 16px 22px;
              border-radius: 6px; margin: 12px 0; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 12px;
          font-size: 11px; font-weight: 600; margin-right: 3px; }
  .type-passive { background: #dcfce7; color: #166534; }
  .type-rotation { background: #fce7f3; color: #9f1239; }
  .type-active { background: #dbeafe; color: #1e40af; }
  .type-hybrid { background: #ede9fe; color: #5b21b6; }
</style>
</head>
<body>
<div class="container">

<h1>🔬 Iter43 — 根本見直し：全く違うアプローチも含めて比較</h1>
<p class="subtitle">
  元金 <b>$10,000</b> ｜ 期間 2020-01 〜 2024-12 (5年) ｜
  これまで「I34の微調整」ばかりだったのを反省して、<b>まったく違うタイプの戦略</b>も並べて比較
</p>

<!-- トップ発見 -->
<div class="card">
  <h2>🎉 この比較で判明した3つの大発見</h2>
  <div class="summary-grid" id="bigFindings"></div>
</div>

<!-- 総合比較表 -->
<div class="card">
  <h2>📊 12戦略 全比較表（年率降順）</h2>
  <p style="color:#666;font-size:13px;">
    <span class="pill type-passive">受動型</span> = 何もしない／自動保有<br>
    <span class="pill type-rotation">ローテーション型</span> = 毎月銘柄を入れ替える<br>
    <span class="pill type-active">能動型</span> = レバレッジとSLで積極運用<br>
    <span class="pill type-hybrid">ハイブリッド</span> = 複数戦略の合わせ技
  </p>
  <table id="bigTable"></table>
</div>

<!-- 年別棒 -->
<div class="card">
  <h2>📈 年別リターン比較</h2>
  <div class="chart-box"><canvas id="yearlyChart"></canvas></div>
</div>

<!-- 資産曲線 -->
<div class="card">
  <h2>💰 資産推移（対数スケール）</h2>
  <p style="color:#666;font-size:13px;margin-top:0;">
    「どの戦略が最終的に一番勝ったか」が直感的にわかります。
  </p>
  <div class="chart-box"><canvas id="equityChart"></canvas></div>
</div>

<!-- リスク vs リターン散布図 -->
<div class="card">
  <h2>⚖️ リスクとリターンのバランス (散布図)</h2>
  <p style="color:#666;font-size:13px;margin-top:0;">
    右下ほど安全で高利益（理想）、左上ほど危険で低利益（最悪）。
  </p>
  <div class="chart-box"><canvas id="riskReturnChart"></canvas></div>
</div>

<!-- 詳細分析 -->
<div class="card">
  <h2>🔍 戦略タイプ別の深掘り</h2>
  <div id="typeAnalysis"></div>
</div>

<!-- 結論と次の打ち手 -->
<div class="card">
  <h2>🎯 結論と次のIter44案</h2>
  <div id="conclusion"></div>
</div>

</div>

<script>
const DATA = __DATA_JSON__;
const RESULTS = DATA.results;
const NAMES = Object.keys(RESULTS);
const COLORS = [
  "#f59e0b","#fbbf24","#facc15",  // R01-R03 受動 (黄)
  "#10b981","#34d399",              // R04 R04b マイルド (緑)
  "#ec4899","#f472b6",              // R05 R06 モメンタム (ピンク)
  "#6366f1","#818cf8","#a5b4fc",    // R07-R09 能動 (青)
  "#8b5cf6","#a78bfa"                // R10 R11 ハイブリッド (紫)
];

function yen(n) { return "$" + Number(n).toLocaleString("ja-JP", {maximumFractionDigits: 0}); }
function pct(n) { return (n >= 0 ? "+" : "") + Number(n).toFixed(1) + "%"; }

// 戦略タイプ分類
function typeOf(n) {
  if (n.startsWith("R01") || n.startsWith("R02") || n.startsWith("R03")) return "passive";
  if (n.startsWith("R04")) return "passive";
  if (n.startsWith("R05") || n.startsWith("R06")) return "rotation";
  if (n.startsWith("R07") || n.startsWith("R08") || n.startsWith("R09")) return "active";
  return "hybrid";
}

// ========== Big findings ==========
const best = RESULTS[DATA.best_total];
const safest = NAMES.reduce((a,b) => RESULTS[a].max_dd < RESULTS[b].max_dd ? a : b);
const sBest = RESULTS[safest];
const r04 = RESULTS["R04b BTCマイルド+金利3%"];

let findHtml = `
<div class="sum-card gold">
  <h3>🏆 最大の勝者</h3>
  <div class="big">${DATA.best_total}</div>
  <div class="sub">年率 <b>${pct(best.avg_annual_ret)}</b> ｜ $10K → <b>${yen(best.final)}</b></div>
  <div class="sub">5年で <b>${Math.round(best.final/10000)}倍</b>、清算 <b>${best.n_liquidations}回</b></div>
</div>
<div class="sum-card blue">
  <h3>🛡️ 最安全（DD最小）</h3>
  <div class="big">${safest}</div>
  <div class="sub">DD <b>${sBest.max_dd.toFixed(1)}%</b> ｜ 年率 <b>${pct(sBest.avg_annual_ret)}</b></div>
  <div class="sub">清算 <b>${sBest.n_liquidations}回</b> ｜ $10K → <b>${yen(sBest.final)}</b></div>
</div>
<div class="sum-card purple">
  <h3>⚖️ 意外なダークホース</h3>
  <div class="big">R04b BTCマイルド</div>
  <div class="sub">たったの2条件で年率 <b>${pct(r04.avg_annual_ret)}</b></div>
  <div class="sub">2022年 <b>${pct(r04.yearly["2022"])}</b>（BTC -64%時）/ 清算0</div>
</div>
`;
document.getElementById("bigFindings").innerHTML = findHtml;

// ========== Big table ==========
const sorted = [...NAMES].sort((a, b) => RESULTS[b].avg_annual_ret - RESULTS[a].avg_annual_ret);
let bigHtml = `<tr>
  <th>順</th><th>戦略</th><th>タイプ</th>
  <th>2020</th><th>2021</th><th>2022</th><th>2023</th><th>2024</th>
  <th>年率</th><th>DD</th><th>Sharpe</th><th>清算</th>
  <th>最終資産</th><th>ﾏｲﾅｽ年</th>
</tr>`;
sorted.forEach((n, idx) => {
  const r = RESULTS[n];
  const t = typeOf(n);
  let cls = "";
  if (n === DATA.best_total) cls = "winner";
  else if (n === safest) cls = "safe";
  else if (t === "hybrid") cls = "hybrid";

  const typeLabel = {
    passive: '<span class="pill type-passive">受動</span>',
    rotation: '<span class="pill type-rotation">ローテ</span>',
    active: '<span class="pill type-active">能動</span>',
    hybrid: '<span class="pill type-hybrid">ハイブリ</span>',
  }[t];

  let row = `<tr class="${cls}"><td>${idx+1}</td><td><b>${n}</b></td><td>${typeLabel}</td>`;
  [2020,2021,2022,2023,2024].forEach(y => {
    const v = r.yearly[y];
    row += `<td class="${v<0?'bad':'good'}">${pct(v)}</td>`;
  });
  row += `<td class="${r.avg_annual_ret>=70?'good':''}"><b>${pct(r.avg_annual_ret)}</b></td>`;
  row += `<td>${r.max_dd.toFixed(1)}%</td>`;
  row += `<td>${r.sharpe.toFixed(2)}</td>`;
  row += `<td class="${r.n_liquidations>0?'bad':''}">${r.n_liquidations||0}</td>`;
  row += `<td><b>${yen(r.final)}</b></td>`;
  row += `<td class="${r.negative_years>0?'bad':'good'}">${r.negative_years}</td>`;
  row += `</tr>`;
  bigHtml += row;
});
document.getElementById("bigTable").innerHTML = bigHtml;

// ========== Yearly chart ==========
new Chart(document.getElementById("yearlyChart"), {
  type: "bar",
  data: {
    labels: [2020,2021,2022,2023,2024],
    datasets: NAMES.map((n, i) => ({
      label: n,
      data: [2020,2021,2022,2023,2024].map(y => RESULTS[n].yearly[y] ?? 0),
      backgroundColor: COLORS[i % COLORS.length],
    }))
  },
  options: {
    plugins: { legend: { position: "bottom" } },
    scales: { y: { type: "symlog", ticks: { callback: v => v + "%" } } }
  }
});

// ========== Equity chart ==========
const allDates = new Set();
NAMES.forEach(n => RESULTS[n].equity_weekly.forEach(e => allDates.add(e.ts)));
const sortedDates = [...allDates].sort();

new Chart(document.getElementById("equityChart"), {
  type: "line",
  data: {
    labels: sortedDates,
    datasets: NAMES.map((n, i) => {
      const emap = {};
      RESULTS[n].equity_weekly.forEach(e => emap[e.ts] = e.equity);
      return {
        label: n,
        data: sortedDates.map(d => emap[d] ?? null),
        borderColor: COLORS[i % COLORS.length],
        backgroundColor: COLORS[i % COLORS.length] + "15",
        borderWidth: n === DATA.best_total ? 3.5 : 1.5,
        pointRadius: 0,
        tension: 0.1,
      };
    })
  },
  options: {
    plugins: { legend: { position: "bottom" } },
    scales: {
      y: { type: "logarithmic", ticks: { callback: v => "$" + v.toLocaleString() } },
      x: { ticks: { maxTicksLimit: 15 } }
    }
  }
});

// ========== Risk vs Return scatter ==========
new Chart(document.getElementById("riskReturnChart"), {
  type: "scatter",
  data: {
    datasets: NAMES.map((n, i) => ({
      label: n,
      data: [{ x: RESULTS[n].max_dd, y: RESULTS[n].avg_annual_ret }],
      backgroundColor: COLORS[i % COLORS.length],
      pointRadius: 10,
      pointHoverRadius: 14,
    }))
  },
  options: {
    plugins: { legend: { position: "bottom" } },
    scales: {
      x: { title: { display: true, text: "最大DD (%)（小さいほど安全）" },
           ticks: { callback: v => v + "%" } },
      y: { title: { display: true, text: "年率リターン (%)（大きいほど利益）" },
           ticks: { callback: v => v + "%" } }
    }
  }
});

// ========== Type analysis ==========
let typeHtml = `
<div class="insight">
  <h3>🟢 受動型（BTC/ETH 保有系）— シンプルだけど強い！</h3>
  <p>
    <b>R01 BTC単純保有だけでも年率+66.6%</b>。5年持っていれば $10K → $128K。
    しかし2022年 -64% の大暴落を食らうので、DDは77%（一時的に4分の3になる）という精神的にキツい時期があります。
  </p>
  <p>
    <b>R04b BTCマイルド（EMA200フィルタ+金利3%）は静かな優等生</b>：
    年率 +55%、DD 54%、<b>2022年 -5.3%</b> と大崩れを回避。<b>複雑な能動戦略（AC, ACH）と遜色ない成績を、売買ルール2つだけで達成</b>しました。
  </p>
</div>

<div class="insight">
  <h3>🌸 ローテーション型（モメンタムTop3/5）— 優勝候補</h3>
  <p>
    <b>R05 モメンタムTop3 は年率+130.3% で全戦略中1位</b>。
    毎月、過去90日でもっとも強かった3銘柄に乗り換えるだけの「単純ルール」ですが、$10Kが5年で<b>$648K（65倍）</b>に。
  </p>
  <p>
    弱点は <b>2022年 -42.8%</b>。BTC EMA200フィルタが付いているので現金退避はしますが、リバランスの谷間で被害を受けています。
    DDも69%と大きく、精神的にタフでないと続けにくい。
  </p>
</div>

<div class="insight">
  <h3>🔵 能動型（I34/AC/ACH）— 複雑だが魅力度低下</h3>
  <p>
    AC（Iter41推奨）は年率+86.9%ですが、モメンタムTop3の+130%に<b>40ポイントも負けています</b>。
    456〜463回の取引をして、清算21〜30回して、この結果。<b>複雑さに見合っていない</b>というのが正直な結論です。
  </p>
  <p>
    ACH（動的レバ）は DD 45%まで下げて清算4回に減らしたのが唯一の救い。安全性重視派にはまだ意味がある。
  </p>
</div>

<div class="insight">
  <h3>🟣 ハイブリッド型 — 意外にバランス良し</h3>
  <p>
    <b>R10 ハイブリッド 50/50</b>（BTCマイルド半分 + AC半分）は
    <b>年率+73.9% / DD 48%</b>。能動型ACのDD 51%よりも低く、年率も負けてない。
    <b>2022年 -3.0%</b> と、ほぼ横ばいで済みました。<b>「コツコツ型の良さ」と「爆益チャンス」を両取り</b>できる設計。
  </p>
</div>
`;
document.getElementById("typeAnalysis").innerHTML = typeHtml;

// ========== 結論 ==========
document.getElementById("conclusion").innerHTML = `
<div class="critical">
  <h3>🚨 これまでの「I34の微調整」という方向性は間違っていました</h3>
  <p>Iter41・Iter42で時間をかけてAC/ACHを改良してきましたが、<b>単純なモメンタムTop3 がそれらを大きく上回ります</b>。
    根本的にアプローチを変えるべきです。</p>
</div>

<div class="insight">
  <h3>🎯 Iter44の推奨方向（3つの候補）</h3>

  <h3>候補1（最重要）: モメンタムTop3 のリスク改善</h3>
  <ul>
    <li>現状：年率+130% だが 2022年 -43%、DD 69%</li>
    <li>改善案：リバランス頻度を月次→週次、BTCレジームチェックを EMA200 → EMA100 で強化、リバランス時にポジションサイズを市場ボラで調整</li>
    <li>目標：年率 +80% でDD 40%、毎年プラス</li>
  </ul>

  <h3>候補2: トリプルハイブリッド</h3>
  <ul>
    <li>資金を 3分割：40% BTCマイルド + 40% モメンタムTop3 + 20% AC</li>
    <li>いずれか1つが失敗しても、他がカバー</li>
    <li>期待値：年率 +80〜100%、DD 45%、毎年プラス達成の可能性大</li>
  </ul>

  <h3>候補3: モメンタムTop3 + BTCマイルド の50/50</h3>
  <ul>
    <li>最も単純でシンプル</li>
    <li>2022年の穴をBTCマイルドが埋める</li>
    <li>期待値：年率 +90%、DD 50%</li>
  </ul>

  <p style="margin-top:14px;color:#1d4ed8;font-weight:700;font-size:15px;">
    強く推奨：候補1（モメンタムTop3のリスク改善）を最優先で試す。
    これで毎年プラス達成の可能性が最も高いです。
  </p>
</div>
`;
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
