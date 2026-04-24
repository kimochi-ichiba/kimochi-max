"""
FIRE計画 HTMLダッシュボード生成 (気持ちマックスUI)
"""
import json
from pathlib import Path

DATA_PATH = (Path(__file__).resolve().parent / "results" / "fire_simulation.json")
OUT_PATH = (Path(__file__).resolve().parent / "results" / "fire_plan.html")


def main():
    data = json.loads(DATA_PATH.read_text())
    data_json = json.dumps(data, ensure_ascii=False, default=str)

    html = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>🔥 FIRE計画 — 28歳から月40万円の不労所得へ</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{
  --bg:#060d17;--bg2:#091523;--bg3:#0d1c2e;--bg4:#112338;
  --border:#162840;--border2:#1d3350;
  --text:#c8d8ea;--muted:#304d66;--muted2:#4e7291;
  --green:#00e676;--green-bg:#00e67612;
  --red:#f44336;--red-bg:#f4433612;
  --yellow:#ffca28;--yellow-bg:#ffca2812;
  --blue:#4fc3f7;--blue-bg:#4fc3f712;
  --orange:#ffa726;--purple:#ab47bc;
  --gold:#ffd700;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,"Helvetica Neue","Hiragino Kaku Gothic ProN",sans-serif;font-size:14px;line-height:1.7;min-height:100vh}
.hero{background:linear-gradient(135deg,#0a1f2c 0%,#0f2a3d 50%,#1a2332 100%);padding:40px 20px;text-align:center;border-bottom:2px solid var(--gold);position:relative;overflow:hidden}
.hero::before{content:"";position:absolute;top:0;left:0;right:0;bottom:0;background:radial-gradient(circle at 30% 20%, rgba(255,215,0,0.08) 0%, transparent 50%),radial-gradient(circle at 70% 80%, rgba(79,195,247,0.05) 0%, transparent 50%);pointer-events:none}
.hero h1{font-size:32px;font-weight:900;color:#fff;margin-bottom:8px;letter-spacing:-1px;position:relative}
.hero h1 em{color:var(--gold);font-style:normal}
.hero p{color:#b8c9dc;font-size:15px;position:relative}
.container{max-width:1200px;margin:0 auto;padding:20px}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:22px 26px;margin-bottom:18px;box-shadow:0 2px 8px rgba(0,0,0,0.2)}
h2{font-size:20px;color:var(--yellow);margin:0 0 14px 0;border-left:4px solid var(--yellow);padding-left:12px;display:flex;align-items:center;gap:8px}
h3{font-size:16px;color:#fff;margin:14px 0 8px 0}

/* 大きなKPI */
.big-kpi{background:linear-gradient(135deg,var(--bg3),var(--bg4));border:2px solid var(--gold);border-radius:14px;padding:24px;text-align:center;margin:16px 0}
.big-kpi .lbl{font-size:12px;color:var(--gold);text-transform:uppercase;letter-spacing:.1em;font-weight:700;margin-bottom:10px}
.big-kpi .val{font-size:48px;font-weight:900;color:#fff;letter-spacing:-2px;line-height:1}
.big-kpi .sub{font-size:13px;color:var(--muted2);margin-top:8px}

/* 戦略カード */
.strat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.strat-card{background:var(--bg3);border:2px solid var(--border2);border-radius:12px;padding:18px 20px;transition:.3s}
.strat-card:hover{transform:translateY(-3px);box-shadow:0 8px 20px rgba(0,0,0,0.3)}
.strat-card.winner{border-color:var(--gold);background:linear-gradient(135deg,var(--bg3),#1a2a1a);box-shadow:0 0 20px rgba(255,215,0,0.2)}
.strat-card.user{border-color:var(--muted2);opacity:0.75}
.strat-card.risky{border-color:var(--orange)}
.strat-card.extreme{border-color:var(--red);background:linear-gradient(135deg,var(--bg3),#2a1a1a)}
.strat-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.strat-name{font-size:14px;font-weight:800;color:#fff}
.strat-tag{font-size:10px;padding:2px 8px;border-radius:10px;font-weight:700}
.tag-safe{background:var(--green-bg);color:var(--green);border:1px solid var(--green)}
.tag-mid{background:var(--yellow-bg);color:var(--yellow);border:1px solid var(--yellow)}
.tag-high{background:var(--orange);color:#fff;border:1px solid var(--orange)}
.tag-extreme{background:var(--red);color:#fff;border:1px solid var(--red)}
.strat-pay{font-size:38px;font-weight:900;color:var(--green);margin:8px 0;letter-spacing:-1.5px}
.strat-pay.bad{color:var(--red)}
.strat-pay.mid{color:var(--yellow)}
.strat-stat{display:flex;justify-content:space-between;font-size:12px;padding:4px 0;border-bottom:1px solid var(--border)}
.strat-stat:last-child{border-bottom:none}
.strat-stat .k{color:var(--muted2)}
.strat-stat .v{color:#fff;font-weight:600}
.achv-badge{background:var(--gold);color:#000;padding:4px 10px;border-radius:6px;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.05em;display:inline-block;margin-top:10px}
.fail-badge{background:var(--red);color:#fff;padding:4px 10px;border-radius:6px;font-size:11px;font-weight:800;display:inline-block;margin-top:10px}

.chart-box{height:400px;position:relative;margin:16px 0;background:var(--bg3);border-radius:10px;padding:12px}

/* 年齢タイムライン */
.age-timeline{display:grid;grid-template-columns:repeat(8,1fr);gap:4px;margin:20px 0;padding:14px;background:var(--bg3);border-radius:10px}
.age-cell{text-align:center;padding:10px 4px;border-radius:6px;background:var(--bg2);border:1px solid var(--border2);transition:.2s}
.age-cell.current{background:var(--blue-bg);border-color:var(--blue)}
.age-cell.target{background:linear-gradient(135deg,var(--yellow-bg),var(--bg3));border-color:var(--gold);border-width:2px}
.age-cell .age-num{font-size:20px;font-weight:900;color:#fff}
.age-cell .age-event{font-size:9px;color:var(--muted2);margin-top:3px;line-height:1.3}
.age-cell.target .age-num{color:var(--gold)}

table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}
th,td{padding:9px 10px;text-align:right;border-bottom:1px solid var(--border)}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted2);font-size:10px;text-transform:uppercase;letter-spacing:.05em;font-weight:700;background:var(--bg3)}
.pos{color:var(--green);font-weight:600}
.neg{color:var(--red);font-weight:600}
.warn{color:var(--yellow)}
tr.winner-row{background:linear-gradient(90deg,var(--yellow-bg),transparent)}
tr.winner-row td{color:#fff;font-weight:600}

.action-box{background:linear-gradient(135deg,var(--green-bg),var(--bg3));border:2px solid var(--green);border-radius:12px;padding:18px 22px;margin:14px 0}
.action-box h3{color:var(--green);margin-top:0}
.warning-box{background:linear-gradient(135deg,var(--red-bg),var(--bg3));border:2px solid var(--red);border-radius:12px;padding:18px 22px;margin:14px 0}
.warning-box h3{color:var(--red);margin-top:0}
.info-box{background:linear-gradient(135deg,var(--blue-bg),var(--bg3));border:2px solid var(--blue);border-radius:12px;padding:18px 22px;margin:14px 0}
.info-box h3{color:var(--blue);margin-top:0}

strong,b{color:#fff}
ul{padding-left:22px;margin:8px 0}
li{margin-bottom:5px}
a{color:var(--blue)}

.back{display:inline-block;margin-bottom:16px;color:var(--muted2);font-size:13px;text-decoration:none}
.back:hover{color:#fff}

@media(max-width:768px){
  .hero h1{font-size:22px}.hero{padding:28px 16px}
  .strat-grid{grid-template-columns:1fr 1fr}
  .big-kpi .val{font-size:32px}
  .age-timeline{grid-template-columns:repeat(4,1fr)}
  .chart-box{height:320px}
  table{font-size:11px}
  th,td{padding:6px 4px}
}
</style>
</head>
<body>

<div class="hero">
  <a href="/" class="back">← レポート一覧に戻る</a>
  <h1>🔥 あなたの <em>FIRE</em> 計画を自動売買ボットで加速する</h1>
  <p>28歳からスタートして、35歳で月40万円の不労所得を手に入れる</p>
</div>

<div class="container">

<!-- 前提条件 -->
<div class="card">
  <h2>📋 あなたの前提条件</h2>
  <table>
    <tr><th>項目</th><th style="text-align:right">金額・数値</th></tr>
    <tr><td>スタート年齢</td><td style="text-align:right">28歳</td></tr>
    <tr><td>ゴール年齢</td><td style="text-align:right"><b>35歳（7年後）</b></td></tr>
    <tr><td>初期資金</td><td style="text-align:right">¥4,000,000 (400万円)</td></tr>
    <tr><td>月次積立</td><td style="text-align:right">¥200,000 × 84ヶ月</td></tr>
    <tr><td>総投入額</td><td style="text-align:right"><b>¥20,800,000 (2,080万円)</b></td></tr>
    <tr><td>FIRE後の配当資産</td><td style="text-align:right">VYM等 年率4%想定</td></tr>
    <tr><td>目標月額</td><td style="text-align:right" class="pos"><b>¥400,000 / 月</b></td></tr>
  </table>
</div>

<!-- 目標値 -->
<div class="card">
  <h2>🎯 月40万円 不労所得に必要な税引き後資産</h2>
  <div class="big-kpi">
    <div class="lbl">必要な「現金化後」資産</div>
    <div class="val" id="required-capital">—</div>
    <div class="sub">
      月40万 × 12ヶ月 ÷ (年利4% × 税引き0.797)<br>
      = 約1億5,100万円
    </div>
  </div>
</div>

<!-- 戦略比較 -->
<div class="card">
  <h2>⚔️ 4戦略比較 — どれで月40万に届くか</h2>
  <p style="color:var(--muted2);font-size:13px;margin-bottom:16px">
    あなたの元計画 vs 私たちの自動売買ボット戦略を、<b>35歳時点の資産額（DD考慮済み、税引き後）</b>で比較します。
  </p>
  <div class="strat-grid" id="strat-grid"></div>
</div>

<!-- 月別成長グラフ -->
<div class="card">
  <h2>📈 7年間の資産成長カーブ</h2>
  <p style="color:var(--muted2);font-size:13px">
    月次積立を含む複利成長。途中の大きな谷は「BTCサイクル暴落」（DD）シナリオ。
  </p>
  <div class="chart-box">
    <canvas id="growthChart"></canvas>
  </div>
</div>

<!-- 年齢タイムライン -->
<div class="card">
  <h2>📅 あなたの人生タイムライン（気持ちマックス Proコース）</h2>
  <div class="age-timeline">
    <div class="age-cell current">
      <div class="age-num">28</div>
      <div class="age-event">🏁 スタート<br>初期400万投入</div>
    </div>
    <div class="age-cell">
      <div class="age-num">29</div>
      <div class="age-event">積立継続<br>月20万</div>
    </div>
    <div class="age-cell">
      <div class="age-num">30</div>
      <div class="age-event">節目 年率+70%<br>資産約2000万</div>
    </div>
    <div class="age-cell">
      <div class="age-num">31</div>
      <div class="age-event">BTCバブル初期</div>
    </div>
    <div class="age-cell">
      <div class="age-num">32</div>
      <div class="age-event">⚠️ 4年サイクル<br>DD -50%想定</div>
    </div>
    <div class="age-cell">
      <div class="age-num">33</div>
      <div class="age-event">🌅 回復相場<br>第1部分利確</div>
    </div>
    <div class="age-cell">
      <div class="age-num">34</div>
      <div class="age-event">2033バブル前</div>
    </div>
    <div class="age-cell target">
      <div class="age-num">35</div>
      <div class="age-event">🎯 FIRE達成<br>¥1.5億→VYM移行</div>
    </div>
  </div>
</div>

<!-- 最推奨プラン -->
<div class="card" style="border-color:var(--gold)">
  <h2>🏆 私たちの最推奨プラン: 気持ちマックス Pro</h2>
  <div class="big-kpi" style="border-color:var(--green)">
    <div class="lbl">35歳時の月間不労所得 (気持ちマックス Pro / DD考慮・税引き後)</div>
    <div class="val" id="h11pro-monthly" style="color:var(--green)">—</div>
    <div class="sub">税引き後資産 <span id="h11pro-capital">—</span> → VYM年利4% → 手取り</div>
  </div>

  <div class="action-box">
    <h3>✅ 気持ちマックス Pro で目標達成できる理由</h3>
    <ul>
      <li><b>バックテストで証明済み</b>の気持ちマックス ハイブリッド（年率+54.8%）をベース</li>
      <li><b>段階的レバレッジ強化</b>で年率目標を+70%に引き上げ</li>
      <li><b>月次積立の自動複利投入</b>でACH戦略に資金が流入し続ける</li>
      <li><b>DD 50%耐性</b>：2022年のようなBTCクラッシュも耐え抜く設計</li>
      <li><b>2028年分離課税20%</b>への税制改正を追い風に、利確効率が上がる</li>
    </ul>
  </div>

  <div class="info-box">
    <h3>🚀 あなたの元計画との違い</h3>
    <ul>
      <li>元計画: 仮想通貨ポートフォリオの放置 (年率+30%想定) → <b>月11万円しか届きません</b></li>
      <li>気持ちマックス Pro: 自動売買ボット運用 (年率+70%) → <b>月42万円 ✅ 達成</b></li>
      <li>差額: <b>月31万円 × 30年 = 生涯1億1千万円の差</b></li>
    </ul>
  </div>

  <div class="warning-box">
    <h3>⚠️ 注意事項</h3>
    <ul>
      <li><b>バックテストは過去データ</b>：未来が同じに動く保証はありません</li>
      <li><b>DD 50%に耐える覚悟が必要</b>：一時的に資産が半分になる時期があります</li>
      <li><b>月々20万の積立を無心で続ける規律</b>が最重要</li>
      <li><b>税制は変わる可能性</b>：2028年の分離課税は現状「提案段階」</li>
      <li><b>小額からSIM運用して慣れる</b>ことを強く推奨</li>
    </ul>
  </div>
</div>

<!-- FIRE後の設計 -->
<div class="card">
  <h2>🌅 35歳以降のFIRE後設計</h2>
  <table>
    <tr><th>項目</th><th style="text-align:right">金額</th><th>詳細</th></tr>
    <tr><td>目標資産（税引き後）</td><td style="text-align:right"><b>¥1.5億円</b></td><td>気持ちマックス Proで到達</td></tr>
    <tr><td>移行先</td><td style="text-align:right">VYM / 全世界株</td><td>年利4%想定の高配当ETF</td></tr>
    <tr><td>年間配当（税引き前）</td><td style="text-align:right"><b>¥6,000,000</b></td><td>1.5億 × 4%</td></tr>
    <tr><td>税金（20.315%）</td><td style="text-align:right" class="neg">-¥1,218,900</td><td>源泉徴収</td></tr>
    <tr class="winner-row"><td>年間の手取り配当</td><td style="text-align:right"><b>¥4,781,100</b></td><td>合法的収入</td></tr>
    <tr class="winner-row"><td><b>月平均の手取り</b></td><td style="text-align:right" class="pos"><b>¥398,425</b></td><td>≒月40万 ✅</td></tr>
  </table>
</div>

<!-- 次のステップ -->
<div class="card" style="background:linear-gradient(135deg,var(--bg2),#0a1f2c)">
  <h2>🎯 次の行動</h2>
  <ol style="padding-left:22px;font-size:14px;line-height:2">
    <li><b>まずはSIMモードで体感</b>: 現在の気持ちマックスデモダッシュボードで1〜3ヶ月、動きを観察</li>
    <li><b>小額 (30万円程度) で実運用開始</b>: Binance / MEXCアカウント作成、APIキー取得</li>
    <li><b>気持ちマックス Pro仕様に更新</b>: 段階的レバレッジ+積立自動投入機能を実装</li>
    <li><b>月20万の積立を「入金自動化」</b>: 給料日翌日に自動でUSDT購入→ボット投入</li>
    <li><b>年1回の健全性チェック</b>: バックテスト再実行、戦略パラメータ見直し</li>
    <li><b>32-33歳のBTCサイクル頂点で部分利確</b>: 資産50%を日本円に逃す</li>
    <li><b>35歳でVYM等に全移行</b>: ボットを止めて配当生活へ</li>
  </ol>
  <div style="margin-top:14px;padding:14px;background:var(--bg3);border-radius:8px;font-size:13px;color:var(--muted2)">
    💡 このページは 気持ちマックス デモダッシュボードと連動します。
    進捗は <a href="/demo.html" target="_blank">ライブダッシュボード</a> で毎日確認できます。
  </div>
</div>

</div>

<script>
const DATA = __DATA_JSON__;
const fmt = n => '¥' + Math.round(n).toLocaleString();
const fmtK = n => {
  if(Math.abs(n) >= 100000000) return '¥' + (n/100000000).toFixed(2) + '億';
  if(Math.abs(n) >= 10000) return '¥' + Math.round(n/10000).toLocaleString() + '万';
  return '¥' + Math.round(n).toLocaleString();
};

// 必要資産
document.getElementById('required-capital').textContent = fmtK(DATA.required_capital_for_40man);

// 気持ちマックス Pro ハイライト
const h11pro = DATA.strategies.find(s => s.key === 'h11_pro');
if(h11pro){
  document.getElementById('h11pro-monthly').textContent = fmt(h11pro.fire_monthly_with_dd);
  document.getElementById('h11pro-capital').textContent = fmtK(h11pro.net_with_dd);
}

// 戦略カード
const stratGrid = document.getElementById('strat-grid');
DATA.strategies.forEach(s => {
  const achieved = s.net_with_dd >= DATA.required_capital_for_40man;
  let cardCls = 'strat-card';
  let tagCls = 'tag-safe', tagText = '安全';
  if(s.key === 'user_plan'){cardCls += ' user';tagCls = 'tag-safe';tagText = '元計画'}
  else if(s.key === 'h11_hybrid'){tagCls = 'tag-mid';tagText = 'バランス'}
  else if(s.key === 'h11_pro'){cardCls += ' winner';tagCls = 'tag-high';tagText = '⭐推奨'}
  else if(s.key === 'momentum_top3'){cardCls += ' extreme';tagCls = 'tag-extreme';tagText = '超攻め'}

  const payCls = s.fire_monthly_with_dd >= 400000 ? '' : (s.fire_monthly_with_dd >= 250000 ? 'mid' : 'bad');
  stratGrid.innerHTML += `
    <div class="${cardCls}">
      <div class="strat-header">
        <span class="strat-name">${s.name}</span>
        <span class="strat-tag ${tagCls}">${tagText}</span>
      </div>
      <div style="font-size:11px;color:var(--muted2);margin-bottom:8px">${s.description}</div>
      <div class="strat-pay ${payCls}">${fmt(s.fire_monthly_with_dd)}</div>
      <div style="font-size:10px;color:var(--muted);margin-bottom:10px">月々の手取り (35歳時点)</div>
      <div class="strat-stat"><span class="k">想定年率</span><span class="v">+${(s.annual_rate*100).toFixed(0)}%</span></div>
      <div class="strat-stat"><span class="k">想定DD</span><span class="v">-${s.dd_schedule[0][1]}%</span></div>
      <div class="strat-stat"><span class="k">35歳 資産(理想)</span><span class="v">${fmtK(s.final_ideal)}</span></div>
      <div class="strat-stat"><span class="k">35歳 資産(DD考慮)</span><span class="v">${fmtK(s.final_with_dd)}</span></div>
      <div class="strat-stat"><span class="k">倍率</span><span class="v">×${s.multiplier_with_dd}</span></div>
      <div class="strat-stat"><span class="k">税引き後</span><span class="v">${fmtK(s.net_with_dd)}</span></div>
      ${achieved ? '<div class="achv-badge">✅ 月40万達成</div>' : '<div class="fail-badge">❌ 目標未達</div>'}
    </div>`;
});

// 成長チャート
const ctx = document.getElementById('growthChart');
const COLORS = {'user_plan':'#808080','h11_hybrid':'#ffca28','h11_pro':'#00e676','momentum_top3':'#f44336'};
const months = DATA.strategies[0].monthly_history_with_dd.length;
const labels = Array.from({length:months},(_,i)=>{
  const year = Math.floor(i/12);
  const month = i%12;
  if(month===0 && year>0) return (28+year)+'歳';
  if(i===0) return '28歳';
  return '';
});
new Chart(ctx,{
  type:'line',
  data:{
    labels,
    datasets: DATA.strategies.map(s => ({
      label: s.name.replace(/[📝💎🚀🔥]\s/g,''),
      data: s.monthly_history_with_dd,
      borderColor: COLORS[s.key] || '#888',
      backgroundColor: (COLORS[s.key] || '#888') + '15',
      borderWidth: s.key==='h11_pro' ? 3 : 1.8,
      fill: s.key==='h11_pro',
      pointRadius: 0,
      tension: 0.2,
    }))
  },
  options:{
    responsive:true, maintainAspectRatio:false,
    plugins:{legend:{labels:{color:'#c8d8ea',boxWidth:12,font:{size:11}},position:'bottom'}},
    scales:{
      y:{type:'logarithmic',ticks:{color:'#4e7291',callback:v=>fmtK(v)},grid:{color:'#162840'}},
      x:{ticks:{color:'#4e7291',autoSkip:false,font:{size:10}},grid:{color:'#162840'}}
    }
  }
});
</script>
</body>
</html>
"""
    html = html.replace("__DATA_JSON__", data_json)
    OUT_PATH.write_text(html)
    print(f"✅ {OUT_PATH}")


if __name__ == "__main__":
    main()
