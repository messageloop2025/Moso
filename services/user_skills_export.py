"""每用户 Agent Skills 导出 / 导入（JSON 包，含 SKILL.md 与同目录附属文件）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from services.user_skills_registry import (
    create_user_skill,
    get_user_skill_raw_by_name,
    list_user_skills,
    list_skill_resource_files,
    normalize_skill_name,
    read_skill_content,
    skill_dir_path,
    update_user_skill,
    write_skill_content,
)


def export_user_skills_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


async def export_user_skills_bundle(
    db,
    user_id: int,
    user: dict,
    *,
    include_disabled: bool = True,
) -> dict[str, Any]:
    items = await list_user_skills(db, user_id, user)
    skills_out: dict[str, Any] = {}
    for row in items:
        if not include_disabled and not bool(row.get("enabled", 1)):
            continue
        slug = row.get("name") or ""
        if not slug:
            continue
        content = await read_skill_content(user, slug)
        files: dict[str, str] = {}
        for rel in list_skill_resource_files(user, slug):
            if rel == "SKILL.md":
                continue
            path = skill_dir_path(user, slug) / rel.replace("/", Path.sep)
            if path.is_file():
                try:
                    files[rel] = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
        skills_out[slug] = {
            "display_name": row.get("display_name") or slug,
            "description": row.get("description") or "",
            "enabled": bool(row.get("enabled", 1)),
            "chat_enabled": bool(row.get("chat_enabled", 1)),
            "chat_scope_web": bool(row.get("chat_scope_web", 1)),
            "chat_scope_host": bool(row.get("chat_scope_host", 1)),
            "chat_scope_integration": bool(row.get("chat_scope_integration", 0)),
            "content": content,
            "files": files,
        }
    return {
        "_edgeops": {"version": 1, "type": "agent-skills"},
        "skills": skills_out,
    }


def parse_skills_import_blob(raw: str | dict) -> dict[str, dict[str, Any]]:
    if isinstance(raw, str):
        data = json.loads(raw)
    else:
        data = raw
    if not isinstance(data, dict):
        raise ValueError("JSON 根须为对象")
    skills = data.get("skills")
    if not isinstance(skills, dict):
        raise ValueError("未找到 skills 对象")
    if not skills:
        raise ValueError("skills 为空")
    out: dict[str, dict[str, Any]] = {}
    for key, entry in skills.items():
        slug = normalize_skill_name(str(key))
        if not isinstance(entry, dict):
            raise ValueError(f"{slug}: 配置须为对象")
        out[slug] = entry
    return out


async def import_user_skills_bundle(
    db,
    user_id: int,
    user: dict,
    raw: str | dict,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    parsed = parse_skills_import_blob(raw)
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    for slug, entry in parsed.items():
        try:
            content = entry.get("content")
            if content is not None and not str(content).strip():
                content = None
            existing = await get_user_skill_raw_by_name(db, user_id, slug)
            if existing and not overwrite:
                skipped.append(slug)
                continue
            kwargs = {
                "display_name": str(entry.get("display_name") or slug)[:120],
                "description": str(entry.get("description") or "")[:2000],
                "enabled": bool(entry.get("enabled", True)),
                "chat_enabled": bool(entry.get("chat_enabled", True)),
                "chat_scope_web": bool(entry.get("chat_scope_web", True)),
                "chat_scope_host": bool(entry.get("chat_scope_host", True)),
                "chat_scope_integration": bool(entry.get("chat_scope_integration", False)),
            }
            if existing:
                await update_user_skill(
                    db,
                    user_id,
                    user,
                    int(existing["id"]),
                    content=content if content is not None else None,
                    **kwargs,
                )
                updated.append(slug)
            else:
                await create_user_skill(
                    db,
                    user_id,
                    user,
                    name=slug,
                    content=content,
                    **kwargs,
                )
                created.append(slug)
            files = entry.get("files") if isinstance(entry.get("files"), dict) else {}
            base = skill_dir_path(user, slug)
            for rel, text in files.items():
                rel_s = str(rel).strip().replace("\\", "/").lstrip("/")
                if not rel_s or ".." in rel_s.split("/") or rel_s == "SKILL.md":
                    continue
                dest = (base / rel_s).resolve()
                try:
                    dest.relative_to(base.resolve())
                except ValueError:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(str(text or ""), encoding="utf-8")
        except Exception as e:
            errors.append({"name": slug, "error": str(e)})
    return {
        "success": len(errors) == 0,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "count": len(created) + len(updated),
    }
