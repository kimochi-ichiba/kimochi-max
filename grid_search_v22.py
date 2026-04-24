"""
grid_search_v22.py — v2.2 設定最適化グリッドサーチ + Benchmark 比較
==================================================================

段階的グリッドサーチ (計 ~40 組):
  Stage 1: BTC/ACH/USDT 重み配分
  Stage 2: top_n × lookback
  Stage 3: rebalance_days × corr_threshold
  Stage 4: weight_method × ADX_MIN

各セルで以下を集計:
  - IS 窓 3 つ + OOS 窓 3 つ (walk-forward)
  - 10 項目メトリクス (metrics.compute_all_metrics)
  - stability (感度分析 + 本番用/壊れやすい判定)

Benchmark 4 種 (benchmarks.py):
  - BTC buy & hold
  - 毎月 DCA
  - 単純トレンドフォロー (BTC EMA200)
  - ランダムエントリー (seed=42)

出力:
  - results/iter66_grid_results.json
  - results/iter66_grid_report.html
  - results/iter66_grid_report.md

実行:
  PYTHONUTF8=1 .venv/Scripts/python.exe grid_search_v22.py [--smoke]
"""
from __future__ import annotations

import argparse
import json
import pickle
import statistics
import sys
import time
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmarks import (
    buy_hold_benchmark,
    monthly_dca_benchmark,
    random_entry_benchmark,
    trend_follow_benchmark,
)
from metrics import compute_all_metrics
from stability_analysis import (
    CRYPTO_PROFILE,
    classify_setting,
    overfitting_summary,
    parameter_sensitivity,
    top_n_by_metric,
)

PROJECT = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT / "results"
OUT_JSON = RESULTS_DIR / "iter66_grid_results.json"
OUT_HTML = RESULTS_DIR / "iter66_grid_report.html"
OUT_MD = RESULTS_DIR / "iter66_grid_report.md"

CACHE_CANDIDATES = [
    RESULTS_DIR / "_cache_alldata.pkl",
    RESULTS_DIR / "_iter61_cache.pkl",
]

# 62 銘柄ユニバース (demo_runner.py の ACH_UNIVERSE - UNIVERSE_REMOVE 相当)
UNIVERSE_REMOVE = {"MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"}

# ─────────────────────────────
# Walk-forward 窓定義
# ─────────────────────────────
# IS = 3 年 / OOS = 1 年、1 年刻みスライド
WINDOWS = [
    {"id": "W1", "is_start": "2020-01-01", "is_end": "2022-12-31",
     "oos_start": "2023-01-01", "oos_end": "2023-12-31"},
    {"id": "W2", "is_start": "2021-01-01", "is_end": "2023-12-31",
     "oos_start": "2024-01-01", "oos_end": "2024-12-31"},
    {"id": "W3", "is_start": "2020-01-01", "is_end": "2023-12-31",
     "oos_start": "2024-01-01", "oos_end": "2024-12-31"},
]

# ─────────────────────────────
# パラメータグリッド (段階探索)
# ─────────────────────────────

# Stage 1: 重み配分 (合計 1.0 制約、USDT ≥ 0.2 制約)
WEIGHT_GRID = [
    (0.30, 0.30, 0.40),
    (0.35, 0.35, 0.30),
    (0.40, 0.40, 0.20),
    (0.30, 0.40, 0.30),
    (0.40, 0.30, 0.30),
]

# Stage 2: top_n × lookback
TOP_N_GRID = [2, 3, 4]
LOOKBACK_GRID = [20, 25, 30, 35]

# Stage 3: rebalance × corr_threshold
REBALANCE_GRID = [5, 7, 14]
CORR_GRID = [0.70, 0.80, 0.90]

# Stage 4: weight_method × ADX_MIN
WEIGHT_METHOD_GRID = ["equal", "momentum"]
ADX_MIN_GRID = [10, 15, 20]

# ─────────────────────────────
# データ読み込み
# ─────────────────────────────
def load_cache() -> dict[str, pd.DataFrame]:
    """利用可能なキャッシュを優先順で読み込む。
    BTC に EMA200 が無ければ付与する。
    """
    for path in CACHE_CANDIDATES:
        if path.exists():
            with open(path, "rb") as f:
                data = pickle.load(f)
            # DataFrame dict の想定。BTC に ema200 が無ければ足す
            if "BTC/USDT" in data:
                df = data["BTC/USDT"]
                if "ema200" not in df.columns:
                    df = df.copy()
                    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
                    data["BTC/USDT"] = df
            print(f"キャッシュ読込: {path.name} ({len(data)} 銘柄)")
            return data
    raise FileNotFoundError(
        "データキャッシュが見つかりません: "
        f"{[str(p) for p in CACHE_CANDIDATES]}"
    )


def make_universe(all_data: dict[str, pd.DataFrame]) -> list[str]:
    """キャッシュから ACH ユニバースを作る (BTC は含めない、UNIVERSE_REMOVE を除外)."""
    return sorted(
        s for s in all_data.keys()
        if s != "BTC/USDT" and s not in UNIVERSE_REMOVE
    )


def make_btc_ema_bool(all_data: dict[str, pd.DataFrame]) -> pd.Series:
    """regime ラベル用の BTC > EMA200 bool Series を生成."""
    btc = all_data.get("BTC/USDT")
    if btc is None:
        return pd.Series(dtype=bool)
    return (btc["close"] > btc["ema200"]).fillna(False)


# ─────────────────────────────
# セル実行: run_bt_v22_exact のラッパー
# ─────────────────────────────
def _run_cell_raw(
    all_data: dict,
    universe: list[str],
    start: str,
    end: str,
    *,
    btc_w: float,
    ach_w: float,
    usdt_w: float,
    top_n: int,
    lookback: int,
    rebalance_days: int,
    corr_threshold: float,
    weight_method: str,
    adx_min: int,
    ach_bear_immediate: bool = True,
    initial: float = 10_000.0,
) -> dict[str, Any]:
    """_iter59_v22_verify.run_bt_v22_exact を呼び、equity_curve + trades 相当を返す."""
    # 遅延 import (循環回避)
    import _iter59_v22_verify as V

    r = V.run_bt_v22_exact(
        all_data, universe, start, end,
        btc_w=btc_w, ach_w=ach_w, usdt_w=usdt_w,
        top_n=top_n, lookback=lookback,
        rebalance_days=rebalance_days,
        adx_min=adx_min,
        corr_threshold=corr_threshold,
        weight_method=weight_method,
        ach_bear_immediate=ach_bear_immediate,
        initial=initial,
    )
    # run_bt_v22_exact は trades を返さないので空 list
    # 代わりに weekly equity から equity_curve を構成
    equity_weekly = r.get("equity_weekly", [])
    equity_curve = [
        {"ts": pd.to_datetime(pt["ts"]), "equity": float(pt["equity"])}
        for pt in equity_weekly
    ]
    return {
        "equity_curve": equity_curve,
        "trades": [],  # run_bt_v22_exact は trades 詳細を返さない
        "n_trades": r.get("n_trades", 0),
        "n_bear_exits": r.get("n_bear_exits", 0),
        "final": r.get("final", initial),
    }


def run_cell_with_metrics(
    all_data: dict,
    universe: list[str],
    start: str,
    end: str,
    btc_ema_bool: pd.Series,
    **params: Any,
) -> dict[str, Any]:
    """1 セルを実行して compute_all_metrics を返す."""
    raw = _run_cell_raw(all_data, universe, start, end, **params)
    metrics = compute_all_metrics(
        raw["equity_curve"],
        raw["trades"],
        btc_ema200_bool=btc_ema_bool,
    )
    metrics["n_trades"] = raw["n_trades"]  # run_bt 側のカウントで上書き
    metrics["n_bear_exits"] = raw["n_bear_exits"]
    metrics["final_equity"] = raw["final"]
    return metrics


# ─────────────────────────────
# グリッド生成
# ─────────────────────────────
def enumerate_stages(smoke: bool = False) -> list[dict[str, Any]]:
    """段階探索のセルを enumerate. smoke=True で各 Stage 1-2 組のみ."""
    cells: list[dict[str, Any]] = []
    default = {
        "btc_w": 0.35, "ach_w": 0.35, "usdt_w": 0.30,
        "top_n": 3, "lookback": 25, "rebalance_days": 7,
        "corr_threshold": 0.80, "weight_method": "momentum",
        "adx_min": 15,
    }

    # Stage 1: 重み配分
    weight_list = WEIGHT_GRID[:2] if smoke else WEIGHT_GRID
    for btc_w, ach_w, usdt_w in weight_list:
        p = dict(default)
        p.update({"btc_w": btc_w, "ach_w": ach_w, "usdt_w": usdt_w})
        cells.append({"stage": 1, "params": p, "label": f"w({btc_w:.2f},{ach_w:.2f},{usdt_w:.2f})"})

    # Stage 2: top_n × lookback
    top_list = TOP_N_GRID[:2] if smoke else TOP_N_GRID
    lb_list = LOOKBACK_GRID[:2] if smoke else LOOKBACK_GRID
    for top_n, lookback in product(top_list, lb_list):
        p = dict(default)
        p.update({"top_n": top_n, "lookback": lookback})
        cells.append({"stage": 2, "params": p, "label": f"top{top_n}_lb{lookback}"})

    # Stage 3: rebalance × corr
    reb_list = REBALANCE_GRID[:2] if smoke else REBALANCE_GRID
    corr_list = CORR_GRID[:2] if smoke else CORR_GRID
    for rb, ct in product(reb_list, corr_list):
        p = dict(default)
        p.update({"rebalance_days": rb, "corr_threshold": ct})
        cells.append({"stage": 3, "params": p, "label": f"rb{rb}_corr{ct}"})

    # Stage 4: weight_method × ADX
    wm_list = WEIGHT_METHOD_GRID[:1] if smoke else WEIGHT_METHOD_GRID
    adx_list = ADX_MIN_GRID[:2] if smoke else ADX_MIN_GRID
    for wm, adx in product(wm_list, adx_list):
        p = dict(default)
        p.update({"weight_method": wm, "adx_min": adx})
        cells.append({"stage": 4, "params": p, "label": f"{wm}_adx{adx}"})

    # USDT 比率 < 0.2 のセルを除外 (清算リスク回避、固定でも残すが Stage 1 のみ対象)
    cells = [c for c in cells if c["params"]["usdt_w"] >= 0.20]
    return cells


# ─────────────────────────────
# メイン実行
# ─────────────────────────────
def run_grid(
    all_data: dict,
    universe: list[str],
    smoke: bool = False,
    with_sensitivity: bool = True,
) -> dict[str, Any]:
    cells = enumerate_stages(smoke=smoke)
    btc_ema_bool = make_btc_ema_bool(all_data)
    t0 = time.time()

    # Benchmark 実行 (全期間, W3 の IS + OOS 合算期間)
    bench_period_start = WINDOWS[-1]["is_start"]
    bench_period_end = WINDOWS[-1]["oos_end"]
    benchmarks = run_benchmarks(
        all_data, universe, bench_period_start, bench_period_end
    )

    cell_results: list[dict[str, Any]] = []
    for i, cell in enumerate(cells):
        params = cell["params"]
        print(f"[{i + 1}/{len(cells)}] stage{cell['stage']} {cell['label']} ...", flush=True)
        per_window: dict[str, dict[str, Any]] = {}
        for w in WINDOWS:
            is_m = run_cell_with_metrics(
                all_data, universe, w["is_start"], w["is_end"],
                btc_ema_bool, **params,
            )
            oos_m = run_cell_with_metrics(
                all_data, universe, w["oos_start"], w["oos_end"],
                btc_ema_bool, **params,
            )
            per_window[w["id"]] = {"is": is_m, "oos": oos_m}

        # 感度分析 (W3 で実施、代表 3 param)
        sensitivity = None
        if with_sensitivity:
            def cell_runner(**p):
                return run_cell_with_metrics(
                    all_data, universe,
                    WINDOWS[-1]["oos_start"], WINDOWS[-1]["oos_end"],
                    btc_ema_bool, **p,
                )
            try:
                sensitivity = parameter_sensitivity(
                    cell_runner, params,
                    sensitivity_targets=("top_n", "lookback", "corr_threshold"),
                )
            except Exception as e:
                sensitivity = {"error": str(e), "max_relative_change_pct": 0.0}

        # W3 の IS/OOS で判定 (仮想通貨現物運用向けプロファイル)
        classification = classify_setting(
            per_window["W3"]["is"],
            per_window["W3"]["oos"],
            sensitivity,
            params["usdt_w"],
            profile=CRYPTO_PROFILE,
        )

        cell_results.append({
            "stage": cell["stage"],
            "label": cell["label"],
            "params": params,
            "windows": per_window,
            "sensitivity": sensitivity,
            "classification": classification["classification"],
            "fragile_reasons": classification["fragile_reasons"],
            "production_checks": classification["production_checks"],
            "metrics_snapshot": classification["metrics_snapshot"],
        })

    elapsed = time.time() - t0

    return {
        "windows": WINDOWS,
        "benchmarks": benchmarks,
        "cells": cell_results,
        "top_production_ready": top_n_by_metric(
            cell_results, "production_ready", "oos_sharpe", 5
        ),
        "top_fragile": top_n_by_metric(
            cell_results, "fragile", "oos_sharpe", 5
        ),
        "overfitting_summary": overfitting_summary(cell_results),
        "meta": {
            "elapsed_sec": round(elapsed, 1),
            "num_cells": len(cells),
            "smoke_mode": smoke,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def run_benchmarks(
    all_data: dict,
    universe: list[str],
    start: str,
    end: str,
    initial: float = 10_000.0,
) -> dict[str, dict[str, Any]]:
    """Benchmark 4 種を実行し metrics まで計算."""
    out: dict[str, dict[str, Any]] = {}
    btc_ema_bool = make_btc_ema_bool(all_data)

    bm_runs = [
        ("buy_hold_BTC", lambda: buy_hold_benchmark(
            all_data, "BTC/USDT", start, end, initial)),
        ("monthly_dca_BTC", lambda: monthly_dca_benchmark(
            all_data, "BTC/USDT", start, end, initial)),
        ("trend_follow_BTC", lambda: trend_follow_benchmark(
            all_data, "BTC/USDT", start, end, initial, ema_period=200)),
        ("random_entry", lambda: random_entry_benchmark(
            all_data, universe, start, end, initial,
            top_n=3, rebalance_days=7, seed=42)),
    ]

    for name, fn in bm_runs:
        try:
            br = fn()
            m = compute_all_metrics(
                br.equity_curve, br.trades, btc_ema200_bool=btc_ema_bool
            )
            # JSON serializable に (symbol/regime は dict のまま OK)
            out[name] = {
                "name": br.name,
                "metrics": m,
                "num_trades": len(br.trades),
                "equity_start": br.equity_curve[0]["equity"] if br.equity_curve else initial,
                "equity_end": br.equity_curve[-1]["equity"] if br.equity_curve else initial,
            }
        except Exception as e:
            out[name] = {"error": str(e)}

    return out


# ─────────────────────────────
# レポート生成
# ─────────────────────────────
def build_md(payload: dict[str, Any]) -> str:
    m = payload["meta"]
    cells = payload["cells"]
    benchmarks = payload["benchmarks"]

    lines = [
        "# iter66: v2.2 設定最適化 グリッドサーチ + Benchmark 比較",
        "",
        f"生成: {m['generated_at']} / 実行時間: {m['elapsed_sec']}s / セル数: {m['num_cells']}"
        + (" (smoke mode)" if m.get("smoke_mode") else ""),
        "",
        "## 判定サマリ",
        f"- production_ready: {sum(1 for c in cells if c['classification'] == 'production_ready')} cells",
        f"- fragile: {sum(1 for c in cells if c['classification'] == 'fragile')} cells",
        f"- neutral: {sum(1 for c in cells if c['classification'] == 'neutral')} cells",
        "",
        "## Benchmark 成績 (全期間)",
        "| Benchmark | CAGR% | MaxDD% | Sharpe | Trades | 最終資金 |",
        "|---|---|---|---|---|---|",
    ]
    for name, b in benchmarks.items():
        if "error" in b:
            lines.append(f"| {name} | ERROR: {b['error']} | - | - | - | - |")
            continue
        mm = b["metrics"]
        lines.append(
            f"| {name} | {mm['cagr_pct']:+.2f} | {mm['max_drawdown_pct']:.2f} "
            f"| {mm['sharpe_ratio']:.2f} | {b['num_trades']} "
            f"| ${b['equity_end']:,.0f} |"
        )

    # 本番用 Top 5 + 詳細分析
    lines += ["", "## 本番用候補 Top 5 (OOS Sharpe 降順)", ""]
    if not payload["top_production_ready"]:
        lines.append("(該当なし — 全セルが本番用条件に満たない)")
    else:
        lines.append("| Stage | Label | OOS Sharpe | OOS MaxDD | IS-OOS CAGR gap | 感度 |")
        lines.append("|---|---|---|---|---|---|")
        for c in payload["top_production_ready"]:
            ms = c["metrics_snapshot"]
            lines.append(
                f"| {c['stage']} | `{c['label']}` "
                f"| {ms['oos_sharpe']:.2f} | {ms['oos_dd_pct']:.1f}% "
                f"| {ms['is_oos_cagr_gap_pct']:+.2f}% "
                f"| {ms['sensitivity_max_change_pct']:.1f}% |"
            )
        # 各 Top の詳細展開
        lines += ["", "### Top 5 詳細分析 (W3 OOS 基準)", ""]
        for rank, c in enumerate(payload["top_production_ready"], 1):
            oos_m = c["windows"]["W3"]["oos"]
            yearly = oos_m.get("yearly_return_pct", {})
            regime = oos_m.get("regime_breakdown", {})
            sens = c.get("sensitivity", {}).get("per_param", {})
            params = c["params"]

            lines.append(f"#### {rank}. `{c['label']}` (Stage {c['stage']})")
            lines.append("")
            lines.append(
                f"**パラメータ**: "
                f"BTC/ACH/USDT={params['btc_w']:.2f}/{params['ach_w']:.2f}/{params['usdt_w']:.2f}, "
                f"top_n={params['top_n']}, lookback={params['lookback']}, "
                f"rebalance={params['rebalance_days']}d, corr={params['corr_threshold']}, "
                f"method={params['weight_method']}, adx={params['adx_min']}"
            )
            lines.append("")
            lines.append(
                f"**年別リターン (W3 OOS 期間)**: "
                + ", ".join(f"{y}: {v:+.1f}%" for y, v in yearly.items())
                if yearly else "**年別リターン**: (なし)"
            )
            lines.append("")
            if regime.get("bull") or regime.get("bear"):
                bull = regime.get("bull", {})
                bear = regime.get("bear", {})
                lines.append(
                    f"**Regime 別**: "
                    f"bull ({bull.get('weeks', 0)}週, {bull.get('return_pct', 0):+.1f}%, "
                    f"DD {bull.get('max_dd_pct', 0):.1f}%) / "
                    f"bear ({bear.get('weeks', 0)}週, {bear.get('return_pct', 0):+.1f}%, "
                    f"DD {bear.get('max_dd_pct', 0):.1f}%)"
                )
                lines.append("")
            if sens:
                lines.append("**感度 (Sharpe 相対変化)**:")
                for k, s in sens.items():
                    lines.append(
                        f"- {k}: {s['minus_param']}→{s.get('minus_metric')} / "
                        f"{s['plus_param']}→{s.get('plus_metric')} "
                        f"(max Δ {s['relative_change_pct']:.1f}%)"
                    )
                lines.append("")
            # Benchmark 比較
            lines.append(
                f"**Benchmark 対比**: このセル OOS Sharpe {c['metrics_snapshot']['oos_sharpe']:.2f} "
                f"(BTC buy&hold 全期間 Sharpe {payload['benchmarks'].get('buy_hold_BTC', {}).get('metrics', {}).get('sharpe_ratio', 0):.2f})"
            )
            lines.append("")

    # 中立候補 Top 5 (本番用にあと一歩)
    neutral_cells = [c for c in payload["cells"] if c["classification"] == "neutral"]
    neutral_top = sorted(
        neutral_cells,
        key=lambda r: r["metrics_snapshot"].get("oos_sharpe", 0.0),
        reverse=True,
    )[:5]
    lines += ["", "## 中立 Top 5 (production_ready にあと一歩)", ""]
    if not neutral_top:
        lines.append("(該当なし)")
    else:
        lines.append("| Stage | Label | OOS Sharpe | OOS MaxDD | IS-OOS gap | 不合格項目 |")
        lines.append("|---|---|---|---|---|---|")
        for c in neutral_top:
            ms = c["metrics_snapshot"]
            failed = [
                k.replace("_", " ")
                for k, v in c["production_checks"].items()
                if not v
            ]
            lines.append(
                f"| {c['stage']} | `{c['label']}` "
                f"| {ms['oos_sharpe']:.2f} | {ms['oos_dd_pct']:.1f}% "
                f"| {ms['is_oos_cagr_gap_pct']:+.2f}% "
                f"| {', '.join(failed) or '(なし)'} |"
            )
        # neutral Top の詳細展開
        lines += ["", "### 中立 Top 詳細分析 (W3 OOS 基準)", ""]
        for rank, c in enumerate(neutral_top, 1):
            oos_m = c["windows"]["W3"]["oos"]
            yearly = oos_m.get("yearly_return_pct", {})
            regime = oos_m.get("regime_breakdown", {})
            sens = c.get("sensitivity", {}).get("per_param", {})
            params = c["params"]

            lines.append(f"#### {rank}. `{c['label']}` (Stage {c['stage']})")
            lines.append("")
            lines.append(
                f"**パラメータ**: "
                f"BTC/ACH/USDT={params['btc_w']:.2f}/{params['ach_w']:.2f}/{params['usdt_w']:.2f}, "
                f"top_n={params['top_n']}, lookback={params['lookback']}, "
                f"rebalance={params['rebalance_days']}d, corr={params['corr_threshold']}, "
                f"method={params['weight_method']}, adx={params['adx_min']}"
            )
            if yearly:
                lines.append(
                    "**年別リターン (W3 OOS 期間)**: "
                    + ", ".join(f"{y}: {v:+.1f}%" for y, v in yearly.items())
                )
            if regime.get("bull") or regime.get("bear"):
                bull = regime.get("bull", {})
                bear = regime.get("bear", {})
                lines.append(
                    f"**Regime 別**: "
                    f"bull ({bull.get('weeks', 0)}週, {bull.get('return_pct', 0):+.1f}%, "
                    f"DD {bull.get('max_dd_pct', 0):.1f}%) / "
                    f"bear ({bear.get('weeks', 0)}週, {bear.get('return_pct', 0):+.1f}%, "
                    f"DD {bear.get('max_dd_pct', 0):.1f}%)"
                )
            if sens:
                lines.append("")
                lines.append("**感度 (Sharpe 相対変化)**:")
                for k, s in sens.items():
                    minus_m = s.get("minus_metric")
                    plus_m = s.get("plus_metric")
                    lines.append(
                        f"- {k}: {s['minus_param']}→"
                        f"{minus_m if minus_m is None else f'{minus_m:.2f}'} / "
                        f"{s['plus_param']}→"
                        f"{plus_m if plus_m is None else f'{plus_m:.2f}'} "
                        f"(max Δ {s['relative_change_pct']:.1f}%)"
                    )
            lines.append("")

    # 壊れやすい Top 5
    lines += ["", "## 壊れやすい Top 5 (OOS Sharpe 降順、ただし判定は fragile)", ""]
    if not payload["top_fragile"]:
        lines.append("(該当なし)")
    else:
        lines.append("| Stage | Label | OOS Sharpe | OOS MaxDD | 理由 |")
        lines.append("|---|---|---|---|---|")
        for c in payload["top_fragile"]:
            ms = c["metrics_snapshot"]
            lines.append(
                f"| {c['stage']} | `{c['label']}` "
                f"| {ms['oos_sharpe']:.2f} | {ms['oos_dd_pct']:.1f}% "
                f"| {'; '.join(c['fragile_reasons'])} |"
            )

    lines += [
        "",
        "## 過学習診断",
        f"IS-OOS CAGR gap 分布: {payload['overfitting_summary']}",
        "",
        "## 窓定義",
    ]
    for w in payload["windows"]:
        lines.append(
            f"- **{w['id']}**: IS {w['is_start']}〜{w['is_end']} / "
            f"OOS {w['oos_start']}〜{w['oos_end']}"
        )

    return "\n".join(lines)


def build_html(payload: dict[str, Any]) -> str:
    m = payload["meta"]
    cells = payload["cells"]
    benchmarks = payload["benchmarks"]

    # Benchmark カード
    bm_rows = []
    for name, b in benchmarks.items():
        if "error" in b:
            bm_rows.append(f"<tr><td>{name}</td><td colspan='5'>ERROR: {b['error']}</td></tr>")
            continue
        mm = b["metrics"]
        bm_rows.append(
            f"<tr><td>{name}</td>"
            f"<td class='num'>{mm['cagr_pct']:+.2f}%</td>"
            f"<td class='num'>{mm['max_drawdown_pct']:.2f}%</td>"
            f"<td class='num'>{mm['sharpe_ratio']:.2f}</td>"
            f"<td class='num'>{b['num_trades']}</td>"
            f"<td class='num'>${b['equity_end']:,.0f}</td></tr>"
        )

    # セル行
    cell_rows = []
    for c in cells:
        ms = c["metrics_snapshot"]
        cls = c["classification"]
        color = {
            "production_ready": "#c8e6c9",
            "fragile": "#ffcdd2",
            "neutral": "#fff9c4",
        }.get(cls, "white")
        cell_rows.append(
            f"<tr style='background:{color}'>"
            f"<td>{c['stage']}</td>"
            f"<td><code>{c['label']}</code></td>"
            f"<td>{cls}</td>"
            f"<td class='num'>{ms['oos_sharpe']:.2f}</td>"
            f"<td class='num'>{ms['oos_dd_pct']:.1f}%</td>"
            f"<td class='num'>{ms['is_oos_cagr_gap_pct']:+.2f}%</td>"
            f"<td class='num'>{ms['sensitivity_max_change_pct']:.1f}%</td>"
            f"</tr>"
        )

    prod_count = sum(1 for c in cells if c["classification"] == "production_ready")
    fragile_count = sum(1 for c in cells if c["classification"] == "fragile")

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>iter66 v2.2 設定最適化グリッド</title>
<style>
body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 1200px;
        margin: 24px auto; padding: 0 16px; background:#f5f5f5; }}
h1 {{ color: #1565c0; border-bottom: 3px solid #1565c0; }}
h2 {{ color: #1976d2; margin-top: 32px; }}
table {{ border-collapse: collapse; width: 100%; background: white;
         box-shadow: 0 1px 4px rgba(0,0,0,0.1); margin-bottom: 16px; }}
th, td {{ padding: 8px 12px; border-bottom: 1px solid #ddd; }}
th {{ background: #1565c0; color: white; text-align: left; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 3px; font-size: 12px; }}
.summary-cards {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin: 20px 0; }}
.card {{ background: white; padding: 20px; border-radius: 8px;
         box-shadow: 0 1px 4px rgba(0,0,0,0.1); text-align: center; }}
.card h3 {{ margin: 0 0 8px; color: #666; font-size: 14px; }}
.card .num-big {{ font-size: 32px; font-weight: bold; }}
.prod {{ color: #2e7d32; }}
.frag {{ color: #c62828; }}
.neut {{ color: #f9a825; }}
</style></head><body>
<h1>iter66: v2.2 設定最適化グリッドサーチ + Benchmark 比較</h1>
<p>生成: {m['generated_at']} / 実行時間: {m['elapsed_sec']}s / セル数: {m['num_cells']}
{(' / <b>smoke mode</b>' if m.get('smoke_mode') else '')}</p>

<div class="summary-cards">
<div class="card"><h3>production_ready</h3><div class="num-big prod">{prod_count}</div></div>
<div class="card"><h3>fragile</h3><div class="num-big frag">{fragile_count}</div></div>
<div class="card"><h3>neutral</h3><div class="num-big neut">{len(cells) - prod_count - fragile_count}</div></div>
</div>

<h2>Benchmark 成績 (全期間)</h2>
<table>
<tr><th>Benchmark</th><th>CAGR</th><th>MaxDD</th><th>Sharpe</th><th>Trades</th><th>最終資金</th></tr>
{''.join(bm_rows)}
</table>

<h2>グリッド結果 全セル</h2>
<table>
<tr><th>Stage</th><th>Label</th><th>分類</th><th>OOS Sharpe</th>
<th>OOS DD</th><th>IS-OOS gap</th><th>感度 max</th></tr>
{''.join(cell_rows)}
</table>

<h2>過学習診断</h2>
<pre>{json.dumps(payload['overfitting_summary'], indent=2, ensure_ascii=False)}</pre>

</body></html>
"""
    return html


# ─────────────────────────────
# main
# ─────────────────────────────
def _json_default(obj):
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    if isinstance(obj, pd.Series):
        return obj.tolist()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true",
                        help="各 Stage を 1-2 組に絞った動作確認モード")
    parser.add_argument("--no-sensitivity", action="store_true",
                        help="感度分析をスキップ (セル数 × 6 倍高速化)")
    args = parser.parse_args()

    print("=" * 70)
    print("iter66: v2.2 設定最適化グリッドサーチ + Benchmark 比較")
    print("=" * 70)

    all_data = load_cache()
    universe = make_universe(all_data)
    print(f"ユニバース: {len(universe)} 銘柄 (BTC 除く)")

    payload = run_grid(
        all_data, universe,
        smoke=args.smoke,
        with_sensitivity=not args.no_sensitivity,
    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )
    OUT_MD.write_text(build_md(payload), encoding="utf-8")
    OUT_HTML.write_text(build_html(payload), encoding="utf-8")

    print(f"\nJSON: {OUT_JSON}")
    print(f"MD:   {OUT_MD}")
    print(f"HTML: {OUT_HTML}")
    print(f"\n完了: {payload['meta']['elapsed_sec']}s, {payload['meta']['num_cells']} セル")
    return 0


if __name__ == "__main__":
    sys.exit(main())
