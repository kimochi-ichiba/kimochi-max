"""iter スクリプトに record_run dual-write パッチを当てる半自動ヘルパ.

各 iter の `OUT_JSON.write_text(...)` の直後に record_run コールを挿入する。
完全自動化は難しいので、対象 iter ごとに「ヒント」を出力し、人間レビューを促す。

Usage:
    # ドライラン: パッチ提案を表示
    python scripts/iter_dualwrite_patcher.py --target _iter45_low_dd

    # 全 rewrite_backtest 候補のヒント一覧
    python scripts/iter_dualwrite_patcher.py --list-targets

    # パッチ適用 (バックアップ作成)
    python scripts/iter_dualwrite_patcher.py --target _iter45_low_dd --apply
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

CLASSIFICATION_PATH = PROJECT / "tests" / "fixtures" / "iter_classification.json"

PATCH_TEMPLATE = '''
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Phase 3.4: DB dual-write (auto-patched, 要レビュー)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    try:
        from db.repositories.runs_repo import normalize_metrics, record_run
        import ulid as _ulid
        # TODO: ここに各 iter のメトリクスから record_run コールを書く
        # 参考: _iter59_v22_verify.py の dual-write 部分 (例)
        # rid = record_run(
        #     strategy_id="{name}::main",
        #     run_type="single_backtest",
        #     params={{...}},
        #     universe=universe if "universe" in dir() else [],
        #     period=("{start}", "{end}"),
        #     metrics=normalize_metrics({{...}}),
        #     script_name="{name}.py",
        #     trial_group_id=str(_ulid.new()),
        # )
        print("\\n💾 DB dual-write: 手動補完が必要 (see TODO)")
    except (ImportError, Exception) as e:
        print(f"\\n⚠️ DB dual-write skipped: {{e}}")
'''


def _load_classification() -> dict:
    if not CLASSIFICATION_PATH.exists():
        return {"classification": {}, "counts": {}}
    return json.loads(CLASSIFICATION_PATH.read_text(encoding="utf-8"))


def _list_rewrite_targets() -> list[str]:
    data = _load_classification()
    return sorted([
        name for name, cat in data["classification"].items()
        if cat in ("rewrite_backtest", "rewrite_results")
    ])


def _has_dualwrite(src: str) -> bool:
    """既に dual-write が入っているか."""
    return "DB dual-write" in src or "record_run(" in src


def _find_insertion_point(src: str) -> int | None:
    """OUT_JSON.write_text(...) の直後の改行位置."""
    m = re.search(
        r"(OUT_JSON\.write_text|RESULTS_DIR.*\.write_text|"
        r"results_dir.*\.write_text|to_json\([^)]*\))",
        src,
    )
    if not m:
        return None
    # その後の改行 + (任意の print) の終わりを探す
    end = m.end()
    # OUT_JSON 行の改行を見つける
    rest = src[end:]
    eol = rest.find("\n")
    if eol == -1:
        return None
    insertion = end + eol + 1  # 改行直後
    # print(...) があればその直後まで進める
    while True:
        line_end = src.find("\n", insertion)
        if line_end == -1:
            break
        line = src[insertion:line_end]
        if line.strip().startswith("print("):
            insertion = line_end + 1
        else:
            break
    return insertion


def patch(name: str, *, apply: bool = False) -> dict:
    path = PROJECT / f"{name}.py"
    if not path.exists():
        return {"error": f"not found: {path}"}
    src = path.read_text(encoding="utf-8")

    if _has_dualwrite(src):
        return {"status": "already_patched", "path": str(path)}

    insert_at = _find_insertion_point(src)
    if insert_at is None:
        return {"status": "no_insertion_point",
                "hint": "OUT_JSON.write_text 等のパターンが見つからない、手動編集要"}

    patch_text = PATCH_TEMPLATE.format(
        name=name,
        start="2020-01-01",
        end="2024-12-31",
    )
    new_src = src[:insert_at] + patch_text + src[insert_at:]

    if apply:
        backup = path.with_suffix(".py.pre_patch")
        shutil.copy(path, backup)
        path.write_text(new_src, encoding="utf-8")
        return {"status": "patched", "backup": str(backup)}

    # dry: 抜粋を返す
    snippet_start = max(0, insert_at - 200)
    snippet_end = min(len(new_src), insert_at + len(patch_text) + 100)
    return {
        "status": "dry_run",
        "insert_at": insert_at,
        "preview": new_src[snippet_start:snippet_end],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", help="iter 名 (例: _iter45_low_dd)")
    parser.add_argument("--list-targets", action="store_true")
    parser.add_argument("--apply", action="store_true",
                        help="実際に書き換える (.pre_patch バックアップ作成)")
    args = parser.parse_args()

    if args.list_targets:
        targets = _list_rewrite_targets()
        print(f"rewrite 対象 ({len(targets)} 本):")
        for t in targets:
            print(f"  {t}")
        return 0

    if not args.target:
        parser.print_help()
        return 1

    res = patch(args.target, apply=args.apply)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
