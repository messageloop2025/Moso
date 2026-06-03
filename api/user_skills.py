"""当前用户 Agent Skills API（web/fs/<user>/skills/）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api.auth import get_current_user
from database import get_db
from services.user_skills_export import (
    export_user_skills_bundle,
    export_user_skills_json,
    import_user_skills_bundle,
)
from services.markdown_sections import read_markdown_document, search_markdown_sections
from services.user_skills_registry import (
    collect_description_warnings,
    create_user_skill,
    default_skill_template_content,
    delete_user_skill,
    get_user_skill,
    list_user_skills,
    normalize_skill_name,
    read_skill_resource_file,
    require_user_skills_access,
    scan_user_skills_from_disk,
    update_user_skill,
    user_skills_feature_enabled,
)

router = APIRouter(prefix="/api/user-skills", tags=["用户 Skills"])


class SkillCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field("", max_length=120)
    description: str = Field("", max_length=2000)
    content: str | None = None
    enabled: bool = True
    chat_enabled: bool = True
    chat_scope_web: bool = True
    chat_scope_host: bool = True
    chat_scope_integration: bool = False


class SkillUpdateBody(BaseModel):
    display_name: str | None = None
    description: str | None = None
    content: str | None = None
    enabled: bool | None = None
    chat_enabled: bool | None = None
    chat_scope_web: bool | None = None
    chat_scope_host: bool | None = None
    chat_scope_integration: bool | None = None


class SkillImportBody(BaseModel):
    data: str | dict
    overwrite: bool = False


async def _guard_skills(user) -> None:
    db = await get_db()
    try:
        await require_user_skills_access(db, user)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


@router.get("/status")
async def user_skills_status(user=Depends(get_current_user)):
    db = await get_db()
    enabled = await user_skills_feature_enabled(db, int(user["id"]))
    return {
        "success": True,
        "skills_enabled": enabled,
        "can_use": enabled,
        "skills_root": "skills/",
        "format": "web/fs/<username>/skills/<name>/SKILL.md",
        "progressive_disclosure": True,
    }


@router.get("/template")
async def user_skills_template(
    name: str = "my-skill",
    description: str = "",
    user=Depends(get_current_user),
):
    await _guard_skills(user)
    return {
        "success": True,
        "content": default_skill_template_content(name=name, description=description),
        "format": "Cursor Agent Skills (YAML frontmatter + Markdown)",
    }


@router.get("/export")
async def export_my_skills(
    include_disabled: bool = True,
    user=Depends(get_current_user),
):
    await _guard_skills(user)
    db = await get_db()
    data = await export_user_skills_bundle(
        db, int(user["id"]), user, include_disabled=include_disabled
    )
    text = export_user_skills_json(data)
    return Response(
        content=text,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="edgeops-skills.json"'},
    )


@router.post("/import")
async def import_my_skills(body: SkillImportBody, user=Depends(get_current_user)):
    await _guard_skills(user)
    db = await get_db()
    try:
        result = await import_user_skills_bundle(
            db,
            int(user["id"]),
            user,
            body.data,
            overwrite=body.overwrite,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    items = await list_user_skills(db, int(user["id"]), user)
    return {"success": True, **result, "skills": items}


@router.get("")
async def list_my_skills(user=Depends(get_current_user)):
    await _guard_skills(user)
    db = await get_db()
    items = await list_user_skills(db, int(user["id"]), user)
    return {"success": True, "skills": items}


@router.post("/scan")
async def scan_my_skills(user=Depends(get_current_user)):
    await _guard_skills(user)
    db = await get_db()
    result = await scan_user_skills_from_disk(db, int(user["id"]), user)
    items = await list_user_skills(db, int(user["id"]), user)
    return {"success": True, **result, "skills": items}


@router.post("")
async def create_my_skill(body: SkillCreateBody, user=Depends(get_current_user)):
    await _guard_skills(user)
    db = await get_db()
    if (
        not (body.description or "").strip()
        and not (body.display_name or "").strip()
        and body.content is not None
        and not str(body.content).strip()
    ):
        raise HTTPException(status_code=400, detail="请提供 description 或 SKILL.md 正文")
    try:
        row = await create_user_skill(
            db,
            int(user["id"]),
            user,
            name=body.name,
            display_name=body.display_name,
            description=body.description,
            content=body.content,
            enabled=body.enabled,
            chat_enabled=body.chat_enabled,
            chat_scope_web=body.chat_scope_web,
            chat_scope_host=body.chat_scope_host,
            chat_scope_integration=body.chat_scope_integration,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    warnings = collect_description_warnings(row.get("name") or body.name, row.get("description") or "")
    return {"success": True, "skill": row, "warnings": warnings}


@router.get("/by-name/{skill_name}/markdown")
async def read_skill_markdown(
    skill_name: str,
    path: str = "SKILL.md",
    sections_only: bool = False,
    max_level: int = 6,
    section_index: int | None = None,
    section_path: list[str] | None = None,
    heading: str | None = None,
    max_chars: int | None = None,
    include_heading: bool = True,
    include_children: bool = True,
    q: str | None = None,
    scope: str = "all",
    regex: bool = False,
    case_insensitive: bool = True,
    max_hits: int = 30,
    user=Depends(get_current_user),
):
    """读取 Skill 内 Markdown：章节清单 / 按节读取 / 章节搜索（q 非空时搜索）。"""
    await _guard_skills(user)
    rel = (path or "SKILL.md").strip().replace("\\", "/")
    slug = normalize_skill_name(skill_name)
    try:
        text = read_skill_resource_file(user, slug, rel)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        if q and q.strip():
            payload = search_markdown_sections(
                text,
                q.strip(),
                scope=scope,
                regex=regex,
                case_insensitive=case_insensitive,
                max_level=max_level,
                max_hits=max_hits,
            )
            payload["mode"] = "search"
        else:
            payload = read_markdown_document(
                text,
                sections_only=sections_only,
                max_level=max_level,
                section_index=section_index,
                section_path=section_path,
                heading=heading,
                max_chars=max_chars,
                include_heading=include_heading,
                include_children=include_children,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "name": slug, "path": rel, **payload}


@router.get("/{skill_id}")
async def get_my_skill(skill_id: int, user=Depends(get_current_user)):
    await _guard_skills(user)
    db = await get_db()
    row = await get_user_skill(db, int(user["id"]), skill_id, user)
    if not row:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    warnings = collect_description_warnings(row.get("name") or "", row.get("description") or "")
    return {"success": True, "skill": row, "warnings": warnings}


@router.put("/{skill_id}")
async def update_my_skill(skill_id: int, body: SkillUpdateBody, user=Depends(get_current_user)):
    await _guard_skills(user)
    db = await get_db()
    try:
        row = await update_user_skill(
            db,
            int(user["id"]),
            user,
            skill_id,
            display_name=body.display_name,
            description=body.description,
            content=body.content,
            enabled=body.enabled,
            chat_enabled=body.chat_enabled,
            chat_scope_web=body.chat_scope_web,
            chat_scope_host=body.chat_scope_host,
            chat_scope_integration=body.chat_scope_integration,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Skill 不存在") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    warnings = collect_description_warnings(row.get("name") or "", row.get("description") or "")
    return {"success": True, "skill": row, "warnings": warnings}


@router.delete("/{skill_id}")
async def delete_my_skill(skill_id: int, remove_files: bool = False, user=Depends(get_current_user)):
    await _guard_skills(user)
    db = await get_db()
    ok = await delete_user_skill(
        db, int(user["id"]), user, skill_id, remove_files=remove_files
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return {"success": True}
