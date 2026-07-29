"""当前用户 Agent Skills API（web/fs/<user>/skills/）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api.auth import get_current_user
from database import get_db
from services.user_skills_export import (
    export_user_skills_bundle,
    export_user_skills_json,
    export_user_skills_tgz,
    import_user_skills_bundle,
    import_user_skills_tgz,
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
    extract_slash_arg_meta,
    get_user_skill,
    iter_skill_command_files,
    list_user_skill_groups_summary,
    list_user_skills,
    normalize_skill_name,
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


@router.get("/export-tgz")
async def export_my_skills_tgz(
    include_disabled: bool = True,
    ids: str = "",
    user=Depends(get_current_user),
):
    """导出个人 Skills 为 gzip tar（.tgz），含目录树与二进制附属文件。"""
    await _guard_skills(user)
    db = await get_db()
    skill_ids = None
    raw_ids = (ids or "").strip()
    if raw_ids:
        try:
            skill_ids = [int(x) for x in raw_ids.split(",") if str(x).strip()]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="ids 须为逗号分隔的整数") from exc
    try:
        blob = await export_user_skills_tgz(
            db,
            int(user["id"]),
            user,
            skill_ids=skill_ids,
            include_disabled=include_disabled,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    uname = (user.get("username") or "user").strip() or "user"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in uname)[:64] or "user"
    return Response(
        content=blob,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="edgeops-skills-{safe}.tgz"',
        },
    )


@router.post("/import-tgz")
async def import_my_skills_tgz(
    file: UploadFile = File(...),
    overwrite: bool = Form(False),
    user=Depends(get_current_user),
):
    """上传 .tgz / .tar.gz，自动解压并导入个人 Skills。"""
    await _guard_skills(user)
    filename = (file.filename or "").lower()
    if filename and not (
        filename.endswith(".tgz") or filename.endswith(".tar.gz")
    ):
        raise HTTPException(status_code=400, detail="请上传 .tgz 或 .tar.gz 文件")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    db = await get_db()
    try:
        result = await import_user_skills_tgz(
            db,
            int(user["id"]),
            user,
            raw,
            overwrite=bool(overwrite),
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
async def list_slash_commands(
    scope: str = "web",
    user=Depends(get_current_user),
):
    """输入框 `/` 菜单：已启用且 chat_enabled 的用户 Skills + commands/ + 组织 Skills。

    query ``scope``：``web`` | ``host`` | ``integration`` | ``all``，按 Skill 场景开关过滤，与 resolve 对齐。
    """
    await _guard_skills(user)
    from pathlib import Path

    from services.user_skills_registry import skill_md_path

    scope_val = (scope or "web").strip().lower() or "web"
    if scope_val not in ("web", "host", "integration", "all", "default", "local"):
        scope_val = "web"
    if scope_val in ("default", "local"):
        scope_val = "web"

    db = await get_db()
    items: list[dict] = []
    rows = await list_user_skills(db, int(user["id"]), user, enabled=True)
    for s in rows:
        if s.get("chat_enabled") is False:
            continue
        if scope_val == "host" and not s.get("chat_scope_host", True):
            continue
        if scope_val == "integration" and not s.get("chat_scope_integration", False):
            continue
        if scope_val == "web" and not s.get("chat_scope_web", True):
            continue
        slash = (s.get("slash_name") or s.get("name") or "").strip().lstrip("/")
        if not slash:
            continue
        skill_text = ""
        try:
            p = skill_md_path(user, s["name"])
            if p.is_file():
                skill_text = p.read_text(encoding="utf-8")[:16000]
        except Exception:
            skill_text = ""
        meta = extract_slash_arg_meta(skill_text, slash)
        # commands/ 子命令名也作为父斜杠的参数建议
        cmd_aliases: list[str] = []
        try:
            for cmd in iter_skill_command_files(user, s["name"]):
                cmd_aliases.append(cmd["alias"])
        except Exception:
            cmd_aliases = []
        parent_suggestions = list(meta.get("arg_suggestions") or [])
        for a in cmd_aliases:
            if a and a not in parent_suggestions:
                parent_suggestions.append(a)
        items.append(
            {
                "slash": "/" + slash,
                "name": s.get("name"),
                "display_name": s.get("display_name") or s.get("name"),
                "description": (s.get("description") or "")[:200],
                "source": "user",
                "params_hint": meta.get("params_hint") or "",
                "arg_suggestions": parent_suggestions[:16],
                "usage_examples": meta.get("usage_examples") or [],
            }
        )
        try:
            for cmd in iter_skill_command_files(user, s["name"]):
                cmd_text = ""
                try:
                    cmd_path = Path(cmd["path"])
                    if cmd_path.is_file():
                        cmd_text = cmd_path.read_text(encoding="utf-8")[:16000]
                except Exception:
                    cmd_text = ""
                cmeta = extract_slash_arg_meta(cmd_text, cmd["alias"])
                items.append(
                    {
                        "slash": cmd["slash"],
                        "name": s.get("name"),
                        "display_name": cmd["alias"],
                        "description": f"{cmd['rel']} → {s.get('name')}",
                        "source": "commands",
                        "params_hint": cmeta.get("params_hint") or "",
                        "arg_suggestions": cmeta.get("arg_suggestions") or [],
                        "usage_examples": cmeta.get("usage_examples") or [],
                    }
                )
        except Exception:
            pass
    try:
        org_rows = await db.execute_fetchall(
            "SELECT name, display_name, description, slash_name, content "
            "FROM org_skills WHERE enabled=1 ORDER BY name"
        )
        for r in org_rows:
            slash = (r["slash_name"] or r["name"] or "").strip().lstrip("/")
            if not slash:
                continue
            ometa = extract_slash_arg_meta((r["content"] or "")[:16000], slash)
            items.append(
                {
                    "slash": "/" + slash,
                    "name": r["name"],
                    "display_name": r["display_name"] or r["name"],
                    "description": (r["description"] or "")[:200],
                    "source": "org",
                    "params_hint": ometa.get("params_hint") or "",
                    "arg_suggestions": ometa.get("arg_suggestions") or [],
                    "usage_examples": ometa.get("usage_examples") or [],
                }
            )
    except Exception:
        pass
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
# === Event Rules API ===

@router.get("/event-rules")
async def list_event_rules(
    event_name: str | None = None,
    enabled: bool | None = None,
    user=Depends(get_current_user),
):
    """列出当前用户的 Event 规则。"""
    await _guard_skills(user)
    db = await get_db()
    sql = "SELECT * FROM event_rules WHERE user_id = ?"
    params: list = [int(user["id"])]
    if event_name:
        sql += " AND event_name = ?"
        params.append(event_name)
    if enabled is not None:
        sql += " AND enabled = ?"
        params.append(1 if enabled else 0)
    sql += " ORDER BY priority DESC, id ASC"
    rows = [dict(r) for r in (await db.execute_fetchall(sql, tuple(params)) or [])]
    return {"success": True, "rules": rows, "count": len(rows)}


@router.post("/event-rules")
async def create_or_update_event_rule(
    body: dict,
    user=Depends(get_current_user),
):
    """新增/更新 Event 规则。"""
    await _guard_skills(user)
    db = await get_db()
    uid = int(user["id"])
    rid = body.get("id")
    delete = bool(body.get("delete", False))
    if delete and rid:
        await db.execute("DELETE FROM event_rules WHERE id = ? AND user_id = ?", (int(rid), uid))
        await db.commit()
        return {"success": True, "deleted": True}
    event_name = str(body.get("event_name") or "").strip()
    if not event_name:
        raise HTTPException(status_code=400, detail="event_name 不能为空")
    matcher = str(body.get("matcher") or "*").strip()
    decision = str(body.get("decision") or "allow").strip().lower()
    if decision not in ("allow", "deny", "ask"):
        decision = "allow"
    reason = str(body.get("reason") or "")[:500]
    priority = int(body.get("priority", 0))
    ac = body.get("action_config") or {}
    action_config = json.dumps(ac, ensure_ascii=False) if isinstance(ac, dict) else str(ac)
    skill_id = body.get("skill_id")
    enabled = 1 if body.get("enabled", True) is not False else 0
    if rid:
        existing = await db.execute_fetchall("SELECT id FROM event_rules WHERE id = ? AND user_id = ?", (int(rid), uid))
        if not existing:
            raise HTTPException(status_code=404, detail="规则不存在")
        await db.execute(
            "UPDATE event_rules SET event_name=?, matcher=?, decision=?, reason=?, priority=?, action_config=?, enabled=?, skill_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
            (event_name, matcher, decision, reason, priority, action_config, enabled, skill_id, int(rid), uid),
        )
    else:
        existing = await db.execute_fetchall(
            "SELECT id FROM event_rules WHERE user_id=? AND event_name=? AND matcher=?", (uid, event_name, matcher)
        )
        if existing:
            await db.execute(
                "UPDATE event_rules SET decision=?, reason=?, priority=?, action_config=?, enabled=?, skill_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (decision, reason, priority, action_config, enabled, skill_id, existing[0]["id"]),
            )
            rid = existing[0]["id"]
        else:
            cur = await db.execute(
                "INSERT INTO event_rules (user_id, skill_id, event_name, matcher, decision, reason, priority, action_config, enabled) VALUES (?,?,?,?,?,?,?,?,?)",
                (uid, skill_id, event_name, matcher, decision, reason, priority, action_config, enabled),
            )
            rid = cur.lastrowid
    await db.commit()
    return {"success": True, "id": rid}


@router.put("/event-rules/{rule_id}/toggle")
async def toggle_event_rule(
    rule_id: int,
    body: dict,
    user=Depends(get_current_user),
):
    """启用/禁用 Event 规则。"""
    await _guard_skills(user)
    db = await get_db()
    uid = int(user["id"])
    enabled = 1 if body.get("enabled", True) is not False else 0
    await db.execute("UPDATE event_rules SET enabled=? WHERE id=? AND user_id=?", (enabled, rule_id, uid))
    await db.commit()
    return {"success": True, "id": rule_id, "enabled": bool(enabled)}


@router.delete("/event-rules/{rule_id}")
async def delete_event_rule(rule_id: int, user=Depends(get_current_user)):
    """删除 Event 规则。"""
    await _guard_skills(user)
    db = await get_db()
    uid = int(user["id"])
    await db.execute("DELETE FROM event_rules WHERE id = ? AND user_id = ?", (rule_id, uid))
    await db.commit()
    return {"success": True, "deleted": True}


@router.get("/event-rules/export")
async def export_event_rules(user=Depends(get_current_user)):
    """导出 Event 规则为 JSON。"""
    await _guard_skills(user)
    db = await get_db()
    uid = int(user["id"])
    rows = [dict(r) for r in (await db.execute_fetchall(
        "SELECT event_name, matcher, decision, reason, priority, action_config, enabled FROM event_rules WHERE user_id = ? ORDER BY priority DESC, id ASC",
        (uid,),
    ) or [])]
    return {"success": True, "rules": rows, "count": len(rows)}


@router.post("/event-rules/import")
async def import_event_rules(body: dict, user=Depends(get_current_user)):
    """从 JSON 导入 Event 规则。"""
    await _guard_skills(user)
    db = await get_db()
    uid = int(user["id"])
    raw = body.get("config_json", "")
    try:
        rules = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="config_json 格式不正确")
    if not isinstance(rules, list):
        raise HTTPException(status_code=400, detail="config_json 必须是数组")
    overwrite = bool(body.get("overwrite", False))
    imported = 0
    skipped = 0
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        ev = str(rule.get("event_name") or "").strip()
        if not ev:
            continue
        matcher = str(rule.get("matcher") or "*").strip()
        decision = str(rule.get("decision") or "allow").strip().lower()
        if decision not in ("allow", "deny", "ask"):
            decision = "allow"
        existing = await db.execute_fetchall(
            "SELECT id FROM event_rules WHERE user_id=? AND event_name=? AND matcher=?", (uid, ev, matcher)
        )
        if existing:
            if overwrite:
                await db.execute(
                    "UPDATE event_rules SET decision=?, reason=?, priority=?, action_config=?, enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (decision, str(rule.get("reason", ""))[:500], int(rule.get("priority", 0)),
                     str(rule.get("action_config", "{}")), 1 if rule.get("enabled", True) is not False else 0,
                     existing[0]["id"]),
                )
                imported += 1
            else:
                skipped += 1
        else:
            await db.execute(
                "INSERT INTO event_rules (user_id, event_name, matcher, decision, reason, priority, action_config, enabled) VALUES (?,?,?,?,?,?,?,?)",
                (uid, ev, matcher, decision, str(rule.get("reason", ""))[:500],
                 int(rule.get("priority", 0)), str(rule.get("action_config", "{}")),
                 1 if rule.get("enabled", True) is not False else 0),
            )
            imported += 1
    await db.commit()
    return {"success": True, "imported": imported, "skipped": skipped}


# === Middleware Config API ===

@router.get("/middleware-config")
async def list_middleware_config(user=Depends(get_current_user)):
    """列出当前用户的中间件配置。"""
    await _guard_skills(user)
    db = await get_db()
    uid = int(user["id"])
    rows = [dict(r) for r in (await db.execute_fetchall(
        "SELECT * FROM user_middleware_config WHERE user_id = ? ORDER BY middleware_name", (uid,)
    ) or [])]
    return {"success": True, "configs": rows, "count": len(rows)}


@router.post("/middleware-config")
async def configure_middleware(body: dict, user=Depends(get_current_user)):
    """配置中间件。"""
    await _guard_skills(user)
    db = await get_db()
    uid = int(user["id"])
    mw_name = str(body.get("middleware_name") or "").strip()
    if not mw_name:
        raise HTTPException(status_code=400, detail="middleware_name 不能为空")
    enabled = 1 if body.get("enabled", True) is not False else 0
    conf = body.get("config_json") or {}
    config_json = json.dumps(conf, ensure_ascii=False) if isinstance(conf, dict) else str(conf)
    await db.execute(
        "INSERT INTO user_middleware_config (user_id, middleware_name, enabled, config_json) VALUES (?,?,?,?) ON CONFLICT(user_id, middleware_name) DO UPDATE SET enabled=?, config_json=?",
        (uid, mw_name, enabled, config_json, enabled, config_json),
    )
    await db.commit()
    return {"success": True, "middleware_name": mw_name, "enabled": bool(enabled)}
    return {"success": True, "middleware_name": mw_name, "enabled": bool(enabled)}


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


