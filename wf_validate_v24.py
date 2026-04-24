"""
iter71: v2.4 トレイル閾値 Walk-Forward 過学習検証 (2020-2025)
==================================================================

目的:
    PR #8 (v2.4) で導入された dynamic_regime + trail_stop_ach (30%) +
    trail_stop_btc (20%) が、W4 (2024-01〜2025-04-19, 1.3年) 以外の
    期間でも有効か、あるいは W4 期間への過学習かを検証する。

手法:
    Walk-Forward 窓 (IS で最適化 → 独立した OOS で評価) を 4 つ切り、
    trail_ach × trail_btc × bull_ach_weight のパラメータ格子を各窓で探索。
    - W1: IS 2020-2021 / OOS 2022 (ベア相場 OOS・最重要)
    - W2: IS 2020-2022 / OOS 2023
    - W3: IS 2021-2023 / OOS 2024
    - W4: IS 2022-2024 / OOS 2025-01〜2025-04 (現行 PR #8 と同条件)

使い方:
    python wf_validate_v24.py --windows W1,W2,W3,W4
    python wf_validate_v24.py --windows W1  (単一窓のみ)

出力:
    results/wf_validate_v24/
      - summary.md       各窓の最良セルとロバスト性評価
      - all_cells.csv    全セルの生結果
      - iter71_report.md PR 本文テンプレ

過学習判定 (提案):
    ロバスト: 4 窓の OOS 最良セルのパラメータがだいたい一致
             & 全窓で OOS MaxDD < 60%
    過学習: 最良パラメータが窓ごとにバラバラ、もしくは
           ベア OOS (W1) で OOS CAGR が大幅マイナス
"""
from __future__ import annotations

import argparse
import csv
import json
import pickle
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

import _iter43_rethink as R43  # summarize() 再利用
from _iter54_comprehensive import select_top

RESULTS_DIR = PROJECT / "results"
OUT_DIR = RESULTS_DIR / "wf_validate_v24"
CACHE_CANDIDATES = [
    RESULTS_DIR / "_cache_alldata.pkl",
    RESULTS_DIR / "_iter61_cache.pkl",
    RESULTS_DIR / "_bear_test_cache.pkl",
]

# demo_runner.py と揃える
FEE = 0.0010
SLIP = 0.0005
UNIVERSE_REMOVE = {"MATIC/USDT", "FTM/USDT", "MKR/USDT", "EOS/USDT"}

# ─────────────────────────────
# Walk-Forward 窓定義
# ─────────────────────────────
WINDOWS: list[dict[str, str]] = [
    {
        "id": "W1",
        "is_start": "2020-01-01", "is_end": "2021-12-31",
        "oos_start": "2022-01-01", "oos_end": "2022-12-31",
        "note": "ベア OOS (最重要: 過学習を一番炙り出す)",
    },
    {
        "id": "W2",
        "is_start": "2020-01-01", "is_end": "2022-12-31",
        "oos_start": "2023-01-01", "oos_end": "2023-12-31",
        "note": "ベア含む IS → 回復期 OOS",
    },
    {
        "id": "W3",
        "is_start": "2021-01-01", "is_end": "2023-12-31",
        "oos_start": "2024-01-01", "oos_end": "2024-12-31",
        "note": "ブル OOS",
    },
    {
        "id": "W4",
        "is_start": "2022-01-01", "is_end": "2024-12-31",
        "oos_start": "2025-01-01", "oos_end": "2025-04-19",
        "note": "PR #8 で示された現行窓 (比較用)",
    },
]

# ─────────────────────────────
# パラメータグリッド
# ─────────────────────────────
BULL_ACH_WEIGHT_GRID = [0.50, 0.55, 0.60, 0.65]
TRAIL_STOP_ACH_GRID = [0.20, 0.25, 0.30, 0.35]
TRAIL_STOP_BTC_GRID = [0.15, 0.20, 0.25, 0.30]

# 固定パラメータ (demo_runner.py v2.4 相当)
BTC_W = 0.35
ACH_W = 0.35
USDT_W = 0.30
TOP_N = 2
LOOKBACK = 25
REBALANCE_DAYS = 7
ADX_MIN = 15
CORR_THRESHOLD = 0.80
WEIGHT_METHOD = "momentum"


# ─────────────────────────────
# データ読み込み
# ─────────────────────────────
def load_cache() -> dict[str, pd.DataFrame]:
    for path in CACHE_CANDIDATES:
        if path.exists():
            with open(path, "rb") as f:
                data = pickle.load(f)
            if "BTC/USDT" in data:
                df = data["BTC/USDT"]
                if "ema200" not in df.columns:
                    df = df.copy()
                    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
                    data["BTC/USDT"] = df
            print(f"📦 キャッシュ読込: {path.name} ({len(data)} 銘柄)")
            return data
    raise FileNotFoundError(f"キャッシュ未検出: {CACHE_CANDIDATES}")


def make_universe(all_data: dict[str, pd.DataFrame]) -> list[str]:
    return sorted(
        s for s in all_data.keys()
        if s not in UNIVERSE_REMOVE
    )


def _reb_key(date: pd.Timestamp, days: int) -> int:
    doy = date.dayofyear + (date.year - 2020) * 366
    return doy // days


# ─────────────────────────────
# v2.4 バックテスト本体
# ─────────────────────────────
@dataclass
class BTResult:
    final: float
    total_ret: float
    cagr: float
    max_dd: float
    sharpe: float
    calmar: float
    n_trades: int
    n_bear_exits: int
    n_trail_ach: int
    n_trail_btc: int
    days: int


def run_bt_v24(
    all_data: dict[str, pd.DataFrame],
    universe: list[str],
    start: str,
    end: str,
    *,
    bull_ach_weight: float,
    trail_stop_ach: float,
    trail_stop_btc: float,
    initial: float = 10_000.0,
) -> BTResult:
    """demo_runner.py v2.4 の決定ロジックをバックテスト向けに再現。

    - Bull 時に ACH 目標比率を bull_ach_weight まで USDT から補充
    - Bear 時に ACH の現金を USDT へ完全退避 (ポジションは既に Bear 即時退避で売却済)
    - ACH の mtm が peak から trail_stop_ach 下落で全売却 + USDT へ完全退避
    - BTC の btc_value が peak から trail_stop_btc 下落で全売却 + USDT へ完全退避
    """
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    btc_df = all_data["BTC/USDT"]
    dates = [d for d in btc_df.index if start_ts <= d <= end_ts]
    if not dates:
        raise ValueError(f"期間 {start} 〜 {end} にデータがありません")

    btc_cash = initial * BTC_W
    btc_qty = 0.0
    btc_entry = 0.0
    btc_peak = 0.0  # v2.4: trail_stop_btc 用

    ach_cash = initial * ACH_W
    ach_positions: dict[str, float] = {}  # {sym: qty}
    ach_peak = 0.0  # v2.4: trail_stop_ach 用

    usdt_cash = initial * USDT_W

    equity_curve = [{"ts": dates[0] - pd.Timedelta(days=1), "equity": initial}]
    n_trades = 0
    n_bear_exits = 0
    n_trail_ach = 0
    n_trail_btc = 0
    last_key: int | None = None

    for date in dates:
        btc_r = btc_df.loc[date]
        price = float(btc_r["close"])
        ema200 = btc_r.get("ema200")
        btc_bullish = not pd.isna(ema200) and price > float(ema200)

        # ─────────────────────────────
        # BTC 側
        # ─────────────────────────────
        # 1) BTC BUY (Bear→Bull 転換)
        if btc_qty == 0 and btc_bullish:
            buy_p = price * (1 + SLIP)
            btc_qty = btc_cash / buy_p * (1 - FEE)
            btc_entry = buy_p
            btc_cash = 0
            btc_peak = btc_qty * price  # v2.4: peak 初期化
            n_trades += 1
        # 2) v2.4: BTC トレイル判定 (EMA200 判定より先)
        elif btc_qty > 0 and trail_stop_btc is not None:
            btc_value_now = btc_cash + btc_qty * price
            if btc_value_now > btc_peak:
                btc_peak = btc_value_now
            elif btc_peak > 0 and (btc_peak - btc_value_now) / btc_peak >= trail_stop_btc:
                sell_p = price * (1 - SLIP)
                proceeds = btc_qty * sell_p * (1 - FEE)
                usdt_cash += proceeds  # USDT へ完全退避
                btc_qty = 0
                btc_cash = 0
                btc_peak = 0.0
                n_trades += 1
                n_trail_btc += 1
        # 3) 通常 SELL (Bull→Bear 転換) - トレイルで既に売却済みなら no-op
        if btc_qty > 0 and not btc_bullish:
            sell_p = price * (1 - SLIP)
            btc_cash += btc_qty * sell_p * (1 - FEE)
            btc_qty = 0
            btc_peak = 0.0
            n_trades += 1

        # ─────────────────────────────
        # ACH: Bear 即時退避 (v2.2 機能、v2.4 でも保持)
        # ─────────────────────────────
        if not btc_bullish and ach_positions:
            for sym in list(ach_positions.keys()):
                df = all_data[sym]
                if date in df.index:
                    p = float(df.loc[date, "close"]) * (1 - SLIP)
                    ach_cash += ach_positions[sym] * p * (1 - FEE)
                    n_trades += 1
                    n_bear_exits += 1
                    ach_positions.pop(sym)

            # v2.4 dynamic_regime: Bear 時に ACH cash を USDT へ完全退避
            if ach_cash > 0:
                usdt_cash += ach_cash
                ach_cash = 0

        # ACH / USDT 金利 (demo_runner.py と同じ)
        if btc_qty == 0:
            btc_cash *= (1 + 0.03 / 365)
        usdt_cash *= (1 + 0.03 / 365)

        # ─────────────────────────────
        # v2.4: ACH トレイル判定 (リバランス前、mtm で判定)
        # ─────────────────────────────
        ach_mtm = ach_cash
        for sym, qty in ach_positions.items():
            df = all_data[sym]
            if date in df.index:
                ach_mtm += qty * float(df.loc[date, "close"])

        ach_trail_fired = False
        if trail_stop_ach is not None:
            if ach_mtm > ach_peak:
                ach_peak = ach_mtm
            elif ach_peak > 0 and (ach_peak - ach_mtm) / ach_peak >= trail_stop_ach:
                # 発動: 全ポジション売却 + ACH cash 全部 USDT へ
                for sym in list(ach_positions.keys()):
                    df = all_data[sym]
                    if date in df.index:
                        p = float(df.loc[date, "close"]) * (1 - SLIP)
                        ach_cash += ach_positions[sym] * p * (1 - FEE)
                        n_trades += 1
                        ach_positions.pop(sym)
                usdt_cash += ach_cash
                ach_cash = 0
                ach_peak = 0.0
                n_trail_ach += 1
                ach_trail_fired = True

        # ─────────────────────────────
        # ACH リバランス (トレイル発動日はスキップ)
        # ─────────────────────────────
        cur_key = _reb_key(date, REBALANCE_DAYS)
        if cur_key != last_key and not ach_trail_fired:
            # 既存ポジションを全決済 (Bear 退避で既に空のことが多い)
            for sym in list(ach_positions.keys()):
                df = all_data[sym]
                if date in df.index:
                    p = float(df.loc[date, "close"]) * (1 - SLIP)
                    ach_cash += ach_positions[sym] * p * (1 - FEE)
                    n_trades += 1
                    ach_positions.pop(sym)

            if not btc_bullish:
                last_key = cur_key
            else:
                # v2.4 dynamic_regime: Bull 時に ACH 目標比率を補充
                total_eq = btc_cash + btc_qty * price + ach_cash + usdt_cash
                target_ach = total_eq * bull_ach_weight
                if ach_cash < target_ach:
                    shortage = target_ach - ach_cash
                    take = min(shortage, usdt_cash)
                    if take > 0:
                        ach_cash += take
                        usdt_cash -= take

                sel = select_top(
                    all_data, universe, date, TOP_N, LOOKBACK,
                    ADX_MIN, 0, WEIGHT_METHOD, False,
                )
                if sel:
                    if WEIGHT_METHOD == "momentum":
                        pos_rets = [max(r, 0.01) for _, r in sel]
                        total_w = sum(pos_rets)
                        weights = [r / total_w for r in pos_rets]
                    else:
                        weights = [1.0 / len(sel)] * len(sel)

                    for (sym, _), w in zip(sel, weights):
                        df = all_data[sym]
                        if date not in df.index:
                            continue
                        p_buy = float(df.loc[date, "close"]) * (1 + SLIP)
                        cost = ach_cash * w
                        if cost > 0:
                            qty = cost / p_buy * (1 - FEE)
                            ach_positions[sym] = qty
                            n_trades += 1
                    used = sum(ach_cash * w for w in weights)
                    ach_cash -= used
                    # ACH 新規購入後 peak を現在の mtm にリセット
                    ach_peak_new = ach_cash
                    for sym, qty in ach_positions.items():
                        df = all_data[sym]
                        if date in df.index:
                            ach_peak_new += qty * float(df.loc[date, "close"])
                    ach_peak = max(ach_peak, ach_peak_new)
                last_key = cur_key

        # 日次エクイティ記録
        ach_value = ach_cash
        for sym, qty in ach_positions.items():
            df = all_data[sym]
            if date in df.index:
                ach_value += qty * float(df.loc[date, "close"])
        total = btc_cash + btc_qty * price + ach_value + usdt_cash
        equity_curve.append({"ts": date, "equity": total})

    return _finalize(
        equity_curve, initial,
        n_trades, n_bear_exits, n_trail_ach, n_trail_btc,
    )


def _finalize(
    equity_curve: list[dict[str, Any]],
    initial: float,
    n_trades: int,
    n_bear_exits: int,
    n_trail_ach: int,
    n_trail_btc: int,
) -> BTResult:
    df = pd.DataFrame(equity_curve).set_index("ts")
    df.index = pd.to_datetime(df.index)
    eq = df["equity"]
    final = float(eq.iloc[-1])
    total_ret = (final - initial) / initial * 100
    days = (df.index[-1] - df.index[0]).days
    years = max(days / 365.25, 1 / 365.25)
    cagr = ((final / initial) ** (1 / years) - 1) * 100 if final > 0 else -100.0

    peak, max_dd = initial, 0.0
    for e in eq:
        peak = max(peak, float(e))
        if peak > 0:
            dd = (peak - float(e)) / peak * 100
            max_dd = max(max_dd, dd)

    daily = eq.resample("D").last().ffill()
    daily_ret = daily.pct_change().dropna()
    if len(daily_ret) > 1 and daily_ret.std() > 0:
        sharpe = float(daily_ret.mean() / daily_ret.std() * np.sqrt(365))
    else:
        sharpe = 0.0
    calmar = cagr / max_dd if max_dd > 0 else 0.0

    return BTResult(
        final=round(final, 2),
        total_ret=round(total_ret, 2),
        cagr=round(cagr, 2),
        max_dd=round(max_dd, 2),
        sharpe=round(sharpe, 2),
        calmar=round(calmar, 2),
        n_trades=n_trades,
        n_bear_exits=n_bear_exits,
        n_trail_ach=n_trail_ach,
        n_trail_btc=n_trail_btc,
        days=days,
    )


# ─────────────────────────────
# Walk-Forward 実行
# ─────────────────────────────
def run_wf(
    all_data: dict[str, pd.DataFrame],
    universe: list[str],
    windows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    grid = list(product(BULL_ACH_WEIGHT_GRID, TRAIL_STOP_ACH_GRID, TRAIL_STOP_BTC_GRID))
    total = len(windows) * len(grid) * 2  # IS + OOS
    print(f"🧮 総セル数: {total} ({len(windows)} 窓 × {len(grid)} グリッド × IS/OOS)")

    done = 0
    for win in windows:
        for bw, ta, tb in grid:
            # IS
            is_r = run_bt_v24(
                all_data, universe, win["is_start"], win["is_end"],
                bull_ach_weight=bw, trail_stop_ach=ta, trail_stop_btc=tb,
            )
            # OOS
            oos_r = run_bt_v24(
                all_data, universe, win["oos_start"], win["oos_end"],
                bull_ach_weight=bw, trail_stop_ach=ta, trail_stop_btc=tb,
            )
            rows.append({
                "window": win["id"],
                "is_start": win["is_start"], "is_end": win["is_end"],
                "oos_start": win["oos_start"], "oos_end": win["oos_end"],
                "bull_ach_weight": bw,
                "trail_stop_ach": ta,
                "trail_stop_btc": tb,
                **{f"is_{k}": v for k, v in asdict(is_r).items()},
                **{f"oos_{k}": v for k, v in asdict(oos_r).items()},
            })
            done += 2
            if done % 32 == 0:
                print(f"  ... {done}/{total} 済 ({done*100//total}%)")
    return rows


# ─────────────────────────────
# レポート生成
# ─────────────────────────────
def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def best_by_is_calmar(rows: list[dict[str, Any]], window_id: str) -> dict[str, Any]:
    """IS Calmar (CAGR/MaxDD) で最良セルを選定 → そのセルの OOS 成績が本物の汎化性能"""
    window_rows = [r for r in rows if r["window"] == window_id]
    return max(window_rows, key=lambda r: r["is_calmar"])


def write_summary(rows: list[dict[str, Any]], windows: list[dict[str, str]], path: Path) -> None:
    lines = [
        "# iter71: v2.4 トレイル閾値 Walk-Forward 検証サマリー",
        "",
        f"生成日時: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"総セル数: {len(rows)} (窓 {len(windows)} × グリッド {len(BULL_ACH_WEIGHT_GRID)*len(TRAIL_STOP_ACH_GRID)*len(TRAIL_STOP_BTC_GRID)})",
        "",
        "## 各窓の IS 最良セル (Calmar) と OOS 成績",
        "",
        "| 窓 | IS 期間 | OOS 期間 | bull_w | trail_ach | trail_btc | IS CAGR | IS DD | **OOS CAGR** | **OOS DD** | **OOS Sharpe** | OOS 判定 |",
        "|----|---------|---------|--------|-----------|-----------|--------|-------|-------------|------------|---------------|---------|",
    ]

    bests: list[dict[str, Any]] = []
    for win in windows:
        b = best_by_is_calmar(rows, win["id"])
        bests.append(b)
        verdict = _verdict(b)
        lines.append(
            f"| {b['window']} | {b['is_start']}〜{b['is_end']} | {b['oos_start']}〜{b['oos_end']} | "
            f"{b['bull_ach_weight']:.2f} | {b['trail_stop_ach']:.2f} | {b['trail_stop_btc']:.2f} | "
            f"{b['is_cagr']:+.1f}% | {b['is_max_dd']:.1f}% | "
            f"**{b['oos_cagr']:+.1f}%** | **{b['oos_max_dd']:.1f}%** | **{b['oos_sharpe']:.2f}** | {verdict} |"
        )

    lines += [
        "",
        "## ロバスト性診断",
        "",
    ]
    bw_set = {b["bull_ach_weight"] for b in bests}
    ta_set = {b["trail_stop_ach"] for b in bests}
    tb_set = {b["trail_stop_btc"] for b in bests}
    lines.append(f"- IS 最良セルの bull_ach_weight 分散: {len(bw_set)} 種 {sorted(bw_set)}")
    lines.append(f"- IS 最良セルの trail_stop_ach 分散: {len(ta_set)} 種 {sorted(ta_set)}")
    lines.append(f"- IS 最良セルの trail_stop_btc 分散: {len(tb_set)} 種 {sorted(tb_set)}")
    oos_dds = [b["oos_max_dd"] for b in bests]
    oos_cagrs = [b["oos_cagr"] for b in bests]
    lines.append(
        f"- OOS MaxDD: min {min(oos_dds):.1f}% / max {max(oos_dds):.1f}% / 平均 {sum(oos_dds)/len(oos_dds):.1f}%"
    )
    lines.append(
        f"- OOS CAGR: min {min(oos_cagrs):+.1f}% / max {max(oos_cagrs):+.1f}% / 平均 {sum(oos_cagrs)/len(oos_cagrs):+.1f}%"
    )

    # 最重要窓 W1 (ベア OOS) の生存判定
    w1 = next((b for b in bests if b["window"] == "W1"), None)
    if w1:
        lines += [
            "",
            "## 🚨 最重要判定: W1 (ベア OOS 2022) での生存性",
            "",
            f"- OOS CAGR: **{w1['oos_cagr']:+.2f}%** (0% 以上が最低生存ライン)",
            f"- OOS MaxDD: **{w1['oos_max_dd']:.2f}%** (55% 以下が目標)",
            f"- トレイル発動: ACH {w1['oos_n_trail_ach']} 回 / BTC {w1['oos_n_trail_btc']} 回 / Bear 退避 {w1['oos_n_bear_exits']} 回",
            "",
        ]
        if w1["oos_cagr"] < 0:
            lines.append("**結論: ベア相場で損失。v2.4 は過学習の可能性が高い。**")
        elif w1["oos_max_dd"] > 60:
            lines.append("**結論: ベア相場生存だが DD 過大。要改善。**")
        else:
            lines.append("**結論: ベア相場でも健全。v2.4 は採用候補。**")

    lines += [
        "",
        "## 過学習の有無 (パラメータ分散による判定)",
        "",
    ]
    if len(bw_set) == 1 and len(ta_set) == 1 and len(tb_set) == 1:
        lines.append("✅ 全窓で同一パラメータが最適 → **過学習ではない可能性が高い**")
    elif max(len(bw_set), len(ta_set), len(tb_set)) <= 2:
        lines.append("🟡 窓ごとの最適パラメータが近接 → **軽度の過学習**")
    else:
        lines.append("🔴 窓ごとに最適パラメータがバラバラ → **過学習の可能性が高い**")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _verdict(row: dict[str, Any]) -> str:
    if row["oos_cagr"] < 0:
        return "🔴 損失"
    if row["oos_max_dd"] > 60:
        return "🟡 DD 過大"
    if row["oos_cagr"] > 30 and row["oos_max_dd"] < 55:
        return "✅ 良好"
    return "🟡 可"


def write_iter71_report(rows: list[dict[str, Any]], windows: list[dict[str, str]], path: Path) -> None:
    """PR 本文テンプレ"""
    bests = [best_by_is_calmar(rows, w["id"]) for w in windows]
    w1 = next((b for b in bests if b["window"] == "W1"), None)
    lines = [
        "# iter71: v2.4 トレイル閾値の Walk-Forward 検証 (2020-2025)",
        "",
        "## 概要",
        "",
        "PR #8 (v2.4) で導入された dynamic_regime + trail_stop_ach 30% + trail_stop_btc 20% が",
        "W4 期間 (1.3 年) 以外の相場でも有効か、Walk-Forward 4 窓で検証した。",
        "",
        "## 結果: 各窓で IS Calmar 最良セル → OOS 成績",
        "",
        "| 窓 | IS | OOS | bull_w | trail_ach | trail_btc | OOS CAGR | OOS DD | OOS Sharpe |",
        "|----|----|----|--------|-----------|-----------|---------|--------|-----------|",
    ]
    for b in bests:
        lines.append(
            f"| {b['window']} | {b['is_start'][:7]}〜{b['is_end'][:7]} | {b['oos_start'][:7]}〜{b['oos_end'][:7]} | "
            f"{b['bull_ach_weight']:.2f} | {b['trail_stop_ach']:.2f} | {b['trail_stop_btc']:.2f} | "
            f"{b['oos_cagr']:+.1f}% | {b['oos_max_dd']:.1f}% | {b['oos_sharpe']:.2f} |"
        )

    lines += [
        "",
        "## 判定",
        "",
    ]
    if w1:
        if w1["oos_cagr"] < 0:
            lines.append("- 🔴 **ベア OOS (W1 = 2022) で損失** → v2.4 はブル相場前提の過学習の可能性。")
        elif w1["oos_max_dd"] > 60:
            lines.append("- 🟡 **ベア OOS で生存するが DD 過大** → trail 閾値を詰めても限界。")
        else:
            lines.append("- ✅ **ベア OOS (W1 = 2022) でも生存** → v2.4 は採用候補。")

    ta_set = {b["trail_stop_ach"] for b in bests}
    tb_set = {b["trail_stop_btc"] for b in bests}
    if max(len(ta_set), len(tb_set)) >= 3:
        lines.append("- 🔴 **最適パラメータが窓ごとに大きく変わる** → 過学習の強い証拠。")
    elif max(len(ta_set), len(tb_set)) == 2:
        lines.append("- 🟡 **最適パラメータが軽度にばらつく** → 閾値の頑健性に疑問。")
    else:
        lines.append("- ✅ **全窓で同一パラメータが最適** → 過学習の疑いは低い。")

    lines += [
        "",
        "## PR #8 に対する推奨",
        "",
        "上記 W1 (ベア OOS) と過学習判定を踏まえて、PR #8 のマージ可否を判断する根拠とする。",
        "詳細は `results/wf_validate_v24/summary.md` 参照。",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# ─────────────────────────────
# CLI
# ─────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="v2.4 Walk-Forward 検証")
    parser.add_argument(
        "--windows", default="W1,W2,W3,W4",
        help="実行する窓 ID をカンマ区切り (例: W1,W2)",
    )
    parser.add_argument(
        "--out", default=str(OUT_DIR),
        help="出力ディレクトリ",
    )
    args = parser.parse_args()

    selected_ids = set(args.windows.split(","))
    windows = [w for w in WINDOWS if w["id"] in selected_ids]
    if not windows:
        raise SystemExit(f"指定窓 {args.windows} は WINDOWS に存在しません")

    all_data = load_cache()
    universe = make_universe(all_data)
    print(f"🌐 ユニバース: {len(universe)} 銘柄")

    rows = run_wf(all_data, universe, windows)

    out_dir = Path(args.out)
    write_csv(rows, out_dir / "all_cells.csv")
    write_summary(rows, windows, out_dir / "summary.md")
    write_iter71_report(rows, windows, out_dir / "iter71_report.md")

    print(f"\n✅ 完了: {out_dir}/")
    print(f"   - all_cells.csv ({len(rows)} 行)")
    print(f"   - summary.md")
    print(f"   - iter71_report.md")


if __name__ == "__main__":
    main()
