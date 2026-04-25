"""v4 vs v2.5 を実データで年次・四半期分析、JSON 出力."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

from wf_validate_v24 import load_cache, make_universe, run_bt_v24


def calc_yearly(equity_curve, initial=10000.0):
    """エクイティから年次リターンを計算."""
    df = pd.DataFrame(equity_curve).set_index("ts")
    df.index = pd.to_datetime(df.index)
    yearly = {}
    prev = initial
    for year in range(2020, 2026):
        yr = df[df.index.year == year]
        if yr.empty:
            continue
        end_eq = float(yr["equity"].iloc[-1])
        ret = (end_eq / prev - 1) * 100 if prev > 0 else 0
        yearly[str(year)] = {
            "ret_pct": round(ret, 2),
            "end_equity": round(end_eq, 2),
            "start_equity": round(prev, 2),
        }
        prev = end_eq
    return yearly


def run_with_curve(all_data, universe, start, end, **kwargs):
    """エクイティ曲線も返すラッパー (run_bt_v24 を改造して返す)."""
    # run_bt_v24 は equity_curve を内部に持つが返さないので、
    # ここでは BTResult のみ。代わりに単純に run_bt_v24 を呼ぶ。
    r = run_bt_v24(all_data, universe, start, end, **kwargs)
    # equity_curve 取得のため、quick re-implementation:
    # 既存 run_bt_v24 を流用、結果のみで十分
    return r


# v2.5_chop
V25_KW = dict(
    bull_ach_weight=0.60, trail_stop_ach=0.30, trail_stop_btc=0.20,
    btc_weight=0.35, ach_weight=0.35, usdt_weight=0.30,
    multi_lookback=True, top_n=2,
    chop_atr_filter=True, chop_atr_threshold=0.04, chop_atr_multiplier=2.0,
)

# v4_attack
V4_KW = dict(
    bull_ach_weight=0.70, trail_stop_ach=0.40, trail_stop_btc=0.25,
    btc_weight=0.40, ach_weight=0.40, usdt_weight=0.20,
    multi_lookback=False, top_n=3,
    chop_atr_filter=False,
)


def main():
    all_data = load_cache()
    universe = make_universe(all_data)
    print(f"🌐 ユニバース: {len(universe)} 銘柄")

    # エクイティ曲線も含めて取得するため、run_bt_v24 を内部呼び出し
    # ただし equity_curve を取り出すには中身を改造必要 → 代わりに年次区切りで実行
    output = {
        "title": "気持ちマックス Bot バックテスト実測 v2.5 vs v4.0",
        "data_source": "results/_iter61_cache.pkl (62銘柄、Binance + 5ソース検証済)",
        "universe_size": len(universe),
        "configs": {
            "v2.5_chop": V25_KW,
            "v4_attack": V4_KW,
        },
        "periods": {},
    }

    # 期間別実測
    periods = [
        ("full_2020_2024",  "2020-01-01", "2024-12-31", "5 年フル (bull dominant)"),
        ("2020_2021",       "2020-01-01", "2021-12-31", "アルト爆発期"),
        ("2022_only",       "2022-01-01", "2022-12-31", "ベア年単独"),
        ("2023_only",       "2023-01-01", "2023-12-31", "回復年"),
        ("2024_only",       "2024-01-01", "2024-12-31", "ブル年"),
        ("2025_q1",         "2025-01-01", "2025-04-19", "chop 相場"),
        ("3_3yr_hard",      "2022-01-01", "2025-04-19", "難しい 3.3 年"),
    ]

    print("\n=== 実測データ取得中 ===")
    for pid, start, end, label in periods:
        print(f"  {pid} ({label}): {start} 〜 {end}")
        v25_r = run_bt_v24(all_data, universe, start, end, **V25_KW)
        v4_r = run_bt_v24(all_data, universe, start, end, **V4_KW)
        output["periods"][pid] = {
            "label": label,
            "start": start, "end": end,
            "days": v25_r.days,
            "v2.5_chop": {
                "final": v25_r.final, "total_ret": v25_r.total_ret,
                "cagr": v25_r.cagr, "max_dd": v25_r.max_dd,
                "sharpe": v25_r.sharpe, "calmar": v25_r.calmar,
                "n_trades": v25_r.n_trades,
                "n_bear_exits": v25_r.n_bear_exits,
                "n_trail_ach": v25_r.n_trail_ach,
                "n_trail_btc": v25_r.n_trail_btc,
            },
            "v4_attack": {
                "final": v4_r.final, "total_ret": v4_r.total_ret,
                "cagr": v4_r.cagr, "max_dd": v4_r.max_dd,
                "sharpe": v4_r.sharpe, "calmar": v4_r.calmar,
                "n_trades": v4_r.n_trades,
                "n_bear_exits": v4_r.n_bear_exits,
                "n_trail_ach": v4_r.n_trail_ach,
                "n_trail_btc": v4_r.n_trail_btc,
            },
            "diff": {
                "v4_vs_v25_final": v4_r.final - v25_r.final,
                "v4_vs_v25_pct_pt": v4_r.total_ret - v25_r.total_ret,
                "v4_vs_v25_dd_pt": v4_r.max_dd - v25_r.max_dd,
            },
        }

    out_path = PROJECT / "results" / "v4_yearly_data.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    print(f"\n💾 saved: {out_path}")

    # コンソール表示
    print("\n=== 実測サマリー ===")
    print(f"{'期間':<15} {'v2.5_chop final':>18} {'v4_attack final':>18} {'差分':>15}")
    for pid, info in output["periods"].items():
        print(f"  {pid:<15} ${info['v2.5_chop']['final']:>15,.0f} "
              f"${info['v4_attack']['final']:>15,.0f} "
              f"{info['diff']['v4_vs_v25_final']:>+14,.0f}")


if __name__ == "__main__":
    main()
