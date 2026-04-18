"""
dashboard.py - 売買Pro UIスタイル完全再現版
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import ccxt
from flask import Flask, jsonify, make_response, render_template_string

app = Flask(__name__)

STATE_FILE = "kelly_bot_state.json"
INITIAL = 3000.0

_cache = {"data": None, "ts": 0}


def load_state() -> dict:
    path = Path(STATE_FILE)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def load_monitor_log(limit=50) -> list:
    """monitor.logから最新ログを読む"""
    path = Path("monitor.log")
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
        return lines[-limit:]
    except Exception:
        return []


def get_dashboard_data() -> dict:
    now = time.time()
    if _cache["data"] and now - _cache["ts"] < 5:
        return _cache["data"]

    state = load_state()
    if not state:
        return {"error": "ボット未起動"}

    positions = state.get("positions", {})
    total_capital = state.get("total_capital", 0)
    start_capital = state.get("start_capital", INITIAL)
    last_rebal = state.get("last_rebalance", "")

    pos_data = []
    total_unrealized = 0
    total_position_value = 0
    btc_price = None

    try:
        ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
        for sym, pos in positions.items():
            try:
                ticker = ex.fetch_ticker(sym)
                current = float(ticker["last"])
                if "BTC" in sym:
                    btc_price = current
                entry = pos["entry_price"]
                size = pos["size"]
                leverage = pos["leverage"]
                margin = pos.get("initial_margin", 0)

                unrealized = (current - entry) * size
                pct_on_margin = (unrealized / margin * 100) if margin > 0 else 0
                price_change_pct = (current / entry - 1) * 100
                total_unrealized += unrealized

                position_value = margin + unrealized
                total_position_value += position_value

                pos_data.append({
                    "symbol": sym.split("/")[0],
                    "entry": entry,
                    "current": current,
                    "price_change_pct": price_change_pct,
                    "size": size,
                    "leverage": leverage,
                    "margin": margin,
                    "unrealized": unrealized,
                    "pnl_pct": pct_on_margin,
                    "value": position_value,
                })
            except Exception:
                pass

        # BTCが未取得ならticker取得
        if btc_price is None:
            try:
                btc_price = float(ex.fetch_ticker("BTC/USDT:USDT")["last"])
            except Exception:
                btc_price = 0
    except Exception as e:
        return {"error": f"API接続失敗: {e}"}

    cash = total_capital - sum(pos.get("initial_margin", 0) for pos in positions.values())
    total_equity = cash + total_position_value
    total_pnl = total_equity - start_capital
    total_pnl_pct = (total_equity / start_capital - 1) * 100

    days_since = 0
    next_rebal = ""
    days_left = 30
    if last_rebal:
        try:
            last_dt = datetime.fromisoformat(last_rebal.split(".")[0])
            days_since = (datetime.now() - last_dt).days
            next_rebal_dt = last_dt + timedelta(days=30)
            next_rebal = next_rebal_dt.strftime("%Y-%m-%d")
            days_left = max(0, 30 - days_since)
        except Exception:
            pass

    trades_hist = state.get("trades_history", [])

    data = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start_capital": start_capital,
        "total_equity": total_equity,
        "cash": cash,
        "total_unrealized": total_unrealized,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "dd_pct": min(0, total_pnl_pct),
        "days_since": days_since,
        "days_left": days_left,
        "last_rebalance": last_rebal[:19] if last_rebal else "",
        "next_rebalance": next_rebal,
        "positions": pos_data,
        "btc_price": btc_price or 0,
        "projection_1y": start_capital * (1.1028 ** 12),
        "projection_2y": start_capital * (1.1028 ** 24),
        "monthly_expected": 10.28,
        "trade_count": len(trades_hist),
        "recent_trades": trades_hist[-10:],
        "monitor_logs": load_monitor_log(40),
    }

    _cache["data"] = data
    _cache["ts"] = now
    return data


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kelly Bot — BNB/BTC</title>
<style>
:root{
  --bg:#060d17;--bg2:#091523;--bg3:#0d1c2e;--bg4:#112338;
  --border:#162840;--border2:#1d3350;
  --text:#c8d8ea;--muted:#304d66;--muted2:#4e7291;
  --green:#00e676;--green-bg:#00e67612;
  --red:#f44336;--red-bg:#f4433612;
  --yellow:#ffca28;--yellow-bg:#ffca2812;
  --blue:#4fc3f7;--blue-bg:#4fc3f712;
  --cyan:#26c6da;--orange:#ffa726;
  --purple:#ab47bc;--purple-bg:#ab47bc12;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg);color:var(--text);font-family:-apple-system,"Helvetica Neue",sans-serif;font-size:13px;height:100%;overflow:hidden}

/* ─── ヘッダー ─── */
.hdr{
  background:var(--bg2);border-bottom:1px solid var(--border);
  padding:0 18px;height:48px;display:flex;align-items:center;gap:12px;
  flex-shrink:0;
}
.hdr-logo{font-size:16px;font-weight:900;color:#fff;letter-spacing:-.5px}
.hdr-logo em{color:var(--yellow);font-style:normal}
.live-dot{width:8px;height:8px;border-radius:50%;background:var(--green);flex-shrink:0;
  box-shadow:0 0 8px var(--green);animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.hbadge{padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700}
.hb-sim{background:#0a2c17;color:var(--green);border:1px solid #174d28}
.hb-sym{background:var(--bg4);color:var(--blue);border:1px solid var(--border2)}
.hb-lev{background:var(--yellow-bg);color:var(--yellow);border:1px solid #3d3000}
.hb-strat{background:var(--purple-bg);color:var(--purple);border:1px solid #ab47bc40}
.hbtn{padding:5px 14px;border-radius:7px;font-size:12px;font-weight:700;border:none;cursor:pointer;transition:.15s}
.hbtn:hover{filter:brightness(1.2)}
.hbtn-pause{background:var(--bg4);color:var(--yellow);border:1px solid var(--border2)}
.hbtn-start{background:#0d2b14;color:var(--green);border:1px solid #174d28}
.hdr-right{margin-left:auto;font-size:11px;color:var(--muted2)}

/* ─── 統計バー ─── */
.statsbar{
  display:grid;grid-template-columns:repeat(7,1fr);
  background:var(--bg2);border-bottom:1px solid var(--border);flex-shrink:0;
}
.sb{padding:10px 16px;border-right:1px solid var(--border);min-width:0}
.sb:last-child{border-right:none}
.sb-val{font-size:21px;font-weight:900;letter-spacing:-.6px;line-height:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sb-lbl{font-size:9px;color:var(--muted2);text-transform:uppercase;letter-spacing:.06em;font-weight:700;margin-top:3px}
.sb-sub{font-size:10px;color:var(--muted);margin-top:2px}
.g{color:var(--green)}.r{color:var(--red)}.y{color:var(--yellow)}.b{color:var(--blue)}.c{color:var(--cyan)}.o{color:var(--orange)}.p{color:var(--purple)}

/* ─── DD バー ─── */
.ddbar{
  background:var(--bg2);border-bottom:1px solid var(--border);
  padding:7px 18px;display:flex;align-items:center;gap:14px;flex-shrink:0;
}
.dd-lbl{font-size:10px;color:var(--muted2);font-weight:700;text-transform:uppercase;letter-spacing:.06em}
.dd-num{font-size:22px;font-weight:900;letter-spacing:-1px;min-width:80px}
.dd-track{flex:1;max-width:220px;height:5px;background:var(--border2);border-radius:3px;overflow:hidden}
.dd-fill{height:100%;border-radius:3px;transition:width .5s,background .5s}
.dd-pill{font-size:11px;font-weight:800;padding:2px 9px;border-radius:10px}
.ddp-ok{background:var(--green-bg);color:var(--green)}
.ddp-warn{background:var(--yellow-bg);color:var(--yellow)}
.ddp-danger{background:var(--red-bg);color:var(--red)}
.dd-price{margin-left:auto;font-size:13px;font-weight:700;color:var(--text)}

/* インフォボックス (Kelly指標用) */
.info-box{display:flex;flex-direction:column;align-items:center;min-width:80px;
  background:var(--bg3);border:1px solid var(--border2);border-radius:8px;padding:4px 10px}
.info-lbl{font-size:9px;color:var(--muted2);font-weight:700;letter-spacing:.06em}
.info-val{font-size:18px;font-weight:900;line-height:1.1}
.info-sub{font-size:9px;color:var(--muted2)}

/* ─── タブ ─── */
.tabs{
  background:var(--bg2);border-bottom:2px solid var(--border);
  display:flex;padding:0 18px;gap:2px;flex-shrink:0;
}
.tb{padding:9px 18px;font-size:13px;font-weight:700;cursor:pointer;
  border-bottom:3px solid transparent;color:var(--muted2);transition:.15s;margin-bottom:-2px}
.tb.on{color:#fff;border-bottom-color:var(--blue)}
.tb:hover:not(.on){color:var(--text)}

/* ─── レイアウト ─── */
.body-wrap{display:flex;flex-direction:column;height:100vh}
.main{display:grid;grid-template-columns:1fr 350px;flex:1;overflow:hidden}
.lpanel{overflow-y:auto;border-right:1px solid var(--border)}
.rpanel{overflow-y:auto;background:var(--bg2)}

/* ─── チャートセクション ─── */
.chart-wrap{border-bottom:2px solid var(--border);background:var(--bg)}
.chart-toolbar{
  display:flex;align-items:center;justify-content:space-between;
  padding:8px 14px;background:var(--bg2);border-bottom:1px solid var(--border);
}
.chart-title{font-size:12px;font-weight:700;color:var(--muted2);text-transform:uppercase;letter-spacing:.05em}
.chart-area{padding:20px;display:flex;align-items:center;justify-content:center;min-height:240px;
  background:linear-gradient(180deg,var(--bg) 0%,var(--bg2) 100%)}

/* 大きな中央数字 */
.big-display{text-align:center}
.big-display .bd-lbl{font-size:11px;color:var(--muted2);text-transform:uppercase;letter-spacing:.1em;font-weight:700;margin-bottom:12px}
.big-display .bd-val{font-size:72px;font-weight:900;letter-spacing:-3px;line-height:1;margin-bottom:8px}
.big-display .bd-sub{font-size:14px;color:var(--muted2)}

/* ─── テーブル ─── */
.ttable{width:100%;border-collapse:collapse}
.ttable th{
  padding:8px 14px;font-size:10px;font-weight:700;color:var(--muted2);
  text-transform:uppercase;letter-spacing:.05em;background:var(--bg3);
  border-bottom:1px solid var(--border);position:sticky;top:0;z-index:5;text-align:left;
}
.ttable td{padding:11px 14px;border-bottom:1px solid var(--border);vertical-align:middle}
.ttable tr:hover td{background:var(--bg3)}
.ttable tr.lrow td:first-child{border-left:3px solid var(--green)}

/* バッジ */
.pill{display:inline-block;padding:3px 9px;border-radius:11px;font-size:10px;font-weight:800;letter-spacing:.02em}
.pl-g{background:var(--green-bg);color:var(--green);border:1px solid #00e67630}
.pl-r{background:var(--red-bg);color:var(--red);border:1px solid #f4433630}
.pl-y{background:var(--yellow-bg);color:var(--yellow);border:1px solid #ffca2830}
.pl-b{background:var(--blue-bg);color:var(--blue);border:1px solid #4fc3f730}

/* ─── システムログ (右パネル) ─── */
.log-hdr{
  padding:9px 14px;background:var(--bg3);border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  font-size:10px;font-weight:700;color:var(--muted2);text-transform:uppercase;letter-spacing:.06em;
  position:sticky;top:0;z-index:5;
}
.logrow{padding:5px 13px;border-bottom:1px solid #0a141f;font-size:11px;display:flex;gap:8px;line-height:1.45}
.logrow:hover{background:var(--bg3)}
.logts{color:var(--muted);white-space:nowrap;font-size:10px;font-family:monospace;flex-shrink:0;margin-top:1px}
.logmsg{word-break:break-word}
.lc-i{color:var(--text)}.lc-w{color:var(--yellow)}.lc-e{color:var(--red)}
.lc-g{color:var(--green)}.lc-b{color:var(--blue)}.lc-c{color:var(--cyan)}

/* ─── ポートフォリオタブ ─── */
.pf-cards{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:var(--border)}
.pfc{background:var(--bg3);padding:18px 20px}
.pfc .pfl{font-size:9px;color:var(--muted2);text-transform:uppercase;letter-spacing:.07em;font-weight:700;margin-bottom:7px}
.pfc .pfv{font-size:28px;font-weight:900;letter-spacing:-.6px}
.pfc .pfs{font-size:11px;color:var(--muted);margin-top:5px}
.sig-section{padding:16px 18px}
.sig-ttl{font-size:10px;font-weight:700;color:var(--muted2);text-transform:uppercase;letter-spacing:.07em;margin-bottom:12px}
.tf3{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px}
.tfbox{background:var(--bg3);border-radius:9px;padding:12px;text-align:center;border:1px solid var(--border2)}
.tfbox .tfn{font-size:10px;color:var(--muted2);font-weight:700;margin-bottom:5px}
.tfbox .tfd{font-size:15px;font-weight:900;margin-bottom:4px}
.tfbox .tfs{font-size:10px;color:var(--muted)}

/* ─── 予測カード ─── */
.proj-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--border)}
.proj-card{background:var(--bg3);padding:20px;text-align:center}
.proj-card.highlight{background:linear-gradient(135deg,#0a1f2c,#0f2a3d);border:1px solid #4fc3f730}
.proj-lbl{font-size:10px;color:var(--muted2);text-transform:uppercase;letter-spacing:.07em;font-weight:700;margin-bottom:10px}
.proj-val{font-size:32px;font-weight:900;letter-spacing:-.8px;margin-bottom:4px}
.proj-sub{font-size:11px;color:var(--muted)}

.tpane{display:none}.tpane.on{display:block}

/* エラー */
.error-box{padding:40px;text-align:center}
.error-box h3{color:var(--red);font-size:18px;margin-bottom:10px}

/* モバイル */
@media(max-width:768px){
  html,body{overflow:auto;height:auto}
  .body-wrap{height:auto;min-height:100vh}
  .hdr{flex-wrap:wrap;height:auto;padding:10px 12px;gap:8px}
  .hdr-right{display:none}
  .statsbar{grid-template-columns:repeat(4,1fr)}
  .sb{padding:10px 8px}
  .sb-val{font-size:17px}
  .ddbar{flex-wrap:wrap}
  .main{grid-template-columns:1fr}
  .lpanel{border-right:none}
  .rpanel{border-top:2px solid var(--border);max-height:40vh}
  .pf-cards{grid-template-columns:1fr 1fr}
  .proj-grid{grid-template-columns:1fr}
  .big-display .bd-val{font-size:48px}
}
</style>
</head>
<body>
<div class="body-wrap">

<!-- ヘッダー -->
<div class="hdr">
  <div class="live-dot"></div>
  <div class="hdr-logo">Kelly<em>Bot</em></div>
  <div class="hbadge hb-sim">PAPER</div>
  <div class="hbadge hb-sym">BNB • BTC</div>
  <div class="hbadge hb-lev" id="avg-lev">1.0倍</div>
  <div class="hbadge hb-strat">Kelly 0.5x</div>
  <button class="hbtn hbtn-pause">⏸ 一時停止</button>
  <div class="hdr-right" id="updated">起動中...</div>
</div>

<!-- 統計バー (7項目) -->
<div class="statsbar">
  <div class="sb"><div class="sb-val" id="s-eq">—</div><div class="sb-lbl">総資産</div><div class="sb-sub" id="s-eq2"></div></div>
  <div class="sb"><div class="sb-val" id="s-up">—</div><div class="sb-lbl">含み損益</div><div class="sb-sub" id="s-up2"></div></div>
  <div class="sb"><div class="sb-val" id="s-pnl">—</div><div class="sb-lbl">総損益</div><div class="sb-sub" id="s-pnl2"></div></div>
  <div class="sb"><div class="sb-val b" id="s-cash">—</div><div class="sb-lbl">手元現金</div><div class="sb-sub">待機中</div></div>
  <div class="sb"><div class="sb-val y" id="s-days">—</div><div class="sb-lbl">次のリバランス</div><div class="sb-sub" id="s-days2"></div></div>
  <div class="sb"><div class="sb-val c" id="s-ps">0件</div><div class="sb-lbl">保有中</div><div class="sb-sub">BNB+BTC</div></div>
  <div class="sb"><div class="sb-val p" id="s-proj">—</div><div class="sb-lbl">1年後予測</div><div class="sb-sub">月+10.28%想定</div></div>
</div>

<!-- DD バー -->
<div class="ddbar">
  <div class="dd-lbl">DD</div>
  <div class="dd-num r" id="ddnum">-0.00%</div>
  <div class="dd-track"><div class="dd-fill" id="ddfill" style="width:0%;background:var(--green)"></div></div>
  <div class="dd-pill ddp-ok" id="ddpill">正常</div>
  <div style="margin-left:auto;display:flex;align-items:center;gap:10px">
    <div class="info-box">
      <span class="info-lbl">BNB Kelly</span>
      <span class="info-val b" id="bnb-kelly">—</span>
      <span class="info-sub">推奨レバ</span>
    </div>
    <div class="info-box">
      <span class="info-lbl">BTC Kelly</span>
      <span class="info-val c" id="btc-kelly">—</span>
      <span class="info-sub">推奨レバ</span>
    </div>
    <div class="info-box" style="min-width:90px">
      <span class="info-lbl">月次期待値</span>
      <span class="info-val g">+10.28%</span>
      <span class="info-sub">過去4年平均</span>
    </div>
    <div class="dd-price" id="btcprice">BTC: —</div>
  </div>
</div>

<!-- タブ -->
<div class="tabs">
  <div class="tb on" id="t-live" onclick="sw('live')">⚡ ライブ取引</div>
  <div class="tb" id="t-pf" onclick="sw('pf')">📊 ポートフォリオ</div>
  <div class="tb" id="t-proj" onclick="sw('proj')">🔮 将来予測</div>
  <div class="tb" id="t-strat" onclick="sw('strat')">⚙️ 戦略</div>
</div>

<!-- メイン -->
<div class="main">
  <div class="lpanel">
    <!-- ライブ取引タブ -->
    <div class="tpane on" id="p-live">
      <div class="chart-wrap">
        <div class="chart-toolbar">
          <span class="chart-title">📈 現在の運用状態</span>
        </div>
        <div class="chart-area">
          <div class="big-display">
            <div class="bd-lbl">総資産 (USD)</div>
            <div class="bd-val" id="big-equity">$3,000.00</div>
            <div class="bd-sub" id="big-pnl">損益: +$0.00 (+0.00%)</div>
          </div>
        </div>
      </div>
      <table class="ttable">
        <thead>
          <tr>
            <th style="width:80px">状態</th>
            <th>銘柄</th>
            <th style="width:90px">レバ</th>
            <th style="width:120px">入値</th>
            <th style="width:120px">現在値</th>
            <th style="width:100px">変動</th>
            <th style="width:130px;text-align:right">含み損益</th>
          </tr>
        </thead>
        <tbody id="ltbody"></tbody>
      </table>
    </div>

    <!-- ポートフォリオタブ -->
    <div class="tpane" id="p-pf">
      <div class="pf-cards">
        <div class="pfc"><div class="pfl">総資産</div><div class="pfv" id="pf-eq">—</div><div class="pfs" id="pf-eqs"></div></div>
        <div class="pfc"><div class="pfl">含み損益</div><div class="pfv" id="pf-up">—</div><div class="pfs">未実現</div></div>
        <div class="pfc"><div class="pfl">総損益</div><div class="pfv" id="pf-pnl">—</div><div class="pfs" id="pf-pnls"></div></div>
      </div>
      <div class="pf-cards" style="margin-top:1px">
        <div class="pfc"><div class="pfl">手元現金</div><div class="pfv b" id="pf-cash">—</div><div class="pfs">待機中</div></div>
        <div class="pfc"><div class="pfl">取引数</div><div class="pfv c" id="pf-trades">—</div><div class="pfs">エントリー+決済</div></div>
        <div class="pfc"><div class="pfl">DD</div><div class="pfv r" id="pf-dd">—</div><div class="pfs">最大ドローダウン</div></div>
      </div>

      <div class="sig-section">
        <div class="sig-ttl">💼 ポジション明細</div>
        <table class="ttable">
          <thead>
            <tr>
              <th>通貨</th>
              <th>サイズ</th>
              <th>Kelly</th>
              <th>入値</th>
              <th>現在値</th>
              <th>変動</th>
              <th>証拠金</th>
              <th>評価額</th>
              <th style="text-align:right">未実現</th>
            </tr>
          </thead>
          <tbody id="pf-positions"></tbody>
        </table>
      </div>
    </div>

    <!-- 将来予測タブ -->
    <div class="tpane" id="p-proj">
      <div class="proj-grid">
        <div class="proj-card">
          <div class="proj-lbl">現在</div>
          <div class="proj-val" id="pj-now">$3,000</div>
          <div class="proj-sub">スタート地点</div>
        </div>
        <div class="proj-card highlight">
          <div class="proj-lbl">1年後 (月+10.28%)</div>
          <div class="proj-val b" id="pj-1y">$9,707</div>
          <div class="proj-sub" id="pj-1ys">3.2倍に成長</div>
        </div>
        <div class="proj-card">
          <div class="proj-lbl">2年後</div>
          <div class="proj-val y" id="pj-2y">$31,408</div>
          <div class="proj-sub" id="pj-2ys">10.5倍に成長</div>
        </div>
      </div>

      <div class="sig-section">
        <div class="sig-ttl">📈 月別成長シミュレーション</div>
        <table class="ttable">
          <thead>
            <tr>
              <th>経過月</th>
              <th>予測残高</th>
              <th>増加額</th>
              <th>倍率</th>
            </tr>
          </thead>
          <tbody id="growth-tbody"></tbody>
        </table>
      </div>

      <div class="sig-section">
        <div class="sig-ttl">🏆 バックテスト実績 (2022年クラッシュ含む過去4年)</div>
        <div class="tf3">
          <div class="tfbox"><div class="tfn">1年 プラス率</div><div class="tfd g">92%</div><div class="tfs">33/36期間</div></div>
          <div class="tfbox"><div class="tfn">2年 プラス率</div><div class="tfd g">100%</div><div class="tfs">12/12期間</div></div>
          <div class="tfbox"><div class="tfn">清算リスク</div><div class="tfd g">ゼロ</div><div class="tfs">過去4年</div></div>
        </div>
      </div>
    </div>

    <!-- 戦略タブ -->
    <div class="tpane" id="p-strat">
      <div class="sig-section">
        <div class="sig-ttl">⚙️ 戦略パラメータ</div>
        <div class="tf3">
          <div class="tfbox"><div class="tfn">Kelly Fraction</div><div class="tfd y">0.5</div><div class="tfs">Half Kelly</div></div>
          <div class="tfbox"><div class="tfn">Lookback</div><div class="tfd">60日</div><div class="tfs">2ヶ月分析</div></div>
          <div class="tfbox"><div class="tfn">Max Leverage</div><div class="tfd r">10倍</div><div class="tfs">上限キャップ</div></div>
        </div>
        <div class="tf3">
          <div class="tfbox"><div class="tfn">Rebalance</div><div class="tfd b">30日</div><div class="tfs">月次更新</div></div>
          <div class="tfbox"><div class="tfn">Cooldown</div><div class="tfd p">-25%</div><div class="tfs">大損失でStop</div></div>
          <div class="tfbox"><div class="tfn">配分</div><div class="tfd c">70/30</div><div class="tfs">BNB/BTC</div></div>
        </div>

        <div class="sig-ttl" style="margin-top:20px">📖 パラメータ解説</div>
        <div style="padding:10px 14px;background:var(--bg3);border-radius:8px;margin-bottom:10px">
          <div style="color:var(--yellow);font-weight:700;margin-bottom:4px">🎯 Kelly Fraction 0.5</div>
          <div style="font-size:12px;color:var(--muted2);line-height:1.6">数学的に最適なレバレッジの半分を使用。プロのファンドも使う保守的設定。</div>
        </div>
        <div style="padding:10px 14px;background:var(--bg3);border-radius:8px;margin-bottom:10px">
          <div style="color:var(--yellow);font-weight:700;margin-bottom:4px">📅 Lookback 60日</div>
          <div style="font-size:12px;color:var(--muted2);line-height:1.6">過去60日（2ヶ月）のデータから市場ボラティリティを計算。短すぎず長すぎない最適値。</div>
        </div>
        <div style="padding:10px 14px;background:var(--bg3);border-radius:8px;margin-bottom:10px">
          <div style="color:var(--yellow);font-weight:700;margin-bottom:4px">💪 Max Leverage 10倍</div>
          <div style="font-size:12px;color:var(--muted2);line-height:1.6">Kelly式が高レバを推奨しても10倍でキャップ。清算リスク抑制と+10%月次の両立。</div>
        </div>
        <div style="padding:10px 14px;background:var(--bg3);border-radius:8px;margin-bottom:10px">
          <div style="color:var(--yellow);font-weight:700;margin-bottom:4px">🔄 Rebalance 30日</div>
          <div style="font-size:12px;color:var(--muted2);line-height:1.6">30日ごとに全ポジション清算→Kelly再計算→新ポジション。月1回の戦略更新。</div>
        </div>
        <div style="padding:10px 14px;background:var(--bg3);border-radius:8px">
          <div style="color:var(--yellow);font-weight:700;margin-bottom:4px">🛑 Cooldown -25%</div>
          <div style="font-size:12px;color:var(--muted2);line-height:1.6">前月-25%以下の大損失なら翌月取引停止。連敗パターンを防ぐ安全装置。</div>
        </div>
      </div>
    </div>
  </div>

  <!-- 右パネル: ログ -->
  <div class="rpanel">
    <div class="log-hdr">
      <span>📝 システムログ</span>
      <span id="log-count" style="color:var(--muted)">0件</span>
    </div>
    <div id="logbox"></div>
  </div>
</div>

</div>

<script>
let _data = null;
let currentTab = 'live';

function formatMoney(n) { return '$' + n.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
function formatMoneyCompact(n) {
    if (Math.abs(n) >= 1000) return '$' + Math.round(n).toLocaleString();
    return '$' + n.toFixed(2);
}
function formatPct(n, sign = true) { return (sign && n >= 0 ? '+' : '') + n.toFixed(2) + '%'; }
function pctClass(n) { return n > 0.001 ? 'g' : (n < -0.001 ? 'r' : 'y'); }

function sw(tab) {
    currentTab = tab;
    document.querySelectorAll('.tb').forEach(e => e.classList.remove('on'));
    document.getElementById('t-' + tab).classList.add('on');
    document.querySelectorAll('.tpane').forEach(e => e.classList.remove('on'));
    document.getElementById('p-' + tab).classList.add('on');
    render();
}

function updateHeader(d) {
    document.getElementById('updated').textContent = '更新: ' + d.updated.split(' ')[1];
    // 平均レバ
    if (d.positions && d.positions.length > 0) {
        const avgLev = d.positions.reduce((s, p) => s + (p.leverage || 0), 0) / d.positions.length;
        document.getElementById('avg-lev').textContent = avgLev.toFixed(2) + '倍';
    }
    document.getElementById('btcprice').textContent = 'BTC: ' + (d.btc_price ? '$' + Math.round(d.btc_price).toLocaleString() : '—');

    // Kelly表示
    const bnb = d.positions.find(p => p.symbol === 'BNB');
    const btc = d.positions.find(p => p.symbol === 'BTC');
    document.getElementById('bnb-kelly').textContent = bnb ? bnb.leverage.toFixed(2) + 'x' : '—';
    document.getElementById('btc-kelly').textContent = btc ? btc.leverage.toFixed(2) + 'x' : '—';
}

function updateStats(d) {
    const eq = document.getElementById('s-eq');
    eq.textContent = formatMoneyCompact(d.total_equity);
    eq.className = 'sb-val ' + pctClass(d.total_pnl);
    document.getElementById('s-eq2').textContent = '初期 ' + formatMoneyCompact(d.start_capital);

    const up = document.getElementById('s-up');
    up.textContent = (d.total_unrealized >= 0 ? '+' : '') + formatMoneyCompact(d.total_unrealized);
    up.className = 'sb-val ' + pctClass(d.total_unrealized);
    document.getElementById('s-up2').textContent = '未実現';

    const pnl = document.getElementById('s-pnl');
    pnl.textContent = (d.total_pnl >= 0 ? '+' : '') + formatMoneyCompact(d.total_pnl);
    pnl.className = 'sb-val ' + pctClass(d.total_pnl);
    document.getElementById('s-pnl2').textContent = formatPct(d.total_pnl_pct);

    document.getElementById('s-cash').textContent = formatMoneyCompact(d.cash);
    document.getElementById('s-days').textContent = d.days_left + '日';
    document.getElementById('s-days2').textContent = d.next_rebalance || '—';
    document.getElementById('s-ps').textContent = (d.positions || []).length + '件';
    document.getElementById('s-proj').textContent = formatMoneyCompact(d.projection_1y);
}

function updateDD(d) {
    const ddNum = document.getElementById('ddnum');
    ddNum.textContent = formatPct(d.dd_pct, false);
    ddNum.className = 'dd-num ' + pctClass(d.dd_pct);
    const ddFill = document.getElementById('ddfill');
    const ddAbs = Math.abs(d.dd_pct);
    ddFill.style.width = Math.min(100, ddAbs * 3) + '%';
    if (ddAbs < 5) ddFill.style.background = 'var(--green)';
    else if (ddAbs < 15) ddFill.style.background = 'var(--yellow)';
    else ddFill.style.background = 'var(--red)';

    const ddPill = document.getElementById('ddpill');
    if (ddAbs < 5) { ddPill.className = 'dd-pill ddp-ok'; ddPill.textContent = '正常'; }
    else if (ddAbs < 15) { ddPill.className = 'dd-pill ddp-warn'; ddPill.textContent = '警戒'; }
    else { ddPill.className = 'dd-pill ddp-danger'; ddPill.textContent = '危険'; }
}

function renderLive(d) {
    document.getElementById('big-equity').textContent = formatMoney(d.total_equity);
    document.getElementById('big-equity').className = 'bd-val ' + pctClass(d.total_pnl);
    const pnl = d.total_pnl;
    document.getElementById('big-pnl').innerHTML =
        '損益: <span class="' + pctClass(pnl) + '">' + (pnl >= 0 ? '+' : '') + formatMoney(pnl) + ' (' + formatPct(d.total_pnl_pct) + ')</span>';

    let html = '';
    for (const p of (d.positions || [])) {
        html += '<tr class="lrow">' +
            '<td><span class="pill pl-g">運用中</span></td>' +
            '<td style="font-weight:900;color:var(--yellow);font-size:14px">' + p.symbol + '/USDT</td>' +
            '<td><span class="pill pl-b">' + p.leverage.toFixed(2) + 'x</span></td>' +
            '<td>' + formatMoneyCompact(p.entry) + '</td>' +
            '<td>' + formatMoneyCompact(p.current) + '</td>' +
            '<td class="' + pctClass(p.price_change_pct) + '">' + formatPct(p.price_change_pct) + '</td>' +
            '<td style="text-align:right" class="' + pctClass(p.unrealized) + '">' +
            (p.unrealized >= 0 ? '+' : '') + formatMoney(p.unrealized) + '<br>' +
            '<span style="font-size:10px;color:var(--muted2)">' + formatPct(p.pnl_pct) + '</span></td>' +
            '</tr>';
    }
    if (!html) html = '<tr><td colspan="7" style="text-align:center;padding:32px;color:var(--muted)">ポジションなし (Cooldown中)</td></tr>';
    document.getElementById('ltbody').innerHTML = html;
}

function renderPf(d) {
    document.getElementById('pf-eq').textContent = formatMoneyCompact(d.total_equity);
    document.getElementById('pf-eq').className = 'pfv ' + pctClass(d.total_pnl);
    document.getElementById('pf-eqs').textContent = '初期 ' + formatMoneyCompact(d.start_capital);

    document.getElementById('pf-up').textContent = (d.total_unrealized >= 0 ? '+' : '') + formatMoneyCompact(d.total_unrealized);
    document.getElementById('pf-up').className = 'pfv ' + pctClass(d.total_unrealized);

    document.getElementById('pf-pnl').textContent = (d.total_pnl >= 0 ? '+' : '') + formatMoneyCompact(d.total_pnl);
    document.getElementById('pf-pnl').className = 'pfv ' + pctClass(d.total_pnl);
    document.getElementById('pf-pnls').textContent = formatPct(d.total_pnl_pct);

    document.getElementById('pf-cash').textContent = formatMoneyCompact(d.cash);
    document.getElementById('pf-trades').textContent = (d.trade_count || 0) + '件';
    document.getElementById('pf-dd').textContent = formatPct(d.dd_pct, false);

    let html = '';
    for (const p of (d.positions || [])) {
        html += '<tr>' +
            '<td style="font-weight:900;color:var(--yellow)">' + p.symbol + '</td>' +
            '<td>' + p.size.toFixed(6) + '</td>' +
            '<td><span class="pill pl-b">' + p.leverage.toFixed(2) + 'x</span></td>' +
            '<td>' + formatMoneyCompact(p.entry) + '</td>' +
            '<td>' + formatMoneyCompact(p.current) + '</td>' +
            '<td class="' + pctClass(p.price_change_pct) + '">' + formatPct(p.price_change_pct) + '</td>' +
            '<td>' + formatMoneyCompact(p.margin) + '</td>' +
            '<td>' + formatMoneyCompact(p.value) + '</td>' +
            '<td style="text-align:right" class="' + pctClass(p.unrealized) + '">' +
            (p.unrealized >= 0 ? '+' : '') + formatMoneyCompact(p.unrealized) + '</td>' +
            '</tr>';
    }
    if (!html) html = '<tr><td colspan="9" style="text-align:center;padding:24px;color:var(--muted)">ポジションなし</td></tr>';
    document.getElementById('pf-positions').innerHTML = html;
}

function renderProj(d) {
    document.getElementById('pj-now').textContent = formatMoneyCompact(d.start_capital);
    document.getElementById('pj-1y').textContent = formatMoneyCompact(d.projection_1y);
    document.getElementById('pj-1ys').textContent = (d.projection_1y / d.start_capital).toFixed(1) + '倍に成長';
    document.getElementById('pj-2y').textContent = formatMoneyCompact(d.projection_2y);
    document.getElementById('pj-2ys').textContent = (d.projection_2y / d.start_capital).toFixed(1) + '倍に成長';

    let html = '';
    for (const m of [1, 3, 6, 9, 12, 18, 24]) {
        const bal = d.start_capital * Math.pow(1.1028, m);
        const gain = bal - d.start_capital;
        const multi = bal / d.start_capital;
        html += '<tr>' +
            '<td><b>' + m + 'ヶ月</b></td>' +
            '<td>' + formatMoneyCompact(bal) + '</td>' +
            '<td class="g">+' + formatMoneyCompact(gain) + '</td>' +
            '<td><span class="pill pl-g">' + multi.toFixed(2) + '倍</span></td>' +
            '</tr>';
    }
    document.getElementById('growth-tbody').innerHTML = html;
}

function renderLogs(d) {
    const logs = d.monitor_logs || [];
    let html = '';
    for (const line of logs.slice(-50).reverse()) {
        let cls = 'lc-i';
        if (line.includes('ERROR')) cls = 'lc-e';
        else if (line.includes('WARNING')) cls = 'lc-w';
        else if (line.includes('✅')) cls = 'lc-g';
        else if (line.includes('INFO')) cls = 'lc-b';

        // タイムスタンプ抽出
        const m = line.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})/);
        const ts = m ? m[1].substring(11, 19) : '';
        const msg = m ? line.substring(m[0].length).replace(/^[,\s\[\]\w]+\s/, '') : line;

        html += '<div class="logrow">' +
            '<span class="logts">' + ts + '</span>' +
            '<span class="logmsg ' + cls + '">' + msg.replace(/[<>]/g, '') + '</span>' +
            '</div>';
    }
    document.getElementById('logbox').innerHTML = html || '<div style="padding:20px;color:var(--muted);text-align:center;font-size:11px">ログなし</div>';
    document.getElementById('log-count').textContent = logs.length + '件';
}

function render() {
    if (!_data || _data.error) {
        if (_data && _data.error) {
            document.querySelector('.lpanel').innerHTML =
                '<div class="error-box"><h3>⚠️ ' + _data.error + '</h3></div>';
        }
        return;
    }
    updateHeader(_data);
    updateStats(_data);
    updateDD(_data);
    if (currentTab === 'live') renderLive(_data);
    else if (currentTab === 'pf') renderPf(_data);
    else if (currentTab === 'proj') renderProj(_data);
    renderLogs(_data);
}

async function loadData() {
    try {
        const res = await fetch('/api/status?t=' + Date.now());
        _data = await res.json();
        render();
    } catch (err) {
        console.error(err);
    }
}

loadData();
setInterval(loadData, 10000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    response = make_response(render_template_string(HTML_TEMPLATE))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/api/status")
def api_status():
    response = make_response(jsonify(get_dashboard_data()))
    response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("🤖 Kelly Bot Dashboard (売買Pro UIスタイル)")
    print("=" * 60)
    print("📱 URL: http://localhost:8765")
    print("⌨️  Safari: Cmd+Option+R でハードリロード")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=8765, debug=False)
