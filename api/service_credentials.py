"""主机服务凭证 REST API（元数据；密码不可查询）。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from api.auth import get_current_user
from services.credential_vault import (
    SETTINGS_KEY,
    add_credential,
    credentials_vault_enabled,
    delete_credential,
    get_credential_for_user,
    search_credentials_for_user,
    perform_service_password_injection,
    update_credential,
)

router = APIRouter(prefix="/api/service-credentials", tags=["服务凭证"])


class ServiceCredentialCreate(BaseModel):
    service: str
    password: Optional[str] = None
    address: str = ""
    port: Optional[int] = None
    service_username: str = ""
    label: str = ""
    notes: str = ""
    linked_credential_id: Optional[int] = None
    linked_host_id: Optional[int] = None
    host_id: Optional[int] = None


class ServiceCredentialUpdate(BaseModel):
    service: Optional[str] = None
    password: Optional[str] = None
    address: Optional[str] = None
    port: Optional[int] = None
    service_username: Optional[str] = None
    label: Optional[str] = None
    notes: Optional[str] = None
    linked_credential_id: Optional[int] = None
    linked_host_id: Optional[int] = None
    host_id: Optional[int] = None


class ServiceCredentialInject(BaseModel):
    credential_id: Optional[int] = None
    target: str = "terminal"
    host_id: Optional[int] = None
    slot: Optional[int] = None
    channel_id: Optional[int] = None
    scope_id: Optional[str] = None
    require_password_prompt: bool = True
    use_host_login: bool = False


async def _require_vault_enabled():
    if not await credentials_vault_enabled():
        raise HTTPException(status_code=403, detail="凭证库功能未启用（需管理员在系统设置中开启 credentials_vault_enabled）")


@router.get("/enabled")
async def vault_feature_status(user=Depends(get_current_user)):
    return {"success": True, "enabled": await credentials_vault_enabled(), "settings_key": SETTINGS_KEY}


@router.get("")
async def list_service_credentials(
    service: Optional[str] = Query(None),
    address: Optional[str] = Query(None),
    port: Optional[int] = Query(None),
    service_username: Optional[str] = Query(None),
    host_id: Optional[int] = Query(None, description="本机 sudo 时传入：仅匹配绑定该主机的凭证"),
    keyword: Optional[str] = Query(None, description="模糊搜索 id/address/username/label/notes/service"),
    command_hint: Optional[str] = Query(None, description="从命令推断 service/address 等并过滤"),
    sort_by: Optional[str] = Query("last_accessed_at"),
    sort_order: Optional[str] = Query("desc"),
    limit: Optional[int] = Query(50, ge=1, le=200),
    user=Depends(get_current_user),
):
    await _require_vault_enabled()
    result = await search_credentials_for_user(
        user["id"],
        service=service,
        address=address,
        port=port,
        service_username=service_username,
        host_id=host_id,
        keyword=keyword,
        command_hint=command_hint,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
    )
    return {"success": True, **result}


@router.get("/{credential_id}")
async def get_service_credential(credential_id: int, user=Depends(get_current_user)):
    await _require_vault_enabled()
    item = await get_credential_for_user(user["id"], credential_id)
    if not item:
        raise HTTPException(status_code=404, detail="凭证不存在")
    return {"success": True, "credential": item}


@router.post("")
async def create_service_credential(body: ServiceCredentialCreate, user=Depends(get_current_user)):
    await _require_vault_enabled()
    if not body.password and not body.linked_host_id and not body.linked_credential_id:
        raise HTTPException(status_code=400, detail="需要 password，或设置 linked_host_id / linked_credential_id")
    try:
        item = await add_credential(
            user,
            service=body.service,
            password=body.password,
            address=body.address,
            port=body.port,
            service_username=body.service_username,
            label=body.label,
            notes=body.notes,
            linked_credential_id=body.linked_credential_id,
            linked_host_id=body.linked_host_id,
            host_id=body.host_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "credential": item}


@router.put("/{credential_id}")
async def update_service_credential(
    credential_id: int, body: ServiceCredentialUpdate, user=Depends(get_current_user)
):
    await _require_vault_enabled()
    try:
        item = await update_credential(
            user,
            credential_id,
            service=body.service,
            password=body.password,
            address=body.address,
            port=body.port,
            service_username=body.service_username,
            label=body.label,
            notes=body.notes,
            linked_credential_id=body.linked_credential_id,
            linked_host_id=body.linked_host_id,
            host_id=body.host_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "credential": item}


@router.delete("/{credential_id}")
async def remove_service_credential(credential_id: int, user=Depends(get_current_user)):
    await _require_vault_enabled()
    try:
        await delete_credential(user, credential_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"success": True, "message": "已删除"}


@router.post("/inject")
async def inject_service_password(body: ServiceCredentialInject, user=Depends(get_current_user)):
    """注入密码到终端/SSH 通道（不返回明文）。credential_id 或 use_host_login+host_id。"""
    await _require_vault_enabled()
    result = await perform_service_password_injection(
        user,
        credential_id=body.credential_id,
        target=body.target,
        host_id=body.host_id,
        slot=body.slot,
        channel_id=body.channel_id,
        terminal_scope_id=body.scope_id,
        require_password_prompt=body.require_password_prompt,
        use_host_login=body.use_host_login,
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result)
    return result
