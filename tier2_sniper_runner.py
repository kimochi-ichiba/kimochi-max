"""tier2 Sniper Bot 雛形 (実 SIM 用).

demo_runner.py (tier1) と完全分離した sniper Bot。state も別、ログも別、
資金も独立。新規上場 (Binance / DEX) を検知して 5x TP / -50% SL で運用。

⚠️ バックテスト (PR #14) では 100% v2.5_chop に劣るため、本実装は
現状 SIM 推奨ではない。将来 Pump.fun 等の DEX データソースが
整った時の参考実装。

Usage:
    python tier2_sniper_runner.py           # SIM (デフォルト)
    python tier2_sniper_runner.py --once    # 1 回のみ実行 (テスト)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import urllib.request

PROJECT = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT / "results"
STATE_PATH = RESULTS_DIR / "sniper_state.json"
LOG_PATH = PROJECT / "sniper_runner.log"

# 設定
INITIAL = 2500.0  # 初期 $2,500 (10 万円の 25%)
LISTING_WATCH_DAYS = 30
TP_MULTIPLE = 5.0
SL_PCT = 0.50
ALLOC_PER_TRADE_PCT = 0.20  # pool の 20% × 5 並列
MAX_CONCURRENT = 5
TIMEOUT_DAYS = 180
TICK_INTERVAL_SEC = 60  # 1 分ごとチェック
SNAPSHOT_INTERVAL_SEC = 300  # 5 分ごと state 保存

# データソース
BINANCE_NEW_LISTINGS_URL = (
    "https://api.binance.com/api/v3/exchangeInfo"
)
BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"


def log(msg: str, also_print: bool = True):
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    line = f"[{ts}] {msg}"
    if also_print:
        print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def http_get_json(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ─────────────────────────────
# state 管理
# ─────────────────────────────
def fresh_state() -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "version": "tier2-sniper-0.1",
        "version_name": "Tier 2 Sniper Bot v0.1 (雛形)",
        "started_at": now,
        "last_update": now,
        "initial_capital": INITIAL,
        "cash": INITIAL,
        "open_positions": {},     # symbol -> {entry_price, entry_ts, qty, alloc}
        "trades": [],             # 過去のトレード履歴
        "stats": {
            "n_tp": 0, "n_sl": 0, "n_timeout": 0,
            "total_pnl": 0.0,
        },
        "watched_symbols": [],    # 検知した新規上場銘柄リスト
    }


def load_state() -> dict:
    if not STATE_PATH.exists():
        return fresh_state()
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"⚠️ state読込失敗: {e} → 初期化")
        return fresh_state()


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["last_update"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(STATE_PATH)


# ─────────────────────────────
# 新規上場検知 (簡易)
# ─────────────────────────────
def fetch_listed_symbols() -> set[str]:
    """Binance に上場中の USDT ペアシンボル一覧を取得."""
    try:
        info = http_get_json(BINANCE_NEW_LISTINGS_URL, timeout=20)
        symbols = info.get("symbols", [])
        return {
            s["baseAsset"] for s in symbols
            if s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"
            and not s.get("isMarginTradingAllowed", False) is None  # 上場確認
        }
    except Exception as e:
        log(f"⚠️ Binance API 取得失敗: {e}")
        return set()


def detect_new_listings(prev_set: set[str], current_set: set[str]) -> list[str]:
    """前回スナップショットとの差分で新規上場を検知."""
    return sorted(current_set - prev_set)


# ─────────────────────────────
# 価格取得
# ─────────────────────────────
def fetch_price(symbol: str) -> float | None:
    try:
        r = http_get_json(f"{BINANCE_PRICE_URL}?symbol={symbol}USDT")
        return float(r["price"])
    except Exception:
        return None


# ─────────────────────────────
# tick 処理
# ─────────────────────────────
def process_tick(state: dict, mode: str = "sim") -> None:
    """1 tick の処理: 新規上場検知 + 既存ポジション TP/SL 判定."""
    now = datetime.now(timezone.utc)

    # 1) オープンポジションの TP/SL チェック
    for sym in list(state["open_positions"].keys()):
        pos = state["open_positions"][sym]
        cur_price = fetch_price(sym)
        if cur_price is None:
            continue

        entry = pos["entry_price"]
        tp_target = entry * TP_MULTIPLE
        sl_target = entry * (1 - SL_PCT)
        entry_ts = datetime.fromisoformat(pos["entry_ts"])
        days_held = (now - entry_ts).days

        exit_reason = None
        if cur_price <= sl_target:
            exit_reason = "SL"
        elif cur_price >= tp_target:
            exit_reason = "TP"
        elif days_held >= TIMEOUT_DAYS:
            exit_reason = "TIMEOUT"

        if exit_reason:
            proceeds = pos["qty"] * cur_price * 0.999  # fee 込み
            pnl = proceeds - pos["alloc"]
            state["cash"] += proceeds
            state["trades"].append({
                "ts": now.isoformat(timespec="seconds"),
                "symbol": sym, "action": "EXIT",
                "reason": exit_reason,
                "entry_price": entry, "exit_price": cur_price,
                "qty": pos["qty"], "pnl_usd": round(pnl, 2),
                "mode": mode,
            })
            state["stats"][f"n_{exit_reason.lower()}"] += 1
            state["stats"]["total_pnl"] += pnl
            del state["open_positions"][sym]
            log(f"  💰 EXIT {sym} ({exit_reason}) @ ${cur_price:.6f} pnl=${pnl:+.2f}")

    # 2) 新規上場検知
    prev_set = set(state.get("watched_symbols", []))
    current_set = fetch_listed_symbols()
    if not current_set:
        return  # API 失敗時はスキップ

    new_listings = detect_new_listings(prev_set, current_set)
    state["watched_symbols"] = sorted(current_set)

    for sym in new_listings:
        # max concurrent 制限
        if len(state["open_positions"]) >= MAX_CONCURRENT:
            log(f"  ⚠️ {sym} 検知、ただし max_concurrent={MAX_CONCURRENT} 到達でスキップ")
            continue
        # 既保有 skip
        if sym in state["open_positions"]:
            continue
        # 資金チェック
        alloc = state["initial_capital"] * ALLOC_PER_TRADE_PCT
        if state["cash"] < alloc:
            log(f"  ⚠️ {sym} 検知、ただし資金不足 (${state['cash']:.2f} < ${alloc:.2f})")
            continue

        price = fetch_price(sym)
        if price is None:
            continue
        buy_price = price * 1.005  # slip 0.5%
        qty = alloc / buy_price * 0.999  # fee
        state["open_positions"][sym] = {
            "entry_price": buy_price,
            "entry_ts": now.isoformat(timespec="seconds"),
            "qty": qty,
            "alloc": alloc,
        }
        state["cash"] -= alloc
        state["trades"].append({
            "ts": now.isoformat(timespec="seconds"),
            "symbol": sym, "action": "ENTRY",
            "price": buy_price, "qty": qty, "alloc": alloc,
            "mode": mode,
        })
        log(f"  🎯 SNIPE {sym} @ ${buy_price:.6f} qty={qty:.4f} alloc=${alloc:.2f}")


# ─────────────────────────────
# main loop
# ─────────────────────────────
def run_once() -> None:
    state = load_state()
    log("━" * 60)
    log("🎯 Tier 2 Sniper Bot — 1 tick (--once)")
    log(f"  cash: ${state['cash']:.2f}  open_positions: {len(state['open_positions'])}")
    process_tick(state, mode="sim")
    save_state(state)


def run_loop() -> None:
    state = load_state()
    log("━" * 60)
    log("🎯 Tier 2 Sniper Bot 起動 (永続ループ)")
    log(f"  initial: ${state['initial_capital']:.2f}")
    log(f"  cash: ${state['cash']:.2f}")
    log(f"  open_positions: {len(state['open_positions'])}")
    log(f"  既上場銘柄数: {len(state.get('watched_symbols', []))}")
    log("━" * 60)

    last_snapshot = time.time()
    try:
        while True:
            try:
                process_tick(state, mode="sim")
            except Exception as e:
                log(f"⚠️ tick error: {e}")

            now = time.time()
            if now - last_snapshot > SNAPSHOT_INTERVAL_SEC:
                save_state(state)
                last_snapshot = now

            time.sleep(TICK_INTERVAL_SEC)
    except KeyboardInterrupt:
        log("⚠️ KeyboardInterrupt - graceful shutdown")
        save_state(state)


def main():
    parser = argparse.ArgumentParser(description="Tier 2 Sniper Bot")
    parser.add_argument("--once", action="store_true",
                        help="1 回だけ実行して終了 (テスト用)")
    args = parser.parse_args()

    if args.once:
        run_once()
    else:
        run_loop()


if __name__ == "__main__":
    main()
