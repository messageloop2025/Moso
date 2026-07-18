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
    bulk_assign_skills_to_group,
    bulk_set_group_skills_enabled,
    collect_description_warnings,
    create_user_skill,
    create_user_skill_group,
    default_skill_template_content,
    delete_user_skill,
    delete_user_skill_group,
    detect_slash_params_hint,
    get_user_skill,
    iter_skill_command_files,
    list_user_skill_groups_summary,
    list_user_skills,
    normalize_skill_name,
    read_skill_command_file,
    read_skill_content,
    read_skill_resource_file,
    require_user_skills_access,
    scan_user_skills_from_disk,
    update_user_skill,
    update_user_skill_group,
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
    group_id: int | None = None
    slash_name: str = ""
    hooks_enabled: bool = False
    pre_tool_use_matcher: str = ""
    pre_tool_use_decision: str = "ask"
    allowed_tools: str = ""
    hooks_json: str | None = None


class SkillUpdateBody(BaseModel):
    display_name: str | None = None
    description: str | None = None
    content: str | None = None
    enabled: bool | None = None
    chat_enabled: bool | None = None
    chat_scope_web: bool | None = None
    chat_scope_host: bool | None = None
    chat_scope_integration: bool | None = None
    group_id: int | None = None
    slash_name: str | None = None
    hooks_enabled: bool | None = None
    pre_tool_use_matcher: str | None = None
    pre_tool_use_decision: str | None = None
    allowed_tools: str | None = None
    hooks_json: str | None = None


class SkillGroupCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    sort_order: int = 0


class SkillGroupUpdateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class SkillGroupBulkEnabledBody(BaseModel):
    enabled: bool = True
    group_id: int | None = None


class SkillGroupBulkAssignBody(BaseModel):
    group_id: int | None = Field(..., description="目标分组 id；null 表示移入「未分组」")
    skill_ids: list[int] | None = Field(default=None, description="要移动的 Skill id 列表")
    all_ungrouped: bool = Field(
        default=False,
        description="为 true 时将当前用户全部未分组 Skill 移入 group_id（忽略 skill_ids）",
    )


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
async def list_my_skills(
    user=Depends(get_current_user),
    enabled: str | None = None,
    group_id: str | None = None,
):
    await _guard_skills(user)
    db = await get_db()
    en_filter: bool | None = None
    if enabled is not None and str(enabled).strip() != "":
        en_filter = str(enabled).strip().lower() in ("1", "true", "yes", "on")
    gid = "all"
    if group_id is not None and str(group_id).strip() != "":
        raw = str(group_id).strip().lower()
        if raw in ("none", "null", "ungrouped", "0"):
            gid = None
        elif raw == "all":
            gid = "all"
        else:
            gid = int(raw)
    items = await list_user_skills(
        db,
        int(user["id"]),
        user,
        enabled=en_filter,
        group_id=gid,
    )
    groups = await list_user_skill_groups_summary(db, int(user["id"]))
    return {"success": True, "skills": items, "groups": groups}


@router.get("/slash-commands")
async def list_slash_commands(user=Depends(get_current_user)):
    """输入框 `/` 菜单：用户 Skills + 组织 Skills + skills/*/commands 映射。"""
    await _guard_skills(user)
    db = await get_db()
    items: list[dict] = []
    rows = await list_user_skills(db, int(user["id"]), user, enabled=True)
    for s in rows:
        slash = (s.get("slash_name") or s.get("name") or "").strip().lstrip("/")
        if not slash:
            continue
        params_hint = ""
        try:
            content = await read_skill_content(user, s["name"])
            params_hint = detect_slash_params_hint(content)
        except Exception:
            params_hint = ""
        items.append(
            {
                "slash": "/" + slash,
                "name": s.get("name"),
                "display_name": s.get("display_name") or s.get("name"),
                "description": (s.get("description") or "")[:200],
                "source": "user",
                "params_hint": params_hint,
            }
        )
        # commands/ 目录：额外 slash 别名（文件名）
        try:
            for cmd in iter_skill_command_files(user, s["name"]):
                cmd_text = ""
                try:
                    cmd_text = read_skill_command_file(user, s["name"], cmd["alias"]) or ""
                except Exception:
                    cmd_text = ""
                items.append(
                    {
                        "slash": cmd["slash"],
                        "name": s.get("name"),
                        "display_name": cmd["alias"],
                        "description": f"{cmd['rel']} → {s.get('name')}",
                        "source": "commands",
                        "params_hint": detect_slash_params_hint(cmd_text),
                    }
                )
        except Exception:
            pass
    try:
        org_rows = await db.execute_fetchall(
            "SELECT name, display_name, description, slash_name FROM org_skills WHERE enabled=1 ORDER BY name"
        )
        for r in org_rows:
            slash = (r["slash_name"] or r["name"] or "").strip().lstrip("/")
            if not slash:
                continue
            items.append(
                {
                    "slash": "/" + slash,
                    "name": r["name"],
                    "display_name": r["display_name"] or r["name"],
                    "description": (r["description"] or "")[:200],
                    "source": "org",
                    "params_hint": "{{arg}}",
                }
            )
    except Exception:
        pass
    # 去重（同 slash 保留先出现的）
    seen: set[str] = set()
    out = []
    for it in items:
        k = it["slash"].lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return {"success": True, "commands": out}


@router.get("/groups")
async def list_my_skill_groups(user=Depends(get_current_user)):
    await _guard_skills(user)
    db = await get_db()
    groups = await list_user_skill_groups_summary(db, int(user["id"]))
    return {"success": True, "groups": groups}


@router.post("/groups")
async def create_my_skill_group(body: SkillGroupCreateBody, user=Depends(get_current_user)):
    await _guard_skills(user)
    db = await get_db()
    try:
        row = await create_user_skill_group(
            db,
            int(user["id"]),
            name=body.name,
            sort_order=body.sort_order,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "group": row}


@router.put("/groups/{group_id}")
async def update_my_skill_group(
    group_id: int,
    body: SkillGroupUpdateBody,
    user=Depends(get_current_user),
):
    await _guard_skills(user)
    db = await get_db()
    try:
        row = await update_user_skill_group(
            db, int(user["id"]), group_id, name=body.name
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="分组不存在") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "group": row}


@router.delete("/groups/{group_id}")
async def delete_my_skill_group(group_id: int, user=Depends(get_current_user)):
    await _guard_skills(user)
    db = await get_db()
    ok = await delete_user_skill_group(db, int(user["id"]), group_id)
    if not ok:
        raise HTTPException(status_code=404, detail="分组不存在")
    return {"success": True}


@router.post("/groups/bulk-enabled")
async def bulk_set_my_skill_group_enabled(
    body: SkillGroupBulkEnabledBody,
    user=Depends(get_current_user),
):
    await _guard_skills(user)
    db = await get_db()
    try:
        result = await bulk_set_group_skills_enabled(
            db,
            int(user["id"]),
            group_id=body.group_id,
            enabled=body.enabled,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="分组不存在") from None
    return {"success": True, **result}


@router.post("/groups/bulk-assign")
async def bulk_assign_my_skill_group(
    body: SkillGroupBulkAssignBody,
    user=Depends(get_current_user),
):
    await _guard_skills(user)
    db = await get_db()
    try:
        result = await bulk_assign_skills_to_group(
            db,
            int(user["id"]),
            group_id=body.group_id,
            skill_ids=body.skill_ids,
            all_ungrouped=body.all_ungrouped,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LookupError:
        raise HTTPException(status_code=404, detail="分组不存在") from None
    return {"success": True, **result}


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
            group_id=body.group_id,
            slash_name=body.slash_name,
            hooks_enabled=body.hooks_enabled,
            pre_tool_use_matcher=body.pre_tool_use_matcher,
            pre_tool_use_decision=body.pre_tool_use_decision,
            allowed_tools=body.allowed_tools,
            hooks_json=body.hooks_json,
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
    fields = body.model_dump(exclude_unset=True)
    try:
        row = await update_user_skill(
            db,
            int(user["id"]),
            user,
            skill_id,
            display_name=fields.get("display_name"),
            description=fields.get("description"),
            content=fields.get("content"),
            enabled=fields.get("enabled"),
            chat_enabled=fields.get("chat_enabled"),
            chat_scope_web=fields.get("chat_scope_web"),
            chat_scope_host=fields.get("chat_scope_host"),
            chat_scope_integration=fields.get("chat_scope_integration"),
            group_id=fields["group_id"] if "group_id" in fields else ...,
            slash_name=fields.get("slash_name"),
            hooks_enabled=fields.get("hooks_enabled"),
            pre_tool_use_matcher=fields.get("pre_tool_use_matcher"),
            pre_tool_use_decision=fields.get("pre_tool_use_decision"),
            allowed_tools=fields.get("allowed_tools"),
            hooks_json=fields["hooks_json"] if "hooks_json" in fields else ...,
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
