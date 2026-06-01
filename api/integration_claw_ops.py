"""ClawOps（OpenClaw）集成：manifest / 提示词 / 更新检查 / 统一 invoke。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from database import get_db
from api.auth import get_current_user
from services.claw_ops_registry import (
    check_plugin_update,
    get_claw_ops_manifest,
    invoke_claw_ops_tool,
)

router = APIRouter(prefix="/api/integration/claw-ops", tags=["ClawOps 集成"])

CLAW_CLIENT_HEADER = "X-EdgeOps-Client"


async def optional_claw_client(
    x_edgeops_client: str | None = Header(None, alias=CLAW_CLIENT_HEADER),
) -> str | None:
    return (x_edgeops_client or "").strip().lower() or None


class InvokeRequest(BaseModel):
    tool: str = Field(..., min_length=1, description="edgeops_* 工具名")
    arguments: dict[str, Any] = Field(default_factory=dict)


@router.get("/manifest")
async def claw_ops_manifest(
    base_url: str = Query("", description="可选，用于提示词中的 毛竹 根地址展示"),
    plugin_version: str = Query("", description="可选，claw-ops 插件版本"),
    capabilities_version: str = Query("", description="可选，客户端已缓存的 capabilities 版本"),
    user=Depends(get_current_user),
):
    """
    ClawOps 能力清单：扩展工具 schema、系统提示词、版本要求。
    v1.1.0+ 插件启动/ping 时按 extended_tools 动态 registerTool；
    capabilities_version 未变时仍返回完整 manifest（含 unchanged 标记）。
    """
    manifest = get_claw_ops_manifest(base_url=base_url)
    if plugin_version:
        manifest["update_check"] = check_plugin_update(plugin_version)
    if capabilities_version and capabilities_version == manifest.get("capabilities_version"):
        manifest["unchanged"] = True
    return manifest


@router.get("/check-update")
async def claw_ops_check_update(
    plugin_version: str = Query(..., min_length=1),
    user=Depends(get_current_user),
):
    return check_plugin_update(plugin_version)


@router.get("/system-prompt")
async def claw_ops_system_prompt(
    base_url: str = Query(""),
    user=Depends(get_current_user),
):
    manifest = get_claw_ops_manifest(base_url=base_url)
    return manifest.get("system_prompt") or {}


@router.post("/invoke")
async def claw_ops_invoke(
    req: InvokeRequest,
    user=Depends(get_current_user),
    client: str | None = Depends(optional_claw_client),
):
    """统一工具调用入口；ClawOps 扩展工具与 edgeops_invoke 使用。

    业务失败（工具名非法、参数错误、执行异常等）以 `{success: false, error}` 形式
    在 HTTP 200 body 内返回，便于客户端统一解析；仅鉴权 / 客户端头校验走 4xx。
    """
    if client and client not in ("openclaw", "claw-ops", "mcp"):
        raise HTTPException(status_code=403, detail=f"不支持的 {CLAW_CLIENT_HEADER}: {client}")
    tool = (req.tool or "").strip()
    if not tool:
        return {"success": False, "error": "tool 不能为空"}
    db = await get_db()
    try:
        return await invoke_claw_ops_tool(db, user, tool, req.arguments)
    except Exception as e:  # noqa: BLE001 - 兜底，避免内部异常直接 500
        return {"success": False, "error": f"调用失败: {e}", "tool": tool}
