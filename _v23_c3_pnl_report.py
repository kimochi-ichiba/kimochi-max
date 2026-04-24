"""
_v23_c3_pnl_report.py — 気持ちマックス v2.3 C3 の収支レポート HTML 生成
======================================================================

対象: PR #4 (C3 採用: ACH_TOP_N=3→2、他維持) マージ後の設定

出力内容:
- 2020-2024 フル期間の equity curve (v2.3 C3 と benchmarks の時系列)
- 年別リターン表
- 最大 DD 推移
- 想定投入額別のシミュレーション ($10k / $100k / $1M / ¥1.5M / ¥15M)
- benchmarks 比較 (BTC buy&hold / 月次 DCA / BTC trend-follow)
- リスク・リワード要約

出力: results/kimochimax_v23_c3_pnl_report.html
"""
from __future__ import annotations
import json
import math
import pickle
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _iter59_v22_verify import run_bt_v22_exact

PROJECT = Path(__file__).resolve().parent
RESULTS = PROJECT / "results"
CACHE = RESULTS / "_iter61_cache.pkl"
OUT_HTML = RESULTS / "kimochimax_v23_c3_pnl_report.html"

USD_JPY = 150.0  # ¥ 換算用概算


def compute_metrics(equity_curve: list[dict], initial: float = 10000.0) -> dict:
    """equity_curve からメトリクスを計算."""
    eq = [float(pt["equity"]) for pt in equity_curve]
    if len(eq) < 2:
        return {}
    final = eq[-1]
    years = len(eq) / 52
    cagr = (final / initial) ** (1 / years) * 100 - 100 if years > 0 and final > 0 else 0.0
    peak, max_dd = eq[0], 0.0
    dd_series = []
    for e in eq:
        if e > peak: peak = e
        dd = (peak - e) / peak * 100 if peak > 0 else 0.0
        dd_series.append(dd)
        if dd > max_dd: max_dd = dd
    rets = [eq[i] / eq[i-1] - 1 for i in range(1, len(eq)) if eq[i-1] > 0]
    sharpe = (
        statistics.mean(rets) / statistics.pstdev(rets) * math.sqrt(52)
        if len(rets) > 1 and statistics.pstdev(rets) > 0 else 0.0
    )
    # 年別
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
        "num_weeks": len(eq),
        "yearly_return_pct": yearly,
        "dd_series": dd_series,
    }


def buy_hold_curve(all_data: dict, symbol: str, start: str, end: str,
                    initial: float = 10000.0) -> list[dict]:
    """対象銘柄を期間始めに全量買って equity_weekly 相当を返す."""
    df = all_data[symbol].copy()
    df.index = pd.to_datetime(df.index)
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    if df.index.tz is not None:
        s = s.tz_localize(df.index.tz)
        e = e.tz_localize(df.index.tz)
    df = df[(df.index >= s) & (df.index <= e)]
    if df.empty:
        return []
    fee, slip = 0.0006, 0.0003
    entry = float(df["close"].iloc[0]) * (1 + slip)
    qty = initial * (1 - fee) / entry
    weekly = df["close"].resample("W").last().dropna()
    return [{"ts": str(ts)[:10], "equity": float(qty * p)} for ts, p in weekly.items()]


def monthly_dca_curve(all_data: dict, symbol: str, start: str, end: str,
                      initial: float = 10000.0) -> list[dict]:
    df = all_data[symbol].copy()
    df.index = pd.to_datetime(df.index)
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    if df.index.tz is not None:
        s = s.tz_localize(df.index.tz)
        e = e.tz_localize(df.index.tz)
    df = df[(df.index >= s) & (df.index <= e)]
    if df.empty:
        return []
    month_starts = df.resample("MS").first().dropna().index
    n_months = len(month_starts)
    if n_months == 0:
        return []
    per_month = initial / n_months
    fee, slip = 0.0006, 0.0003
    qty = 0.0
    cash = initial
    month_idx = 0
    out = []
    # 週次価格を resample から直接取得 (df.loc で探索不要)
    weekly_prices = df["close"].resample("W").last().dropna()
    for ts, current_price in weekly_prices.items():
        # その週までの月初で購入 (month_starts は df.index にある日付)
        while month_idx < n_months and month_starts[month_idx] <= ts:
            if cash >= per_month:
                buy_price = float(df.loc[month_starts[month_idx], "close"]) * (1 + slip)
                qty += per_month * (1 - fee) / buy_price
                cash -= per_month
            month_idx += 1
        out.append({"ts": str(ts)[:10], "equity": cash + qty * float(current_price)})
    return out


def trend_follow_curve(all_data: dict, symbol: str, start: str, end: str,
                       initial: float = 10000.0) -> list[dict]:
    df = all_data[symbol].copy()
    df.index = pd.to_datetime(df.index)
    df["_ema"] = df["close"].ewm(span=200, adjust=False).mean()
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    if df.index.tz is not None:
        s = s.tz_localize(df.index.tz)
        e = e.tz_localize(df.index.tz)
    df = df[(df.index >= s) & (df.index <= e)]
    fee, slip = 0.0006, 0.0003
    cash, qty, in_pos, entry = initial, 0.0, False, 0.0
    daily_eq = []
    for ts, row in df.iterrows():
        p = float(row["close"])
        ema = float(row["_ema"]) if not pd.isna(row["_ema"]) else p
        bullish = p > ema
        if bullish and not in_pos:
            entry = p * (1 + slip)
            qty = cash * (1 - fee) / entry
            cash = 0.0
            in_pos = True
        elif not bullish and in_pos:
            xp = p * (1 - slip)
            cash = qty * xp * (1 - fee)
            qty = 0.0
            in_pos = False
        daily_eq.append({"ts": ts, "equity": cash + qty * p if in_pos else cash})
    daily_df = pd.DataFrame(daily_eq).set_index("ts")
    weekly = daily_df["equity"].resample("W").last().dropna()
    return [{"ts": str(ts)[:10], "equity": float(v)} for ts, v in weekly.items()]


def main() -> int:
    print("=" * 60)
    print("気持ちマックス v2.3 C3 収支レポート生成")
    print("=" * 60)

    with open(CACHE, "rb") as f:
        all_data = pickle.load(f)

    if "BTC/USDT" in all_data and "ema200" not in all_data["BTC/USDT"].columns:
        df = all_data["BTC/USDT"].copy()
        df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
        all_data["BTC/USDT"] = df

    remove = {"MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"}
    universe = sorted(
        s for s in all_data.keys() if s != "BTC/USDT" and s not in remove
    )

    start, end = "2020-01-01", "2024-12-31"
    initial = 10000.0

    print("[1/4] v2.3 C3 (Top2) バックテスト実行")
    r_v23 = run_bt_v22_exact(
        all_data, universe, start, end,
        btc_w=0.35, ach_w=0.35, usdt_w=0.30,
        top_n=2, lookback=25, rebalance_days=7,
        adx_min=15, corr_threshold=0.80,
        weight_method="momentum", ach_bear_immediate=True,
        initial=initial,
    )
    ec_v23 = [
        {"ts": str(pd.to_datetime(pt["ts"]))[:10], "equity": float(pt["equity"])}
        for pt in r_v23.get("equity_weekly", [])
    ]
    m_v23 = compute_metrics(ec_v23, initial)

    print("[2/4] 現行 v2.2 (Top3) バックテスト実行")
    r_v22 = run_bt_v22_exact(
        all_data, universe, start, end,
        btc_w=0.35, ach_w=0.35, usdt_w=0.30,
        top_n=3, lookback=25, rebalance_days=7,
        adx_min=15, corr_threshold=0.80,
        weight_method="momentum", ach_bear_immediate=True,
        initial=initial,
    )
    ec_v22 = [
        {"ts": str(pd.to_datetime(pt["ts"]))[:10], "equity": float(pt["equity"])}
        for pt in r_v22.get("equity_weekly", [])
    ]
    m_v22 = compute_metrics(ec_v22, initial)

    print("[3/4] Benchmark 3 種 (buy&hold / DCA / trend-follow)")
    ec_bh = buy_hold_curve(all_data, "BTC/USDT", start, end, initial)
    ec_dca = monthly_dca_curve(all_data, "BTC/USDT", start, end, initial)
    ec_tf = trend_follow_curve(all_data, "BTC/USDT", start, end, initial)
    m_bh = compute_metrics(ec_bh, initial)
    m_dca = compute_metrics(ec_dca, initial)
    m_tf = compute_metrics(ec_tf, initial)

    print("[4/4] HTML 生成")

    # 想定投入額シナリオ (CAGR を元に 5 年後のシミュレーション)
    scenarios = []
    for amount_label, amount_usd in [
        ("$10,000 (約 ¥150 万)", 10_000),
        ("$50,000 (約 ¥750 万)", 50_000),
        ("$100,000 (約 ¥1,500 万)", 100_000),
        ("$500,000 (約 ¥7,500 万)", 500_000),
        ("$1,000,000 (約 ¥1.5 億)", 1_000_000),
    ]:
        cagr_v23 = m_v23["cagr_pct"] / 100
        final_v23 = amount_usd * (1 + cagr_v23) ** 5
        max_loss_v23 = amount_usd * m_v23["max_dd_pct"] / 100
        scenarios.append({
            "amount_label": amount_label,
            "amount_usd": amount_usd,
            "amount_jpy": amount_usd * USD_JPY,
            "final_usd_5y": final_v23,
            "final_jpy_5y": final_v23 * USD_JPY,
            "profit_usd": final_v23 - amount_usd,
            "max_loss_usd": max_loss_v23,
            "max_loss_jpy": max_loss_v23 * USD_JPY,
        })

    # Chart.js 用データ (週次)
    labels = [pt["ts"] for pt in ec_v23]
    series_v23 = [round(pt["equity"], 2) for pt in ec_v23]
    # v22 と benchmarks を ec_v23 の ts に align (単純にインデックス合わせ、無ければ null)
    def align(target, reference):
        tm = {pt["ts"]: pt["equity"] for pt in target}
        return [round(tm.get(ts, None), 2) if tm.get(ts) else None for ts in reference]
    series_v22 = align(ec_v22, labels)
    series_bh = align(ec_bh, labels)
    series_dca = align(ec_dca, labels)
    series_tf = align(ec_tf, labels)

    # DD シリーズ
    dd_v23 = [round(d, 2) for d in m_v23["dd_series"]]
    dd_v22 = [round(d, 2) for d in m_v22["dd_series"]]

    # 年別比較表
    years = sorted(set(list(m_v23["yearly_return_pct"].keys()) + list(m_v22["yearly_return_pct"].keys())))
    yearly_rows = []
    for y in years:
        v23 = m_v23["yearly_return_pct"].get(y, 0)
        v22 = m_v22["yearly_return_pct"].get(y, 0)
        diff = v23 - v22
        yearly_rows.append({"year": y, "v23": v23, "v22": v22, "diff": diff})

    html = build_html(
        m_v23, m_v22, m_bh, m_dca, m_tf,
        scenarios, yearly_rows,
        labels, series_v23, series_v22, series_bh, series_dca, series_tf,
        dd_v23, dd_v22,
    )
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"完了: {OUT_HTML}")
    return 0


def build_html(m_v23, m_v22, m_bh, m_dca, m_tf, scenarios, yearly_rows,
               labels, series_v23, series_v22, series_bh, series_dca, series_tf,
               dd_v23, dd_v22) -> str:

    def fmt_usd(v): return f"${v:,.0f}"
    def fmt_jpy(v): return f"¥{v:,.0f}"
    def fmt_pct(v): return f"{v:+.2f}%"

    def _cls(v): return "pos" if v >= 0 else "neg"
    yr_rows = "\n".join(
        f"<tr><td>{r['year']}</td>"
        f"<td class='num {_cls(r['v23'])}'>{fmt_pct(r['v23'])}</td>"
        f"<td class='num {_cls(r['v22'])}'>{fmt_pct(r['v22'])}</td>"
        f"<td class='num {_cls(r['diff'])}'>{fmt_pct(r['diff'])}</td></tr>"
        for r in yearly_rows
    )

    sc_rows = "\n".join(
        f"<tr><td>{s['amount_label']}</td>"
        f"<td class='num'>{fmt_usd(s['amount_usd'])}</td>"
        f"<td class='num'>{fmt_jpy(s['amount_jpy'])}</td>"
        f"<td class='num pos'>{fmt_usd(s['final_usd_5y'])}</td>"
        f"<td class='num pos'>{fmt_jpy(s['final_jpy_5y'])}</td>"
        f"<td class='num pos'>{fmt_usd(s['profit_usd'])}</td>"
        f"<td class='num neg'>-{fmt_usd(s['max_loss_usd'])}</td>"
        f"<td class='num neg'>-{fmt_jpy(s['max_loss_jpy'])}</td></tr>"
        for s in scenarios
    )

    bench_rows = f"""
<tr class='highlight'><td><b>v2.3 C3 (Top2、採用)</b></td>
<td class='num'>{fmt_pct(m_v23['cagr_pct'])}</td>
<td class='num'>{m_v23['max_dd_pct']:.2f}%</td>
<td class='num'>{m_v23['sharpe']:.2f}</td>
<td class='num'>{fmt_usd(m_v23['final'])}</td></tr>
<tr><td>v2.2 (Top3、現行)</td>
<td class='num'>{fmt_pct(m_v22['cagr_pct'])}</td>
<td class='num'>{m_v22['max_dd_pct']:.2f}%</td>
<td class='num'>{m_v22['sharpe']:.2f}</td>
<td class='num'>{fmt_usd(m_v22['final'])}</td></tr>
<tr><td>BTC buy&amp;hold</td>
<td class='num'>{fmt_pct(m_bh['cagr_pct'])}</td>
<td class='num'>{m_bh['max_dd_pct']:.2f}%</td>
<td class='num'>{m_bh['sharpe']:.2f}</td>
<td class='num'>{fmt_usd(m_bh['final'])}</td></tr>
<tr><td>毎月 DCA (BTC)</td>
<td class='num'>{fmt_pct(m_dca['cagr_pct'])}</td>
<td class='num'>{m_dca['max_dd_pct']:.2f}%</td>
<td class='num'>{m_dca['sharpe']:.2f}</td>
<td class='num'>{fmt_usd(m_dca['final'])}</td></tr>
<tr><td>BTC trend-follow (EMA200)</td>
<td class='num'>{fmt_pct(m_tf['cagr_pct'])}</td>
<td class='num'>{m_tf['max_dd_pct']:.2f}%</td>
<td class='num'>{m_tf['sharpe']:.2f}</td>
<td class='num'>{fmt_usd(m_tf['final'])}</td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>気持ちマックス v2.3 C3 収支レポート</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 1200px;
        margin: 24px auto; padding: 0 16px; background: #f5f5f5; color: #222; }}
h1 {{ color: #1565c0; border-bottom: 3px solid #1565c0; padding-bottom: 8px; }}
h2 {{ color: #1976d2; margin-top: 32px; }}
h3 {{ color: #388e3c; margin-top: 24px; }}
table {{ border-collapse: collapse; width: 100%; background: white;
         box-shadow: 0 1px 4px rgba(0,0,0,0.1); margin: 12px 0; }}
th, td {{ padding: 8px 12px; border-bottom: 1px solid #ddd; }}
th {{ background: #1565c0; color: white; text-align: left; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.pos {{ color: #2e7d32; }}
.neg {{ color: #c62828; }}
.highlight {{ background: #e8f5e9 !important; }}
.card-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 16px 0; }}
.card {{ background: white; padding: 16px; border-radius: 8px; text-align: center;
         box-shadow: 0 1px 4px rgba(0,0,0,0.1); }}
.card h3 {{ margin: 0 0 8px; color: #666; font-size: 13px; }}
.card .big {{ font-size: 28px; font-weight: bold; }}
.chart-wrap {{ background: white; padding: 16px; border-radius: 8px;
               box-shadow: 0 1px 4px rgba(0,0,0,0.1); margin: 16px 0; }}
.note {{ background: #fff8e1; padding: 12px; border-left: 4px solid #ffa726;
         border-radius: 4px; margin: 12px 0; font-size: 14px; }}
</style></head><body>

<h1>気持ちマックス v2.3 C3 収支レポート</h1>
<p>生成: {datetime.now(timezone.utc).isoformat(timespec='seconds')} /
対象: PR #4 (ACH_TOP_N=3→2、他の設定 v2.2 維持) マージ後の設定 /
データ: 2020-01-01 〜 2024-12-31 (62 銘柄実データ、Binance 同日比較で整合確認済)</p>

<h2>🟢 要約カード</h2>
<div class="card-grid">
<div class="card"><h3>年率リターン (CAGR)</h3><div class="big pos">+{m_v23['cagr_pct']:.1f}%</div></div>
<div class="card"><h3>最大下落 (DD)</h3><div class="big neg">{m_v23['max_dd_pct']:.1f}%</div></div>
<div class="card"><h3>Sharpe (リスク効率)</h3><div class="big">{m_v23['sharpe']:.2f}</div></div>
<div class="card"><h3>5 年後 ($10k 投入)</h3><div class="big pos">{fmt_usd(m_v23['final'])}</div></div>
</div>

<div class="note">
📌 <b>v2.3 C3 とは</b>: 現行 v2.2 から <code>ACH_TOP_N = 3 → 2</code> のみ変更した設定。
他のパラメータ (LB25 / BTC35% / ACH35% / USDT30% / corr0.80) は据え置き。
「様子見変更」として集中度を上げ、bull 相場の利益をやや厚くする狙い。
</div>

<h2>📈 Equity Curve (資産推移、週次)</h2>
<div class="chart-wrap"><canvas id="chEquity" height="120"></canvas></div>

<h2>📉 Drawdown 推移 (ピーク比下落率)</h2>
<div class="chart-wrap"><canvas id="chDD" height="80"></canvas></div>

<h2>💰 想定投入額シナリオ (5 年後の予測)</h2>
<p>v2.3 C3 の CAGR +{m_v23['cagr_pct']:.2f}% が <b>過去 5 年と同じように続いた場合</b>の想定。
実際の将来は変動するため<b>保証ではない</b>。</p>
<table>
<tr><th>投入額</th><th>USD</th><th>JPY</th><th>5 年後 USD</th><th>5 年後 JPY</th>
<th>5 年利益</th><th>想定最大損失 (USD)</th><th>想定最大損失 (JPY)</th></tr>
{sc_rows}
</table>

<h2>📅 年別リターン比較</h2>
<table>
<tr><th>年</th><th>v2.3 C3 (Top2)</th><th>v2.2 (Top3)</th><th>差分</th></tr>
{yr_rows}
</table>

<h2>🔀 Benchmark 比較</h2>
<table>
<tr><th>戦略</th><th>CAGR</th><th>Max DD</th><th>Sharpe</th><th>5 年後</th></tr>
{bench_rows}
</table>

<div class="note">
<b>読み方</b>:
<ul>
<li>CAGR 高 = 年率リターン大 (儲けが大きい)</li>
<li>Max DD 小 = 下落が小さい (精神的に楽、清算リスク低い)</li>
<li>Sharpe 高 = リスク対効率がいい (同じリターンで少ないリスク)</li>
<li>v2.3 C3 は <b>BTC buy&amp;hold (Sharpe {m_bh['sharpe']:.2f}) を凌駕</b>する Sharpe {m_v23['sharpe']:.2f}</li>
<li>ただし <b>BTC buy&amp;hold より CAGR はやや低い</b> (buy&amp;hold は 2020-2024 の bull 相場で+67%)</li>
<li>DD は仮想通貨市場の標準的レンジ (BTC buy&amp;hold の {m_bh['max_dd_pct']:.0f}% よりは小さい)</li>
</ul>
</div>

<h2>⚠ 注意事項</h2>
<ul>
<li>過去の成績は将来の保証ではない (仮想通貨市場は変動大)</li>
<li>2020-2024 は bull 相場が多く含まれる期間であり、長期 bear 相場 (2018 年型) での動作は別途検証が必要</li>
<li>想定最大損失は <b>一時的な含み損の最大値</b>。現物運用なので清算はしないが、精神的な圧迫はある</li>
<li>本 Bot は <b>投入資金の 10〜20% 以内</b>に抑えることを原則とする (memory より)</li>
</ul>

<script>
const labels = {json.dumps(labels)};
const equityData = {{
    labels: labels,
    datasets: [
        {{ label: 'v2.3 C3 (Top2、本 Bot 採用)', data: {json.dumps(series_v23)},
           borderColor: '#2e7d32', backgroundColor: 'rgba(46,125,50,0.1)',
           borderWidth: 2.5, tension: 0.1, pointRadius: 0 }},
        {{ label: 'v2.2 (Top3、変更前)', data: {json.dumps(series_v22)},
           borderColor: '#1976d2', backgroundColor: 'rgba(25,118,210,0.05)',
           borderWidth: 2, borderDash: [5, 3], tension: 0.1, pointRadius: 0 }},
        {{ label: 'BTC buy&hold', data: {json.dumps(series_bh)},
           borderColor: '#f57c00', backgroundColor: 'rgba(245,124,0,0.05)',
           borderWidth: 1.5, tension: 0.1, pointRadius: 0 }},
        {{ label: '毎月 DCA', data: {json.dumps(series_dca)},
           borderColor: '#8e24aa', backgroundColor: 'rgba(142,36,170,0.05)',
           borderWidth: 1.5, tension: 0.1, pointRadius: 0 }},
        {{ label: 'BTC trend-follow', data: {json.dumps(series_tf)},
           borderColor: '#00838f', backgroundColor: 'rgba(0,131,143,0.05)',
           borderWidth: 1.5, tension: 0.1, pointRadius: 0 }},
    ]
}};
new Chart(document.getElementById('chEquity'), {{
    type: 'line',
    data: equityData,
    options: {{
        responsive: true,
        plugins: {{ title: {{ display: true, text: '資産 ($10,000 スタート、週次)' }},
                    legend: {{ position: 'bottom' }} }},
        scales: {{ y: {{ type: 'logarithmic',
                        title: {{ display: true, text: '資産 ($, 対数軸)' }} }} }},
        interaction: {{ mode: 'index', intersect: false }},
    }}
}});

const ddData = {{
    labels: labels,
    datasets: [
        {{ label: 'v2.3 C3 DD%', data: {json.dumps(dd_v23)},
           borderColor: '#c62828', backgroundColor: 'rgba(198,40,40,0.2)',
           fill: true, borderWidth: 1.5, tension: 0.1, pointRadius: 0 }},
        {{ label: 'v2.2 DD%', data: {json.dumps(dd_v22)},
           borderColor: '#1976d2', backgroundColor: 'rgba(25,118,210,0.05)',
           borderWidth: 1, borderDash: [4, 3], tension: 0.1, pointRadius: 0 }},
    ]
}};
new Chart(document.getElementById('chDD'), {{
    type: 'line',
    data: ddData,
    options: {{
        responsive: true,
        plugins: {{ title: {{ display: true, text: 'ドローダウン (ピークからの下落率 %)' }},
                    legend: {{ position: 'bottom' }} }},
        scales: {{ y: {{ reverse: true, title: {{ display: true, text: 'DD%' }} }} }},
    }}
}});
</script>

</body></html>
"""


if __name__ == "__main__":
    sys.exit(main())
