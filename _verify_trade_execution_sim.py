"""
_verify_trade_execution_sim.py — バックテスト結果の構造的整合性検証

Phase 0-3: iter47_trade_limit.json に対して以下を検証:
  1. equity_weekly の日付が実在するカレンダー日か
  2. equity_weekly の週刻みに欠損がないか (±10日許容)
  3. 週次で ±50%超のジャンプがないか (合成データ/バグ検出)
  4. yearly 集計が equity_weekly の年末週と整合するか (±5%)
  5. blocked_events_sample の symbol が ACH_UNIVERSE に存在するか
  6. blocked_events_sample の date が実在日付か
  7. 5年連続カバー (2020-01-01 〜 2024-12-31 を網羅)

判定:
  すべての項目 PASS → PASS
  1項目でも軽度の逸脱 → WARN
  欠損/合成疑い/範囲外症状 → FAIL

出力: results/trade_execution_sim.{json,html}
"""
from __future__ import annotations
import sys, json
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT = Path("/Users/sanosano/projects/kimochi-max")
RESULTS_DIR = PROJECT / "results"
IN_JSON = RESULTS_DIR / "iter47_trade_limit.json"
OUT_JSON = RESULTS_DIR / "trade_execution_sim.json"
OUT_HTML = RESULTS_DIR / "trade_execution_sim.html"

# demo_runner.py L70-76 と同期
ACH_UNIVERSE = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "AVAX", "DOGE", "TRX", "DOT",
    "MATIC", "LINK", "UNI", "LTC", "ATOM", "NEAR", "ICP", "ETC", "XLM", "FIL",
    "APT", "ARB", "OP", "INJ", "SUI", "SEI", "TIA", "RUNE", "FTM", "ALGO",
    "SAND", "MANA", "AXS", "CHZ", "ENJ", "GRT", "AAVE", "MKR", "SNX", "CRV",
    "HBAR", "EOS", "VET", "THETA", "EGLD", "XTZ", "FLOW", "IOTA", "DASH", "ZEC",
}

EXPECTED_START = datetime(2020, 1, 1, tzinfo=timezone.utc)
EXPECTED_END = datetime(2024, 12, 31, tzinfo=timezone.utc)
WEEK_GAP_TOLERANCE_DAYS = 10  # 1週 ±3日
MAX_WEEKLY_JUMP_PCT = 50.0
YEARLY_VS_WEEKLY_TOLERANCE_PCT = 5.0


def parse_date(s: str):
    """ISO日付文字列をdatetimeに変換。失敗時は None"""
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def check_equity_weekly_structure(equity_weekly: list) -> dict:
    """weekly 推移の構造的妥当性"""
    issues = []
    points = []
    for i, pt in enumerate(equity_weekly):
        dt = parse_date(pt.get("ts", ""))
        eq = pt.get("equity")
        if dt is None:
            issues.append(f"index {i}: ts='{pt.get('ts')}' が無効日付")
            continue
        if not isinstance(eq, (int, float)):
            issues.append(f"index {i} ({pt.get('ts')}): equity が数値でない ({eq})")
            continue
        points.append((dt, eq, i))

    if len(points) < 2:
        return {"status": "FAIL", "issues": ["データ点が2未満"], "ok_count": 0, "total": len(equity_weekly)}

    # 週刻みチェック
    gap_issues = []
    jump_issues = []
    prev_dt, prev_eq, _ = points[0]
    for i in range(1, len(points)):
        dt, eq, idx = points[i]
        gap_days = (dt - prev_dt).days
        if abs(gap_days - 7) > WEEK_GAP_TOLERANCE_DAYS:
            gap_issues.append(f"index {idx} ({dt.date()}): 前週から {gap_days}日（期待7日±{WEEK_GAP_TOLERANCE_DAYS}）")
        if prev_eq > 0:
            change_pct = abs((eq - prev_eq) / prev_eq * 100)
            if change_pct > MAX_WEEKLY_JUMP_PCT:
                jump_issues.append(f"index {idx} ({dt.date()}): 週次変動 {change_pct:.1f}%（閾値{MAX_WEEKLY_JUMP_PCT}%）")
        prev_dt, prev_eq = dt, eq

    # 期間カバレッジ
    first_dt = points[0][0]
    last_dt = points[-1][0]
    coverage_ok = (first_dt <= EXPECTED_START + timedelta(days=10)
                   and last_dt >= EXPECTED_END - timedelta(days=14))
    if not coverage_ok:
        issues.append(f"期間カバー不足: {first_dt.date()} 〜 {last_dt.date()} "
                      f"(期待 {EXPECTED_START.date()} 〜 {EXPECTED_END.date()})")

    issues.extend(gap_issues[:5])  # 最初の5件のみ詳細
    issues.extend(jump_issues[:5])

    status = "PASS"
    if not coverage_ok or jump_issues:
        status = "FAIL"
    elif gap_issues:
        status = "WARN"

    return {
        "status": status,
        "total": len(equity_weekly),
        "ok_count": len(points),
        "first_date": str(first_dt.date()),
        "last_date": str(last_dt.date()),
        "coverage_ok": coverage_ok,
        "gap_issues_count": len(gap_issues),
        "jump_issues_count": len(jump_issues),
        "issues": issues[:15],
    }


def check_yearly_vs_weekly(yearly: dict, equity_weekly: list, initial: float) -> dict:
    """yearly 集計が equity_weekly の年末と整合するか"""
    if not yearly or not equity_weekly:
        return {"status": "WARN", "issues": ["yearly または equity_weekly が空"]}

    # year -> last weekly equity in that year
    year_to_last_eq = {}
    for pt in equity_weekly:
        dt = parse_date(pt.get("ts", ""))
        if dt is None:
            continue
        y = str(dt.year)
        year_to_last_eq[y] = pt.get("equity", 0)

    issues = []
    checks = []
    for year in sorted(yearly.keys()):
        yearly_ret = yearly[year]  # これは年率リターン% (例: 34.5)
        # 前年末 equity から yearly_ret% 増加 = 今年末 equity
        if year not in year_to_last_eq:
            issues.append(f"{year}年の weekly データが無い")
            continue
        actual_eq = year_to_last_eq[year]

        # 前年末 equity (weekly)
        prev_year = str(int(year) - 1)
        if prev_year in year_to_last_eq:
            prev_eq = year_to_last_eq[prev_year]
        else:
            prev_eq = initial  # 初年度

        expected_eq = prev_eq * (1 + yearly_ret / 100)
        if expected_eq > 0:
            diff_pct = abs((actual_eq - expected_eq) / expected_eq * 100)
        else:
            diff_pct = 0

        ok = diff_pct <= YEARLY_VS_WEEKLY_TOLERANCE_PCT
        checks.append({
            "year": year,
            "yearly_ret_pct": yearly_ret,
            "expected_eq": round(expected_eq, 2),
            "actual_eq": round(actual_eq, 2),
            "diff_pct": round(diff_pct, 2),
            "ok": ok,
        })
        if not ok:
            issues.append(f"{year}年: yearly={yearly_ret:.2f}%から期待 ${expected_eq:,.0f}, "
                          f"実測 ${actual_eq:,.0f} (乖離 {diff_pct:.2f}%)")

    n_ok = sum(1 for c in checks if c["ok"])
    status = "PASS" if n_ok == len(checks) else ("WARN" if n_ok >= len(checks) - 1 else "FAIL")
    return {"status": status, "checks": checks, "issues": issues}


def check_blocked_events(blocked_events: list) -> dict:
    """blocked_events の date/symbol 妥当性"""
    if not blocked_events:
        return {"status": "PASS", "total": 0, "issues": [], "note": "該当イベントなし"}

    issues = []
    invalid_dates = 0
    invalid_syms = 0
    for i, ev in enumerate(blocked_events):
        dt = parse_date(ev.get("date", ""))
        if dt is None:
            invalid_dates += 1
            if len(issues) < 5:
                issues.append(f"index {i}: date='{ev.get('date')}' 無効")
        elif not (EXPECTED_START - timedelta(days=30) <= dt <= EXPECTED_END + timedelta(days=30)):
            invalid_dates += 1
            if len(issues) < 5:
                issues.append(f"index {i}: date={dt.date()} が範囲外")
        sym = ev.get("sym", "")
        base = sym.split("/")[0] if "/" in sym else sym
        if base and base not in ACH_UNIVERSE:
            invalid_syms += 1
            if len(issues) < 5:
                issues.append(f"index {i}: sym='{sym}' が ACH_UNIVERSE に無い")

    status = "PASS"
    if invalid_dates > 0 or invalid_syms > 0:
        status = "FAIL"
    return {
        "status": status,
        "total": len(blocked_events),
        "invalid_dates": invalid_dates,
        "invalid_symbols": invalid_syms,
        "issues": issues,
    }


def verify_pattern(pattern: dict, initial: float) -> dict:
    weekly_check = check_equity_weekly_structure(pattern.get("equity_weekly", []))
    yearly_check = check_yearly_vs_weekly(pattern.get("yearly", {}),
                                           pattern.get("equity_weekly", []), initial)
    blocked_check = check_blocked_events(pattern.get("blocked_events_sample", []))

    statuses = [weekly_check["status"], yearly_check["status"], blocked_check["status"]]
    if "FAIL" in statuses:
        verdict = "FAIL"
    elif "WARN" in statuses:
        verdict = "WARN"
    else:
        verdict = "PASS"

    return {
        "pattern_name": pattern.get("pattern_name", f"max_daily_trades={pattern.get('max_daily_trades')}"),
        "max_daily_trades": pattern.get("max_daily_trades"),
        "verdict": verdict,
        "weekly_structure": weekly_check,
        "yearly_consistency": yearly_check,
        "blocked_events": blocked_check,
    }


def verify() -> dict:
    if not IN_JSON.exists():
        return {
            "script": "_verify_trade_execution_sim.py",
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "verdict": "ERROR",
            "error": f"Input not found: {IN_JSON}",
        }

    d = json.loads(IN_JSON.read_text())
    initial = d.get("initial", 10000)
    patterns = d.get("patterns", [])

    pattern_results = [verify_pattern(p, initial) for p in patterns]
    verdicts = [p["verdict"] for p in pattern_results]
    if "FAIL" in verdicts:
        overall = "FAIL"
    elif "WARN" in verdicts:
        overall = "WARN"
    else:
        overall = "PASS"

    return {
        "script": "_verify_trade_execution_sim.py",
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "input_file": str(IN_JSON),
        "initial_capital": initial,
        "expected_start": EXPECTED_START.isoformat(),
        "expected_end": EXPECTED_END.isoformat(),
        "thresholds": {
            "week_gap_tolerance_days": WEEK_GAP_TOLERANCE_DAYS,
            "max_weekly_jump_pct": MAX_WEEKLY_JUMP_PCT,
            "yearly_vs_weekly_tolerance_pct": YEARLY_VS_WEEKLY_TOLERANCE_PCT,
        },
        "patterns": pattern_results,
        "verdict": overall,
    }


def generate_html(result: dict) -> str:
    if result["verdict"] == "ERROR":
        return f"<html><body><h1>検証失敗</h1><p>{result.get('error')}</p></body></html>"

    overall = result["verdict"]
    verdict_color = {"PASS": "#48bb78", "WARN": "#ed8936", "FAIL": "#e53e3e"}[overall]
    verdict_label = {
        "PASS": "✅ バックテスト結果の構造は健全",
        "WARN": "⚠️ 軽度の逸脱あり（データ確認推奨）",
        "FAIL": "🔴 合成データ・欠損・範囲外あり（要調査）",
    }[overall]

    pattern_sections = ""
    for p in result["patterns"]:
        v = p["verdict"]
        vc = {"PASS": "#48bb78", "WARN": "#ed8936", "FAIL": "#e53e3e"}[v]

        ws = p["weekly_structure"]
        yc = p["yearly_consistency"]
        be = p["blocked_events"]

        weekly_issues = "".join(f"<li>{x}</li>" for x in ws.get("issues", [])[:5]) or "<li>なし</li>"
        yearly_rows = ""
        for c in yc.get("checks", []):
            ok_icon = "✅" if c["ok"] else "❌"
            yearly_rows += (f'<tr><td>{c["year"]}年</td>'
                            f'<td style="text-align:right;">{c["yearly_ret_pct"]:+.2f}%</td>'
                            f'<td style="text-align:right;">${c["expected_eq"]:,.0f}</td>'
                            f'<td style="text-align:right;">${c["actual_eq"]:,.0f}</td>'
                            f'<td style="text-align:right;">{c["diff_pct"]:.2f}%</td>'
                            f'<td style="text-align:center;">{ok_icon}</td></tr>')
        blocked_issues = "".join(f"<li>{x}</li>" for x in be.get("issues", [])[:5]) or "<li>なし</li>"

        pattern_sections += f'''
<div class="card">
<h2>{p["pattern_name"]}</h2>
<div style="padding:8px 14px; background:{vc}22; border-left:5px solid {vc};
            border-radius:8px; margin-bottom:14px;">
  <strong style="color:{vc};">判定: {v}</strong>
</div>

<h3>① 週次推移の構造 ({ws["status"]})</h3>
<div class="mini-stats">
  <div>期間: {ws.get("first_date","?")} 〜 {ws.get("last_date","?")}</div>
  <div>データ点: {ws.get("ok_count", 0)} / {ws.get("total", 0)}</div>
  <div>週刻み異常: {ws.get("gap_issues_count", 0)}件</div>
  <div>ジャンプ異常: {ws.get("jump_issues_count", 0)}件</div>
</div>
<ul class="issue-list">{weekly_issues}</ul>

<h3>② 年別集計 vs 週次整合 ({yc["status"]})</h3>
<table>
<thead><tr><th>年</th><th>yearly%</th><th>期待$</th><th>実測$</th><th>乖離%</th><th>OK</th></tr></thead>
<tbody>{yearly_rows}</tbody>
</table>

<h3>③ ブロックイベント妥当性 ({be["status"]})</h3>
<div class="mini-stats">
  <div>総数: {be.get("total", 0)}件</div>
  <div>無効日付: {be.get("invalid_dates", 0)}件</div>
  <div>無効銘柄: {be.get("invalid_symbols", 0)}件</div>
</div>
<ul class="issue-list">{blocked_issues}</ul>
</div>
'''

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>バックテスト整合性検証 | 気持ちマックス</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, "Hiragino Kaku Gothic ProN", sans-serif;
       background: linear-gradient(135deg,#667eea 0%,#764ba2 100%); min-height: 100vh;
       padding: 20px; color: #2c3e50; }}
.container {{ max-width: 1100px; margin: 0 auto; }}
h1 {{ color: white; text-align: center; font-size: 2rem; margin-bottom: 8px;
      text-shadow: 0 2px 10px rgba(0,0,0,0.2); }}
.subtitle {{ color: rgba(255,255,255,0.9); text-align: center; margin-bottom: 24px; }}
.verdict-badge {{ display: inline-block; padding: 12px 24px; border-radius: 999px;
                  font-size: 1.3rem; font-weight: 700; color: white;
                  background: {verdict_color}; margin-bottom: 20px; }}
.verdict-wrap {{ text-align: center; }}
.card {{ background: white; border-radius: 16px; padding: 24px; margin-bottom: 20px;
        box-shadow: 0 10px 40px rgba(0,0,0,0.1); }}
.card h2 {{ font-size: 1.3rem; margin-bottom: 16px; color: #4a5568;
           border-left: 5px solid #667eea; padding-left: 12px; }}
.card h3 {{ font-size: 1.05rem; margin: 18px 0 8px; color: #4a5568; }}
.mini-stats {{ display: flex; gap: 16px; flex-wrap: wrap;
              background: #f7fafc; padding: 10px 14px; border-radius: 8px; font-size: 0.9rem; }}
.mini-stats div {{ font-weight: 500; }}
.issue-list {{ padding: 10px 14px 10px 32px; background: #fff8e6; border-radius: 8px;
              margin-top: 8px; font-size: 0.88rem; line-height: 1.7; }}
.explain {{ background: #f0f4ff; border-left: 4px solid #667eea; padding: 14px;
          border-radius: 8px; margin: 14px 0; line-height: 1.8; font-size: 0.95rem; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 0.9rem; }}
th {{ background: #667eea; color: white; padding: 8px; text-align: left; }}
td {{ padding: 8px; border-bottom: 1px solid #e2e8f0; }}
.footer {{ text-align: center; color: rgba(255,255,255,0.8); padding: 20px;
          font-size: 0.85rem; }}
</style></head><body>
<div class="container">
<h1>🔬 バックテスト整合性検証レポート</h1>
<p class="subtitle">iter47 の構造的妥当性を7項目チェック</p>

<div class="verdict-wrap">
  <div class="verdict-badge">{verdict_label}</div>
</div>

<div class="card">
<h2>📖 何をチェックしているか</h2>
<div class="explain">
<strong>バックテスト結果の「構造的な妥当性」</strong>を検証するツールです。数字が正しいかではなく、
「その数字を生み出すデータの形」に合成データ・欠損・架空イベントが混入していないかを見ます。<br><br>

<strong>7つのチェック:</strong><br>
① 週次データの日付が実在のカレンダー日か<br>
② 週刻みに大きな欠損（±10日超）がないか<br>
③ 週次で±50%超のジャンプがないか（合成データの疑い検出）<br>
④ yearly 集計と週次末尾が整合するか（±5%以内）<br>
⑤ blocked_events_sample の日付が2020-2024の範囲内か<br>
⑥ blocked_events_sample の銘柄が ACH_UNIVERSE に存在するか<br>
⑦ 期間カバレッジが2020-01〜2024-12を網羅しているか<br><br>

<strong>判定基準:</strong><br>
・ 🟢 <strong>PASS</strong>: 全項目OK → データは本物のバックテスト結果<br>
・ 🟡 <strong>WARN</strong>: 軽度の逸脱 → 再取得推奨<br>
・ 🔴 <strong>FAIL</strong>: 合成疑い/欠損/範囲外 → <strong>当該戦略の結果を信用しない</strong>
</div>
</div>

{pattern_sections}

<div class="footer">生成日時: {result["ran_at"]}<br>🤖 気持ちマックス Phase 0 検証基盤</div>
</div></body></html>"""
    return html


def main():
    print("=" * 70)
    print("🔬 バックテスト整合性検証 (Phase 0-3)")
    print("=" * 70)

    result = verify()
    if result["verdict"] == "ERROR":
        print(f"❌ エラー: {result.get('error')}")
        sys.exit(1)

    print(f"\n判定: {result['verdict']}")
    for p in result["patterns"]:
        print(f"\n  📊 {p['pattern_name']} (判定: {p['verdict']})")
        ws = p["weekly_structure"]
        print(f"    ① 週次構造: {ws['status']} (期間 {ws.get('first_date')}〜{ws.get('last_date')}, "
              f"データ点 {ws.get('ok_count', 0)}/{ws.get('total', 0)}, "
              f"gap異常 {ws.get('gap_issues_count', 0)}, jump異常 {ws.get('jump_issues_count', 0)})")
        yc = p["yearly_consistency"]
        print(f"    ② 年次整合: {yc['status']}")
        be = p["blocked_events"]
        print(f"    ③ ブロック: {be['status']} (総{be.get('total', 0)}件, "
              f"無効日付{be.get('invalid_dates', 0)}, 無効銘柄{be.get('invalid_symbols', 0)})")

    OUT_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\n💾 JSON: {OUT_JSON}")

    html = generate_html(result)
    OUT_HTML.write_text(html)
    print(f"💾 HTML: {OUT_HTML}")

    if result["verdict"] == "FAIL":
        flag = PROJECT / "HALLUCINATION_DETECTED.flag"
        with open(flag, "a") as f:
            f.write(f"[{result['ran_at']}] trade_execution_sim FAIL\n")
        print(f"\n🚨 HALLUCINATION_DETECTED.flag 追記: {flag}")
        sys.exit(1)


if __name__ == "__main__":
    main()
