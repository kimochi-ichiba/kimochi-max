"""
気持ちマックス v2.2 デモトレード・ライブランナー (毎秒WebSocket版)
======================================================================
Binance WebSocket で BTC 価格を毎秒受信、3層タイマーで処理:

 [毎秒] tick: WSから最新価格取得 → 総資産再計算 → インメモリ更新
 [60秒] snapshot: equity_history に1分粒度追加 → state.json atomic write
 [5分]  EMA200再計算 (REST klines) + BTCマイルドシグナル判定
 [24h]  50銘柄モメンタム更新 (ヒートマップ)

構成:
  - BTC 40% : EMA200上で保有、下で現金化 (BTCマイルド)
  - ACH 40% : Top3モメンタム実市場連動 (過去90日リターン、月次リバランス)
  - USDT 20%: 年3%金利 (日割)

起動:
  python3 demo_runner.py           # 通常ループ (毎秒tick)
  python3 demo_runner.py --once    # 1回だけREST処理 (既存互換)
  python3 demo_runner.py --reset   # 状態リセットして最初から
"""
from __future__ import annotations
import sys, json, time, urllib.request, urllib.error, urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import deque

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import discord_notify
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False

try:
    from ws_ticker import BTCTickerStream, WEBSOCKET_AVAILABLE
except ImportError:
    WEBSOCKET_AVAILABLE = False
    BTCTickerStream = None

try:
    from live_trader import LiveTrader, get_mode as get_live_mode
    LIVE_TRADER_AVAILABLE = True
except ImportError:
    LIVE_TRADER_AVAILABLE = False
    get_live_mode = lambda: "sim"

PROJECT = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT / "results"
STATE_PATH = RESULTS_DIR / "demo_state.json"
LOG_PATH = PROJECT / "demo_runner.log"

# 設定
INITIAL = 10_000.0
BTC_WEIGHT = 0.35   # v2.1: 0.40 → 0.35 (USDT cushion 増強で損失抑制)
ACH_WEIGHT = 0.35   # v2.1: 0.40 → 0.35
USDT_WEIGHT = 0.30  # v2.1: 0.20 → 0.30 (+10%クッションで DD -5pt 改善 / iter56)
USDT_ANNUAL_RATE = 0.03
LOOP_INTERVAL = 300  # 旧互換: REST のみモードの時に使う
TICK_INTERVAL = 1    # 毎秒tick
SNAPSHOT_INTERVAL = 60   # 60秒ごとにstate.json永続化
EMA_REFRESH_INTERVAL = 300  # 5分ごとにEMA200再計算
# 取引履歴保持件数 (1000件: 月数件 × 60ヶ月 = 数百件で十分余裕)
MAX_TRADE_HISTORY = 1000
# equity履歴保持件数 (100,000件 = 1分粒度で約69日分)
#   旧値 2000 では 33時間分しか持てず、月間グラフ・週間振り返り不可
#   100K件で約2.4MB JSON → 妥当なファイルサイズ
MAX_EQUITY_HISTORY = 100_000

# ━━ F3_YEAREND: 年末リスク回避設定 ━━━━━━━━━━━━━━━━━━━━━━━━━
# iter60 バックテストで唯一全条件クリアした防御機能。
# 12/30〜翌1/2 の期間は全ポジション決済&新規エントリー停止。
# 年末の薄商い+機関投資家手じまいによる大きな下落リスクを回避する。
# バックテスト効果: 年利126%→121%で微減だが最大DD改善、全年プラス維持。
ENABLE_YEAREND_EXIT = True         # 2026-04-23 有効化。12/30〜翌1/2の年末リスクを回避。
YEAREND_EXIT_START_DAY = 30         # 12月のこの日から決済開始 (12/30)
YEAREND_REENTRY_MONTH = 1           # 翌年のこの月から再エントリー許可
YEAREND_REENTRY_DAY = 2             # 翌年のこの日から再エントリー許可 (1/2〜)


def is_yearend_period(now: datetime) -> bool:
    """12/30 00:00 UTC 〜 翌年1/2 00:00 UTC の間 True を返す。
    year-end risk avoidance window の判定関数。
    """
    if now.month == 12 and now.day >= YEAREND_EXIT_START_DAY:
        return True
    if now.month == YEAREND_REENTRY_MONTH and now.day < YEAREND_REENTRY_DAY:
        return True
    return False


# ACH: モメンタムTop3戦略パラメータ
# 2026-04-21 更新: iter49 厳重バックテスト + 5ソース検証の結果を反映
#   除外: MATIC, FTM, MKR, EOS (Binance取引停止、他取引所でも取引不可)
#   追加: POL, TON, ONDO, JUP, WLD, LDO, IMX, WIF, ENA, GALA, JASMY, PENDLE,
#         MINA, RENDER, STRK, SUSHI (16銘柄、Binance+MEXC+Bybit+CoinGecko 全PASS)
ACH_UNIVERSE = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "AVAX", "DOGE", "TRX", "DOT",
    "LINK", "UNI", "LTC", "ATOM", "NEAR", "ICP", "ETC", "XLM", "FIL",
    "APT", "ARB", "OP", "INJ", "SUI", "SEI", "TIA", "RUNE", "ALGO",
    "SAND", "MANA", "AXS", "CHZ", "ENJ", "GRT", "AAVE", "SNX", "CRV",
    "HBAR", "VET", "THETA", "EGLD", "XTZ", "FLOW", "IOTA", "DASH", "ZEC",
    # iter49 追加分 (16銘柄、5ソース検証PASS)
    "POL", "TON", "ONDO", "JUP", "WLD", "LDO", "IMX", "WIF",
    "ENA", "GALA", "JASMY", "PENDLE", "MINA", "RENDER", "STRK", "SUSHI",
]
ACH_TOP_N = 2
# 2026-04-22 v2.1 更新: iter55/iter56 で最終最適化確定
#   USDT30% cushion + 相関フィルター0.80 + モメンタム加重
#   vs 現行v2: ret +4575% → +8931% (2倍), DD 75.3% → 70.5% (-4.8pt)
# 2026-04-24 hybrid 検証: LB20 は DD 悪化 (46.82% → 55.61%) のため LB25 維持
#   実データ検証で Top3/LB25 が Calmar 1.88 と判明。milestone_extraction 追加で Calmar 2.47 可能
# 2026-04-24 C3 採用: Top3→Top2 のみ変更 (LB25/USDT30/corr0.80 は現行維持)
#   iter66 母関数 2020-2024 フル期間検証 (feat/grid-search-benchmark ベースライン):
#     現行 Top3: CAGR +122.7%, MaxDD 62.2%, Sharpe 1.23, final $564K
#     Top2 のみ: CAGR +120.2%, MaxDD 62.0%, Sharpe 1.17, final $533K
#     CAGR 影響 -2.5pp と軽微 (集中度上昇で勝ち銘柄の寄与大も、勝率は減)
#   DD 減らすには corr 0.70 併用が必要だが CAGR -30pp の代償大で不採用
#   Top2 単体は「様子見変更」: v2.2 base の安全性はほぼ維持したまま挙動観察
ACH_LOOKBACK_DAYS = 25
ACH_REBALANCE_DAYS = 7
ACH_CANDIDATE_N = 10           # v2.1: Top10 候補から相関フィルター後に Top3 選定
ACH_CORR_THRESHOLD = 0.80      # v2.1: 相関 0.80 以上は除外 (集中リスク軽減)
ACH_CORR_LOOKBACK_DAYS = 60    # v2.1: 相関計算の過去日数
ACH_WEIGHT_METHOD = "momentum" # v2.1: "equal" or "momentum" (リターン強度で加重)


def log(msg, also_print=True):
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
    line = f"[{ts}] {msg}"
    if also_print:
        print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def http_get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_btc_price_and_ema200():
    """Binance public API から BTC の現在価格と EMA200 を取得"""
    # 現在価格
    ticker_url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    current_price = float(http_get_json(ticker_url)["price"])

    # 過去220日の日次終値取得
    klines_url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=220"
    klines = http_get_json(klines_url)
    closes = [float(k[4]) for k in klines]

    # EMA200 計算 (最後の価格時点)
    alpha = 2 / (200 + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = alpha * c + (1 - alpha) * ema

    # 24h変化率
    ticker_24h_url = "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"
    ticker_24h = http_get_json(ticker_24h_url)
    change_24h_pct = float(ticker_24h["priceChangePercent"])
    volume_24h = float(ticker_24h["quoteVolume"])

    return {
        "current_price": round(current_price, 2),
        "ema200": round(ema, 2),
        "change_24h_pct": round(change_24h_pct, 2),
        "volume_24h_usdt": round(volume_24h, 0),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ACH: Top3モメンタム (実市場連動) のユーティリティ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def fetch_all_current_prices(symbols):
    """指定シンボル全てのBinance現在価格を1回のAPI呼び出しで取得"""
    if not symbols:
        return {}
    syms_json = json.dumps([f"{s}USDT" for s in symbols])
    url = f"https://api.binance.com/api/v3/ticker/price?symbols={urllib.parse.quote(syms_json)}"
    try:
        r = http_get_json(url)
    except Exception as e:
        log(f"⚠️ 一括取得失敗、個別取得にフォールバック: {e}")
        r = []
        for s in symbols:
            try:
                p = http_get_json(f"https://api.binance.com/api/v3/ticker/price?symbol={s}USDT")
                r.append({"symbol": p["symbol"], "price": p["price"]})
                time.sleep(0.05)
            except Exception:
                continue
    result = {}
    for item in r:
        sym = item["symbol"].replace("USDT", "")
        result[sym] = float(item["price"])
    return result


def fetch_momentum_returns(symbols, lookback_days=90):
    """各銘柄の過去 lookback_days 日のリターンを計算 (1回のAPI呼出で軽く)"""
    returns = {}
    for s in symbols:
        try:
            url = (f"https://api.binance.com/api/v3/klines?symbol={s}USDT"
                   f"&interval=1d&limit={lookback_days + 1}")
            klines = http_get_json(url, timeout=15)
            if len(klines) < 2:
                continue
            start_close = float(klines[0][4])
            end_close = float(klines[-1][4])
            if start_close <= 0:
                continue
            ret = (end_close - start_close) / start_close * 100
            returns[s] = {
                "return_pct": round(ret, 2),
                "start_price": start_close,
                "end_price": end_close,
                "klines_count": len(klines),
            }
            time.sleep(0.05)  # レート制限回避
        except Exception as e:
            returns[s] = {"error": str(e)}
    return returns


def select_top_n_momentum(returns, n=3):
    """リターン上位N銘柄を選定 (シンプル版, v2.0互換)"""
    valid = [(s, r["return_pct"]) for s, r in returns.items() if "return_pct" in r]
    valid.sort(key=lambda x: x[1], reverse=True)
    return valid[:n]


def fetch_daily_returns_series(symbol: str, lookback_days: int) -> list:
    """銘柄の日次リターン系列を取得 (相関計算用)"""
    try:
        url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT"
               f"&interval=1d&limit={lookback_days + 1}")
        klines = http_get_json(url, timeout=15)
        if len(klines) < 10:
            return []
        closes = [float(k[4]) for k in klines]
        returns = [(closes[i+1] - closes[i]) / closes[i] for i in range(len(closes)-1) if closes[i] > 0]
        return returns
    except Exception:
        return []


def calc_correlation(r1: list, r2: list) -> float:
    """2つのリターン系列の相関係数 (Pearson)"""
    n = min(len(r1), len(r2))
    if n < 5:
        return 0.0
    r1 = r1[-n:]; r2 = r2[-n:]
    m1 = sum(r1) / n
    m2 = sum(r2) / n
    cov = sum((r1[i]-m1) * (r2[i]-m2) for i in range(n))
    v1 = sum((x-m1)**2 for x in r1)
    v2 = sum((x-m2)**2 for x in r2)
    if v1 <= 0 or v2 <= 0:
        return 0.0
    return cov / (v1 * v2) ** 0.5


def select_top_n_corr_aware(returns, n=3, candidate_n=10,
                             corr_threshold=0.80, corr_lookback=60):
    """v2.1: 相関考慮 Top N 選定
    候補 Top candidate_n からモメンタム高い順に選び、
    既選銘柄との相関 < corr_threshold なら追加。
    足りなければモメンタム順で補完。
    """
    valid = [(s, r["return_pct"]) for s, r in returns.items() if "return_pct" in r]
    valid.sort(key=lambda x: x[1], reverse=True)
    candidates = valid[:candidate_n]
    if len(candidates) <= n:
        return candidates[:n]

    # 相関計算用の日次リターン系列を取得 (候補のみ)
    log(f"   🔗 相関フィルター: {len(candidates)}候補から Top{n}選定...")
    series_map = {}
    for sym, _ in candidates:
        s = fetch_daily_returns_series(sym, corr_lookback)
        if s:
            series_map[sym] = s

    selected = []
    for sym, ret in candidates:
        if sym not in series_map:
            if len(selected) < n:
                selected.append((sym, ret))
            continue
        ok = True
        for sel_sym, _ in selected:
            if sel_sym in series_map:
                c = calc_correlation(series_map[sym], series_map[sel_sym])
                if abs(c) >= corr_threshold:
                    ok = False
                    break
        if ok:
            selected.append((sym, ret))
            if len(selected) >= n:
                break

    # 補完
    while len(selected) < n and len(selected) < len(candidates):
        for sym, ret in candidates:
            if not any(s == sym for s, _ in selected):
                selected.append((sym, ret))
                break

    return selected[:n]


def compute_momentum_weights(top_list: list) -> list:
    """v2.1: リターン強度で配分重みを計算
    負のリターンも min 0.01 で扱い、強い銘柄により多く配分
    """
    if not top_list:
        return []
    pos_rets = [max(ret, 0.01) for _, ret in top_list]
    total = sum(pos_rets)
    if total <= 0:
        return [1.0 / len(top_list)] * len(top_list)
    return [r / total for r in pos_rets]


def ach_update(state, btc_price_now, btc_ema200):
    """ACH 部分を実市場データで更新
       - 月次リバランス時: Top3モメンタム選定 + 全決済 + 新規購入
       - 通常時: 保有中ポジションの時価評価のみ
       - BTC < EMA200 (bear regime) の時は新規購入しない
    """
    ach = state["ach_part"]
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec='seconds')

    # positions辞書がなければ初期化
    if "positions" not in ach:
        ach["positions"] = {}
    if "last_rebalance" not in ach:
        ach["last_rebalance"] = None

    # 【v2.2 新機能】ACH 即時ベア退避
    # BTC < EMA200 を検知した瞬間に ACH 保有ポジションを全売却して USDT 退避
    # リバランス待たず即時実行することで、弱気相場の DD を大幅削減
    if btc_ema200 and btc_price_now < btc_ema200 and ach.get("positions"):
        log(f"   🚨 v2.2 ACH即時ベア退避: BTC < EMA200 検知 → ACH 全ポジション売却")
        fee = 0.0006; slip = 0.0003
        cur_prices_bear = fetch_all_current_prices(list(ach["positions"].keys()))
        for sym, pos in list(ach["positions"].items()):
            cur_price = cur_prices_bear.get(sym, pos.get("entry_price", 0))
            sell_price = cur_price * (1 - slip)
            proceeds = pos["qty"] * sell_price * (1 - fee)
            pnl = proceeds - (pos["qty"] * pos["entry_price"] / (1 - fee))
            ach["cash"] += proceeds
            state["trades"].append({
                "ts": now_iso, "part": "ACH", "action": "SELL",
                "symbol": sym, "price": round(cur_price, 6),
                "qty": round(pos["qty"], 6), "value_usd": round(proceeds, 2),
                "pnl_usd": round(pnl, 2),
                "reason": "v2.2_bear_regime_exit",
                "mode": "SIM",
            })
            log(f"🔴 ACH BEAR SELL {sym} @ ${cur_price:.4f} P&L=${pnl:+.2f}")
            if DISCORD_AVAILABLE:
                try:
                    discord_notify.notify_trade("SELL", f"ACH:{sym}(BEAR)", cur_price,
                                                 pos["qty"], proceeds, pnl_usd=pnl)
                except Exception as e:
                    log(f"⚠️ Discord通知失敗: {e}")
        ach["positions"] = {}
        ach["last_regime"] = "bear_exited"
        ach["virtual_equity"] = ach["cash"]

    # ━━ F3_YEAREND: ACH 年末リスク回避 ━━━━━━━━━━━━━━━━━
    # 12/30 〜 翌 1/2 の期間は ACH 全ポジションを強制売却し、
    # 新規エントリー(リバランス)も停止する。
    yearend_now = ENABLE_YEAREND_EXIT and is_yearend_period(now)
    if yearend_now and ach.get("positions"):
        log(f"   🎆 F3_YEAREND: 年末期間検知 → ACH 全ポジション売却")
        fee = 0.0006; slip = 0.0003
        cur_prices_ye = fetch_all_current_prices(list(ach["positions"].keys()))
        for sym, pos in list(ach["positions"].items()):
            cur_price = cur_prices_ye.get(sym, pos.get("entry_price", 0))
            sell_price = cur_price * (1 - slip)
            proceeds = pos["qty"] * sell_price * (1 - fee)
            pnl = proceeds - (pos["qty"] * pos["entry_price"] / (1 - fee))
            ach["cash"] += proceeds
            state["trades"].append({
                "ts": now_iso, "part": "ACH", "action": "SELL",
                "symbol": sym, "price": round(cur_price, 6),
                "qty": round(pos["qty"], 6), "value_usd": round(proceeds, 2),
                "pnl_usd": round(pnl, 2),
                "reason": "F3_YEAREND_exit",
                "mode": "SIM",
            })
            log(f"🎆 ACH YEAREND SELL {sym} @ ${cur_price:.4f} P&L=${pnl:+.2f}")
            if DISCORD_AVAILABLE:
                try:
                    discord_notify.notify_trade("SELL", f"ACH:{sym}(YEAREND)", cur_price,
                                                 pos["qty"], proceeds, pnl_usd=pnl)
                except Exception as e:
                    log(f"⚠️ Discord通知失敗: {e}")
        ach["positions"] = {}
        ach["last_regime"] = "yearend_exited"
        ach["virtual_equity"] = ach["cash"]

    # 年末期間は以後の処理（リバランス・新規購入）を完全スキップして終了
    if yearend_now:
        ach["virtual_equity"] = ach["cash"]
        return

    # リバランス判定
    needs_rebalance = False
    if ach["last_rebalance"] is None:
        needs_rebalance = True
    else:
        last_rb = datetime.fromisoformat(ach["last_rebalance"].replace("Z", "+00:00"))
        if last_rb.tzinfo is None:
            last_rb = last_rb.replace(tzinfo=timezone.utc)
        days_since = (now - last_rb).total_seconds() / 86400
        if days_since >= ACH_REBALANCE_DAYS:
            needs_rebalance = True

    # ヒートマップが未生成 or 古い (24時間以上) なら再取得だけする
    hm_refresh = False
    if not state.get("momentum_heatmap"):
        hm_refresh = True
    else:
        hm_updated = state.get("momentum_heatmap_updated")
        if hm_updated:
            last_hm = datetime.fromisoformat(hm_updated.replace("Z", "+00:00"))
            if last_hm.tzinfo is None:
                last_hm = last_hm.replace(tzinfo=timezone.utc)
            if (now - last_hm).total_seconds() >= 86400:  # 24時間経過
                hm_refresh = True

    # リバランスせずとも、ヒートマップ独立更新
    if hm_refresh and not needs_rebalance:
        log(f"🔍 [ヒートマップ独立更新] {len(ACH_UNIVERSE)}銘柄モメンタム取得中...")
        try:
            returns = fetch_momentum_returns(ACH_UNIVERSE, ACH_LOOKBACK_DAYS)
            hm_data = [
                {"symbol": s, "return_pct": r["return_pct"]}
                for s, r in returns.items() if "return_pct" in r
            ]
            hm_data.sort(key=lambda x: x["return_pct"], reverse=True)
            state["momentum_heatmap"] = hm_data
            state["momentum_heatmap_updated"] = now_iso
            log(f"   ✅ ヒートマップ更新完了 ({len(hm_data)}銘柄)")
            hm_refresh = False  # 既に取得したので以降スキップ
        except Exception as e:
            log(f"   ⚠️ ヒートマップ取得失敗: {e}")

    # まず保有中ポジションを時価評価 (Tickごとに更新)
    current_prices = {}
    if ach.get("positions"):
        try:
            current_prices = fetch_all_current_prices(list(ach["positions"].keys()))
        except Exception as e:
            log(f"⚠️ ACH価格取得失敗: {e}")

    mtm_total = ach.get("cash", 0)
    for sym, pos in ach["positions"].items():
        cur_price = current_prices.get(sym, pos.get("entry_price", 0))
        pos["current_price"] = cur_price
        pos["current_value"] = pos["qty"] * cur_price
        pos["unrealized_pnl"] = (cur_price - pos["entry_price"]) * pos["qty"]
        mtm_total += pos["current_value"]
    ach["virtual_equity"] = round(mtm_total, 4)

    # リバランス実行
    if needs_rebalance:
        log(f"🔄 ACH 月次リバランス実行")

        # 全決済
        if ach.get("positions"):
            fee = 0.0006; slip = 0.0003
            for sym, pos in list(ach["positions"].items()):
                cur_price = current_prices.get(sym, pos["entry_price"])
                sell_price = cur_price * (1 - slip)
                proceeds = pos["qty"] * sell_price * (1 - fee)
                pnl = proceeds - (pos["entry_price"] * pos["qty"])
                ach["cash"] = ach.get("cash", 0) + proceeds
                state["trades"].append({
                    "ts": now_iso, "part": "ACH", "action": "SELL",
                    "symbol": sym, "price": round(cur_price, 6),
                    "qty": round(pos["qty"], 6),
                    "value_usd": round(proceeds, 2),
                    "pnl_usd": round(pnl, 2),
                    "reason": "rebalance",
                    "mode": "SIM",
                })
                log(f"🔴 ACH SELL {sym} @ ${cur_price:.4f} qty={pos['qty']:.6f} P&L=${pnl:+.2f}")
                if DISCORD_AVAILABLE:
                    try:
                        discord_notify.notify_trade("SELL", f"ACH:{sym}", cur_price,
                                                     pos["qty"], proceeds, pnl_usd=pnl)
                    except Exception as e:
                        log(f"⚠️ Discord通知失敗: {e}")
            ach["positions"] = {}

        # BTCレジーム判定: bear regime (BTC < EMA200) は新規購入スキップ = 現金待機
        if btc_price_now < btc_ema200:
            log(f"   ⚠️ BTC < EMA200 (Bear regime) → ACH 新規購入スキップ、現金待機")
            # Bear時でもヒートマップは生成 (観察用)
            if hm_refresh:
                log(f"   🔍 ヒートマップ用に{len(ACH_UNIVERSE)}銘柄モメンタム取得中...")
                returns = fetch_momentum_returns(ACH_UNIVERSE, ACH_LOOKBACK_DAYS)
                hm_data = [
                    {"symbol": s, "return_pct": r["return_pct"]}
                    for s, r in returns.items() if "return_pct" in r
                ]
                hm_data.sort(key=lambda x: x["return_pct"], reverse=True)
                state["momentum_heatmap"] = hm_data
                state["momentum_heatmap_updated"] = now_iso
                log(f"   ✅ ヒートマップ生成完了 ({len(hm_data)}銘柄)")
            ach["last_rebalance"] = now_iso
            ach["last_regime"] = "bear_skip"
            ach["virtual_equity"] = ach["cash"]
            return

        # モメンタム上位3銘柄を選定
        log(f"   🔍 {len(ACH_UNIVERSE)}銘柄のモメンタム取得中...")
        returns = fetch_momentum_returns(ACH_UNIVERSE, ACH_LOOKBACK_DAYS)
        # ヒートマップ用に全銘柄のリターンをstateに保存
        hm_data = [
            {"symbol": s, "return_pct": r["return_pct"]}
            for s, r in returns.items() if "return_pct" in r
        ]
        hm_data.sort(key=lambda x: x["return_pct"], reverse=True)
        state["momentum_heatmap"] = hm_data
        state["momentum_heatmap_updated"] = now_iso
        # v2.1: 相関考慮 Top N 選定 (Top10候補 → 相関<0.80 の Top3)
        top = select_top_n_corr_aware(returns, n=ACH_TOP_N,
                                        candidate_n=ACH_CANDIDATE_N,
                                        corr_threshold=ACH_CORR_THRESHOLD,
                                        corr_lookback=ACH_CORR_LOOKBACK_DAYS)
        if not top:
            log(f"   ⚠️ 候補銘柄なし、現金待機")
            ach["last_rebalance"] = now_iso
            return

        ach["last_top3"] = [{"symbol": s, "return_pct": r} for s, r in top]
        log(f"   📈 v2.1 Top{ACH_TOP_N} (相関フィルター後): " + ", ".join([f"{s} (+{r:.1f}%)" for s, r in top]))

        # v2.1: モメンタム加重配分 (equal or momentum)
        weights = compute_momentum_weights(top) if ACH_WEIGHT_METHOD == "momentum" else [1.0/len(top)] * len(top)
        log(f"   ⚖️ 配分 ({ACH_WEIGHT_METHOD}): " + ", ".join([f"{s}:{w*100:.1f}%" for (s, _), w in zip(top, weights)]))

        fee = 0.0006; slip = 0.0003
        current_prices_new = fetch_all_current_prices([s for s, _ in top])
        for (sym, ret), w in zip(top, weights):
            price = current_prices_new.get(sym)
            if not price:
                continue
            buy_price = price * (1 + slip)
            cash_per_pos = ach["cash"] * w
            qty = cash_per_pos / buy_price * (1 - fee)
            if qty <= 0:
                continue
            ach["positions"][sym] = {
                "qty": qty,
                "entry_price": buy_price,
                "entry_ts": now_iso,
                "current_price": price,
                "current_value": qty * price,
                "unrealized_pnl": 0,
                "momentum_at_entry": ret,
            }
            state["trades"].append({
                "ts": now_iso, "part": "ACH", "action": "BUY",
                "symbol": sym, "price": round(buy_price, 6),
                "qty": round(qty, 6),
                "value_usd": round(qty * buy_price, 2),
                "reason": f"momentum_top3 (+{ret:.1f}%)",
                "mode": "SIM",
            })
            log(f"🟢 ACH BUY {sym} @ ${buy_price:.4f} qty={qty:.6f}")
            if DISCORD_AVAILABLE:
                try:
                    discord_notify.notify_trade("BUY", f"ACH:{sym}", buy_price,
                                                 qty, qty * buy_price)
                except Exception as e:
                    log(f"⚠️ Discord通知失敗: {e}")
        ach["cash"] = 0
        ach["last_rebalance"] = now_iso
        ach["last_regime"] = "bull_invested"

        # MTM 再計算
        total = ach["cash"]
        for pos in ach["positions"].values():
            total += pos["current_value"]
        ach["virtual_equity"] = round(total, 4)


def fresh_state():
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return {
        "version": "2.2",
        "version_name": "気持ちマックス v2.2",
        "mode": "SIM",
        "started_at": now,
        "initial_capital": INITIAL,
        "last_update": now,

        "btc_part": {
            "cash": INITIAL * BTC_WEIGHT,
            "btc_qty": 0.0,
            "position": False,
            "last_btc_price": 0.0,
            "last_ema200": 0.0,
            "last_signal": "HOLD",
            "entry_price": 0.0,
            "entry_ts": None,
        },
        "ach_part": {
            "cash": INITIAL * ACH_WEIGHT,
            "virtual_equity": INITIAL * ACH_WEIGHT,
            "last_tick": now,
            "positions": {},          # {sym: {qty, entry_price, entry_ts, current_price, current_value, unrealized_pnl, momentum_at_entry}}
            "last_rebalance": None,   # 次回リバランス判定用
            "last_regime": None,      # "bear_skip" or "bull_invested"
            "last_top3": [],          # 最終リバランス時の選定銘柄
            "strategy": "momentum_top3",
            "lookback_days": ACH_LOOKBACK_DAYS,
            "rebalance_days": ACH_REBALANCE_DAYS,
            "note": "実市場連動: Top3モメンタム戦略 (過去90日リターン上位3銘柄保有、月次リバランス)",
        },
        "usdt_part": {
            "cash": INITIAL * USDT_WEIGHT,
            "last_tick": now,
        },

        "total_equity": INITIAL,
        "peak_equity": INITIAL,
        "max_dd_observed": 0.0,
        "ticks_processed": 0,

        "trades": [],
        "equity_history": [
            {"ts": now, "total": INITIAL,
             "btc": INITIAL * BTC_WEIGHT,
             "ach": INITIAL * ACH_WEIGHT,
             "usdt": INITIAL * USDT_WEIGHT}
        ],
        "btc_price_history": [],
    }


def load_state():
    if not STATE_PATH.exists():
        return fresh_state()
    try:
        state = json.loads(STATE_PATH.read_text())
        # マイグレーション: ach_part に新フィールドを追加 (旧版の理論値複利→実市場連動)
        ach = state.get("ach_part", {})
        migrated = False
        if "positions" not in ach:
            ach["positions"] = {}
            migrated = True
        if "last_rebalance" not in ach:
            ach["last_rebalance"] = None  # None だと次のtickでリバランス走る
            migrated = True
        if ach.get("strategy") != "momentum_top3":
            ach["strategy"] = "momentum_top3"
            ach["lookback_days"] = ACH_LOOKBACK_DAYS
            ach["rebalance_days"] = ACH_REBALANCE_DAYS
            ach["last_regime"] = None
            ach["last_top3"] = []
            ach["note"] = "実市場連動: Top3モメンタム戦略"
            migrated = True
        # v2 マイグレーション: パラメータが旧版なら更新し、メタ情報を v2 化
        if ach.get("lookback_days") != ACH_LOOKBACK_DAYS:
            ach["lookback_days"] = ACH_LOOKBACK_DAYS
            migrated = True
        if ach.get("rebalance_days") != ACH_REBALANCE_DAYS:
            ach["rebalance_days"] = ACH_REBALANCE_DAYS
            ach["last_rebalance"] = None  # 次の tick で強制リバランス
            migrated = True
        if state.get("version") not in ("2.0", "2.1", "2.2"):
            state["version"] = "2.2"
            state["version_name"] = "気持ちマックス v2.2"
            migrated = True
        if state.get("version") in ("2.0", "2.1"):
            # v2.0/v2.1 → v2.2 アップグレード (ACH即時ベア退避機能追加)
            state["version"] = "2.2"
            state["version_name"] = "気持ちマックス v2.2"
            # 旧 40/40/20 配分のキャッシュを新 35/35/30 に再配分
            total_cash = state.get("btc_part", {}).get("cash", 0) + \
                         state.get("ach_part", {}).get("cash", 0) + \
                         state.get("usdt_part", {}).get("cash", 0)
            if total_cash > 0 and not state.get("btc_part", {}).get("position"):
                # BTC 保有してないときのみキャッシュ再配分
                state["btc_part"]["cash"] = total_cash * BTC_WEIGHT
                state["ach_part"]["cash"] = total_cash * ACH_WEIGHT
                state["usdt_part"]["cash"] = total_cash * USDT_WEIGHT
                log(f"   💰 v2.2 cash 再配分: BTC {BTC_WEIGHT*100:.0f}% / ACH {ACH_WEIGHT*100:.0f}% / USDT {USDT_WEIGHT*100:.0f}%")
            migrated = True
        # ach_config を常に最新パラメータで更新 (v2.1 新フィールド含む)
        expected_cfg = {
            "top_n": ACH_TOP_N,
            "lookback_days": ACH_LOOKBACK_DAYS,
            "rebalance_days": ACH_REBALANCE_DAYS,
            "universe_size": len(ACH_UNIVERSE),
            "candidate_n": ACH_CANDIDATE_N,
            "corr_threshold": ACH_CORR_THRESHOLD,
            "corr_lookback": ACH_CORR_LOOKBACK_DAYS,
            "weight_method": ACH_WEIGHT_METHOD,
        }
        if state.get("ach_config") != expected_cfg:
            state["ach_config"] = expected_cfg
            migrated = True
        # 比率 (weights) も記録
        expected_weights = {"btc": BTC_WEIGHT, "ach": ACH_WEIGHT, "usdt": USDT_WEIGHT}
        if state.get("portfolio_weights") != expected_weights:
            state["portfolio_weights"] = expected_weights
            migrated = True
        if migrated:
            log(f"🔄 state.json マイグレーション: v2.2 (Top{ACH_TOP_N}/LB{ACH_LOOKBACK_DAYS}/週次/"
                f"BTC{BTC_WEIGHT:.0%}/ACH{ACH_WEIGHT:.0%}/USDT{USDT_WEIGHT:.0%}/Corr{ACH_CORR_THRESHOLD}/"
                f"{ACH_WEIGHT_METHOD}加重) に移行")
        return state
    except Exception as e:
        log(f"⚠️ state読込失敗: {e} → 初期化")
        return fresh_state()


def save_state(state):
    # 履歴を制限してファイルサイズを抑える
    state["trades"] = state["trades"][-MAX_TRADE_HISTORY:]
    state["equity_history"] = state["equity_history"][-MAX_EQUITY_HISTORY:]
    state["btc_price_history"] = state["btc_price_history"][-MAX_EQUITY_HISTORY:]

    # アトミック書き込み
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str, ensure_ascii=False))
    tmp.replace(STATE_PATH)


def process_tick(state, btc_data):
    """1tick分の処理: 価格取得→各部の更新"""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec='seconds')

    btc_price = btc_data["current_price"]
    ema200 = btc_data["ema200"]

    # ━━ F3_YEAREND: 年末リスク回避 ━━━━━━━━━━━━━━━━━
    # 12/30 〜 翌 1/2 の期間は BTC/ACH を全決済し、新規エントリーを停止。
    # USDT 部分は通常通り金利計算を継続する。
    yearend = ENABLE_YEAREND_EXIT and is_yearend_period(now)

    # ━━ BTCマイルド部分 ━━━━━━━━━━━━━━━━━
    btc = state["btc_part"]
    btc["last_btc_price"] = btc_price
    btc["last_ema200"] = ema200

    signal_action = None
    if yearend and btc["position"]:
        # F3_YEAREND: 年末は BTC を強制売却
        fee = 0.0006; slip = 0.0003
        sell_price = btc_price * (1 - slip)
        proceeds = btc["btc_qty"] * sell_price * (1 - fee)
        pnl = proceeds - (btc["entry_price"] * btc["btc_qty"] if btc["entry_price"] else 0)
        qty_was = btc["btc_qty"]
        btc["cash"] = proceeds
        btc["btc_qty"] = 0
        btc["position"] = False
        btc["last_signal"] = "YEAREND-SELL"
        state["trades"].append({
            "ts": now_iso, "part": "BTC", "action": "SELL",
            "price": btc_price, "qty": round(qty_was, 6),
            "value_usd": round(proceeds, 2),
            "pnl_usd": round(pnl, 2),
            "ema200": ema200,
            "reason": "F3_YEAREND_exit",
            "mode": "SIM",
        })
        signal_action = f"🎆 BTC YEAREND SELL @ ${btc_price:,.2f} (年末リスク回避, P&L: ${pnl:+,.2f})"
        log(signal_action)
        if DISCORD_AVAILABLE:
            try:
                discord_notify.notify_trade("SELL", "BTC(YEAREND)", btc_price,
                                             qty_was, proceeds, pnl_usd=pnl, ema200=ema200)
            except Exception as e:
                log(f"⚠️ Discord通知失敗: {e}")
    elif yearend and not btc["position"]:
        # F3_YEAREND: 年末は新規 BTC 購入を見送り
        btc["last_signal"] = "YEAREND-SKIP"
    elif btc_price > ema200 and not btc["position"]:
        # BUY
        fee = 0.0006; slip = 0.0003
        buy_price = btc_price * (1 + slip)
        btc_qty = btc["cash"] / buy_price * (1 - fee)
        btc["btc_qty"] = btc_qty
        btc["cash"] = 0
        btc["position"] = True
        btc["last_signal"] = "BUY"
        btc["entry_price"] = btc_price
        btc["entry_ts"] = now_iso
        state["trades"].append({
            "ts": now_iso, "part": "BTC", "action": "BUY",
            "price": btc_price, "qty": round(btc_qty, 6),
            "value_usd": round(btc_qty * btc_price, 2),
            "ema200": ema200, "mode": "SIM",
        })
        signal_action = f"🟢 BTC BUY @ ${btc_price:,.2f} (EMA200=${ema200:,.2f})"
        log(signal_action)
        # Discord通知
        if DISCORD_AVAILABLE:
            try:
                discord_notify.notify_trade("BUY", "BTC", btc_price,
                                             btc_qty, btc_qty * btc_price, ema200=ema200)
            except Exception as e:
                log(f"⚠️ Discord通知失敗: {e}")
    elif btc_price < ema200 and btc["position"]:
        # SELL
        fee = 0.0006; slip = 0.0003
        sell_price = btc_price * (1 - slip)
        proceeds = btc["btc_qty"] * sell_price * (1 - fee)
        pnl = proceeds - (btc["entry_price"] * btc["btc_qty"] if btc["entry_price"] else 0)
        qty_was = btc["btc_qty"]
        btc["cash"] = proceeds
        btc["btc_qty"] = 0
        btc["position"] = False
        btc["last_signal"] = "SELL"
        state["trades"].append({
            "ts": now_iso, "part": "BTC", "action": "SELL",
            "price": btc_price, "qty": round(qty_was, 6),
            "value_usd": round(proceeds, 2),
            "pnl_usd": round(pnl, 2),
            "ema200": ema200, "mode": "SIM",
        })
        signal_action = f"🔴 BTC SELL @ ${btc_price:,.2f} (EMA200=${ema200:,.2f}, P&L: ${pnl:+,.2f})"
        log(signal_action)
        # Discord通知
        if DISCORD_AVAILABLE:
            try:
                discord_notify.notify_trade("SELL", "BTC", btc_price,
                                             qty_was, proceeds, pnl_usd=pnl, ema200=ema200)
            except Exception as e:
                log(f"⚠️ Discord通知失敗: {e}")
    else:
        btc["last_signal"] = "HOLD-IN" if btc["position"] else "HOLD-OUT"

    # ━━ ACH部分 (Top3モメンタム 実市場連動) ━━━━━━━━━━━━━━━━━
    try:
        ach_update(state, btc_price, ema200)
        state["ach_part"]["last_tick"] = now_iso
    except Exception as e:
        log(f"⚠️ ACH更新失敗 (既存保有継続): {e}")

    # ━━ USDT部分 (年3%複利) ━━━━━━━━━━━━━━━━━
    usdt = state["usdt_part"]
    last_usdt_tick = datetime.fromisoformat(usdt["last_tick"].replace("Z", "+00:00"))
    if last_usdt_tick.tzinfo is None:
        last_usdt_tick = last_usdt_tick.replace(tzinfo=timezone.utc)
    usdt_days = (now - last_usdt_tick).total_seconds() / 86400
    if usdt_days > 0:
        daily_rate = (1 + USDT_ANNUAL_RATE) ** (1/365) - 1
        usdt["cash"] *= (1 + daily_rate) ** usdt_days
        usdt["last_tick"] = now_iso

    # ━━ 総資産計算 ━━━━━━━━━━━━━━━━━
    btc_value = btc["cash"] + btc["btc_qty"] * btc_price
    ach = state["ach_part"]
    ach_value = ach.get("virtual_equity", ach.get("cash", 0))
    usdt_value = usdt["cash"]
    total = btc_value + ach_value + usdt_value

    state["total_equity"] = round(total, 2)
    state["peak_equity"] = round(max(state["peak_equity"], total), 2)
    state["max_dd_observed"] = round(
        max(state["max_dd_observed"],
            (state["peak_equity"] - total) / state["peak_equity"] * 100), 2)
    state["ticks_processed"] += 1
    state["last_update"] = now_iso

    # 履歴
    state["equity_history"].append({
        "ts": now_iso,
        "total": round(total, 2),
        "btc": round(btc_value, 2),
        "ach": round(ach_value, 2),
        "usdt": round(usdt_value, 2),
    })
    state["btc_price_history"].append({
        "ts": now_iso,
        "price": btc_price,
        "ema200": ema200,
    })

    # 24h変化情報保持
    state["btc_24h_change_pct"] = btc_data["change_24h_pct"]
    state["btc_24h_volume_usdt"] = btc_data["volume_24h_usdt"]

    log(f"📊 総資産 ${total:,.2f} | BTC:${btc_value:,.0f} ACH:${ach_value:,.0f} "
        f"USDT:${usdt_value:,.0f} | BTC=${btc_price:,.2f} (24h: {btc_data['change_24h_pct']:+.2f}%)")

    # Discord通知: DD警告 & 日次サマリー
    if DISCORD_AVAILABLE:
        try:
            dd_pct = state["max_dd_observed"]
            discord_notify.notify_dd_alert(dd_pct, total, state["peak_equity"], INITIAL)
            # 日次サマリー (JST 21:00頃に1回)
            jst_hour = (now.hour + 9) % 24
            if jst_hour == 21:
                pnl = total - INITIAL
                pnl_pct = (total / INITIAL - 1) * 100
                discord_notify.notify_daily_summary(
                    total, INITIAL, pnl, pnl_pct, dd_pct,
                    btc_price, ema200, btc["last_signal"],
                    btc_value, ach_value, usdt_value, len(state["trades"])
                )
        except Exception as e:
            log(f"⚠️ Discord通知処理でエラー: {e}")


def run_once():
    """1回実行"""
    try:
        log("🔄 Binance API から最新価格取得中...")
        btc_data = fetch_btc_price_and_ema200()
        log(f"   BTC: ${btc_data['current_price']:,.2f} | EMA200: ${btc_data['ema200']:,.2f} "
            f"| 24h: {btc_data['change_24h_pct']:+.2f}%")

        state = load_state()
        process_tick(state, btc_data)
        save_state(state)
        return True
    except urllib.error.URLError as e:
        log(f"⚠️ ネットワークエラー: {e}")
        return False
    except Exception as e:
        log(f"⚠️ エラー: {e}")
        import traceback
        log(traceback.format_exc(), also_print=False)
        return False


def run_loop():
    """3層タイマー永続ループ (毎秒tick / 60秒snapshot / 5分EMA再計算)"""
    log("=" * 60)
    log("🚀 気持ちマックス v2.2 デモトレードランナー 起動 (WebSocket版)")
    log(f"   初期資金: ${INITIAL:,.0f}")
    log(f"   構成: BTC {BTC_WEIGHT*100:.0f}% + ACH {ACH_WEIGHT*100:.0f}% + USDT {USDT_WEIGHT*100:.0f}%")
    log(f"   ACH設定: Top{ACH_TOP_N} / LB{ACH_LOOKBACK_DAYS}日 / リバランス{ACH_REBALANCE_DAYS}日 / {len(ACH_UNIVERSE)}銘柄")
    log(f"   v2.2新機能: ACH即時ベア退避 + Top{ACH_CANDIDATE_N}候補→相関<{ACH_CORR_THRESHOLD}→{ACH_WEIGHT_METHOD}加重")
    log(f"   Tick {TICK_INTERVAL}秒 / Snapshot {SNAPSHOT_INTERVAL}秒 / EMA更新 {EMA_REFRESH_INTERVAL}秒")
    live_mode_startup = get_live_mode() if LIVE_TRADER_AVAILABLE else "sim"
    if live_mode_startup == "live":
        log(f"   🔴 取引モード: LIVE (本番発注が有効です！ 実資金が動きます)")
    elif live_mode_startup == "dry_run":
        log(f"   🟡 取引モード: DRY_RUN (APIキーあり・LIVE_ENABLED未設定)")
    else:
        log(f"   🟢 取引モード: SIM (仮想資金のみ・安全)")
    if DISCORD_AVAILABLE:
        try:
            cfg = discord_notify.load_config()
            if cfg.get("enabled"):
                discord_notify.notify_startup(INITIAL)
                log("   Discord通知: 有効")
            else:
                log("   Discord通知: 未設定 (python3 discord_notify.py setup で設定)")
        except Exception as e:
            log(f"   Discord通知: エラー {e}")
    log("=" * 60)

    # WebSocket モード使えない時はREST fallback
    if not WEBSOCKET_AVAILABLE or BTCTickerStream is None:
        log("⚠️ websocket-client未インストール → REST 5分間隔モードで稼働")
        while True:
            run_once()
            try:
                time.sleep(LOOP_INTERVAL)
            except KeyboardInterrupt:
                log("⚠️ 中断されました")
                break
        return

    # WebSocket モード
    stream = BTCTickerStream(log_fn=lambda m: log(m, also_print=False))
    stream.start()
    log("🔌 Binance WebSocket 接続開始")

    # 初回REST: EMA200/momentum 取得して state 初期化
    try:
        btc_data = fetch_btc_price_and_ema200()
        log(f"   初回 BTC: ${btc_data['current_price']:,.2f} | EMA200: ${btc_data['ema200']:,.2f}")
        state = load_state()
        process_tick(state, btc_data)
        save_state(state)
    except Exception as e:
        log(f"⚠️ 初期化エラー: {e}")
        state = load_state()

    # キャッシュしたEMA200 と 24h統計 (WSから上書き)
    cached_ema200 = state.get("btc_part", {}).get("last_ema200", 0)
    last_snapshot = time.time()
    last_ema_refresh = time.time()
    tick_count = 0

    while True:
        try:
            now = time.time()

            # 【毎秒】tick: WS からBTC価格取得 → 総資産再計算
            ws_data = stream.get()
            if ws_data["price"] and stream.is_fresh(max_age_seconds=30):
                btc_price = ws_data["price"]
                # state の BTC 情報を更新 (EMA200はキャッシュ値)
                btc = state["btc_part"]
                prev_price = btc.get("last_btc_price", 0)
                btc["last_btc_price"] = btc_price
                if cached_ema200:
                    btc["last_ema200"] = cached_ema200
                state["btc_24h_change_pct"] = ws_data.get("change_24h_pct", 0)
                state["btc_24h_volume_usdt"] = ws_data.get("volume_24h_usdt", 0)

                # BTCシグナル判定 (HOLD系のみ、BUY/SELL発動はsnapshot時で安全に)
                if btc_price > cached_ema200 and btc.get("position"):
                    btc["last_signal"] = "HOLD-IN"
                elif btc_price < cached_ema200 and not btc.get("position"):
                    btc["last_signal"] = "HOLD-OUT"
                elif btc_price > cached_ema200 and not btc.get("position"):
                    btc["last_signal"] = "READY-BUY"   # 次snapshotでBUY
                elif btc_price < cached_ema200 and btc.get("position"):
                    btc["last_signal"] = "READY-SELL"  # 次snapshotでSELL

                # 総資産リアルタイム再計算
                btc_value = btc["cash"] + btc.get("btc_qty", 0) * btc_price
                ach_value = state["ach_part"].get("virtual_equity",
                                                   state["ach_part"].get("cash", 0))
                usdt_value = state["usdt_part"]["cash"]
                total = btc_value + ach_value + usdt_value
                state["total_equity"] = round(total, 2)
                state["peak_equity"] = round(max(state.get("peak_equity", INITIAL), total), 2)
                if state["peak_equity"] > 0:
                    dd_now = (state["peak_equity"] - total) / state["peak_equity"] * 100
                    state["max_dd_observed"] = round(max(state.get("max_dd_observed", 0), dd_now), 2)
                state["last_update"] = datetime.now(timezone.utc).isoformat(timespec='seconds')
                state["ws_connected"] = ws_data.get("connected", False)
                state["ws_age_sec"] = round(now - ws_data["ts"], 1) if ws_data["ts"] else None
                state["trading_mode"] = get_live_mode() if LIVE_TRADER_AVAILABLE else "sim"

                tick_count += 1
                # 詳細ログは60秒ごとのみ
                if tick_count % 60 == 1:
                    log(f"📊 [tick {tick_count}] ${total:,.2f} | BTC=${btc_price:,.2f} "
                        f"(WS fresh {state['ws_age_sec']}s) 24h: {state['btc_24h_change_pct']:+.2f}%")

            # 【60秒】snapshot: equity_history追加 + state.json永続化
            if now - last_snapshot >= SNAPSHOT_INTERVAL:
                last_snapshot = now
                btc_value = state["btc_part"]["cash"] + state["btc_part"].get("btc_qty", 0) * state["btc_part"]["last_btc_price"]
                ach_value = state["ach_part"].get("virtual_equity", state["ach_part"].get("cash", 0))
                usdt_value = state["usdt_part"]["cash"]
                now_iso = datetime.now(timezone.utc).isoformat(timespec='seconds')
                state.setdefault("equity_history", []).append({
                    "ts": now_iso,
                    "total": round(state["total_equity"], 2),
                    "btc": round(btc_value, 2),
                    "ach": round(ach_value, 2),
                    "usdt": round(usdt_value, 2),
                })
                state.setdefault("btc_price_history", []).append({
                    "ts": now_iso,
                    "price": state["btc_part"]["last_btc_price"],
                    "ema200": cached_ema200,
                })

                # 【BUY/SELL シグナル発動】 snapshot時にまとめて発動 (安全)
                sig = state["btc_part"].get("last_signal", "")
                if sig == "READY-BUY":
                    fee = 0.0006; slip = 0.0003
                    btc = state["btc_part"]
                    live_mode = get_live_mode() if LIVE_TRADER_AVAILABLE else "sim"
                    buy_price = btc["last_btc_price"] * (1 + slip)
                    # SIM計算 (どのモードでも同じ)
                    btc_qty = btc["cash"] / buy_price * (1 - fee)
                    # LIVE実発注
                    live_result = None
                    if live_mode == "live":
                        try:
                            trader = LiveTrader()
                            log(f"🔴 [LIVE] 実発注開始 BTC BUY ${btc['cash']:.2f}")
                            live_result = trader.market_buy("BTCUSDT", quote_usd=round(btc["cash"], 2))
                            # 実発注された数量を state に反映
                            if live_result and "executedQty" in live_result:
                                btc_qty = float(live_result["executedQty"])
                            log(f"✅ [LIVE] 実発注成功: qty={btc_qty:.6f}")
                        except Exception as e:
                            log(f"❌ [LIVE] 発注失敗、SIMフォールバック: {e}")
                            live_mode = "sim"
                    elif live_mode == "dry_run":
                        log(f"🟡 [DRY_RUN] 発注スキップ (LIVE_ENABLED=1で有効化可)")
                    btc["btc_qty"] = btc_qty
                    btc["cash"] = 0
                    btc["position"] = True
                    btc["last_signal"] = "BUY"
                    btc["entry_price"] = btc["last_btc_price"]
                    btc["entry_ts"] = now_iso
                    state["trades"].append({
                        "ts": now_iso, "part": "BTC", "action": "BUY",
                        "price": btc["last_btc_price"], "qty": round(btc_qty, 6),
                        "value_usd": round(btc_qty * btc["last_btc_price"], 2),
                        "ema200": cached_ema200,
                        "mode": live_mode.upper(),
                        "live_order_id": live_result.get("orderId") if live_result else None,
                    })
                    log(f"🟢 BTC BUY @ ${btc['last_btc_price']:,.2f} qty={btc_qty:.6f} [{live_mode.upper()}]")
                    if DISCORD_AVAILABLE:
                        try:
                            discord_notify.notify_trade("BUY", "BTC", btc["last_btc_price"],
                                                         btc_qty, btc_qty * btc["last_btc_price"],
                                                         ema200=cached_ema200)
                        except Exception: pass
                elif sig == "READY-SELL":
                    fee = 0.0006; slip = 0.0003
                    btc = state["btc_part"]
                    live_mode = get_live_mode() if LIVE_TRADER_AVAILABLE else "sim"
                    sell_price = btc["last_btc_price"] * (1 - slip)
                    proceeds = btc["btc_qty"] * sell_price * (1 - fee)
                    pnl = proceeds - (btc.get("entry_price", 0) * btc["btc_qty"])
                    qty_was = btc["btc_qty"]
                    # LIVE実発注
                    live_result = None
                    if live_mode == "live":
                        try:
                            trader = LiveTrader()
                            log(f"🔴 [LIVE] 実発注開始 BTC SELL {qty_was:.6f}")
                            live_result = trader.market_sell_all("BTCUSDT", "BTC")
                            if live_result and "cummulativeQuoteQty" in live_result:
                                proceeds = float(live_result["cummulativeQuoteQty"])
                                pnl = proceeds - (btc.get("entry_price", 0) * qty_was)
                            log(f"✅ [LIVE] SELL成功: proceeds=${proceeds:.2f}")
                        except Exception as e:
                            log(f"❌ [LIVE] 発注失敗、SIMフォールバック: {e}")
                            live_mode = "sim"
                    elif live_mode == "dry_run":
                        log(f"🟡 [DRY_RUN] SELL発注スキップ")
                    btc["cash"] = proceeds
                    btc["btc_qty"] = 0
                    btc["position"] = False
                    btc["last_signal"] = "SELL"
                    state["trades"].append({
                        "ts": now_iso, "part": "BTC", "action": "SELL",
                        "price": btc["last_btc_price"], "qty": round(qty_was, 6),
                        "value_usd": round(proceeds, 2),
                        "pnl_usd": round(pnl, 2),
                        "ema200": cached_ema200,
                        "mode": live_mode.upper(),
                        "live_order_id": live_result.get("orderId") if live_result else None,
                    })
                    log(f"🔴 BTC SELL @ ${btc['last_btc_price']:,.2f} P&L=${pnl:+,.2f} [{live_mode.upper()}]")
                    if DISCORD_AVAILABLE:
                        try:
                            discord_notify.notify_trade("SELL", "BTC", btc["last_btc_price"],
                                                         qty_was, proceeds, pnl_usd=pnl,
                                                         ema200=cached_ema200)
                        except Exception: pass

                # USDT金利加算 (1分経過分の複利)
                usdt = state["usdt_part"]
                daily_rate = (1 + USDT_ANNUAL_RATE) ** (1/365) - 1
                minute_rate = daily_rate / 1440
                usdt["cash"] *= (1 + minute_rate * (SNAPSHOT_INTERVAL / 60))
                usdt["last_tick"] = now_iso

                save_state(state)
                log(f"💾 snapshot #{tick_count//60} 保存 ({len(state['equity_history'])}ポイント)")

            # 【5分】EMA200 再計算 + ACH更新
            if now - last_ema_refresh >= EMA_REFRESH_INTERVAL:
                last_ema_refresh = now
                try:
                    btc_data = fetch_btc_price_and_ema200()
                    cached_ema200 = btc_data["ema200"]
                    log(f"📐 EMA200再計算: ${cached_ema200:,.2f} (BTC: ${btc_data['current_price']:,.2f})")
                    # ACH更新も同じタイミングで
                    try:
                        ach_update(state, btc_data["current_price"], cached_ema200)
                        state["ach_part"]["last_tick"] = datetime.now(timezone.utc).isoformat(timespec='seconds')
                    except Exception as e:
                        log(f"⚠️ ACH更新失敗: {e}")
                    # DD警告通知
                    if DISCORD_AVAILABLE:
                        try:
                            discord_notify.notify_dd_alert(
                                state["max_dd_observed"], state["total_equity"],
                                state["peak_equity"], INITIAL)
                        except Exception: pass
                    save_state(state)
                except Exception as e:
                    log(f"⚠️ EMA200再計算失敗: {e}")

            # 毎秒スリープ
            time.sleep(TICK_INTERVAL)

        except KeyboardInterrupt:
            log("⚠️ 中断されました")
            save_state(state)
            stream.stop()
            break
        except Exception as e:
            log(f"⚠️ tickエラー: {e}")
            time.sleep(TICK_INTERVAL)


if __name__ == "__main__":
    if "--reset" in sys.argv:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
            log("🗑️ 状態リセット完了")
    if "--once" in sys.argv:
        run_once()
    else:
        run_loop()
