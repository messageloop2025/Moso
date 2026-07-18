"""Skills API（AI 可调用的能力列表与执行入口）"""
from fastapi import APIRouter, Depends, HTTPException

from database import get_db
from api.auth import get_current_user

router = APIRouter(prefix="/api/skills", tags=["Skills"])


@router.get("")
async def list_skills(user=Depends(get_current_user), include_deprecated: bool = False):
    """Prompt Skills 只读列表（与 TOOLS 摘要并存；deprecated 默认隐藏）。"""
    db = await get_db()
    if include_deprecated:
        rows = await db.execute_fetchall(
            "SELECT id, code, name, description, parameters_schema, enabled, "
            "COALESCE(deprecated, 0) AS deprecated FROM skills WHERE enabled = 1 ORDER BY deprecated ASC, id"
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT id, code, name, description, parameters_schema, enabled, "
            "COALESCE(deprecated, 0) AS deprecated FROM skills "
            "WHERE enabled = 1 AND COALESCE(deprecated, 0) = 0 ORDER BY id"
        )
    skills = []
    for r in rows:
        d = dict(r)
        d["deprecated"] = bool(d.get("deprecated"))
        d["source"] = "prompt_skills_table"
        skills.append(d)
    return {"success": True, "skills": skills, "note": "Prompt Skills 表为遗留视图；运行时以 TOOLS 为准"}


@router.get("/{skill_id}")
async def get_skill(skill_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM skills WHERE id = ? AND enabled = 1", (skill_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    skill = dict(rows[0])
    skill["deprecated"] = bool(skill.get("deprecated"))
    skill["source"] = "prompt_skills_table"
    return {"success": True, "skill": skill}
