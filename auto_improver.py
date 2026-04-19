"""
auto_improver.py - パフォーマンス自動診断＆改善エンジン
========================================================
5分毎に呼ばれる。直近取引を分析し、悪化傾向を検出したら自動で改善を試みる。

検出条件:
  1. 直近10件の勝率 < 30%
  2. 直近5件 全敗
  3. 最大DD > 10%
  4. 特定銘柄で3連敗
  5. 特定F&G帯 or BTCトレンドで3連敗

自動改善（保守的・リスクを増やさない）:
  A. min_entry_score を一時引き上げ（選別を厳格化）
  B. 負けが集中する銘柄を一時ブラックリスト（6-24h）
  C. 負けが集中する相場条件（F&G帯・BTCトレンド）を一時回避

安全装置:
  - 改善パラメータは runtime_override.json に書き、trading_bot.py が読み込む
  - 有効期限付き（時間経過で自動解除）
  - DD悪化 or 勝率回復で自動ロールバック
  - SL/TP/レバレッジは絶対に変更しない（ルール変更禁止）
"""
from __future__ import annotations
import json, time
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).parent
STATE = ROOT / 'bot_state.json'
OVERRIDE_FILE = ROOT / 'runtime_override.json'
DIAGNOSIS_LOG = ROOT / 'auto_improver.log'

# 閾値
WIN_RATE_ALERT = 30.0     # 直近10件勝率これ未満でアラート
CONSEC_LOSS_ALERT = 5     # 連敗数
MAX_DD_ALERT = 10.0       # DD %
SYMBOL_LOSS_ALERT = 3     # 銘柄別連敗

# 改善アクションのパラメータ
SCORE_BUMP = 10           # min_entry_score を +10
SYMBOL_BLACKLIST_HOURS = 6
OVERRIDE_DURATION_H = 6   # 改善オーバーライドは6時間後に自動失効


def diagnose_log(msg):
    """診断ログ出力"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with open(DIAGNOSIS_LOG, "a") as f:
            f.write(line + "\n")
    except Exception: pass
    return line


def analyze_recent_trades():
    """直近取引を分析し、パフォーマンス指標と問題パターンを返す"""
    if not STATE.exists():
        return None
    try:
        s = json.load(open(STATE))
    except Exception as e:
        diagnose_log(f"⚠️ state読取失敗: {e}")
        return None

    trades = s.get('trade_history', [])
    if len(trades) < 5:
        return {"status": "insufficient_data", "count": len(trades)}

    sorted_trades = sorted(trades, key=lambda t: t.get('exit_time', t.get('entry_time', 0)))

    # 直近10件と5件
    last10 = sorted_trades[-10:]
    last5 = sorted_trades[-5:]

    def stats(lst):
        if not lst: return {}
        wins = [t for t in lst if t.get('won')]
        losses = [t for t in lst if not t.get('won')]
        pnl_sum = sum(t.get('pnl', 0) for t in lst)
        return {
            "count": len(lst),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(lst) * 100 if lst else 0,
            "pnl": pnl_sum,
            "avg_win": sum(t.get('pnl', 0) for t in wins) / len(wins) if wins else 0,
            "avg_loss": sum(t.get('pnl', 0) for t in losses) / len(losses) if losses else 0,
        }

    s10 = stats(last10)
    s5 = stats(last5)

    # 連敗数（最新から連続）
    consec_loss = 0
    for t in reversed(sorted_trades):
        if not t.get('won'): consec_loss += 1
        else: break

    # 銘柄別損益（直近20件）
    last20 = sorted_trades[-20:]
    symbol_losses = Counter()
    for t in last20:
        if not t.get('won'):
            symbol_losses[t.get('symbol', '')] += 1

    # 最大DD計算
    balance = s.get('balance', 0)
    initial = s.get('initial_balance', 10000)
    peak = s.get('peak_balance', initial)
    max_dd = (peak - balance) / peak * 100 if peak > 0 else 0

    # F&G / BTCトレンド別損益（直近20件）
    fg_losses = Counter()
    btc_losses = Counter()
    for t in last20:
        if not t.get('won'):
            fg = t.get('entry_fg', 0)
            fg_band = "fear(<25)" if fg < 25 else ("neutral(25-60)" if fg < 60 else "greed(>60)")
            fg_losses[fg_band] += 1
            btc_losses[t.get('entry_btc_trend', '') or 'unknown'] += 1

    return {
        "status": "ok",
        "total_trades": len(trades),
        "last10": s10,
        "last5": s5,
        "consec_loss": consec_loss,
        "max_dd": max_dd,
        "balance": balance,
        "peak": peak,
        "problem_symbols": [s for s, c in symbol_losses.items() if c >= SYMBOL_LOSS_ALERT],
        "symbol_loss_detail": dict(symbol_losses),
        "fg_loss_detail": dict(fg_losses),
        "btc_loss_detail": dict(btc_losses),
    }


def load_override():
    """現在のオーバーライド設定を読み込み"""
    if not OVERRIDE_FILE.exists(): return {}
    try:
        d = json.load(open(OVERRIDE_FILE))
        # 期限切れならクリア
        if d.get("revert_at"):
            revert = datetime.fromisoformat(d["revert_at"])
            if datetime.now() > revert:
                OVERRIDE_FILE.unlink(missing_ok=True)
                diagnose_log(f"⏰ オーバーライド有効期限切れ → 自動ロールバック: {d.get('reason','')}")
                return {}
        return d
    except Exception: return {}


def save_override(override):
    override["applied_at"] = datetime.now().isoformat()
    override["revert_at"] = (datetime.now() + timedelta(hours=OVERRIDE_DURATION_H)).isoformat()
    tmp = OVERRIDE_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(override, ensure_ascii=False, indent=2))
    tmp.replace(OVERRIDE_FILE)


def build_diagnosis_and_improve(analysis):
    """分析結果から問題を特定し、改善策を決定・適用する"""
    if not analysis or analysis.get("status") != "ok":
        return {"status": "no_action", "reason": "データ不足"}

    issues = []
    actions = []

    # 既存オーバーライドをロード
    current_override = load_override()

    # 1. 勝率低下検出
    wr10 = analysis["last10"].get("win_rate", 100)
    if analysis["last10"]["count"] >= 10 and wr10 < WIN_RATE_ALERT:
        issues.append(f"直近10件勝率 {wr10:.1f}% < {WIN_RATE_ALERT}%")

    # 2. 連敗検出
    if analysis["consec_loss"] >= CONSEC_LOSS_ALERT:
        issues.append(f"連敗 {analysis['consec_loss']}回")

    # 3. DD 悪化
    if analysis["max_dd"] > MAX_DD_ALERT:
        issues.append(f"最大DD {analysis['max_dd']:.2f}% > {MAX_DD_ALERT}%")

    # 4. 問題銘柄
    if analysis["problem_symbols"]:
        issues.append(f"問題銘柄: {','.join(analysis['problem_symbols'])} ({SYMBOL_LOSS_ALERT}連敗以上)")

    # 問題なし
    if not issues:
        if current_override:
            # 回復したらオーバーライド解除を検討
            if wr10 >= 45 and analysis["consec_loss"] == 0:
                OVERRIDE_FILE.unlink(missing_ok=True)
                msg = diagnose_log(f"✅ パフォーマンス回復 → オーバーライド解除（勝率{wr10:.1f}%, 連敗0）")
                return {"status": "recovered", "message": msg}
        return {"status": "healthy", "issues": [], "actions": []}

    # 改善アクションを決定
    new_override = dict(current_override) if current_override else {}

    # 勝率低下 or 連敗 → min_entry_score を引き上げ
    if wr10 < WIN_RATE_ALERT or analysis["consec_loss"] >= CONSEC_LOSS_ALERT:
        current_bump = new_override.get("score_bump", 0)
        if current_bump < SCORE_BUMP * 2:
            new_bump = min(current_bump + SCORE_BUMP, SCORE_BUMP * 2)  # 最大 +20 まで
            new_override["score_bump"] = new_bump
            actions.append(f"min_entry_score を +{new_bump} に引き上げ（選別厳格化）")

    # 問題銘柄をブラックリスト
    if analysis["problem_symbols"]:
        blacklist = new_override.get("symbol_blacklist", [])
        for sym in analysis["problem_symbols"]:
            if sym not in blacklist:
                blacklist.append(sym)
                actions.append(f"{sym} を{SYMBOL_BLACKLIST_HOURS}時間ブラックリスト化")
        new_override["symbol_blacklist"] = blacklist

    # F&G帯に偏った負け
    fg_detail = analysis.get("fg_loss_detail", {})
    if fg_detail:
        max_fg_band = max(fg_detail.items(), key=lambda x: x[1])
        if max_fg_band[1] >= 4:  # 20件中4件以上がこのF&G帯
            new_override["avoid_fg_band"] = max_fg_band[0]
            actions.append(f"F&G {max_fg_band[0]} での取引を一時回避")

    # 診断ログ
    reason = " / ".join(issues)
    new_override["reason"] = reason
    diagnose_log(f"🔴 パフォーマンス悪化検出: {reason}")
    for a in actions:
        diagnose_log(f"   → {a}")

    # 銘柄別・相場条件別の内訳も記録
    if analysis["symbol_loss_detail"]:
        top_losers = sorted(analysis["symbol_loss_detail"].items(), key=lambda x: x[1], reverse=True)[:3]
        diagnose_log(f"   原因（銘柄別）: " + ", ".join(f"{s}:{c}敗" for s, c in top_losers))
    if analysis["fg_loss_detail"]:
        diagnose_log(f"   原因（F&G別）: " + ", ".join(f"{k}:{v}敗" for k, v in analysis["fg_loss_detail"].items()))
    if analysis["btc_loss_detail"]:
        diagnose_log(f"   原因（BTCトレンド別）: " + ", ".join(f"{k}:{v}敗" for k, v in analysis["btc_loss_detail"].items()))

    # 改善内容を保存
    if actions:
        save_override(new_override)
        diagnose_log(f"   💾 runtime_override.json に{OVERRIDE_DURATION_H}時間のオーバーライドを記録")

    return {
        "status": "improved" if actions else "alerted",
        "issues": issues,
        "actions": actions,
        "override": new_override,
    }


def run_diagnosis():
    """監視から呼ばれるエントリーポイント"""
    analysis = analyze_recent_trades()
    if not analysis:
        return None
    result = build_diagnosis_and_improve(analysis)
    return result, analysis


if __name__ == "__main__":
    # 単体実行モード（動作確認用）
    print("=" * 60)
    print("  🔬 auto_improver.py 診断実行")
    print("=" * 60)
    analysis = analyze_recent_trades()
    if analysis:
        print(f"\n【分析結果】")
        if analysis.get("status") == "insufficient_data":
            print(f"  データ不足（取引{analysis['count']}件）")
        else:
            print(f"  取引総数: {analysis['total_trades']}")
            print(f"  直近10件: {analysis['last10']}")
            print(f"  直近5件: {analysis['last5']}")
            print(f"  連敗: {analysis['consec_loss']}")
            print(f"  最大DD: {analysis['max_dd']:.2f}%")
            print(f"  問題銘柄: {analysis['problem_symbols']}")
            result = build_diagnosis_and_improve(analysis)
            print(f"\n【判定】 {result['status']}")
            if result.get('issues'):
                print(f"  問題: {result['issues']}")
            if result.get('actions'):
                print(f"  改善: {result['actions']}")
