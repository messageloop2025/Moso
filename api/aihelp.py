"""AI 帮助文档 REST API（web/aihelp）：章节清单、按节读取、全文搜索。"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import config
from api.auth import get_current_user, _is_admin_role
from services.aihelp_paths import (
    aihelp_base_dir,
    list_aihelp_md_paths_sync,
    read_aihelp_text_async,
    resolve_aihelp_path,
)
from services.markdown_sections import (
    read_markdown_document,
    search_markdown_corpus,
    search_markdown_sections,
)

router = APIRouter(prefix="/api/aihelp", tags=["AI帮助"])


class AihelpWriteBody(BaseModel):
    content: str = ""


@router.get("/index")
async def get_aihelp_index(
    sections_only: bool = Query(False, description="true 时仅返回章节清单"),
    max_level: int = Query(6, ge=1, le=6),
    include_preamble: bool = Query(False),
    section_index: Optional[int] = Query(None),
    section_path: Optional[list[str]] = Query(None),
    heading: Optional[str] = Query(None),
    max_chars: Optional[int] = Query(None, ge=64, le=200_000),
    include_heading: bool = Query(True),
    include_children: bool = Query(True),
    case_insensitive: bool = Query(False),
    user=Depends(get_current_user),
):
    _ = user
    try:
        text = await read_aihelp_text_async("index.md")
    except FileNotFoundError:
        text = "# AI 帮助文档\n\n（暂无目录，请联系管理员维护 web/aihelp/index.md）\n"
    try:
        payload = read_markdown_document(
            text,
            sections_only=sections_only,
            max_level=max_level,
            include_preamble=include_preamble,
            section_index=section_index,
            section_path=section_path,
            heading=heading,
            case_insensitive=case_insensitive,
            max_chars=max_chars,
            include_heading=include_heading,
            include_children=include_children,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "path": "index.md", **payload}


@router.get("/files")
async def list_aihelp_files(user=Depends(get_current_user)):
    _ = user
    files = await asyncio.to_thread(list_aihelp_md_paths_sync)
    return {"success": True, "files": files}


@router.get("/file")
async def get_aihelp_file(
    path: str = Query(..., min_length=1, description="相对 aihelp 的路径，如 hosts.md"),
    sections_only: bool = Query(False),
    max_level: int = Query(6, ge=1, le=6),
    include_preamble: bool = Query(False),
    section_index: Optional[int] = Query(None),
    section_path: Optional[list[str]] = Query(None),
    heading: Optional[str] = Query(None),
    max_chars: Optional[int] = Query(None, ge=64, le=200_000),
    include_heading: bool = Query(True),
    include_children: bool = Query(True),
    case_insensitive: bool = Query(False),
    user=Depends(get_current_user),
):
    _ = user
    path = path.strip().replace("\\", "/").lstrip("/")
    try:
        text = await read_aihelp_text_async(path)
        payload = read_markdown_document(
            text,
            sections_only=sections_only,
            max_level=max_level,
            include_preamble=include_preamble,
            section_index=section_index,
            section_path=section_path,
            heading=heading,
            case_insensitive=case_insensitive,
            max_chars=max_chars,
            include_heading=include_heading,
            include_children=include_children,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail="文件不存在") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "path": path, **payload}


@router.get("/search")
async def search_aihelp(
    q: str = Query(..., min_length=1, description="搜索关键字"),
    path: Optional[str] = Query(None, description="限定单个文件；空则搜索全部 .md"),
    scope: str = Query("all", description="titles | content | all"),
    regex: bool = Query(False),
    case_insensitive: bool = Query(True),
    max_level: int = Query(6, ge=1, le=6),
    max_hits: int = Query(30, ge=1, le=100),
    snippet_chars: int = Query(200, ge=40, le=2000),
    user=Depends(get_current_user),
):
    _ = user
    q = q.strip()
    max_files = int(getattr(config, "MARKDOWN_SECTIONS_SEARCH_MAX_FILES", 100))

    async def _load_one(rel: str) -> tuple[str, str] | None:
        try:
            text = await read_aihelp_text_async(rel)
            return rel, text
        except (FileNotFoundError, ValueError):
            return None

    try:
        if path and path.strip():
            rel = path.strip().replace("\\", "/").lstrip("/")
            pair = await _load_one(rel)
            if not pair:
                raise HTTPException(status_code=404, detail="文件不存在")
            payload = search_markdown_sections(
                pair[1],
                q,
                scope=scope,
                regex=regex,
                case_insensitive=case_insensitive,
                max_level=max_level,
                max_hits=max_hits,
                snippet_chars=snippet_chars,
            )
            payload["path"] = rel
            return {"success": True, **payload}

        rels = await asyncio.to_thread(list_aihelp_md_paths_sync)
        rels = rels[:max_files]
        pairs: list[tuple[str, str]] = []
        for rel in rels:
            got = await _load_one(rel)
            if got:
                pairs.append(got)
        payload = search_markdown_corpus(
            pairs,
            q,
            scope=scope,
            regex=regex,
            case_insensitive=case_insensitive,
            max_level=max_level,
            max_hits=max_hits,
            snippet_chars=snippet_chars,
        )
        return {"success": True, **payload}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.put("/file")
async def put_aihelp_file(
    path: str = Query(..., min_length=1),
    body: AihelpWriteBody = ...,
    user=Depends(get_current_user),
):
    if not _is_admin_role(user.get("role")):
        raise HTTPException(status_code=403, detail="仅管理员可编辑帮助文档")
    path = path.strip().replace("\\", "/").lstrip("/")
    try:
        resolved = resolve_aihelp_path(path)
        await asyncio.to_thread(resolved.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(resolved.write_text, body.content or "", encoding="utf-8")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "path": path, "message": "已写入"}
