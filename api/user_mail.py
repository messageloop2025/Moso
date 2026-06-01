"""当前用户发信（SMTP）配置 API：与管理员全局邮件配置独立。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from database import get_db
from api.auth import get_current_user
from services.user_mail import USER_MAIL_SETUP_HINT_ZH, load_user_mail_config, public_mail_config_for_api, upsert_user_mail_from_patch

router = APIRouter(prefix="/api/user-mail-config", tags=["用户发信配置"])


class UserMailConfigBody(BaseModel):
    mail_enabled: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_use_tls: bool | None = None
    smtp_use_ssl: bool | None = None


@router.get("")
async def fetch_user_mail_settings(user=Depends(get_current_user)):
    db = await get_db()
    cfg = await load_user_mail_config(db, user["id"])
    pwd_set = bool((cfg.get("smtp_password") or "").strip())
    return {"success": True, "config": public_mail_config_for_api(cfg, pwd_set), "setup_hint": USER_MAIL_SETUP_HINT_ZH}


@router.put("")
async def save_user_mail_settings(body: UserMailConfigBody, user=Depends(get_current_user)):
    db = await get_db()
    patch = body.model_dump(exclude_unset=True)
    ok, err, pub = await upsert_user_mail_from_patch(db, user["id"], patch)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    return {"success": True, "config": pub}
