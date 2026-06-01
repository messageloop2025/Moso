"""Office / PDF 等富文档 → Markdown（Microsoft MarkItDown）。

供聊天附件 `read_chat_attachment` 在读取 document 类附件时调用；
转换结果缓存为同目录下的 ``<原文件名>.extracted.md`` 旁路文件，避免重复转换。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import config

logger = logging.getLogger("edgeops.markitdown")

CONVERTIBLE_EXTENSIONS = frozenset({
    ".pdf",
    ".docx",
    ".doc",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
    ".html",
    ".htm",
    ".epub",
})

CONVERTIBLE_MIME_PREFIXES = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
)


def is_markitdown_enabled() -> bool:
    return bool(getattr(config, "MARKITDOWN_ENABLED", True))


def is_markitdown_convertible(filename: str, mime: str = "") -> bool:
    """是否应通过 MarkItDown 转为 Markdown 供 AI 阅读。"""
    if not is_markitdown_enabled():
        return False
    name = (filename or "").strip()
    ext = Path(name).suffix.lower()
    if ext in CONVERTIBLE_EXTENSIONS:
        return True
    mime_l = (mime or "").lower()
    if mime_l == "application/pdf":
        return True
    return any(mime_l.startswith(p) for p in CONVERTIBLE_MIME_PREFIXES)


def markdown_sidecar_path(source: Path) -> Path:
    """旁路缓存：``report.pdf`` → ``report.pdf.extracted.md``。"""
    return source.with_name(source.name + ".extracted.md")


def _convert_sync(path: Path) -> dict:
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        return {
            "success": False,
            "error": (
                "服务器未安装 markitdown。请在部署环境执行: "
                "pip install 'markitdown[pdf,docx,pptx,xlsx]'"
            ),
            "import_error": str(exc),
        }
    try:
        converter = MarkItDown()
        result = converter.convert(str(path))
        text = ""
        title = None
        if result is not None:
            text = (getattr(result, "text_content", None) or "") or ""
            title = getattr(result, "title", None) or None
        return {"success": True, "markdown": text, "title": title}
    except Exception as exc:
        logger.exception("MarkItDown 转换失败: %s", path)
        return {"success": False, "error": str(exc)}


async def convert_file_to_markdown(path: Path, *, use_cache: bool = True) -> dict:
    """将本地文件转为 Markdown 文本；成功时含 ``markdown`` 字段。"""
    path = path.resolve()
    if not path.is_file():
        return {"success": False, "error": "文件不存在"}

    sidecar = markdown_sidecar_path(path)
    if use_cache and sidecar.is_file():
        try:
            if sidecar.stat().st_mtime >= path.stat().st_mtime:
                text = await asyncio.to_thread(sidecar.read_text, encoding="utf-8", errors="replace")
                return {
                    "success": True,
                    "markdown": text,
                    "source": "cache",
                    "sidecar": str(sidecar),
                }
        except OSError as exc:
            logger.warning("读取 Markdown 缓存失败: %s", exc)

    out = await asyncio.to_thread(_convert_sync, path)
    if not out.get("success"):
        return out

    text = out.get("markdown") or ""
    max_chars = int(getattr(config, "MARKITDOWN_MAX_OUTPUT_CHARS", 500_000))
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    if use_cache and text:
        try:
            await asyncio.to_thread(sidecar.write_text, text, encoding="utf-8")
        except OSError as exc:
            logger.warning("写入 Markdown 缓存失败: %s", exc)

    return {
        "success": True,
        "markdown": text,
        "truncated": truncated,
        "source": "markitdown",
        "title": out.get("title"),
    }


def remove_markdown_sidecar(source: Path) -> None:
    """删除附件时一并清理旁路缓存。"""
    try:
        sidecar = markdown_sidecar_path(source)
        if sidecar.is_file():
            sidecar.unlink()
    except OSError as exc:
        logger.warning("删除 Markdown 缓存失败: %s", exc)
