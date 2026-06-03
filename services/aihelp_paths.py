"""web/aihelp 路径解析与读取（REST 与 AI 工具共用）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import config


def aihelp_base_dir() -> Path:
    base = getattr(config, "AIHELP_DIR", None) or (Path(config.BASE_DIR) / "web" / "aihelp")
    return Path(base)


def resolve_aihelp_path(relative: str) -> Path:
    """将相对路径解析到 aihelp 目录内，禁止 .. 与绝对路径。"""
    base = aihelp_base_dir()
    relative = (relative or "").strip().replace("\\", "/").lstrip("/")
    if ".." in relative or relative.startswith("/"):
        raise ValueError("路径不允许")
    if not relative:
        return base
    resolved = (base / relative).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError("路径不允许") from exc
    return resolved


def read_aihelp_text_sync(relative: str) -> str:
    resolved = resolve_aihelp_path(relative)
    if not resolved.is_file():
        raise FileNotFoundError("文件不存在")
    return resolved.read_text(encoding="utf-8", errors="replace")


async def read_aihelp_text_async(relative: str) -> str:
    return await asyncio.to_thread(read_aihelp_text_sync, relative)


def list_aihelp_md_paths_sync() -> list[str]:
    base = aihelp_base_dir()
    if not base.is_dir():
        return []
    out: list[str] = []
    for p in sorted(base.rglob("*.md")):
        try:
            out.append(str(p.relative_to(base)).replace("\\", "/"))
        except ValueError:
            continue
    return out
