"""安全审计：筛选 chat_mode:* / Hook 拒绝 / 模式切换等 operation_logs。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.auth import get_current_user, require_admin
from database import get_db

router = APIRouter(prefix="/api/security-audit", tags=["安全审计"])


@router.get("")
async def list_security_audit(
    page: int = 1,
    page_size: int = 50,
    user_id: int | None = None,
    session_id: int | None = None,
    q: str | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    user=Depends(require_admin),
):
    page_size = 20 if page_size not in (20, 50, 100) else page_size
    page = max(1, page)
    offset = (page - 1) * page_size
    db = await get_db()
    conditions = [
        "("
        "l.operation LIKE 'chat_mode:%' OR "
        "l.operation LIKE 'hook:%' OR "
        "l.operation = 'chat_mode_change' OR "
        "l.operation LIKE '%preToolUse%' OR "
        "l.operation LIKE '%allowed_tools%'"
        ")"
    ]
    params: list = []
    if user_id is not None:
        conditions.append("l.user_id = ?")
        params.append(user_id)
    if session_id is not None:
        conditions.append("(l.params LIKE ? OR l.details LIKE ?)")
        needle = f"%session_id%{session_id}%"
        params.extend([needle, f"%\"session_id\": {session_id}%"])
    if q and q.strip():
        conditions.append("(l.operation LIKE ? OR l.params LIKE ? OR COALESCE(l.details,'') LIKE ?)")
        like = "%" + q.strip() + "%"
        params.extend([like, like, like])
    if from_time and from_time.strip():
        conditions.append("l.created_at >= ?")
        params.append(from_time.strip())
    if to_time and to_time.strip():
        conditions.append("l.created_at <= ?")
        params.append(to_time.strip())
    where = " WHERE " + " AND ".join(conditions)
    count_rows = await db.execute_fetchall(
        "SELECT COUNT(*) as n FROM operation_logs l" + where, params
    )
    total = count_rows[0][0] if count_rows else 0
    query = (
        "SELECT l.*, u.username FROM operation_logs l "
        "LEFT JOIN users u ON u.id = l.user_id"
        + where
        + " ORDER BY l.created_at DESC LIMIT ? OFFSET ?"
    )
    rows = await db.execute_fetchall(query, params + [page_size, offset])
    return {
        "success": True,
        "logs": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
