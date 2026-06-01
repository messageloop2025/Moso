"""最佳实践 API：记录推荐实现方法，各用户独立；支持 AI 与用户增删改查"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from database import get_db
from api.auth import get_current_user, _is_admin_role

router = APIRouter(prefix="/api/best-practices", tags=["最佳实践"])


def _can_access_bp(row: dict, user: dict) -> bool:
    return _is_admin_role(user.get("role")) or (row.get("created_by") == user["id"])


class BestPracticeCreate(BaseModel):
    title: str
    category: str = ""
    content: str
    source: str = "manual"


class BestPracticeUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    content: Optional[str] = None


def _check_page_size(page_size: int) -> int:
    if page_size not in (20, 50, 100):
        return 20
    return page_size


@router.get("")
async def list_best_practices(
    user=Depends(get_current_user),
    category: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    page_size = _check_page_size(page_size)
    offset = (page - 1) * page_size
    db = await get_db()
    base = "FROM best_practices WHERE 1=1"
    params = []
    if not _is_admin_role(user.get("role")):
        base += " AND created_by = ?"
        params.append(user["id"])
    if category and category.strip():
        base += " AND category = ?"
        params.append(category.strip())
    if keyword and keyword.strip():
        base += " AND (title LIKE ? OR content LIKE ? OR category LIKE ?)"
        q = "%" + keyword.strip() + "%"
        params.extend([q, q, q])
    count_rows = await db.execute_fetchall("SELECT COUNT(*) as n " + base, params)
    total = count_rows[0][0] if count_rows else 0
    query = "SELECT id, title, category, content, source, created_by, created_at, updated_at " + base + " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    params.extend([page_size, offset])
    rows = await db.execute_fetchall(query, params)
    return {"success": True, "items": [dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/categories")
async def list_categories(user=Depends(get_current_user)):
    db = await get_db()
    if _is_admin_role(user.get("role")):
        rows = await db.execute_fetchall(
            "SELECT DISTINCT category FROM best_practices WHERE category != '' ORDER BY category"
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT DISTINCT category FROM best_practices WHERE created_by = ? AND category != '' ORDER BY category",
            (user["id"],),
        )
    return {"success": True, "categories": [r["category"] for r in rows]}


@router.get("/{item_id}")
async def get_best_practice(item_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM best_practices WHERE id = ?", (item_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="记录不存在")
    if not _can_access_bp(dict(rows[0]), user):
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"success": True, "item": dict(rows[0])}


@router.post("")
async def create_best_practice(
    body: BestPracticeCreate,
    user=Depends(get_current_user),
):
    db = await get_db()
    title = (body.title or "").strip()
    content = (body.content or "").strip()
    if not title or not content:
        raise HTTPException(status_code=400, detail="标题和内容必填")
    await db.execute(
        """INSERT INTO best_practices (title, category, content, source, created_by, updated_at)
           VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (title, (body.category or "").strip()[:100], content, (body.source or "manual")[:50], user["id"]),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    return {"success": True, "id": (await cur.fetchone())[0]}


@router.put("/{item_id}")
async def update_best_practice(
    item_id: int,
    body: BestPracticeUpdate,
    user=Depends(get_current_user),
):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, created_by FROM best_practices WHERE id = ?", (item_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="记录不存在")
    if not _can_access_bp(dict(rows[0]), user):
        raise HTTPException(status_code=404, detail="记录不存在")
    updates, params = [], []
    if body.title is not None:
        updates.append("title = ?")
        params.append((body.title or "").strip())
    if body.category is not None:
        updates.append("category = ?")
        params.append((body.category or "").strip()[:100])
    if body.content is not None:
        updates.append("content = ?")
        params.append((body.content or "").strip())
    if updates:
        params.append(item_id)
        await db.execute(
            f"UPDATE best_practices SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            params,
        )
        await db.commit()
    return {"success": True}


@router.delete("/{item_id}")
async def delete_best_practice(item_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, created_by FROM best_practices WHERE id = ?", (item_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="记录不存在")
    if not _can_access_bp(dict(rows[0]), user):
        raise HTTPException(status_code=404, detail="记录不存在")
    await db.execute("DELETE FROM best_practices WHERE id = ?", (item_id,))
    await db.commit()
    return {"success": True}
