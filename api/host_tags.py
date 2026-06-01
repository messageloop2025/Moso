"""主机标签 API（每用户私有标签；同一主机可按用户维度打不同标签）"""
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from database import get_db
from api.auth import get_current_user
from api.hosts import _can_access_host

router = APIRouter(prefix="/api/host-tags", tags=["主机标签"])


_HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


class HostTagCreate(BaseModel):
    name: str
    color: Optional[str] = ""


class HostTagUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None


class HostTagAssignRequest(BaseModel):
    tag_ids: list[int] = []


def _normalize_tag_name(name: str) -> str:
    return (name or "").strip()


def _normalize_tag_color(color: Optional[str]) -> str:
    value = (color or "").strip()
    if not value:
        return ""
    if not _HEX_COLOR_RE.match(value):
        raise HTTPException(status_code=400, detail="标签颜色格式无效，请使用 #RRGGBB")
    if not value.startswith("#"):
        value = "#" + value
    return value.upper()


async def _ensure_tag_owned(db, *, tag_id: int, user_id: int) -> dict:
    rows = await db.execute_fetchall(
        "SELECT id, name, color, created_by, created_at, updated_at FROM host_tags WHERE id = ? AND created_by = ?",
        (tag_id, user_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="标签不存在")
    return dict(rows[0])


async def _ensure_host_accessible(db, *, host_id: int, user: dict) -> dict:
    rows = await db.execute_fetchall("SELECT id, created_by FROM hosts WHERE id = ?", (host_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="主机不存在")
    host_row = dict(rows[0])
    if not await _can_access_host(db, host_row, user):
        raise HTTPException(status_code=404, detail="主机不存在")
    return host_row


@router.get("")
async def list_host_tags(user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.id, t.name, t.color, t.created_at, t.updated_at,
                  COUNT(hut.host_id) AS host_count
           FROM host_tags t
           LEFT JOIN host_user_tags hut
             ON hut.tag_id = t.id AND hut.user_id = ?
           WHERE t.created_by = ?
           GROUP BY t.id, t.name, t.color, t.created_at, t.updated_at
           ORDER BY t.name COLLATE NOCASE, t.id""",
        (user["id"], user["id"]),
    )
    return {"success": True, "tags": [dict(r) for r in rows]}


@router.post("")
async def create_host_tag(body: HostTagCreate, user=Depends(get_current_user)):
    name = _normalize_tag_name(body.name)
    if not name:
        raise HTTPException(status_code=400, detail="标签名不能为空")
    color = _normalize_tag_color(body.color)
    db = await get_db()
    dup = await db.execute_fetchall(
        "SELECT id FROM host_tags WHERE created_by = ? AND lower(trim(name)) = lower(trim(?)) LIMIT 1",
        (user["id"], name),
    )
    if dup:
        raise HTTPException(status_code=400, detail="该标签名已存在")
    cur = await db.execute(
        """INSERT INTO host_tags (name, color, created_by)
           VALUES (?, ?, ?)""",
        (name, color, user["id"]),
    )
    await db.commit()
    return {"success": True, "id": cur.lastrowid}


@router.put("/{tag_id}")
async def update_host_tag(tag_id: int, body: HostTagUpdate, user=Depends(get_current_user)):
    db = await get_db()
    await _ensure_tag_owned(db, tag_id=tag_id, user_id=user["id"])
    updates = []
    params = []
    if body.name is not None:
        name = _normalize_tag_name(body.name)
        if not name:
            raise HTTPException(status_code=400, detail="标签名不能为空")
        dup = await db.execute_fetchall(
            """SELECT id FROM host_tags
               WHERE created_by = ? AND lower(trim(name)) = lower(trim(?)) AND id <> ?
               LIMIT 1""",
            (user["id"], name, tag_id),
        )
        if dup:
            raise HTTPException(status_code=400, detail="该标签名已存在")
        updates.append("name = ?")
        params.append(name)
    if body.color is not None:
        updates.append("color = ?")
        params.append(_normalize_tag_color(body.color))
    if not updates:
        return {"success": True}
    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(tag_id)
    params.append(user["id"])
    await db.execute(
        f"UPDATE host_tags SET {', '.join(updates)} WHERE id = ? AND created_by = ?",
        params,
    )
    await db.commit()
    return {"success": True}


@router.delete("/{tag_id}")
async def delete_host_tag(tag_id: int, user=Depends(get_current_user)):
    db = await get_db()
    await _ensure_tag_owned(db, tag_id=tag_id, user_id=user["id"])
    await db.execute("DELETE FROM host_tags WHERE id = ? AND created_by = ?", (tag_id, user["id"]))
    await db.commit()
    return {"success": True}


@router.get("/hosts/{host_id}")
async def get_host_tags_for_host(host_id: int, user=Depends(get_current_user)):
    db = await get_db()
    await _ensure_host_accessible(db, host_id=host_id, user=user)
    rows = await db.execute_fetchall(
        """SELECT t.id, t.name, t.color
           FROM host_user_tags hut
           JOIN host_tags t ON t.id = hut.tag_id
           WHERE hut.user_id = ? AND hut.host_id = ? AND t.created_by = ?
           ORDER BY t.name COLLATE NOCASE, t.id""",
        (user["id"], host_id, user["id"]),
    )
    return {"success": True, "tags": [dict(r) for r in rows]}


@router.put("/hosts/{host_id}")
async def set_host_tags_for_host(host_id: int, body: HostTagAssignRequest, user=Depends(get_current_user)):
    db = await get_db()
    await _ensure_host_accessible(db, host_id=host_id, user=user)
    raw_ids = body.tag_ids or []
    tag_ids = sorted({int(x) for x in raw_ids if x is not None})
    if tag_ids:
        placeholders = ",".join(["?"] * len(tag_ids))
        rows = await db.execute_fetchall(
            f"""SELECT id FROM host_tags
                WHERE created_by = ? AND id IN ({placeholders})""",
            [user["id"], *tag_ids],
        )
        exists = {int(r["id"]) for r in rows}
        missing = [tid for tid in tag_ids if tid not in exists]
        if missing:
            raise HTTPException(status_code=400, detail="存在无效标签 ID")
    await db.execute("DELETE FROM host_user_tags WHERE user_id = ? AND host_id = ?", (user["id"], host_id))
    for tid in tag_ids:
        await db.execute(
            """INSERT OR IGNORE INTO host_user_tags (user_id, host_id, tag_id)
               VALUES (?, ?, ?)""",
            (user["id"], host_id, tid),
        )
    await db.commit()
    rows = await db.execute_fetchall(
        """SELECT t.id, t.name, t.color
           FROM host_user_tags hut
           JOIN host_tags t ON t.id = hut.tag_id
           WHERE hut.user_id = ? AND hut.host_id = ?
           ORDER BY t.name COLLATE NOCASE, t.id""",
        (user["id"], host_id),
    )
    return {"success": True, "tags": [dict(r) for r in rows]}
