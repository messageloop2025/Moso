"""MCP 专用集成 REST（无 Web UI 假设；编排路由仅接受 MCP 客户端标识）。"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from database import get_db
from api.auth import get_current_user
from services.ai_skills import execute_tool
from services.mcp_orchestrator import (
    control_mcp_agent_task,
    get_mcp_agent_task_output,
    list_mcp_agent_tasks,
    run_mcp_orchestrate_chat,
)

router = APIRouter(prefix="/api/integration/mcp", tags=["MCP 集成"])

MCP_CLIENT_HEADER = "X-EdgeOps-Client"
MCP_RUNTIME_SCOPES = ("integration", "mcp_orchestrate", "mcp_runtime")


async def require_mcp_client(
    x_edgeops_client: str | None = Header(None, alias=MCP_CLIENT_HEADER),
) -> None:
    if (x_edgeops_client or "").strip().lower() != "mcp":
        raise HTTPException(
            status_code=403,
            detail=f"此接口仅 MCP 客户端可用；请在 HTTP 头 {MCP_CLIENT_HEADER}: mcp 调用",
        )


async def _ensure_mcp_runtime_session(db, user: dict, session_id: int | None) -> int:
    if session_id is not None:
        rows = await db.execute_fetchall(
            f"""SELECT id FROM ai_chat_sessions
                WHERE id=? AND user_id=?
                  AND COALESCE(session_scope,'default') IN ({",".join("?" * len(MCP_RUNTIME_SCOPES))})""",
            (session_id, user["id"], *MCP_RUNTIME_SCOPES),
        )
        if rows:
            return int(session_id)

    rows = await db.execute_fetchall(
        """SELECT id FROM ai_chat_sessions
           WHERE user_id=? AND session_scope='mcp_runtime'
           ORDER BY updated_at DESC LIMIT 1""",
        (user["id"],),
    )
    if rows:
        return int(rows[0]["id"])

    from datetime import datetime

    title = "MCP-SSH-" + datetime.now().strftime("%Y%m%d%H%M%S")
    await db.execute(
        "INSERT INTO ai_chat_sessions (user_id, title, session_scope) VALUES (?, ?, 'mcp_runtime')",
        (user["id"], title),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    return int((await cur.fetchone())[0])


async def _tool_json(name: str, arguments: dict, user: dict, *, session_id: int | None = None) -> dict:
    raw = await execute_tool(
        name,
        arguments,
        user,
        scope="default",
        ui_capable=False,
        session_id=session_id,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"success": False, "error": raw[:2000]}


class SshExecuteRequest(BaseModel):
    host_id: int
    command: str = Field(..., min_length=1)
    timeout: int | None = Field(default=None, ge=5, le=300)
    detach: bool = False
    poll_log: bool = False
    log_path: str | None = None
    tail_lines: int | None = Field(default=None, ge=1, le=500)
    session_id: int | None = None


class HostPromptUpdateRequest(BaseModel):
    content: str = Field(default="", max_length=50_000)


class HostPromptAppendRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=50_000)


class ProbeCapabilitiesRequest(BaseModel):
    refresh: bool = False
    max_age_hours: int = Field(default=24, ge=0, le=168)
    timeout: int = Field(default=40, ge=10, le=120)


class OrchestrateChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=200_000)
    session_id: int | None = None
    host_id: int | None = None


class TaskControlRequest(BaseModel):
    action: str = Field(..., description="stop | supplement")
    message: str = Field(default="", max_length=8000)


class RemoteFsWriteRequest(BaseModel):
    host_id: int
    path: str = Field(..., min_length=1)
    content: str = Field(default="")


@router.post("/ssh-execute", dependencies=[Depends(require_mcp_client)])
async def mcp_ssh_execute(req: SshExecuteRequest, user=Depends(get_current_user)):
    """MCP：非交互 SSH（支持 detach / poll_log，无 Web UI）。"""
    db = await get_db()
    sid = await _ensure_mcp_runtime_session(db, user, req.session_id)
    args: dict[str, Any] = {
        "host_id": req.host_id,
        "command": req.command,
        "detach": req.detach,
        "poll_log": req.poll_log,
    }
    if req.timeout is not None:
        args["timeout"] = req.timeout
    if req.log_path:
        args["log_path"] = req.log_path
    if req.tail_lines is not None:
        args["tail_lines"] = req.tail_lines
    out = await _tool_json("ssh_execute", args, user, session_id=sid)
    out["session_id"] = sid
    return out


@router.post("/hosts/{host_id}/capabilities/probe", dependencies=[Depends(require_mcp_client)])
async def mcp_probe_capabilities(
    host_id: int,
    req: ProbeCapabilitiesRequest,
    user=Depends(get_current_user),
):
    out = await _tool_json(
        "probe_host_capabilities",
        {
            "host_id": host_id,
            "refresh": req.refresh,
            "max_age_hours": req.max_age_hours,
            "timeout": req.timeout,
        },
        user,
    )
    return out


@router.get("/hosts/{host_id}/capabilities", dependencies=[Depends(require_mcp_client)])
async def mcp_get_capabilities(host_id: int, user=Depends(get_current_user)):
    out = await _tool_json("get_host_capabilities", {"host_id": host_id}, user)
    return out


@router.put("/hosts/{host_id}/prompt", dependencies=[Depends(require_mcp_client)])
async def mcp_update_host_prompt(
    host_id: int,
    req: HostPromptUpdateRequest,
    user=Depends(get_current_user),
):
    out = await _tool_json(
        "update_host_prompt",
        {"host_id": host_id, "content": req.content},
        user,
    )
    return out


@router.post("/hosts/{host_id}/prompt/append", dependencies=[Depends(require_mcp_client)])
async def mcp_append_host_prompt(
    host_id: int,
    req: HostPromptAppendRequest,
    user=Depends(get_current_user),
):
    out = await _tool_json(
        "append_host_prompt",
        {"host_id": host_id, "text": req.text},
        user,
    )
    return out


@router.get("/orchestrate/capabilities", dependencies=[Depends(require_mcp_client)])
async def mcp_orchestrate_capabilities():
    return {
        "success": True,
        "orchestrate_v1": True,
        "mcp_only": True,
        "modes": ["reply_direct", "background_task"],
        "task_control": ["stop", "supplement"],
    }


@router.post("/orchestrate/chat", dependencies=[Depends(require_mcp_client)])
async def mcp_orchestrate_chat(req: OrchestrateChatRequest, user=Depends(get_current_user)):
    db = await get_db()
    return await run_mcp_orchestrate_chat(
        db,
        user,
        req.message,
        session_id=req.session_id,
        host_id=req.host_id,
    )


@router.get("/orchestrate/tasks", dependencies=[Depends(require_mcp_client)])
async def mcp_orchestrate_tasks(
    session_id: int | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    user=Depends(get_current_user),
):
    db = await get_db()
    return await list_mcp_agent_tasks(
        db, user, session_id=session_id, status=status, limit=limit
    )


@router.get("/orchestrate/tasks/{task_id}", dependencies=[Depends(require_mcp_client)])
async def mcp_orchestrate_task_output(task_id: int, user=Depends(get_current_user)):
    db = await get_db()
    out = await get_mcp_agent_task_output(db, user, task_id)
    if not out.get("success"):
        raise HTTPException(status_code=404, detail=out.get("error") or "任务不存在")
    return out


@router.post("/orchestrate/tasks/{task_id}/control", dependencies=[Depends(require_mcp_client)])
async def mcp_orchestrate_task_control(
    task_id: int,
    req: TaskControlRequest,
    user=Depends(get_current_user),
):
    db = await get_db()
    out = await control_mcp_agent_task(db, user, task_id, req.action, req.message)
    if not out.get("success"):
        raise HTTPException(status_code=400, detail=out.get("error") or "控制失败")
    return out


@router.get("/sessions/{session_id}/messages", dependencies=[Depends(require_mcp_client)])
async def mcp_session_messages(
    session_id: int,
    limit: int = Query(50, ge=1, le=200),
    user=Depends(get_current_user),
):
    """读取 integration / mcp_orchestrate / mcp_runtime 会话消息（只读）。"""
    from services.integration_session_helpers import list_integration_scope_messages

    db = await get_db()
    return await list_integration_scope_messages(
        db, user, session_id, limit=limit
    )


@router.get("/remote-fs/list", dependencies=[Depends(require_mcp_client)])
async def mcp_remote_fs_list(
    host_id: int = Query(...),
    path: str = Query("/"),
    user=Depends(get_current_user),
):
    from api.remote_fs import remote_list

    return await remote_list(host_id=host_id, path=path, user=user)


@router.get("/remote-fs/read", dependencies=[Depends(require_mcp_client)])
async def mcp_remote_fs_read(
    host_id: int = Query(...),
    path: str = Query(..., min_length=1),
    user=Depends(get_current_user),
):
    from api.remote_fs import remote_read

    return await remote_read(host_id=host_id, path=path, user=user)


@router.post("/remote-fs/write", dependencies=[Depends(require_mcp_client)])
async def mcp_remote_fs_write(req: RemoteFsWriteRequest, user=Depends(get_current_user)):
    from api.remote_fs import WriteBody, remote_write

    return await remote_write(
        host_id=req.host_id,
        path=req.path,
        body=WriteBody(content=req.content),
        user=user,
    )
