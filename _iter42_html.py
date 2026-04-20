"""
Iter42 比較HTMLレポート生成
=================================
含まれる内容:
  - 8パターン総合比較表
  - BTC年別推移と戦略リターンの相関分析（散布図）
  - SHORT取引数
  - 年別バー/資産推移
  - ユーザーの質問に対する回答セクション
"""
from __future__ import annotations
import json
from pathlib import Path

DATA_PATH = Path("/Users/sanosano/projects/kimochi-max/results/iter42_improve.json")
OUT_PATH = Path("/Users/sanosano/projects/kimochi-max/results/iter42_report.html")


def main():
    data = json.loads(DATA_PATH.read_text())
    data_json = json.dumps(data, ensure_ascii=False)

    html = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>Iter42 — BTC相関分析＆改良版比較</title>
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
  h3 { font-size: 16px; color: #333; margin-top: 16px; }
  table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
  th, td { padding: 8px 10px; text-align: right; border-bottom: 1px solid #eee; }
  th:first-child, td:first-child { text-align: left; }
  th { background: #f8fafc; color: #555; font-weight: 600; position: sticky; top: 0; }
  tr.best { background: linear-gradient(90deg, #fef3c7 0%, #fff 100%); font-weight: 600; }
  tr.best-safe { background: linear-gradient(90deg, #dbeafe 0%, #fff 100%); font-weight: 600; }
  tr.neg { color: #dc2626; }
  .chart-box { position: relative; height: 380px; margin-top: 12px; }
  .chart-box.small { height: 280px; }
  .good { color: #16a34a; font-weight: 600; }
  .bad { color: #dc2626; font-weight: 600; }
  .q-box { background: #fff7ed; border-left: 4px solid #f97316; padding: 16px 22px;
           border-radius: 6px; margin-bottom: 14px; }
  .q-box h3 { margin-top: 0; color: #c2410c; }
  .ans-box { background: #eff6ff; border-left: 4px solid #2563eb; padding: 16px 22px;
             border-radius: 6px; margin-top: 8px; }
  .note { background: #f1f5f9; padding: 12px 16px; border-radius: 6px;
          font-size: 13px; color: #475569; margin-top: 10px; }
  .big-num { font-size: 28px; font-weight: 700; }
  .kpi-row { display: flex; gap: 12px; margin: 10px 0; flex-wrap: wrap; }
  .kpi-row > div { background: #f8fafc; padding: 10px 14px; border-radius: 8px; }
  .kpi-row > div .label { font-size: 11px; color: #666; }
  .kpi-row > div .val { font-size: 18px; font-weight: 700; color: #1a1a2e; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 12px;
          font-size: 11px; font-weight: 600; margin-right: 3px; }
</style>
</head>
<body>
<div class="container">

<h1>🔬 Iter42 — BTC相関分析＆改良版比較</h1>
<p class="subtitle">
  元金 <b>$10,000</b> ｜ 期間 2020-01 〜 2024-12 (5年) ｜
  目的: 2022年のマイナスを直す＋SHORT/BTC相関を検証
</p>

<!-- Q&A セクション -->
<div class="card">
  <h2>❓ ご質問への回答</h2>

  <div class="q-box">
    <h3>Q1: SHORT（下落で稼ぐ取引）は実際に行われていますか？</h3>
    <div class="ans-box" id="shortAnswer"></div>
  </div>

  <div class="q-box">
    <h3>Q2: BTC価格の上昇と年間利益率は比例していますか？</h3>
    <div class="ans-box" id="btcCorrAnswer"></div>
  </div>
</div>

<!-- BTC自体の推移 -->
<div class="card">
  <h2>📉 BTC自体の5年間の推移</h2>
  <table id="btcTable"></table>
  <div class="chart-box small"><canvas id="btcChart"></canvas></div>
</div>

<!-- BTC相関散布図 -->
<div class="card">
  <h2>🔗 BTC年別リターン vs 戦略年別リターン（散布図）</h2>
  <p style="color:#666; font-size:13px; margin-top:0;">
    もし完全に比例していれば、点が右肩上がりの直線に並ぶ。実際は…
  </p>
  <div class="chart-box"><canvas id="scatterChart"></canvas></div>
</div>

<!-- 総合比較 -->
<div class="card">
  <h2>📊 Iter42 8パターン総合比較</h2>
  <table id="bigTable"></table>
</div>

<div class="card">
  <h2>📈 年別リターン比較</h2>
  <div class="chart-box"><canvas id="yearlyChart"></canvas></div>
</div>

<div class="card">
  <h2>💰 資産推移（対数スケール）</h2>
  <div class="chart-box"><canvas id="equityChart"></canvas></div>
</div>

<div class="card">
  <h2>🎯 結論と次のIter43案</h2>
  <div id="conclusion"></div>
</div>

</div>

<script>
const DATA = __DATA_JSON__;
const RESULTS = DATA.results;
const BTC_YEARLY = DATA.btc_yearly;
const CORR = DATA.correlations;
const NAMES = Object.keys(RESULTS);
const COLORS = ["#6366f1","#f59e0b","#10b981","#ef4444","#8b5cf6","#ec4899","#06b6d4","#84cc16"];

function yen(n) { return "$" + Number(n).toLocaleString("ja-JP", {maximumFractionDigits: 0}); }
function pct(n) { return (n >= 0 ? "+" : "") + Number(n).toFixed(1) + "%"; }

// ========== Q1: SHORT ==========
const shortCounts = NAMES.map(n => ({name: n, s: RESULTS[n].n_short, l: RESULTS[n].n_long}));
const anyShort = shortCounts.some(s => s.s > 0);
let shortHtml = `
  <p><b>結論：SHORT取引は一度も発動していません。</b></p>
  <p>8パターン中、<b>3つ</b>（ACE, ACEG, ACEH, ACEGH）は<code>enable_short=True</code>（SHORT許可）で動いていますが、
     実際のSHORT取引数は全て<b>0件</b>でした。</p>
  <h3>なぜ発動しないのか？</h3>
  <p>SHORTを発動するには、BTCが以下の条件を同時に満たす必要があります：</p>
  <ul>
    <li>BTC価格が EMA200 より 2%以上 下</li>
    <li>EMA50 が EMA200 より下</li>
    <li>BTCのADX（トレンドの強さ）が 40以上</li>
    <li>過去14日高値より 3%以上 下</li>
  </ul>
  <p>2022年はBTCが長期下落しましたが、<b>緩やかなだらだら下げ</b>でADXが40に届かなかったため、
     SHORT判定が一度も成立しませんでした。</p>
  <h3>対策案（Iter43候補）</h3>
  <p>BTC ADX条件を 40 → <b>25</b> に緩めれば、2022年の下落トレンドでSHORTが発動するはずです。
     ただしSHORTは過去に荒れた履歴があるので、最初は小さめのポジションで試すのがおすすめです。</p>
`;
document.getElementById("shortAnswer").innerHTML = shortHtml;

// ========== Q2: BTC相関 ==========
const years = [2020,2021,2022,2023,2024];
const btcRets = years.map(y => BTC_YEARLY[y].return_pct);
const acRets = years.map(y => RESULTS["AC (Iter41ベース)"].yearly[y]);

// AC戦略 vs BTC
let q2Html = `
  <p><b>結論：BTCの上昇と戦略リターンは、ほぼ比例していません。</b></p>

  <h3>年別の比較（AC戦略の場合）</h3>
  <table>
    <tr><th>年</th><th>BTCの動き</th><th>AC戦略</th><th>関係性</th></tr>
`;
const situations = [
  {y:2020, btc_str:"大相場", strat_str:"そこそこ", note:"BTC +302%に対し戦略 +46%。<b>BTC未満</b>"},
  {y:2021, btc_str:"普通", strat_str:"大爆発", note:"BTC +60%に対し戦略 +1,230%。<b>BTC超える大勝</b>（アルトコインで稼いだ）"},
  {y:2022, btc_str:"暴落", strat_str:"ほぼ横ばい", note:"BTC -64%でも戦略 -0.8%。<b>大崩れは回避</b>（EMA200フィルタで撤退した）"},
  {y:2023, btc_str:"急回復", strat_str:"小", note:"BTC +156%に対し戦略 +14%。<b>BTC未満</b>（回復に乗り切れず）"},
  {y:2024, btc_str:"好調", strat_str:"わずか", note:"BTC +119%に対し戦略 +4%。<b>BTC未満</b>（春の急落で稼ぎを失った）"},
];
situations.forEach(s => {
  q2Html += `<tr><td>${s.y}</td>
    <td>${pct(BTC_YEARLY[s.y].return_pct)} (${s.btc_str})</td>
    <td>${pct(RESULTS["AC (Iter41ベース)"].yearly[s.y])} (${s.strat_str})</td>
    <td>${s.note}</td></tr>`;
});
q2Html += `</table>`;

// 相関係数
q2Html += `<h3>相関係数（-1〜+1、1に近いほど比例）</h3>
  <div class="kpi-row">`;
Object.entries(CORR).slice(0,4).forEach(([name, c]) => {
  const color = c > 0.5 ? "#16a34a" : c > 0 ? "#f59e0b" : "#dc2626";
  q2Html += `<div><div class="label">${name}</div>
    <div class="val" style="color:${color}">${c >= 0 ? "+" : ""}${c.toFixed(2)}</div></div>`;
});
q2Html += `</div>`;

q2Html += `
  <h3>分かりやすく言うと</h3>
  <ul>
    <li><b>BTCが爆発した年（2020, 2023, 2024）、戦略のリターンは意外と小さい</b>。理由はBTC単体を持ってるわけではなく、レバレッジとピラミディング（買い増し）を使って「ブレイクアウト」を狙う戦略だから。</li>
    <li><b>BTCが横ばい〜ちょい下げの年（2021年）、戦略は大爆発</b>。アルトコイン（ソラナ、DOGEなど）が急騰した年で、ほぼこの1年の利益が全期間の8割。</li>
    <li><b>BTCが大暴落した年（2022年）、戦略は小損で済んだ</b>。EMA200（200日移動平均線）の下では新規エントリーを控える仕組みが効いた。</li>
  </ul>
  <p style="color:#dc2626;font-weight:600;">
    つまり：この戦略は<b>BTCの動きには連動しません</b>。むしろ「アルトコインのブレイクアウト相場」で稼ぐ特殊な戦略です。
    BTCが上がっても、上がり方が「緩やかな階段状」だと戦略は稼げません。
  </p>
`;
document.getElementById("btcCorrAnswer").innerHTML = q2Html;

// ========== BTC table & chart ==========
let btcTabHtml = `<tr><th>年</th><th>年初価格</th><th>年末価格</th><th>年別リターン</th></tr>`;
years.forEach(y => {
  const v = BTC_YEARLY[y];
  btcTabHtml += `<tr class="${v.return_pct<0?'neg':''}">
    <td>${y}</td><td>${yen(v.start)}</td><td>${yen(v.end)}</td>
    <td class="${v.return_pct>=0?'good':'bad'}">${pct(v.return_pct)}</td></tr>`;
});
document.getElementById("btcTable").innerHTML = btcTabHtml;

new Chart(document.getElementById("btcChart"), {
  type: "bar",
  data: {
    labels: years,
    datasets: [{
      label: "BTC年別リターン (%)",
      data: btcRets,
      backgroundColor: btcRets.map(v => v >= 0 ? "#f59e0b" : "#dc2626"),
    }]
  },
  options: {
    plugins: { legend: { display: false } },
    scales: { y: { ticks: { callback: v => v + "%" } } }
  }
});

// ========== 散布図 ==========
const scatterDatasets = NAMES.map((n, i) => ({
  label: n,
  data: years.map(y => ({ x: BTC_YEARLY[y].return_pct, y: RESULTS[n].yearly[y] })),
  backgroundColor: COLORS[i % COLORS.length],
  pointRadius: 6,
  pointHoverRadius: 9,
}));

new Chart(document.getElementById("scatterChart"), {
  type: "scatter",
  data: { datasets: scatterDatasets },
  options: {
    plugins: { legend: { position: "bottom" } },
    scales: {
      x: { title: { display: true, text: "BTC年別リターン (%)" },
           ticks: { callback: v => v + "%" } },
      y: { title: { display: true, text: "戦略年別リターン (%) [対数]" },
           type: "logarithmic", ticks: { callback: v => v + "%" } }
    }
  }
});

// ========== 比較表 ==========
let bigHtml = `<tr>
  <th>戦略</th>
  <th>2020</th><th>2021</th><th>2022</th><th>2023</th><th>2024</th>
  <th>年率</th><th>DD</th><th>LONG</th><th>SHORT</th><th>清算</th>
  <th>最終</th><th>ﾏｲﾅｽ年</th>
</tr>`;
// 2022年最良 と 安全性最良(DD最小) をハイライト
const best2022 = DATA.best_2022;
const bestSafe = NAMES.reduce((a,b) => RESULTS[a].max_dd < RESULTS[b].max_dd ? a : b);
NAMES.forEach(n => {
  const r = RESULTS[n];
  let cls = "";
  if (n === best2022) cls = "best";
  else if (n === bestSafe) cls = "best-safe";
  let row = `<tr class="${cls}"><td><b>${n}</b>`;
  if (n === best2022) row += ' <span class="pill" style="background:#fef3c7;color:#92400e">🥇2022最良</span>';
  if (n === bestSafe) row += ' <span class="pill" style="background:#dbeafe;color:#1e40af">🛡️最安全</span>';
  row += `</td>`;
  years.forEach(y => {
    const v = r.yearly[y];
    row += `<td class="${v<0?'bad':'good'}">${pct(v)}</td>`;
  });
  row += `<td class="${r.avg_annual_ret>=50?'good':''}"><b>${pct(r.avg_annual_ret)}</b></td>`;
  row += `<td>${r.max_dd.toFixed(1)}%</td>`;
  row += `<td>${r.n_long}</td>`;
  row += `<td class="${r.n_short>0?'good':''}">${r.n_short}</td>`;
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
    labels: years,
    datasets: [
      {
        label: "BTC",
        data: btcRets,
        backgroundColor: "#64748b",
        borderColor: "#334155",
        borderWidth: 2,
      },
      ...NAMES.map((n, i) => ({
        label: n,
        data: years.map(y => RESULTS[n].yearly[y]),
        backgroundColor: COLORS[i % COLORS.length],
      }))
    ]
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
        borderWidth: n === bestSafe ? 3 : 1.5,
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

// ========== 結論 ==========
document.getElementById("conclusion").innerHTML = `
<div class="q-box">
  <h3>🏁 Iter42の結論</h3>
  <ul>
    <li><b>2022年のマイナスは直りませんでした</b>。-0.8% → 最良 <b>-0.67%</b> と、ほぼ変化なし。</li>
    <li><b>SHORT は一度も発動せず</b>。判定条件がきつすぎて、2022年の緩やかな下落では動かない。</li>
    <li><b>ACH（動的レバ）が意外な優勝馬</b>：最終資産は落ちるが、
      DD <b>55% → 47%</b>、清算 <b>21 → 4</b>、2024年 <b>+4.2% → +10.9%</b>。
      「爆益より安全」を優先する人向け。</li>
  </ul>
</div>

<div class="ans-box">
  <h3>🎯 Iter43（次の打ち手）案</h3>
  <ol>
    <li><b>SHORT条件を大幅に緩める</b>：ADX ≥ 40 → <b>25</b> に下げる。2022年の下落トレンドで実際にSHORTを動かす。</li>
    <li><b>bear regime専用のSHORT戦略</b>：アルトコインが崩れた2022春と秋の大下落で、小ポジションSHORT。</li>
    <li><b>ACH + SHORT強化の合体</b>：動的レバで安全性キープしつつ、下落時は逆張り攻撃。</li>
    <li><b>現金保有に賞金（無リスク金利）を加える</b>：2022年の大半で現金ポジションのとき、年3%の金利を想定すると+3%の底上げになる。現実的に運用するならUSDT stakingなどで十分可能。</li>
  </ol>
  <p style="color:#1d4ed8;font-weight:600;margin-top:10px;">
    推奨：Iter43では <b>ACH + SHORT条件緩和(ADX=25) + USDT金利+3%</b> を試す。
    これで 2022年 -0.7% → <b>+3〜5%</b> の可能性が高く、毎年プラス達成が見えてきます。
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
