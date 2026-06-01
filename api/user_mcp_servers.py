"""当前用户 MCP 服务器配置 API（每用户隔离）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api.auth import get_current_user
from database import get_db
from services.user_mcp_client import (
    invalidate_user_mcp_cache,
    refresh_user_mcp_server_tools,
    test_user_mcp_server,
)
from services.user_mcp_import import import_user_mcp_servers
from services.user_mcp_export import export_user_mcp_servers, export_user_mcp_servers_json
from services.user_mcp_registry import (
    create_user_mcp_server,
    delete_user_mcp_server,
    get_user_mcp_server,
    get_user_mcp_server_raw,
    list_user_mcp_servers,
    update_user_mcp_server,
)

router = APIRouter(prefix="/api/user-mcp-servers", tags=["用户 MCP 配置"])


class McpServerCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field("", max_length=120)
    transport: str = Field("stdio", description="stdio | sse | streamable_http")
    enabled: bool = True
    chat_enabled: bool = True
    chat_scope_web: bool = True
    chat_scope_host: bool = True
    chat_scope_integration: bool = True
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)


class McpServerUpdateBody(BaseModel):
    name: str | None = None
    display_name: str | None = None
    transport: str | None = None
    enabled: bool | None = None
    chat_enabled: bool | None = None
    chat_scope_web: bool | None = None
    chat_scope_host: bool | None = None
    chat_scope_integration: bool | None = None
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None


class McpServerImportBody(BaseModel):
    config: str | dict[str, Any] = Field(..., description="Cursor 风格 mcp.json 或 {mcpServers:{...}}")
    overwrite: bool = False
    chat_enabled: bool = True
    chat_scope_web: bool = True
    chat_scope_host: bool = True
    chat_scope_integration: bool = True


def _config_from_body(body: McpServerCreateBody | McpServerUpdateBody) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("command", "args", "url", "env", "headers"):
        if hasattr(body, key):
            val = getattr(body, key)
            if val is not None:
                out[key] = val
    return out


@router.get("")
async def list_my_mcp_servers(user=Depends(get_current_user)):
    db = await get_db()
    items = await list_user_mcp_servers(db, user["id"])
    return {
        "success": True,
        "servers": items,
        "transports": [
            {"id": "stdio", "label": "stdio（本地命令，Mac/Linux/Windows）"},
            {"id": "sse", "label": "SSE（远程 HTTP）"},
            {"id": "streamable_http", "label": "Streamable HTTP"},
        ],
    }


@router.post("")
async def create_my_mcp_server(body: McpServerCreateBody, user=Depends(get_current_user)):
    db = await get_db()
    try:
        row = await create_user_mcp_server(
            db,
            user["id"],
            name=body.name,
            display_name=body.display_name,
            transport=body.transport,
            config=_config_from_body(body),
            enabled=body.enabled,
            chat_enabled=body.chat_enabled,
            chat_scope_web=body.chat_scope_web,
            chat_scope_host=body.chat_scope_host,
            chat_scope_integration=body.chat_scope_integration,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        if "UNIQUE" in str(e).upper():
            raise HTTPException(status_code=409, detail="同名 MCP 服务器已存在") from e
        raise
    invalidate_user_mcp_cache(user_id=user["id"])
    return {"success": True, "server": row}


@router.post("/import")
async def import_my_mcp_servers(body: McpServerImportBody, user=Depends(get_current_user)):
    db = await get_db()
    try:
        result = await import_user_mcp_servers(
            db,
            user["id"],
            body.config,
            overwrite=body.overwrite,
            chat_enabled=body.chat_enabled,
            chat_scope_web=body.chat_scope_web,
            chat_scope_host=body.chat_scope_host,
            chat_scope_integration=body.chat_scope_integration,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    invalidate_user_mcp_cache(user_id=user["id"])
    return {"success": bool(result.get("success")), **result}


@router.get("/export")
async def export_my_mcp_servers(
    user=Depends(get_current_user),
    include_disabled: bool = True,
    include_edgeops_meta: bool = True,
    download: bool = False,
):
    """导出 Cursor 风格 mcp.json；download=true 时作为附件下载。"""
    db = await get_db()
    data = await export_user_mcp_servers(
        db,
        user["id"],
        include_disabled=include_disabled,
        include_edgeops_meta=include_edgeops_meta,
    )
    text = export_user_mcp_servers_json(data)
    if download:
        username = (user.get("username") or "user").strip() or "user"
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in username)[:32]
        filename = f"edgeops-mcp-{safe}.json"
        return Response(
            content=text,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return {"success": True, "config": data, "json": text, "count": len(data.get("mcpServers") or {})}


@router.get("/{server_id}")
async def get_my_mcp_server(server_id: int, user=Depends(get_current_user)):
    db = await get_db()
    row = await get_user_mcp_server(db, user["id"], server_id)
    if not row:
        raise HTTPException(status_code=404, detail="MCP 服务器不存在")
    return {"success": True, "server": row}


@router.put("/{server_id}")
async def update_my_mcp_server(
    server_id: int, body: McpServerUpdateBody, user=Depends(get_current_user)
):
    db = await get_db()
    try:
        row = await update_user_mcp_server(
            db,
            user["id"],
            server_id,
            name=body.name,
            display_name=body.display_name,
            transport=body.transport,
            config_patch=_config_from_body(body),
            enabled=body.enabled,
            chat_enabled=body.chat_enabled,
            chat_scope_web=body.chat_scope_web,
            chat_scope_host=body.chat_scope_host,
            chat_scope_integration=body.chat_scope_integration,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="MCP 服务器不存在") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    invalidate_user_mcp_cache(user_id=user["id"], server_id=server_id)
    return {"success": True, "server": row}


@router.delete("/{server_id}")
async def delete_my_mcp_server(server_id: int, user=Depends(get_current_user)):
    db = await get_db()
    ok = await delete_user_mcp_server(db, user["id"], server_id)
    if not ok:
        raise HTTPException(status_code=404, detail="MCP 服务器不存在")
    invalidate_user_mcp_cache(user_id=user["id"], server_id=server_id)
    return {"success": True}


@router.post("/{server_id}/test")
async def test_my_mcp_server(server_id: int, user=Depends(get_current_user)):
    db = await get_db()
    raw = await get_user_mcp_server_raw(db, user["id"], server_id)
    if not raw:
        raise HTTPException(status_code=404, detail="MCP 服务器不存在")
    result = await test_user_mcp_server(db, user["id"], raw)
    server = await get_user_mcp_server(db, user["id"], server_id)
    return {"success": bool(result.get("success")), "detail": result, "server": server}


@router.post("/{server_id}/refresh-tools")
async def refresh_my_mcp_server_tools(server_id: int, user=Depends(get_current_user)):
    db = await get_db()
    result = await refresh_user_mcp_server_tools(db, user["id"], server_id=server_id)
    if not result.get("success") and result.get("error") == "MCP 服务器不存在":
        raise HTTPException(status_code=404, detail=result["error"])
    return result
