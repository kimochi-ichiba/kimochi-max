"""
_fix_macos_paths.py — macOS ハードコードパス一括置換ツール (Windows 移植用)

対象: /Users/sanosano/projects/kimochi-max → Path(__file__).resolve().parent

置換パターン:
  (A) sys.path.insert(0, "/Users/sanosano/projects/kimochi-max")
      → sys.path.insert(0, str(Path(__file__).resolve().parent))
  (B) Path("/Users/sanosano/projects/kimochi-max")
      → Path(__file__).resolve().parent
  (C) Path("/Users/sanosano/projects/kimochi-max/xxx/yyy")
      → Path(__file__).resolve().parent / "xxx" / "yyy"
  (D) "/Users/sanosano/projects/kimochi-max" という裸文字列
      → str(Path(__file__).resolve().parent)  (稀ケース)

from pathlib import Path が無いファイルには追加。
"""
import re
import sys
from pathlib import Path

MAC_ROOT_CANDIDATES = [
    "/Users/sanosano/projects/kimochi-max",
    "/Users/sanosano/projects/crypto-bot-pro",  # fork 元の旧 path
]
MAC_ROOT = MAC_ROOT_CANDIDATES[0]  # 互換用
TARGET_DIR = Path(__file__).resolve().parent

ROOT_ALT = "|".join(re.escape(r) for r in MAC_ROOT_CANDIDATES)
# Path("/Users/sanosano/projects/{kimochi-max|crypto-bot-pro}/a/b/c")
PAT_PATH_SUB = re.compile(
    r'Path\(\s*["\'](?:' + ROOT_ALT + r')/([^"\']+)["\']\s*\)'
)
# Path("/Users/sanosano/projects/{kimochi-max|crypto-bot-pro}")
PAT_PATH_ROOT = re.compile(
    r'Path\(\s*["\'](?:' + ROOT_ALT + r')["\']\s*\)'
)
# sys.path.insert(0, "/Users/sanosano/projects/{kimochi-max|crypto-bot-pro}")
PAT_SYSPATH = re.compile(
    r'sys\.path\.insert\(\s*0\s*,\s*["\'](?:' + ROOT_ALT + r')["\']\s*\)'
)
# 最後の手段: 裸の "/Users/sanosano/projects/..."
PAT_LITERAL = re.compile(r'["\'](?:' + ROOT_ALT + r')["\']')


def fix_file(p: Path) -> int:
    text = p.read_text(encoding="utf-8")
    original = text
    changes = 0

    # (C) Path(MAC_ROOT/a/b/c) → Path(__file__).parent / "a" / "b" / "c"
    def sub_path_sub(m):
        tail = m.group(1)
        parts = [x for x in tail.split("/") if x]
        joined = " / ".join(f'"{x}"' for x in parts)
        return f'(Path(__file__).resolve().parent / {joined})'
    text, n = PAT_PATH_SUB.subn(sub_path_sub, text)
    changes += n

    # (B) Path(MAC_ROOT) → Path(__file__).resolve().parent
    text, n = PAT_PATH_ROOT.subn("Path(__file__).resolve().parent", text)
    changes += n

    # (A) sys.path.insert(0, MAC_ROOT)
    text, n = PAT_SYSPATH.subn(
        'sys.path.insert(0, str(Path(__file__).resolve().parent))',
        text
    )
    changes += n

    # (D) 裸 literal "MAC_ROOT" → str(Path(__file__).resolve().parent)
    text, n = PAT_LITERAL.subn(
        'str(Path(__file__).resolve().parent)',
        text
    )
    changes += n

    if changes == 0:
        return 0

    # from pathlib import Path が無ければ追加
    if "from pathlib import Path" not in text and "import pathlib" not in text:
        lines = text.split("\n")
        insert_at = 0
        for i, ln in enumerate(lines):
            if ln.startswith("from __future__"):
                insert_at = i + 1
            elif ln.startswith("import ") or ln.startswith("from "):
                insert_at = i
                break
        lines.insert(insert_at, "from pathlib import Path")
        text = "\n".join(lines)

    if text != original:
        p.write_text(text, encoding="utf-8")
        return changes
    return 0


def main():
    files = list(TARGET_DIR.glob("*.py"))
    total_files = 0
    total_changes = 0
    for f in files:
        if f.name == "_fix_macos_paths.py":
            continue
        try:
            n = fix_file(f)
            if n > 0:
                print(f"  ✓ {f.name}: {n} 置換")
                total_files += 1
                total_changes += n
        except Exception as e:
            print(f"  ✗ {f.name}: {e}", file=sys.stderr)
    print(f"\n合計: {total_files} ファイル / {total_changes} 置換")


if __name__ == "__main__":
    main()
