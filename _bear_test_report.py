"""
_bear_test_report.py — 2018-2022 bear 相場耐性テスト + HTML レポート
===================================================================

目的: 気持ちマックス v2.3 C3 (Top2) が bear 相場でどう振る舞うか検証。
  - 2018 年 BTC -84% の歴史的 bear
  - 2022 年 BTC -65% + FTX 崩壊
  両方を含む 2018-01-01 〜 2022-12-31 の 5 年で同じ Bot を回す。

ステップ:
  1. Binance API から 62 銘柄の日足データを取得 (2018-2022)
  2. 新キャッシュ _bear_test_cache.pkl に保存
  3. run_bt_v22_exact で v2.3 C3 (Top2) と v2.2 (Top3) を実行
  4. 既存 2020-2024 結果と並べて HTML 出力

出力: results/bear_test_report.html
"""
from __future__ import annotations

import json
import math
import pickle
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _iter59_v22_verify import run_bt_v22_exact

PROJECT = Path(__file__).resolve().parent
RESULTS = PROJECT / "results"
CACHE_OUT = RESULTS / "_bear_test_cache.pkl"
CACHE_EXISTING = RESULTS / "_iter61_cache.pkl"  # 既存 2020-2024 用
OUT_HTML = RESULTS / "bear_test_report.html"
OUT_JSON = RESULTS / "bear_test_report.json"

# demo_runner.py から取得
UNIVERSE = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "AVAX", "DOGE", "TRX", "DOT",
    "LINK", "UNI", "LTC", "ATOM", "NEAR", "ICP", "ETC", "XLM", "FIL",
    "APT", "ARB", "OP", "INJ", "SUI", "SEI", "TIA", "RUNE", "ALGO",
    "SAND", "MANA", "AXS", "CHZ", "ENJ", "GRT", "AAVE", "SNX", "CRV",
    "HBAR", "VET", "THETA", "EGLD", "XTZ", "FLOW", "IOTA", "DASH", "ZEC",
    "POL", "TON", "ONDO", "JUP", "WLD", "LDO", "IMX", "WIF",
    "ENA", "GALA", "JASMY", "PENDLE", "MINA", "RENDER", "STRK", "SUSHI",
]


def fetch_klines(symbol_usdt: str, start_ms: int, end_ms: int, limit: int = 1000):
    """Binance klines API 1 リクエスト."""
    params = {
        "symbol": symbol_usdt,
        "interval": "1d",
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": str(limit),
    }
    url = f"https://api.binance.com/api/v3/klines?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_historical_daily(symbol: str, start: str, end: str) -> pd.DataFrame | None:
    """指定銘柄の日足を取得。取得できなければ None."""
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
    sym_usdt = f"{symbol}USDT"

    all_klines = []
    cur_start = start_ms
    while cur_start < end_ms:
        try:
            klines = fetch_klines(sym_usdt, cur_start, end_ms, 1000)
        except Exception as e:
            print(f"  {symbol}: API エラー {e}")
            return None
        if not klines:
            break
        all_klines.extend(klines)
        last_open = klines[-1][0]
        cur_start = last_open + 86400_000
        if len(klines) < 1000:
            break
        time.sleep(0.1)  # rate limit

    if len(all_klines) < 100:
        return None

    df = pd.DataFrame(all_klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "n_trades",
        "taker_buy_base", "taker_buy_quote", "_ignore",
    ])
    # tz-naive + ns precision (既存 _iter61_cache.pkl と同形式)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms").astype("datetime64[ns]")
    df = df.set_index("open_time")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df[["open", "high", "low", "close", "volume"]]


def build_bear_cache(start: str = "2018-01-01", end: str = "2022-12-31",
                     btc_warmup_start: str = "2017-07-01") -> dict:
    """62 銘柄のデータを取得しキャッシュ構造を返す.

    BTC のみ btc_warmup_start から先行取得することで EMA200 の warm-up を確保する。
    これにより 2018-01-01 からのバックテストで EMA200 判定が信頼できる値になる。
    """
    if CACHE_OUT.exists():
        print(f"既存キャッシュ使用: {CACHE_OUT.name}")
        with open(CACHE_OUT, "rb") as f:
            return pickle.load(f)

    print(f"Binance から {len(UNIVERSE)} 銘柄 × {start} 〜 {end} を取得")
    print(f"  (BTC のみ {btc_warmup_start} から先行取得: EMA200 warm-up)")
    all_data = {}
    succeeded = []
    failed = []
    t0 = time.time()
    for i, sym in enumerate(UNIVERSE, 1):
        fetch_start = btc_warmup_start if sym == "BTC" else start
        df = fetch_historical_daily(sym, fetch_start, end)
        if df is None or df.empty:
            failed.append(sym)
            print(f"  [{i:2d}/{len(UNIVERSE)}] {sym}: ❌ 取得不可")
            continue
        all_data[f"{sym}/USDT"] = df
        succeeded.append(sym)
        first_date = df.index[0].strftime("%Y-%m-%d")
        last_date = df.index[-1].strftime("%Y-%m-%d")
        print(f"  [{i:2d}/{len(UNIVERSE)}] {sym}: {first_date} 〜 {last_date} ({len(df)} 日)")

    elapsed = time.time() - t0
    print(f"取得完了: 成功 {len(succeeded)} / 失敗 {len(failed)} ({elapsed:.0f}s)")
    if failed:
        print(f"  失敗銘柄 ({len(failed)}): {', '.join(failed)}")

    # EMA200 を BTC に付与
    if "BTC/USDT" in all_data:
        btc = all_data["BTC/USDT"].copy()
        btc["ema200"] = btc["close"].ewm(span=200, adjust=False).mean()
        all_data["BTC/USDT"] = btc

    with open(CACHE_OUT, "wb") as f:
        pickle.dump(all_data, f)
    print(f"保存: {CACHE_OUT}")
    return all_data


def compute_metrics(equity_curve, initial=10000.0):
    eq = [float(pt["equity"]) for pt in equity_curve]
    if len(eq) < 2:
        return {}
    final = eq[-1]
    years = len(eq) / 52
    cagr = (final / initial) ** (1 / years) * 100 - 100 if years > 0 and final > 0 else 0.0
    peak, max_dd = eq[0], 0.0
    for e in eq:
        if e > peak: peak = e
        dd = (peak - e) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd: max_dd = dd
    rets = [eq[i] / eq[i-1] - 1 for i in range(1, len(eq)) if eq[i-1] > 0]
    sharpe = (
        statistics.mean(rets) / statistics.pstdev(rets) * math.sqrt(52)
        if len(rets) > 1 and statistics.pstdev(rets) > 0 else 0.0
    )
    df = pd.DataFrame(equity_curve).set_index("ts")
    df.index = pd.to_datetime(df.index)
    yearly = {}
    prev = initial
    for ts, row in df["equity"].resample("YE").last().items():
        yearly[str(ts.year)] = round((float(row) / prev - 1) * 100, 2) if prev > 0 else 0.0
        prev = float(row)
    return {
        "cagr_pct": round(cagr, 2),
        "max_dd_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "final": round(final, 0),
        "yearly": yearly,
    }


def run_bt(all_data, universe, start, end, *, top_n):
    r = run_bt_v22_exact(
        all_data, universe, start, end,
        btc_w=0.35, ach_w=0.35, usdt_w=0.30,
        top_n=top_n, lookback=25, rebalance_days=7,
        adx_min=15, corr_threshold=0.80,
        weight_method="momentum", ach_bear_immediate=True,
        initial=10000.0,
    )
    ec = [
        {"ts": str(pd.to_datetime(pt["ts"]))[:10], "equity": float(pt["equity"])}
        for pt in r.get("equity_weekly", [])
    ]
    return ec, r.get("n_bear_exits", 0)


def main() -> int:
    print("=" * 60)
    print("bear 相場耐性テスト (2018-2022)")
    print("=" * 60)

    # データ取得
    all_data = build_bear_cache("2018-01-01", "2022-12-31")

    # tz-naive に正規化 (既存 _iter61_cache.pkl 形式に合わせる、run_bt_v22_exact 互換)
    for sym in list(all_data.keys()):
        df = all_data[sym]
        if df.index.tz is not None:
            df = df.copy()
            df.index = df.index.tz_localize(None)
            all_data[sym] = df

    # BTC に ema200 を再付与 (copy で消えた場合の保険)
    if "BTC/USDT" in all_data and "ema200" not in all_data["BTC/USDT"].columns:
        btc = all_data["BTC/USDT"].copy()
        btc["ema200"] = btc["close"].ewm(span=200, adjust=False).mean()
        all_data["BTC/USDT"] = btc

    remove = {"MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"}
    universe = sorted(s for s in all_data.keys() if s != "BTC/USDT" and s not in remove)
    print(f"\nバックテスト universe: {len(universe)} 銘柄")

    # 2018-2022 v2.3 C3 (Top2)
    print("\n[1/4] 2018-2022 v2.3 C3 (Top2)")
    ec_c3_bear, bear_exits_c3 = run_bt(all_data, universe, "2018-01-01", "2022-12-31", top_n=2)
    m_c3_bear = compute_metrics(ec_c3_bear)

    # 2018-2022 v2.2 (Top3)
    print("[2/4] 2018-2022 v2.2 (Top3)")
    ec_v22_bear, bear_exits_v22 = run_bt(all_data, universe, "2018-01-01", "2022-12-31", top_n=3)
    m_v22_bear = compute_metrics(ec_v22_bear)

    # 既存キャッシュで 2020-2024 v2.3 C3
    print("[3/4] 2020-2024 v2.3 C3 (比較用)")
    with open(CACHE_EXISTING, "rb") as f:
        existing = pickle.load(f)
    if "BTC/USDT" in existing and "ema200" not in existing["BTC/USDT"].columns:
        btc = existing["BTC/USDT"].copy()
        btc["ema200"] = btc["close"].ewm(span=200, adjust=False).mean()
        existing["BTC/USDT"] = btc
    uni_ex = sorted(s for s in existing.keys() if s != "BTC/USDT" and s not in remove)
    ec_c3_bull, _ = run_bt(existing, uni_ex, "2020-01-01", "2024-12-31", top_n=2)
    m_c3_bull = compute_metrics(ec_c3_bull)

    # BTC buy&hold 2018-2022
    print("[4/4] BTC buy&hold 2018-2022 (比較用)")
    btc_df = all_data["BTC/USDT"]
    # tz-naive に合わせる (all_data は上で tz-naive 正規化済)
    s_ts = pd.Timestamp("2018-01-01")
    e_ts = pd.Timestamp("2022-12-31")
    btc_slice = btc_df[(btc_df.index >= s_ts) & (btc_df.index <= e_ts)]
    fee, slip = 0.0006, 0.0003
    entry_p = float(btc_slice["close"].iloc[0]) * (1 + slip)
    qty = 10000.0 * (1 - fee) / entry_p
    weekly = btc_slice["close"].resample("W").last().dropna()
    ec_bh_bear = [{"ts": str(ts)[:10], "equity": float(qty * p)} for ts, p in weekly.items()]
    m_bh_bear = compute_metrics(ec_bh_bear)

    # ペイロード
    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "universe_size": len(universe),
            "universe_size_bull": len(uni_ex),
        },
        "bear_period_2018_2022": {
            "v23_c3_top2": {**m_c3_bear, "bear_exits": bear_exits_c3},
            "v22_top3": {**m_v22_bear, "bear_exits": bear_exits_v22},
            "btc_buy_hold": m_bh_bear,
        },
        "bull_period_2020_2024": {
            "v23_c3_top2": m_c3_bull,
        },
        "equity_curves": {
            "bear_c3": ec_c3_bear,
            "bear_v22": ec_v22_bear,
            "bear_btc_bh": ec_bh_bear,
            "bull_c3": ec_c3_bull,
        },
    }
    OUT_JSON.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    html = build_html(payload)
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"\n完了")
    print(f"JSON: {OUT_JSON}")
    print(f"HTML: {OUT_HTML}")
    return 0


def build_html(payload: dict) -> str:
    bear = payload["bear_period_2018_2022"]
    bull = payload["bull_period_2020_2024"]

    def fmt_pct(v): return f"{v:+.2f}%"
    def _cls(v): return "pos" if v >= 0 else "neg"

    def yearly_rows(m):
        rows = []
        for y, v in sorted(m.get("yearly", {}).items()):
            rows.append(
                f"<tr><td>{y}</td><td class='num {_cls(v)}'>{fmt_pct(v)}</td></tr>"
            )
        return "".join(rows)

    c3 = bear["v23_c3_top2"]
    v22 = bear["v22_top3"]
    bh = bear["btc_buy_hold"]
    c3_bull = bull["v23_c3_top2"]

    # 診断テキスト
    bear_survived = c3["cagr_pct"] > -10 and c3["max_dd_pct"] < 90
    verdict = "🟢 bear 相場でも生存" if bear_survived else "🔴 bear 相場で大損"
    verdict_color = "#2e7d32" if bear_survived else "#c62828"

    # equity curve データ
    ec_c3_bear = payload["equity_curves"]["bear_c3"]
    ec_v22_bear = payload["equity_curves"]["bear_v22"]
    ec_btc_bear = payload["equity_curves"]["bear_btc_bh"]
    labels_bear = [pt["ts"] for pt in ec_c3_bear]
    series_c3 = [round(pt["equity"], 2) for pt in ec_c3_bear]

    def align(target_list, labels):
        m = {pt["ts"]: pt["equity"] for pt in target_list}
        return [round(m[l], 2) if l in m else None for l in labels]

    series_v22 = align(ec_v22_bear, labels_bear)
    series_btc = align(ec_btc_bear, labels_bear)

    yearly_c3 = yearly_rows(c3)
    yearly_v22 = yearly_rows(v22)
    yearly_bh = yearly_rows(bh)

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>bear 相場耐性テスト (2018-2022) — 気持ちマックス v2.3 C3</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 1200px;
        margin: 24px auto; padding: 0 16px; background: #f5f5f5; color: #222; }}
h1 {{ color: #1565c0; border-bottom: 3px solid #1565c0; padding-bottom: 8px; }}
h2 {{ color: #1976d2; margin-top: 32px; }}
h3 {{ color: #388e3c; }}
table {{ border-collapse: collapse; width: 100%; background: white;
         box-shadow: 0 1px 4px rgba(0,0,0,0.1); margin: 12px 0; }}
th, td {{ padding: 8px 12px; border-bottom: 1px solid #ddd; }}
th {{ background: #1565c0; color: white; text-align: left; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.pos {{ color: #2e7d32; }}
.neg {{ color: #c62828; }}
.highlight {{ background: #e8f5e9 !important; }}
.verdict {{ background: white; padding: 20px; border-left: 6px solid {verdict_color};
            border-radius: 8px; margin: 20px 0; }}
.verdict h2 {{ color: {verdict_color}; margin: 0 0 8px; }}
.card-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 16px 0; }}
.card {{ background: white; padding: 16px; border-radius: 8px; text-align: center;
         box-shadow: 0 1px 4px rgba(0,0,0,0.1); }}
.card h3 {{ margin: 0 0 8px; color: #666; font-size: 13px; }}
.card .big {{ font-size: 24px; font-weight: bold; }}
.chart-wrap {{ background: white; padding: 16px; border-radius: 8px;
               box-shadow: 0 1px 4px rgba(0,0,0,0.1); margin: 16px 0; }}
.note {{ background: #fff8e1; padding: 12px; border-left: 4px solid #ffa726;
         border-radius: 4px; margin: 12px 0; font-size: 14px; }}
.cols-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
</style></head><body>

<h1>🐻 bear 相場耐性テスト (2018-2022)</h1>
<p>生成: {payload['meta']['generated_at']} /
対象: 気持ちマックス v2.3 C3 (Top2) + v2.2 (Top3) + BTC buy&hold /
universe: bear 期 {payload['meta']['universe_size']} 銘柄 (2018 時点で Binance 利用可) /
bull 期 {payload['meta']['universe_size_bull']} 銘柄</p>

<div class="verdict">
<h2>{verdict}</h2>
<p>2018 年 BTC -84% + 2022 年 BTC -65% を含む 5 年で v2.3 C3 の成績:
<b>CAGR {fmt_pct(c3['cagr_pct'])}</b>、<b>最大 DD {c3['max_dd_pct']:.1f}%</b>、
<b>最終資金 ${c3['final']:,.0f}</b> ($10,000 スタート)。
{"戦略は bear を耐えた。" if bear_survived else "戦略は bear で機能しなかった。見直しが必要。"}</p>
</div>

<h2>📊 bear 期 (2018-2022) vs bull 期 (2020-2024) 比較</h2>
<div class="card-grid">
<div class="card"><h3>bear 期 CAGR</h3>
<div class="big {_cls(c3['cagr_pct'])}">{fmt_pct(c3['cagr_pct'])}</div></div>
<div class="card"><h3>bear 期 MaxDD</h3>
<div class="big neg">{c3['max_dd_pct']:.1f}%</div></div>
<div class="card"><h3>bull 期 CAGR (参考)</h3>
<div class="big pos">{fmt_pct(c3_bull['cagr_pct'])}</div></div>
<div class="card"><h3>bull 期 MaxDD (参考)</h3>
<div class="big neg">{c3_bull['max_dd_pct']:.1f}%</div></div>
</div>

<h2>📈 equity curve (2018-2022)</h2>
<div class="chart-wrap"><canvas id="chBear" height="120"></canvas></div>

<h2>📅 年別リターン詳細</h2>
<div class="cols-2">
<div>
<h3>v2.3 C3 (Top2、採用)</h3>
<table><tr><th>年</th><th>リターン</th></tr>{yearly_c3}</table>
<p>bear 退避発動: {c3['bear_exits']} 回</p>
</div>
<div>
<h3>BTC buy&amp;hold (比較)</h3>
<table><tr><th>年</th><th>リターン</th></tr>{yearly_bh}</table>
</div>
</div>

<h3>v2.2 (Top3、変更前) との比較</h3>
<table>
<tr><th>年</th><th>v2.3 C3</th><th>v2.2 Top3</th><th>BTC 放置</th></tr>
""" + "".join(
        f"<tr><td>{y}</td>"
        f"<td class='num {_cls(c3['yearly'].get(y, 0))}'>{fmt_pct(c3['yearly'].get(y, 0))}</td>"
        f"<td class='num {_cls(v22['yearly'].get(y, 0))}'>{fmt_pct(v22['yearly'].get(y, 0))}</td>"
        f"<td class='num {_cls(bh['yearly'].get(y, 0))}'>{fmt_pct(bh['yearly'].get(y, 0))}</td></tr>"
        for y in sorted(set(list(c3['yearly'].keys()) + list(v22['yearly'].keys()) + list(bh['yearly'].keys())))
    ) + f"""
</table>

<h2>🔀 総合サマリ (5 年総計)</h2>
<table>
<tr><th>戦略</th><th>CAGR</th><th>MaxDD</th><th>Sharpe</th><th>最終資金</th><th>bear 退避</th></tr>
<tr class='highlight'><td><b>v2.3 C3 (Top2、採用)</b></td>
<td class='num {_cls(c3['cagr_pct'])}'>{fmt_pct(c3['cagr_pct'])}</td>
<td class='num neg'>{c3['max_dd_pct']:.1f}%</td>
<td class='num'>{c3['sharpe']:.2f}</td>
<td class='num'>${c3['final']:,.0f}</td>
<td class='num'>{c3['bear_exits']} 回</td></tr>
<tr><td>v2.2 (Top3、変更前)</td>
<td class='num {_cls(v22['cagr_pct'])}'>{fmt_pct(v22['cagr_pct'])}</td>
<td class='num neg'>{v22['max_dd_pct']:.1f}%</td>
<td class='num'>{v22['sharpe']:.2f}</td>
<td class='num'>${v22['final']:,.0f}</td>
<td class='num'>{v22['bear_exits']} 回</td></tr>
<tr><td>BTC buy&amp;hold</td>
<td class='num {_cls(bh['cagr_pct'])}'>{fmt_pct(bh['cagr_pct'])}</td>
<td class='num neg'>{bh['max_dd_pct']:.1f}%</td>
<td class='num'>{bh['sharpe']:.2f}</td>
<td class='num'>${bh['final']:,.0f}</td>
<td class='num'>-</td></tr>
</table>

<h2>🔎 読み方</h2>
<div class="note">
<ul>
<li><b>bear 期 CAGR がプラス</b>: bear 相場でも資産が増える (戦略が効いている証拠)</li>
<li><b>bear 期 CAGR がマイナス</b>: bear 相場で損失 (BTC 放置より遥かにマシならまだ OK)</li>
<li><b>bear 期 CAGR &lt; -20%</b>: 戦略の見直し必要</li>
<li><b>bear 退避の回数が多い</b>: ACH 即時ベア退避 (v2.2 新機能) が頻繁に発動 → 正常</li>
<li><b>Sharpe &gt; 0</b>: リスクに見合うリターンあり</li>
</ul>
</div>

<h2>⚠ 注意事項</h2>
<ul>
<li>2018 年初時点で Binance に存在した銘柄は限定的 ({payload['meta']['universe_size']} 銘柄)。
bull 期 ({payload['meta']['universe_size_bull']} 銘柄) より少ない</li>
<li>universe が小さいため、ACH モメンタム戦略の「分散」が弱くなる傾向あり</li>
<li>2018 と 2022 の 2 回の bear を含む 5 年は仮想通貨史上最も厳しい期間の 1 つ</li>
<li>ここで生き残れれば、将来の bear にも対応できる可能性が高い</li>
</ul>

<script>
const labels = {json.dumps(labels_bear)};
new Chart(document.getElementById('chBear'), {{
    type: 'line',
    data: {{
        labels: labels,
        datasets: [
            {{ label: 'v2.3 C3 (Top2、採用)', data: {json.dumps(series_c3)},
               borderColor: '#2e7d32', backgroundColor: 'rgba(46,125,50,0.1)',
               borderWidth: 2.5, tension: 0.1, pointRadius: 0 }},
            {{ label: 'v2.2 (Top3、変更前)', data: {json.dumps(series_v22)},
               borderColor: '#1976d2', backgroundColor: 'rgba(25,118,210,0.05)',
               borderWidth: 2, borderDash: [5, 3], tension: 0.1, pointRadius: 0 }},
            {{ label: 'BTC buy&hold', data: {json.dumps(series_btc)},
               borderColor: '#f57c00', backgroundColor: 'rgba(245,124,0,0.05)',
               borderWidth: 1.5, tension: 0.1, pointRadius: 0 }},
        ]
    }},
    options: {{
        responsive: true,
        plugins: {{ title: {{ display: true, text: '2018-2022 equity curve ($10,000 スタート、週次、対数軸)' }},
                    legend: {{ position: 'bottom' }} }},
        scales: {{ y: {{ type: 'logarithmic',
                        title: {{ display: true, text: '資産 ($, 対数軸)' }} }} }},
        interaction: {{ mode: 'index', intersect: false }},
    }}
}});
</script>

</body></html>
"""


if __name__ == "__main__":
    sys.exit(main())
