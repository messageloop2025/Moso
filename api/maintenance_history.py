"""服务器维护历史 API（按 IP 标识，支持增删改查）"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from database import get_db
from api.auth import get_current_user, _is_admin_role

router = APIRouter(prefix="/api/maintenance-history", tags=["维护历史"])


class HistoryCreate(BaseModel):
    host: str
    port: int = 22
    category: str
    content: str = ""
    file_path: Optional[str] = None
    details: Optional[str] = None


class HistoryUpdate(BaseModel):
    category: Optional[str] = None
    content: Optional[str] = None
    file_path: Optional[str] = None
    details: Optional[str] = None


def _check_page_size(page_size: int) -> int:
    if page_size not in (20, 50, 100):
        return 20
    return page_size


@router.get("")
async def list_history(
    host: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user=Depends(get_current_user),
):
    page_size = _check_page_size(page_size)
    offset = (page - 1) * page_size
    db = await get_db()
    base = "FROM server_maintenance_history m LEFT JOIN users u ON m.created_by = u.id WHERE 1=1"
    params = []
    if not _is_admin_role(user.get("role")):
        base += " AND m.created_by = ?"
        params.append(user["id"])
    if host:
        base += " AND m.host = ?"
        params.append(host)
    if category:
        base += " AND m.category = ?"
        params.append(category)
    count_rows = await db.execute_fetchall("SELECT COUNT(*) as n " + base, params)
    total = count_rows[0][0] if count_rows else 0
    query = (
        "SELECT m.id, m.host, m.port, m.category, m.content, m.file_path, m.details, m.created_at, m.created_by, u.username as created_by_name "
        + base + " ORDER BY m.created_at DESC LIMIT ? OFFSET ?"
    )
    params.extend([page_size, offset])
    rows = await db.execute_fetchall(query, params)
    items = []
    for r in rows:
        d = dict(r)
        d["created_by_name"] = d.get("created_by_name") or ""
        items.append(d)
    return {"success": True, "items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/{item_id}")
async def get_history_item(item_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM server_maintenance_history WHERE id = ?", (item_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="记录不存在")
    r = dict(rows[0])
    if not _is_admin_role(user.get("role")) and r.get("created_by") != user["id"]:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"success": True, "item": r}


@router.post("")
async def create_history(body: HistoryCreate, user=Depends(get_current_user)):
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO server_maintenance_history (host, port, category, content, file_path, details, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            body.host,
            body.port,
            body.category,
            body.content or "",
            body.file_path,
            body.details,
            user["id"],
        ),
    )
    await db.commit()
    return {"success": True, "id": cursor.lastrowid}


@router.put("/{item_id}")
async def update_history(item_id: int, body: HistoryUpdate, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, created_by FROM server_maintenance_history WHERE id = ?", (item_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="记录不存在")
    r = dict(rows[0])
    if not _is_admin_role(user.get("role")) and r.get("created_by") != user["id"]:
        raise HTTPException(status_code=403, detail="仅创建人或管理员可修改")
    updates, params = [], []
    for f in ("category", "content", "file_path", "details"):
        v = getattr(body, f, None)
        if v is not None:
            updates.append(f"{f} = ?")
            params.append(v)
    if updates:
        params.append(item_id)
        await db.execute(f"UPDATE server_maintenance_history SET {', '.join(updates)} WHERE id = ?", params)
        await db.commit()
    return {"success": True}


@router.delete("/{item_id}")
async def delete_history(item_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, created_by FROM server_maintenance_history WHERE id = ?", (item_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="记录不存在")
    r = dict(rows[0])
    if not _is_admin_role(user.get("role")) and r.get("created_by") != user["id"]:
        raise HTTPException(status_code=403, detail="仅创建人或管理员可删除")
    await db.execute("DELETE FROM server_maintenance_history WHERE id = ?", (item_id,))
    await db.commit()
    return {"success": True}
