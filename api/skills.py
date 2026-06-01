"""Skills API（AI 可调用的能力列表与执行入口）"""
from fastapi import APIRouter, Depends, HTTPException

from database import get_db
from api.auth import get_current_user

router = APIRouter(prefix="/api/skills", tags=["Skills"])


@router.get("")
async def list_skills(user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, code, name, description, parameters_schema, enabled FROM skills WHERE enabled = 1 ORDER BY id"
    )
    return {"success": True, "skills": [dict(r) for r in rows]}


@router.get("/{skill_id}")
async def get_skill(skill_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM skills WHERE id = ? AND enabled = 1", (skill_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return {"success": True, "skill": dict(rows[0])}
