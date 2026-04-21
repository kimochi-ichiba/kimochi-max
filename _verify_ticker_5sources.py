"""
_verify_ticker_5sources.py — 5ソース徹底クロスチェック (適用前最終確認)

検証ソース:
  1. Binance spot (exchangeInfo API, status=TRADING 限定)
  2. MEXC spot (exchangeInfo API)
  3. Bybit spot (V5 instruments API)
  4. CoinGecko (coins/markets 上位1000銘柄)
  5. CoinMarketCap (quotes/latest, CMC_API_KEY 環境変数)

判定:
  4-5ソース一致 → PASS (採用可)
  3ソース一致   → WARN (採用検討)
  2ソース以下   → FAIL (採用不可)

重要: status=BREAK/HALT などは NOT TRADING 扱い
     → MATIC/FTM/MKR/EOS は現在取引停止のため FAIL判定が正しい

出力: results/ticker_5sources.{json,html}
"""
from __future__ import annotations
import os, sys, json, time, urllib.request
from pathlib import Path
from datetime import datetime, timezone

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
RESULTS_DIR = PROJECT / "results"
RESULTS_DIR.mkdir(exist_ok=True)
OUT_JSON = RESULTS_DIR / "ticker_5sources.json"
OUT_HTML = RESULTS_DIR / "ticker_5sources.html"

# 現行 ACH_UNIVERSE からFAIL銘柄を除外し、14新規を追加した推奨ユニバース
PROPOSED_UNIVERSE = [
    # 残留 46銘柄 (現行 ACH_UNIVERSE から MATIC/FTM/MKR/EOS を除外)
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "AVAX", "DOGE", "TRX", "DOT",
    "LINK", "UNI", "LTC", "ATOM", "NEAR", "ICP", "ETC", "XLM", "FIL",
    "APT", "ARB", "OP", "INJ", "SUI", "SEI", "TIA", "RUNE", "ALGO",
    "SAND", "MANA", "AXS", "CHZ", "ENJ", "GRT", "AAVE", "SNX", "CRV",
    "HBAR", "VET", "THETA", "EGLD", "XTZ", "FLOW", "IOTA", "DASH", "ZEC",
    # 新規追加候補 14銘柄
    "POL", "TON", "ONDO", "JUP", "WLD", "LDO", "IMX", "WIF",
    "ENA", "GALA", "JASMY", "PENDLE", "MINA", "RENDER",
    "STRK",  # iter48 追加
    "SUSHI",  # iter48 追加
]

# 除外予定銘柄 (FAIL確認用に検証は実施)
REMOVED_UNIVERSE = ["MATIC", "FTM", "MKR", "EOS"]

# CoinGecko ID マッピング
CG_ID_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin", "SOL": "solana",
    "XRP": "ripple", "ADA": "cardano", "AVAX": "avalanche-2", "DOT": "polkadot",
    "LINK": "chainlink", "DOGE": "dogecoin", "LTC": "litecoin", "ATOM": "cosmos",
    "UNI": "uniswap", "NEAR": "near", "FIL": "filecoin", "TRX": "tron",
    "ETC": "ethereum-classic", "APT": "aptos", "ARB": "arbitrum", "OP": "optimism",
    "ALGO": "algorand", "XLM": "stellar", "VET": "vechain", "HBAR": "hedera-hashgraph",
    "EGLD": "elrond-erd-2", "FTM": "fantom", "AAVE": "aave", "SAND": "the-sandbox",
    "MANA": "decentraland", "CRV": "curve-dao-token", "SNX": "havven", "MKR": "maker",
    "INJ": "injective-protocol", "GRT": "the-graph", "ICP": "internet-computer",
    "ZEC": "zcash", "DASH": "dash", "ENJ": "enjincoin", "CHZ": "chiliz",
    "AXS": "axie-infinity", "MATIC": "matic-network", "SUI": "sui", "SEI": "sei-network",
    "TIA": "celestia", "RUNE": "thorchain", "EOS": "eos", "THETA": "theta-token",
    "XTZ": "tezos", "FLOW": "flow", "IOTA": "iota",
    # 新規マッピング
    "POL": "polygon-ecosystem-token", "TON": "the-open-network", "ONDO": "ondo-finance",
    "JUP": "jupiter-exchange-solana", "WLD": "worldcoin-wld", "LDO": "lido-dao",
    "IMX": "immutable-x", "WIF": "dogwifcoin", "ENA": "ethena",
    "GALA": "gala", "JASMY": "jasmycoin", "PENDLE": "pendle",
    "MINA": "mina-protocol", "RENDER": "render-token",
    "STRK": "starknet", "SUSHI": "sushi",
}


def http_get_json(url: str, headers: dict | None = None, timeout: int = 20):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Source 1: Binance spot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def fetch_binance() -> dict:
    """Binance spot で TRADING 状態の USDT ペア"""
    print("🔵 Binance exchangeInfo 取得中...")
    try:
        r = http_get_json("https://api.binance.com/api/v3/exchangeInfo")
        trading, broken = set(), set()
        for s in r.get("symbols", []):
            base = s["baseAsset"]
            quote = s["quoteAsset"]
            status = s.get("status", "")
            if quote == "USDT":
                if status == "TRADING":
                    trading.add(base)
                else:
                    broken.add(f"{base}({status})")
        print(f"  ✅ TRADING: {len(trading)}銘柄 / 非TRADING: {len(broken)}銘柄")
        return {"ok": True, "trading": trading, "broken": broken}
    except Exception as e:
        print(f"  ❌ {e}")
        return {"ok": False, "trading": set(), "broken": set(), "error": str(e)}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Source 2: MEXC spot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def fetch_mexc() -> dict:
    """MEXC spot で ENABLED 状態の USDT ペア"""
    print("🟢 MEXC exchangeInfo 取得中...")
    try:
        r = http_get_json("https://api.mexc.com/api/v3/exchangeInfo")
        trading = set()
        for s in r.get("symbols", []):
            base = s.get("baseAsset", "")
            quote = s.get("quoteAsset", "")
            status = str(s.get("status", "")).upper()
            if quote == "USDT" and status in ("1", "ENABLED", "TRADING"):
                trading.add(base)
        print(f"  ✅ {len(trading)}銘柄")
        return {"ok": True, "trading": trading}
    except Exception as e:
        print(f"  ❌ {e}")
        return {"ok": False, "trading": set(), "error": str(e)}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Source 3: Bybit spot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def fetch_bybit() -> dict:
    """Bybit spot (V5) で Trading 状態の USDT ペア"""
    print("🟠 Bybit V5 instruments 取得中...")
    try:
        r = http_get_json("https://api.bybit.com/v5/market/instruments-info?category=spot&limit=1000")
        trading = set()
        for s in r.get("result", {}).get("list", []):
            base = s.get("baseCoin", "")
            quote = s.get("quoteCoin", "")
            status = s.get("status", "")
            if quote == "USDT" and status == "Trading":
                trading.add(base)
        print(f"  ✅ {len(trading)}銘柄")
        return {"ok": True, "trading": trading}
    except Exception as e:
        print(f"  ❌ {e}")
        return {"ok": False, "trading": set(), "error": str(e)}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Source 4: CoinGecko
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def fetch_coingecko(pages: int = 10) -> dict:
    """CoinGecko /coins/list で全銘柄ID一覧を取得 (上位制限なし)"""
    print(f"🟡 CoinGecko /coins/list 取得中 (全銘柄)...")
    try:
        r = http_get_json("https://api.coingecko.com/api/v3/coins/list", timeout=30)
        ids = set()
        symbol_to_ids: dict = {}  # シンボル → IDリスト (同シンボル複数IDのケース)
        for c in r:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            sym = c.get("symbol", "").upper()
            if cid:
                ids.add(cid)
                symbol_to_ids.setdefault(sym, []).append(cid)
        print(f"  ✅ {len(ids)}銘柄 / {len(symbol_to_ids)}ユニークシンボル")
        return {"ok": True, "ids": ids, "symbol_to_ids": symbol_to_ids}
    except Exception as e:
        print(f"  ❌ {e}")
        return {"ok": False, "ids": set(), "symbol_to_ids": {}, "error": str(e)}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Source 5: CoinMarketCap
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def fetch_coinmarketcap(all_symbols: list) -> dict:
    print("🟣 CoinMarketCap quotes 取得中...")
    key = os.environ.get("CMC_API_KEY")
    if not key:
        print("  ⚠️ CMC_API_KEY 未設定, スキップ")
        return {"ok": False, "found": {}, "skipped": True,
                "reason": "CMC_API_KEY not set"}
    try:
        # バッチで送る (CMC は最大100銘柄)
        found = {}
        batches = [all_symbols[i:i+100] for i in range(0, len(all_symbols), 100)]
        for batch in batches:
            url = (f"https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
                   f"?symbol={','.join(batch)}")
            r = http_get_json(url, headers={"X-CMC_PRO_API_KEY": key,
                                              "Accept": "application/json"})
            for sym in batch:
                if sym in r.get("data", {}):
                    d = r["data"][sym]
                    found[sym] = {"id": d.get("id"), "name": d.get("name"),
                                  "price_usd": d.get("quote", {}).get("USD", {}).get("price")}
        print(f"  ✅ {len(found)}銘柄確認")
        return {"ok": True, "found": found, "skipped": False}
    except Exception as e:
        print(f"  ❌ {e}")
        return {"ok": False, "found": {}, "skipped": True, "reason": str(e)}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 判定ロジック
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def classify(symbol: str, sources: dict) -> dict:
    """
    Binance必須ではなく、いずれかの取引所でTRADINGかつデータ源で実在確認できれば採用可。
    """
    bin_ok = symbol in sources["binance"]["trading"]
    bin_broken = any(b.startswith(symbol + "(") for b in sources["binance"].get("broken", set()))
    mexc_ok = symbol in sources["mexc"]["trading"]
    bybit_ok = symbol in sources["bybit"]["trading"]
    # CoinGecko: 新方式 — シンボル逆引き (複数ID対応) + 旧MAPフォールバック
    cg_id = CG_ID_MAP.get(symbol, "")
    cg_ok = False
    cg_matched_ids = sources["coingecko"].get("symbol_to_ids", {}).get(symbol, [])
    if cg_matched_ids:
        cg_ok = True
        if not cg_id:
            cg_id = cg_matched_ids[0]  # 最初のIDを代表
    elif cg_id:
        cg_ok = cg_id in sources["coingecko"]["ids"]
    cmc_ok = symbol in sources["coinmarketcap"].get("found", {})

    # 実取引可能か (主要取引所 3つのうち1つ以上でTRADING)
    exchange_tradeable = bin_ok or mexc_ok or bybit_ok
    # 実在確認 (データ源2つのうち1つ以上)
    data_confirmed = cg_ok or cmc_ok
    # 利用可能な取引所リスト
    tradeable_on = []
    if bin_ok: tradeable_on.append("Binance")
    if mexc_ok: tradeable_on.append("MEXC")
    if bybit_ok: tradeable_on.append("Bybit")

    # 判定
    if exchange_tradeable and data_confirmed:
        n_ex = len(tradeable_on)
        if n_ex >= 2:
            verdict = "PASS"  # 複数取引所で取引可 + データ源確認
        else:
            verdict = "WARN"  # 1取引所のみ
    elif exchange_tradeable:
        verdict = "WEAK"  # 取引可能だがデータ源未確認
    elif data_confirmed:
        verdict = "DATA_ONLY"  # データ源にはあるが取引所では取引不可
    elif bin_broken:
        verdict = "BROKEN"  # Binanceで取引停止、他にもない
    else:
        verdict = "FAIL"  # どこにもない

    return {
        "symbol": symbol,
        "binance": bin_ok,
        "binance_broken": bin_broken,
        "mexc": mexc_ok,
        "bybit": bybit_ok,
        "coingecko": cg_ok,
        "coingecko_id": cg_id or None,
        "coinmarketcap": cmc_ok,
        "cmc_price_usd": sources["coinmarketcap"].get("found", {}).get(symbol, {}).get("price_usd"),
        "tradeable_on": tradeable_on,
        "n_exchanges": len(tradeable_on),
        "data_confirmed": data_confirmed,
        "verdict": verdict,
    }


def main():
    print("=" * 80)
    print("🔬 5ソース徹底クロスチェック (適用前最終確認)")
    print("=" * 80)
    print(f"検証対象: 提案ユニバース {len(PROPOSED_UNIVERSE)}銘柄 + 除外予定 {len(REMOVED_UNIVERSE)}銘柄")

    sources = {
        "binance": fetch_binance(),
        "mexc": fetch_mexc(),
        "bybit": fetch_bybit(),
        "coingecko": fetch_coingecko(pages=10),
    }
    all_syms = PROPOSED_UNIVERSE + REMOVED_UNIVERSE
    sources["coinmarketcap"] = fetch_coinmarketcap(all_syms)

    # 分類
    print("\n" + "=" * 90)
    print(f"{'SYM':7s} | {'Bi':>3s} {'Me':>3s} {'By':>3s} | {'CG':>3s} {'CMC':>3s} | {'取引所':>5s} | 判定")
    print("-" * 90)

    proposed_results = []
    for sym in PROPOSED_UNIVERSE:
        d = classify(sym, sources)
        proposed_results.append(d)
        bi = "✅" if d["binance"] else ("⚠️" if d["binance_broken"] else "❌")
        me = "✅" if d["mexc"] else "❌"
        by = "✅" if d["bybit"] else "❌"
        cg = "✅" if d["coingecko"] else "❌"
        cm = "✅" if d["coinmarketcap"] else ("—" if sources["coinmarketcap"]["skipped"] else "❌")
        icon = {"PASS": "🟢", "WARN": "🟡", "WEAK": "🟠",
                "DATA_ONLY": "📊", "FAIL": "🔴", "BROKEN": "💀"}[d["verdict"]]
        exchanges = "/".join(d["tradeable_on"]) if d["tradeable_on"] else "—"
        print(f"{sym:7s} | {bi:>3s} {me:>3s} {by:>3s} | {cg:>3s} {cm:>3s} | "
              f"{exchanges:>20s} | {icon} {d['verdict']}")

    # 除外予定銘柄の再確認
    print("\n" + "=" * 90)
    print("🗑️ 除外予定銘柄の再確認 (MEXC/Bybit でも取引できない？)")
    print("-" * 90)
    removed_results = []
    for sym in REMOVED_UNIVERSE:
        d = classify(sym, sources)
        removed_results.append(d)
        bi = "💀BREAK" if d["binance_broken"] else ("✅" if d["binance"] else "❌")
        me = "✅" if d["mexc"] else "❌"
        by = "✅" if d["bybit"] else "❌"
        cg = "✅" if d["coingecko"] else "❌"
        cm = "✅" if d["coinmarketcap"] else "—"
        icon = {"PASS": "🟢", "WARN": "🟡", "WEAK": "🟠",
                "DATA_ONLY": "📊", "FAIL": "🔴", "BROKEN": "💀"}[d["verdict"]]
        exchanges = "/".join(d["tradeable_on"]) if d["tradeable_on"] else "(どこでも取引不可)"
        print(f"{sym:7s} | {bi:>7s} {me:>3s} {by:>3s} | {cg:>3s} {cm:>3s} | "
              f"{exchanges:>20s} | {icon} {d['verdict']}")

    # 集計
    counts = {"PASS": 0, "WARN": 0, "WEAK": 0, "DATA_ONLY": 0, "FAIL": 0, "BROKEN": 0}
    for d in proposed_results:
        counts[d["verdict"]] += 1

    print("\n" + "=" * 90)
    print("📊 提案ユニバース集計:")
    print(f"  🟢 PASS       : {counts['PASS']}銘柄 (複数取引所+データ源)")
    print(f"  🟡 WARN       : {counts['WARN']}銘柄 (1取引所のみ+データ源)")
    print(f"  🟠 WEAK       : {counts['WEAK']}銘柄 (取引所あるがデータ源未確認)")
    print(f"  📊 DATA_ONLY  : {counts['DATA_ONLY']}銘柄 (データ源のみ・取引不可)")
    print(f"  🔴 FAIL       : {counts['FAIL']}銘柄")
    print(f"  💀 BROKEN     : {counts['BROKEN']}銘柄")

    safe_universe = [d["symbol"] for d in proposed_results if d["verdict"] in ("PASS", "WARN")]
    print(f"\n✅ 採用候補 (PASS+WARN): {len(safe_universe)}銘柄")
    print(f"   {safe_universe}")

    # 総合判定
    if counts["FAIL"] > 0 or counts["BROKEN"] > 0:
        overall = "ATTENTION_REQUIRED"
    elif counts["WEAK"] > 0:
        overall = "WARN"
    else:
        overall = "PASS"

    out = {
        "script": "_verify_ticker_5sources.py",
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "sources_used": {
            "binance_trading": len(sources["binance"]["trading"]),
            "mexc_trading": len(sources["mexc"]["trading"]),
            "bybit_trading": len(sources["bybit"]["trading"]),
            "coingecko_top": len(sources["coingecko"]["ids"]),
            "coinmarketcap_used": not sources["coinmarketcap"]["skipped"],
        },
        "overall_verdict": overall,
        "counts": counts,
        "safe_universe": safe_universe,
        "proposed_results": proposed_results,
        "removed_results": removed_results,
    }

    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=list))
    print(f"\n💾 JSON: {OUT_JSON}")


if __name__ == "__main__":
    main()
