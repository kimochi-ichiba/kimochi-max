"""iter*.py 間の import 依存を AST で抽出し、DAG を出力.

Phase 3.4 (40 本書き換え) の順序決定に使う。葉ノード (他から import されない)
から書き換えれば、上位 iter のグローバル定数書換 (例: M.CORR_THRESHOLD = 0.80)
を破壊しない。

Usage:
    python scripts/analyze_iter_dependencies.py
    python scripts/analyze_iter_dependencies.py --out tests/fixtures/iter_dependency_graph.json
    python scripts/analyze_iter_dependencies.py --topological  # トポソート順を表示
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT / "tests" / "fixtures" / "iter_dependency_graph.json"


def _extract_iter_imports(source: str) -> list[str]:
    """ソースから `import _iter\\d+_*` の名前を抽出."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("_iter"):
                    out.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("_iter"):
                out.append(node.module)
    return out


def build_graph() -> dict[str, list[str]]:
    """{iter_name: [importしている iter 名のリスト]} を返す."""
    graph: dict[str, list[str]] = {}
    for path in sorted(PROJECT.glob("_iter*.py")):
        name = path.stem
        deps = _extract_iter_imports(path.read_text(encoding="utf-8", errors="ignore"))
        graph[name] = sorted(set(deps))
    return graph


def find_leaves(graph: dict[str, list[str]]) -> list[str]:
    """他から import されていない iter (葉ノード)."""
    imported_by_someone: set[str] = set()
    for deps in graph.values():
        imported_by_someone.update(deps)
    return sorted([name for name in graph if name not in imported_by_someone])


def topological_order(graph: dict[str, list[str]]) -> list[str]:
    """Kahn 法でトポソート。基盤 (in-degree=0) → 葉ノードの順.

    書き換えはこの逆順 (葉から先) が安全。
    """
    in_degree: dict[str, int] = defaultdict(int)
    for name, deps in graph.items():
        for d in deps:
            in_degree[name] += 1
    queue = [n for n in graph if in_degree[n] == 0]
    order: list[str] = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m, deps in graph.items():
            if n in deps:
                in_degree[m] -= 1
                if in_degree[m] == 0 and m not in order:
                    queue.append(m)
    return order


def stats(graph: dict[str, list[str]]) -> dict:
    n_files = len(graph)
    n_with_deps = sum(1 for v in graph.values() if v)
    n_imported = len({d for v in graph.values() for d in v})
    leaves = find_leaves(graph)
    most_depended = sorted(
        ((n, sum(1 for v in graph.values() if n in v)) for n in graph),
        key=lambda x: x[1], reverse=True,
    )[:5]
    return {
        "n_files": n_files,
        "n_with_iter_deps": n_with_deps,
        "n_imported_iters": n_imported,
        "n_leaves": len(leaves),
        "leaves": leaves[:10],
        "most_depended": most_depended,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="iter 依存 DAG 抽出")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--topological", action="store_true")
    parser.add_argument("--stats-only", action="store_true")
    args = parser.parse_args()

    graph = build_graph()
    s = stats(graph)
    print(f"n_iter_files: {s['n_files']}")
    print(f"with iter imports: {s['n_with_iter_deps']}")
    print(f"unique imported iters: {s['n_imported_iters']}")
    print(f"leaf nodes (safe to rewrite first): {s['n_leaves']}")
    print("most depended-upon (rewrite LAST):")
    for name, count in s["most_depended"]:
        if count > 0:
            print(f"  {name}: imported by {count} iter(s)")

    if args.topological:
        print("\nTopological order (base → leaves):")
        for n in topological_order(graph):
            deps = graph.get(n, [])
            print(f"  {n}  <- {deps}")

    if not args.stats_only:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "graph": graph,
            "stats": {k: v for k, v in s.items() if k != "leaves"},
            "leaves": find_leaves(graph),
            "topological_order": topological_order(graph),
        }
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        print(f"\nsaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
