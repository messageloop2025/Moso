#!/usr/bin/env python3
"""Remove UTF-8 BOM from text files under the repo (safe for .json / .bat / .md)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
TEXT_SUFFIXES = {
    ".json", ".md", ".html", ".py", ".ts", ".js", ".css", ".bat", ".sql", ".yml", ".yaml",
    ".txt",
}


def main() -> None:
    n = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(p in path.parts for p in SKIP_DIRS):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        b = path.read_bytes()
        if not b.startswith(b"\xef\xbb\xbf"):
            continue
        path.write_bytes(b[3:])
        n += 1
        print(path.relative_to(ROOT))
    print(f"Stripped BOM from {n} files")


if __name__ == "__main__":
    main()
