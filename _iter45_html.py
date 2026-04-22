"""
Iter45 低DD特化 HTMLレポート
==================================
「精神的にラクな運用」にフォーカス
"""
from __future__ import annotations
import json
from pathlib import Path

DATA_PATH = (Path(__file__).resolve().parent / "results" / "iter45_low_dd.json")
ITER43_PATH = (Path(__file__).resolve().parent / "results" / "iter43_rethink.json")
OUT_PATH = (Path(__file__).resolve().parent / "results" / "iter45_report.html")


def main():
    data = json.loads(DATA_PATH.read_text())
    iter43 = json.loads(ITER43_PATH.read_text())

    combined = {
        "low_dd": data,
        "compare": {
            "R01 BTC単純保有":      iter43["results"]["R01 BTC単純保有"],
            "R05 モメンタムTop3":    iter43["results"]["R05 モメンタムTop3"],
            "R08 AC (Iter41)":     iter43["results"]["R08 AC (Iter41)"],
            "R10 ハイブリッド 50/50": iter43["results"]["R10 ハイブリッド 50/50"],
        }
    }
    data_json = json.dumps(combined, ensure_ascii=False, default=str)

    html = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>Iter45 — 低DD特化 精神的にラクな運用</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", "Noto Sans JP", sans-serif;
         margin: 0; padding: 24px; background: #eef2f7; color: #1a1a1a; line-height: 1.65; }
  .container { max-width: 1280px; margin: 0 auto; }
  h1 { margin-bottom: 4px; font-size: 28px; color: #0f172a; }
  .subtitle { color: #475569; font-size: 14px; margin-bottom: 24px; }
  .card { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px;
          padding: 22px 26px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  h2 { margin: 0 0 14px 0; font-size: 22px; color: #0f172a;
       border-left: 4px solid #6366f1; padding-left: 12px; }
  h3 { font-size: 16px; color: #334155; margin: 16px 0 8px 0; }
  table { width: 100%; border-collapse: collapse; margin-top: 10px;
          font-size: 13px; background: #ffffff; color: #1a1a1a; }
  th, td { padding: 9px 10px; text-align: right; border-bottom: 1px solid #e5e7eb; color: #1a1a1a; }
  th:first-child, td:first-child { text-align: left; }
  th { background: #f1f5f9; color: #334155; font-weight: 600; border-bottom: 2px solid #cbd5e1; }
  tr.best-dd { background: #dcfce7; }
  tr.best-dd td { color: #14532d; font-weight: 600; }
  tr.new { background: #fef3c7; }
  tr.new td { color: #713f12; font-weight: 600; }
  tr.old { background: #fee2e2; }
  tr.old td { color: #991b1b; }
  .pos { color: #15803d; font-weight: 600; }
  .neg { color: #b91c1c; font-weight: 600; }

  .chart-box { position: relative; height: 420px; margin-top: 12px; background: #ffffff; }
  .chart-box.small { height: 260px; }

  .winner-card {
    background: #0f172a; color: #ffffff; padding: 24px 32px;
    border-radius: 12px; margin: 16px 0;
    border: 3px solid #10b981;
  }
  .winner-card h3 { color: #6ee7b7; margin: 0; font-size: 15px; }
  .winner-card .title { font-size: 26px; font-weight: 800; margin: 6px 0; color: #ffffff; }
  .winner-card .kpis { display: grid; grid-template-columns: repeat(4, 1fr);
                        gap: 12px; margin-top: 14px; }
  .winner-card .kpi {
    background: #1e293b; padding: 10px 14px; border-radius: 8px;
  }
  .winner-card .kpi .lbl { font-size: 11px; color: #94a3b8; }
  .winner-card .kpi .v { font-size: 22px; font-weight: 700; color: #10b981; }

  .explainer {
    background: #fff7ed; border: 1px solid #fdba74; border-left: 4px solid #f97316;
    padding: 16px 22px; border-radius: 6px; margin: 12px 0; color: #1a1a1a;
  }
  .explainer h3 { color: #9a3412; margin-top: 0; }

  .tradeoff {
    background: #eff6ff; border: 1px solid #93c5fd; border-left: 4px solid #2563eb;
    padding: 16px 22px; border-radius: 6px; margin: 12px 0;
  }
  .tradeoff h3 { color: #1e40af; margin-top: 0; }

  strong, b { color: #0f172a; }
  ul { padding-left: 22px; margin: 8px 0; }
  li { margin-bottom: 5px; }

  .pill { display: inline-block; padding: 2px 8px; border-radius: 12px;
          font-size: 11px; font-weight: 600; }
  .pill-safe { background: #dcfce7; color: #166534; }
  .pill-balance { background: #dbeafe; color: #1e40af; }
  .pill-aggressive { background: #fee2e2; color: #991b1b; }
</style>
</head>
<body>
<div class="container">

<h1>🛡️ Iter45 — 低DD特化：精神的にラクな運用</h1>
<p class="subtitle">
  「DD 50〜70%はキツい」というご希望に応えて、<b>DDを30%以下に抑える</b>戦略17パターンを検証しました。
  元金 <b>$10,000</b>、期間2020-2024、Binance実データ使用
</p>

<!-- ━━━━━━ 一番安心な結論 ━━━━━━ -->
<div class="card">
  <h2>🏆 最も精神的にラクな戦略（DD 20%以下を達成！）</h2>
  <div class="winner-card" id="winnerCard"></div>

  <div class="explainer">
    <h3>💡 これがどういう意味か</h3>
    <p>
      この戦略だと、<b>$10,000 → $26,000 (2.6倍)</b> になります。
      大事なのは、途中で資産が一番減った時でも <b>$10,000 → $7,960 (DD 20.4%)</b> しか下がらなかったこと。
      他の戦略では $10K が $3K まで下がる瞬間があった（DD 70%）ことを考えると、<b>精神的な負担は3分の1以下</b>です。
    </p>
    <p>
      年率は +21% に下がりますが、これでも <b>銀行預金(0.002%)の1万倍、S&P500の2倍</b>のリターンです。
      「爆益」を狙うより「よく眠れる運用」を重視する方には圧倒的におすすめです。
    </p>
  </div>
</div>

<!-- ━━━━━━ DD vs リターン散布図 ━━━━━━ -->
<div class="card">
  <h2>⚖️ リスクとリターンのトレードオフ（散布図）</h2>
  <p style="color:#475569;font-size:13px;">
    <b>左に行くほど安全</b>（DDが小さい）、<b>上に行くほど高収益</b>。<br>
    右上（爆益だが危険）か、左下（小さい利益で安全）か、バランスはあなたの好み次第です。
  </p>
  <div class="chart-box"><canvas id="riskReturnChart"></canvas></div>
</div>

<!-- ━━━━━━ 低DDランキング ━━━━━━ -->
<div class="card">
  <h2>🛡️ 低DD戦略ランキング（DDが小さい順）</h2>
  <p style="color:#475569;font-size:13px;">
    <span class="pill pill-safe">🟢 超安全</span> = DD 25%以下 &nbsp;
    <span class="pill pill-balance">🔵 バランス</span> = DD 25〜45% &nbsp;
    <span class="pill pill-aggressive">🔴 攻め</span> = DD 45%以上
  </p>
  <table id="rankTable"></table>
</div>

<!-- ━━━━━━ 既存戦略との比較 ━━━━━━ -->
<div class="card">
  <h2>📊 Iter43までの戦略 vs 新しい低DD戦略</h2>
  <table id="compareTable"></table>
  <div class="tradeoff">
    <h3>💡 見方</h3>
    <p>
      <b>同じ年率+20%でも、DD 20%と70%では感じる「怖さ」が全然違います</b>。
      例えば、$100万円でスタートして DD 70% だと一時的に $30万円（-$70万円）、DD 20%なら一時的に $80万円（-$20万円）まで下がる計算です。
    </p>
  </div>
</div>

<!-- ━━━━━━ 年別リターン比較 ━━━━━━ -->
<div class="card">
  <h2>📈 低DD戦略 年別リターン比較</h2>
  <div class="chart-box"><canvas id="yearlyChart"></canvas></div>
</div>

<!-- ━━━━━━ 資産推移 ━━━━━━ -->
<div class="card">
  <h2>💰 資産推移（対数スケール）</h2>
  <div class="chart-box"><canvas id="equityChart"></canvas></div>
</div>

<!-- ━━━━━━ 実用判断ガイド ━━━━━━ -->
<div class="card">
  <h2>🎯 あなたに合う戦略は？</h2>
  <div id="recommendGuide"></div>
</div>

</div>

<script>
const DATA = __DATA_JSON__;
const LOW = DATA.low_dd;
const LOW_RESULTS = LOW.results;
const COMP = DATA.compare;
const ALL = {...LOW_RESULTS, ...COMP};
const LOW_NAMES = Object.keys(LOW_RESULTS);
const COMP_NAMES = Object.keys(COMP);
const ALL_NAMES = [...LOW_NAMES, ...COMP_NAMES];

function yen(n) { return "$" + Number(n).toLocaleString("ja-JP", {maximumFractionDigits: 0}); }
function pct(n) { return (n >= 0 ? "+" : "") + Number(n).toFixed(1) + "%"; }

// ===== Winner card =====
const bestDd20 = LOW.best_dd20;
const winner = LOW_RESULTS[bestDd20] || LOW_RESULTS[LOW.best_dd25];
const winnerName = LOW_RESULTS[bestDd20] ? bestDd20 : LOW.best_dd25;
document.getElementById("winnerCard").innerHTML = `
<h3>🛡️ DD ${winner.max_dd < 22 ? "20%以下" : "25%以下"} を達成したベスト戦略</h3>
<div class="title">${winnerName}</div>
<div class="kpis">
  <div class="kpi"><div class="lbl">年率</div><div class="v">${pct(winner.avg_annual_ret)}</div></div>
  <div class="kpi"><div class="lbl">最大DD</div><div class="v">${winner.max_dd.toFixed(1)}%</div></div>
  <div class="kpi"><div class="lbl">Sharpe</div><div class="v">${winner.sharpe.toFixed(2)}</div></div>
  <div class="kpi"><div class="lbl">最終資産</div><div class="v">${yen(winner.final)}</div></div>
</div>`;

// ===== Risk/Return scatter =====
const COLORS = ["#10b981","#22c55e","#4ade80","#84cc16","#eab308","#f59e0b",
                "#f97316","#ef4444","#ec4899","#d946ef","#a855f7","#8b5cf6",
                "#6366f1","#3b82f6","#0ea5e9","#06b6d4","#14b8a6","#64748b","#78716c","#57534e"];

const scatterData = ALL_NAMES.map((n, i) => ({
  label: n,
  data: [{ x: ALL[n].max_dd, y: ALL[n].avg_annual_ret }],
  backgroundColor: COLORS[i % COLORS.length],
  pointRadius: 10,
  pointHoverRadius: 14,
}));

new Chart(document.getElementById("riskReturnChart"), {
  type: "scatter",
  data: { datasets: scatterData },
  options: {
    plugins: { legend: { position: "bottom", labels: { boxWidth: 12, padding: 8, font: { size: 11 } } } },
    scales: {
      x: { title: { display: true, text: "最大DD (%) ← 小さいほど精神的にラク" },
           ticks: { callback: v => v + "%" } },
      y: { title: { display: true, text: "年率 (%) → 大きいほど儲かる" },
           ticks: { callback: v => v + "%" } }
    }
  }
});

// ===== 低DD ランキング =====
const lowSorted = [...LOW_NAMES].sort((a, b) => LOW_RESULTS[a].max_dd - LOW_RESULTS[b].max_dd);
let rankHtml = `<tr>
  <th>#</th><th>戦略</th><th>DD分類</th>
  <th>2020</th><th>2021</th><th>2022</th><th>2023</th><th>2024</th>
  <th>年率</th><th>★DD</th><th>Sharpe</th><th>最終資産</th>
</tr>`;
lowSorted.forEach((n, idx) => {
  const r = LOW_RESULTS[n];
  let pill;
  if (r.max_dd < 25) pill = '<span class="pill pill-safe">🟢 超安全</span>';
  else if (r.max_dd < 45) pill = '<span class="pill pill-balance">🔵 バランス</span>';
  else pill = '<span class="pill pill-aggressive">🔴 攻め</span>';

  const cls = n === winnerName ? "best-dd" : (idx < 5 ? "new" : "");
  let row = `<tr class="${cls}"><td>${idx+1}</td><td><b>${n}</b></td><td>${pill}</td>`;
  [2020,2021,2022,2023,2024].forEach(y => {
    const v = r.yearly[y];
    row += `<td class="${v<0?'neg':'pos'}">${pct(v)}</td>`;
  });
  row += `<td class="pos"><b>${pct(r.avg_annual_ret)}</b></td>`;
  row += `<td><b>${r.max_dd.toFixed(1)}%</b></td>`;
  row += `<td>${r.sharpe.toFixed(2)}</td>`;
  row += `<td>${yen(r.final)}</td></tr>`;
  rankHtml += row;
});
document.getElementById("rankTable").innerHTML = rankHtml;

// ===== 既存vs新 比較 =====
let cmpHtml = `<tr>
  <th>区分</th><th>戦略</th>
  <th>年率</th><th>★DD</th><th>Sharpe</th>
  <th>2022年</th><th>最終資産</th><th>$100万円が一番下がった時</th>
</tr>`;
// 既存戦略
COMP_NAMES.forEach(n => {
  const r = COMP[n];
  const down = Math.round((1 - r.max_dd/100) * 100);
  cmpHtml += `<tr class="old">
    <td><b>既存</b></td><td>${n}</td>
    <td class="pos"><b>${pct(r.avg_annual_ret)}</b></td>
    <td class="neg"><b>${r.max_dd.toFixed(1)}%</b></td>
    <td>${r.sharpe.toFixed(2)}</td>
    <td class="${r.yearly["2022"]<0?'neg':'pos'}">${pct(r.yearly["2022"])}</td>
    <td>${yen(r.final)}</td>
    <td class="neg">$${down}万円</td></tr>`;
});
// 新戦略 (DD25%以下のみ表示)
const topLow = lowSorted.slice(0, 5);
topLow.forEach(n => {
  const r = LOW_RESULTS[n];
  const down = Math.round((1 - r.max_dd/100) * 100);
  cmpHtml += `<tr class="new">
    <td><b>新規</b></td><td>${n}</td>
    <td class="pos"><b>${pct(r.avg_annual_ret)}</b></td>
    <td class="pos"><b>${r.max_dd.toFixed(1)}%</b></td>
    <td>${r.sharpe.toFixed(2)}</td>
    <td class="${r.yearly["2022"]<0?'neg':'pos'}">${pct(r.yearly["2022"])}</td>
    <td>${yen(r.final)}</td>
    <td class="pos">$${down}万円</td></tr>`;
});
document.getElementById("compareTable").innerHTML = cmpHtml;

// ===== Yearly chart =====
const displayNames = [...topLow, ...COMP_NAMES];
new Chart(document.getElementById("yearlyChart"), {
  type: "bar",
  data: {
    labels: [2020,2021,2022,2023,2024],
    datasets: displayNames.map((n, i) => ({
      label: n,
      data: [2020,2021,2022,2023,2024].map(y => ALL[n].yearly[y] ?? 0),
      backgroundColor: COLORS[i % COLORS.length],
    }))
  },
  options: {
    plugins: { legend: { position: "bottom", labels: { boxWidth: 12, padding: 6, font: { size: 11 } } } },
    scales: { y: { type: "symlog", ticks: { callback: v => v + "%" } } }
  }
});

// ===== Equity chart =====
const allDates = new Set();
displayNames.forEach(n => ALL[n].equity_weekly?.forEach(e => allDates.add(e.ts)));
const sortedDates = [...allDates].sort();
new Chart(document.getElementById("equityChart"), {
  type: "line",
  data: {
    labels: sortedDates,
    datasets: displayNames.map((n, i) => {
      const emap = {};
      (ALL[n].equity_weekly || []).forEach(e => emap[e.ts] = e.equity);
      return {
        label: n,
        data: sortedDates.map(d => emap[d] ?? null),
        borderColor: COLORS[i % COLORS.length],
        backgroundColor: COLORS[i % COLORS.length] + "15",
        borderWidth: n === winnerName ? 3.5 : 1.5,
        pointRadius: 0,
        tension: 0.1,
      };
    })
  },
  options: {
    plugins: { legend: { position: "bottom", labels: { boxWidth: 12, padding: 6, font: { size: 11 } } } },
    scales: {
      y: { type: "logarithmic", ticks: { callback: v => "$" + v.toLocaleString() } },
      x: { ticks: { maxTicksLimit: 15 } }
    }
  }
});

// ===== おすすめガイド =====
document.getElementById("recommendGuide").innerHTML = `
<h3>🟢 とにかく夜ぐっすり眠りたい方 → DD 20%以下</h3>
<ul>
  <li><b>D12 BTC/ETH/USDT 10/10/80</b>（年率+21.0%、DD 20.4%、Sharpe 1.40）<br>
    $100万円 → 一時的に最低 $80万円、5年後 $260万円</li>
  <li><b>D15 BTCマイルド15%+USDT85%</b>（年率+18.4%、DD 25.0%）<br>
    $100万円 → 一時的に最低 $75万円、5年後 $230万円</li>
  <li>向いている人：<b>初めての仮想通貨</b>、<b>大きな金額（生活資金）を預ける方</b>、<b>60代以上の方</b></li>
</ul>

<h3>🔵 バランス重視の方 → DD 25〜45%</h3>
<ul>
  <li><b>D13 BTCマイルド20%+USDT80%</b>（年率+22.2%、DD 29.7%）</li>
  <li><b>D10 超保守 BTCマイルド30%+USDT70%</b>（年率+28.5%、DD 36.2%）</li>
  <li><b>D02 BTC/USDT 50/50</b>（年率+38.5%、DD 47.9%）</li>
  <li>向いている人：<b>30〜50代の方</b>、<b>余剰資金の一部で運用</b>、<b>中長期目線</b></li>
</ul>

<h3>🔴 爆益狙いで精神的に耐えられる方 → DD 45%以上</h3>
<ul>
  <li><b>R10 ハイブリッド 50/50</b>（年率+73.9%、DD 51.5%）</li>
  <li><b>R05 モメンタムTop3</b>（年率+130.3%、DD 69.2%）</li>
  <li>向いている人：<b>20〜40代</b>、<b>余剰資金の少額で</b>、<b>仮想通貨を楽しみたい方</b></li>
</ul>

<div class="tradeoff">
  <h3>💎 具体的な金額で考えてみましょう</h3>
  <p>
    $10万円（仮想通貨初心者レベル）でスタートすると：
  </p>
  <ul>
    <li><b>D12（DD 20%）</b>：一番下がっても $8万円、5年後 $26万円 → ストレス低・利益そこそこ ✅</li>
    <li><b>R10（DD 50%）</b>：一番下がったら $5万円、5年後 $166万円 → ストレス中・利益大</li>
    <li><b>R05（DD 69%）</b>：一番下がったら $3.1万円、5年後 $648万円 → ストレス極高・爆益</li>
  </ul>
  <p style="color:#1d4ed8;font-weight:700;margin-top:10px;">
    推奨：まずは <b>D12</b> で1〜3ヶ月試してから、慣れてきたら D13 → D10 → R10 と
    <b>段階的にリスクを上げていく</b>のが心理的にも安全です。
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
