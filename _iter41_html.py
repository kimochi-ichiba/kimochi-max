"""
Iter41 比較HTMLレポート生成
"""
from __future__ import annotations
import json
from pathlib import Path

DATA_PATH = (Path(__file__).resolve().parent / "results" / "iter41_improve.json")
OUT_PATH = (Path(__file__).resolve().parent / "results" / "iter41_report.html")


def main():
    data = json.loads(DATA_PATH.read_text())
    data_json = json.dumps(data, ensure_ascii=False)

    html = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>Iter41 — I34改良版 7パターン比較</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", sans-serif;
         margin: 0; padding: 24px; background: #f5f6f8; color: #222; line-height: 1.6; }
  .container { max-width: 1240px; margin: 0 auto; }
  h1 { margin-bottom: 4px; font-size: 26px; color: #1a1a2e; }
  .subtitle { color: #666; font-size: 14px; margin-bottom: 24px; }
  .card { background: white; border-radius: 10px; padding: 20px 24px; margin-bottom: 18px;
          box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
  h2 { margin-top: 0; font-size: 20px; color: #2a2a4a; border-left: 4px solid #6366f1;
       padding-left: 10px; }
  h3 { font-size: 16px; color: #333; }
  table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
  th, td { padding: 8px 10px; text-align: right; border-bottom: 1px solid #eee; }
  th:first-child, td:first-child { text-align: left; }
  th { background: #f8fafc; color: #555; font-weight: 600; position: sticky; top: 0; }
  tr.best { background: linear-gradient(90deg, #fef3c7 0%, #fff 100%); font-weight: 600; }
  tr.best-annual { background: linear-gradient(90deg, #dbeafe 0%, #fff 100%); font-weight: 600; }
  tr.neg { color: #dc2626; }
  .chart-box { position: relative; height: 380px; margin-top: 12px; }
  .chart-box.small { height: 260px; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 12px;
          font-size: 11px; font-weight: 600; margin-right: 3px; }
  .good { color: #16a34a; font-weight: 600; }
  .bad { color: #dc2626; font-weight: 600; }
  .star { color: #d97706; }
  .summary-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 12px; }
  .sum-card { background: #fffbeb; padding: 14px 18px; border-radius: 8px;
              border-left: 4px solid #f59e0b; }
  .sum-card.blue { background: #eff6ff; border-left-color: #2563eb; }
  .sum-card h3 { margin: 0 0 6px 0; }
  .notes { background: #ecfdf5; border-left: 4px solid #10b981; padding: 14px 18px;
           border-radius: 6px; margin-top: 14px; }
  .warning { background: #fef2f2; border-left: 4px solid #dc2626; padding: 14px 18px;
             border-radius: 6px; margin-top: 14px; }
  .kpi-row { display: flex; gap: 10px; margin: 8px 0; flex-wrap: wrap; }
  .kpi-row > span { background: #f1f5f9; padding: 4px 10px; border-radius: 6px; font-size: 12px; }
</style>
</head>
<body>
<div class="container">

<h1>🔬 Iter41 — I34改良版 7パターン比較</h1>
<p class="subtitle">
  元金 <b>$10,000</b> ｜ 期間 2020-01 〜 2024-12 (5年) ｜ 実データ: Binance ｜
  目的: 2024年のマイナスを解消する
</p>

<div class="card">
  <h2>🏆 ベスト戦略</h2>
  <div class="summary-grid" id="sumCards"></div>
  <div class="notes" id="notesBox"></div>
</div>

<div class="card">
  <h2>📊 7パターン 総合比較表</h2>
  <table id="bigTable"></table>
</div>

<div class="card">
  <h2>📈 年別リターン比較</h2>
  <p style="color:#666; font-size:13px; margin-top:0;">
    2024年のマイナスがどれだけ解消されたか一目で確認。
  </p>
  <div class="chart-box"><canvas id="yearlyChart"></canvas></div>
</div>

<div class="card">
  <h2>💰 資産推移（対数スケール）</h2>
  <p style="color:#666; font-size:13px; margin-top:0;">
    対数スケール：2021年の大相場と2024年を同じスケールで見るため。
  </p>
  <div class="chart-box"><canvas id="equityChart"></canvas></div>
</div>

<div class="card">
  <h2>📉 最大ドローダウン比較</h2>
  <div class="chart-box small"><canvas id="ddChart"></canvas></div>
</div>

<div class="card">
  <h2>🎯 次の打ち手（Iter42案）</h2>
  <div class="warning" id="nextBox"></div>
</div>

</div>

<script>
const DATA = __DATA_JSON__;
const RESULTS = DATA.results;
const NAMES = Object.keys(RESULTS);
const COLORS = ["#6366f1", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#ec4899", "#06b6d4"];

function yen(n) { return "$" + Number(n).toLocaleString("ja-JP", {maximumFractionDigits: 0}); }
function pct(n) { return (n >= 0 ? "+" : "") + Number(n).toFixed(1) + "%"; }

// ========== Summary cards ==========
const bestAnnual = DATA.best_annual;
const bestPos = DATA.best_no_negative;
const bAnn = RESULTS[bestAnnual];
let sumHtml = "";
sumHtml += `<div class="sum-card">
  <h3>🏆 年率最大: ${bestAnnual}</h3>
  <div class="kpi-row">
    <span>年率 <b class="good">${pct(bAnn.avg_annual_ret)}</b></span>
    <span>$10K → <b>${yen(bAnn.final)}</b></span>
    <span>DD <b>${bAnn.max_dd.toFixed(1)}%</b></span>
    <span>清算 <b>${bAnn.n_liquidations}件</b></span>
    <span>マイナス年 <b>${bAnn.negative_years}</b></span>
  </div>
</div>`;
if (bestPos) {
  const bP = RESULTS[bestPos];
  sumHtml += `<div class="sum-card blue">
    <h3>🎯 毎年プラス達成: ${bestPos}</h3>
    <div class="kpi-row">
      <span>年率 <b class="good">${pct(bP.avg_annual_ret)}</b></span>
      <span>$10K → <b>${yen(bP.final)}</b></span>
      <span>DD <b>${bP.max_dd.toFixed(1)}%</b></span>
      <span>清算 <b>${bP.n_liquidations}件</b></span>
    </div>
  </div>`;
} else {
  // 全パターンマイナス年1以上の場合、2024がプラスのものを強調
  const noFourNeg = Object.entries(RESULTS).filter(([n,r]) => r.yearly["2024"] >= 0);
  if (noFourNeg.length > 0) {
    noFourNeg.sort((a,b)=>b[1].avg_annual_ret - a[1].avg_annual_ret);
    const [n, r] = noFourNeg[0];
    sumHtml += `<div class="sum-card blue">
      <h3>✅ 2024年プラス化成功: ${n}</h3>
      <div class="kpi-row">
        <span>2024年 <b class="good">${pct(r.yearly["2024"])}</b></span>
        <span>年率 <b class="good">${pct(r.avg_annual_ret)}</b></span>
        <span>DD <b>${r.max_dd.toFixed(1)}%</b></span>
        <span>マイナス年 <b>${r.negative_years}</b></span>
      </div>
    </div>`;
  }
}
document.getElementById("sumCards").innerHTML = sumHtml;

// Notes
const base = RESULTS["I34 (ベースライン)"];
const ac = RESULTS["AC (A+C 推奨)"];
const c = RESULTS["C (BTC EMA50フィルタ)"];
document.getElementById("notesBox").innerHTML = `
<h3 style="margin-top:0;">💡 分かったこと</h3>
<ul style="margin: 8px 0 0 0;">
  <li><b>案C（BTC EMA50フィルタ）が決定的に効く</b>：
    2024年 <span class="bad">${pct(base.yearly["2024"])}</span> → <span class="good">${pct(c.yearly["2024"])}</span> に改善。
    さらに年率も +84.0% → <b>+97.5%</b> に向上しました（BTC下落時に余計な取引を避けるため）。</li>
  <li><b>案A（ピラミ4→2）単独では年率が落ちる</b>：
    +84% → +72.9%。ピラミディングで大きく取れる2021年のリターンが大きく減ります（+1107% → +815%）。</li>
  <li><b>案AC（A+C組合せ）が最もバランス良し</b>：
    年率 +86.9% / DD 54.7%（ベースラインより -13pt改善）/ 清算 21件（-9件）/ 2024年 +4.2%。</li>
  <li><b>唯一の課題：2022年がわずかにマイナス</b> （-0.8%〜-1.2%）。
    BTC下落年で、EMA50フィルタが効きすぎて小さくマイナスに。毎年プラスには、あと一歩。</li>
</ul>
`;

// ========== Big table ==========
let bigHtml = `<tr>
  <th>戦略</th>
  <th>2020</th><th>2021</th><th>2022</th><th>2023</th><th>2024</th>
  <th>年率</th><th>最大DD</th><th>勝率</th><th>取引</th><th>清算</th>
  <th>最終資産</th><th>ﾏｲﾅｽ年</th>
</tr>`;
NAMES.forEach(n => {
  const r = RESULTS[n];
  const isBestAnn = n === bestAnnual;
  const isBestPos = bestPos && n === bestPos;
  let cls = "";
  if (isBestPos) cls = "best";
  else if (isBestAnn) cls = "best-annual";
  let row = `<tr class="${cls}"><td><b>${n}</b>`;
  if (isBestPos) row += ' <span class="pill" style="background:#fef3c7;color:#92400e">🏆毎年+</span>';
  if (isBestAnn) row += ' <span class="pill" style="background:#dbeafe;color:#1e40af">🚀年率</span>';
  row += `</td>`;
  [2020,2021,2022,2023,2024].forEach(y => {
    const v = r.yearly[y];
    row += `<td class="${v<0?'bad':'good'}">${pct(v)}</td>`;
  });
  row += `<td class="${r.avg_annual_ret>=50?'good':''}"><b>${pct(r.avg_annual_ret)}</b></td>`;
  row += `<td>${r.max_dd.toFixed(1)}%</td>`;
  row += `<td>${r.win_rate.toFixed(1)}%</td>`;
  row += `<td>${r.n_trades}</td>`;
  row += `<td class="${r.n_liquidations>0?'bad':''}">${r.n_liquidations}</td>`;
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
    labels: ["2020","2021","2022","2023","2024"],
    datasets: NAMES.map((n, i) => ({
      label: n,
      data: [2020,2021,2022,2023,2024].map(y => RESULTS[n].yearly[y] ?? 0),
      backgroundColor: COLORS[i % COLORS.length],
    }))
  },
  options: {
    plugins: { legend: { position: "bottom" } },
    scales: {
      y: {
        type: "logarithmic",
        ticks: { callback: v => v + "%" },
        title: { display: true, text: "年率リターン (%) [対数]" }
      }
    }
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
        borderWidth: n === (bestPos || bestAnnual) ? 3 : 1.5,
        pointRadius: 0,
        tension: 0.1,
      };
    })
  },
  options: {
    plugins: { legend: { position: "bottom" } },
    scales: {
      y: {
        type: "logarithmic",
        ticks: { callback: v => "$" + v.toLocaleString() }
      },
      x: { ticks: { maxTicksLimit: 15 } }
    }
  }
});

// ========== DD chart ==========
new Chart(document.getElementById("ddChart"), {
  type: "bar",
  data: {
    labels: NAMES,
    datasets: [{
      label: "最大DD (%)",
      data: NAMES.map(n => RESULTS[n].max_dd),
      backgroundColor: NAMES.map((n, i) =>
        RESULTS[n].max_dd < 50 ? "#16a34a" :
        RESULTS[n].max_dd < 60 ? "#f59e0b" : "#dc2626"),
    }]
  },
  options: {
    plugins: { legend: { display: false } },
    scales: { y: { ticks: { callback: v => v + "%" } } }
  }
});

// ========== Next steps ==========
document.getElementById("nextBox").innerHTML = `
<h3 style="margin-top:0;">🎯 Iter42の課題：2022年のマイナスを直す</h3>
<p>
  今回のAC案で <b>2024年のマイナス問題は解決</b> しました。しかし副作用として
  <b>2022年が +2.2% → -0.8%</b> と小さくマイナスに転落しました。2022年は仮想通貨の冬で、
  BTC EMA50フィルタが厳しすぎて取引機会を逃した結果と考えられます。
</p>
<h3>次に試す改良案</h3>
<ul>
  <li><b>案E: SHORT解禁（下落相場で稼ぐ）</b> — 2022年のBTC下落を逆に取る。ただしSHORTは過去に荒れた履歴あり。</li>
  <li><b>案F: 現金保有ルール</b> — bullでもbearでもない時（2022年の大半）は取引せず、現金で持つだけ。損をしない。</li>
  <li><b>案G: EMA50フィルタを「2日連続下抜け」に緩める</b> — ダマシ回避、取引機会を増やす。</li>
  <li><b>案H: 2022年専用の「動的レバ調整」</b> — BTC ATR（値動き幅）が高い時はレバ2.5→1.5に下げる。</li>
</ul>
<p style="margin-top:10px;color:#1d4ed8;font-weight:600;">
  推奨：AC を土台に、まず案G（EMA50フィルタ緩和）を試すのが最もリスク低く、2022年プラス化の可能性が高いです。
</p>
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
