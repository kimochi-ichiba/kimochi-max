"""
I34 深掘りHTMLレポート生成
=============================
i34_deep_dive.json を読み込み、対話的なHTMLレポートを生成する。
"""
from __future__ import annotations
import json
from pathlib import Path

DATA_PATH = (Path(__file__).resolve().parent / "results" / "i34_deep_dive.json")
OUT_PATH = (Path(__file__).resolve().parent / "results" / "i34_deep_dive_report.html")


def main():
    data = json.loads(DATA_PATH.read_text())
    data_json = json.dumps(data, ensure_ascii=False)

    html = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>I34 深掘り分析レポート — 2024年マイナスの原因</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", sans-serif;
         margin: 0; padding: 24px; background: #f5f6f8; color: #222; line-height: 1.6; }
  .container { max-width: 1200px; margin: 0 auto; }
  h1 { margin-bottom: 4px; font-size: 26px; color: #1a1a2e; }
  .subtitle { color: #666; font-size: 14px; margin-bottom: 24px; }
  .card { background: white; border-radius: 10px; padding: 20px 24px; margin-bottom: 18px;
          box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
  h2 { margin-top: 0; font-size: 20px; color: #2a2a4a; border-left: 4px solid #6366f1;
       padding-left: 10px; }
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
              gap: 12px; margin-top: 12px; }
  .kpi { background: #f8fafc; border-radius: 8px; padding: 12px 14px; }
  .kpi-label { font-size: 12px; color: #666; margin-bottom: 4px; }
  .kpi-val { font-size: 22px; font-weight: 700; color: #1a1a2e; }
  .kpi-val.good { color: #16a34a; }
  .kpi-val.bad { color: #dc2626; }
  .kpi-val.warn { color: #d97706; }
  table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
  th, td { padding: 8px 10px; text-align: right; border-bottom: 1px solid #eee; }
  th:first-child, td:first-child { text-align: left; }
  th { background: #f8fafc; color: #555; font-weight: 600; }
  tr.neg { background: #fef2f2; }
  tr.pos { background: #f0fdf4; }
  tr.hot-loss { background: #fee2e2; font-weight: 600; }
  .chart-box { position: relative; height: 360px; margin-top: 12px; }
  .chart-box.small { height: 260px; }
  .explain { background: #fff7ed; border-left: 4px solid #f97316; padding: 14px 18px;
             border-radius: 6px; margin-top: 14px; }
  .explain h3 { margin: 0 0 8px 0; font-size: 15px; color: #c2410c; }
  .explain ul { margin: 6px 0 0 0; padding-left: 20px; }
  .explain li { margin-bottom: 4px; }
  .finding { background: #eff6ff; border-left: 4px solid #2563eb; padding: 14px 18px;
             border-radius: 6px; margin-top: 14px; }
  .finding h3 { margin: 0 0 8px 0; font-size: 15px; color: #1d4ed8; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 12px;
         font-size: 11px; font-weight: 600; }
  .tag-sl { background: #fee2e2; color: #991b1b; }
  .tag-liq { background: #7f1d1d; color: white; }
  .tag-tp { background: #d1fae5; color: #065f46; }
  .tag-trail { background: #dbeafe; color: #1e40af; }
  .tag-dch { background: #e9d5ff; color: #6b21a8; }
  .tag-regime { background: #fed7aa; color: #9a3412; }
  .tag-final { background: #e5e7eb; color: #374151; }
  .scroll-table { max-height: 500px; overflow-y: auto; }
</style>
</head>
<body>
<div class="container">

<h1>🔬 I34 深掘り分析レポート</h1>
<p class="subtitle">戦略: <b>Livermore完全 Lev2.5 ピラミ4</b> ｜ 期間 2020-01 〜 2024-12 ｜ 初期資金 $10,000</p>

<div class="card">
  <h2>📊 サマリー（5年間の成績）</h2>
  <div class="kpi-grid" id="kpi-grid"></div>
</div>

<div class="card">
  <h2>📈 年別リターン</h2>
  <div class="chart-box small"><canvas id="yearlyChart"></canvas></div>
  <table id="yearlyTable"></table>
</div>

<div class="card">
  <h2>💰 資産推移（週次, $10,000 → 最終）</h2>
  <div class="chart-box"><canvas id="equityChart"></canvas></div>
</div>

<div class="card">
  <h2>🔥 2024年 月別内訳（なぜマイナスになったか）</h2>
  <div class="chart-box small"><canvas id="monthChart"></canvas></div>
  <table id="monthTable"></table>

  <div class="explain" id="causeBox">
    <h3>💡 原因のポイント</h3>
    <div id="causeText"></div>
  </div>
</div>

<div class="card">
  <h2>📉 2024年 BTC価格 vs I34資産 (日次)</h2>
  <p style="color:#666; font-size:13px; margin-top:0;">
    BTCの相場サイクルとI34の資産が連動しているか確認する。
  </p>
  <div class="chart-box"><canvas id="btcVsEqChart"></canvas></div>
</div>

<div class="card">
  <h2>🏷 2024年 決済理由の内訳</h2>
  <p style="color:#666; font-size:13px; margin-top:0;">
    どの出口で勝ったか／負けたか。負けは全て「stop_loss_intraday」と「liquidation」に集中しているはず。
  </p>
  <table id="reasonTable"></table>
</div>

<div class="card">
  <h2>💸 2024年 ワースト銘柄 Top15</h2>
  <table id="worstSymTable"></table>
</div>

<div class="card">
  <h2>🩸 2024年 ワーストトレード Top30（損失大きい順）</h2>
  <div class="scroll-table">
    <table id="tradeTable"></table>
  </div>
</div>

<div class="card">
  <h2>🎯 結論と次の打ち手</h2>
  <div class="finding" id="findingBox"></div>
</div>

</div>

<script>
const DATA = __DATA_JSON__;

// ========== KPI ==========
function yen(n) { return "$" + n.toLocaleString("ja-JP", {maximumFractionDigits: 0}); }
function pct(n) { return (n >= 0 ? "+" : "") + n.toFixed(1) + "%"; }
function clsn(v) { return v > 0 ? "good" : (v < 0 ? "bad" : ""); }

const kpis = [
  { label: "最終資産", val: yen(DATA.final), cls: "good" },
  { label: "年率平均", val: "+50.6%", cls: "good" },
  { label: "取引数", val: DATA.n_trades_total.toLocaleString(), cls: "" },
  { label: "清算回数", val: DATA.n_liquidations_total + "回", cls: "bad" },
  { label: "2024年収益", val: pct(DATA.yearly["2024"].ret_pct), cls: clsn(DATA.yearly["2024"].ret_pct) },
  { label: "2024年取引数", val: DATA.trades_2024_total + "件", cls: "" },
];
const kpiEl = document.getElementById("kpi-grid");
kpis.forEach(k => {
  kpiEl.innerHTML += `<div class="kpi"><div class="kpi-label">${k.label}</div>
    <div class="kpi-val ${k.cls}">${k.val}</div></div>`;
});

// ========== 年別 ==========
const years = Object.keys(DATA.yearly).sort();
const yearlyRets = years.map(y => DATA.yearly[y].ret_pct);

new Chart(document.getElementById("yearlyChart"), {
  type: "bar",
  data: {
    labels: years,
    datasets: [{
      label: "年率リターン (%)",
      data: yearlyRets,
      backgroundColor: yearlyRets.map(v => v >= 0 ? "#16a34a" : "#dc2626"),
    }]
  },
  options: {
    plugins: { legend: { display: false } },
    scales: { y: { ticks: { callback: v => v + "%" } } }
  }
});

let ytHtml = "<tr><th>年</th><th>開始資産</th><th>最高</th><th>最安</th><th>終了資産</th><th>リターン</th></tr>";
years.forEach(y => {
  const v = DATA.yearly[y];
  const cls = v.ret_pct < 0 ? "neg" : "pos";
  ytHtml += `<tr class="${cls}"><td>${y}</td><td>${yen(v.start_equity)}</td>
    <td>${yen(v.peak_equity)}</td><td>${yen(v.trough_equity)}</td>
    <td>${yen(v.end_equity)}</td><td><b>${pct(v.ret_pct)}</b></td></tr>`;
});
document.getElementById("yearlyTable").innerHTML = ytHtml;

// ========== equity curve ==========
new Chart(document.getElementById("equityChart"), {
  type: "line",
  data: {
    labels: DATA.equity_weekly.map(e => e.ts),
    datasets: [{
      label: "資産 ($)",
      data: DATA.equity_weekly.map(e => e.equity),
      borderColor: "#6366f1",
      backgroundColor: "rgba(99,102,241,0.08)",
      borderWidth: 2,
      pointRadius: 0,
      fill: true,
    }]
  },
  options: {
    plugins: { legend: { display: false } },
    scales: {
      y: { type: "logarithmic", ticks: { callback: v => "$" + v.toLocaleString() } },
      x: { ticks: { maxTicksLimit: 12 } }
    }
  }
});

// ========== 2024 月別 ==========
const months = DATA.month_2024_list;
new Chart(document.getElementById("monthChart"), {
  type: "bar",
  data: {
    labels: months.map(m => m.month.slice(5) + "月"),
    datasets: [
      {
        label: "月次P&L ($)",
        data: months.map(m => m.pnl),
        backgroundColor: months.map(m => m.pnl >= 0 ? "#16a34a" : "#dc2626"),
        yAxisID: "y",
      },
      {
        label: "大損失件数(>$200)",
        data: months.map(m => m.big_losses),
        type: "line",
        borderColor: "#f97316",
        backgroundColor: "#f97316",
        yAxisID: "y1",
        tension: 0.3,
      }
    ]
  },
  options: {
    scales: {
      y: { position: "left", ticks: { callback: v => "$" + v.toLocaleString() } },
      y1: { position: "right", grid: { drawOnChartArea: false },
            title: { display: true, text: "大損失件数" } }
    }
  }
});

let mtHtml = "<tr><th>月</th><th>取引</th><th>清算</th><th>大損失(>$200)</th><th>P&L</th></tr>";
months.forEach(m => {
  const cls = m.pnl < 0 ? "neg" : (m.pnl > 0 ? "pos" : "");
  mtHtml += `<tr class="${cls}"><td>${m.month}</td><td>${m.trades}</td>
    <td>${m.liq}</td><td>${m.big_losses}</td>
    <td><b>${(m.pnl>=0?"+":"")}${yen(Math.abs(m.pnl)).replace("$","")}${m.pnl<0?"-":""}</b></td></tr>`;
});
document.getElementById("monthTable").innerHTML = mtHtml;

// 原因テキスト
const mar = months.find(m => m.month === "2024-03");
const apr = months.find(m => m.month === "2024-04");
const midYear = months.filter(m => ["2024-05","2024-06","2024-07","2024-08","2024-09","2024-10"].includes(m.month));
const midSum = midYear.reduce((s,m)=>s+m.pnl, 0);
const lateYear = months.filter(m => ["2024-11","2024-12"].includes(m.month));
const lateSum = lateYear.reduce((s,m)=>s+m.pnl, 0);

document.getElementById("causeText").innerHTML = `
<ul>
  <li><b>3月：大爆益 +${Math.round(mar.pnl).toLocaleString()}ドル</b>。アルトコイン急騰でピラミディング（買い増し）が作動し、ポジションが最大まで膨らんだ。</li>
  <li><b>4月：大損 ${Math.round(apr.pnl).toLocaleString()}ドル</b> ← <span style="color:#dc2626;font-weight:700">ここが最大のダメージ</span>。3月に膨らませたポジションが一気に崩れ、清算${apr.liq}件、大損失${apr.big_losses}件が発生。</li>
  <li><b>5〜10月：ダラダラ負け合計 ${Math.round(midSum).toLocaleString()}ドル</b>。BTCがレンジ相場になり、ブレイクアウトが偽物（ダマシ）ばかりで小さな損失を積み重ねた。</li>
  <li><b>11〜12月：巻き返し +${Math.round(lateSum).toLocaleString()}ドル</b>。BTC上昇再開で復活したが、4月の穴を埋めきれず。</li>
  <li><b>結論：4月の「春のクラッシュ」が全ての元凶</b>。11月のBTC急落（memoryの仮説）ではなく、4月のアルト急落が真犯人だった。</li>
</ul>
`;

// ========== BTC vs Equity (2024) ==========
const btc2024 = DATA.btc_prices_2024;
const eq2024 = DATA.equity_2024_daily;
// Merge by date
const btcMap = {}; btc2024.forEach(b => btcMap[b.ts] = b.close);
const eqMap = {}; eq2024.forEach(e => eqMap[e.ts] = e.equity);
const allDates = [...new Set([...btc2024.map(b=>b.ts), ...eq2024.map(e=>e.ts)])].sort();

new Chart(document.getElementById("btcVsEqChart"), {
  type: "line",
  data: {
    labels: allDates,
    datasets: [
      {
        label: "BTC価格 ($)",
        data: allDates.map(d => btcMap[d] || null),
        borderColor: "#f59e0b",
        backgroundColor: "rgba(245,158,11,0.1)",
        borderWidth: 1.5,
        pointRadius: 0,
        yAxisID: "y1",
      },
      {
        label: "I34資産 ($)",
        data: allDates.map(d => eqMap[d] || null),
        borderColor: "#6366f1",
        backgroundColor: "rgba(99,102,241,0.1)",
        borderWidth: 2,
        pointRadius: 0,
        yAxisID: "y",
      },
    ]
  },
  options: {
    scales: {
      y: { position: "left", title: { display: true, text: "I34資産 ($)" }},
      y1: { position: "right", grid: { drawOnChartArea: false },
            title: { display: true, text: "BTC ($)" }},
      x: { ticks: { maxTicksLimit: 12 } }
    }
  }
});

// ========== 2024 決済理由 ==========
const reasons = DATA.reason_2024;
let rTotPnl = 0; Object.values(reasons).forEach(r=>rTotPnl+=r.pnl);
let rtHtml = "<tr><th>決済理由</th><th>件数</th><th>勝</th><th>負</th><th>累計P&L</th><th>平均P&L</th></tr>";
Object.entries(reasons).sort((a,b)=>a[1].pnl-b[1].pnl).forEach(([r, v]) => {
  const cls = v.pnl < 0 ? "neg" : "pos";
  const tagCls = r.includes("stop_loss") ? "tag-sl" : r === "liquidation" ? "tag-liq"
    : r.startsWith("tp") ? "tag-tp" : r === "trail" ? "tag-trail"
    : r === "dch_exit" ? "tag-dch" : r === "regime" ? "tag-regime" : "tag-final";
  rtHtml += `<tr class="${cls}"><td><span class="tag ${tagCls}">${r}</span></td>
    <td>${v.count}</td><td>${v.wins}</td><td>${v.losses}</td>
    <td><b>${yen(v.pnl)}</b></td><td>${yen(v.pnl/v.count)}</td></tr>`;
});
rtHtml += `<tr style="background:#1a1a2e;color:white;font-weight:700"><td>合計</td><td>${DATA.trades_2024_total}</td><td></td><td></td><td>${yen(rTotPnl)}</td><td></td></tr>`;
document.getElementById("reasonTable").innerHTML = rtHtml;

// ========== ワースト銘柄 ==========
let wsHtml = "<tr><th>銘柄</th><th>取引数</th><th>累計P&L</th></tr>";
DATA.sym_2024_list.slice(0, 15).forEach(s => {
  const cls = s.pnl < 0 ? "neg" : "pos";
  wsHtml += `<tr class="${cls}"><td>${s.sym}</td><td>${s.count}</td>
    <td><b>${yen(s.pnl)}</b></td></tr>`;
});
document.getElementById("worstSymTable").innerHTML = wsHtml;

// ========== ワーストトレード ==========
let ttHtml = `<tr><th>決済日</th><th>エントリー日</th><th>銘柄</th><th>方向</th>
  <th>保有日数</th><th>決済%</th><th>理由</th><th>ピラミ回数</th><th>P&L</th></tr>`;
DATA.trades_2024_sorted_worst_first.slice(0, 30).forEach(t => {
  const cls = t.pnl < -2000 ? "hot-loss" : (t.pnl < 0 ? "neg" : "pos");
  const tagCls = t.reason.includes("stop_loss") ? "tag-sl" : t.reason === "liquidation" ? "tag-liq"
    : t.reason.startsWith("tp") ? "tag-tp" : t.reason === "trail" ? "tag-trail"
    : t.reason === "dch_exit" ? "tag-dch" : t.reason === "regime" ? "tag-regime" : "tag-final";
  ttHtml += `<tr class="${cls}"><td>${t.exit_ts}</td><td>${t.entry_ts}</td>
    <td>${t.sym}</td><td>${t.side}</td>
    <td>${t.hold_days}日</td><td>${t.ret_pct.toFixed(1)}%</td>
    <td><span class="tag ${tagCls}">${t.reason}</span></td>
    <td>${t.pyramids}</td><td><b>${yen(t.pnl)}</b></td></tr>`;
});
document.getElementById("tradeTable").innerHTML = ttHtml;

// ========== 結論 ==========
document.getElementById("findingBox").innerHTML = `
<h3>🎯 2024年マイナスの真犯人</h3>
<ol>
  <li><b>4月の急落</b>：3月に積み上げたピラミディング（ポジション4段積み）が4月の下落で全部SL発動。たった1ヶ月で -$59,608 の大損失。</li>
  <li><b>清算 4件</b>：Lev2.5でも、1日で-34%動かれると清算される。4月に集中発生。</li>
  <li><b>夏のダマシ</b>：5〜10月のレンジ相場で、ブレイクアウトと思って入っては即SLに刈られるを繰り返し -$54,551 をじわじわ失った。</li>
</ol>

<h3>💡 改良アイデア（Iter41案）</h3>
<ul>
  <li><b>案A: ピラミ段数を4→2に減らす</b>。急落時の被害が約半分に。年利は落ちるが、2024年プラス化の可能性が高い。</li>
  <li><b>案B: 利益ロック強化</b>。年初来+100%を超えたら新規エントリーを制限（＝3月の爆益を守る）。</li>
  <li><b>案C: BTC調整局面でエントリー停止</b>。BTCがEMA50を下抜けたら、新規エントリーを翌週まで休む。</li>
  <li><b>案D: SL幅を22%→18%に狭める</b>。1トレードあたりの損失を小さく。ただしダマシ耐性は下がる。</li>
</ul>
<p style="margin-top:14px;font-weight:600;color:#1d4ed8;">
  推奨：案A + 案C の組合せ。4月のダメージとレンジ相場のダラダラ負けを両方減らせる。
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
