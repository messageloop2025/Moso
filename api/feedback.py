"""系统内用户反馈 API。

用户端：
- GET    /api/feedback                列出我提交的所有反馈（含管理员回复）
- POST   /api/feedback                提交一条新反馈
- GET    /api/feedback/{id}           查看一条反馈详情（含回复链）
- PATCH  /api/feedback/{id}           编辑（仅 status=open 且无任何回复时允许）
- DELETE /api/feedback/{id}           撤回（同上限制；物理删除）

管理员端：
- GET    /api/admin/feedback?filter=all|unread|open|replied|ignored
- GET    /api/admin/feedback/{id}
- POST   /api/admin/feedback/{id}/reply
- POST   /api/admin/feedback/{id}/ignore
- POST   /api/admin/feedback/{id}/reopen
- POST   /api/admin/feedback/{id}/mark-read
- POST   /api/admin/feedback/mark-all-read
- PATCH  /api/admin/feedback/replies/{reply_id}      仅作者本人可改
- DELETE /api/admin/feedback/replies/{reply_id}      管理员撤回回复
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from database import get_db
from api.auth import get_current_user, require_admin
from services.feedback import (
    create_feedback,
    delete_reply_admin,
    get_feedback_detail,
    ignore_feedback_admin,
    list_feedback_for_admin,
    list_feedback_for_user,
    mark_all_feedback_read,
    mark_feedback_read,
    reopen_feedback_admin,
    reply_feedback_admin,
    update_feedback_by_user,
    update_reply_admin,
    withdraw_feedback_by_user,
)
from services.feedback_notify import schedule_notify_admins_on_new_feedback

user_router = APIRouter(prefix="/api/feedback", tags=["用户反馈"])
admin_router = APIRouter(prefix="/api/admin/feedback", tags=["用户反馈（管理员）"])


class FeedbackCreateBody(BaseModel):
    title: str | None = None
    content: str
    category: str | None = None


class FeedbackPatchBody(BaseModel):
    title: str | None = None
    content: str | None = None
    category: str | None = None


class ReplyBody(BaseModel):
    content: str


# ── 用户端 ──

@user_router.get("")
async def list_my_feedback(user=Depends(get_current_user)):
    db = await get_db()
    items = await list_feedback_for_user(db, user["id"])
    return {"success": True, "items": items}


@user_router.post("")
async def post_my_feedback(body: FeedbackCreateBody, user=Depends(get_current_user)):
    db = await get_db()
    try:
        item = await create_feedback(
            db, user["id"],
            title=body.title or "",
            content=body.content,
            category=body.category or "general",
            is_ai_submitted=False,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    schedule_notify_admins_on_new_feedback(
        db, feedback=item, submitter_username=(user.get("username") or ""),
    )
    return {"success": True, "item": item}


@user_router.get("/{feedback_id}")
async def get_my_feedback(feedback_id: int, user=Depends(get_current_user)):
    db = await get_db()
    is_admin = (user.get("role") or "").lower() == "admin"
    item = await get_feedback_detail(db, feedback_id, requester_user_id=user["id"], is_admin=is_admin)
    if not item:
        raise HTTPException(status_code=404, detail="反馈不存在或无权访问")
    return {"success": True, "item": item}


@user_router.patch("/{feedback_id}")
async def patch_my_feedback(feedback_id: int, body: FeedbackPatchBody, user=Depends(get_current_user)):
    db = await get_db()
    try:
        item = await update_feedback_by_user(
            db, user["id"], feedback_id,
            title=body.title, content=body.content, category=body.category,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "item": item}


@user_router.delete("/{feedback_id}")
async def delete_my_feedback(feedback_id: int, user=Depends(get_current_user)):
    db = await get_db()
    try:
        await withdraw_feedback_by_user(db, user["id"], feedback_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True}


# ── 管理员端 ──

@admin_router.get("")
async def admin_list_feedback(
    filter: str = "all", limit: int = 100, offset: int = 0, _user=Depends(require_admin)
):
    db = await get_db()
    res = await list_feedback_for_admin(db, filter_kind=filter, limit=limit, offset=offset)
    return {"success": True, **res}


@admin_router.get("/{feedback_id}")
async def admin_get_feedback(feedback_id: int, _user=Depends(require_admin)):
    db = await get_db()
    item = await get_feedback_detail(db, feedback_id, requester_user_id=0, is_admin=True)
    if not item:
        raise HTTPException(status_code=404, detail="反馈不存在")
    await mark_feedback_read(db, feedback_id)
    return {"success": True, "item": item}


@admin_router.post("/{feedback_id}/reply")
async def admin_reply(feedback_id: int, body: ReplyBody, user=Depends(require_admin)):
    db = await get_db()
    try:
        reply = await reply_feedback_admin(db, feedback_id, user["id"], body.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "reply": reply}


@admin_router.post("/{feedback_id}/ignore")
async def admin_ignore(feedback_id: int, _user=Depends(require_admin)):
    db = await get_db()
    item = await ignore_feedback_admin(db, feedback_id)
    if not item:
        raise HTTPException(status_code=404, detail="反馈不存在")
    return {"success": True, "item": item}


@admin_router.post("/{feedback_id}/reopen")
async def admin_reopen(feedback_id: int, _user=Depends(require_admin)):
    db = await get_db()
    item = await reopen_feedback_admin(db, feedback_id)
    if not item:
        raise HTTPException(status_code=404, detail="反馈不存在")
    return {"success": True, "item": item}


@admin_router.post("/{feedback_id}/mark-read")
async def admin_mark_read(feedback_id: int, _user=Depends(require_admin)):
    db = await get_db()
    await mark_feedback_read(db, feedback_id)
    return {"success": True}


@admin_router.post("/mark-all-read")
async def admin_mark_all_read(_user=Depends(require_admin)):
    db = await get_db()
    n = await mark_all_feedback_read(db)
    return {"success": True, "marked": n}


@admin_router.patch("/replies/{reply_id}")
async def admin_update_reply(reply_id: int, body: ReplyBody, user=Depends(require_admin)):
    db = await get_db()
    try:
        reply = await update_reply_admin(db, reply_id, user["id"], body.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "reply": reply}


@admin_router.delete("/replies/{reply_id}")
async def admin_delete_reply(reply_id: int, _user=Depends(require_admin)):
    db = await get_db()
    fb_id = await delete_reply_admin(db, reply_id)
    if fb_id is None:
        raise HTTPException(status_code=404, detail="回复不存在")
    return {"success": True, "feedback_id": fb_id}
