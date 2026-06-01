"""当前用户搜索服务（GitHub / Aliyun IQS / ...）配置 API。

约定：
- 配置始终按当前登录用户隔离；管理员也以「自己的用户身份」配置自己的 Key。
- GET /api/search-config/providers   返回所有已注册 provider 的元数据（含字段 schema），用于前端动态渲染表单。
- GET /api/search-config             返回当前用户在所有 provider 下的配置（脱敏，不回显 api_key）。
- PUT /api/search-config/{provider}  patch 写入指定 provider；空串 / "***" 不覆盖原 api_key。
- DELETE /api/search-config/{provider}  彻底删除该 provider 的配置（等同清空）。
- POST /api/search-config/{provider}/test  用当前已保存的 Key 测试连通性。
- POST /api/search-config/{provider}/search  以当前用户配置发起一次搜索（前端验证用，可选）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from database import get_db
from api.auth import get_current_user
from services.search_config import (
    all_providers_meta,
    call_search,
    delete_user_search_config,
    list_user_search_configs,
    upsert_user_search_config,
)
from services.search_providers import get_provider

router = APIRouter(prefix="/api/search-config", tags=["搜索服务配置"])


class UpsertBody(BaseModel):
    api_key: str | None = None
    enabled: bool | None = None
    extra: dict | None = None


class SearchBody(BaseModel):
    query: str
    options: dict | None = None


@router.get("/providers")
async def list_provider_meta(user=Depends(get_current_user)):
    """返回所有已注册搜索服务的元数据（不含任何用户密钥）。"""
    return {"success": True, "providers": all_providers_meta()}


@router.get("")
async def get_my_search_configs(user=Depends(get_current_user)):
    """当前用户在所有 provider 下的配置（脱敏）。"""
    db = await get_db()
    items = await list_user_search_configs(db, user["id"])
    return {"success": True, "configs": items, "providers": all_providers_meta()}


@router.put("/{provider}")
async def upsert_my_search_config(
    provider: str, body: UpsertBody, user=Depends(get_current_user)
):
    """patch 写入指定 provider 的配置；api_key 为空 / "***" 时不覆盖原值。"""
    if not get_provider(provider):
        raise HTTPException(status_code=404, detail=f"未知的搜索服务 provider：{provider}")
    db = await get_db()
    try:
        pub = await upsert_user_search_config(
            db,
            user["id"],
            provider,
            api_key=body.api_key,
            enabled=body.enabled,
            extra=body.extra,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "config": pub}


@router.delete("/{provider}")
async def delete_my_search_config(provider: str, user=Depends(get_current_user)):
    if not get_provider(provider):
        raise HTTPException(status_code=404, detail=f"未知的搜索服务 provider：{provider}")
    db = await get_db()
    await delete_user_search_config(db, user["id"], provider)
    return {"success": True}


@router.post("/{provider}/test")
async def test_my_search_config(provider: str, user=Depends(get_current_user)):
    """用当前已保存的 Key 跑一次最小搜索，验证连通性。"""
    prov = get_provider(provider)
    if not prov:
        raise HTTPException(status_code=404, detail=f"未知的搜索服务 provider：{provider}")
    db = await get_db()
    res = await call_search(db, user["id"], provider, "test", options={"limit": 1})
    return {"success": bool(res.get("success")), "detail": res}


@router.post("/{provider}/search")
async def search_via_provider(
    provider: str, body: SearchBody, user=Depends(get_current_user)
):
    """前端预览用：用当前用户配置直接发起一次搜索。"""
    if not get_provider(provider):
        raise HTTPException(status_code=404, detail=f"未知的搜索服务 provider：{provider}")
    db = await get_db()
    res = await call_search(db, user["id"], provider, body.query, options=body.options or {})
    return res
