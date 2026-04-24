"""
短期ランキング上位2戦略の5年詳細バックテスト
===========================================
対象: DL アグレッシブ, DL 素早く (2023-2024の2年で月+13%超を記録)
検証: 2020-2024 を年別 + 5年通期で持続性を判定
ベンチマーク: DL MAX 2x (推奨・既知の5年+638%), BTC Buy&Hold
データ: Binance日足 BTC/USDT のみ（合成禁止・本番データ強制）
健全性: 6項目チェック (価格>0, NaN無し, タイムスタンプ連続, 出来高>0, Binance強制, 価格変動妥当性)
"""
from __future__ import annotations
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config
from data_fetcher import DataFetcher
from _dynamic_lev_max2x import dynamic_leverage_custom
from _racsm_backtest import validate_ohlcv_data, assert_binance_source


# 対象戦略と ADX レベル → レバレッジ
STRATEGIES = {
    "DL アグレッシブ (1.5/2.5/3.5)": [(20, 1.5), (30, 2.5), (40, 3.5)],
    "DL 素早く (1/2/3 @20/25/30)":   [(20, 1.0), (25, 2.0), (30, 3.0)],
    "DL MAX 2x 推奨 (1/2 @20/30)":   [(20, 1.0), (30, 2.0)],
    "DL 標準 3x (1/2/3 @20/30/40)":  [(20, 1.0), (30, 2.0), (40, 3.0)],
}

PERIODS = [
    ("2020-01-01", "2020-12-31", "2020 ブルラン開始"),
    ("2021-01-01", "2021-12-31", "2021 ピーク&暴落"),
    ("2022-01-01", "2022-12-31", "2022 ベアマーケット"),
    ("2023-01-01", "2023-12-31", "2023 回復"),
    ("2024-01-01", "2024-12-31", "2024 新高値"),
    ("2020-01-01", "2024-12-31", "▶ 5年通期"),
]

INITIAL = 10_000.0


def run():
    fetcher = DataFetcher(Config())
    assert_binance_source(fetcher)  # Binance以外は即RuntimeError

    # BTC Buy&Hold は別ロジックで取得・検証
    bh_by_period = {}
    print("📥 データ取得 + 健全性6項目チェック中...")
    for s, e, label in PERIODS:
        df = fetcher.fetch_historical_ohlcv("BTC/USDT", "1d", s, e)
        validate_ohlcv_data(df, "BTC/USDT", "1d")
        bh = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        bh_by_period[label] = round(bh, 2)
    print("✅ 全期間データ健全性 OK (価格>0, NaN無し, タイムスタンプ連続, 出来高>0, Binance強制, 変動妥当性)\n")

    # 戦略実行
    results = {strat: {} for strat in STRATEGIES}
    for s, e, label in PERIODS:
        print(f"\n🔬 検証期間: {label} ({s} 〜 {e})")
        for strat_name, levels in STRATEGIES.items():
            try:
                r = dynamic_leverage_custom(fetcher, s, e, levels, INITIAL)
                results[strat_name][label] = r
                print(f"  {strat_name:35s}: "
                      f"{r['total_return_pct']:+8.2f}% / 月 {r['monthly_avg']:+6.2f}% / "
                      f"DD {r['max_dd_pct']:>5.1f}% / 勝率 {r['win_rate_pct']}%")
            except Exception as ex:
                results[strat_name][label] = {"error": str(ex)}
                print(f"  {strat_name:35s}: ERROR {ex}")

    # ── 比較テーブル表示 ──
    print(f"\n\n{'═' * 120}")
    print(f"📊 全期間比較表（年別成績）")
    print(f"{'═' * 120}")
    headers = ["期間"] + list(STRATEGIES.keys()) + ["BTC B&H"]
    col_w = 30
    row_fmt = f"{{:<{col_w}s}}" + f" | {{:>16s}}" * (len(STRATEGIES)) + " | {:>10s}"
    print(row_fmt.format(*[h[:col_w] for h in headers]))
    print("-" * 120)

    for s, e, label in PERIODS:
        row = [label[:col_w]]
        for strat_name in STRATEGIES:
            r = results[strat_name][label]
            if "error" in r:
                row.append("ERROR")
            else:
                row.append(f"{r['total_return_pct']:+6.1f}%/DD{r['max_dd_pct']:.0f}%")
        row.append(f"{bh_by_period[label]:+.1f}%")
        print(row_fmt.format(*row))

    # ── 5年通期の複利最終残高 ──
    print(f"\n{'═' * 120}")
    print(f"💰 $10,000 → 5年後の最終残高（2020-2024 通期）")
    print(f"{'═' * 120}")
    final_label = "▶ 5年通期"
    finals = []
    for strat_name in STRATEGIES:
        r = results[strat_name][final_label]
        if "error" not in r:
            final_eq = INITIAL * (1 + r["total_return_pct"] / 100)
            finals.append((strat_name, final_eq, r["total_return_pct"],
                           r["monthly_avg"], r["max_dd_pct"]))
    # Buy&Hold も追加
    bh_final = INITIAL * (1 + bh_by_period[final_label] / 100)
    finals.append(("BTC Buy & Hold", bh_final, bh_by_period[final_label], None, None))

    # 降順
    finals.sort(key=lambda x: x[1], reverse=True)
    print(f"{'順位':4s} {'戦略':40s} {'最終残高':>15s}  {'5年利回':>10s}  {'月平均':>8s}  {'最大DD':>8s}")
    print("-" * 120)
    for i, (name, final, tot, mavg, dd) in enumerate(finals, 1):
        mavg_s = f"{mavg:+.2f}%" if mavg is not None else "—"
        dd_s   = f"{dd:.1f}%" if dd is not None else "—"
        print(f"{i:>4d}  {name:40s}  ${final:>12,.0f}  {tot:+9.1f}%  {mavg_s:>8s}  {dd_s:>8s}")

    # ── 最悪年 ──
    print(f"\n{'═' * 120}")
    print(f"⚠️  最悪年の成績（ロバストネス指標）")
    print(f"{'═' * 120}")
    for strat_name in STRATEGIES:
        worst = None
        worst_label = None
        for s, e, label in PERIODS:
            if label == final_label: continue  # 通期は除く
            r = results[strat_name].get(label, {})
            if "error" in r: continue
            ret = r.get("total_return_pct", 0)
            if worst is None or ret < worst:
                worst = ret
                worst_label = label
        print(f"  {strat_name:40s}: 最悪 {worst_label:25s}  {worst:+.2f}%")

    # JSON保存
    out_path = (Path(__file__).resolve().parent / "results" / "top2_5year.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_data = {
        "strategies": results,
        "btc_buy_hold": bh_by_period,
        "periods": [{"start": s, "end": e, "label": l} for s, e, l in PERIODS],
        "initial_capital": INITIAL,
    }
    out_path.write_text(json.dumps(save_data, indent=2, ensure_ascii=False))
    print(f"\n💾 {out_path}")


if __name__ == "__main__":
    run()
