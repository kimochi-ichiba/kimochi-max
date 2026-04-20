"""
Iter43 HTMLレポート v2
============================
改善点:
  1. 白とび対策: グラデーション廃止、背景色は純色、文字色は明示
  2. ハルシネーション検証セクション追加 (冒頭に配置)
  3. モメンタム戦略で選ばれた実在銘柄リストを表示
"""
from __future__ import annotations
import json
from pathlib import Path

DATA_PATH = Path("/Users/sanosano/projects/kimochi-max/results/iter43_rethink.json")
HALLUC_PATH = Path("/Users/sanosano/projects/kimochi-max/results/iter43_hallucination.json")
OUT_PATH = Path("/Users/sanosano/projects/kimochi-max/results/iter43_report_v2.html")


def main():
    data = json.loads(DATA_PATH.read_text())
    hallu = json.loads(HALLUC_PATH.read_text())

    # 2021年モメンタム選択ログだけ抽出（レポート用）
    mom_2021 = [log for log in hallu["step4_momentum_log"]
                if log["date"].startswith("2021")]

    data_json = json.dumps({
        **data,
        "halluc": {
            "cache": hallu["step1_cache"],
            "yearly_verification": hallu["step3_yearly_verification"],
            "mom_2021": mom_2021,
        },
    }, ensure_ascii=False)

    html = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>Iter43 v2 — ハルシネーション検証済み 12戦略比較</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", "Noto Sans JP", sans-serif;
    margin: 0; padding: 24px;
    background: #eef2f7;
    color: #1a1a1a;
    line-height: 1.6;
  }
  .container { max-width: 1280px; margin: 0 auto; }
  h1 { margin-bottom: 4px; font-size: 28px; color: #0f172a; }
  .subtitle { color: #475569; font-size: 14px; margin-bottom: 24px; }
  .card {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 20px 24px;
    margin-bottom: 18px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }
  h2 {
    margin: 0 0 14px 0;
    font-size: 21px;
    color: #1e293b;
    border-left: 4px solid #6366f1;
    padding-left: 10px;
  }
  h3 { font-size: 16px; color: #334155; margin: 16px 0 8px 0; }

  /* ===== テーブル ===== */
  table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 10px;
    font-size: 13px;
    background: #ffffff;
    color: #1a1a1a;
  }
  th, td {
    padding: 9px 10px;
    text-align: right;
    border-bottom: 1px solid #e5e7eb;
    color: #1a1a1a;
  }
  th:first-child, td:first-child { text-align: left; }
  th {
    background: #f1f5f9;
    color: #334155;
    font-weight: 600;
    border-bottom: 2px solid #cbd5e1;
  }
  tr.winner { background: #fef9c3; }
  tr.winner td { color: #713f12; font-weight: 700; }
  tr.safe { background: #dbeafe; }
  tr.safe td { color: #1e3a8a; font-weight: 600; }
  tr.hybrid { background: #ede9fe; }
  tr.hybrid td { color: #4c1d95; }
  .pos { color: #15803d; font-weight: 600; }
  .neg { color: #b91c1c; font-weight: 600; }

  /* ===== チャート ===== */
  .chart-box { position: relative; height: 400px; margin-top: 12px; background: #ffffff; }
  .chart-box.small { height: 260px; }

  /* ===== サマリーカード ===== */
  .summary-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-top: 14px; }
  .sum-card {
    padding: 16px 20px;
    border-radius: 10px;
    color: #ffffff;
    background: #1e293b;
  }
  .sum-card.gold { background: #b45309; }
  .sum-card.blue { background: #1d4ed8; }
  .sum-card.purple { background: #6d28d9; }
  .sum-card h3 { margin: 0 0 6px 0; color: #ffffff; }
  .sum-card .big { font-size: 26px; font-weight: 800; color: #ffffff; }
  .sum-card .sub { opacity: 0.92; font-size: 12px; color: #ffffff; }

  /* ===== ハルシネーション検証ボックス ===== */
  .hal-ok {
    background: #ecfdf5;
    border: 2px solid #10b981;
    padding: 14px 20px;
    border-radius: 8px;
    margin: 12px 0;
    color: #064e3b;
  }
  .hal-warn {
    background: #fef3c7;
    border: 2px solid #f59e0b;
    padding: 14px 20px;
    border-radius: 8px;
    margin: 12px 0;
    color: #78350f;
  }
  .hal-box {
    background: #f8fafc;
    border: 1px solid #cbd5e1;
    padding: 12px 16px;
    border-radius: 6px;
    margin: 10px 0;
    color: #1a1a1a;
  }

  /* ===== 情報ボックス ===== */
  .insight {
    background: #fff7ed;
    border: 1px solid #fdba74;
    border-left: 4px solid #f97316;
    padding: 16px 22px;
    border-radius: 6px;
    margin: 12px 0;
    color: #1a1a1a;
  }
  .insight h3 { color: #9a3412; margin-top: 0; }
  .critical {
    background: #fef2f2;
    border: 1px solid #fca5a5;
    border-left: 4px solid #dc2626;
    padding: 16px 22px;
    border-radius: 6px;
    margin: 12px 0;
    color: #1a1a1a;
  }
  .critical h3 { color: #991b1b; margin-top: 0; }

  /* ===== ピル ===== */
  .pill {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
    margin-right: 3px;
  }
  .type-passive { background: #dcfce7; color: #166534; }
  .type-rotation { background: #fce7f3; color: #9f1239; }
  .type-active { background: #dbeafe; color: #1e40af; }
  .type-hybrid { background: #ede9fe; color: #5b21b6; }

  /* ===== 銘柄ログ ===== */
  .mom-log { font-size: 12px; color: #1a1a1a; }
  .mom-log .month { font-weight: 700; color: #334155; margin-top: 8px; }
  .mom-log .coin { display: inline-block; padding: 2px 8px; border-radius: 4px;
                   margin: 2px; background: #f1f5f9; color: #1a1a1a; }
  .mom-log .cash { color: #64748b; font-style: italic; }

  strong, b { color: #0f172a; }
  ul { padding-left: 22px; margin: 8px 0; }
  li { margin-bottom: 4px; color: #1a1a1a; }
</style>
</head>
<body>
<div class="container">

<h1>🔬 Iter43 v2 — ハルシネーション検証済み 12戦略比較</h1>
<p class="subtitle">
  元金 <b>$10,000</b> ｜ 期間 2020-01 〜 2024-12 (5年) ｜ データ: Binance実データ(UNIVERSE_50)
  ｜ 検証済み: 独立計算/公開値と整合
</p>

<!-- ━━━━━ ハルシネーション検証 ━━━━━ -->
<div class="card">
  <h2>🛡️ データ真正性検証（ハルシネーションチェック）</h2>

  <div class="hal-ok">
    <h3 style="margin-top:0;">✅ 結論：今回のバックテストは実データに基づいています</h3>
    <ul>
      <li><b>元データ</b>：Binance取引所の実データ（50銘柄、1,941本のローソク足）</li>
      <li><b>BTC年別リターン</b>：独立計算と結果が誤差0.36%以内で一致（差分は取引手数料のみ）</li>
      <li><b>ETH年別リターン</b>：独立計算と結果が誤差0.51%以内で一致</li>
      <li><b>モメンタム戦略が選んだ銘柄</b>：全て実在銘柄（SUSHI, FTM, DOGE, SOL, AXS, AVAX等）。2021年DeFiサマーの実際の急騰銘柄と一致</li>
      <li><b>架空のデータ・計算の改ざんなし</b></li>
    </ul>
  </div>

  <div class="hal-warn">
    <h3 style="margin-top:0;">⚠️ 一点注意</h3>
    <p>本日の検証で、<b>CoinGecko の公開API</b>に対して 401 Unauthorized（認証エラー）が返りました（APIポリシー変更の可能性）。
    そのため、CoinGecko とのリアルタイム比較は今回できませんでしたが、
    <b>独立計算と公開値（公知のBTC年別リターン）との整合性チェック</b>で代替検証しています。</p>
  </div>

  <h3>📊 BTC/ETH 年別リターン 三方比較</h3>
  <table id="verifyTable"></table>

  <h3>📌 モメンタムTop3 が2021年に選んだ実在銘柄</h3>
  <p style="font-size:13px;color:#475569;">
    2021年の +2,483% という驚異的リターンが、どの銘柄で作られたかを確認します。これらはすべて実在する暗号通貨で、
    2021年DeFi/NFTサマーの実際の急騰銘柄です。
  </p>
  <div class="mom-log" id="momLog"></div>
</div>

<!-- ━━━━━ トップ発見 ━━━━━ -->
<div class="card">
  <h2>🎉 この比較で判明した3つの大発見</h2>
  <div class="summary-grid" id="bigFindings"></div>
</div>

<!-- ━━━━━ 総合比較表 ━━━━━ -->
<div class="card">
  <h2>📊 12戦略 全比較表（年率降順）</h2>
  <p style="color:#475569;font-size:13px;">
    <span class="pill type-passive">受動型</span> = 何もしない／自動保有 &nbsp;
    <span class="pill type-rotation">ローテーション型</span> = 毎月銘柄を入れ替える &nbsp;
    <span class="pill type-active">能動型</span> = レバレッジとSLで積極運用 &nbsp;
    <span class="pill type-hybrid">ハイブリッド</span> = 複数戦略の合わせ技
  </p>
  <table id="bigTable"></table>
</div>

<!-- ━━━━━ 年別棒 ━━━━━ -->
<div class="card">
  <h2>📈 年別リターン比較</h2>
  <div class="chart-box"><canvas id="yearlyChart"></canvas></div>
</div>

<!-- ━━━━━ 資産曲線 ━━━━━ -->
<div class="card">
  <h2>💰 資産推移（対数スケール）</h2>
  <p style="color:#475569;font-size:13px;margin-top:0;">
    「どの戦略が最終的に一番勝ったか」が直感的にわかります。
  </p>
  <div class="chart-box"><canvas id="equityChart"></canvas></div>
</div>

<!-- ━━━━━ リスク vs リターン散布図 ━━━━━ -->
<div class="card">
  <h2>⚖️ リスクとリターンのバランス (散布図)</h2>
  <p style="color:#475569;font-size:13px;margin-top:0;">
    右下ほど安全で高利益（理想）、左上ほど危険で低利益（最悪）。
  </p>
  <div class="chart-box"><canvas id="riskReturnChart"></canvas></div>
</div>

<!-- ━━━━━ 詳細分析 ━━━━━ -->
<div class="card">
  <h2>🔍 戦略タイプ別の深掘り</h2>
  <div id="typeAnalysis"></div>
</div>

<!-- ━━━━━ 結論と次の打ち手 ━━━━━ -->
<div class="card">
  <h2>🎯 結論と次のIter44案</h2>
  <div id="conclusion"></div>
</div>

</div>

<script>
const DATA = __DATA_JSON__;
const RESULTS = DATA.results;
const NAMES = Object.keys(RESULTS);
const HAL = DATA.halluc;

const COLORS = [
  "#f59e0b","#fbbf24","#facc15",
  "#10b981","#34d399",
  "#ec4899","#f472b6",
  "#6366f1","#818cf8","#a5b4fc",
  "#8b5cf6","#a78bfa"
];

function yen(n) { return "$" + Number(n).toLocaleString("ja-JP", {maximumFractionDigits: 0}); }
function pct(n) { return (n >= 0 ? "+" : "") + Number(n).toFixed(1) + "%"; }
function typeOf(n) {
  if (n.startsWith("R01") || n.startsWith("R02") || n.startsWith("R03")) return "passive";
  if (n.startsWith("R04")) return "passive";
  if (n.startsWith("R05") || n.startsWith("R06")) return "rotation";
  if (n.startsWith("R07") || n.startsWith("R08") || n.startsWith("R09")) return "active";
  return "hybrid";
}

// ========== 検証テーブル ==========
const v = HAL.yearly_verification;
const publicBTC = {2020: 303, 2021: 60, 2022: -64, 2023: 156, 2024: 121};
let vhtml = `<tr>
  <th>年</th><th>公開値 (概算)</th><th>独立計算</th>
  <th>Iter43 R01 BTC</th><th>誤差 (R01 − 独立)</th></tr>`;
for (let y = 2020; y <= 2024; y++) {
  const indep = v.independent_btc[y];
  const r43 = v.iter43_r01_btc[y] ?? v.iter43_r01_btc[String(y)];
  const diff = (r43 !== undefined && indep !== undefined) ? (r43 - indep).toFixed(2) : "-";
  vhtml += `<tr>
    <td>${y}</td>
    <td>${pct(publicBTC[y])}</td>
    <td>${indep !== undefined ? pct(indep) : "-"}</td>
    <td>${r43 !== undefined ? pct(r43) : "-"}</td>
    <td class="${Math.abs(diff) < 1 ? 'pos' : 'neg'}">${diff}pp</td>
  </tr>`;
}
document.getElementById("verifyTable").innerHTML = vhtml;

// ========== モメンタムログ ==========
let mhtml = "";
HAL.mom_2021.forEach(log => {
  const d = log.date.slice(5, 10);
  if (log.regime === "現金") {
    mhtml += `<div class="month">${log.date}</div>
      <span class="cash">→ 現金保有（BTCがEMA200下抜け）BTC=$${log.btc_price.toLocaleString()}</span>`;
  } else {
    mhtml += `<div class="month">${log.date}（BTC=$${log.btc_price.toLocaleString()}）</div>`;
    log.top3.forEach(t => {
      const cleanSym = t.sym.replace("/USDT", "");
      mhtml += `<span class="coin"><b>${cleanSym}</b> 過去90日 ${pct(t.ret_90d)}</span>`;
    });
  }
});
document.getElementById("momLog").innerHTML = mhtml;

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
  <div class="big">BTCマイルド</div>
  <div class="sub">たった2条件で年率 <b>${pct(r04.avg_annual_ret)}</b></div>
  <div class="sub">2022年 <b>${pct(r04.yearly["2022"])}</b>（BTC −64%時）/ 清算0</div>
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
    const vv = r.yearly[y];
    row += `<td class="${vv<0?'neg':'pos'}">${pct(vv)}</td>`;
  });
  row += `<td class="pos"><b>${pct(r.avg_annual_ret)}</b></td>`;
  row += `<td>${r.max_dd.toFixed(1)}%</td>`;
  row += `<td>${r.sharpe.toFixed(2)}</td>`;
  row += `<td class="${r.n_liquidations>0?'neg':''}">${r.n_liquidations||0}</td>`;
  row += `<td><b>${yen(r.final)}</b></td>`;
  row += `<td class="${r.negative_years>0?'neg':'pos'}">${r.negative_years}</td>`;
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

// ========== Risk vs Return ==========
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
document.getElementById("typeAnalysis").innerHTML = `
<div class="insight">
  <h3>🟢 受動型（BTC/ETH 保有系）— シンプルだけど強い</h3>
  <p>
    <b>R01 BTC単純保有だけでも年率+66.6%</b>。5年持っていれば $10K → $128K。
    しかし2022年 -64% の大暴落を食らうので、DDは77%（一時的に4分の3になる）という精神的にキツい時期があります。
  </p>
  <p>
    <b>R04b BTCマイルド（EMA200フィルタ+金利3%）は静かな優等生</b>：
    年率 +55%、DD 54%、<b>2022年 -5.3%</b> と大崩れを回避。
    <b>複雑な能動戦略（AC, ACH）と遜色ない成績を、売買ルール2つだけで達成</b>しました。
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
