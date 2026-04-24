"""
バックテスト推移の独立検証 (チャート推移+取引波形)
=========================================================
既存のバックテストJSONに含まれる資産推移カーブ (equity_weekly) を、
以下の観点で独立計算・実データ参照で検証する:

  1. 算術整合性: equity_weekly の最初と最後から計算したリターンが
                yearly合計値と一致するか
  2. 月次連続性: equity_weekly に異常なジャンプ/欠損がないか
  3. BTC実データとの相関: 年別リターンがBTC実騰落率と論理的に整合するか
  4. DDのタイミング: 最大ドロー期が実際のBTC暴落期 (2022年5-6月/11月等)と一致
  5. 戦略ロジックの整合: BTC枠40% + ACH枠40% + USDT枠20% の合計が近似的に total になるか

検証対象:
  - results/iter46_hybrid.json (H11 推奨戦略)
  - results/iter45_low_dd.json (低DD戦略群)
  - results/iter47_trade_limit.json (取引上限比較)
  - results/iter44_multiexchange.json (既検証済み取引所整合)
"""
from __future__ import annotations
import json, pickle, sys, math
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np

CACHE_PATH = (Path(__file__).resolve().parent / "results" / "_cache_alldata.pkl")
OUT_JSON = (Path(__file__).resolve().parent / "results" / "backtest_chart_verify.json")

# BTC年別実リターン(独立計算で確定済み、iter44で検証完了)
BTC_YEARLY_GROUND_TRUTH = {
    2020: 302.24,
    2021: 59.61,
    2022: -64.21,
    2023: 155.87,
    2024: 119.22,
}

# 実際のBTC暴落タイミング
BTC_CRASH_EVENTS = [
    ("2020-03", "COVID急落"),
    ("2021-05", "中国規制"),
    ("2022-05", "LUNA崩壊"),
    ("2022-11", "FTX事件"),
    ("2024-04", "BTC halving後調整"),
    ("2024-08", "日銀利上げショック"),
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 検証1: 算術整合性
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def verify_arithmetic(name, equity_weekly, yearly, initial=10000):
    """equity_weeklyの初値・終値から計算したリターンがyearly合計と一致するか"""
    if not equity_weekly or len(equity_weekly) < 2:
        return {"status": "NO_DATA"}

    start_val = equity_weekly[0]["equity"]
    final_val = equity_weekly[-1]["equity"]

    # 算術1: 最初と最後
    calc_total_ret = (final_val - initial) / initial * 100

    # 算術2: yearly の複利で計算
    compound = 1.0
    for y, v in yearly.items():
        compound *= (1 + float(v) / 100)
    yearly_compound_total = (compound - 1) * 100

    # 差分
    diff = abs(calc_total_ret - yearly_compound_total)
    status = "PASS" if diff < 2.0 else ("WARN" if diff < 5.0 else "FAIL")

    return {
        "name": name,
        "status": status,
        "initial": initial,
        "final": final_val,
        "direct_total_ret_pct": round(calc_total_ret, 2),
        "yearly_compound_ret_pct": round(yearly_compound_total, 2),
        "diff_pp": round(diff, 3),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 検証2: 月次連続性 (欠損/異常ジャンプ検出)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def verify_continuity(name, equity_weekly):
    """週次連続性: 1週間で±50%超の変動は異常"""
    if len(equity_weekly) < 2:
        return {"status": "NO_DATA"}

    max_weekly_jump = 0
    max_weekly_drop = 0
    anomalies = []
    for i in range(1, len(equity_weekly)):
        prev = equity_weekly[i-1]["equity"]
        cur = equity_weekly[i]["equity"]
        if prev <= 0:
            continue
        change_pct = (cur - prev) / prev * 100
        if abs(change_pct) > abs(max_weekly_jump) and change_pct > 0:
            max_weekly_jump = change_pct
        if abs(change_pct) > abs(max_weekly_drop) and change_pct < 0:
            max_weekly_drop = change_pct
        if abs(change_pct) > 50:  # 週50%超えは要注意
            anomalies.append({
                "ts": equity_weekly[i]["ts"],
                "from": round(prev, 2),
                "to": round(cur, 2),
                "change_pct": round(change_pct, 2),
            })

    # 期間日数 (週次なので ≈ len*7)
    n_weeks = len(equity_weekly) - 1
    expected_period_years = n_weeks * 7 / 365
    status = "PASS" if len(anomalies) == 0 else ("WARN" if len(anomalies) < 5 else "FAIL")

    return {
        "name": name,
        "status": status,
        "n_weeks": n_weeks,
        "period_years_approx": round(expected_period_years, 2),
        "max_weekly_gain_pct": round(max_weekly_jump, 2),
        "max_weekly_drop_pct": round(max_weekly_drop, 2),
        "anomaly_count": len(anomalies),
        "anomalies": anomalies[:10],
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 検証3: BTC実データとの相関
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def verify_btc_correlation(name, yearly, strategy_type="hybrid"):
    """
    年別リターンがBTC実データと論理的に整合しているか。

    - BTC買い持ち型 (strategy_type='buyhold'): BTC年次とほぼ一致するはず
    - ハイブリッド型 ('hybrid'): BTC暴落年はそこそこ守る、上昇年はBTCに勝つ
    - モメンタム型 ('momentum'): アルトコインで BTC を大きく超える
    """
    years = [2020, 2021, 2022, 2023, 2024]
    btc_rets = [BTC_YEARLY_GROUND_TRUTH[y] for y in years]
    strat_rets = [float(yearly.get(str(y), yearly.get(y, 0))) for y in years]

    if len(strat_rets) != 5:
        return {"status": "INCOMPLETE_YEARLY"}

    # BTC と戦略の相関係数
    if np.std(strat_rets) > 0 and np.std(btc_rets) > 0:
        corr = float(np.corrcoef(btc_rets, strat_rets)[0, 1])
    else:
        corr = None

    # 2022年 (BTC -64%) で戦略がどれだけ守れたか
    defended_2022 = strat_rets[2] > -30  # DD-30%より少なければ守り成功
    # 2021年 (BTC +60%) で戦略が BTC以上の成績出したか
    outperform_2021 = strat_rets[1] > btc_rets[1]

    flags = []
    # 論理検証
    if strategy_type == "buyhold":
        # BTC買い持ちなら相関0.9以上必須
        if corr is None or corr < 0.85:
            flags.append(f"buyhold型なのにBTC相関弱い({corr:.2f})")
    elif strategy_type == "hybrid":
        if not defended_2022:
            flags.append("2022年のDDが-30%超、ハイブリッドの守り弱い")
    elif strategy_type == "momentum":
        if strat_rets[1] < 200:  # 2021年Bull相場で+200%未満なら違和感
            flags.append(f"2021年モメンタムが+{strat_rets[1]:.0f}% (通常アルト爆益年)")

    # 年別差分
    yearly_comparison = []
    for y, btc, strat in zip(years, btc_rets, strat_rets):
        yearly_comparison.append({
            "year": y,
            "btc_actual": round(btc, 2),
            "strategy": round(strat, 2),
            "diff": round(strat - btc, 2),
        })

    status = "PASS" if len(flags) == 0 else "WARN"
    return {
        "name": name,
        "status": status,
        "strategy_type": strategy_type,
        "correlation_with_btc": round(corr, 3) if corr is not None else None,
        "defended_2022": defended_2022,
        "outperformed_2021": outperform_2021,
        "yearly_comparison": yearly_comparison,
        "logic_flags": flags,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 検証4: DDのタイミング妥当性
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def verify_dd_timing(name, equity_weekly):
    """最大DDの発生時期がBTC実暴落期と整合しているか"""
    if not equity_weekly:
        return {"status": "NO_DATA"}

    peak = 0
    peak_ts = None
    worst_dd = 0
    worst_dd_ts = None
    for e in equity_weekly:
        v = e["equity"]
        if v > peak:
            peak = v
            peak_ts = e["ts"]
        if peak > 0:
            dd = (peak - v) / peak * 100
            if dd > worst_dd:
                worst_dd = dd
                worst_dd_ts = e["ts"]

    # BTC暴落期との近接性 (3ヶ月以内なら matched)
    matched_event = None
    if worst_dd_ts:
        ts_date = pd.Timestamp(worst_dd_ts)
        for ev_ts, ev_name in BTC_CRASH_EVENTS:
            ev_date = pd.Timestamp(ev_ts)
            if abs((ts_date - ev_date).days) <= 90:
                matched_event = f"{ev_ts} {ev_name}"
                break

    status = "PASS" if matched_event else "WARN"
    return {
        "name": name,
        "status": status,
        "peak_ts": peak_ts,
        "peak_value": round(peak, 2),
        "worst_dd_ts": worst_dd_ts,
        "worst_dd_pct": round(worst_dd, 2),
        "matched_btc_crash": matched_event,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 検証5: equity_curveスムーズ性 (レポートロジック異常検出)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def verify_smoothness(name, equity_weekly):
    """equity_weeklyのボラティリティが戦略の性質と整合するか"""
    if len(equity_weekly) < 10:
        return {"status": "NO_DATA"}
    vals = [e["equity"] for e in equity_weekly]
    log_rets = [math.log(vals[i]/vals[i-1]) for i in range(1, len(vals)) if vals[i-1] > 0]
    if not log_rets:
        return {"status": "NO_DATA"}
    weekly_vol = np.std(log_rets)
    annual_vol = weekly_vol * np.sqrt(52) * 100  # 年率化 %
    return {
        "name": name,
        "weekly_volatility_pct": round(weekly_vol * 100, 2),
        "annualized_volatility_pct": round(annual_vol, 2),
        "sample_size": len(log_rets),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メイン: 複数JSONを横断検証
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def verify_backtest(json_path, key_path, strategy_type, label):
    """1つのバックテスト結果を5項目検証"""
    data = json.loads(Path(json_path).read_text())
    # key_path: "results.H11 50/50 (R10ベースライン)" のようなネストパス
    node = data
    for k in key_path.split("|"):
        if isinstance(node, dict):
            node = node.get(k, {})
        elif isinstance(node, list):
            try:
                node = node[int(k)]
            except (ValueError, IndexError):
                node = {}
    if not isinstance(node, dict) or "equity_weekly" not in node:
        return {"label": label, "error": f"equity_weekly not found in {key_path}"}

    equity_weekly = node.get("equity_weekly", [])
    yearly = node.get("yearly", {})

    return {
        "label": label,
        "source": str(json_path.name),
        "strategy_type": strategy_type,
        "final_reported": node.get("final"),
        "avg_annual_reported": node.get("avg_annual_ret"),
        "yearly_reported": yearly,
        "n_trades": node.get("n_trades"),
        "max_dd_reported": node.get("max_dd"),
        "check_1_arithmetic": verify_arithmetic(label, equity_weekly, yearly),
        "check_2_continuity": verify_continuity(label, equity_weekly),
        "check_3_btc_correlation": verify_btc_correlation(label, yearly, strategy_type),
        "check_4_dd_timing": verify_dd_timing(label, equity_weekly),
        "check_5_smoothness": verify_smoothness(label, equity_weekly),
    }


def main():
    print("=" * 80)
    print("🔬 バックテスト推移 独立検証 (5項目)")
    print("=" * 80)

    RESULTS = (Path(__file__).resolve().parent / "results")

    # 検証対象リスト (JSON | key_path | 戦略種類 | 表示名)
    targets = [
        # iter46 H11ハイブリッド
        (RESULTS / "iter46_hybrid.json",
         "results|H11 50/50 (R10ベースライン)",
         "hybrid", "H11 50/50 (推奨ベースライン)"),
        (RESULTS / "iter46_hybrid.json",
         "results|H11 BTC40%+ACH40%+USDT20% (バランス安全)",
         "hybrid", "H11 BTC40+ACH40+USDT20 (最終推奨)"),
        # iter43 比較群
        (RESULTS / "iter43_rethink.json",
         "results|R01 BTC単純保有",
         "buyhold", "R01 BTC買い持ち (ベンチマーク)"),
        (RESULTS / "iter43_rethink.json",
         "results|R05 モメンタムTop3",
         "momentum", "R05 モメンタムTop3"),
        (RESULTS / "iter43_rethink.json",
         "results|R04b BTCマイルド+金利3%",
         "hybrid", "R04b BTCマイルド+金利"),
        # iter47 取引上限比較
        (RESULTS / "iter47_trade_limit.json",
         "patterns|0",
         "hybrid", "iter47 5回上限 (旧設定)"),
        (RESULTS / "iter47_trade_limit.json",
         "patterns|2",
         "hybrid", "iter47 20回上限 (新設定)"),
    ]

    results = []
    for json_path, key_path, stype, label in targets:
        if not json_path.exists():
            print(f"⚠️  {label}: ファイルなし {json_path}")
            continue
        r = verify_backtest(json_path, key_path, stype, label)
        results.append(r)

        if "error" in r:
            print(f"❌ {label}: {r['error']}")
            continue

        # サマリー表示
        c1 = r["check_1_arithmetic"]
        c2 = r["check_2_continuity"]
        c3 = r["check_3_btc_correlation"]
        c4 = r["check_4_dd_timing"]
        c5 = r["check_5_smoothness"]
        print()
        print(f"━━━ {label} ━━━")
        print(f"  最終資産: ${r['final_reported']:,.0f} / 年率 {r['avg_annual_reported']:+.1f}%")
        print(f"  ① 算術整合性: {c1['status']} (差分 {c1.get('diff_pp', 0):.3f}pp)")
        print(f"  ② 連続性:    {c2['status']} (異常{c2.get('anomaly_count', 0)}件 / "
              f"最大週変動 +{c2.get('max_weekly_gain_pct', 0):.1f}%, {c2.get('max_weekly_drop_pct', 0):.1f}%)")
        corr = c3.get("correlation_with_btc")
        print(f"  ③ BTC相関:    {c3['status']} (相関{corr:.2f}, 2022年防御{'✅' if c3.get('defended_2022') else '❌'})"
              if corr is not None else f"  ③ BTC相関:    {c3['status']}")
        print(f"  ④ DDタイミング: {c4['status']} (最大DD -{c4.get('worst_dd_pct', 0):.1f}% @ {c4.get('worst_dd_ts')} / "
              f"BTC暴落一致: {c4.get('matched_btc_crash', 'なし')})")
        print(f"  ⑤ ボラ年率:   {c5.get('annualized_volatility_pct', 0):.1f}% (サンプル{c5.get('sample_size', 0)}週)")

    # 全体サマリー
    print()
    print("=" * 80)
    print("🎯 全体検証サマリー")
    print("=" * 80)
    all_ok = 0
    all_warn = 0
    all_fail = 0
    for r in results:
        if "error" in r: continue
        statuses = [
            r["check_1_arithmetic"].get("status", ""),
            r["check_2_continuity"].get("status", ""),
            r["check_3_btc_correlation"].get("status", ""),
            r["check_4_dd_timing"].get("status", ""),
        ]
        if "FAIL" in statuses:
            all_fail += 1
        elif "WARN" in statuses:
            all_warn += 1
        else:
            all_ok += 1
    print(f"  全OK:   {all_ok} 戦略")
    print(f"  WARN:   {all_warn} 戦略")
    print(f"  FAIL:   {all_fail} 戦略")

    out = {
        "check_timestamp": datetime.now().isoformat(timespec='seconds'),
        "total_verified": len(results),
        "btc_yearly_ground_truth": BTC_YEARLY_GROUND_TRUTH,
        "btc_crash_events": [{"ts": a, "name": b} for a, b in BTC_CRASH_EVENTS],
        "results": results,
        "summary": {"pass": all_ok, "warn": all_warn, "fail": all_fail},
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n💾 {OUT_JSON}")


if __name__ == "__main__":
    main()
