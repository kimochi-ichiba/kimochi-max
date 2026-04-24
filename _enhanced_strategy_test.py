"""
_enhanced_strategy_test.py — 案 A (milestone_extraction) + 案 B (動的 regime) の検証
===================================================================================

目的: 現行 v2.3 C3 (Top2) を基礎に、以下 2 つの改良を検証:
  案 A: milestone_extraction - ACH 時価が過去 peak から 30% 下落で全 USDT 退避
  案 B: 動的レジーム - bull 時 ACH 50%, bear 時 ACH 0% (USDT 100%)
  案 A+B 併用

比較: 4 条件 × 2 期間 (2020-2024 bull、2018-2022 bear) = 8 runs
出力: results/enhanced_strategy_report.html
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

PROJECT = Path(__file__).resolve().parent
RESULTS = PROJECT / "results"
OUT_HTML = RESULTS / "enhanced_strategy_report.html"
OUT_JSON = RESULTS / "enhanced_strategy_report.json"

FEE = 0.0010
SLIP = 0.0005


def _reb_key(date, days):
    doy = date.dayofyear + (date.year - 2020) * 366
    return doy // days


def calc_yearly_rets(equity_curve, initial):
    df = pd.DataFrame(equity_curve).set_index("ts")
    df.index = pd.to_datetime(df.index)
    by_year = df.resample("YE").last()
    yearly = {}
    prev = initial
    for ts, row in by_year.iterrows():
        y = str(ts.year)
        eq = row["equity"]
        if prev > 0:
            yearly[y] = round((eq / prev - 1) * 100, 2)
        prev = eq
    return yearly


def run_bt_enhanced(
    all_data, universe, start, end, *,
    btc_w=0.35, ach_w=0.35, usdt_w=0.30,
    top_n=2, lookback=25, rebalance_days=7,
    adx_min=15,
    corr_threshold=0.80,
    weight_method="momentum",
    ach_bear_immediate=True,
    # 拡張オプション
    dynamic_regime=False,       # 案 B: Bull/Bear で weight 切替
    trail_stop_ach=None,        # 案 A: ACH 時価が peak から X 下落で全退避 (0.30 = 30%)
    # Bull / Bear 時の weight (dynamic_regime=True で有効)
    bull_ach_w=0.50,
    initial=10000.0,
):
    """拡張版バックテスト。run_bt_v22_exact をベースに dynamic_regime と
    trail_stop_ach を追加。BTC 戦略の基本ロジックは据え置き。
    """
    import _iter54_comprehensive as M
    M.CORR_THRESHOLD = corr_threshold

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    if not dates:
        return {}

    from _iter54_comprehensive import select_top

    # 初期 cash 配分
    btc_cash = initial * btc_w
    btc_qty = 0.0
    ach_cash = initial * ach_w
    positions = {}
    usdt_cash = initial * usdt_w
    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0
    n_bear_exits = 0
    n_milestone_exits = 0
    last_key = None

    # milestone_extraction 用 peak トラッキング
    ach_peak = initial * ach_w  # ACH 部分の過去最大時価

    for date in dates:
        btc_r = btc_df.loc[date]
        price = btc_r["close"]
        ema200 = btc_r.get("ema200")
        btc_bullish = not pd.isna(ema200) and price > ema200

        # BTC 戦略 (通常通り)
        if btc_qty == 0 and btc_bullish:
            buy_p = price * (1 + SLIP)
            btc_qty = btc_cash / buy_p * (1 - FEE)
            btc_cash = 0
            n_trades += 1
        elif btc_qty > 0 and not btc_bullish:
            sell_p = price * (1 - SLIP)
            btc_cash += btc_qty * sell_p * (1 - FEE)
            btc_qty = 0
            n_trades += 1

        # v2.2: ACH 即時ベア退避
        if ach_bear_immediate and not btc_bullish and positions:
            for sym in list(positions.keys()):
                df = all_data[sym]
                if date in df.index:
                    p = df.loc[date, "close"] * (1 - SLIP)
                    ach_cash += positions[sym] * p * (1 - FEE)
                    n_trades += 1
                    positions.pop(sym)
                    n_bear_exits += 1
            # 案 B dynamic_regime: bear で ACH 全て USDT へ退避
            if dynamic_regime and ach_cash > 0:
                usdt_cash += ach_cash
                ach_cash = 0

        # 日々の金利
        if btc_qty == 0:
            btc_cash *= (1 + 0.03/365)
        usdt_cash *= (1 + 0.03/365)

        # ACH 時価を計算
        ach_value = ach_cash
        for sym, qty in positions.items():
            df = all_data[sym]
            if date in df.index:
                ach_value += qty * df.loc[date, "close"]

        # 案 A: milestone_extraction (trail stop)
        if trail_stop_ach is not None and ach_value > 0:
            if ach_value > ach_peak:
                ach_peak = ach_value
            # peak から trail_stop_ach (例: 30%) 下落で全退避
            if ach_peak > 0 and (ach_peak - ach_value) / ach_peak >= trail_stop_ach:
                # ACH ポジション全売却 + ACH cash も含めて USDT へ
                for sym in list(positions.keys()):
                    df = all_data[sym]
                    if date in df.index:
                        p = df.loc[date, "close"] * (1 - SLIP)
                        ach_cash += positions[sym] * p * (1 - FEE)
                        n_trades += 1
                        positions.pop(sym)
                usdt_cash += ach_cash
                ach_cash = 0
                ach_peak = 0  # リセット
                n_milestone_exits += 1

        # ACH リバランス
        cur_key = _reb_key(date, rebalance_days)
        if cur_key != last_key:
            # 全 ACH 売却 (ポジションがあれば)
            for sym in list(positions.keys()):
                df = all_data[sym]
                if date in df.index:
                    p = df.loc[date, "close"] * (1 - SLIP)
                    ach_cash += positions[sym] * p * (1 - FEE)
                    n_trades += 1
                    positions.pop(sym)

            if not btc_bullish:
                # bear 期は ACH 買わない
                # 案 B の場合、ach_cash を USDT へ (既に ach_bear_immediate で処理済のはず)
                if dynamic_regime and ach_cash > 0:
                    usdt_cash += ach_cash
                    ach_cash = 0
                last_key = cur_key
            else:
                # 案 B dynamic_regime: ACH weight を引き上げ、USDT から補填
                if dynamic_regime:
                    total_eq = btc_cash + btc_qty * price + ach_cash + usdt_cash
                    target_ach_cash = total_eq * bull_ach_w
                    if ach_cash < target_ach_cash:
                        shortage = target_ach_cash - ach_cash
                        take = min(shortage, usdt_cash)
                        ach_cash += take
                        usdt_cash -= take

                # Top N 買い付け
                sel = select_top(all_data, universe, date, top_n, lookback,
                                 adx_min, 0, "momentum", False)
                if sel:
                    if weight_method == "momentum":
                        pos_rets = [max(r, 0.01) for _, r in sel]
                        total_w = sum(pos_rets)
                        weights = [r/total_w for r in pos_rets]
                    else:
                        weights = [1.0/len(sel)] * len(sel)

                    for (sym, _), w in zip(sel, weights):
                        df = all_data[sym]
                        if date in df.index:
                            p_buy = df.loc[date, "close"] * (1 + SLIP)
                            cost = ach_cash * w
                            if cost > 0:
                                qty = cost / p_buy * (1 - FEE)
                                positions[sym] = qty
                                n_trades += 1
                    used = sum(ach_cash * w for w in weights)
                    ach_cash -= used
                last_key = cur_key
                # 案 A: milestone リセット (新規エントリで peak を現在時価に)
                if trail_stop_ach is not None:
                    new_ach_value = ach_cash
                    for sym, qty in positions.items():
                        df = all_data[sym]
                        if date in df.index:
                            new_ach_value += qty * df.loc[date, "close"]
                    ach_peak = max(ach_peak, new_ach_value)

        # 総資産記録
        ach_value = ach_cash
        for sym, qty in positions.items():
            df = all_data[sym]
            if date in df.index:
                ach_value += qty * df.loc[date, "close"]
        total = btc_cash + btc_qty * price + ach_value + usdt_cash
        equity_curve.append({"ts": date, "equity": total})

    # メトリクス
    ew_list = equity_curve[1:]  # 初期ポイントを除く
    eq_df = pd.DataFrame(ew_list).set_index("ts")
    eq_df.index = pd.to_datetime(eq_df.index)
    eq_weekly = eq_df["equity"].resample("W").last().dropna()
    equity_weekly = [{"ts": str(ts)[:10], "equity": float(v)} for ts, v in eq_weekly.items()]

    eq = [pt["equity"] for pt in equity_weekly]
    if len(eq) < 2:
        return {"equity_weekly": [], "n_trades": n_trades, "n_bear_exits": n_bear_exits, "n_milestone_exits": n_milestone_exits}

    final = eq[-1]
    years = len(eq) / 52
    cagr = (final / initial) ** (1 / years) * 100 - 100 if years > 0 and final > 0 else 0
    peak, max_dd = eq[0], 0.0
    for e in eq:
        if e > peak: peak = e
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd
    rets = [eq[i] / eq[i-1] - 1 for i in range(1, len(eq)) if eq[i-1] > 0]
    sharpe = (
        statistics.mean(rets) / statistics.pstdev(rets) * math.sqrt(52)
        if len(rets) > 1 and statistics.pstdev(rets) > 0 else 0
    )
    yearly = calc_yearly_rets(ew_list, initial)

    return {
        "equity_weekly": equity_weekly,
        "cagr_pct": round(cagr, 2),
        "max_dd_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "final": round(final, 0),
        "yearly": yearly,
        "n_trades": n_trades,
        "n_bear_exits": n_bear_exits,
        "n_milestone_exits": n_milestone_exits,
    }


def main() -> int:
    print("=" * 60)
    print("案 A (milestone) + 案 B (動的 regime) 比較テスト")
    print("=" * 60)

    # データロード + 正規化
    def load_norm(path):
        with open(path, "rb") as f:
            d = pickle.load(f)
        for sym in list(d.keys()):
            df = d[sym]
            if df.index.tz is not None:
                df = df.copy()
                df.index = df.index.tz_localize(None)
                d[sym] = df
        if "BTC/USDT" in d and "ema200" not in d["BTC/USDT"].columns:
            btc = d["BTC/USDT"].copy()
            btc["ema200"] = btc["close"].ewm(span=200, adjust=False).mean()
            d["BTC/USDT"] = btc
        return d

    bull_data = load_norm(RESULTS / "_iter61_cache.pkl")
    bear_data = load_norm(RESULTS / "_bear_test_cache.pkl")
    remove = {"MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"}
    uni_bull = sorted(s for s in bull_data.keys() if s != "BTC/USDT" and s not in remove)
    uni_bear = sorted(s for s in bear_data.keys() if s != "BTC/USDT" and s not in remove)

    configs = [
        ("Base (現行 C3)", {}),
        ("B-base: regime bull_w=0.50", {"dynamic_regime": True, "bull_ach_w": 0.50}),
        ("B1: bull_w=0.55", {"dynamic_regime": True, "bull_ach_w": 0.55}),
        ("B2: bull_w=0.60", {"dynamic_regime": True, "bull_ach_w": 0.60}),
        ("B3: lookback=20", {"dynamic_regime": True, "bull_ach_w": 0.50, "lookback": 20}),
        ("B4: lookback=30", {"dynamic_regime": True, "bull_ach_w": 0.50, "lookback": 30}),
        ("B5: corr=0.70", {"dynamic_regime": True, "bull_ach_w": 0.50, "corr_threshold": 0.70}),
        ("B6: top_n=3", {"dynamic_regime": True, "bull_ach_w": 0.50, "top_n": 3}),
    ]

    results = {}

    for period_label, data, universe, start, end in [
        ("Bull 2020-2024", bull_data, uni_bull, "2020-01-01", "2024-12-31"),
        ("Bear 2018-2022", bear_data, uni_bear, "2018-01-01", "2022-12-31"),
    ]:
        print(f"\n--- {period_label} ---")
        results[period_label] = {}
        for label, extra_opts in configs:
            base_opts = dict(
                btc_w=0.35, ach_w=0.35, usdt_w=0.30,
                top_n=2, lookback=25, rebalance_days=7,
                adx_min=15, corr_threshold=0.80,
                weight_method="momentum", ach_bear_immediate=True,
                initial=10000.0,
            )
            base_opts.update(extra_opts)
            r = run_bt_enhanced(data, universe, start, end, **base_opts)
            results[period_label][label] = r
            mi_exits = r.get("n_milestone_exits", 0)
            be_exits = r.get("n_bear_exits", 0)
            print(
                f"  {label:<30} CAGR {r['cagr_pct']:+7.2f}%  "
                f"DD {r['max_dd_pct']:6.2f}%  Sh {r['sharpe']:.2f}  "
                f"final ${r['final']:,.0f}  "
                f"bear退避 {be_exits}  milestone退避 {mi_exits}"
            )

    # HTML 生成
    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "results": results,
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
    results = payload["results"]

    def fmt_pct(v): return f"{v:+.2f}%"
    def _cls(v): return "pos" if v >= 0 else "neg"

    # 比較表
    tables = []
    for period_label, period_results in results.items():
        header = f"<h2>{period_label}</h2><table>"
        header += "<tr><th>設定</th><th>CAGR</th><th>MaxDD</th><th>Sharpe</th><th>5 年後</th><th>取引</th><th>bear 退避</th><th>milestone 退避</th></tr>"
        rows = []
        best_cagr = max(r["cagr_pct"] for r in period_results.values())
        for label, r in period_results.items():
            is_best = r["cagr_pct"] == best_cagr
            hl = " class='highlight'" if is_best else ""
            rows.append(
                f"<tr{hl}><td>{label}</td>"
                f"<td class='num {_cls(r['cagr_pct'])}'>{fmt_pct(r['cagr_pct'])}</td>"
                f"<td class='num neg'>{r['max_dd_pct']:.2f}%</td>"
                f"<td class='num'>{r['sharpe']:.2f}</td>"
                f"<td class='num'>${r['final']:,.0f}</td>"
                f"<td class='num'>{r['n_trades']}</td>"
                f"<td class='num'>{r.get('n_bear_exits', 0)}</td>"
                f"<td class='num'>{r.get('n_milestone_exits', 0)}</td></tr>"
            )
        tables.append(header + "".join(rows) + "</table>")

    # Chart.js equity curves
    bull_results = results.get("Bull 2020-2024", {})
    bear_results = results.get("Bear 2018-2022", {})

    def curves_to_js(period_results):
        labels_set = None
        for r in period_results.values():
            ts_list = [pt["ts"] for pt in r.get("equity_weekly", [])]
            if labels_set is None or len(ts_list) > len(labels_set):
                labels_set = ts_list
        datasets = []
        colors = ["#2e7d32", "#1976d2", "#f57c00", "#c62828"]
        for idx, (label, r) in enumerate(period_results.items()):
            ec = r.get("equity_weekly", [])
            m = {pt["ts"]: pt["equity"] for pt in ec}
            data = [round(m[ts], 2) if ts in m else None for ts in labels_set]
            datasets.append({
                "label": label,
                "data": data,
                "borderColor": colors[idx % len(colors)],
                "borderWidth": 2,
                "tension": 0.1,
                "pointRadius": 0,
            })
        return labels_set, datasets

    bull_labels, bull_ds = curves_to_js(bull_results)
    bear_labels, bear_ds = curves_to_js(bear_results)

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>拡張戦略比較 (案 A + B)</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 1200px;
        margin: 24px auto; padding: 0 16px; background: #f5f5f5; color: #222; }}
h1 {{ color: #1565c0; border-bottom: 3px solid #1565c0; padding-bottom: 8px; }}
h2 {{ color: #1976d2; margin-top: 32px; }}
table {{ border-collapse: collapse; width: 100%; background: white;
         box-shadow: 0 1px 4px rgba(0,0,0,0.1); margin: 12px 0; }}
th, td {{ padding: 8px 12px; border-bottom: 1px solid #ddd; }}
th {{ background: #1565c0; color: white; text-align: left; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.pos {{ color: #2e7d32; }}
.neg {{ color: #c62828; }}
.highlight {{ background: #e8f5e9 !important; font-weight: bold; }}
.chart-wrap {{ background: white; padding: 16px; border-radius: 8px;
               box-shadow: 0 1px 4px rgba(0,0,0,0.1); margin: 16px 0; }}
</style></head><body>
<h1>拡張戦略比較: 案 A (milestone trail) + 案 B (動的 regime)</h1>
<p>生成: {payload['meta']['generated_at']} / 基本設定: Top2/LB25/BTC35/ACH35/USDT30/corr0.80/momentum</p>

<h3>案の説明</h3>
<ul>
<li><b>A: milestone trail 30%</b> — ACH 時価が過去 peak から 30% 下落で全 USDT 退避 (trailing stop)</li>
<li><b>B: 動的 regime (bull 50%)</b> — Bull 時 ACH 比率 50% に引き上げ (USDT から補填)、Bear 時 ACH 0% で全退避</li>
<li><b>A+B 併用</b> — 両方適用</li>
</ul>

{''.join(tables)}

<h2>Equity Curve 比較</h2>

<h3>Bull 2020-2024</h3>
<div class="chart-wrap"><canvas id="chBull" height="120"></canvas></div>

<h3>Bear 2018-2022</h3>
<div class="chart-wrap"><canvas id="chBear" height="120"></canvas></div>

<script>
new Chart(document.getElementById('chBull'), {{
    type: 'line',
    data: {{ labels: {json.dumps(bull_labels)}, datasets: {json.dumps(bull_ds)} }},
    options: {{ responsive: true,
                plugins: {{ title: {{ display: true, text: 'Bull 期 (2020-2024) equity $10k スタート' }},
                           legend: {{ position: 'bottom' }} }},
                scales: {{ y: {{ type: 'logarithmic', title: {{ display: true, text: '$' }} }} }},
                interaction: {{ mode: 'index', intersect: false }} }},
}});
new Chart(document.getElementById('chBear'), {{
    type: 'line',
    data: {{ labels: {json.dumps(bear_labels)}, datasets: {json.dumps(bear_ds)} }},
    options: {{ responsive: true,
                plugins: {{ title: {{ display: true, text: 'Bear 期 (2018-2022) equity $10k スタート' }},
                           legend: {{ position: 'bottom' }} }},
                scales: {{ y: {{ type: 'logarithmic', title: {{ display: true, text: '$' }} }} }},
                interaction: {{ mode: 'index', intersect: false }} }},
}});
</script>

</body></html>
"""


if __name__ == "__main__":
    sys.exit(main())
