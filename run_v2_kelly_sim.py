"""
run_v2_kelly_sim.py — V2 Kelly戦略 ローカルSIM実行
==================================================
バックテストで月+10.49%・プラス率100%・清算0を出したV2 Kelly戦略を
リアルタイムのBinance価格で紙取引（SIM）する。

設定:
  - BNB 70% + BTC 30%
  - Lookback 60日
  - Kelly fraction 0.5
  - Max leverage 10x
  - Cooldown -25%
  - min_leverage_threshold 1.0（Kelly<1.0は取引スキップ）
  - 月次リバランス

実行方法:
  python3 run_v2_kelly_sim.py
  （1時間毎に状態チェック・月次でリバランス・状態はv2_sim_state.jsonに保存）
"""

from __future__ import annotations
import json
import time
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd
import ccxt

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("v2_sim")

# ── 設定 ──
INITIAL = 3_000.0
FEE = 0.0006
SLIP = 0.001
MMR = 0.005

CFG = {
    "allocations": {"BNB": 0.7, "BTC": 0.3},
    "lookback": 60,
    "fraction": 0.5,
    "max_leverage": 10,
    "cooldown_threshold": -0.25,
    "min_leverage_threshold": 1.0,
    "cash_buffer": 0.0,
    "rebalance_days": 30,
}

SYMBOLS = {"BNB": "BNB/USDT:USDT", "BTC": "BTC/USDT:USDT"}
STATE_FILE = Path(__file__).parent / "v2_sim_state.json"
LOG_FILE = Path(__file__).parent / "v2_sim_log.txt"
CHECK_INTERVAL_SEC = 3600  # 1時間毎に状態チェック


@dataclass
class Position:
    entry: float
    size: float
    lev: float
    margin: float
    symbol: str
    opened_at: str


def fetch_history(ex, symbol: str, days: int = 200) -> pd.DataFrame:
    """過去days日の1日足を取得"""
    since_ms = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
    batch = ex.fetch_ohlcv(symbol, "1d", since=since_ms, limit=days + 10)
    if not batch:
        return pd.DataFrame()
    df = pd.DataFrame(batch, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.set_index("timestamp").sort_index().astype(float)


def compute_kelly(df_hist: pd.DataFrame, lookback: int, fraction: float, max_lev: float) -> float:
    returns = df_hist["close"].pct_change().dropna()
    if len(returns) < lookback:
        return 0.0
    recent = returns.tail(lookback)
    mean_ann = recent.mean() * 365
    var_ann = recent.var() * 365
    if var_ann <= 0 or mean_ann <= 0:
        return 0.0
    kelly = (mean_ann / var_ann) * fraction
    return float(np.clip(kelly, 0, max_lev))


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "cash": INITIAL,
        "positions": {},
        "last_rebal": None,
        "last_snapshot": INITIAL,
        "cooldowns": 0,
        "liquidations": 0,
        "started_at": datetime.now().isoformat(),
        "history": [],
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str, ensure_ascii=False))


def log_event(msg: str):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    logger.info(msg)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def get_current_prices(ex) -> Dict[str, float]:
    prices = {}
    for name, sym in SYMBOLS.items():
        ticker = ex.fetch_ticker(sym)
        prices[name] = float(ticker["last"])
    return prices


def compute_equity(state: dict, prices: Dict[str, float]) -> float:
    equity = state["cash"]
    for sym, pos in state["positions"].items():
        if sym in prices:
            pnl = (prices[sym] - pos["entry"]) * pos["size"]
            equity += pos["margin"] + pnl
    return equity


def check_liquidation(state: dict, prices: Dict[str, float]) -> int:
    """清算チェック — メンテナンスマージン割れたらポジション削除"""
    liq = 0
    for sym in list(state["positions"].keys()):
        pos = state["positions"][sym]
        if sym not in prices:
            continue
        p = prices[sym]
        current_pnl = (p - pos["entry"]) * pos["size"]
        eq = pos["margin"] + current_pnl
        mm = p * pos["size"] * MMR
        if eq <= mm:
            log_event(f"💀 清算発生: {sym} エントリー${pos['entry']:.2f} → 現在${p:.2f}")
            del state["positions"][sym]
            liq += 1
    return liq


def should_rebalance(state: dict) -> bool:
    if state["last_rebal"] is None:
        return True
    last = datetime.fromisoformat(state["last_rebal"]) if isinstance(state["last_rebal"], str) else state["last_rebal"]
    return (datetime.now() - last).days >= CFG["rebalance_days"]


def rebalance(ex, state: dict, prices: Dict[str, float]):
    log_event(f"🔄 月次リバランス開始")

    # 決済
    for sym in list(state["positions"].keys()):
        pos = state["positions"][sym]
        if sym not in prices:
            continue
        exit_p = prices[sym] * (1 - SLIP)
        pnl = (exit_p - pos["entry"]) * pos["size"]
        fee = exit_p * pos["size"] * FEE
        realized = max(pos["margin"] + pnl - fee, 0)
        state["cash"] += realized
        pct = (realized / pos["margin"] - 1) * 100 if pos["margin"] > 0 else 0
        log_event(f"   📤 {sym}決済 ${pos['entry']:.2f}→${prices[sym]:.2f} 損益{pct:+.2f}% 回収${realized:.2f}")
        del state["positions"][sym]

    total = state["cash"]

    # Cooldown判定
    cooldown = False
    if state["last_snapshot"] > 0:
        period_ret = total / state["last_snapshot"] - 1
        if period_ret <= CFG["cooldown_threshold"]:
            cooldown = True
            state["cooldowns"] += 1
            log_event(f"   ❄️ クールダウン発動: 前期から{period_ret*100:.1f}%下落 → 今月は取引せず")
    state["last_snapshot"] = total

    if not cooldown:
        usable = total * (1 - CFG["cash_buffer"])

        for sym, w in CFG["allocations"].items():
            df = fetch_history(ex, SYMBOLS[sym], days=200)
            if df.empty:
                log_event(f"   ⚠️ {sym}履歴取得失敗")
                continue

            kl = compute_kelly(df, CFG["lookback"], CFG["fraction"], CFG["max_leverage"])

            if kl < CFG["min_leverage_threshold"]:
                log_event(f"   🚫 {sym}Kelly={kl:.2f}<{CFG['min_leverage_threshold']} スキップ")
                continue

            alloc = usable * w
            entry = prices[sym] * (1 + SLIP)
            notional = alloc * kl
            size = notional / entry
            fee_cost = notional * FEE
            margin = alloc - fee_cost

            if margin <= 0 or margin > state["cash"]:
                continue

            state["positions"][sym] = {
                "entry": entry,
                "size": size,
                "lev": kl,
                "margin": margin,
                "symbol": sym,
                "opened_at": datetime.now().isoformat(),
            }
            state["cash"] -= margin
            log_event(f"   📥 {sym}ロング建て レバ{kl:.2f}x 配分{w*100:.0f}% 投入${margin:.2f} ${entry:.2f}")

    state["last_rebal"] = datetime.now().isoformat()


def check_cycle(ex, state: dict):
    prices = get_current_prices(ex)

    state["liquidations"] += check_liquidation(state, prices)

    if should_rebalance(state):
        rebalance(ex, state, prices)

    equity = compute_equity(state, prices)

    # 履歴記録（1日1件に絞る）
    today_iso = datetime.now().strftime("%Y-%m-%d")
    if not state["history"] or state["history"][-1]["date"] != today_iso:
        state["history"].append({
            "date": today_iso,
            "equity": equity,
            "cash": state["cash"],
            "bnb_price": prices.get("BNB", 0),
            "btc_price": prices.get("BTC", 0),
        })
        if len(state["history"]) > 400:
            state["history"] = state["history"][-400:]

    elapsed_days = (datetime.now() - datetime.fromisoformat(state["started_at"])).days
    ret_pct = (equity / INITIAL - 1) * 100
    pos_count = len(state["positions"])
    log_event(
        f"📊 Equity=${equity:.2f} ({ret_pct:+.2f}%) "
        f"Cash=${state['cash']:.2f} ポジ{pos_count}件 "
        f"経過{elapsed_days}日 清算{state['liquidations']}回 "
        f"BNB=${prices['BNB']:.2f} BTC=${prices['BTC']:.2f}"
    )


def main():
    log_event(f"🚀 V2 Kelly SIM開始 初期資金${INITIAL:.0f}")
    log_event(f"   設定: BNB{CFG['allocations']['BNB']*100:.0f}% + BTC{CFG['allocations']['BTC']*100:.0f}%, "
              f"Kelly fraction {CFG['fraction']}, max lev {CFG['max_leverage']}x, "
              f"Kelly<{CFG['min_leverage_threshold']}スキップ, Cooldown {CFG['cooldown_threshold']*100:.0f}%")

    ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})

    state = load_state()

    while True:
        try:
            check_cycle(ex, state)
            save_state(state)
        except Exception as e:
            log_event(f"⚠️ エラー: {e}")
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
