#!/usr/bin/env python3
"""Replace product display name EdgeOps -> 毛竹 (zh) / Moso (en). Skips technical identifiers."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Whole-word EdgeOps only; skip X-EdgeOps, EdgeOps/ paths, EdgeOpsRestClient, etc.
PAT = re.compile(r"(?<!X-)EdgeOps(?!/|[A-Za-z])")

EN_MARKERS = (
    "/locales/en/",
    "/intro/sources/en.full.html",
    "/intro/en/",
    "\\intro\\en\\",
    "/claw-ops/",
    "/claw-skills/",
)

TEXT_SUFFIXES = {
    ".md", ".json", ".html", ".py", ".ts", ".js", ".css", ".bat", ".sql", ".yml", ".yaml",
    ".txt", ".plugin.json", ".example.json",
}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}


def use_moso(path: Path) -> bool:
    s = str(path).replace("\\", "/")
    return any(m in s for m in EN_MARKERS)


def process_file(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    # ai_agent 主提示词中「勿称 EdgeOps」为禁令原文，须保留英文旧品牌名
    if path.name == "ai_agent.py" and "api" in path.parts:
        return False
    new = PAT.sub("Moso" if use_moso(path) else "毛竹", text)
    if new != text:
        # Windows .bat must use CRLF; avoid UTF-8 BOM on line 1 (breaks @echo off).
        if path.suffix.lower() == ".bat":
            text = new.replace("\r\n", "\n").replace("\r", "\n")
            try:
                text.encode("ascii")
                enc = "ascii"
            except UnicodeEncodeError:
                enc = "utf-8"  # no BOM; keep Chinese in rem/echo with chcp 65001 in script
            path.write_text(text, encoding=enc, newline="\r\n")
        else:
            # JSON/前端资源勿写 BOM（浏览器 JSON.parse 可能失败）
            path.write_text(new, encoding="utf-8", newline="\n")
        return True
    return False


def main() -> None:
    changed = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(p in path.parts for p in SKIP_DIRS):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in ("Dockerfile",):
            continue
        if path.name == "rebrand_to_moso.py":
            continue
        if process_file(path):
            changed.append(path.relative_to(ROOT))
    print(f"Updated {len(changed)} files")


if __name__ == "__main__":
    main()
