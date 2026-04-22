"""
_verify_ticker_existence.py — ティッカー実在性クロスチェック

Phase 0-1: demo_runner.py の ACH_UNIVERSE (50銘柄) を
  - Binance spot (exchangeInfo)
  - CoinGecko (coins/markets 上位500)
  - CoinMarketCap quotes (CMC_API_KEY環境変数必要)
の3ソースで実在性を検証する。

判定:
  - Binance + CoinGecko 両方で見つかる → PASS
  - どちらか1つのみ → WARN
  - 両方に見つからない → FAIL

CMC は補助ソース扱い（未設定ならスキップ）。

出力: results/ticker_existence.{json,html}
"""
from __future__ import annotations
import sys, os, json, time, urllib.request, urllib.error, urllib.parse
from pathlib import Path
from datetime import datetime, timezone

PROJECT = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT / "results"
RESULTS_DIR.mkdir(exist_ok=True)
OUT_JSON = RESULTS_DIR / "ticker_existence.json"
OUT_HTML = RESULTS_DIR / "ticker_existence.html"

# demo_runner.py L70-76 から取得 (手動複製・将来AST抽出に変更可)
ACH_UNIVERSE = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "AVAX", "DOGE", "TRX", "DOT",
    "MATIC", "LINK", "UNI", "LTC", "ATOM", "NEAR", "ICP", "ETC", "XLM", "FIL",
    "APT", "ARB", "OP", "INJ", "SUI", "SEI", "TIA", "RUNE", "FTM", "ALGO",
    "SAND", "MANA", "AXS", "CHZ", "ENJ", "GRT", "AAVE", "MKR", "SNX", "CRV",
    "HBAR", "EOS", "VET", "THETA", "EGLD", "XTZ", "FLOW", "IOTA", "DASH", "ZEC",
]

# _hallucination_full_check.py L60-78 から抽出+拡張
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
}


def http_get_json(url: str, headers: dict | None = None, timeout: int = 15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_binance_symbols() -> set[str]:
    """Binance spot で TRADING 状態の全シンボル"""
    r = http_get_json("https://api.binance.com/api/v3/exchangeInfo")
    out = set()
    for s in r.get("symbols", []):
        if s.get("status") == "TRADING":
            out.add(s["baseAsset"] + "/" + s["quoteAsset"])
    return out


def fetch_coingecko_ids(pages: int = 3) -> set[str]:
    """CoinGecko markets 上位 pages*100 銘柄の id セット"""
    out: set[str] = set()
    for page in range(1, pages + 1):
        try:
            url = (f"https://api.coingecko.com/api/v3/coins/markets"
                   f"?vs_currency=usd&per_page=100&page={page}")
            r = http_get_json(url, timeout=20)
            for c in r:
                if isinstance(c, dict) and c.get("id"):
                    out.add(c["id"])
            time.sleep(1.5)  # rate-limit配慮
        except Exception as e:
            print(f"⚠️ CoinGecko page {page} 取得失敗: {e}")
            break
    return out


def fetch_coinmarketcap_symbols(symbols: list[str]) -> dict:
    """CoinMarketCap quotes/latest で symbols の取得を試行。CMC_API_KEY未設定ならスキップ"""
    key = os.environ.get("CMC_API_KEY")
    if not key:
        return {"skipped": True, "reason": "CMC_API_KEY not set", "found": {}}
    try:
        syms = ",".join(symbols)
        url = (f"https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
               f"?symbol={syms}")
        r = http_get_json(url, headers={"X-CMC_PRO_API_KEY": key, "Accept": "application/json"})
        found = {}
        for s in symbols:
            if s in r.get("data", {}):
                d = r["data"][s]
                price = d.get("quote", {}).get("USD", {}).get("price")
                found[s] = {"id": d.get("id"), "name": d.get("name"), "price_usd": price}
        return {"skipped": False, "found": found, "total": len(found)}
    except Exception as e:
        return {"skipped": True, "reason": f"CMC error: {e}", "found": {}}


def verify(symbols: list[str]) -> dict:
    print(f"📊 {len(symbols)}銘柄の実在性検証を開始")
    print("-" * 70)

    print("🔵 Binance exchangeInfo 取得中...")
    binance = fetch_binance_symbols()
    print(f"  Binance TRADING数: {len(binance)}銘柄")

    print("🟡 CoinGecko markets 取得中（上位300銘柄）...")
    cg_ids = fetch_coingecko_ids(pages=3)
    print(f"  CoinGecko 掲載数: {len(cg_ids)}銘柄")

    print("🟠 CoinMarketCap quotes 取得中...")
    cmc_result = fetch_coinmarketcap_symbols(symbols)
    if cmc_result["skipped"]:
        print(f"  スキップ: {cmc_result['reason']}")
    else:
        print(f"  CMC 確認数: {cmc_result['total']}銘柄")

    print("\n" + "=" * 70)
    print(f"{'SYM':6s} | {'Binance':>9s} | {'CoinGecko':>10s} | {'CMC':>4s} | 判定")
    print("-" * 70)

    details = []
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for sym in symbols:
        usdt_pair = f"{sym}/USDT"
        in_binance = usdt_pair in binance
        cg_id = CG_ID_MAP.get(sym, "")
        in_cg = cg_id in cg_ids if cg_id else False
        in_cmc = sym in cmc_result.get("found", {})

        # 判定ロジック
        core_sources = [in_binance, in_cg]  # 必須: Binance + CoinGecko
        core_hits = sum(1 for x in core_sources if x)
        if core_hits == 2:
            verdict = "PASS"
        elif core_hits == 1:
            verdict = "WARN"
        else:
            verdict = "FAIL"
        counts[verdict] += 1

        cmc_info = cmc_result.get("found", {}).get(sym, {})
        details.append({
            "symbol": sym,
            "binance_pair": usdt_pair,
            "in_binance": in_binance,
            "coingecko_id": cg_id or None,
            "in_coingecko": in_cg,
            "in_coinmarketcap": in_cmc,
            "cmc_price_usd": cmc_info.get("price_usd"),
            "cmc_name": cmc_info.get("name"),
            "verdict": verdict,
        })

        b = "✅" if in_binance else "❌"
        g = "✅" if in_cg else ("—" if not cg_id else "❌")
        c = "✅" if in_cmc else ("—" if cmc_result["skipped"] else "❌")
        icon = {"PASS": "🟢", "WARN": "🟡", "FAIL": "🔴"}[verdict]
        print(f"{sym:6s} | {b:>9s} | {g:>10s} | {c:>4s} | {icon} {verdict}")

    overall = "FAIL" if counts["FAIL"] > 0 else ("WARN" if counts["WARN"] > 0 else "PASS")

    result = {
        "script": "_verify_ticker_existence.py",
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "total_symbols": len(symbols),
        "counts": counts,
        "verdict": overall,
        "sources": {
            "binance_total": len(binance),
            "coingecko_total": len(cg_ids),
            "coinmarketcap": {
                "used": not cmc_result["skipped"],
                "reason": cmc_result.get("reason"),
                "confirmed": cmc_result.get("total", 0),
            },
        },
        "failed_items": [d["symbol"] for d in details if d["verdict"] == "FAIL"],
        "warn_items": [d["symbol"] for d in details if d["verdict"] == "WARN"],
        "details": details,
    }
    return result


def generate_html(result: dict) -> str:
    overall = result["verdict"]
    counts = result["counts"]
    ran_at = result["ran_at"]
    verdict_color = {"PASS": "#48bb78", "WARN": "#ed8936", "FAIL": "#e53e3e"}[overall]
    verdict_label = {"PASS": "✅ 全銘柄 実在確認 OK",
                     "WARN": "⚠️ 一部の銘柄で要注意",
                     "FAIL": "🔴 架空銘柄の疑いあり"}[overall]

    rows = ""
    for d in result["details"]:
        v = d["verdict"]
        bg = {"PASS": "#f0fff4", "WARN": "#fffaf0", "FAIL": "#fff5f5"}[v]
        vc = {"PASS": "#48bb78", "WARN": "#ed8936", "FAIL": "#e53e3e"}[v]
        b = "✅" if d["in_binance"] else "❌"
        g = "✅" if d["in_coingecko"] else ("—" if not d["coingecko_id"] else "❌")
        c = "✅" if d["in_coinmarketcap"] else "—"
        price = f"${d['cmc_price_usd']:,.4f}" if d.get("cmc_price_usd") else "—"
        rows += (f'<tr style="background:{bg};">'
                 f'<td style="font-weight:700;">{d["symbol"]}</td>'
                 f'<td>{d["binance_pair"]}</td>'
                 f'<td style="text-align:center;">{b}</td>'
                 f'<td style="text-align:center;">{g}</td>'
                 f'<td style="text-align:center;">{c}</td>'
                 f'<td style="text-align:right;">{price}</td>'
                 f'<td style="color:{vc}; font-weight:700;">{v}</td>'
                 f'</tr>')

    cmc_note = ""
    if not result["sources"]["coinmarketcap"]["used"]:
        cmc_note = (f'<div class="note">CoinMarketCap は '
                    f'{result["sources"]["coinmarketcap"]["reason"]} のため補助判定のみ</div>')

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ティッカー実在性検証レポート | 気持ちマックス</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, "Hiragino Kaku Gothic ProN", sans-serif;
       background: linear-gradient(135deg,#667eea 0%,#764ba2 100%); min-height: 100vh;
       padding: 20px; color: #2c3e50; }}
.container {{ max-width: 1100px; margin: 0 auto; }}
h1 {{ color: white; text-align: center; font-size: 2rem; margin-bottom: 8px;
      text-shadow: 0 2px 10px rgba(0,0,0,0.2); }}
.subtitle {{ color: rgba(255,255,255,0.9); text-align: center; margin-bottom: 24px; }}
.verdict-badge {{ display: inline-block; padding: 12px 24px; border-radius: 999px;
                  font-size: 1.3rem; font-weight: 700; color: white;
                  background: {verdict_color}; margin: 0 auto 20px; }}
.verdict-wrap {{ text-align: center; margin-bottom: 20px; }}
.card {{ background: white; border-radius: 16px; padding: 28px; margin-bottom: 20px;
        box-shadow: 0 10px 40px rgba(0,0,0,0.1); }}
.card h2 {{ font-size: 1.4rem; margin-bottom: 16px; color: #4a5568;
           border-left: 5px solid #667eea; padding-left: 12px; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
         gap: 12px; margin-top: 14px; }}
.stat {{ padding: 14px; border-radius: 10px; text-align: center; }}
.stat-pass {{ background: #f0fff4; border: 2px solid #48bb78; }}
.stat-warn {{ background: #fffaf0; border: 2px solid #ed8936; }}
.stat-fail {{ background: #fff5f5; border: 2px solid #e53e3e; }}
.stat-val {{ font-size: 2rem; font-weight: 700; }}
.stat-label {{ font-size: 0.9rem; color: #718096; margin-top: 4px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 0.9rem; }}
th {{ background: #667eea; color: white; padding: 10px 8px; text-align: left; }}
td {{ padding: 8px; border-bottom: 1px solid #e2e8f0; }}
.note {{ background: #fffaf0; border-left: 4px solid #ed8936; padding: 12px;
        border-radius: 8px; margin-top: 14px; font-size: 0.9rem; }}
.explain {{ background: #f0f4ff; border-left: 4px solid #667eea; padding: 14px;
          border-radius: 8px; margin: 14px 0; line-height: 1.8; font-size: 0.95rem; }}
.footer {{ text-align: center; color: rgba(255,255,255,0.8); padding: 20px;
          font-size: 0.85rem; }}
</style></head><body>
<div class="container">
<h1>🔍 ティッカー実在性検証レポート</h1>
<p class="subtitle">ACH_UNIVERSE の {result['total_symbols']}銘柄が本物か確認</p>

<div class="verdict-wrap">
  <div class="verdict-badge">{verdict_label}</div>
</div>

<div class="card">
<h2>📊 検証結果サマリー</h2>
<div class="explain">
気持ちマックスの ACH 戦略が使う {result['total_symbols']}銘柄を、
<strong>Binance</strong>、<strong>CoinGecko</strong>、<strong>CoinMarketCap</strong>の3ソースで実在確認。
架空銘柄や消滅銘柄が混入していないか厳格にチェックしました。
</div>
<div class="stats">
  <div class="stat stat-pass"><div class="stat-val">{counts['PASS']}</div>
    <div class="stat-label">✅ PASS（2ソース以上）</div></div>
  <div class="stat stat-warn"><div class="stat-val">{counts['WARN']}</div>
    <div class="stat-label">⚠️ WARN（1ソースのみ）</div></div>
  <div class="stat stat-fail"><div class="stat-val">{counts['FAIL']}</div>
    <div class="stat-label">🔴 FAIL（0ソース）</div></div>
</div>
{cmc_note}
</div>

<div class="card">
<h2>📋 銘柄別詳細</h2>
<table>
<thead><tr><th>SYM</th><th>Binance Pair</th>
<th style="text-align:center;">Binance</th>
<th style="text-align:center;">CoinGecko</th>
<th style="text-align:center;">CMC</th>
<th style="text-align:right;">CMC価格</th>
<th>判定</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</div>

<div class="card">
<h2>💡 この検証の意味</h2>
<div class="explain">
<strong>なぜ重要か:</strong> 過去に消滅した銘柄や取引所から上場廃止された銘柄がユニバースに残っていると、
バックテストで「架空の取引」が発生する可能性があります。
3ソースクロスチェックで、<strong>現在も取引可能な実在銘柄だけ</strong>が対象であることを保証します。
<br><br>
<strong>判定基準:</strong><br>
・ 🟢 <strong>PASS</strong>: Binance + CoinGecko の両方で確認 → 安全<br>
・ 🟡 <strong>WARN</strong>: 片方のみ確認 → 戦略から除外検討<br>
・ 🔴 <strong>FAIL</strong>: どちらにも無い → <strong>即座に戦略から除外すべき</strong>
</div>
</div>

<div class="footer">生成日時: {ran_at}<br>🤖 気持ちマックス Phase 0 検証基盤</div>
</div></body></html>"""
    return html


def main():
    print("=" * 70)
    print("🔍 ティッカー実在性検証 (Phase 0-1)")
    print("=" * 70)
    result = verify(ACH_UNIVERSE)

    print(f"\n📊 判定: {result['verdict']}")
    print(f"   PASS: {result['counts']['PASS']}, WARN: {result['counts']['WARN']}, FAIL: {result['counts']['FAIL']}")

    OUT_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\n💾 JSON: {OUT_JSON}")

    html = generate_html(result)
    OUT_HTML.write_text(html)
    print(f"💾 HTML: {OUT_HTML}")

    # FAILがあった場合のフラグ
    if result["verdict"] == "FAIL":
        flag = PROJECT / "HALLUCINATION_DETECTED.flag"
        flag.write_text(f"[{result['ran_at']}] ticker_existence FAIL: {result['failed_items']}\n")
        print(f"\n🚨 HALLUCINATION_DETECTED.flag 作成済み: {flag}")
        sys.exit(1)


if __name__ == "__main__":
    main()
