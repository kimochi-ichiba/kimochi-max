"""iter71e: v2.5 の Walk-Forward 検証 (v2.4 vs v2.5 の OOS 直接対決).

iter71d の v2.5 提案 (multi_lookback + 配分 25/25/50) は、3.3 年データ全体で
最良案を選んだもので、典型的なデータスヌーピング構造。本当に未来でも効くかは
未検証だった。

このスクリプトは iter71 と同じ 4 窓 (W1-W4) の IS/OOS で v2.4 と v2.5 を直接
対決させ、OOS でも v2.5 が勝つかを確認する。

判定基準:
- 4/4 窓で OOS 改善 → 本物 (90 点級)
- 2-3/4 窓で OOS 改善 → 部分的 (70 点級)
- 0-1/4 窓 → 過学習確定 (40 点以下)

multi_lookback 単独 / 配分変更単独 の効果も切り分けて報告。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wf_validate_v24 import (
    WINDOWS,
    load_cache,
    make_universe,
    run_bt_v24,
)

CONFIGS = [
    {"id": "v2.4",       "name": "v2.4 (PR #8 既定値)",
     "kwargs": {
         "btc_weight": 0.35, "ach_weight": 0.35, "usdt_weight": 0.30,
         "multi_lookback": False,
     }},
    {"id": "v2.5_multi", "name": "v2.5 multi_lookback のみ (35/35/30 + multi_lb)",
     "kwargs": {
         "btc_weight": 0.35, "ach_weight": 0.35, "usdt_weight": 0.30,
         "multi_lookback": True,
     }},
    {"id": "v2.5_alloc", "name": "v2.5 配分のみ (25/25/50, multi_lb なし)",
     "kwargs": {
         "btc_weight": 0.25, "ach_weight": 0.25, "usdt_weight": 0.50,
         "multi_lookback": False,
     }},
    {"id": "v2.5_full",  "name": "v2.5 フル (25/25/50 + multi_lb) ← PR #10",
     "kwargs": {
         "btc_weight": 0.25, "ach_weight": 0.25, "usdt_weight": 0.50,
         "multi_lookback": True,
     }},
]

# 共通固定値 (iter71 の A = PR #8 既定値)
COMMON = {
    "bull_ach_weight": 0.60,
    "trail_stop_ach": 0.30,
    "trail_stop_btc": 0.20,
}


def run_one(all_data, universe, start, end, kwargs) -> dict:
    r = run_bt_v24(all_data, universe, start, end, **COMMON, **kwargs)
    return {"total_ret": r.total_ret, "max_dd": r.max_dd, "sharpe": r.sharpe,
            "n_trail_ach": r.n_trail_ach, "n_trail_btc": r.n_trail_btc,
            "n_bear": r.n_bear_exits}


def main() -> None:
    all_data = load_cache()
    universe = make_universe(all_data)
    print(f"🌐 ユニバース: {len(universe)} 銘柄\n")

    # 全 32 BT 実行
    table: dict = {}  # table[window_id][config_id] = {"is": result, "oos": result}
    for win in WINDOWS:
        table[win["id"]] = {}
        for cfg in CONFIGS:
            is_r = run_one(all_data, universe, win["is_start"], win["is_end"], cfg["kwargs"])
            oos_r = run_one(all_data, universe, win["oos_start"], win["oos_end"], cfg["kwargs"])
            table[win["id"]][cfg["id"]] = {"is": is_r, "oos": oos_r}
            print(f"  {win['id']} {cfg['id']:<11}: IS {is_r['total_ret']:+7.1f}% / "
                  f"OOS {oos_r['total_ret']:+7.1f}% (DD {oos_r['max_dd']:.1f}%)")
        print()

    # === 判定: v2.5_full vs v2.4 の OOS 勝敗 ===
    oos_wins_full = 0
    oos_wins_multi = 0
    oos_wins_alloc = 0
    win_details = []
    for win in WINDOWS:
        v24_oos = table[win["id"]]["v2.4"]["oos"]["total_ret"]
        v25f_oos = table[win["id"]]["v2.5_full"]["oos"]["total_ret"]
        v25m_oos = table[win["id"]]["v2.5_multi"]["oos"]["total_ret"]
        v25a_oos = table[win["id"]]["v2.5_alloc"]["oos"]["total_ret"]
        full_win = v25f_oos > v24_oos
        multi_win = v25m_oos > v24_oos
        alloc_win = v25a_oos > v24_oos
        if full_win: oos_wins_full += 1
        if multi_win: oos_wins_multi += 1
        if alloc_win: oos_wins_alloc += 1
        win_details.append({
            "win": win["id"], "oos": f"{win['oos_start'][:7]}〜{win['oos_end'][:7]}",
            "v24": v24_oos, "v25_multi": v25m_oos, "v25_alloc": v25a_oos, "v25_full": v25f_oos,
            "full_win": full_win, "multi_win": multi_win, "alloc_win": alloc_win,
        })

    # 判定文言
    def verdict(wins: int) -> str:
        if wins == 4: return "✅ 本物 (90/100 点級)"
        if wins == 3: return "🟢 ほぼ本物 (75/100 点)"
        if wins == 2: return "🟡 部分的 (60/100 点)"
        if wins == 1: return "🟠 怪しい (45/100 点)"
        return "🔴 過学習 (30/100 点以下)"

    # === コンソール出力 ===
    print("\n" + "=" * 95)
    print("📊 v2.5 Walk-Forward 検証結果 (4 窓の OOS で v2.4 vs v2.5 直接対決)")
    print("=" * 95)
    print(f"{'窓':<4} {'OOS 期間':<22} {'v2.4 OOS':>10} {'v2.5 multi':>12} {'v2.5 alloc':>12} {'v2.5 full':>12} {'full 勝敗':>10}")
    print("-" * 95)
    for d in win_details:
        mark = "🏆 勝" if d["full_win"] else "❌ 負"
        print(f"{d['win']:<4} {d['oos']:<22} "
              f"{d['v24']:>+9.1f}% {d['v25_multi']:>+11.1f}% "
              f"{d['v25_alloc']:>+11.1f}% {d['v25_full']:>+11.1f}% {mark:>10}")

    print("\n🏆 各案の OOS 勝ち星 (4 窓中):")
    print(f"  v2.5 multi のみ:    {oos_wins_multi}/4  → {verdict(oos_wins_multi)}")
    print(f"  v2.5 配分のみ:      {oos_wins_alloc}/4  → {verdict(oos_wins_alloc)}")
    print(f"  v2.5 フル (PR #10): {oos_wins_full}/4  → {verdict(oos_wins_full)}")

    # === Markdown レポート ===
    lines = [
        "# iter71e: v2.5 の Walk-Forward 検証",
        "",
        "iter71d で v2.5 (multi_lookback + 配分 25/25/50) が v2.4 比 +4.7 万円 (10 万円基準)",
        "の改善を出したが、これは 3.3 年データ全体で選んだ設定を**同じデータで評価**した",
        "data snooping 構造だった。本検証は iter71 と同じ 4 窓 (W1-W4) の IS/OOS で v2.4 と",
        "v2.5 を直接対決させ、OOS でも改善するか確認した。",
        "",
        "## 結果: 各窓の OOS 成績",
        "",
        "| 窓 | OOS 期間 | v2.4 | v2.5 multi のみ | v2.5 配分のみ | **v2.5 フル (PR #10)** | full 勝敗 |",
        "|----|---------|------|----------------|--------------|----------------------|----------|",
    ]
    for d in win_details:
        mark = "🏆 勝" if d["full_win"] else "❌ 負"
        lines.append(
            f"| {d['win']} | {d['oos']} | {d['v24']:+.1f}% | {d['v25_multi']:+.1f}% | "
            f"{d['v25_alloc']:+.1f}% | **{d['v25_full']:+.1f}%** | {mark} |"
        )

    lines += [
        "",
        "## OOS 勝ち星 (4 窓中、A 比改善した窓数)",
        "",
        f"- **v2.5 multi のみ**: {oos_wins_multi}/4 → {verdict(oos_wins_multi)}",
        f"- **v2.5 配分のみ**: {oos_wins_alloc}/4 → {verdict(oos_wins_alloc)}",
        f"- **v2.5 フル (PR #10)**: {oos_wins_full}/4 → {verdict(oos_wins_full)}",
        "",
        "## 解釈",
        "",
    ]

    if oos_wins_full == 4:
        lines += [
            "✅ **PR #10 (v2.5) は本物**。全 4 窓の OOS で v2.4 を上回った。",
            "→ PR #10 のマージを強く推奨。SIM 継続観察しつつ、いずれ実資金投入候補へ。",
        ]
    elif oos_wins_full >= 2:
        lines += [
            f"🟡 **PR #10 は部分的に有効** ({oos_wins_full}/4 窓で勝ち)。",
            "→ マージ可だが、勝てなかった窓のパターンが現実に出たら損が出る。",
            "  SIM 継続観察を強く推奨。実資金投入は 6 ヶ月以上の SIM 結果を見てから。",
        ]
    elif oos_wins_full == 1:
        lines += [
            "🟠 **PR #10 は信頼性低い** (1/4 のみ勝ち)。",
            "→ 1 窓だけの勝ちは偶然の可能性が高い。マージは見送り推奨。",
            "  v2.4 (A) のまま据え置きが無難。",
        ]
    else:
        lines += [
            "🔴 **PR #10 は過学習確定**。OOS では一度も v2.4 に勝てなかった。",
            "→ PR #10 はクローズ推奨。v2.4 (A) で SIM 継続。",
            "  iter71d の +4.7 万円改善は data snooping によるまやかしだった。",
        ]

    lines += [
        "",
        "## 効果分離",
        "",
    ]
    if oos_wins_multi >= 2 and oos_wins_alloc < 2:
        lines.append("- **multi_lookback 単独でも有効** (配分変更は寄与せず)")
    elif oos_wins_alloc >= 2 and oos_wins_multi < 2:
        lines.append("- **配分変更単独でも有効** (multi_lookback は寄与せず)")
    elif oos_wins_full > max(oos_wins_multi, oos_wins_alloc):
        lines.append("- **multi_lookback + 配分変更の組み合わせで初めて効果**")
    elif oos_wins_full < max(oos_wins_multi, oos_wins_alloc):
        lines.append("- **組み合わせると逆効果**。単独の方が良い")
    else:
        lines.append("- multi_lookback と配分変更は独立に同程度の効果 (or 同程度の無効)")

    out = Path(__file__).resolve().parent / "results" / "wf_validate_v24" / "v25_wf_validate.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n💾 保存: {out}")


if __name__ == "__main__":
    main()
