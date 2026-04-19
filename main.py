"""
main.py — エントリポイント・Flask API サーバー
================================================
UIフロントエンドと連携するための Flask ウェブサーバー。
TradingBot をバックグラウンドで動かしながら、
ブラウザから /api/state を叩くと現在状態が取得できる。

起動方法:
    # シミュレーションモード（デフォルト）
    python main.py

    # バックテストモード
    python main.py --mode backtest --start 2024-01-01 --end 2024-12-31

    # 初期資金を変更
    python main.py --balance 50000

    # ポート変更
    python main.py --port 8082
"""

import warnings
# v36.0: macOS LibreSSL 2.8.3 起因の urllib3 警告を先行抑制（他importより前に設定必須）
# urllib3 import時点で警告が発火するため、filterは最初に設定する必要がある
warnings.filterwarnings('ignore', message='urllib3 v2 only supports OpenSSL')

import argparse
import sys
import os
import json
import threading
import time
import logging

import numpy as np

try:
    from flask import Flask, jsonify, request, render_template_string
except ImportError:
    print("pip install flask を実行してください")
    sys.exit(1)
from config import Config, Mode
from trading_bot import TradingBot
from backtester import Backtester
from utils import setup_logger

logger = setup_logger("main")

app   = Flask(__name__)
_bot: TradingBot = None


@app.after_request
def _add_no_cache_headers(response):
    """全レスポンスにキャッシュ無効化ヘッダーを付与。
    ブラウザが古いHTMLをキャッシュして新UIが反映されない問題を防ぐ。"""
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.before_request
def _require_basic_auth():
    """認証は無効化（ユーザー指示: iPhone/MacBookからパスワード不要で見れるように）"""
    return  # 常に認証スキップ


def _sanitize(obj):
    """
    numpy の特殊型（numpy.bool_, numpy.float64 など）を
    Python の標準型（bool, float, int）に変換する。
    Flask の jsonify は標準型しか扱えないため、
    APIレスポンスを返す前に必ずこの関数を通す。
    """
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, float) and (np.isnan(obj) or np.isinf(obj)):
        return None
    return obj


# ════════════════════════════════════════════════════
# ダッシュボード HTML
# ════════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>自動売買 Pro — 日本税制対応版 v20260419b</title>
<!-- Build: 20260419-jp-tax-v2 — 新UI確認用マーカー -->
<script>console.log("%c✅ 新UIロード済み: 日本税制対応版 v20260419b", "color:#00e676;font-weight:bold;font-size:14px");</script>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
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
.hb-live{background:#2c0a0a;color:var(--red);border:1px solid #4d1717}
.hb-sym{background:var(--bg4);color:var(--blue);border:1px solid var(--border2)}
.hb-lev{background:var(--yellow-bg);color:var(--yellow);border:1px solid #3d3000}
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
.g{color:var(--green)}.r{color:var(--red)}.y{color:var(--yellow)}.b{color:var(--blue)}.c{color:var(--cyan)}.o{color:var(--orange)}

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
.tf-switcher{display:flex;gap:4px}
.tf-btn{padding:3px 10px;border-radius:6px;font-size:11px;font-weight:700;border:1px solid var(--border2);
  background:var(--bg3);color:var(--muted2);cursor:pointer;transition:.15s}
.tf-btn.on{background:var(--blue-bg);color:var(--blue);border-color:#4fc3f740}
.tf-btn:hover:not(.on){color:var(--text);background:var(--bg4)}
#chart-el{width:100%;height:360px}

/* 凡例バー */
.legend-bar{
  display:flex;align-items:center;gap:18px;padding:6px 14px;
  background:var(--bg2);border-bottom:1px solid var(--border);font-size:11px;
}
.leg-item{display:flex;align-items:center;gap:5px}
.leg-line{width:22px;height:2px;border-radius:1px}
.leg-dash{background:repeating-linear-gradient(90deg,currentColor 0,currentColor 4px,transparent 4px,transparent 8px);width:22px;height:2px}
.leg-label{color:var(--muted2);font-weight:600}

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
.ttable tr.srow td:first-child{border-left:3px solid var(--red)}

/* バッジ */
.pill{display:inline-block;padding:3px 9px;border-radius:11px;font-size:10px;font-weight:800;letter-spacing:.02em}
.pl-g{background:var(--green-bg);color:var(--green);border:1px solid #00e67630}
.pl-r{background:var(--red-bg);color:var(--red);border:1px solid #f4433630}
.pl-y{background:var(--yellow-bg);color:var(--yellow);border:1px solid #ffca2830}
.pl-b{background:var(--blue-bg);color:var(--blue);border:1px solid #4fc3f730}
.pl-gray{background:var(--bg4);color:var(--muted2);border:1px solid var(--border2)}

/* スコアバー */
.scbar{display:flex;align-items:center;gap:7px}
.sctrack{width:64px;height:5px;background:var(--border2);border-radius:3px;overflow:hidden}
.scfill{height:100%;border-radius:3px;transition:.4s}
.scnum{font-size:13px;font-weight:900;min-width:26px}

/* 空状態 */
.empty{text-align:center;padding:56px 20px}
.empty .ei{font-size:38px;margin-bottom:14px;opacity:.5}
.empty .et{font-size:14px;font-weight:700;color:var(--muted2);margin-bottom:6px}
.empty .es{font-size:12px;color:var(--muted)}

/* ─── システムログ ─── */
.log-hdr{
  padding:9px 14px;background:var(--bg3);border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  font-size:10px;font-weight:700;color:var(--muted2);text-transform:uppercase;letter-spacing:.06em;
  position:sticky;top:0;z-index:5;
}
.logclear{background:none;border:none;color:var(--muted);font-size:10px;cursor:pointer;padding:2px 7px;border-radius:5px}
.logclear:hover{color:var(--text);background:var(--bg4)}
.logrow{padding:5px 13px;border-bottom:1px solid #0a141f;font-size:11px;display:flex;gap:8px;line-height:1.45}
.logrow:hover{background:var(--bg3)}
.logts{color:var(--muted);white-space:nowrap;font-size:10px;font-family:monospace;flex-shrink:0;margin-top:1px}
.logmsg{word-break:break-word}
.lc-i{color:var(--text)}.lc-w{color:var(--yellow)}.lc-e{color:var(--red)}
.lc-g{color:var(--green)}.lc-b{color:var(--blue)}.lc-c{color:var(--cyan)}

/* ─── ポートフォリオタブ ─── */
.pf-cards{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border)}
.pfc{background:var(--bg3);padding:18px 20px}
.pfc .pfl{font-size:9px;color:var(--muted2);text-transform:uppercase;letter-spacing:.07em;font-weight:700;margin-bottom:7px}
.pfc .pfv{font-size:28px;font-weight:900;letter-spacing:-.6px}
.pfc .pfs{font-size:11px;color:var(--muted);margin-top:5px}
.sig-section{padding:16px 18px}
.sig-ttl{font-size:10px;font-weight:700;color:var(--muted2);text-transform:uppercase;letter-spacing:.07em;margin-bottom:12px;display:flex;align-items:center;gap:6px}
.tf3{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px}
.tfbox{background:var(--bg3);border-radius:9px;padding:12px;text-align:center;border:1px solid var(--border2)}
.tfbox .tfn{font-size:10px;color:var(--muted2);font-weight:700;margin-bottom:5px}
.tfbox .tfd{font-size:15px;font-weight:900;margin-bottom:4px}
.tfbox .tfs{font-size:10px;color:var(--muted)}
.risk-grid{display:flex;flex-direction:column;gap:0}
.rrow{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--border)}
.rrow:last-child{border:none}
.rk{font-size:12px;color:var(--muted2)}
.rv{font-size:12px;font-weight:700}

/* ─── 取引履歴 ─── */
.htable{width:100%;border-collapse:collapse}
.htable th{padding:8px 14px;font-size:10px;font-weight:700;color:var(--muted2);text-transform:uppercase;background:var(--bg3);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:5;text-align:left}
.htable td{padding:10px 14px;border-bottom:1px solid var(--border);font-size:12px}
.htable tr:hover td{background:var(--bg3)}
.rtp{color:var(--green);font-weight:700}.rsl{color:var(--red);font-weight:700}
.rtr{color:var(--cyan);font-weight:700}.rto{color:var(--muted2)}

.tpane{display:none}.tpane.on{display:block}

/* ─── クールダウンバー ─── */
.cdbar{
  background:var(--yellow-bg);border-bottom:1px solid var(--border);
  padding:6px 18px;display:flex;align-items:center;gap:14px;flex-shrink:0;
}
.cd-txt{font-size:13px;font-weight:800;color:var(--yellow);flex:1}
.cd-track{width:160px;height:5px;background:var(--border2);border-radius:3px;overflow:hidden}
.cd-fill{height:100%;border-radius:3px;background:var(--yellow);transition:width 1s linear}

/* ─── 1日損失バー ─── */
.dlbar-wrap{display:flex;align-items:center;gap:8px;margin-left:auto}
.dlbar-lbl{font-size:10px;color:var(--muted2);font-weight:700;white-space:nowrap}
.dlbar-track{width:100px;height:5px;background:var(--border2);border-radius:3px;overflow:hidden}
.dlbar-fill{height:100%;border-radius:3px;background:var(--green);transition:width .5s,background .5s}
.dlbar-pct{font-size:11px;font-weight:800;min-width:40px;text-align:right}

/* ─── エクイティチャート ─── */
.eq-section{padding:0;border-bottom:1px solid var(--border)}
.eq-hdr{display:flex;align-items:center;justify-content:space-between;
  padding:8px 14px;background:var(--bg2);border-bottom:1px solid var(--border)}
.eq-title{font-size:12px;font-weight:700;color:var(--muted2);text-transform:uppercase;letter-spacing:.05em}
#eq-chart-el{width:100%;height:180px}

/* ─── 銘柄ランキング ─── */
.rank-section{padding:16px 18px}
.rank-ttl{font-size:10px;font-weight:700;color:var(--muted2);text-transform:uppercase;letter-spacing:.07em;margin-bottom:12px}
.rank-table{width:100%;border-collapse:collapse}
.rank-table th{font-size:10px;color:var(--muted2);font-weight:700;padding:5px 8px;border-bottom:1px solid var(--border);text-transform:uppercase}
.rank-table td{font-size:12px;padding:6px 8px;border-bottom:1px solid var(--border2)}
.rank-table tr:hover td{background:var(--bg3)}

/* ─── テーブルスクロールラッパー ─── */
.table-scroll-wrap{width:100%}

/* ─── モバイル専用タブ（PCでは非表示）─── */
.mob-only{display:none}

/* ════════════════════════════════════════
   スマホレイアウト (≤768px)
   ════════════════════════════════════════ */
@media(max-width:768px){

  /* ─── ベース ─── */
  html,body{overflow:auto;height:auto}
  .body-wrap{height:auto;min-height:100vh}

  /* ─── ヘッダー ─── */
  .hdr{flex-wrap:wrap;height:auto;padding:10px 12px;gap:8px}
  .hdr-logo{font-size:15px}
  .hb-sym,.hdr-right{display:none}
  .hbadge{font-size:11px;padding:5px 10px}
  .hbtn{font-size:13px;padding:9px 16px;min-height:42px;touch-action:manipulation}

  /* ─── 統計バー: 2行×4列グリッド（7項目） ─── */
  .statsbar{
    display:grid;
    grid-template-columns:repeat(4,1fr);
    overflow:visible
  }
  .sb{
    padding:12px 10px;
    border-right:1px solid var(--border);
    border-bottom:1px solid var(--border)
  }
  /* 4列目と7列目は右ボーダーなし */
  .sb:nth-child(4n){border-right:none}
  .sb-val{font-size:18px;letter-spacing:-.4px}
  .sb-lbl{font-size:9px}
  .sb-sub{font-size:9px}

  /* ─── DDバー ─── */
  .ddbar{
    flex-wrap:wrap;padding:8px 12px;gap:8px;
    align-items:center
  }
  .dd-lbl{font-size:10px}
  .dd-num{font-size:18px;min-width:60px}
  .dd-track{max-width:100px}
  .dd-price{margin-left:0;font-size:12px;order:10}
  .dlbar-wrap{margin-left:0;order:9}
  .dlbar-track{width:80px}
  /* Fear&Greed / 資金調達率 / 強気比率 ボックス: コンパクト横並び */
  .ddbar>div[style*="margin-left:auto"]{
    margin-left:0!important;width:100%;
    display:flex!important;gap:6px;align-items:center;flex-wrap:nowrap;
    overflow-x:auto;-webkit-overflow-scrolling:touch;
    padding-bottom:2px;scrollbar-width:none
  }
  .ddbar>div[style*="margin-left:auto"]::-webkit-scrollbar{display:none}
  .ddbar>div[style*="margin-left:auto"]>div{
    min-width:62px;padding:4px 8px!important;flex-shrink:0
  }
  #fg-val,#mb-val{font-size:15px!important}
  #fr-val{font-size:13px!important}
  #fg-lbl,#mb-lbl,#fr-lbl{font-size:8px!important}

  /* ─── クールダウンバー ─── */
  .cdbar{flex-wrap:wrap;padding:8px 12px;gap:8px}
  .cd-track{width:100px}

  /* ─── タブ ─── */
  .tabs{
    overflow-x:auto;-webkit-overflow-scrolling:touch;
    scrollbar-width:none;padding:0 8px
  }
  .tabs::-webkit-scrollbar{display:none}
  .tb{
    padding:12px 16px;font-size:13px;white-space:nowrap;
    min-height:44px;display:flex;align-items:center;
    touch-action:manipulation
  }

  /* ─── モバイル専用要素 ─── */
  .mob-only{display:block}

  /* ─── メインレイアウト: 1カラム ─── */
  .main{grid-template-columns:1fr;overflow:visible}
  .lpanel{overflow:visible;border-right:none}
  .rpanel{display:none;border-top:2px solid var(--border)}
  .rpanel.mob-show{display:block;height:60vh}

  /* ─── チャート ─── */
  #chart-el{height:240px!important}
  #eq-chart-el{height:150px!important}
  .chart-toolbar{flex-wrap:wrap;gap:8px;padding:8px 10px}
  .chart-title{font-size:11px}
  .tf-btn{padding:7px 13px;font-size:12px;min-height:36px;touch-action:manipulation}
  #sym-select{font-size:13px;padding:6px 8px;min-height:36px}

  /* ─── テーブル共通: 横スクロール ─── */
  .table-scroll-wrap{
    overflow-x:auto;-webkit-overflow-scrolling:touch;
    scrollbar-width:thin;scrollbar-color:var(--border2) transparent
  }
  .ttable{font-size:12px;min-width:560px}
  .ttable th,.ttable td{padding:10px 9px}
  .htable{font-size:12px;min-width:580px}
  .htable th,.htable td{padding:10px 9px}

  /* ─── ポートフォリオカード: 2列 ─── */
  .pf-cards{grid-template-columns:1fr 1fr}
  .pfc{padding:14px 12px}
  .pfc .pfv{font-size:22px}
  .pfc .pfl{font-size:9px}
  .pfc .pfs{font-size:10px}

  /* ─── シグナル / リスクグリッド ─── */
  .sig-section{padding:14px 14px}
  .tf3{gap:6px}
  .tfbox{padding:10px 8px;border-radius:7px}
  .tfbox .tfd{font-size:14px}
  .rk,.rv{font-size:12px}
  .rrow{padding:8px 0}

  /* ─── スキャンタブ ─── */
  #scan-result-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
  #scan-result-wrap table{min-width:600px;font-size:12px}
  #scan-btn{
    padding:20px 32px;
    font-size:18px;
    font-weight:700;
    min-height:64px;
    min-width:240px;
    touch-action:manipulation;
    border-radius:12px;
    box-shadow:0 4px 12px rgba(0,200,100,0.25);
    transition:transform 0.1s, box-shadow 0.1s;
  }
  #scan-btn:active{transform:scale(0.97);box-shadow:0 2px 6px rgba(0,200,100,0.15)}
  /* iPhone/スマホ向け: ボタンをさらに大きく＆画面幅いっぱいに */
  @media (max-width: 600px){
    #scan-btn{
      padding:24px 20px;
      font-size:22px;
      min-height:76px;
      width:100%;
      max-width:none;
    }
    .pfc #scan-btn{width:100%}
    /* スキャン結果の買い/売りボタンも大きく */
    .entry-btn{
      padding:16px 24px !important;
      font-size:17px !important;
      min-height:56px !important;
      min-width:110px !important;
      border-radius:12px !important;
    }
    .entry-btn:active{transform:scale(0.95)}
  }

  /* ─── ログ ─── */
  .logrow{padding:8px 12px;font-size:12px;gap:10px}
  .logts{font-size:10px}

  /* ─── 空状態 ─── */
  .empty{padding:40px 16px}
  .empty .ei{font-size:32px}
  .empty .et{font-size:13px}
}
</style>
</head>
<body>
<div class="body-wrap">

<!-- ヘッダー -->
<div class="hdr">
  <div class="live-dot" id="ldot"></div>
  <div class="hdr-logo">売買<em>Pro</em></div>
  <div class="hbadge hb-sim" id="mbadge">SIM</div>
  <div class="hbadge hb-sym">BTC/USDT</div>
  <div class="hbadge hb-lev" id="levbadge">2〜5倍</div>
  <button class="hbtn hbtn-pause" onclick="ctrlBot('stop')">⏸ 一時停止</button>
  <button class="hbtn hbtn-start" id="btn-start" onclick="ctrlBot('start')" style="display:none">▶ 再開</button>
  <button class="hbtn" id="btn-resume" onclick="ctrlBot('reset_cooldown')" style="display:none;background:#1a2a0a;color:#a8e063;border:1px solid #4a8020;font-size:12px;padding:5px 14px;border-radius:7px;font-weight:700;cursor:pointer">🔄 冷却解除・再開</button>
  <div class="hdr-right" id="scaninfo">起動中...</div>
</div>

<!-- 統計バー -->
<div class="statsbar">
  <div class="sb"><div class="sb-val" id="s-eq">—</div><div class="sb-lbl">総資産</div><div class="sb-sub" id="s-eq2"></div></div>
  <div class="sb"><div class="sb-val" id="s-up">—</div><div class="sb-lbl">含み損益</div><div class="sb-sub" id="s-up2"></div></div>
  <div class="sb"><div class="sb-val" id="s-rp">—</div><div class="sb-lbl">確定損益</div><div class="sb-sub" id="s-rp2"></div></div>
  <div class="sb" style="background:linear-gradient(135deg,#2c0a0a 0%,#3d0e0e 100%);border:1px solid #5d1a1a"><div class="sb-val r" id="s-tax" style="font-weight:900">—</div><div class="sb-lbl" style="color:#ff9090">💰 推定税金(日本)</div><div class="sb-sub" id="s-tax-sub">雑所得 <select id="tax-rate-select" onchange="setTaxRate(this.value)" style="background:#3d0e0e;color:#ffb0b0;border:1px solid #5d1a1a;font-size:10px;padding:1px 4px;border-radius:3px;cursor:pointer" title="日本の仮想通貨FXは雑所得・総合課税（所得税+住民税10%）"><option value="auto" selected>自動(累進)</option><option value="0.15">〜195万(15%)</option><option value="0.20">〜330万(20%)</option><option value="0.30">〜695万(30%)</option><option value="0.33">〜900万(33%)</option><option value="0.43">〜1800万(43%)</option><option value="0.50">〜4000万(50%)</option><option value="0.55">4000万超(55%)</option></select></div></div>
  <div class="sb" style="background:linear-gradient(135deg,#0a2c17 0%,#0e3d22 100%);border:1px solid #1a5d33"><div class="sb-val g" id="s-aftertax" style="font-weight:900">—</div><div class="sb-lbl" style="color:#90ff90">💵 税引き後利益</div><div class="sb-sub" id="s-aftertax2">実現利益 − 税金</div></div>
  <div class="sb"><div class="sb-val" id="s-td">—</div><div class="sb-lbl">本日損益</div><div class="sb-sub" id="s-td2"></div></div>
  <div class="sb"><div class="sb-val" id="s-wr">—</div><div class="sb-lbl">勝率</div><div class="sb-sub" id="s-wrs">0勝0敗</div></div>
  <div class="sb"><div class="sb-val y" id="s-lv">—</div><div class="sb-lbl">レバレッジ</div><div class="sb-sub" id="s-st">正常稼働中</div></div>
  <div class="sb"><div class="sb-val b" id="s-ps">0件</div><div class="sb-lbl">保有中</div><div class="sb-sub" id="s-sc"></div></div>
  <div class="sb"><div class="sb-val c" id="s-elapsed">—</div><div class="sb-lbl">検証経過</div><div class="sb-sub" id="s-scancount">スキャン 0回</div></div>
  <div class="sb"><div class="sb-val r" id="s-streak">0</div><div class="sb-lbl">連敗</div><div class="sb-sub" id="s-streak-max">最大連敗 0</div></div>
  <div class="sb"><div class="sb-val g" id="s-pf">—</div><div class="sb-lbl">PF (利益係数)</div><div class="sb-sub" id="s-sharpe">Sharpe —</div></div>
  <div class="sb"><div class="sb-val o" id="s-fees">—</div><div class="sb-lbl">手数料累計</div><div class="sb-sub" id="s-fees-ratio">損益比 —</div></div>
</div>

<!-- DD バー -->
<div class="ddbar">
  <div class="dd-lbl">DD</div>
  <div class="dd-num r" id="ddnum">-0.00%</div>
  <div class="dd-track"><div class="dd-fill" id="ddfill" style="width:0%;background:var(--green)"></div></div>
  <div class="dd-pill ddp-ok" id="ddpill">正常</div>
  <!-- 1日損失バー -->
  <div class="dlbar-wrap">
    <span class="dlbar-lbl">本日損失</span>
    <div class="dlbar-track"><div class="dlbar-fill" id="dlbar-fill" style="width:0%"></div></div>
    <span class="dlbar-pct g" id="dlbar-pct">0%</span>
    <span style="font-size:10px;color:var(--muted2)">/5%</span>
  </div>
  <!-- 5%超過カウンター（デモ用：超えた回数を表示） -->
  <div id="breach-wrap" style="display:none;align-items:center;gap:5px;margin-left:8px;padding:3px 10px;background:var(--red-bg);border:1px solid var(--red);border-radius:10px">
    <span style="font-size:10px;color:var(--red);font-weight:700">⚠️ 5%超過</span>
    <span id="breach-count" style="font-size:15px;font-weight:900;color:var(--red)">0</span>
    <span style="font-size:10px;color:var(--red);font-weight:700">回</span>
  </div>
  <!-- Fear & Greed + ファンディングレート（右寄せ） -->
  <div style="margin-left:auto;display:flex;align-items:center;gap:10px">
    <div style="display:flex;flex-direction:column;align-items:center;min-width:72px;
                background:var(--bg3);border:1px solid var(--border2);border-radius:8px;padding:4px 10px">
      <span style="font-size:9px;color:var(--muted2);font-weight:700;letter-spacing:.06em">恐怖&amp;強欲</span>
      <span id="fg-val" style="font-size:20px;font-weight:900;line-height:1.1">—</span>
      <span id="fg-lbl" style="font-size:9px;color:var(--muted2)">読込中...</span>
    </div>
    <div style="display:flex;flex-direction:column;align-items:center;min-width:80px;
                background:var(--bg3);border:1px solid var(--border2);border-radius:8px;padding:4px 10px">
      <span style="font-size:9px;color:var(--muted2);font-weight:700;letter-spacing:.06em">資金調達率</span>
      <span id="fr-val" style="font-size:16px;font-weight:900;line-height:1.1">—</span>
      <span id="fr-lbl" style="font-size:9px;color:var(--muted2)">上位10銘柄平均</span>
    </div>
    <div style="display:flex;flex-direction:column;align-items:center;min-width:72px;
                background:var(--bg3);border:1px solid var(--border2);border-radius:8px;padding:4px 10px">
      <span style="font-size:9px;color:var(--muted2);font-weight:700;letter-spacing:.06em">強気比率</span>
      <span id="mb-val" style="font-size:20px;font-weight:900;line-height:1.1">—</span>
      <span id="mb-lbl" style="font-size:9px;color:var(--muted2)">市場全体の強気%</span>
    </div>
    <div class="dd-price" id="btcprice">BTC: —</div>
  </div>
</div>
<!-- クールダウンバー（冷却中のときだけ表示） -->
<div class="cdbar" id="cdbar" style="display:none">
  <span style="font-size:16px">⏳</span>
  <span class="cd-txt" id="cd-txt">冷却中...</span>
  <div class="cd-track"><div class="cd-fill" id="cd-fill" style="width:100%"></div></div>
  <button class="hbtn" onclick="ctrlBot('reset_cooldown')" style="background:#1a2a0a;color:#a8e063;border:1px solid #4a8020;font-size:11px;padding:4px 12px">今すぐ再開</button>
</div>

<!-- タブ -->
<div class="tabs">
  <div class="tb on" id="t-live" onclick="sw('live')">⚡ ライブ取引</div>
  <div class="tb" id="t-pf"   onclick="sw('pf')">📊 ポートフォリオ</div>
  <div class="tb" id="t-scan" onclick="sw('scan')">🔍 手動スキャン</div>
  <div class="tb" id="t-hist" onclick="sw('hist')">📋 取引履歴</div>
  <div class="tb" id="t-analysis" onclick="sw('analysis')">📈 分析</div>
  <div class="tb mob-only" id="t-log" onclick="sw('log')">📝 ログ</div>
</div>

<!-- メイン -->
<div class="main">
  <div class="lpanel">

    <!-- ライブ取引タブ -->
    <div class="tpane on" id="p-live">
      <!-- ライブ取引 改善版UI -->

      <!-- ① 保有ポジションサマリー（最上部で一目確認） -->
      <div class="chart-wrap" style="padding:10px 14px">
        <div class="chart-toolbar" style="margin-bottom:8px">
          <span class="chart-title">🎯 保有ポジション</span>
          <span id="live-pos-count" style="font-size:11px;color:var(--muted2);margin-left:auto">0件</span>
        </div>
        <div id="live-positions" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px">
          <div style="color:var(--muted2);font-size:12px;padding:8px">保有ポジションなし — エントリー待機中</div>
        </div>
      </div>

      <!-- ② シグナル一覧 見やすく改善 -->
      <div class="chart-wrap" style="padding:10px 14px">
        <div class="chart-toolbar" style="margin-bottom:6px">
          <span class="chart-title">🔍 シグナル一覧</span>
          <div style="display:flex;gap:6px;margin-left:auto;font-size:11px">
            <button id="live-filter-all" onclick="setLiveFilter('all')" style="background:var(--blue-bg);color:var(--blue);border:1px solid #4fc3f740;padding:3px 10px;border-radius:5px;cursor:pointer">すべて</button>
            <button id="live-filter-long" onclick="setLiveFilter('long')" style="background:var(--bg4);color:var(--muted2);border:1px solid var(--border2);padding:3px 10px;border-radius:5px;cursor:pointer">🟢 LONG</button>
            <button id="live-filter-short" onclick="setLiveFilter('short')" style="background:var(--bg4);color:var(--muted2);border:1px solid var(--border2);padding:3px 10px;border-radius:5px;cursor:pointer">🔴 SHORT</button>
            <button id="live-filter-hot" onclick="setLiveFilter('hot')" style="background:var(--bg4);color:var(--muted2);border:1px solid var(--border2);padding:3px 10px;border-radius:5px;cursor:pointer">🔥 スコア0.5以上</button>
          </div>
        </div>
        <div class="table-scroll-wrap">
        <table class="ttable">
          <thead>
            <tr>
              <th style="width:80px">状態</th>
              <th>銘柄</th>
              <th style="width:120px">AI信頼度</th>
              <th style="width:115px">現在価格</th>
              <th style="width:90px">シグナル</th>
              <th style="width:160px">TP / SL</th>
              <th style="width:110px;text-align:right">含み損益</th>
            </tr>
          </thead>
          <tbody id="ltbody"></tbody>
        </table>
        </div>
      </div>
    </div>

    <!-- ポートフォリオタブ -->
    <div class="tpane" id="p-pf">
      <div class="pf-cards">
        <div class="pfc"><div class="pfl">残高</div><div class="pfv" id="pf-b">—</div><div class="pfs">初期資金との比較</div></div>
        <div class="pfc"><div class="pfl">確定損益</div><div class="pfv" id="pf-p">—</div><div class="pfs" id="pf-pp"></div></div>
        <div class="pfc"><div class="pfl">取引数</div><div class="pfv b" id="pf-t">—</div><div class="pfs" id="pf-wl"></div></div>
        <div class="pfc"><div class="pfl">勝率</div><div class="pfv g" id="pf-w">—</div><div class="pfs">勝ちトレードの割合</div></div>
        <div class="pfc"><div class="pfl">最大ドローダウン</div><div class="pfv r" id="pf-d">—</div><div class="pfs">上限 20%</div></div>
        <div class="pfc"><div class="pfl">スキャン回数</div><div class="pfv c" id="pf-s">—</div><div class="pfs">5秒ごとに相場をチェック</div></div>
      </div>

      <!-- エクイティカーブチャート -->
      <div class="eq-section">
        <div class="eq-hdr">
          <span class="eq-title">📈 資産推移グラフ（エクイティカーブ）</span>
          <span style="font-size:10px;color:var(--muted2)">1分ごと更新</span>
        </div>
        <div id="eq-chart-el" style="width:100%;height:180px;background:var(--bg)">
          <div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:12px">
            取引が始まると資産推移グラフが表示されます
          </div>
        </div>
      </div>

      <!-- 銘柄別ランキング -->
      <div class="rank-section">
        <div class="rank-ttl">🏆 銘柄別 勝率ランキング</div>
        <div id="rank-table-wrap">
          <div style="color:var(--muted);font-size:12px">取引が始まると表示されます</div>
        </div>
      </div>

      <div class="sig-section">
        <div class="sig-ttl">📡 3時間軸シグナル合議（現在の判断）</div>
        <div class="tf3" id="tf3"></div>
        <div class="sig-ttl">🛡️ リスク管理パラメータ</div>
        <div class="risk-grid" id="riskgrid"></div>
      </div>
    </div>

    <!-- 手動スキャンタブ -->
    <div class="tpane" id="p-scan">
      <div style="padding:16px">
        <!-- スキャン実行ボタン -->
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
          <button id="scan-btn" onclick="runManualScan()"
            style="background:linear-gradient(135deg,#1565c0,#1976d2);color:#fff;border:none;
                   padding:10px 28px;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;
                   box-shadow:0 2px 8px rgba(25,118,210,0.4);transition:opacity .2s">
            🔍 今すぐスキャン実行
          </button>
          <span id="scan-status" style="font-size:12px;color:var(--muted)">
            ボタンを押すと上位30銘柄をリアルタイムでスキャンします（約10〜20秒かかります）
          </span>
        </div>

        <!-- 結果テーブル -->
        <div id="scan-result-wrap">
          <table style="width:100%;border-collapse:collapse;font-size:12px">
            <thead>
              <tr style="background:var(--bg3);color:var(--muted2)">
                <th style="padding:6px 8px;text-align:left">銘柄</th>
                <th style="padding:6px 8px;text-align:center">方向</th>
                <th style="padding:6px 8px;text-align:right">スコア</th>
                <th style="padding:6px 8px;text-align:right">現在価格</th>
                <th style="padding:6px 8px;text-align:right">TP</th>
                <th style="padding:6px 8px;text-align:right">SL</th>
                <th style="padding:6px 8px;text-align:center">1時間足</th>
                <th style="padding:6px 8px;text-align:center">時間軸合議</th>
                <th style="padding:6px 8px;text-align:center">操作</th>
              </tr>
            </thead>
            <tbody id="scan-tbody">
              <tr><td colspan="9" style="text-align:center;padding:24px;color:var(--muted)">
                🔍 スキャンボタンを押すとエントリー候補が表示されます
              </td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 取引履歴タブ -->
    <div class="tpane" id="p-hist">
      <div class="table-scroll-wrap">
      <table class="htable">
        <thead>
          <tr>
            <th>時刻</th><th>銘柄</th><th>方向</th>
            <th style="text-align:right">エントリー</th>
            <th style="text-align:right">クローズ</th>
            <th style="text-align:right">損益</th>
            <th>決済理由</th>
          </tr>
        </thead>
        <tbody id="htbody">
          <tr><td colspan="7"><div class="empty"><div class="ei">📋</div><div class="et">取引履歴なし</div><div class="es">クローズしたトレードが表示されます</div></div></td></tr>
        </tbody>
      </table>
      </div>
    </div>

    <!-- 📈 分析タブ -->
    <div class="tpane" id="p-analysis">
      <!-- 日次リターン棒グラフ -->
      <div class="chart-wrap">
        <div class="chart-toolbar">
          <span class="chart-title">📊 日次リターン（直近30日）</span>
          <span class="chart-title" id="daily-sum" style="font-size:11px;color:var(--muted2)"></span>
        </div>
        <div id="daily-chart" style="height:240px;background:var(--bg2);border-radius:8px;padding:12px;display:flex;align-items:flex-end;gap:4px;overflow-x:auto"></div>
      </div>

      <!-- 時間帯別勝率ヒートマップ -->
      <div class="chart-wrap">
        <div class="chart-toolbar">
          <span class="chart-title">🕐 時間帯別勝率ヒートマップ (ローカル時間)</span>
        </div>
        <div id="hourly-heatmap" style="background:var(--bg2);border-radius:8px;padding:12px">
          <div style="display:grid;grid-template-columns:repeat(24,1fr);gap:3px" id="heatmap-grid"></div>
          <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted2);margin-top:6px">
            <span>0時</span><span>6時</span><span>12時</span><span>18時</span><span>23時</span>
          </div>
          <div style="display:flex;gap:8px;margin-top:10px;font-size:11px;color:var(--muted2);align-items:center">
            <span>勝率:</span>
            <span style="background:#00e67640;padding:2px 6px;border-radius:3px;color:var(--green)">≥60%</span>
            <span style="background:#ffca2840;padding:2px 6px;border-radius:3px;color:var(--yellow)">40-60%</span>
            <span style="background:#f4433640;padding:2px 6px;border-radius:3px;color:var(--red)">&lt;40%</span>
            <span style="background:var(--bg4);padding:2px 6px;border-radius:3px;color:var(--muted)">データなし</span>
          </div>
        </div>
      </div>

      <!-- 通知設定 -->
      <div class="chart-wrap">
        <div class="chart-toolbar">
          <span class="chart-title">🔔 LINE/Discord通知設定</span>
        </div>
        <div style="background:var(--bg2);border-radius:8px;padding:14px;font-size:12px;line-height:1.8">
          <div style="color:var(--text);font-weight:700;margin-bottom:10px">異常検知時の通知設定方法</div>
          <div style="color:var(--muted2);margin-bottom:6px">1. 以下の環境変数をシェルで設定:</div>
          <pre style="background:var(--bg);padding:10px;border-radius:6px;font-size:11px;color:var(--cyan);overflow-x:auto;margin:6px 0">export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export LINE_NOTIFY_TOKEN="YOUR_LINE_TOKEN"</pre>
          <div style="color:var(--muted2);margin:10px 0 6px 0">2. 通知条件:</div>
          <ul style="color:var(--text);padding-left:18px;margin:4px 0">
            <li>連敗 4回以上</li>
            <li>ドローダウン 5%以上</li>
            <li>1日の損失 -3%超過</li>
            <li>ボット停止・クールダウン発動</li>
          </ul>
          <div style="color:var(--muted2);margin-top:10px;font-size:11px">
            ※ 現在の通知状態: <span id="notif-status" style="color:var(--yellow);font-weight:700">未設定（環境変数なし）</span>
          </div>
        </div>
      </div>
    </div>

  </div><!-- /lpanel -->

  <!-- 右パネル: システムログ -->
  <div class="rpanel">
    <div class="log-hdr">
      <span>システムログ</span>
      <button class="logclear" onclick="clrLog()">クリア</button>
    </div>
    <div id="logbox"></div>
  </div>
</div><!-- /main -->
</div><!-- /body-wrap -->

<script>
// ─── 定数・ユーティリティ ───
const $ = id => document.getElementById(id);
const USD = v => v==null ? "—" : "$"+Math.abs(v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const SIGN = v => v>=0 ? "+" : "";
const PCT  = v => v==null ? "—" : SIGN(v)+v.toFixed(2)+"%";
const COL  = v => v>0 ? "g" : v<0 ? "r" : "";
const FMT  = ts => new Date(ts*1000).toLocaleTimeString("ja-JP",{hour:"2-digit",minute:"2-digit"});
// 価格表示（PEPE等の超小数点コインに対応）
function P(v) {
  if (v == null) return "—";
  const a = Math.abs(v);
  if (a === 0) return "$0.00";
  if (a >= 1000) return "$" + a.toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
  if (a >= 1)    return "$" + a.toFixed(4);
  if (a >= 0.01) return "$" + a.toFixed(5);
  if (a >= 0.0001) return "$" + a.toFixed(7);
  return "$" + a.toFixed(9);  // PEPE等の超小数点コイン対応
}

let _tab = "live", _logCache = [], _logCleared = false, _chartTf = "5m", _chartSym = "BTC/USDT";
let _chart = null, _candles = null, _pricelines = [];
let _eqChart = null, _eqLine = null;
let _prevPositions = {}, _prevTradeCount = 0;
let _cdTotalSecs = 0;  // クールダウン開始時の秒数（カウントダウン用）
let _cdStartTime = 0;

// ─── ブラウザ通知の許可リクエスト ───
function requestNotifyPermission() {
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission();
  }
}

function sendNotify(title, body) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try { new Notification(title, { body, icon: "" }); } catch {}
}

// ─── エクイティチャート初期化 ───
function initEqChart() {
  if (_eqChart || !$("eq-chart-el")) return;
  // プレースホルダーを削除してチャートコンテナにする
  const el = $("eq-chart-el");
  el.innerHTML = "";
  const LC = LightweightCharts;
  _eqChart = LC.createChart(el, {
    layout: { background: { color: "#060d17" }, textColor: "#7a9ab8" },
    grid: { vertLines: { color: "#162840" }, horzLines: { color: "#162840" } },
    rightPriceScale: { borderColor: "#162840", scaleMargins: { top: .1, bottom: .1 } },
    timeScale: { borderColor: "#162840", timeVisible: true },
    handleScroll: { mouseWheel: true, pressedMouseMove: true },
    handleScale: { mouseWheel: true },
    height: 180,
  });
  _eqLine = _eqChart.addLineSeries({
    color: "#4fc3f7", lineWidth: 2, crosshairMarkerVisible: true,
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
  });
  new ResizeObserver(() => {
    if (_eqChart) _eqChart.applyOptions({ width: el.clientWidth });
  }).observe(el);
}

// ─── エクイティチャート更新 ───
async function updateEqChart() {
  if (!_eqChart) return;
  let d;
  try { d = await (await fetch("/api/equity")).json(); } catch { return; }
  const hist = d.history || [];
  if (hist.length > 1) {
    _eqLine.setData(hist);
    _eqChart.timeScale().fitContent();
  }
}

// ─── 銘柄ランキング更新 ───
async function updateRanking() {
  let d;
  try { d = await (await fetch("/api/symbol_stats")).json(); } catch { return; }
  const stats = d.stats || [];
  const wrap = $("rank-table-wrap");
  if (!wrap) return;
  if (stats.length === 0) {
    wrap.innerHTML = `<div style="color:var(--muted);font-size:12px">取引が始まると表示されます</div>`;
    return;
  }
  let rows = stats.map((s, i) => {
    const medal = i === 0 ? "🥇" : i === 1 ? "🥈" : i === 2 ? "🥉" : `${i+1}.`;
    const wrcol = s.win_rate >= 60 ? "var(--green)" : s.win_rate >= 40 ? "var(--yellow)" : "var(--red)";
    const pnlcol = s.pnl >= 0 ? "var(--green)" : "var(--red)";
    return `<tr>
      <td style="color:var(--text)">${medal} ${s.symbol.replace("/USDT","")}</td>
      <td style="text-align:center;color:${wrcol};font-weight:800">${s.win_rate}%</td>
      <td style="text-align:center;color:var(--muted2)">${s.wins}勝 ${s.losses}敗</td>
      <td style="text-align:right;color:${pnlcol};font-weight:800">${s.pnl >= 0 ? "+" : ""}$${Math.abs(s.pnl).toFixed(2)}</td>
    </tr>`;
  }).join("");
  wrap.innerHTML = `<table class="rank-table">
    <thead><tr><th>銘柄</th><th style="text-align:center">勝率</th><th style="text-align:center">戦績</th><th style="text-align:right">損益</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

// ─── TradingView チャート初期化 ───
function initChart() {
  if (_chart) return;
  if (!$("chart-el")) return;  // チャート削除された場合はスキップ
  const LC = LightweightCharts;
  _chart = LC.createChart($("chart-el"), {
    layout:{background:{color:"#060d17"},textColor:"#7a9ab8"},
    grid:{vertLines:{color:"#162840"},horzLines:{color:"#162840"}},
    crosshair:{mode:LC.CrosshairMode.Normal},
    rightPriceScale:{borderColor:"#162840",scaleMargins:{top:.08,bottom:.08}},
    timeScale:{borderColor:"#162840",timeVisible:true,secondsVisible:false,rightOffset:6},
    handleScroll:{mouseWheel:true,pressedMouseMove:true},
    handleScale:{mouseWheel:true,pinch:true},
  });
  _candles = _chart.addCandlestickSeries({
    upColor:"#00e676", downColor:"#f44336",
    borderUpColor:"#00e676", borderDownColor:"#f44336",
    wickUpColor:"#00e676", wickDownColor:"#f44336",
  });
  // サイズ自動追従
  new ResizeObserver(()=>{
    if(_chart) _chart.applyOptions({width:$("chart-el").clientWidth});
  }).observe($("chart-el"));
}

function setChartSym(sym) {
  _chartSym = sym;
  const t = $("chart-sym-title"); if (t) t.textContent = "📈 " + sym + " チャート";
  updateChart();
}

async function updateChart() {
  if (!_candles) return;
  let d;
  try { d = await (await fetch("/api/chart?tf="+_chartTf+"&sym="+encodeURIComponent(_chartSym))).json(); } catch { return; }

  if (d.candles && d.candles.length > 0) {
    _candles.setData(d.candles);
    _chart.timeScale().fitContent();
  }

  // エントリー・決済マーカーを表示
  if (d.markers && d.markers.length > 0) {
    _candles.setMarkers(d.markers);
  } else {
    _candles.setMarkers([]);
  }

  // 既存のライン削除
  _pricelines.forEach(pl => { try { _candles.removePriceLine(pl); } catch{} });
  _pricelines = [];

  const pos = Object.values(d.positions||{});
  if (pos.length > 0) {
    const lb = $("legend-bar"); if (lb) lb.style.display = "flex";
    pos.forEach(p => {
      // エントリーライン（青点線）
      if (p.entry_price) {
        _pricelines.push(_candles.createPriceLine({
          price: p.entry_price, color:"#4fc3f7", lineWidth:1,
          lineStyle:2, axisLabelVisible:true,
          title:"📍 エントリー",
        }));
      }
      // 利確ライン（緑実線）
      if (p.tp_price) {
        const tpPct = ((p.tp_price - p.entry_price) / p.entry_price * 100 * (p.leverage||2)).toFixed(1);
        _pricelines.push(_candles.createPriceLine({
          price: p.tp_price, color:"#00e676", lineWidth:2,
          lineStyle:0, axisLabelVisible:true,
          title:`🎯 利確目標 (+${tpPct}%)`,
        }));
      }
      // 損切りライン（赤実線）
      if (p.sl_price) {
        const slPct = Math.abs((p.sl_price - p.entry_price) / p.entry_price * 100 * (p.leverage||2)).toFixed(1);
        _pricelines.push(_candles.createPriceLine({
          price: p.sl_price, color:"#f44336", lineWidth:2,
          lineStyle:0, axisLabelVisible:true,
          title:`🛑 損切り (-${slPct}%)`,
        }));
      }
      // 凡例に詳細表示
      if (p.entry_price && p.tp_price && p.sl_price) {
        const tpD = Math.abs(p.tp_price - p.entry_price).toFixed(2);
        const slD = Math.abs(p.sl_price - p.entry_price).toFixed(2);
        $("leg-detail").textContent =
          `利確まで $${tpD} | 損切りまで $${slD} | レバ ${(p.leverage||2).toFixed(0)}倍`;
      }
    });
  } else {
    const lb2 = $("legend-bar"); if (lb2) lb2.style.display = "none";
  }
}

function setTf(tf) {
  _chartTf = tf;
  ["1m","5m","15m"].forEach(t => {
    $("tfb-"+t).classList.toggle("on", t===tf);
  });
  updateChart();
}

// ─── タブ切替 ───
function sw(tab) {
  _tab = tab;
  [["live","t-live","p-live"],["pf","t-pf","p-pf"],["scan","t-scan","p-scan"],["hist","t-hist","p-hist"],["analysis","t-analysis","p-analysis"]].forEach(([n,ti,pi])=>{
    $(ti).classList.toggle("on", n===tab);
    $(pi).classList.toggle("on", n===tab);
  });
  // 📝 ログタブ（モバイル専用: 右パネルをタブ内に展開）
  const tLog = $("t-log");
  if (tLog) tLog.classList.toggle("on", tab === "log");
  const rp = document.querySelector(".rpanel");
  if (rp) rp.classList.toggle("mob-show", tab === "log");
  // ログタブ選択時は他のtpaneを非表示
  if (tab === "log") {
    document.querySelectorAll(".tpane").forEach(el => el.classList.remove("on"));
  }

  if (tab === "pf") {
    setTimeout(() => { initEqChart(); updateEqChart(); updateRanking(); }, 100);
  }
  if (tab === "scan") {
    runManualScan();  // スキャンタブを開いたら自動でスキャン実行
  }
}

// ─── 手動スキャン ───
let _scanTimer = null;
async function runManualScan() {
  const btn = $("scan-btn");
  const status = $("scan-status");
  btn.disabled = true;
  btn.textContent = "⏳ スキャン中...";
  btn.style.opacity = "0.6";
  // プログレス演出（ドット点滅）
  let dots = 0;
  _scanTimer = setInterval(() => {
    dots = (dots + 1) % 4;
    status.textContent = "🔍 上位30銘柄をリアルタイムスキャン中" + ".".repeat(dots);
  }, 400);
  try {
    const res = await (await fetch("/api/manual_scan?top=30")).json();
    const candidates = res.candidates || [];
    const ready = candidates.filter(c => c.ready).length;
    const watching = candidates.filter(c => !c.ready).length;
    status.textContent = ready > 0
      ? `✅ ${ready}件がエントリー推奨 / ${watching}件が監視継続（${new Date().toLocaleTimeString("ja-JP")} スキャン完了）`
      : candidates.length > 0
        ? `👀 エントリー推奨なし / ${watching}件を監視中（${new Date().toLocaleTimeString("ja-JP")} スキャン完了）`
        : `😐 有力候補なし — 相場がレンジ状態かもしれません（${new Date().toLocaleTimeString("ja-JP")}）`;
    renderScanResults(candidates);
  } catch(e) {
    status.textContent = "❌ スキャン失敗: " + e.message;
  } finally {
    clearInterval(_scanTimer);
    btn.disabled = false;
    btn.textContent = "🔍 今すぐスキャン";
    btn.style.opacity = "1";
  }
}

function renderScanResults(candidates) {
  const tbody = $("scan-tbody");
  if (candidates.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;padding:24px;color:var(--muted)">
      😐 スコアが低い銘柄のみです。相場がレンジ（横ばい）状態です。しばらく待って再スキャンしてください。
    </td></tr>`;
    return;
  }
  tbody.innerHTML = candidates.map(c => {
    const isLong  = c.signal === "long";
    const isShort = c.signal === "short";
    const isHold  = c.signal === "hold";
    const sigColor = isLong ? "#4caf50" : isShort ? "#ef5350" : "#888";
    const sigLabel = isLong ? "🟢 LONG" : isShort ? "🔴 SHORT" : "⬜ 監視中";
    const scoreBar = Math.round(c.score * 100);
    const trendIcon = c.trend_1h === "up" ? "↑" : c.trend_1h === "down" ? "↓" : "→";
    const trendColor = c.trend_1h === "up" ? "#4caf50" : c.trend_1h === "down" ? "#ef5350" : "#aaa";
    const rowOpacity = isHold ? "0.65" : "1";
    const readyBadge = c.ready
      ? `<span style="background:#1b5e20;color:#a5d6a7;font-size:9px;padding:2px 6px;border-radius:4px;font-weight:700">推奨</span>`
      : isHold
        ? `<span style="background:#333;color:#888;font-size:9px;padding:2px 6px;border-radius:4px">様子見</span>`
        : `<span style="background:#4a2000;color:#ffcc80;font-size:9px;padding:2px 6px;border-radius:4px">弱シグナル</span>`;
    const entryBtn = (!isHold)
      ? `<button class="entry-btn" onclick="doManualEntry('${c.symbol}','${c.signal}')"
          style="background:${isLong ? 'linear-gradient(135deg,#1b5e20,#2e7d32)' : 'linear-gradient(135deg,#7f0000,#c62828)'};
                 color:#fff;border:none;border-radius:10px;
                 font-weight:800;cursor:pointer;white-space:nowrap;
                 padding:12px 22px;font-size:15px;min-height:48px;min-width:96px;
                 box-shadow:0 3px 8px rgba(0,0,0,0.25);
                 touch-action:manipulation;
                 transition:transform 0.1s, box-shadow 0.1s">
          ${isLong ? '🟢 買い' : '🔴 売り'}
        </button>`
      : `<span style="color:var(--muted2);font-size:11px">—</span>`;
    return `<tr style="border-bottom:1px solid var(--border2);opacity:${rowOpacity};transition:background .15s"
              onmouseover="this.style.background='var(--bg3)'" onmouseout="this.style.background=''">
      <td style="padding:8px;font-weight:700;color:var(--text)">${c.symbol}</td>
      <td style="padding:8px;text-align:center;color:${sigColor};font-weight:700">${sigLabel}</td>
      <td style="padding:8px;text-align:right">
        <div style="display:inline-flex;align-items:center;gap:6px">
          <div style="width:50px;height:6px;background:var(--bg4);border-radius:3px;overflow:hidden">
            <div style="width:${scoreBar}%;height:100%;background:${sigColor};border-radius:3px"></div>
          </div>
          <span style="color:${sigColor};font-weight:700">${(c.score*100).toFixed(0)}%</span>
        </div>
      </td>
      <td style="padding:8px;text-align:right;font-family:monospace">${P(c.price)}</td>
      <td style="padding:8px;text-align:right;color:#4caf50;font-family:monospace">${c.tp_price > 0 ? `↑${P(c.tp_price)}<br><span style="font-size:10px;color:var(--muted2)">+${c.tp_dist_pct}%</span>` : "—"}</td>
      <td style="padding:8px;text-align:right;color:#ef5350;font-family:monospace">${c.sl_price > 0 ? `↓${P(c.sl_price)}` : "—"}</td>
      <td style="padding:8px;text-align:center;color:${trendColor};font-weight:700">${trendIcon} ${c.trend_1h}</td>
      <td style="padding:8px;text-align:center">${readyBadge}</td>
      <td style="padding:8px;text-align:center">${entryBtn}</td>
    </tr>`;
  }).join("");
}

async function doManualEntry(symbol, side) {
  if (!confirm(`${symbol} を ${side === 'long' ? 'ロング（買い）' : 'ショート（売り）'} でエントリーしますか？`)) return;
  try {
    const res = await (await fetch("/api/manual_entry", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({symbol, side})
    })).json();
    if (res.ok) {
      alert(`✅ エントリー成功！\n${symbol} ${side.toUpperCase()}\n価格: $${res.price}\nTP: $${res.tp_price}\nSL: $${res.sl_price}`);
      runManualScan();  // 再スキャンして候補リストを更新
    } else {
      alert(`❌ エントリー失敗\n${res.reason}`);
    }
  } catch(e) {
    alert("❌ 通信エラー: " + e.message);
  }
}

// ─── ボット制御 ───
async function ctrlBot(a) {
  try { await fetch("/api/control",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:a})}); } catch{}
}

// ─── ログクリア ───
function clrLog() { _logCleared=true; _logCache=[]; $("logbox").innerHTML=""; }

// ─── 💰 税率切替関数（localStorage保存）───
function setTaxRate(rate) {
  localStorage.setItem("taxRate", rate);
  update();  // 即座に再計算
}

// ─── ライブ取引タブ フィルター ───
let _liveFilter = "all";
function setLiveFilter(mode) {
  _liveFilter = mode;
  const btns = ["all","long","short","hot"];
  btns.forEach(b => {
    const el = $("live-filter-" + b);
    if (!el) return;
    if (b === mode) {
      el.style.background = "var(--blue-bg)";
      el.style.color = "var(--blue)";
      el.style.border = "1px solid #4fc3f740";
    } else {
      el.style.background = "var(--bg4)";
      el.style.color = "var(--muted2)";
      el.style.border = "1px solid var(--border2)";
    }
  });
  update();  // 即座に再描画
}

// ─── メイン更新 ───
async function update() {
  let d;
  try { d = await (await fetch("/api/state")).json(); } catch { return; }
  if (d.error) { $("scaninfo").textContent="⚠️ "+d.error; return; }

  const lev  = d.leverage||2, bal=d.balance||0, eq=d.total_equity||bal;
  const upnl = d.unrealized_pnl||0, rpnl=d.realized_pnl||0;
  const today= d.today_pnl||0, todayPct=d.today_pnl_pct||0;
  const dd   = d.drawdown_pct||0, wr=d.win_rate||0;
  const won  = d.won_count||0, lost=d.lost_count||0;
  const posM = d.positions||{}, posK=Object.keys(posM);
  const sig  = d.last_signal||{}, cp=d.current_price;

  // ヘッダー
  $("mbadge").textContent = d.mode==="simulation" ? "SIM" : "LIVE";
  $("levbadge").textContent = lev.toFixed(1)+"倍";
  $("scaninfo").textContent = `スキャン ${d.scan_count||0}回 | ${new Date().toLocaleTimeString("ja-JP")}`;
  $("ldot").style.background = d.is_halted ? "var(--red)" : "var(--green)";

  // 統計
  const setS = (id,v,cls,sub,id2,s2) => {
    $(id).textContent=v; $(id).className="sb-val "+(cls||"");
    if(id2) $(id2).textContent=s2||"";
  };
  setS("s-eq", USD(eq), COL(eq-(d.initial||bal)), "残高", "s-eq2", "残高 "+USD(bal));
  setS("s-up", SIGN(upnl)+USD(upnl), COL(upnl), "", "s-up2", PCT((upnl/(d.initial||1))*100/100));
  setS("s-rp", SIGN(rpnl)+USD(rpnl), COL(rpnl), "", "s-rp2", d.realized_pnl_pct!=null?PCT(d.realized_pnl_pct):"");
  setS("s-td", SIGN(today)+USD(today), COL(today), "", "s-td2", PCT(todayPct));

  // ─── 💰 税金計算（日本の仮想通貨FX・雑所得・総合課税）───
  // 課税対象: 実現損益(rpnl)のみ ※含み益(upnl)は確定前なので対象外
  // 日本の仮想通貨FXは株FXと違い「申告分離課税20%」ではなく累進課税（最大55%）
  // 「auto」: 年間利益を予測して累進ブラケットで税額を計算
  // 固定税率: 手動で選択した合計税率を適用
  const JPY_PER_USD = 150;  // USD→JPY換算レート（変動するので目安）

  // 日本の累進税額計算（年間課税所得→税額）※雑所得のみと仮定
  function japanProgressiveTax(profitJPY) {
    if (profitJPY <= 0) return 0;
    // 所得税 = 課税所得 × 税率 − 控除額
    const brackets = [
      { limit: 1950000,  rate: 0.05, deduction: 0 },
      { limit: 3300000,  rate: 0.10, deduction: 97500 },
      { limit: 6950000,  rate: 0.20, deduction: 427500 },
      { limit: 9000000,  rate: 0.23, deduction: 636000 },
      { limit: 18000000, rate: 0.33, deduction: 1536000 },
      { limit: 40000000, rate: 0.40, deduction: 2796000 },
      { limit: Infinity, rate: 0.45, deduction: 4796000 },
    ];
    let incomeTax = 0;
    for (const b of brackets) {
      if (profitJPY <= b.limit) {
        incomeTax = profitJPY * b.rate - b.deduction;
        break;
      }
    }
    const reconstTax = incomeTax * 0.021;    // 復興特別所得税2.1%
    const residentTax = profitJPY * 0.10;    // 住民税10%
    return incomeTax + reconstTax + residentTax;
  }

  const savedRate = localStorage.getItem("taxRate") || "auto";
  const taxRateSelect = document.getElementById("tax-rate-select");
  if (taxRateSelect && taxRateSelect.value !== savedRate) taxRateSelect.value = savedRate;

  // 実現利益がプラスの時だけ税金計算（マイナスなら税金ゼロ）
  const taxableProfit = rpnl > 0 ? rpnl : 0;
  let taxAmount = 0;
  let effectiveRate = 0;
  let taxModeLabel = "";

  if (savedRate === "auto") {
    // 年間利益予測 → 累進税額 → 現時点の割合に換算
    const elapsedSec = Math.max(d.elapsed_sec || 1, 3600);  // 最低1時間
    const annualizeRatio = (365 * 24 * 3600) / elapsedSec;
    const annualProfitUSD = taxableProfit * annualizeRatio;
    const annualProfitJPY = annualProfitUSD * JPY_PER_USD;
    const annualTaxJPY = japanProgressiveTax(annualProfitJPY);
    effectiveRate = annualProfitJPY > 0 ? annualTaxJPY / annualProfitJPY : 0;
    taxAmount = taxableProfit * effectiveRate;  // 現時点の実現益に実効税率を適用
    taxModeLabel = `累進 年予測$${annualProfitUSD.toFixed(0)}→実効${(effectiveRate*100).toFixed(1)}%`;
  } else {
    const taxRate = parseFloat(savedRate);
    effectiveRate = taxRate;
    taxAmount = taxableProfit * taxRate;
    taxModeLabel = `固定${(taxRate*100).toFixed(0)}%`;
  }

  const afterTax = rpnl - taxAmount;  // 実現利益 - 税金

  // 💰 推定税金ボックス
  const taxEl = $("s-tax");
  if (taxEl) {
    taxEl.textContent = taxAmount > 0 ? "-" + USD(taxAmount) : "$0.00";
  }
  const taxSubEl = $("s-tax-sub");
  // (ラベルの「雑所得」部分だけ残す。セレクター自体はHTMLに埋め込み済み)

  // 💵 税引き後利益ボックス
  const aftertaxEl = $("s-aftertax");
  if (aftertaxEl) {
    aftertaxEl.textContent = SIGN(afterTax) + USD(afterTax);
  }
  const aftertax2El = $("s-aftertax2");
  if (aftertax2El) {
    if (rpnl > 0) {
      const afterTaxPct = d.initial ? (afterTax / d.initial * 100) : 0;
      aftertax2El.textContent = `${taxModeLabel} +${afterTaxPct.toFixed(2)}%`;
    } else {
      aftertax2El.textContent = "損失時は税金なし";
    }
  }
  setS("s-wr", wr.toFixed(1)+"%", wr>=55?"g":wr>=45?"y":"r", "", "s-wrs", won+"勝"+lost+"敗");
  setS("s-lv", lev.toFixed(1)+"倍", "y");
  $("s-st").textContent = d.is_halted?"⚠️ 24h停止中":d.is_cooling_down?"⏳ 冷却中":"✅ 正常稼働";
  $("s-st").style.color = d.is_halted?"var(--red)":d.is_cooling_down?"var(--yellow)":"var(--green)";
  // 冷却中のときだけ「冷却解除・再開」ボタンを表示
  if($("btn-resume")) $("btn-resume").style.display = d.is_cooling_down?"inline-block":"none";
  setS("s-ps", posK.length+"/"+(d.max_positions||5)+"件", "b", "", "s-sc", (d.scanned_count||0)+"/"+(d.watch_symbols?.length||25)+"銘柄");

  // ─── ① 検証経過 ───
  // サーバ側で計算された elapsed_text を優先使用（ブラウザキャッシュ影響を受けない）
  if (d.elapsed_text) {
    setS("s-elapsed", d.elapsed_text, "c");
    $("s-scancount").textContent = `スキャン ${(d.scan_count||0).toLocaleString()}回`;
  } else {
    // フォールバック: JS側で計算（古いサーバ用）
    if (!window._firstEqTs) {
      try {
        const eq = await (await fetch("/api/equity")).json();
        if (eq.history && eq.history.length > 0) {
          const first = eq.history[0];
          window._firstEqTs = first.ts || first.timestamp || first.time || d.bot_started_at;
        } else {
          window._firstEqTs = d.bot_started_at;
        }
      } catch { window._firstEqTs = d.bot_started_at; }
    }
    const startTs = window._firstEqTs || d.bot_started_at;
    if (startTs) {
      const elapsedSec = Math.floor(Date.now()/1000 - startTs);
      const totalHours = Math.floor(elapsedSec / 3600);
      const mins = Math.floor((elapsedSec % 3600) / 60);
      const secs = elapsedSec % 60;
      let elapsedText;
      if (totalHours >= 1) {
        elapsedText = `${totalHours}時間${mins}分`;
      } else if (mins >= 1) {
        elapsedText = `${mins}分${secs}秒`;
      } else {
        elapsedText = `${secs}秒`;
      }
      setS("s-elapsed", elapsedText, "c");
      $("s-scancount").textContent = `スキャン ${(d.scan_count||0).toLocaleString()}回`;
    }
  }

  // ─── ② 連敗ストリーク（現在・過去最大）───
  const curStreak = d.consecutive_losses || 0;
  let maxStreak = 0, tmpStreak = 0;
  const th = d.trade_history || [];
  // trade_historyは新しい順なので逆順で走査
  for (let i = th.length - 1; i >= 0; i--) {
    if (th[i].won === false) { tmpStreak++; if (tmpStreak > maxStreak) maxStreak = tmpStreak; }
    else tmpStreak = 0;
  }
  if (curStreak > maxStreak) maxStreak = curStreak;
  setS("s-streak", curStreak.toString(), curStreak >= 4 ? "r" : curStreak >= 2 ? "y" : "g");
  $("s-streak-max").textContent = `最大連敗 ${maxStreak}`;

  // ─── ③ プロフィットファクター(PF)・Sharpe比 ───
  let sumWin = 0, sumLoss = 0, pnlArr = [];
  for (const t of th) {
    const p = t.pnl || 0;
    if (p > 0) sumWin += p;
    else sumLoss += Math.abs(p);
    pnlArr.push(p);
  }
  const pf = sumLoss > 0 ? sumWin / sumLoss : (sumWin > 0 ? 99 : 0);
  setS("s-pf", pf >= 99 ? "∞" : pf.toFixed(2), pf >= 1.5 ? "g" : pf >= 1.0 ? "y" : "r");
  // Sharpe比（簡易: トレード単位でのmean/std × sqrt(N)）
  let sharpe = 0;
  if (pnlArr.length >= 5) {
    const mean = pnlArr.reduce((a,b)=>a+b,0) / pnlArr.length;
    const variance = pnlArr.reduce((a,b)=>a+(b-mean)**2,0) / pnlArr.length;
    const std = Math.sqrt(variance);
    sharpe = std > 0 ? mean / std * Math.sqrt(pnlArr.length) : 0;
  }
  $("s-sharpe").textContent = `Sharpe ${sharpe.toFixed(2)}`;

  // ─── ⑤ 手数料累計・1回あたり手数料（実データベース）───
  // size_usdが入ってる場合はその値で、無い場合は現在のオープンポジション平均で推定
  const feeRate = 0.0004;  // Binance futures taker (往復で×2)
  // オープンポジションから平均ポジションサイズを計算（size_usdのフォールバック用）
  let openSizeAvg = 100;
  if (posK.length > 0) {
    const openSizes = posK.map(k => posM[k].size_usd || 0).filter(s => s > 0);
    if (openSizes.length > 0) {
      openSizeAvg = openSizes.reduce((a,b)=>a+b, 0) / openSizes.length;
    }
  }
  let totalFees = 0;
  let realFeeCount = 0;  // 実size_usdで計算できた件数
  for (const t of th) {
    const sz = (t.size_usd && t.size_usd > 0) ? t.size_usd : openSizeAvg;
    if (t.size_usd && t.size_usd > 0) realFeeCount++;
    totalFees += sz * feeRate * 2;  // 往復で×2
  }
  // closed_tradesが多ければ全体推定に補正（trade_historyは直近50件のみ）
  const totalTrades = d.closed_trades || th.length;
  if (totalTrades > th.length && th.length > 0) {
    totalFees = totalFees * (totalTrades / th.length);
  }
  const avgFeePerTrade = totalTrades > 0 ? totalFees / totalTrades : 0;

  setS("s-fees", "$"+totalFees.toFixed(2), "o");
  // サブ表示：1回あたり + 損益比 + データソース精度
  const feeRatio = rpnl !== 0 ? (totalFees / Math.abs(rpnl) * 100) : 0;
  const dataHint = realFeeCount >= th.length ? "実" : realFeeCount > 0 ? "推" : "概";
  $("s-fees-ratio").textContent = `${dataHint}$${avgFeePerTrade.toFixed(3)}/回・損益比${feeRatio.toFixed(1)}%`;

  // DD
  $("ddnum").textContent = "-"+dd.toFixed(2)+"%";
  $("ddnum").className = "dd-num "+(dd>15?"r":dd>8?"y":"g");
  const ddw = Math.min(100,dd/20*100);
  $("ddfill").style.width = ddw+"%";
  $("ddfill").style.background = dd>15?"var(--red)":dd>8?"var(--yellow)":"var(--green)";
  $("ddpill").textContent = dd>15?"危険":dd>8?"注意":"正常";
  $("ddpill").className = "dd-pill "+(dd>15?"ddp-danger":dd>8?"ddp-warn":"ddp-ok");
  // ── Fear & Greed / Funding Rate / Market Breadth ──
  const ctx = d.market_context || {};
  if (ctx.is_ready) {
    const fg = ctx.fear_greed ?? 50;
    const fgEl = $("fg-val");
    if (fgEl) {
      fgEl.textContent = (ctx.fear_greed_emoji || "") + fg;
      fgEl.className = fg <= 25 ? "g" : fg >= 75 ? "r" : "y";
    }
    const fgLabelMap = {
      "Extreme Fear": "極度の恐怖", "Fear": "恐怖",
      "Neutral": "中立", "Greed": "強欲", "Extreme Greed": "極度の強欲"
    };
    if ($("fg-lbl")) $("fg-lbl").textContent = fgLabelMap[ctx.fear_greed_label] || ctx.fear_greed_label || "";
    const fr = ctx.avg_funding_rate ?? 0;
    const frEl = $("fr-val");
    if (frEl) {
      frEl.textContent = (fr >= 0 ? "+" : "") + fr.toFixed(4) + "%";
      frEl.className = fr > 0.01 ? "r" : fr < -0.01 ? "g" : "y";
    }
    const mb = ctx.market_bullish_pct ?? 50;
    const mbEl = $("mb-val");
    if (mbEl) {
      mbEl.textContent = mb.toFixed(0) + "%";
      mbEl.className = mb >= 60 ? "g" : mb <= 40 ? "r" : "y";
    }
  }
  $("btcprice").textContent = cp ? "BTC $"+cp.toLocaleString("en-US",{maximumFractionDigits:2}) : "BTC: —";

  // ── 1日損失バー ──
  const dlLimit = d.daily_loss_limit_pct || 5;
  const todayLossPct = Math.max(0, -(d.today_pnl_pct || 0));  // 損失のみ（プラスの場合は0）
  const dlW = Math.min(100, todayLossPct / dlLimit * 100);
  const dlEl = $("dlbar-fill");
  if (dlEl) {
    dlEl.style.width = dlW + "%";
    dlEl.style.background = dlW > 80 ? "var(--red)" : dlW > 50 ? "var(--yellow)" : "var(--green)";
  }
  const dlPct = $("dlbar-pct");
  if (dlPct) {
    dlPct.textContent = todayLossPct.toFixed(1) + "%";
    dlPct.className = "dlbar-pct " + (dlW > 80 ? "r" : dlW > 50 ? "y" : "g");
  }
  // 5%超過カウンター表示（1回以上あるときだけ表示）
  const breachCount = d.daily_limit_breach_count || 0;
  const bw = $("breach-wrap");
  if (bw) { bw.style.display = breachCount > 0 ? "flex" : "none"; }
  if ($("breach-count")) $("breach-count").textContent = breachCount;

  // ── クールダウンカウントダウン ──
  const cdbar = $("cdbar");
  if (cdbar) {
    if (d.is_cooling_down && d.cooldown_remaining_m > 0) {
      const totalSecs = d.cooldown_remaining_m * 60;
      const mins = Math.floor(d.cooldown_remaining_m);
      const secs = Math.floor((d.cooldown_remaining_m % 1) * 60);
      $("cd-txt").textContent = `冷却中 — あと ${mins}分 ${secs}秒 で自動再開`;
      const maxCooldown = 60 * 60;  // 最大1時間
      $("cd-fill").style.width = Math.min(100, (totalSecs / maxCooldown) * 100) + "%";
      cdbar.style.display = "flex";
    } else {
      cdbar.style.display = "none";
    }
  }

  // ── ブラウザ通知（エントリー・決済検知） ──
  const currPos = d.positions || {};
  const currTradeCount = d.closed_trades || 0;
  // 新しいポジションが開いたとき
  Object.keys(currPos).forEach(sym => {
    if (!_prevPositions[sym]) {
      const p = currPos[sym];
      const dir = p.side === "long" ? "ロング（買い）" : "ショート（売り）";
      sendNotify(`🟢 エントリー: ${sym}`, `${dir} @ $${(p.entry_price||0).toFixed(4)}`);
    }
  });
  // ポジションが閉じたとき（取引が完了したとき）
  if (currTradeCount > _prevTradeCount && _prevTradeCount > 0) {
    const last = (d.trade_history || [])[0];
    if (last) {
      const icon = last.won ? "✅" : "❌";
      const label = last.won ? "利確" : "損切り";
      const pnlStr = (last.pnl >= 0 ? "+" : "") + "$" + Math.abs(last.pnl).toFixed(2);
      sendNotify(`${icon} ${label}: ${last.symbol}`, `損益: ${pnlStr} (${last.pnl_pct >= 0 ? "+" : ""}${last.pnl_pct.toFixed(2)}%)`);
    }
  }
  _prevPositions = {...currPos};
  _prevTradeCount = currTradeCount;

  // ライブ取引テーブル（25銘柄シグナル一覧）
  const perSig = d.per_signal || {};
  const watchSyms = d.watch_symbols || ["BTC/USDT"];
  const tb = $("ltbody");
  const maxPos = d.max_positions || 5;

  // ─── 保有ポジションカード表示 ───
  const livePos = $("live-positions");
  const livePosCnt = $("live-pos-count");
  if (livePos) {
    if (posK.length === 0) {
      livePos.innerHTML = '<div style="color:var(--muted2);font-size:12px;padding:8px">保有ポジションなし — エントリー待機中</div>';
      if (livePosCnt) livePosCnt.textContent = `0件 / 最大${maxPos}件`;
    } else {
      const cards = posK.map(sym => {
        const p = posM[sym];
        const upnl = p.upnl || 0;
        const upnlPct = p.upnl_pct || 0;
        const cls = upnl >= 0 ? "g" : "r";
        const ageMin = Math.floor((p.age_s || 0) / 60);
        const sideTxt = p.side === "long" ? "🟢 LONG" : "🔴 SHORT";
        return `<div style="background:var(--bg3);border:1px solid var(--border2);border-radius:6px;padding:10px;display:flex;flex-direction:column;gap:4px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="font-weight:800;font-size:14px">${sym}</span>
            <span style="font-size:11px;color:var(--muted2)">${sideTxt} ${p.leverage}x</span>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted2)">
            <span>エントリー: $${p.entry_price}</span>
            <span>現在: $${p.current_price}</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px">
            <span class="${cls}" style="font-weight:900;font-size:16px">${upnl>=0?"+":""}$${upnl.toFixed(2)}</span>
            <span class="${cls}" style="font-size:12px;font-weight:700">${upnlPct>=0?"+":""}${upnlPct.toFixed(2)}%</span>
          </div>
          <div style="font-size:10px;color:var(--muted)">保有${ageMin}分 | TP $${p.tp_price?.toFixed?.(p.tp_price > 1 ? 2 : 5) || p.tp_price} / SL $${p.sl_price?.toFixed?.(p.sl_price > 1 ? 2 : 5) || p.sl_price}</div>
        </div>`;
      }).join("");
      livePos.innerHTML = cards;
      if (livePosCnt) livePosCnt.textContent = `${posK.length}件 / 最大${maxPos}件`;
    }
  }

  if (Object.keys(perSig).length === 0) {
    tb.innerHTML = `<tr><td colspan="7"><div class="empty"><div class="ei">🔍</div><div class="et">銘柄スキャン中...</div><div class="es">25銘柄を順番にスキャンしています<br>最初のシグナルまで約30秒かかります</div></div></td></tr>`;
  } else {
    // 並び順: 1)保有中 2)シグナルあり（スコア高い順）3)スキャン済み待機中
    const sortedSyms = watchSyms.slice().sort((a, b) => {
      const ap = posM[a], bp = posM[b];
      const as = perSig[a], bs_ = perSig[b];
      if (ap && !bp) return -1;
      if (!ap && bp) return 1;
      const aScore = as?.score || 0, bScore = bs_?.score || 0;
      const aSig = as?.signal !== "HOLD" ? 1 : 0, bSig = bs_?.signal !== "HOLD" ? 1 : 0;
      if (aSig !== bSig) return bSig - aSig;
      return bScore - aScore;
    });

    let rows = "";
    sortedSyms.forEach(sym => {
      const ps  = perSig[sym];
      const pos = posM[sym];
      if (!ps && !pos) return;  // まだスキャンしていない銘柄はスキップ

      // ── フィルター適用 ──
      if (_liveFilter === "long") {
        if (pos?.side !== "long" && ps?.signal !== "LONG") return;
      } else if (_liveFilter === "short") {
        if (pos?.side !== "short" && ps?.signal !== "SHORT") return;
      } else if (_liveFilter === "hot") {
        if (!pos && (ps?.score || 0) < 0.5) return;
      }

      const sigDir  = ps?.signal || "HOLD";
      const score2  = ps?.score  || 0;
      const scol    = score2 >= .7 ? "var(--green)" : score2 >= .5 ? "var(--yellow)" : "var(--muted2)";
      const sw2     = Math.round(score2 * 100);
      const symPriceData = d.current_price && sym === "BTC/USDT" ? d.current_price : (pos?.current_price || null);
      const dispPrice = pos?.current_price || symPriceData;

      if (pos) {
        // 保有中ポジション行
        const pu = pos.upnl || 0, pp = pos.upnl_pct || 0;
        const isl = pos.side === "long";
        const amin = Math.floor((pos.age_s || 0) / 60);
        const tpPct = pos.tp_price && pos.entry_price
          ? ((pos.tp_price - pos.entry_price) / pos.entry_price * 100 * (pos.leverage||2)).toFixed(1) : "—";
        const slPct = pos.sl_price && pos.entry_price
          ? Math.abs((pos.sl_price - pos.entry_price) / pos.entry_price * 100 * (pos.leverage||2)).toFixed(1) : "—";
        rows += `<tr class="${isl?"lrow":"srow"}" onclick="setChartSym('${sym}')" style="cursor:pointer">
          <td>
            <span class="pill ${isl?"pl-g":"pl-r"}">${isl?"保有 ↑":"保有 ↓"}</span>
            <div style="font-size:10px;color:var(--yellow);margin-top:3px">${(pos.leverage||2).toFixed(0)}倍レバ</div>
          </td>
          <td>
            <div style="font-size:13px;font-weight:900;color:#fff">${sym.replace("/USDT","")}</div>
            <div style="font-size:10px;color:var(--muted2);margin-top:1px">エントリー: ${P(pos.entry_price)}</div>
            <div style="font-size:10px;color:var(--muted);margin-top:1px">${amin}分保有中</div>
          </td>
          <td>
            <div class="scbar">
              <div class="sctrack"><div class="scfill" style="width:${sw2}%;background:${scol}"></div></div>
              <span class="scnum" style="color:${scol}">${sw2}</span>
            </div>
          </td>
          <td>
            <div style="font-size:14px;font-weight:900;color:#fff">${P(pos.current_price)}</div>
            <div style="font-size:10px;color:var(--muted);margin-top:1px">現在値</div>
          </td>
          <td style="text-align:center">
            <div style="color:${isl?"var(--green)":"var(--red)"};font-size:13px;font-weight:900">${isl?"↑ LONG":"↓ SHORT"}</div>
          </td>
          <td>
            <div style="color:var(--green);font-size:11px;font-weight:700">🎯 ${P(pos.tp_price)} <span style="color:var(--muted2)">+${tpPct}%</span></div>
            <div style="color:var(--red);font-size:11px;font-weight:700;margin-top:3px">🛑 ${P(pos.sl_price)} <span style="color:var(--muted2)">-${slPct}%</span></div>
          </td>
          <td style="text-align:right">
            <div style="font-size:14px;font-weight:900" class="${COL(pu)}">${SIGN(pp)}${pp.toFixed(2)}%</div>
            <div style="font-size:10px;color:var(--muted);margin-top:1px">${SIGN(pu)}$${Math.abs(pu).toFixed(2)}</div>
          </td>
        </tr>`;
      } else if (sigDir !== "HOLD") {
        // シグナルあり・未保有行（スコアが高い順に上位表示）
        const isl = sigDir === "LONG";
        rows += `<tr class="${isl?"lrow":"srow"}" onclick="setChartSym('${sym}')" style="cursor:pointer;opacity:.9">
          <td>
            <span class="pill pl-y">シグナル</span>
            <div style="font-size:10px;color:var(--muted2);margin-top:3px">${posK.length >= maxPos ? "上限到達" : "エントリー待"}</div>
          </td>
          <td>
            <div style="font-size:13px;font-weight:900;color:#fff">${sym.replace("/USDT","")}</div>
            <div style="font-size:10px;color:var(--muted2);margin-top:1px">USDT ペア</div>
          </td>
          <td>
            <div class="scbar">
              <div class="sctrack"><div class="scfill" style="width:${sw2}%;background:${scol}"></div></div>
              <span class="scnum" style="color:${scol}">${sw2}</span>
            </div>
          </td>
          <td>
            <div style="font-size:13px;font-weight:700;color:var(--text)">—</div>
            <div style="font-size:10px;color:var(--muted)">取得中</div>
          </td>
          <td style="text-align:center">
            <div style="color:${isl?"var(--green)":"var(--red)"};font-size:13px;font-weight:900">${isl?"↑ LONG":"↓ SHORT"}</div>
          </td>
          <td><div style="color:var(--muted2);font-size:11px">—</div></td>
          <td style="text-align:right"><div style="color:var(--muted2)">—</div></td>
        </tr>`;
      } else {
        // スキャン済み待機中（折りたたみ表示）
        rows += `<tr style="opacity:.4">
          <td><span class="pill pl-gray">待機中</span></td>
          <td><div style="font-size:12px;font-weight:700;color:var(--muted2)">${sym.replace("/USDT","")}</div></td>
          <td>
            <div class="scbar">
              <div class="sctrack"><div class="scfill" style="width:${sw2}%;background:var(--border2)"></div></div>
              <span class="scnum" style="color:var(--muted)">${sw2}</span>
            </div>
          </td>
          <td><div style="font-size:12px;color:var(--muted2)">—</div></td>
          <td style="text-align:center"><div style="color:var(--muted);font-size:11px">→ 中立</div></td>
          <td><div style="color:var(--muted2);font-size:10px">条件未到達</div></td>
          <td></td>
        </tr>`;
      }
    });
    tb.innerHTML = rows || `<tr><td colspan="7"><div class="empty"><div class="ei">⏳</div><div class="et">スキャン中...</div></div></td></tr>`;
  }

  // ポートフォリオタブ
  const pb=$("pf-b"); pb.textContent=USD(bal); pb.className="pfv "+COL(bal-(d.initial||bal));
  const pp2=$("pf-p"); pp2.textContent=SIGN(rpnl)+USD(rpnl); pp2.className="pfv "+COL(rpnl);
  $("pf-pp").textContent=d.realized_pnl_pct!=null?PCT(d.realized_pnl_pct):"";
  $("pf-t").textContent=(d.closed_trades||0)+"件"; $("pf-wl").textContent=won+"勝 / "+lost+"敗";
  $("pf-w").textContent=wr.toFixed(1)+"%"; $("pf-d").textContent="-"+dd.toFixed(2)+"%"; $("pf-s").textContent=(d.scan_count||0)+"回";

  // 3時間軸
  const tfr=sig.tf_results||{};
  $("tf3").innerHTML=Object.entries(tfr).map(([tf,r])=>{
    const dir=r.direction||"neutral", cls=dir==="long"?"g":dir==="short"?"r":"";
    const arr=dir==="long"?"↑ LONG":dir==="short"?"↓ SHORT":"→ 中立";
    const det=r.details||{};
    return `<div class="tfbox">
      <div class="tfn">${tf}足</div>
      <div class="tfd ${cls}">${arr}</div>
      <div class="tfs">EMA:${det.ema||"?"} RSI:${det.rsi||"?"}</div>
    </div>`;
  }).join("")||`<div style="color:var(--muted);font-size:11px;padding:10px">取得中...</div>`;

  // リスク管理
  $("riskgrid").innerHTML=[
    ["最大ドローダウン上限","20%（デモは記録のみ・継続）","r"],
    ["1トレードのリスク","残高の1%まで","y"],
    ["連続損失クールダウン","5連敗 → 15分停止（デモは継続）",""],
    ["TP目標（利確）","ATR × 5.0 の位置（15分足ATR）","g"],
    ["SL（損切り）","ATR × 2.5 の位置（15分足ATR）","r"],
    ["RR比","2:1（2倍稼いで1倍のリスク）","g"],
    ["トレーリングストップ","+1.5%到達後 -0.8%で決済","c"],
    ["最大同時ポジション", "20件まで","b"],
    ["停止状態", d.is_halted?"⚠️ 24時間停止中":d.is_cooling_down?"⏳ 冷却中":"✅ 正常", d.is_halted?"r":d.is_cooling_down?"y":"g"],
  ].map(([k,v,c])=>`<div class="rrow"><span class="rk">${k}</span><span class="rv ${c}">${v}</span></div>`).join("");

  // 取引履歴
  const hist=d.trade_history||[];
  $("htbody").innerHTML=hist.length===0
    ?`<tr><td colspan="7"><div class="empty"><div class="ei">📋</div><div class="et">取引履歴なし</div></div></td></tr>`
    :hist.map(t=>{
      const rc={"tp":"rtp","sl":"rsl","trailing":"rtr","timeout":"rto"}[t.exit_reason]||"";
      const rl={"tp":"✅ 利確","sl":"🛑 損切り","trailing":"📈 追跡","timeout":"⏰ 時間切れ","force":"🚨 強制"}[t.exit_reason]||t.exit_reason;
      return `<tr>
        <td style="color:var(--muted2);font-size:11px">${FMT(t.exit_time)}</td>
        <td style="font-weight:800">${t.symbol}</td>
        <td><span class="pill ${t.side==="long"?"pl-g":"pl-r"}">${t.side==="long"?"ロング":"ショート"}</span></td>
        <td style="text-align:right">${P(t.entry_price)}</td>
        <td style="text-align:right">${P(t.exit_price)}</td>
        <td style="text-align:right">
          <div class="${COL(t.pnl)}" style="font-weight:900;font-size:13px">${SIGN(t.pnl_pct)}${t.pnl_pct.toFixed(2)}%</div>
          <div style="font-size:10px;color:var(--muted)">${SIGN(t.pnl)}$${Math.abs(t.pnl).toFixed(2)}</div>
        </td>
        <td class="${rc}">${rl}</td>
      </tr>`;
    }).join("");

  // システムログ
  if (!_logCleared) {
    const nl=d.logs||[];
    const news=nl.filter(l=>!_logCache.some(c=>c.ts===l.ts&&c.msg===l.msg));
    news.forEach(l=>{
      const g=l.msg?.includes("🟢")||l.msg?.includes("✅");
      const re=l.msg?.includes("🛑")||l.msg?.includes("🚨")||l.level==="error";
      const w=l.msg?.includes("⚠️")||l.level==="warn";
      const b=l.msg?.includes("📊")||l.msg?.includes("🚀")||l.msg?.includes("📡");
      const c2=l.msg?.includes("📈")||l.msg?.includes("⏰");
      const cls=re?"lc-e":g?"lc-g":w?"lc-w":b?"lc-b":c2?"lc-c":"lc-i";
      const el=document.createElement("div");
      el.className="logrow";
      el.innerHTML=`<span class="logts">${l.ts||""}</span><span class="logmsg ${cls}">${l.msg||""}</span>`;
      $("logbox").prepend(el);
    });
    _logCache=nl;
  }
}

// ─── チャート更新 ───
async function chartLoop() {
  await updateChart();
}

// ─── ① 日次リターン棒グラフ描画 ───
function renderDailyReturns(trade_history) {
  const container = document.getElementById("daily-chart");
  if (!container) return;
  // 日付ごとに損益を集計
  const byDay = {};
  for (const t of trade_history || []) {
    if (!t.exit_time) continue;
    const d = new Date(t.exit_time * 1000);
    const key = d.getFullYear() + "-" + String(d.getMonth()+1).padStart(2,"0") + "-" + String(d.getDate()).padStart(2,"0");
    byDay[key] = (byDay[key] || 0) + (t.pnl || 0);
  }
  const days = Object.keys(byDay).sort().slice(-30);
  if (days.length === 0) {
    container.innerHTML = '<div style="color:var(--muted2);margin:auto">📊 データなし（トレード完了後に表示）</div>';
    document.getElementById("daily-sum").textContent = "";
    return;
  }
  const values = days.map(d => byDay[d]);
  const maxAbs = Math.max(...values.map(Math.abs), 1);
  const totalSum = values.reduce((a,b)=>a+b, 0);
  const winDays = values.filter(v=>v>0).length;
  const lossDays = values.filter(v=>v<0).length;

  container.innerHTML = days.map((d, i) => {
    const v = values[i];
    const heightPct = Math.abs(v) / maxAbs * 95;
    const color = v >= 0 ? "#00e676" : "#f44336";
    const shortDay = d.slice(5); // MM-DD
    return `<div style="flex:1;min-width:18px;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:3px;cursor:default" title="${d}: $${v.toFixed(2)}">
      <div style="background:${color};width:100%;height:${heightPct}%;border-radius:2px 2px 0 0;min-height:2px"></div>
      <div style="font-size:9px;color:var(--muted2);transform:rotate(-45deg);white-space:nowrap;margin-top:4px">${shortDay}</div>
    </div>`;
  }).join("");

  document.getElementById("daily-sum").textContent =
    `30日合計 $${totalSum.toFixed(2)} | 勝ち${winDays}日 / 負け${lossDays}日`;
}

// ─── ⑥ 時間帯別勝率ヒートマップ ───
function renderHourlyHeatmap(trade_history) {
  const grid = document.getElementById("heatmap-grid");
  if (!grid) return;
  // 0-23時ごとに勝率計算
  const byHour = Array.from({length:24}, () => ({w:0, l:0}));
  for (const t of trade_history || []) {
    if (!t.exit_time) continue;
    const h = new Date(t.exit_time * 1000).getHours();
    if (t.won) byHour[h].w++;
    else byHour[h].l++;
  }
  grid.innerHTML = byHour.map((b, h) => {
    const total = b.w + b.l;
    let color, label;
    if (total === 0) { color = "var(--bg4)"; label = "—"; }
    else {
      const wr = b.w / total * 100;
      label = wr.toFixed(0) + "%";
      if (wr >= 60) color = "rgba(0,230,118," + Math.min(wr/100 + 0.2, 1) + ")";
      else if (wr >= 40) color = "rgba(255,202,40," + Math.min(wr/100 + 0.2, 1) + ")";
      else color = "rgba(244,67,54," + Math.min((100-wr)/100 + 0.2, 1) + ")";
    }
    return `<div style="background:${color};border-radius:3px;padding:8px 2px;text-align:center;font-size:10px;font-weight:700;color:#000;min-height:32px" title="${h}時: ${b.w}勝${b.l}敗">${h}<br>${label}</div>`;
  }).join("");
}

// ─── ⑦ 通知状態チェック ───
async function updateNotifStatus() {
  const el = document.getElementById("notif-status");
  if (!el) return;
  try {
    const r = await fetch("/api/notif-status");
    if (r.ok) {
      const d = await r.json();
      if (d.configured) {
        el.textContent = `✅ 設定済み (${(d.channels || []).join(", ")})`;
        el.style.color = "var(--green)";
      } else {
        el.textContent = "未設定（環境変数なし）";
        el.style.color = "var(--yellow)";
      }
    }
  } catch {}
}

// ─── 起動 ───
window.addEventListener("load",()=>{
  requestNotifyPermission();
  initChart();
  update();
  chartLoop();
  setInterval(update, 3000);
  setInterval(chartLoop, 5000);
  setInterval(() => {
    if (_tab === "pf") { updateEqChart(); updateRanking(); }
    if (_tab === "analysis") {
      // 分析タブ表示中: trade_historyを取り直して描画
      fetch("/api/state").then(r => r.json()).then(d => {
        renderDailyReturns(d.trade_history || []);
        renderHourlyHeatmap(d.trade_history || []);
      }).catch(()=>{});
      updateNotifStatus();
    }
  }, 10000);
  // 初回: 分析タブ選択時に描画
  document.getElementById("t-analysis").addEventListener("click", () => {
    fetch("/api/state").then(r => r.json()).then(d => {
      renderDailyReturns(d.trade_history || []);
      renderHourlyHeatmap(d.trade_history || []);
    }).catch(()=>{});
    updateNotifStatus();
  });
});
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


# ════════════════════════════════════════════════════
# Flask API エンドポイント
# ════════════════════════════════════════════════════

@app.route("/api/state")
def api_state():
    """ボットの現在状態を返すメインエンドポイント"""
    if _bot is None:
        return jsonify({"error": "ボットが起動していません"})
    state = _bot.get_account_status()
    # ── サーバー側で経過時間テキストを計算（ブラウザキャッシュ対策）──
    try:
        import time as _time
        # 永続化された検証開始時刻を優先（再起動しても維持される）
        started = getattr(_bot, "_validation_started_at", None)
        if not started:
            eh = getattr(_bot, "_equity_history", None) or []
            if eh:
                first = eh[0]
                started = first.get("time") or first.get("ts") or first.get("timestamp")
        if not started:
            started = getattr(_bot, "_started_at", _time.time())
        elapsed_sec = max(0, int(_time.time() - started))
        hours = elapsed_sec // 3600
        mins = (elapsed_sec % 3600) // 60
        secs = elapsed_sec % 60
        if hours >= 1:
            state["elapsed_text"] = f"{hours}時間{mins}分"
        elif mins >= 1:
            state["elapsed_text"] = f"{mins}分{secs}秒"
        else:
            state["elapsed_text"] = f"{secs}秒"
        state["elapsed_sec"] = elapsed_sec
    except Exception:
        pass
    return jsonify(_sanitize(state))


@app.route("/api/signal")
def api_signal():
    """最新のシグナル評価結果だけを返す"""
    if _bot is None:
        return jsonify({"error": "ボットが起動していません"})
    with _bot._lock:
        signal = _bot._last_signal_result.copy()
    return jsonify(_sanitize(signal))


@app.route("/api/logs")
def api_logs():
    """ログ一覧を返す"""
    if _bot is None:
        return jsonify([])
    limit = int(request.args.get("limit", 50))
    return jsonify(_sanitize(_bot.get_logs()[:limit]))


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    """
    バックテストを実行してサマリーを返す。

    POSTボディ（JSON）:
        {
            "start":   "2024-01-01",
            "end":     "2024-12-31",
            "balance": 10000,
            "tf":      "1h"
        }
    """
    data = request.get_json(silent=True) or {}
    config = Config(
        mode=Mode.BACKTEST,
        initial_balance=float(data.get("balance", 10_000)),
        backtest_start=data.get("start", "2024-01-01"),
        backtest_end=data.get("end",   "2024-12-31"),
    )
    bt     = Backtester(config)
    result = bt.run(
        symbol=config.symbol,
        start=data.get("start", "2024-01-01"),
        end=data.get("end",   "2024-12-31"),
        timeframe=data.get("tf", "1h"),
        initial_balance=float(data.get("balance", 10_000)),
    )
    return jsonify({
        "summary":      result.summary(),
        "trade_count":  len(result.trades),
        "equity_curve": result.equity_curve[-200:],  # 最後の200点のみ返す
    })


@app.route("/api/chart")
def api_chart():
    """チャート用OHLCVデータ＋ポジションTP/SL情報を返す"""
    if _bot is None:
        return jsonify({"candles": [], "positions": {}})
    tf  = request.args.get("tf",  "1m")
    sym = request.args.get("sym", None)
    return jsonify(_sanitize(_bot.get_chart_data(symbol=sym, timeframe=tf)))


@app.route("/api/equity")
def api_equity():
    """資産推移履歴を返す（エクイティカーブ用）"""
    if _bot is None:
        return jsonify({"history": []})
    return jsonify(_sanitize({"history": _bot.get_equity_history()}))


@app.route("/api/notif-status")
def api_notif_status():
    """通知設定（LINE/Discord Webhook）の有無を返す"""
    import os as _os
    channels = []
    if _os.environ.get("DISCORD_WEBHOOK_URL"): channels.append("Discord")
    if _os.environ.get("LINE_NOTIFY_TOKEN"): channels.append("LINE")
    return jsonify({"configured": len(channels) > 0, "channels": channels})


@app.route("/api/market_context")
def api_market_context():
    """Fear&Greed指数・ファンディングレート・市場全体センチメントを返す"""
    if _bot is None:
        return jsonify({"error": "ボットが起動していません"})
    return jsonify(_sanitize(_bot.mktctx.get_snapshot()))


@app.route("/api/symbol_stats")
def api_symbol_stats():
    """銘柄別の勝率・損益ランキングを返す"""
    if _bot is None:
        return jsonify({"stats": []})
    return jsonify(_sanitize({"stats": _bot.get_symbol_stats()}))


@app.route("/api/manual_scan", methods=["GET"])
def api_manual_scan():
    """手動スキャン: 上位30銘柄をリアルタイム並列スキャンしてスコア順に返す"""
    if _bot is None:
        return jsonify({"error": "ボットが起動していません"})
    top_n = int(request.args.get("top", 30))
    candidates = _bot.manual_scan(top_n=top_n)
    return jsonify(_sanitize({"candidates": candidates, "count": len(candidates)}))


@app.route("/api/manual_entry", methods=["POST"])
def api_manual_entry():
    """手動エントリー: 指定銘柄・方向でポジションを開く"""
    if _bot is None:
        return jsonify({"ok": False, "reason": "ボットが起動していません"})
    data   = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "")
    side   = data.get("side", "long").lower()
    if not symbol:
        return jsonify({"ok": False, "reason": "symbolが必要です"})
    if side not in ("long", "short"):
        return jsonify({"ok": False, "reason": "side は long または short を指定してください"})
    result = _bot.manual_entry(symbol, side)
    return jsonify(_sanitize(result))


@app.route("/api/control", methods=["POST"])
def api_control():
    """ボットの停止・再開を制御する"""
    if _bot is None:
        return jsonify({"error": "ボットが起動していません"})
    data   = request.get_json(silent=True) or {}
    action = data.get("action", "")

    if action == "stop":
        _bot.stop()
        return jsonify({"ok": True, "message": "ボットを停止しました"})
    elif action == "start":
        _bot.start()
        return jsonify({"ok": True, "message": "ボットを起動しました"})
    elif action == "reset_cooldown":
        _bot.reset_cooldown()
        return jsonify({"ok": True, "message": "クールダウンを解除しました"})

    return jsonify({"error": f"不明なアクション: {action}"})


# ════════════════════════════════════════════════════
# 起動処理
# ════════════════════════════════════════════════════

def run_simulation(config: Config, port: int):
    """シミュレーションモードで起動"""
    global _bot
    _bot = TradingBot(config)
    _bot.start()

    logger.info(f"🌐 Flask サーバー起動: http://localhost:{port}")
    logger.info(f"   /api/state    : ボット状態確認")
    logger.info(f"   /api/signal   : 最新シグナル確認")
    logger.info(f"   /api/backtest : バックテスト実行 (POST)")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)


def run_backtest(config: Config, start: str, end: str, balance: float):
    """コマンドラインでバックテストを実行して結果を表示"""
    logger.info(f"バックテストモード: {start} 〜 {end}  初期資金: ${balance:,.0f}")
    bt     = Backtester(config)
    result = bt.run(config.symbol, start, end, config.backtest_tf, balance)
    result.print_summary()

    # グラフを保存
    chart_path = os.path.join(os.path.dirname(__file__), "backtest_result.png")
    result.plot_equity_curve(save_path=chart_path)


# ════════════════════════════════════════════════════
# メインエントリポイント
# ════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="🤖 プロ仕様 自動売買ボット v1.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python main.py                              # シミュレーション起動（デフォルト）
  python main.py --balance 50000             # 初期資金5万USDでシミュレーション
  python main.py --mode backtest             # バックテスト実行
  python main.py --mode backtest --start 2023-01-01 --end 2023-12-31
        """
    )
    parser.add_argument("--mode",    default="simulation",
                        choices=["simulation", "backtest"],
                        help="動作モード（simulation/backtest）")
    parser.add_argument("--balance", type=float, default=10_000.0,
                        help="初期資金（USD）")
    parser.add_argument("--symbol",  default="BTC/USDT",
                        help="取引銘柄")
    parser.add_argument("--start",   default="2024-01-01",
                        help="バックテスト開始日")
    parser.add_argument("--end",     default="2024-12-31",
                        help="バックテスト終了日")
    parser.add_argument("--tf",      default="1h",
                        help="バックテスト時間軸（1m/5m/1h/1d）")
    parser.add_argument("--port",    type=int, default=8082,
                        help="Flaskサーバーのポート番号")
    parser.add_argument("--log",     default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="ログレベル")

    args = parser.parse_args()

    config = Config(
        mode=args.mode,
        symbol=args.symbol,
        initial_balance=args.balance,
        backtest_start=args.start,
        backtest_end=args.end,
        backtest_tf=args.tf,
        log_level=args.log,
    )

    print(f"""
╔══════════════════════════════════════════════════════╗
║  🤖 プロ仕様 自動売買ボット v1.0                    ║
║  ────────────────────────────────────────────────── ║
║  モード    : {args.mode:<40s}║
║  銘柄      : {args.symbol:<40s}║
║  初期資金  : ${args.balance:>10,.0f}{"USD":<29s}║
║  レバレッジ: {config.min_leverage:.0f}〜{config.max_leverage:.0f}倍（動的）{"":30s}║
║  最大DD    : {config.max_drawdown*100:.0f}%{"":41s}║
╚══════════════════════════════════════════════════════╝
    """)

    if args.mode == "backtest":
        run_backtest(config, args.start, args.end, args.balance)
    else:
        run_simulation(config, args.port)


if __name__ == "__main__":
    main()
