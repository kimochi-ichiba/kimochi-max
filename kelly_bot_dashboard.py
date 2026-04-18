"""
Kelly Bot ダッシュボード
- 手動スキャン（今すぐKelly計算）
- 手動リバランス
- ポジション状態表示
- iPhone対応の大きなボタン
"""
import os
import sys
import threading
from flask import Flask, jsonify, render_template_string, request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kelly_bot import KellyBot, Config, setup_logger


# ── ボット初期化 ──
logger = setup_logger()
bot_cfg = Config(mode="paper", initial_capital=3000.0, state_file="kelly_bot_state.json")
bot = KellyBot(bot_cfg, logger)
_lock = threading.Lock()

app = Flask(__name__)


HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🤖 Kelly Bot 手動スキャン</title>
<style>
  :root {
    --bg:#0a0a0a; --card:#1a1a1a; --card2:#242424;
    --text:#e0e0e0; --muted:#888; --border:#333;
    --green:#2e7d32; --red:#c62828; --blue:#1976d2; --yellow:#f57f17;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; padding:12px;
    background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;
    font-size:15px; line-height:1.5;
  }
  h1 { font-size:22px; margin:8px 0 16px; }
  h2 { font-size:17px; margin:16px 0 8px; color:var(--muted); }
  .card {
    background:var(--card); padding:16px;
    border-radius:14px; margin-bottom:14px;
    border:1px solid var(--border);
  }
  .status-grid {
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:10px;
  }
  .stat {
    background:var(--card2); padding:12px;
    border-radius:10px;
  }
  .stat-lbl { font-size:11px; color:var(--muted); margin-bottom:4px; }
  .stat-val { font-size:20px; font-weight:700; }
  .stat-val.green { color:#4caf50; }
  .stat-val.red { color:#ef5350; }

  button {
    border:none; cursor:pointer;
    font-family:inherit;
    transition:transform 0.1s, box-shadow 0.1s;
  }
  button:active { transform:scale(0.97); }

  .btn-scan {
    background:linear-gradient(135deg,#1565c0,#0d47a1);
    color:#fff;
    padding:20px 32px;
    font-size:19px; font-weight:700;
    border-radius:14px;
    width:100%;
    min-height:64px;
    box-shadow:0 4px 12px rgba(25,118,210,0.3);
  }
  .btn-rebalance {
    background:linear-gradient(135deg,#e65100,#bf360c);
    color:#fff;
    padding:18px 24px;
    font-size:17px; font-weight:700;
    border-radius:14px;
    width:100%;
    min-height:60px;
    box-shadow:0 4px 12px rgba(230,81,0,0.3);
  }
  .btn-refresh {
    background:#444; color:#fff;
    padding:10px 16px;
    font-size:14px;
    border-radius:8px;
  }

  .sym-row {
    background:var(--card2); padding:14px;
    border-radius:10px; margin-bottom:10px;
  }
  .sym-header {
    display:flex; justify-content:space-between;
    align-items:center; margin-bottom:8px;
  }
  .sym-name { font-size:18px; font-weight:700; }
  .sym-weight { font-size:12px; color:var(--muted); background:#333; padding:3px 8px; border-radius:4px; }
  .sym-info { font-size:13px; color:var(--muted); }
  .sym-lev {
    font-size:24px; font-weight:700;
    color:#4caf50;
    margin:6px 0;
  }
  .sym-lev.low { color:#ff9800; }
  .sym-lev.zero { color:#ef5350; }
  .badge {
    display:inline-block; padding:3px 10px;
    border-radius:6px; font-size:11px; font-weight:700;
  }
  .badge.ok { background:#1b5e20; color:#a5d6a7; }
  .badge.skip { background:#4a2000; color:#ffcc80; }

  .loading {
    text-align:center; padding:40px 20px;
    color:var(--muted);
  }

  /* スマホ */
  @media (max-width: 600px) {
    .status-grid { grid-template-columns:1fr 1fr; }
    h1 { font-size:20px; }
    .stat-val { font-size:18px; }
    .btn-scan { font-size:22px; min-height:76px; padding:24px; }
    .btn-rebalance { font-size:19px; min-height:68px; }
    .sym-name { font-size:20px; }
    .sym-lev { font-size:28px; }
  }
</style>
</head>
<body>

<h1>🤖 Kelly Bot 手動スキャン</h1>

<!-- 現在の状態 -->
<div class="card">
  <h2>📊 現在の状態</h2>
  <div class="status-grid" id="status">
    <div class="stat"><div class="stat-lbl">総資産</div><div class="stat-val" id="capital">—</div></div>
    <div class="stat"><div class="stat-lbl">リターン</div><div class="stat-val" id="return">—</div></div>
    <div class="stat"><div class="stat-lbl">ポジション数</div><div class="stat-val" id="npos">—</div></div>
    <div class="stat"><div class="stat-lbl">次回リバランス</div><div class="stat-val" id="next" style="font-size:14px">—</div></div>
  </div>
  <div id="positions" style="margin-top:14px"></div>
</div>

<!-- スキャンボタン -->
<div class="card">
  <h2>🔍 今すぐKelly計算</h2>
  <p style="font-size:13px;color:var(--muted);margin:4px 0 12px">
    BNB/BTC の過去60日データから最適レバを計算します
  </p>
  <button class="btn-scan" onclick="scanNow()">🔍 今すぐスキャン実行</button>
  <div id="scan-output" style="margin-top:14px"></div>
</div>

<!-- リバランス -->
<div class="card">
  <h2>💰 手動リバランス</h2>
  <p style="font-size:13px;color:var(--muted);margin:4px 0 12px">
    ⚠️ 30日待たずに今すぐ全決済 → 新Kelly基準で再エントリー
  </p>
  <button class="btn-rebalance" onclick="rebalanceNow()">💰 今すぐリバランス実行</button>
</div>

<div style="text-align:center;margin:20px 0">
  <button class="btn-refresh" onclick="refresh()">🔄 更新</button>
</div>

<script>
async function refresh() {
  try {
    const r = await fetch('/api/state').then(r => r.json());
    document.getElementById('capital').textContent = '$' + r.total_capital.toFixed(2);
    const retEl = document.getElementById('return');
    retEl.textContent = (r.return_pct >= 0 ? '+' : '') + r.return_pct.toFixed(2) + '%';
    retEl.className = 'stat-val ' + (r.return_pct >= 0 ? 'green' : 'red');
    document.getElementById('npos').textContent = r.positions.length + '件';
    document.getElementById('next').textContent = r.next_rebalance || '—';

    const posDiv = document.getElementById('positions');
    if (r.positions.length === 0) {
      posDiv.innerHTML = '<div style="color:var(--muted);font-size:13px">📭 ポジションなし</div>';
    } else {
      posDiv.innerHTML = r.positions.map(p => `
        <div class="sym-row">
          <div class="sym-header">
            <span class="sym-name">${p.symbol.replace(':USDT','')}</span>
            <span class="sym-weight">レバ${p.leverage}倍</span>
          </div>
          <div class="sym-info">
            エントリー: $${p.entry_price.toFixed(2)} |
            現在: $${p.current_price.toFixed(2)}
          </div>
          <div class="sym-info" style="color:${p.unrealized_pnl >= 0 ? '#4caf50' : '#ef5350'}">
            未実現PnL: $${p.unrealized_pnl >= 0 ? '+' : ''}${p.unrealized_pnl.toFixed(2)}
            (${p.unrealized_pct >= 0 ? '+' : ''}${p.unrealized_pct.toFixed(2)}%)
          </div>
        </div>
      `).join('');
    }
  } catch (e) {
    console.error(e);
  }
}

async function scanNow() {
  const out = document.getElementById('scan-output');
  out.innerHTML = '<div class="loading">⏳ Kelly計算中... (10秒くらいかかります)</div>';
  try {
    const r = await fetch('/api/scan').then(r => r.json());
    let html = '';
    for (const [sym, info] of Object.entries(r.symbols)) {
      if (info.error) {
        html += `<div class="sym-row" style="color:#ef5350">
          <strong>${sym}</strong><br>エラー: ${info.error}
        </div>`;
        continue;
      }
      const lev = info.kelly_leverage;
      const levClass = lev >= 2 ? '' : lev >= 1 ? 'low' : 'zero';
      const badge = lev >= 1 ? '<span class="badge ok">エントリー対象</span>' : '<span class="badge skip">見送り (Kelly&lt;1)</span>';
      html += `<div class="sym-row">
        <div class="sym-header">
          <span class="sym-name">${sym.replace(':USDT','')}</span>
          <span class="sym-weight">配分${(info.weight*100).toFixed(0)}%</span>
        </div>
        <div class="sym-lev ${levClass}">推奨レバ ${lev.toFixed(0)}倍</div>
        <div class="sym-info">
          年率リターン: ${(info.mean_ann*100).toFixed(1)}%<br>
          年率変動: ${(Math.sqrt(info.var_ann)*100).toFixed(1)}%<br>
          ${badge}
        </div>
      </div>`;
    }
    out.innerHTML = `<h3 style="margin-top:12px">📊 計算結果</h3>${html}`;
  } catch (e) {
    out.innerHTML = `<div style="color:#ef5350">❌ エラー: ${e.message}</div>`;
  }
}

async function rebalanceNow() {
  if (!confirm('⚠️ 本当に今すぐリバランスしますか？\\n\\n既存ポジションは全決済され、新しいKelly基準で再エントリーされます。')) return;
  try {
    const r = await fetch('/api/rebalance', { method: 'POST' }).then(r => r.json());
    alert(r.message);
    refresh();
  } catch (e) {
    alert('❌ エラー: ' + e.message);
  }
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""


@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/api/state')
def api_state():
    """ボット状態を返す（現在価格込み）"""
    with _lock:
        s = bot.state
        positions = []
        for sym, p in s.positions.items():
            try:
                current = bot.get_current_price(sym)
                entry = p["entry_price"]
                size = p["size"]
                margin = p.get("initial_margin", 1.0)
                unrealized_pnl = (current - entry) * size
                unrealized_pct = (unrealized_pnl / margin * 100) if margin > 0 else 0
                positions.append({
                    "symbol": sym,
                    "leverage": p.get("leverage", 0),
                    "entry_price": entry,
                    "current_price": current,
                    "size": size,
                    "unrealized_pnl": unrealized_pnl,
                    "unrealized_pct": unrealized_pct,
                })
            except Exception:
                positions.append({
                    "symbol": sym,
                    "leverage": p.get("leverage", 0),
                    "entry_price": p["entry_price"],
                    "current_price": p["entry_price"],
                    "size": p["size"],
                    "unrealized_pnl": 0,
                    "unrealized_pct": 0,
                })

        # 次回リバランス日
        next_rebalance = "即時"
        if s.last_rebalance:
            try:
                from datetime import datetime, timedelta
                last = datetime.fromisoformat(s.last_rebalance)
                next_at = last + timedelta(days=bot.config.rebalance_days)
                next_rebalance = next_at.strftime('%Y-%m-%d %H:%M')
            except Exception:
                pass

        return jsonify({
            "total_capital": s.total_capital,
            "start_capital": s.start_capital,
            "return_pct": (s.total_capital / s.start_capital - 1) * 100 if s.start_capital > 0 else 0,
            "positions": positions,
            "next_rebalance": next_rebalance,
            "cooldown": s.cooldown_active,
        })


@app.route('/api/scan')
def api_scan():
    """今すぐKelly計算してレバ推奨を返す"""
    with _lock:
        result = {}
        for sym, weight in bot.config.allocations.items():
            try:
                df = bot.fetch_ohlcv(sym, days=bot.config.lookback_days + 30)
                kelly_lev = bot.compute_kelly_leverage(df)
                returns = df["close"].pct_change().dropna()
                recent = returns.tail(bot.config.lookback_days)
                mean_ann = float(recent.mean() * 365)
                var_ann = float(recent.var() * 365)
                current_price = bot.get_current_price(sym)
                result[sym] = {
                    "weight": weight,
                    "kelly_leverage": kelly_lev,
                    "mean_ann": mean_ann,
                    "var_ann": var_ann,
                    "current_price": current_price,
                }
            except Exception as e:
                result[sym] = {"error": str(e), "weight": weight}
        return jsonify({"symbols": result})


@app.route('/api/rebalance', methods=['POST'])
def api_rebalance():
    """手動でリバランスを実行"""
    with _lock:
        try:
            bot.rebalance()
            return jsonify({
                "ok": True,
                "message": f"✅ リバランス完了\n総資本: ${bot.state.total_capital:.2f}"
            })
        except Exception as e:
            return jsonify({
                "ok": False,
                "message": f"❌ リバランス失敗: {e}"
            })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8083))
    logger.info(f"🌐 Kelly Bot Dashboard 起動: http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
