"""
DL MAX 2x Live Bot — シンプル・堅牢・美しいUI
=========================================
戦略: Dynamic Leverage MAX 2x (ADX連動, 5年検証で+638%, $10K→$73,828)
ロジック:
  - BTC/USDT 日足のみ
  - BTC が EMA200 上 かつ EMA50 > EMA200 のみ取引
  - ADX 20-30 → 1倍、ADX 30+ → 2倍
  - 条件外 → 現金保持

モード: SIMULATION（仮想$10,000・実際のお金は動かない）
データ: Binance実データのみ（合成禁止・健全性検証済み）
"""
from __future__ import annotations
import sys, json, time, threading
from pathlib import Path
from datetime import datetime
sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")

import pandas as pd
import numpy as np
from flask import Flask, jsonify, render_template_string

from config import Config
from data_fetcher import DataFetcher
from _racsm_backtest import validate_ohlcv_data, assert_binance_source

# ═══ 設定 ═══
STATE_FILE     = Path("/Users/sanosano/projects/kimochi-max/dl_max2x_state.json")
INITIAL_CAPITAL = 10_000.0
# 監視銘柄（Binance実在・流動性上位50銘柄・検証済み）
SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
    "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT", "DOGE/USDT",
    "LTC/USDT", "BCH/USDT", "ATOM/USDT", "UNI/USDT", "NEAR/USDT",
    "FIL/USDT", "TRX/USDT", "ETC/USDT", "APT/USDT", "ARB/USDT",
    "OP/USDT", "ALGO/USDT", "XLM/USDT", "VET/USDT", "HBAR/USDT",
    "EGLD/USDT", "FTM/USDT", "AAVE/USDT", "SAND/USDT", "MANA/USDT",
    "CRV/USDT", "COMP/USDT", "SUSHI/USDT", "YFI/USDT", "SNX/USDT",
    "MKR/USDT", "IMX/USDT", "INJ/USDT", "GRT/USDT", "ICP/USDT",
    "KAVA/USDT", "ZEC/USDT", "DASH/USDT", "ZIL/USDT", "ONE/USDT",
    "BAT/USDT", "ENJ/USDT", "QNT/USDT", "CHZ/USDT", "AXS/USDT",
]
SYMBOL         = "BTC/USDT"      # レジーム判定用
MAX_POSITIONS  = 50              # 同時保有の最大件数
FEE            = 0.0006
SLIP           = 0.0003
FUNDING_PH     = 0.0000125
PORT           = 8083

# ADX → レバレッジ（DL MAX 2x 検証済み最強設定）
LEVELS = [(20, 1.0), (30, 2.0)]

# チェック間隔: 5分ごとに最新データでシグナル確認
CHECK_INTERVAL_SEC = 300

# ═══ 状態管理 ═══
def initial_state() -> dict:
    return {
        "bot_name": "DL MAX 2x (マルチ銘柄)",
        "started_at": datetime.now().isoformat(),
        "initial_capital": INITIAL_CAPITAL,
        "cash": INITIAL_CAPITAL,
        "positions": {},  # sym → {"qty", "entry_price", "leverage", "entry_ts"}
        "position": None,  # 互換性のため残す（常にNone or 1件目）
        "equity_history": [],
        "trades": [],
        "symbol_data": {},  # sym → {"price", "ema50", "ema200", "adx", "signal_lev"}
        "last_check_ts": None,
        "last_signal": None,
        "scan_count": 0,
        "btc_price": None,
        "btc_ema50": None,
        "btc_ema200": None,
        "btc_adx": None,
        "bot_status": "初期化中",
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return initial_state()


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str))


# ═══ 指標計算 ═══
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # ATR
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    # EMA
    df["ema50"]  = df["close"].ewm(span=50,  adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    # ADX
    up = df["high"] - df["high"].shift()
    dn = df["low"].shift() - df["low"]
    plus_dm  = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    plus_di  = 100 * pd.Series(plus_dm,  index=df.index).rolling(14).mean() / df["atr"]
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(14).mean() / df["atr"]
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
    df["adx"] = dx.rolling(14).mean()
    return df


def target_leverage(row) -> float:
    price = row["close"]
    if not (price > row["ema200"] and row["ema50"] > row["ema200"]):
        return 0.0
    adx = row["adx"]
    if pd.isna(adx):
        return 0.0
    lev = 0.0
    for thr, l in LEVELS:
        if adx >= thr:
            lev = l
    return lev


# ═══ 取引ロジック（マルチ銘柄） ═══
def _fetch_symbol_indicator(fetcher: DataFetcher, sym: str) -> Optional[pd.Series]:
    """個別銘柄の最新日足 + 指標を取得（失敗時はNone）"""
    try:
        from datetime import timedelta
        today = datetime.now()
        start = (today - timedelta(days=320)).strftime("%Y-%m-%d")
        end   = today.strftime("%Y-%m-%d")
        df = fetcher.fetch_historical_ohlcv(sym, "1d", start, end)
        if df.empty:
            return None
        validate_ohlcv_data(df, sym, "1d")
        df = compute_indicators(df)
        return df.iloc[-1]
    except Exception as e:
        print(f"  ⚠️ {sym}: {e}")
        return None


def update_bot(state: dict, fetcher: DataFetcher):
    """
    全監視銘柄をスキャンし、DL MAX 2x ロジックでポジション調整。
    - BTCがレジーム外 → 全ポジション決済
    - 各銘柄独立にADX判定、閾値超えたらエントリー、未達なら決済
    """
    try:
        ts = datetime.now().isoformat()
        state["scan_count"] = state.get("scan_count", 0) + 1
        state["last_check_ts"] = ts

        # 1. BTC のレジーム判定（親フィルタ）
        btc_row = _fetch_symbol_indicator(fetcher, "BTC/USDT")
        if btc_row is None:
            state["bot_status"] = "BTCデータ取得失敗"
            save_state(state)
            return False

        state["btc_price"]  = round(float(btc_row["close"]), 2)
        state["btc_ema50"]  = round(float(btc_row["ema50"]), 2)
        state["btc_ema200"] = round(float(btc_row["ema200"]), 2)
        state["btc_adx"]    = round(float(btc_row["adx"]), 2) if not pd.isna(btc_row["adx"]) else None

        btc_bull = (float(btc_row["close"]) > float(btc_row["ema200"])
                    and float(btc_row["ema50"]) > float(btc_row["ema200"]))

        # 2. 全銘柄の最新指標を取得（並行でもよいが逐次で安定させる）
        sym_data = {}
        for sym in SYMBOLS:
            row = _fetch_symbol_indicator(fetcher, sym)
            if row is None:
                continue
            price = float(row["close"])
            adx   = float(row["adx"]) if not pd.isna(row["adx"]) else 0.0
            lev   = target_leverage(row) if btc_bull else 0.0
            sym_data[sym] = {
                "price":    round(price, 2),
                "ema50":    round(float(row["ema50"]), 2),
                "ema200":   round(float(row["ema200"]), 2),
                "adx":      round(adx, 2),
                "signal_lev": lev,
            }
        state["symbol_data"] = sym_data

        # 3. ポジション管理
        positions = state.get("positions", {})
        opened = closed = 0

        # BTCが弱気になったら全ポジション決済
        if not btc_bull and positions:
            for sym in list(positions.keys()):
                if sym not in sym_data:
                    continue
                _close_one(state, sym, sym_data[sym]["price"], ts, reason="btc_bear")
                closed += 1

        # 既存ポジションの更新（ADX落ちたら決済）
        for sym in list(positions.keys()):
            if sym not in sym_data:
                continue
            d = sym_data[sym]
            # ADX<20 または 個別銘柄がEMA200割れ → 決済
            if d["signal_lev"] == 0:
                _close_one(state, sym, d["price"], ts, reason="signal_off")
                closed += 1

        # 新規エントリー（空きスロット埋める）
        positions = state["positions"]
        open_slots = MAX_POSITIONS - len(positions)
        if btc_bull and open_slots > 0:
            # 候補: まだ保有してない銘柄で signal_lev > 0
            candidates = sorted(
                [(sym, d) for sym, d in sym_data.items()
                 if sym not in positions and d["signal_lev"] > 0],
                key=lambda x: x[1]["adx"],  # ADX高い順
                reverse=True,
            )[:open_slots]

            if candidates:
                # 空きスロット数で現金を均等分割
                available_cash = state["cash"]
                per_slot = available_cash / max(open_slots, 1)

                for sym, d in candidates:
                    if per_slot < 10:  # 少額すぎる場合スキップ
                        break
                    _open_one(state, sym, d["price"], d["signal_lev"], per_slot, ts)
                    opened += 1

        # 4. Equity 計算（全ポジション mark-to-market）
        unreal_total = 0.0
        for sym, p in state["positions"].items():
            if sym in sym_data:
                unreal_total += p["qty"] * (sym_data[sym]["price"] - p["entry_price"]) * p["leverage"]
        equity = state["cash"] + unreal_total

        state["equity_history"].append({"ts": ts, "equity": round(equity, 2)})
        state["equity_history"] = state["equity_history"][-500:]

        # 互換性用の position フィールド（1件目）
        if state["positions"]:
            first_sym = next(iter(state["positions"]))
            state["position"] = {
                **state["positions"][first_sym],
                "symbol": first_sym,
            }
        else:
            state["position"] = None

        # シグナル説明
        n_pos = len(state["positions"])
        if not btc_bull:
            reason = "BTC < EMA200" if float(btc_row["close"]) < float(btc_row["ema200"]) else "EMA50 < EMA200"
            state["last_signal"] = f"⚠️ BTCレジーム外 ({reason}) → 全ポジション決済"
        else:
            active = sum(1 for d in sym_data.values() if d["signal_lev"] > 0)
            state["last_signal"] = (
                f"✅ BTC強気相場 / アクティブ銘柄 {active}/{len(sym_data)} / "
                f"保有 {n_pos}/{MAX_POSITIONS}件"
                + (f" / 今回 OPEN {opened} / CLOSE {closed}" if opened or closed else "")
            )

        state["bot_status"] = f"稼働中 ({n_pos}件保有)"
        save_state(state)
        return True
    except Exception as e:
        state["bot_status"] = f"エラー: {e}"
        import traceback
        traceback.print_exc()
        save_state(state)
        return False


def _open_one(state: dict, sym: str, price: float, lev: float, alloc_usd: float, ts: str):
    """指定銘柄に alloc_usd の元本でレバ lev 倍のポジションを開く"""
    entry_price = price * (1 + SLIP)
    qty = alloc_usd / entry_price
    notional = alloc_usd * lev
    fee = notional * FEE
    # 現金から 元本 + 手数料 を差し引く
    state["cash"] -= (alloc_usd + fee)
    state["positions"][sym] = {
        "qty": qty,
        "entry_price": entry_price,
        "leverage": lev,
        "entry_ts": ts,
        "alloc_usd": alloc_usd,
    }
    state["trades"].append({
        "action": "OPEN",
        "symbol": sym,
        "entry_price": round(entry_price, 2),
        "qty": round(qty, 6),
        "leverage": lev,
        "alloc_usd": round(alloc_usd, 2),
        "ts": ts,
    })


def _close_one(state: dict, sym: str, price_now: float, ts: str, reason: str = "signal"):
    """指定銘柄のポジションを決済"""
    if sym not in state["positions"]:
        return
    p = state["positions"][sym]
    exit_price = price_now * (1 - SLIP)
    pnl = p["qty"] * (exit_price - p["entry_price"]) * p["leverage"]
    notional = p["qty"] * exit_price * p["leverage"]
    pnl -= notional * FEE
    entry_ts_dt = datetime.fromisoformat(p["entry_ts"])
    hold_h = (datetime.now() - entry_ts_dt).total_seconds() / 3600
    pnl -= notional * FUNDING_PH * hold_h
    state["cash"] += p.get("alloc_usd", 0) + pnl  # 元本 + PnL を現金に戻す
    state["trades"].append({
        "action": "CLOSE",
        "symbol": sym,
        "entry_price": p["entry_price"],
        "exit_price": round(exit_price, 2),
        "qty": p["qty"],
        "leverage": p["leverage"],
        "pnl": round(pnl, 2),
        "hold_hours": round(hold_h, 2),
        "ts": ts,
        "reason": reason,
    })
    del state["positions"][sym]


# ═══ Flask UI ═══
app = Flask(__name__)

HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<title>DL MAX 2x — 気持ちマックス</title>
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
html,body{background:var(--bg);color:var(--text);font-family:-apple-system,"Helvetica Neue",sans-serif;font-size:13px;min-height:100%}

/* ヘッダー */
.hdr{background:var(--bg2);border-bottom:1px solid var(--border);padding:0 14px;min-height:48px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.hdr-logo{font-size:17px;font-weight:900;color:#fff;letter-spacing:-.5px}
.hdr-logo em{color:var(--yellow);font-style:normal}
.live-dot{width:8px;height:8px;border-radius:50%;background:var(--green);flex-shrink:0;box-shadow:0 0 8px var(--green);animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.hbadge{padding:4px 12px;border-radius:14px;font-size:11px;font-weight:700}
.hb-sim{background:#0a2c17;color:var(--green);border:1px solid #174d28}
.hb-lev{background:#2c1f0a;color:var(--yellow);border:1px solid #4d3700}
.hbtn{padding:8px 16px;border-radius:10px;font-size:12px;font-weight:700;border:none;cursor:pointer;background:var(--bg4);color:var(--text);border:1px solid var(--border2)}
.hdr-right{margin-left:auto;font-size:11px;color:var(--muted2)}

/* 12+1 ボックスグリッド（本家と同じ） */
.statsbar{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:12px;background:var(--bg)}
.sb{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:12px 14px;min-height:76px;position:relative}
.sb-val{font-size:22px;font-weight:900;letter-spacing:-.6px;line-height:1.05;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sb-lbl{font-size:11px;color:var(--muted2);font-weight:700;margin-top:4px}
.sb-sub{font-size:11px;color:var(--muted);margin-top:2px}
.sb.tax{background:linear-gradient(135deg,#2c0a0a,#3d0e0e);border-color:#5d1a1a}
.sb.tax .sb-lbl{color:#ff9090}
.sb.after{background:linear-gradient(135deg,#0a2c17,#0e3d22);border-color:#1a5d33}
.sb.after .sb-lbl{color:#90ff90}
.g{color:var(--green)}.r{color:var(--red)}.y{color:var(--yellow)}.b{color:var(--blue)}.c{color:var(--cyan)}.o{color:var(--orange)}
@media(max-width:768px){
  .statsbar{grid-template-columns:repeat(2,1fr);gap:6px;padding:10px}
  .sb{padding:10px;min-height:70px}
  .sb-val{font-size:19px}
}

/* 手数料累計単独ロー */
.fees-row{padding:0 12px 12px;background:var(--bg)}
.fees-box{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.fees-val{font-size:20px;font-weight:900;color:var(--orange)}
.fees-lbl{font-size:11px;color:var(--muted2);font-weight:700;margin-top:4px}
.fees-sub{font-size:11px;color:var(--muted);margin-top:2px}

/* DDバー */
.ddbar{background:var(--bg2);border-top:1px solid var(--border);border-bottom:1px solid var(--border);padding:10px 14px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.dd-lbl{font-size:10px;color:var(--muted2);font-weight:700}
.dd-num{font-size:20px;font-weight:900;min-width:80px}
.dd-track{flex:1;min-width:120px;max-width:260px;height:6px;background:var(--border2);border-radius:3px;overflow:hidden}
.dd-fill{height:100%;border-radius:3px;transition:width .5s,background .5s}
.dd-pill{font-size:11px;font-weight:800;padding:3px 12px;border-radius:10px}
.ddp-ok{background:var(--green-bg);color:var(--green)}
.ddp-warn{background:var(--yellow-bg);color:var(--yellow)}
.ddp-danger{background:var(--red-bg);color:var(--red)}

/* 市況カードロー */
.mkt-row{display:flex;gap:8px;padding:10px 12px;background:var(--bg);overflow-x:auto;-webkit-overflow-scrolling:touch}
.mkt-card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:8px 12px;min-width:95px;flex-shrink:0;text-align:center}
.mkt-lbl{font-size:10px;color:var(--muted2);font-weight:700}
.mkt-val{font-size:18px;font-weight:900;margin-top:2px}
.mkt-sub{font-size:10px;color:var(--muted);margin-top:2px}
.mkt-btc{margin-left:auto;background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:8px 14px;white-space:nowrap;display:flex;align-items:center;font-size:13px;font-weight:700;color:var(--text)}

/* 本日損失バー */
.dlrow{display:flex;align-items:center;gap:10px;padding:8px 14px;background:var(--bg2);border-bottom:1px solid var(--border);font-size:12px}
.dl-lbl{color:var(--muted2);font-weight:700}
.dl-track{flex:1;max-width:180px;height:6px;background:var(--border2);border-radius:3px;overflow:hidden}
.dl-fill{height:100%;background:var(--green);border-radius:3px}
.dl-pct{font-size:12px;font-weight:800;color:var(--green)}

/* タブ */
.tabs{display:flex;padding:0 10px;background:var(--bg2);border-bottom:2px solid var(--border);gap:4px;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tb{padding:12px 16px;font-size:13px;font-weight:700;color:var(--muted2);cursor:pointer;white-space:nowrap;border-bottom:3px solid transparent;margin-bottom:-2px}
.tb.on{color:#fff;border-bottom-color:var(--blue)}

/* セクション */
.sec{padding:12px 14px}
.sec-hdr{display:flex;align-items:center;gap:8px;margin-bottom:10px;font-size:12px;font-weight:700;color:var(--text)}
.sec-icon{font-size:14px}

/* ポジション */
.pos-empty{color:var(--muted2);font-size:13px;padding:8px}
.pos-card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:8px}

/* シグナルテーブル */
.sig-wrap{background:var(--bg2);border:1px solid var(--border);border-radius:12px;overflow:hidden;margin:0 14px 14px}
.sig-hdr-row{display:flex;align-items:center;padding:12px 14px;gap:8px;font-size:12px;font-weight:700}
.sig-filter{display:flex;gap:6px;margin-left:auto;font-size:11px}
.sig-btn{padding:4px 10px;border-radius:8px;background:var(--bg4);color:var(--muted2);border:1px solid var(--border2);cursor:pointer;font-weight:700}
.sig-btn.on{background:var(--blue-bg);color:var(--blue);border-color:#4fc3f740}
.sig-btn.long-b{color:var(--green)}
.sig-btn.short-b{color:var(--red)}
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.ttable{width:100%;border-collapse:collapse;min-width:540px}
.ttable th{padding:10px 12px;font-size:10px;font-weight:700;color:var(--muted2);text-transform:uppercase;letter-spacing:.05em;background:var(--bg3);border-bottom:1px solid var(--border);text-align:left}
.ttable td{padding:12px;border-bottom:1px solid var(--border);vertical-align:middle;font-size:12px}
.ttable tr.lrow td:first-child{border-left:3px solid var(--green)}
.ttable tr.srow td:first-child{border-left:3px solid var(--red)}
.pill{display:inline-block;padding:3px 10px;border-radius:11px;font-size:10px;font-weight:800}
.pl-g{background:var(--green-bg);color:var(--green);border:1px solid #00e67630}
.pl-r{background:var(--red-bg);color:var(--red);border:1px solid #f4433630}
.pl-y{background:var(--yellow-bg);color:var(--yellow);border:1px solid #ffca2830}
.pl-gray{background:var(--bg4);color:var(--muted2);border:1px solid var(--border2)}

.scbar{display:flex;align-items:center;gap:8px}
.sctrack{width:60px;height:4px;background:var(--border2);border-radius:3px;overflow:hidden}
.scfill{height:100%;border-radius:3px}
.scnum{font-weight:800}

.empty{text-align:center;padding:40px 16px}
.empty .ei{font-size:36px;margin-bottom:12px;opacity:.5}
.empty .et{font-size:13px;font-weight:700;color:var(--muted2)}
.empty .es{font-size:11px;color:var(--muted);margin-top:4px}

/* タブパネル切替 */
.tpane{display:none}
.tpane.on{display:block}

/* ポートフォリオグリッド */
.pf-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;padding:14px}
.pfc{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:16px}
.pfl{font-size:11px;color:var(--muted2);font-weight:700}
.pfv{font-size:26px;font-weight:900;margin-top:8px;line-height:1.1}
.pfs{font-size:11px;color:var(--muted);margin-top:4px}

/* 手動エントリーボタン（デカめ・押しやすい） */
#scan-btn,#close-btn{
  display:block;width:100%;padding:20px;font-size:17px;font-weight:900;
  border-radius:12px;border:none;cursor:pointer;transition:.1s;
  background:linear-gradient(135deg,#00c853,#00e676);color:#001a0d;
  box-shadow:0 4px 14px rgba(0,230,118,0.3);
}
#scan-btn:hover,#close-btn:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(0,230,118,0.4)}
#scan-btn:active,#close-btn:active{transform:scale(0.97)}
#close-btn{background:linear-gradient(135deg,#d32f2f,#f44336);color:#fff;box-shadow:0 4px 14px rgba(244,67,54,0.3);margin-top:10px}

.ind-row-2{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px;color:var(--muted2)}
.ind-row-2:last-child{border-bottom:none}
.ind-row-2 strong{color:var(--text);font-weight:800}
</style>
</head><body>

<!-- ヘッダー（売買Pro + SIM + レバ + 一時停止） -->
<div class="hdr">
  <div class="live-dot"></div>
  <div class="hdr-logo">売買<em>Pro</em></div>
  <span class="hbadge hb-sim">SIM</span>
  <span class="hbadge hb-lev">2.0倍</span>
  <button class="hbtn">⏸ 一時停止</button>
</div>

<!-- 12ボックス + 1行（本家のstatsbarと完全一致） -->
<div class="statsbar">
  <div class="sb"><div class="sb-val r" id="s-eq">$10,000</div><div class="sb-lbl">総資産</div><div class="sb-sub" id="s-eq2">残高 $10,000.00</div></div>
  <div class="sb"><div class="sb-val" id="s-up">+$0.00</div><div class="sb-lbl">含み損益</div><div class="sb-sub" id="s-up2">+0.00%</div></div>
  <div class="sb"><div class="sb-val r" id="s-rp">$0.00</div><div class="sb-lbl">確定損益</div><div class="sb-sub" id="s-rp2">0.00%</div></div>
  <div class="sb tax"><div class="sb-val r" id="s-tax">$0.00</div><div class="sb-lbl">💰 推定税金(日本)</div><div class="sb-sub">雑所得 自動(累進)</div></div>
  <div class="sb after"><div class="sb-val g" id="s-after">$0.00</div><div class="sb-lbl">💵 税引き後利益</div><div class="sb-sub">損失時は税金なし</div></div>
  <div class="sb"><div class="sb-val r" id="s-td">$0.00</div><div class="sb-lbl">本日損益</div><div class="sb-sub" id="s-td2">0.00%</div></div>
  <div class="sb"><div class="sb-val y" id="s-wr">—</div><div class="sb-lbl">勝率</div><div class="sb-sub" id="s-wrs">0勝0敗</div></div>
  <div class="sb"><div class="sb-val o" id="s-lv">2.0倍</div><div class="sb-lbl">レバレッジ</div><div class="sb-sub">✅ 正常稼働</div></div>
  <div class="sb"><div class="sb-val b" id="s-ps">0/1件</div><div class="sb-lbl">保有中</div><div class="sb-sub" id="s-ps2">1/1銘柄</div></div>
  <div class="sb"><div class="sb-val c" id="s-elapsed">起動中</div><div class="sb-lbl">検証経過</div><div class="sb-sub" id="s-scancount">スキャン 0回</div></div>
  <div class="sb"><div class="sb-val y" id="s-streak">0</div><div class="sb-lbl">連敗</div><div class="sb-sub" id="s-streak-max">最大連敗 0</div></div>
  <div class="sb"><div class="sb-val o" id="s-pf">—</div><div class="sb-lbl">PF (利益係数)</div><div class="sb-sub" id="s-sharpe">Sharpe —</div></div>
</div>

<!-- 手数料累計（本家同様に独立行） -->
<div class="fees-row">
  <div class="fees-box">
    <div class="fees-val" id="s-fees">$0.00</div>
    <div class="fees-lbl">手数料累計</div>
    <div class="fees-sub" id="s-fees-ratio">概$0.00/回・損益比 0.0%</div>
  </div>
</div>

<!-- DDバー -->
<div class="ddbar">
  <span class="dd-lbl">DD</span>
  <span class="dd-num g" id="ddnum">-0.00%</span>
  <div class="dd-track"><div class="dd-fill" id="ddfill" style="width:0%;background:var(--green)"></div></div>
  <span class="dd-pill ddp-ok" id="ddpill">正常</span>
</div>

<!-- 市況カード -->
<div class="mkt-row">
  <div class="mkt-card">
    <div class="mkt-lbl">恐怖&強欲</div>
    <div class="mkt-val" id="fg-val">😨 —</div>
    <div class="mkt-sub" id="fg-lbl">読込中</div>
  </div>
  <div class="mkt-card">
    <div class="mkt-lbl">資金調達率</div>
    <div class="mkt-val y" id="fr-val">—</div>
    <div class="mkt-sub">上位10銘柄平均</div>
  </div>
  <div class="mkt-card">
    <div class="mkt-lbl">強気比率</div>
    <div class="mkt-val g" id="mb-val">—</div>
    <div class="mkt-sub">市場全体の強気%</div>
  </div>
  <div class="mkt-btc" id="btcprice">BTC $—</div>
</div>

<!-- 本日損失バー -->
<div class="dlrow">
  <span class="dl-lbl">本日損失</span>
  <div class="dl-track"><div class="dl-fill" id="dl-fill" style="width:0%"></div></div>
  <span class="dl-pct" id="dl-pct">0.0%</span>
  <span style="font-size:11px;color:var(--muted2)">/5%</span>
</div>

<!-- タブ（ボタンでクリック可能） -->
<div class="tabs">
  <div class="tb on" onclick="sw(event, 'live')">⚡ ライブ取引</div>
  <div class="tb" onclick="sw(event, 'pf')">📊 ポートフォリオ</div>
  <div class="tb" onclick="sw(event, 'scan')">🔍 手動エントリー</div>
  <div class="tb" onclick="sw(event, 'hist')">📋 取引履歴</div>
</div>

<!-- タブパネル: ライブ取引 -->
<div class="tpane on" id="p-live">
  <div class="sec">
    <div class="sec-hdr"><span class="sec-icon">🎯</span>保有ポジション</div>
    <div id="pos-area"><div class="pos-empty">保有ポジションなし — エントリー待機中</div></div>
  </div>
  <div class="sig-wrap">
    <div class="sig-hdr-row">
      <span>🔍 シグナル一覧</span>
      <span style="margin-left:auto;font-size:11px;color:var(--muted)">BTC特化戦略</span>
    </div>
    <div class="table-wrap">
      <table class="ttable">
        <thead>
          <tr>
            <th style="width:100px">状態</th>
            <th>銘柄</th>
            <th style="width:140px">ADX強度</th>
            <th style="width:120px">現在価格</th>
            <th style="width:90px">シグナル</th>
          </tr>
        </thead>
        <tbody id="sig-body"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- タブパネル: ポートフォリオ -->
<div class="tpane" id="p-pf" style="display:none">
  <div class="pf-grid">
    <div class="pfc"><div class="pfl">残高</div><div class="pfv" id="pf-bal">—</div><div class="pfs">初期 $10,000 から</div></div>
    <div class="pfc"><div class="pfl">確定損益</div><div class="pfv g" id="pf-rpnl">—</div><div class="pfs" id="pf-rpct">—</div></div>
    <div class="pfc"><div class="pfl">取引数</div><div class="pfv b" id="pf-ntrades">0</div><div class="pfs" id="pf-wl">—</div></div>
    <div class="pfc"><div class="pfl">勝率</div><div class="pfv y" id="pf-winrate">—</div><div class="pfs">勝ちトレードの割合</div></div>
    <div class="pfc"><div class="pfl">最大DD</div><div class="pfv r" id="pf-dd">0.00%</div><div class="pfs">ピーク時からの下落</div></div>
    <div class="pfc"><div class="pfl">スキャン回数</div><div class="pfv c" id="pf-scans">0</div><div class="pfs">15分毎に相場確認</div></div>
  </div>
</div>

<!-- タブパネル: 手動エントリー -->
<div class="tpane" id="p-scan" style="display:none">
  <div style="padding:20px 14px">
    <div style="background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:14px">
      <div style="font-size:14px;font-weight:700;margin-bottom:10px;color:var(--text)">✋ 手動エントリー / 決済</div>
      <div style="font-size:12px;color:var(--muted2);margin-bottom:14px;line-height:1.6">
        DL MAX 2x 戦略は BTC が EMA200 を超えるまで自動エントリーしません。<br>
        このボタンで <strong style="color:var(--yellow)">シグナルを無視して手動で取引</strong> できます（デモ用）。
      </div>
      <button id="scan-btn" onclick="manualEntry()">🚀 空きスロット全部にエントリー（2倍ロング）</button>
      <button id="close-btn" onclick="manualClose()" style="display:none">💥 全ポジション一括決済</button>
      <div id="scan-msg" style="margin-top:14px;font-size:12px;color:var(--muted2)"></div>
    </div>
    <div style="background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:20px">
      <div style="font-size:14px;font-weight:700;margin-bottom:10px;color:var(--text)">📊 現在のBTC状況</div>
      <div class="ind-row-2"><span>BTC 価格</span><strong id="sc-price">—</strong></div>
      <div class="ind-row-2"><span>EMA 50</span><strong id="sc-ema50">—</strong></div>
      <div class="ind-row-2"><span>EMA 200</span><strong id="sc-ema200">—</strong></div>
      <div class="ind-row-2"><span>ADX</span><strong id="sc-adx">—</strong></div>
      <div class="ind-row-2" style="margin-top:8px;padding-top:10px;border-top:1px solid var(--border)">
        <span>自動戦略の判断</span><strong id="sc-signal" style="color:var(--cyan)">—</strong>
      </div>
    </div>
  </div>
</div>

<!-- タブパネル: 取引履歴 -->
<div class="tpane" id="p-hist" style="display:none">
  <div class="table-wrap" style="padding:12px">
    <table class="ttable" style="min-width:600px">
      <thead>
        <tr>
          <th>日時</th>
          <th style="width:80px">アクション</th>
          <th>価格</th>
          <th>数量</th>
          <th>レバ</th>
          <th style="text-align:right">損益</th>
        </tr>
      </thead>
      <tbody id="hist-body">
        <tr><td colspan="6"><div class="empty"><div class="ei">📭</div><div class="et">取引履歴なし</div><div class="es">エントリーするとここに表示されます</div></div></td></tr>
      </tbody>
    </table>
  </div>
</div>

<script>
const fmt = n => n === null || n === undefined ? '—' : '$' + Number(n).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
const fmtBtc = n => n === null || n === undefined ? '—' : '$' + Number(n).toLocaleString(undefined, {maximumFractionDigits: 0});
const fmtInt = n => n === null || n === undefined ? '—' : '$' + Math.round(Number(n)).toLocaleString();

// タブ切替
function sw(e, id) {
  document.querySelectorAll('.tb').forEach(t => t.classList.remove('on'));
  document.querySelectorAll('.tpane').forEach(p => { p.style.display = 'none'; p.classList.remove('on'); });
  if (e && e.currentTarget) e.currentTarget.classList.add('on');
  const pane = document.getElementById('p-' + id);
  if (pane) { pane.style.display = 'block'; pane.classList.add('on'); }
}

// 手動エントリー
async function manualEntry() {
  const btn = document.getElementById('scan-btn');
  const msg = document.getElementById('scan-msg');
  btn.disabled = true;
  btn.textContent = '⏳ エントリー中...';
  msg.style.color = 'var(--muted2)';
  msg.textContent = 'Binance実データで約定中...';
  try {
    const r = await fetch('/api/manual_entry', {method: 'POST'});
    const d = await r.json();
    if (d.ok) {
      msg.style.color = 'var(--green)';
      msg.innerHTML = `✅ <strong>エントリー成功！</strong> ${d.qty.toFixed(6)} BTC @ $${d.entry_price.toFixed(2)}`;
      btn.textContent = '✅ エントリー完了';
      setTimeout(() => refresh(), 500);
    } else {
      msg.style.color = 'var(--red)';
      msg.textContent = '❌ ' + (d.msg || 'エラー');
      btn.disabled = false;
      btn.textContent = '🚀 空きスロット全部にエントリー（2倍ロング）';
    }
  } catch (e) {
    msg.style.color = 'var(--red)';
    msg.textContent = '❌ 通信エラー';
    btn.disabled = false;
    btn.textContent = '🚀 空きスロット全部にエントリー（2倍ロング）';
  }
}

// 手動決済
async function manualClose() {
  const btn = document.getElementById('close-btn');
  const msg = document.getElementById('scan-msg');
  btn.disabled = true;
  btn.textContent = '⏳ 決済中...';
  try {
    const r = await fetch('/api/manual_close', {method: 'POST'});
    const d = await r.json();
    if (d.ok) {
      msg.style.color = d.pnl >= 0 ? 'var(--green)' : 'var(--red)';
      msg.innerHTML = `✅ <strong>決済完了</strong> 損益: ${d.pnl >= 0 ? '+' : ''}$${d.pnl.toFixed(2)}`;
      setTimeout(() => refresh(), 500);
    } else {
      msg.style.color = 'var(--red)';
      msg.textContent = '❌ ' + (d.msg || 'エラー');
    }
  } catch (e) {
    msg.style.color = 'var(--red)';
    msg.textContent = '❌ 通信エラー';
  }
  btn.disabled = false;
  btn.textContent = '💥 全ポジション一括決済';
}

function elapsed(startIso) {
  if (!startIso) return '—';
  const ms = Date.now() - new Date(startIso).getTime();
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  if (h === 0) return m + '分';
  return h + '時間' + m + '分';
}

async function refresh() {
  try {
    const r = await fetch('/api/state');
    const s = await r.json();
    const hist = s.equity_history || [];
    const trades = s.trades || [];
    const latestEq = hist.length ? hist[hist.length - 1].equity : s.cash;
    const initCap  = s.initial_capital;

    // ─ 総資産 ─
    document.getElementById('s-eq').textContent  = fmtInt(latestEq);
    document.getElementById('s-eq2').textContent = '残高 ' + fmt(s.cash);

    // ─ 含み損益（全ポジションの未決済PnL合計）─
    let unrealized = 0;
    const posMapLocal = s.positions || {};
    const sdLocal = s.symbol_data || {};
    Object.entries(posMapLocal).forEach(([sym, p]) => {
      const cur = (sdLocal[sym] && sdLocal[sym].price) ? sdLocal[sym].price : p.entry_price;
      unrealized += p.qty * (cur - p.entry_price) * p.leverage;
    });
    const upEl = document.getElementById('s-up');
    upEl.textContent = (unrealized >= 0 ? '+' : '-') + fmt(Math.abs(unrealized));
    upEl.className   = 'sb-val ' + (unrealized >= 0 ? 'g' : 'r');
    const upPct = initCap ? (unrealized / initCap * 100) : 0;
    document.getElementById('s-up2').textContent = (upPct >= 0 ? '+' : '') + upPct.toFixed(2) + '%';

    // ─ 確定損益（全CLOSEトレードのPnL合計）─
    const closedTrades = trades.filter(t => t.action === 'CLOSE');
    const realizedPnl = closedTrades.reduce((a, t) => a + (t.pnl || 0), 0);
    const rpEl = document.getElementById('s-rp');
    rpEl.textContent = (realizedPnl >= 0 ? '+' : '-') + fmt(Math.abs(realizedPnl));
    rpEl.className   = 'sb-val ' + (realizedPnl >= 0 ? 'g' : 'r');
    const rpPct = initCap ? (realizedPnl / initCap * 100) : 0;
    document.getElementById('s-rp2').textContent = (rpPct >= 0 ? '+' : '') + rpPct.toFixed(2) + '%';

    // ─ 税金・税引後（日本累進・雑所得ざっくり） ─
    let taxRate = 0.20;  // 累進想定の中位
    if (realizedPnl <= 0) taxRate = 0;
    const tax = Math.max(0, realizedPnl * taxRate);
    const afterTax = realizedPnl - tax;
    document.getElementById('s-tax').textContent   = fmt(tax);
    document.getElementById('s-after').textContent = fmt(afterTax);

    // ─ 本日損益（直近24h以内のトレード）─
    const nowMs = Date.now();
    const todayPnl = closedTrades.filter(t => (nowMs - new Date(t.ts).getTime()) < 86400000)
                                  .reduce((a, t) => a + (t.pnl || 0), 0);
    const tdEl = document.getElementById('s-td');
    tdEl.textContent = (todayPnl >= 0 ? '+' : '-') + fmt(Math.abs(todayPnl));
    tdEl.className   = 'sb-val ' + (todayPnl >= 0 ? 'g' : 'r');
    const tdPct = initCap ? (todayPnl / initCap * 100) : 0;
    document.getElementById('s-td2').textContent = (tdPct >= 0 ? '+' : '') + tdPct.toFixed(2) + '%';

    // ─ 勝率 ─
    const wins   = closedTrades.filter(t => (t.pnl || 0) > 0).length;
    const losses = closedTrades.length - wins;
    document.getElementById('s-wr').textContent  = closedTrades.length ? Math.round(wins / closedTrades.length * 100) + '%' : '—';
    document.getElementById('s-wrs').textContent = wins + '勝' + losses + '敗';

    // ─ レバレッジ（平均）─
    const nPos = Object.keys(posMapLocal).length;
    const avgLev = nPos ? (Object.values(posMapLocal).reduce((a,p)=>a+p.leverage,0) / nPos) : 2.0;
    document.getElementById('s-lv').textContent = avgLev.toFixed(1) + '倍';

    // ─ 保有中 ─
    const maxPos = 50;
    document.getElementById('s-ps').textContent  = `${nPos}/${maxPos}件`;
    document.getElementById('s-ps2').textContent = `監視 ${Object.keys(sdLocal).length}銘柄`;

    // ─ 検証経過 ─
    document.getElementById('s-elapsed').textContent    = elapsed(s.started_at);
    document.getElementById('s-scancount').textContent  = 'スキャン ' + (s.scan_count || 0) + '回';

    // ─ 連敗 ─
    let streak = 0, maxStreak = 0;
    closedTrades.forEach(t => {
      if ((t.pnl || 0) > 0) streak = 0;
      else { streak++; if (streak > maxStreak) maxStreak = streak; }
    });
    document.getElementById('s-streak').textContent     = streak;
    document.getElementById('s-streak-max').textContent = '最大連敗 ' + maxStreak;

    // ─ PF / Sharpe ─
    const totalWin = closedTrades.filter(t => (t.pnl || 0) > 0).reduce((a, t) => a + (t.pnl || 0), 0);
    const totalLoss = Math.abs(closedTrades.filter(t => (t.pnl || 0) < 0).reduce((a, t) => a + (t.pnl || 0), 0));
    const pf = totalLoss > 0 ? totalWin / totalLoss : (totalWin > 0 ? 99.9 : 0);
    document.getElementById('s-pf').textContent = closedTrades.length ? pf.toFixed(2) : '—';

    // ─ 手数料累計（簡易推定: notional × 0.06% × 2）─
    const totalFees = trades.reduce((a, t) => {
      const notional = t.qty * (t.exit_price || t.entry_price) * (t.leverage || 1);
      return a + notional * 0.0006;
    }, 0);
    document.getElementById('s-fees').textContent = fmt(totalFees);
    const feesPerTrade = trades.length ? (totalFees / trades.length) : 0;
    const feesRatio = Math.abs(realizedPnl) > 0 ? (totalFees / Math.abs(realizedPnl) * 100) : 0;
    document.getElementById('s-fees-ratio').textContent =
      `概$${feesPerTrade.toFixed(3)}/回・損益比 ${feesRatio.toFixed(1)}%`;

    // ─ DD ─
    let peak = initCap, maxDd = 0;
    hist.forEach(h => {
      if (h.equity > peak) peak = h.equity;
      const dd = peak > 0 ? (peak - h.equity) / peak * 100 : 0;
      if (dd > maxDd) maxDd = dd;
    });
    document.getElementById('ddnum').textContent = (maxDd > 0 ? '-' : '') + maxDd.toFixed(2) + '%';
    const ddFill = document.getElementById('ddfill');
    const ddPill = document.getElementById('ddpill');
    ddFill.style.width = Math.min(100, maxDd * 4) + '%';
    if (maxDd < 5)      { ddFill.style.background = 'var(--green)';  ddPill.className = 'dd-pill ddp-ok';     ddPill.textContent = '正常'; }
    else if (maxDd<15) { ddFill.style.background = 'var(--yellow)'; ddPill.className = 'dd-pill ddp-warn';  ddPill.textContent = '注意'; }
    else               { ddFill.style.background = 'var(--red)';     ddPill.className = 'dd-pill ddp-danger'; ddPill.textContent = '危険'; }

    // ─ BTC価格 ─
    document.getElementById('btcprice').textContent = 'BTC ' + fmtBtc(s.btc_price);

    // ─ 本日損失バー ─
    const dailyLossPct = todayPnl < 0 ? Math.abs(todayPnl) / initCap * 100 : 0;
    document.getElementById('dl-fill').style.width = Math.min(100, dailyLossPct * 20) + '%';
    document.getElementById('dl-pct').textContent  = dailyLossPct.toFixed(1) + '%';

    // ─ 保有ポジション（複数件対応）─
    const posArea = document.getElementById('pos-area');
    const posMap = s.positions || {};
    const syms = Object.keys(posMap);
    if (syms.length === 0) {
      posArea.innerHTML = '<div class="pos-empty">保有ポジションなし — エントリー待機中</div>';
    } else {
      const sd = s.symbol_data || {};
      posArea.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:8px">${
        syms.map(sym => {
          const p = posMap[sym];
          const curPrice = (sd[sym] && sd[sym].price) ? sd[sym].price : p.entry_price;
          const unreal = p.qty * (curPrice - p.entry_price) * p.leverage;
          const retPct = (unreal / (p.alloc_usd || 1) * 100);
          const coin = sym.replace('/USDT', '');
          return `
            <div class="pos-card" style="padding:10px">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                <div><span class="pill pl-g">L</span> <strong style="font-size:13px">${coin}</strong></div>
                <strong style="font-size:13px;color:${unreal>=0?'var(--green)':'var(--red)'}">${unreal>=0?'+':''}$${unreal.toFixed(2)}</strong>
              </div>
              <div style="font-size:10px;color:var(--muted2);line-height:1.6">
                元本: $${(p.alloc_usd||0).toFixed(0)} × ${p.leverage}x<br>
                エントリー: $${p.entry_price.toFixed(p.entry_price<10?4:2)}<br>
                現在値: $${curPrice.toFixed(curPrice<10?4:2)} (${retPct>=0?'+':''}${retPct.toFixed(2)}%)
              </div>
            </div>`;
        }).join('')
      }</div>`;
    }

    // ─ シグナル一覧（全銘柄）─
    const sigBody = document.getElementById('sig-body');
    const btcBull = (s.btc_price && s.btc_ema200 && s.btc_ema50)
                    ? (s.btc_price > s.btc_ema200 && s.btc_ema50 > s.btc_ema200)
                    : false;
    const entries = Object.entries(sdLocal);
    if (entries.length === 0) {
      sigBody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px">データ取得中...</td></tr>';
    } else {
      // ADX降順でソート
      entries.sort((a, b) => (b[1].adx || 0) - (a[1].adx || 0));
      sigBody.innerHTML = entries.slice(0, 25).map(([sym, d]) => {
        const coin = sym.replace('/USDT', '');
        const held = posMapLocal[sym] ? true : false;
        const adx  = d.adx || 0;
        const lev  = btcBull ? (d.signal_lev || 0) : 0;
        const scorePct = Math.min(100, adx * 2);
        let sigType = '—', sigClass = 'pl-gray', rowClass = '', stateTxt = '';
        if (held) {
          stateTxt = '<span class="pill pl-g">保有</span>';
          sigType = '✓ LONG';
          sigClass = 'pl-g';
          rowClass = 'lrow';
        } else if (lev >= 2) {
          stateTxt = '<span class="pill pl-g">強ENTRY</span>';
          sigType = `↑ 2x`;
          sigClass = 'pl-g';
          rowClass = 'lrow';
        } else if (lev >= 1) {
          stateTxt = '<span class="pill pl-y">ENTRY</span>';
          sigType = `↑ 1x`;
          sigClass = 'pl-y';
          rowClass = 'lrow';
        } else {
          stateTxt = '<span class="pill pl-gray">待機</span>';
          sigType = '—';
        }
        const price = d.price || 0;
        const priceFmt = price < 1 ? price.toFixed(5) : (price < 10 ? price.toFixed(4) : price.toFixed(2));
        return `
          <tr class="${rowClass}">
            <td>${stateTxt}</td>
            <td><strong>${coin}</strong></td>
            <td>
              <div class="scbar">
                <div class="sctrack"><div class="scfill" style="width:${scorePct}%;background:${lev>0?'var(--green)':'var(--muted2)'}"></div></div>
                <span class="scnum">${Math.round(scorePct)}</span>
              </div>
            </td>
            <td>$${priceFmt}</td>
            <td><span class="pill ${sigClass}">${sigType}</span></td>
          </tr>`;
      }).join('');
    }

    // ─ ポートフォリオタブ ─
    document.getElementById('pf-bal').textContent     = fmt(latestEq);
    document.getElementById('pf-rpnl').textContent    = (realizedPnl >= 0 ? '+' : '-') + fmt(Math.abs(realizedPnl));
    document.getElementById('pf-rpnl').className      = 'pfv ' + (realizedPnl >= 0 ? 'g' : 'r');
    document.getElementById('pf-rpct').textContent    = (rpPct >= 0 ? '+' : '') + rpPct.toFixed(2) + '%';
    document.getElementById('pf-ntrades').textContent = trades.length;
    document.getElementById('pf-wl').textContent      = wins + '勝 / ' + losses + '敗';
    document.getElementById('pf-winrate').textContent = closedTrades.length ? Math.round(wins/closedTrades.length*100) + '%' : '—';
    document.getElementById('pf-dd').textContent      = maxDd.toFixed(2) + '%';
    document.getElementById('pf-scans').textContent   = s.scan_count || 0;

    // ─ 手動エントリータブの状態更新 ─
    document.getElementById('sc-price').textContent  = fmtBtc(s.btc_price);
    document.getElementById('sc-ema50').textContent  = fmtBtc(s.btc_ema50);
    document.getElementById('sc-ema200').textContent = fmtBtc(s.btc_ema200);
    document.getElementById('sc-adx').textContent    = s.btc_adx !== null ? s.btc_adx.toFixed(2) : '—';
    document.getElementById('sc-signal').textContent = s.last_signal || '—';

    // エントリー/決済ボタンの表示切替
    const scanBtn  = document.getElementById('scan-btn');
    const closeBtn = document.getElementById('close-btn');
    if (s.position) {
      scanBtn.style.display  = 'none';
      closeBtn.style.display = 'block';
    } else {
      scanBtn.style.display  = 'block';
      closeBtn.style.display = 'none';
      scanBtn.disabled = false;
      scanBtn.textContent = '🚀 空きスロット全部にエントリー（2倍ロング）';
    }

    // ─ 取引履歴タブ ─
    const histBody = document.getElementById('hist-body');
    if (trades.length === 0) {
      histBody.innerHTML = '<tr><td colspan="6"><div class="empty"><div class="ei">📭</div><div class="et">取引履歴なし</div><div class="es">エントリーするとここに表示されます</div></div></td></tr>';
    } else {
      histBody.innerHTML = trades.slice().reverse().map(t => {
        const rClass = t.action === 'OPEN' ? 'lrow' : '';
        const pClass = t.action === 'OPEN' ? 'pl-g' : 'pl-r';
        const pnlClass = (t.pnl || 0) >= 0 ? 'g' : 'r';
        const manual = t.manual ? ' <span style="font-size:9px;color:var(--yellow)">✋手動</span>' : '';
        return `
          <tr class="${rClass}">
            <td style="font-size:11px;color:var(--muted2)">${new Date(t.ts).toLocaleString('ja-JP')}</td>
            <td><span class="pill ${pClass}">${t.action}</span>${manual}</td>
            <td>${fmtBtc(t.exit_price || t.entry_price)}</td>
            <td>${t.qty.toFixed(6)}</td>
            <td>${t.leverage}x</td>
            <td class="${pnlClass}" style="text-align:right;font-weight:800">${t.pnl !== undefined ? ((t.pnl>=0?'+':'')+fmt(t.pnl)) : '—'}</td>
          </tr>`;
      }).join('');
    }
  } catch (e) { console.error(e); }
}

// 市況データ（Fear & Greed, Funding rate, Bull%）を並列取得
async function refreshMarket() {
  // Fear & Greed
  try {
    const r = await fetch('https://api.alternative.me/fng/?limit=1');
    const d = await r.json();
    if (d.data && d.data[0]) {
      const val = parseInt(d.data[0].value);
      const cls = d.data[0].value_classification;
      const jp  = {'Extreme Fear':'極度の恐怖','Fear':'恐怖','Neutral':'中立','Greed':'強欲','Extreme Greed':'極度の強欲'}[cls] || cls;
      let emo = '😐';
      if (val < 25)      emo = '😱';
      else if (val < 45) emo = '😨';
      else if (val > 75) emo = '🤑';
      else if (val > 55) emo = '😊';
      document.getElementById('fg-val').textContent = emo + ' ' + val;
      document.getElementById('fg-lbl').textContent = jp;
    }
  } catch (e) {}

  // Funding rate (BTCUSDT perp)
  try {
    const r = await fetch('https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT');
    const d = await r.json();
    if (d.lastFundingRate) {
      const fr = parseFloat(d.lastFundingRate) * 100;
      const frEl = document.getElementById('fr-val');
      frEl.textContent = (fr >= 0 ? '+' : '') + fr.toFixed(4) + '%';
      frEl.className   = 'mkt-val ' + (fr >= 0.01 ? 'g' : (fr < -0.01 ? 'r' : 'y'));
    }
  } catch (e) {}

  // Bull% (上位銘柄の24h+%比率)
  try {
    const r = await fetch('https://api.binance.com/api/v3/ticker/24hr?type=MINI');
    const d = await r.json();
    const usdt = d.filter(t => t.symbol.endsWith('USDT')).slice(0, 50);
    const bulls = usdt.filter(t => parseFloat(t.priceChangePercent) > 0).length;
    const pct = Math.round(bulls / usdt.length * 100);
    document.getElementById('mb-val').textContent = pct + '%';
  } catch (e) {}
}

refresh();
refreshMarket();
setInterval(refresh, 5000);
setInterval(refreshMarket, 120000);
</script>
</body></html>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/state")
def api_state():
    return jsonify(load_state())


@app.route("/api/manual_entry", methods=["POST"])
def api_manual_entry():
    """
    手動「スロット埋め」: 現在保有していない全銘柄の最新価格を取得し、
    空きスロット数だけ均等に資金を振り分けてレバ2倍ロングでエントリー。
    """
    state = load_state()
    try:
        fetcher = DataFetcher(Config())
        assert_binance_source(fetcher)
        # 未保有銘柄のリスト
        open_slots = MAX_POSITIONS - len(state.get("positions", {}))
        if open_slots <= 0:
            return jsonify({"ok": False, "msg": "空きスロットなし"}), 400

        candidates = [s for s in SYMBOLS if s not in state.get("positions", {})][:open_slots]
        if not candidates:
            return jsonify({"ok": False, "msg": "候補銘柄なし"}), 400

        ts = datetime.now().isoformat()
        per_slot = state["cash"] / max(open_slots, 1)
        if per_slot < 10:
            return jsonify({"ok": False, "msg": f"現金不足 per_slot=${per_slot:.2f}"}), 400

        opened = 0
        failed = []
        for sym in candidates:
            try:
                ticker = fetcher._exchange.fetch_ticker(sym)
                price = float(ticker["last"])
                if price <= 0:
                    failed.append(sym)
                    continue
                _open_one(state, sym, price, 2.0, per_slot, ts)
                opened += 1
            except Exception as e:
                failed.append(f"{sym}({e})")

        state["last_signal"] = f"✋ 手動エントリー {opened}件 / 候補{len(candidates)}件 (1件あたり${per_slot:.0f})"
        state["bot_status"] = f"稼働中 ({len(state['positions'])}件保有)"
        save_state(state)
        return jsonify({
            "ok": True, "opened": opened, "candidates": len(candidates),
            "per_slot": round(per_slot, 2), "failed": failed,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/manual_close", methods=["POST"])
def api_manual_close():
    """全ポジション一括決済"""
    state = load_state()
    try:
        positions = state.get("positions", {})
        if not positions:
            return jsonify({"ok": False, "msg": "ポジションなし"}), 400
        fetcher = DataFetcher(Config())
        assert_binance_source(fetcher)

        ts = datetime.now().isoformat()
        closed = 0
        total_pnl = 0.0
        for sym in list(positions.keys()):
            try:
                ticker = fetcher._exchange.fetch_ticker(sym)
                price_now = float(ticker["last"])
                p = state["positions"][sym]
                exit_price = price_now * (1 - SLIP)
                pnl = p["qty"] * (exit_price - p["entry_price"]) * p["leverage"]
                notional = p["qty"] * exit_price * p["leverage"]
                pnl -= notional * FEE
                entry_ts_dt = datetime.fromisoformat(p["entry_ts"])
                hold_h = (datetime.now() - entry_ts_dt).total_seconds() / 3600
                pnl -= notional * FUNDING_PH * hold_h
                state["cash"] += p.get("alloc_usd", 0) + pnl
                state["trades"].append({
                    "action": "CLOSE",
                    "symbol": sym,
                    "entry_price": p["entry_price"],
                    "exit_price": round(exit_price, 2),
                    "qty": p["qty"],
                    "leverage": p["leverage"],
                    "pnl": round(pnl, 2),
                    "hold_hours": round(hold_h, 2),
                    "ts": ts,
                    "manual": True,
                })
                total_pnl += pnl
                del state["positions"][sym]
                closed += 1
            except Exception as e:
                print(f"Close error {sym}: {e}")

        state["last_signal"] = f"✋ 全決済: {closed}件 / PnL合計 {total_pnl:+.2f}"
        state["position"] = None
        save_state(state)
        return jsonify({"ok": True, "closed": closed, "total_pnl": total_pnl})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# BTCチャート用のローソク足＋EMA履歴
_btc_cache = {"ts": 0, "data": None}
@app.route("/api/btc_history")
def api_btc_history():
    """直近90日のBTC日足 + EMA50/EMA200（キャッシュ10分）"""
    now = time.time()
    if _btc_cache["data"] and now - _btc_cache["ts"] < 600:
        return jsonify(_btc_cache["data"])
    try:
        fetcher = DataFetcher(Config())
        assert_binance_source(fetcher)
        from datetime import timedelta
        today = datetime.now()
        start = (today - timedelta(days=320)).strftime("%Y-%m-%d")
        end   = today.strftime("%Y-%m-%d")
        df = fetcher.fetch_historical_ohlcv(SYMBOL, "1d", start, end)
        validate_ohlcv_data(df, SYMBOL, "1d")
        df = compute_indicators(df).tail(120)  # 直近120本表示
        candles = [{
            "time": int(ts.timestamp()),
            "open": float(r["open"]), "high": float(r["high"]),
            "low":  float(r["low"]),  "close": float(r["close"]),
        } for ts, r in df.iterrows()]
        ema50  = [{"time": int(ts.timestamp()), "value": float(r["ema50"])}
                  for ts, r in df.iterrows() if not pd.isna(r["ema50"])]
        ema200 = [{"time": int(ts.timestamp()), "value": float(r["ema200"])}
                  for ts, r in df.iterrows() if not pd.isna(r["ema200"])]
        data = {"candles": candles, "ema50": ema50, "ema200": ema200}
        _btc_cache["ts"]   = now
        _btc_cache["data"] = data
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══ バックグラウンドのシグナルチェックループ ═══
def signal_loop():
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)
    while True:
        try:
            state = load_state()
            update_bot(state, fetcher)
        except Exception as e:
            print(f"Signal loop error: {e}")
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    # 初回: state ファイルなければ作成
    if not STATE_FILE.exists():
        save_state(initial_state())
        print(f"💾 新規state作成: {STATE_FILE}")

    # バックグラウンドでシグナルループ
    t = threading.Thread(target=signal_loop, daemon=True)
    t.start()

    # 初回の即時チェック（起動直後に指標を埋めるため）
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)
    state = load_state()
    update_bot(state, fetcher)

    # Flask起動
    print(f"\n🚀 DL MAX 2x Bot 起動中...")
    print(f"📱 URL: http://localhost:{PORT}")
    print(f"📱 iPhone: http://<MacのWiFi IP>:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
