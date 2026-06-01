"""用户 API 访问令牌：创建/列出/撤销，供第三方以当前用户权限调用 HTTP API（与 JWT 登录共用 Authorization: Bearer）。"""
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from database import get_db
from api.auth import get_current_user, hash_api_access_token

router = APIRouter(prefix="/api/user-api-tokens", tags=["API 访问令牌"])

MAX_TOKENS_PER_USER = 50


class CreateApiTokenBody(BaseModel):
    name: str = Field(default="", max_length=128)


@router.get("")
async def list_my_api_tokens(user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT id, name, token_prefix, created_at, last_used_at
           FROM api_tokens WHERE user_id = ? ORDER BY id DESC""",
        (user["id"],),
    )
    tokens = [dict(r) for r in rows]
    return {"success": True, "tokens": tokens}


@router.post("")
async def create_api_token(body: CreateApiTokenBody, user=Depends(get_current_user)):
    db = await get_db()
    count_rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM api_tokens WHERE user_id = ?",
        (user["id"],),
    )
    n = int(count_rows[0]["n"] or 0) if count_rows else 0
    if n >= MAX_TOKENS_PER_USER:
        raise HTTPException(
            status_code=400,
            detail=f"每人最多创建 {MAX_TOKENS_PER_USER} 个 API 令牌，请先删除不再使用的令牌",
        )
    plain = "eop_" + secrets.token_urlsafe(32)
    token_hash = hash_api_access_token(plain)
    prefix = plain[:14] + "…" if len(plain) > 14 else plain
    name = (body.name or "").strip()[:128]
    cur = await db.execute(
        """INSERT INTO api_tokens (user_id, name, token_hash, token_prefix)
           VALUES (?, ?, ?, ?)""",
        (user["id"], name, token_hash, prefix),
    )
    await db.commit()
    token_id = cur.lastrowid
    return {
        "success": True,
        "id": token_id,
        "name": name,
        "token": plain,
        "token_hint": prefix,
        "message": "请立即保存完整令牌，关闭后将无法再次查看",
    }


@router.delete("/{token_id}")
async def revoke_api_token(token_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM api_tokens WHERE id = ? AND user_id = ?",
        (token_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="令牌不存在或无权删除")
    await db.execute(
        "DELETE FROM api_tokens WHERE id = ? AND user_id = ?",
        (token_id, user["id"]),
    )
    await db.commit()
    return {"success": True}
