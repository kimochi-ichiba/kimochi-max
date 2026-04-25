"""iter*.py を「アーカイブ / 書き換え / 残す」に分類.

判定基準 (heuristic):
- HTML 生成専用 (json.dump で json 結果なし、HTMLwrite_text のみ): archive
- バックテスト実行 (run_bt 関数 or summarize() 呼び出し): rewrite
- import されている (BASE iter): rewrite (基盤、最後)
- それ以外: review (人間判定)

Usage:
    python scripts/classify_iters.py
    python scripts/classify_iters.py --out tests/fixtures/iter_classification.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

DEFAULT_OUT = PROJECT / "tests" / "fixtures" / "iter_classification.json"


def _load_dep_graph() -> dict:
    p = PROJECT / "tests" / "fixtures" / "iter_dependency_graph.json"
    if not p.exists():
        # 依存解析がなければ空 graph
        return {"graph": {}, "leaves": [], "stats": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def _scan_iter(path: Path) -> dict:
    """iter ファイルから判定用のシグナルを抽出."""
    src = path.read_text(encoding="utf-8", errors="ignore")
    sig = {
        "lines": len(src.splitlines()),
        "has_json_dump": bool(re.search(r"json\.dumps?|json\.dump|to_json", src)),
        "has_html_write": bool(re.search(r"\.html\b.*write_text|html =|HTML", src)),
        "has_summarize_call": bool(re.search(r"\bsummarize\s*\(", src)),
        "has_run_bt_def": bool(re.search(r"def\s+run_bt", src)),
        "has_run_bt_call": bool(re.search(r"\brun_bt\w*\s*\(", src)),
        "has_select_top_call": bool(re.search(r"\bselect_top\b", src)),
        "has_main_function": bool(re.search(r"def\s+main\s*\(", src)),
        "writes_json_results": bool(re.search(
            r"results.*\.json|OUT_JSON|RESULTS_DIR.*json|write_text.*json",
            src,
        )),
    }
    return sig


def classify(name: str, sig: dict, is_base: bool) -> str:
    """1 ファイルを分類."""
    if is_base:
        return "rewrite_base"  # 基盤 iter (他から import される、最後に書換)

    # HTML レポート専用 (バックテスト実行なし)
    if sig["has_html_write"] and not (sig["has_run_bt_def"] or sig["has_run_bt_call"]):
        return "archive_html"

    # 名前で判定
    if "_html" in name:
        return "archive_html"

    # backtest 実行スクリプト
    if sig["has_summarize_call"] or sig["has_run_bt_def"] or sig["has_run_bt_call"]:
        return "rewrite_backtest"

    # JSON 出力ありで HTML なし → 結果出力スクリプト
    if sig["writes_json_results"] and sig["has_main_function"]:
        return "rewrite_results"

    return "review"


def main() -> int:
    parser = argparse.ArgumentParser(description="iter 分類スクリプト")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    graph_data = _load_dep_graph()
    base_set = set()
    for name, deps in graph_data.get("graph", {}).items():
        # 他から import されているなら BASE
        for dep in deps:
            base_set.add(dep)

    classification: dict[str, str] = {}
    counts: dict[str, int] = defaultdict(int)
    for path in sorted(PROJECT.glob("_iter*.py")):
        name = path.stem
        sig = _scan_iter(path)
        cat = classify(name, sig, name in base_set)
        classification[name] = cat
        counts[cat] += 1

    print(f"分類結果 ({sum(counts.values())} files):")
    for cat in ("rewrite_base", "rewrite_backtest", "rewrite_results",
                "archive_html", "review"):
        n = counts.get(cat, 0)
        print(f"  {cat}: {n}")

    print(f"\nArchive 候補 ({counts.get('archive_html', 0)}):")
    for name, cat in sorted(classification.items()):
        if cat == "archive_html":
            print(f"  {name}")

    print(f"\nRewrite 必須 (基盤 + バックテスト):")
    for name, cat in sorted(classification.items()):
        if cat in ("rewrite_base", "rewrite_backtest"):
            print(f"  [{cat}] {name}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "classification": classification,
        "counts": dict(counts),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nsaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
