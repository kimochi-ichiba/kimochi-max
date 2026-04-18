#!/usr/bin/env python3
"""
analyze_performance.py — ボット成績の簡易分析ツール
====================================================
使い方: python3 analyze_performance.py
"""
import json
from collections import defaultdict
from datetime import datetime

def run():
    d = json.load(open("bot_state.json"))
    history = d.get("trade_history", [])
    balance = d.get("balance", 0)
    initial = d.get("initial_balance", 10000)

    if not history:
        print("取引履歴がありません。")
        return

    wins   = [t for t in history if t.get("pnl", 0) > 0]
    losses = [t for t in history if t.get("pnl", 0) <= 0]
    total_pnl = sum(t.get("pnl", 0) for t in history)
    avg_win   = sum(t.get("pnl", 0) for t in wins)   / len(wins)   if wins   else 0
    avg_loss  = sum(t.get("pnl", 0) for t in losses) / len(losses) if losses else 0

    print("=" * 55)
    print("  ボット成績レポート")
    print("=" * 55)
    print(f"  残高:      ${balance:>10,.2f}  (開始: ${initial:,.2f})")
    print(f"  損益:      ${balance-initial:>+10,.2f}  ({(balance/initial-1)*100:+.2f}%)")
    print(f"  ポジション: {len(d.get('positions', {}))}件")
    print()
    print(f"  総トレード: {len(history)}件")
    print(f"  勝ち:       {len(wins)}件  ({len(wins)/len(history)*100:.1f}%)")
    print(f"  負け:       {len(losses)}件  ({len(losses)/len(history)*100:.1f}%)")
    print(f"  平均利益:   ${avg_win:+.2f}")
    print(f"  平均損失:   ${avg_loss:+.2f}")
    rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    print(f"  RR比:       {rr:.2f}:1  (目標: 2.5:1以上)")
    breakeven_wr = 1 / (1 + rr) * 100 if rr > 0 else 99
    print(f"  損益分岐勝率: {breakeven_wr:.1f}%  (現在{len(wins)/len(history)*100:.1f}%)")
    if len(wins)/len(history)*100 > breakeven_wr:
        print("  ★ 現在の設定では期待値プラスです！")
    else:
        print(f"  ▲ あと{breakeven_wr - len(wins)/len(history)*100:.1f}%勝率を上げると損益分岐")
    print()

    # 決済タイプ別
    print("  【決済タイプ別】")
    from collections import Counter
    for reason in ["tp", "trailing", "sl", "timeout"]:
        group = [t for t in history if t.get("exit_reason") == reason]
        if group:
            w = len([t for t in group if t.get("pnl", 0) > 0])
            p = sum(t.get("pnl", 0) for t in group)
            print(f"  {reason:10s}: {len(group):3d}件 勝率{w/len(group)*100:4.0f}% 合計${p:+.2f}")

    print()
    # ロング vs ショート
    longs  = [t for t in history if t.get("side") == "long"]
    shorts = [t for t in history if t.get("side") == "short"]
    lw = len([t for t in longs  if t.get("pnl", 0) > 0])
    sw = len([t for t in shorts if t.get("pnl", 0) > 0])
    print("  【ロング vs ショート】")
    if longs:
        print(f"  ロング:  {len(longs):3d}件 勝率{lw/len(longs)*100:4.0f}% 合計${sum(t.get('pnl',0) for t in longs):+.2f}")
    if shorts:
        print(f"  ショート: {len(shorts):3d}件 勝率{sw/len(shorts)*100:4.0f}% 合計${sum(t.get('pnl',0) for t in shorts):+.2f}")

    print()
    # 損失ワースト5銘柄
    sym_pnl = defaultdict(float)
    for t in history:
        sym_pnl[t.get("symbol", "?").replace("/USDT", "")] += t.get("pnl", 0)
    worst = sorted(sym_pnl.items(), key=lambda x: x[1])[:5]
    print("  【損失ワースト5銘柄】")
    for sym, pnl in worst:
        print(f"  {sym:12s}: ${pnl:+.2f}")

    print()
    # 直近10件 v23.0: score/F&G/BTCも表示
    print("  【直近10件】")
    for t in history[-10:]:
        mark = "✅" if t.get("pnl", 0) > 0 else "❌"
        sc   = t.get("entry_score", 0)
        fg   = t.get("entry_fg", 0)
        btc  = t.get("entry_btc_trend", "?")
        sc_s = f" sc={sc:.0f}" if sc > 0 else ""
        fg_s = f" F&G={fg}" if fg > 0 else ""
        btc_s = f" BTC={btc}" if btc else ""
        print(f"  {mark} {t.get('symbol','?'):14s} {t.get('side','?'):5s} "
              f"${t.get('pnl',0):+.2f} ({t.get('exit_reason','?')}){sc_s}{fg_s}{btc_s}")

    # v23.0: BTCトレンド別・F&G帯別の統計（データがある場合のみ）
    meta_trades = [t for t in history if t.get("entry_btc_trend")]
    if meta_trades:
        print()
        print("  【BTCトレンド別成績（v23.0データ）】")
        for trend in ["up", "down", "range"]:
            grp = [t for t in meta_trades if t.get("entry_btc_trend") == trend]
            if grp:
                ww = len([t for t in grp if t.get("pnl", 0) > 0])
                pp = sum(t.get("pnl", 0) for t in grp)
                print(f"  BTC={trend:5s}: {len(grp):3d}件 勝率{ww/len(grp)*100:4.0f}% 合計${pp:+.2f}")
        print()
        print("  【F&G帯別成績（v23.0データ）】")
        bands = [("Extreme Fear", 0, 25), ("Fear", 26, 49), ("Neutral+", 50, 100)]
        for label, lo, hi in bands:
            grp = [t for t in meta_trades if lo <= t.get("entry_fg", 0) <= hi]
            if grp:
                ww = len([t for t in grp if t.get("pnl", 0) > 0])
                pp = sum(t.get("pnl", 0) for t in grp)
                print(f"  {label:15s}: {len(grp):3d}件 勝率{ww/len(grp)*100:4.0f}% 合計${pp:+.2f}")

    print("=" * 55)

if __name__ == "__main__":
    run()
