"""组织级 Agent Skills（管理员维护，用户只读启用）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth import get_current_user, require_admin
from database import get_db

router = APIRouter(prefix="/api/org-skills", tags=["组织 Skills"])


class OrgSkillBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    display_name: str = ""
    description: str = ""
    content: str = ""
    enabled: bool = True
    slash_name: str = ""
    allowed_tools: str = ""


@router.get("")
async def list_org_skills(user=Depends(get_current_user)):
    db = await get_db()
    is_admin = (user.get("role") or "") in ("admin", "manager", "管理员")
    if is_admin:
        rows = await db.execute_fetchall(
            "SELECT * FROM org_skills ORDER BY name ASC"
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM org_skills WHERE enabled=1 ORDER BY name ASC"
        )
    items = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled"))
        d["slash_command"] = "/" + ((d.get("slash_name") or d.get("name") or "").lstrip("/"))
        items.append(d)
    return {"success": True, "skills": items}


@router.post("")
async def create_org_skill(body: OrgSkillBody, user=Depends(require_admin)):
    db = await get_db()
    name = (body.name or "").strip().lower()
    slash = (body.slash_name or name).strip().lstrip("/")
    try:
        await db.execute(
            """INSERT INTO org_skills
               (name, display_name, description, content, enabled, slash_name, allowed_tools, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                (body.display_name or name)[:120],
                (body.description or "")[:2000],
                body.content or "",
                1 if body.enabled else 0,
                slash,
                (body.allowed_tools or "")[:2000],
                user["id"],
            ),
        )
        await db.commit()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"创建失败: {e}") from e
    return {"success": True}


@router.put("/{skill_id}")
async def update_org_skill(skill_id: int, body: OrgSkillBody, user=Depends(require_admin)):
    db = await get_db()
    slash = (body.slash_name or body.name).strip().lstrip("/")
    cur = await db.execute(
        """UPDATE org_skills SET display_name=?, description=?, content=?, enabled=?,
           slash_name=?, allowed_tools=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (
            (body.display_name or body.name)[:120],
            (body.description or "")[:2000],
            body.content or "",
            1 if body.enabled else 0,
            slash,
            (body.allowed_tools or "")[:2000],
            skill_id,
        ),
    )
    await db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="不存在")
    return {"success": True}


@router.delete("/{skill_id}")
async def delete_org_skill(skill_id: int, user=Depends(require_admin)):
    db = await get_db()
    cur = await db.execute("DELETE FROM org_skills WHERE id=?", (skill_id,))
    await db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="不存在")
    return {"success": True}
