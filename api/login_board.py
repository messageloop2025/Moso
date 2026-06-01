"""登录页匿名留言板 API。

公开接口（不鉴权）：
- GET  /api/login-board                列出当前要在登录页展示的留言+回复
- POST /api/login-board                匿名访客提交一条留言（IP 级频控）
- GET  /api/login-board/captcha        提交前先请求一个数学验证码

管理员接口（require_admin）：
- GET    /api/admin/login-board                       全部留言（含未审核 / 已隐藏）
- POST   /api/admin/login-board/{id}/reply            管理员对某条留言发回复
- PATCH  /api/admin/login-board/{id}                  改 status / show_on_login（留言或回复均可）
- DELETE /api/admin/login-board/{id}                  硬删除一条留言（含其回复）
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from database import get_db
from api.auth import (
    create_captcha_token,
    require_admin,
    verify_and_consume_captcha,
    _generate_captcha,
)
from services.feedback import (
    check_anon_rate_limit,
    create_anon_message,
    delete_anon_message_admin,
    list_anon_messages_admin,
    list_login_board_public,
    reply_anon_message_admin,
    update_anon_message_admin,
)

public_router = APIRouter(prefix="/api/login-board", tags=["登录页留言板"])
admin_router = APIRouter(prefix="/api/admin/login-board", tags=["登录页留言板（管理员）"])


def _client_ip(req: Request) -> str:
    """提取客户端 IP；优先 X-Forwarded-For，其次 client.host。"""
    fwd = req.headers.get("x-forwarded-for") or req.headers.get("X-Forwarded-For") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    return (req.client.host if req.client else "") or ""


def _client_ua(req: Request) -> str:
    return (req.headers.get("user-agent") or req.headers.get("User-Agent") or "")[:255]


class AnonPostBody(BaseModel):
    nickname: str | None = None
    content: str
    captcha_token: str | None = None
    captcha_answer: str | None = None


class AnonReplyBody(BaseModel):
    content: str
    show_on_login: bool | None = False


class AnonPatchBody(BaseModel):
    show_on_login: bool | None = None
    status: str | None = None  # 'pending' / 'approved' / 'hidden'


# ── 公开接口 ──

@public_router.get("/captcha")
async def get_anon_captcha():
    """登录页留言板提交用的简易数学验证码。"""
    q, a = _generate_captcha()
    return {"question": q, "captcha_token": create_captcha_token(a)}


@public_router.get("")
async def list_login_board(limit: int = 30):
    db = await get_db()
    items = await list_login_board_public(db, limit=limit)
    return {"success": True, "items": items}


@public_router.post("")
async def post_anon_message(body: AnonPostBody, request: Request):
    if not await verify_and_consume_captcha(body.captcha_token, body.captcha_answer):
        raise HTTPException(status_code=400, detail="验证码错误或已过期，请刷新后重试")
    db = await get_db()
    ip = _client_ip(request)
    ok, why = await check_anon_rate_limit(db, ip)
    if not ok:
        raise HTTPException(status_code=429, detail=why)
    try:
        msg = await create_anon_message(
            db,
            nickname=(body.nickname or ""),
            content=body.content,
            ip_address=ip,
            user_agent=_client_ua(request),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "success": True,
        "message": "留言已提交，待管理员审核后可在登录页公开展示。",
        "item": {"id": msg.get("id"), "status": msg.get("status")},
    }


# ── 管理员接口 ──

@admin_router.get("")
async def admin_list_anon_messages(status: str | None = None, limit: int = 100, offset: int = 0, _user=Depends(require_admin)):
    db = await get_db()
    items = await list_anon_messages_admin(db, status=status, limit=limit, offset=offset)
    return {"success": True, "items": items}


@admin_router.post("/{msg_id}/reply")
async def admin_reply_anon(msg_id: int, body: AnonReplyBody, user=Depends(require_admin)):
    db = await get_db()
    try:
        reply = await reply_anon_message_admin(
            db, msg_id, user["id"], body.content, show_on_login=bool(body.show_on_login)
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "reply": reply}


@admin_router.patch("/{msg_id}")
async def admin_update_anon(msg_id: int, body: AnonPatchBody, _user=Depends(require_admin)):
    db = await get_db()
    try:
        item = await update_anon_message_admin(
            db, msg_id, show_on_login=body.show_on_login, status=body.status
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if item is None:
        raise HTTPException(status_code=404, detail="留言不存在")
    return {"success": True, "item": item}


@admin_router.delete("/{msg_id}")
async def admin_delete_anon(msg_id: int, _user=Depends(require_admin)):
    db = await get_db()
    ok = await delete_anon_message_admin(db, msg_id)
    if not ok:
        raise HTTPException(status_code=404, detail="留言不存在")
    return {"success": True}
