"""系统设置与操作日志 API"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from database import get_db
from api.auth import get_current_user, require_admin, _is_admin_role
from services.system_ai_usage import (
    SETTINGS_KEY_SYSTEM_AI_USAGE_LIMIT,
    parse_system_ai_usage_limit_value,
)

router = APIRouter(prefix="/api", tags=["系统设置"])
# 公开（不鉴权）端点：仅暴露登录页等无需登录的页面所需的少量开关位
public_router = APIRouter(prefix="/api/public", tags=["系统设置（公开）"])


# 登录页 / 公开页面相关的"开关"键白名单：只有这些 key 才允许通过公开接口读取，
# 默认均为开启（与历史行为一致）。新增公开开关请在此处登记。
_PUBLIC_LOGIN_WIDGET_KEYS = {
    "login_widget_message_board_enabled": True,
    "login_widget_public_messages_enabled": True,
}
LOGIN_ANNOUNCEMENT_KEY = "login_announcement_md"


def _coerce_bool(raw: object, default: bool) -> bool:
    """把 settings 表里 TEXT 形式的值统一解析为布尔。空值 / 缺失 → default。"""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if not s:
        return default
    return s in ("1", "true", "yes", "on", "y", "t")


@public_router.get("/login-widgets")
async def get_public_login_widget_flags():
    """返回登录页公开浮窗 / 公开留言展示区的展示开关。

    无需鉴权；前端登录页据此决定是否渲染对应区块。值由管理员在后台配置；
    若某个 key 不在 settings 表中，沿用 _PUBLIC_LOGIN_WIDGET_KEYS 中的默认值。
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT key, value FROM settings WHERE key IN ({})".format(
            ",".join("?" * (len(_PUBLIC_LOGIN_WIDGET_KEYS) + 1))
        ),
        tuple(_PUBLIC_LOGIN_WIDGET_KEYS.keys()) + (LOGIN_ANNOUNCEMENT_KEY,),
    )
    raw = {r["key"]: r["value"] for r in rows}
    flags = {
        key: _coerce_bool(raw.get(key), default)
        for key, default in _PUBLIC_LOGIN_WIDGET_KEYS.items()
    }
    return {"success": True, "flags": flags, "announcement_md": raw.get(LOGIN_ANNOUNCEMENT_KEY, "") or ""}


class SettingRequest(BaseModel):
    key: str
    value: str


@router.get("/settings")
async def get_settings(user=Depends(require_admin)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM settings ORDER BY key")
    result = [dict(r) for r in rows]
    return {"success": True, "settings": result}


def _is_secret_key(key: str) -> bool:
    """是否敏感键（列表接口会脱敏，更新时空/*** 不覆盖原值）"""
    k = (key or "").lower()
    return "key" in k or "secret" in k or "password" in k


@router.post("/settings")
async def update_setting(req: SettingRequest, user=Depends(require_admin)):
    from services.site_time import SETTINGS_KEY_SITE_TZ, validate_iana_timezone

    if _is_secret_key(req.key) and (req.value is None or req.value.strip() in ("", "***")):
        return {"success": True}
    if (req.key or "").strip() == SETTINGS_KEY_SITE_TZ:
        ok, err_or_val = validate_iana_timezone(req.value or "")
        if not ok:
            raise HTTPException(status_code=400, detail=err_or_val)
        req = SettingRequest(key=req.key, value=err_or_val)
    if (req.key or "").strip() == SETTINGS_KEY_SYSTEM_AI_USAGE_LIMIT:
        ok, parsed = parse_system_ai_usage_limit_value(req.value)
        if not ok:
            raise HTTPException(status_code=400, detail=parsed)
        req = SettingRequest(key=req.key, value=str(parsed))
    db = await get_db()
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=?, updated_at=CURRENT_TIMESTAMP",
        (req.key, req.value, req.value),
    )
    await db.commit()
    return {"success": True}


def _check_page_size(page_size: int) -> int:
    if page_size not in (20, 50, 100):
        return 20
    return page_size


@router.get("/logs")
async def list_logs(
    page: int = 1,
    page_size: int = 20,
    host_id: int = None,
    user_id: int = None,
    user=Depends(get_current_user),
):
    """操作日志：普通用户仅能查看自己的；管理员可查看全部，并可按 user_id 筛选。"""
    page_size = _check_page_size(page_size)
    page = max(1, page)
    offset = (page - 1) * page_size
    db = await get_db()
    base = """
        FROM operation_logs l LEFT JOIN users u ON u.id = l.user_id
    """
    conditions, params = [], []
    if not _is_admin_role(user.get("role")):
        conditions.append("l.user_id = ?")
        params.append(user["id"])
    elif user_id is not None:
        conditions.append("l.user_id = ?")
        params.append(user_id)
    if host_id is not None:
        conditions.append("l.host_id = ?")
        params.append(host_id)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    count_rows = await db.execute_fetchall("SELECT COUNT(*) as n FROM operation_logs l" + where, params)
    total = count_rows[0][0] if count_rows else 0
    query = "SELECT l.*, u.username " + base + where + " ORDER BY l.created_at DESC LIMIT ? OFFSET ?"
    params.extend([page_size, offset])
    rows = await db.execute_fetchall(query, params)
    return {"success": True, "logs": [dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/logs/export")
async def export_logs(
    limit: int = 5000,
    host_id: Optional[int] = None,
    user_id: Optional[int] = None,
    user=Depends(get_current_user),
):
    """导出操作日志为 JSON，供前端生成 CSV 下载。普通用户仅自己的；管理员可全部或按 user_id。limit 最大 10000。"""
    limit = max(1, min(10000, limit))
    db = await get_db()
    base = """
        FROM operation_logs l LEFT JOIN users u ON u.id = l.user_id
    """
    conditions, params = [], []
    if not _is_admin_role(user.get("role")):
        conditions.append("l.user_id = ?")
        params.append(user["id"])
    elif user_id is not None:
        conditions.append("l.user_id = ?")
        params.append(user_id)
    if host_id is not None:
        conditions.append("l.host_id = ?")
        params.append(host_id)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    rows = await db.execute_fetchall(
        "SELECT l.*, u.username " + base + where + " ORDER BY l.created_at DESC LIMIT ?",
        params,
    )
    return {"success": True, "logs": [dict(r) for r in rows]}


@router.delete("/logs")
async def clear_logs(
    user_id: Optional[int] = None,
    user=Depends(get_current_user),
):
    """删除操作日志：普通用户仅能清空自己的；管理员可清空全部或指定用户的。"""
    db = await get_db()
    if _is_admin_role(user.get("role")):
        if user_id is not None:
            await db.execute("DELETE FROM operation_logs WHERE user_id = ?", (user_id,))
        else:
            await db.execute("DELETE FROM operation_logs")
    else:
        await db.execute("DELETE FROM operation_logs WHERE user_id = ?", (user["id"],))
    await db.commit()
    return {"success": True, "message": "已清空操作日志"}
