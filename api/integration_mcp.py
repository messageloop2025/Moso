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


class HttpRequestBody(BaseModel):
    url: str = Field(..., min_length=1)
    method: str = Field(default="GET")
    headers: dict[str, str] | None = None
    query: dict[str, str] | None = None
    body: str | None = None
    body_encoding: str = Field(default="text")
    timeout: int | None = Field(default=None, ge=5, le=3600)
    max_response_bytes: int | None = Field(default=None, ge=1024)
    follow_redirects: bool = True
    session_id: int | None = None


class HttpDownloadBody(BaseModel):
    url: str = Field(..., min_length=1)
    local_path: str = Field(..., min_length=1)
    headers: dict[str, str] | None = None
    session_managed: bool | None = None
    max_bytes: int | None = Field(default=None, ge=0)
    chunked: bool = False
    chunk_size: int | None = Field(default=None, ge=1024 * 1024)
    chunk_index: int | None = Field(default=None, ge=0)
    merge_chunks: bool = True
    delete_parts: bool = True
    timeout: int | None = Field(default=None, ge=5, le=3600)
    follow_redirects: bool = True
    session_id: int | None = None


class HttpDownloadMergeBody(BaseModel):
    local_path: str = Field(..., min_length=1)
    part_paths: list[str] | None = None
    delete_parts: bool = True
    session_id: int | None = None


class HttpUploadBody(BaseModel):
    url: str = Field(..., min_length=1)
    local_path: str = Field(..., min_length=1)
    method: str = Field(default="POST")
    headers: dict[str, str] | None = None
    field_name: str = Field(default="file")
    form_fields: dict[str, str] | None = None
    content_type: str | None = None
    multipart: bool = True
    max_bytes: int | None = Field(default=None, ge=0)
    timeout: int | None = Field(default=None, ge=5, le=3600)
    follow_redirects: bool = True
    session_id: int | None = None


class ScpPushBody(BaseModel):
    host_id: int
    remote_path: str = Field(..., min_length=1)
    local_path: str | None = None
    content: str | None = None
    recursive: bool = False
    timeout: int | None = Field(default=None, ge=30, le=3600)
    session_id: int | None = None


class ScpPullBody(BaseModel):
    host_id: int
    remote_path: str = Field(..., min_length=1)
    local_path: str = Field(..., min_length=1)
    recursive: bool = False
    session_managed: bool | None = None
    max_bytes: int | None = Field(default=None, ge=0)
    timeout: int | None = Field(default=None, ge=30, le=3600)
    session_id: int | None = None


class BatchCreateBody(BaseModel):
    operation_type: str = Field(..., min_length=1)
    scope_type: str = Field(..., min_length=1)
    scope_value: list[int] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    tag_match_mode: str = "any"


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


@router.post("/http-request", dependencies=[Depends(require_mcp_client)])
async def mcp_http_request(req: HttpRequestBody, user=Depends(get_current_user)):
    """MCP：HTTP/HTTPS 出站请求。"""
    db = await get_db()
    sid = await _ensure_mcp_runtime_session(db, user, req.session_id)
    args: dict[str, Any] = {
        "url": req.url,
        "method": req.method,
        "body_encoding": req.body_encoding,
        "follow_redirects": req.follow_redirects,
    }
    if req.headers:
        args["headers"] = req.headers
    if req.query:
        args["query"] = req.query
    if req.body is not None:
        args["body"] = req.body
    if req.timeout is not None:
        args["timeout"] = req.timeout
    if req.max_response_bytes is not None:
        args["max_response_bytes"] = req.max_response_bytes
    out = await _tool_json("http_request", args, user, session_id=sid)
    out["session_id"] = sid
    return out


@router.post("/http-download", dependencies=[Depends(require_mcp_client)])
async def mcp_http_download(req: HttpDownloadBody, user=Depends(get_current_user)):
    """MCP：HTTP/HTTPS 下载到用户 web/fs。"""
    db = await get_db()
    sid = await _ensure_mcp_runtime_session(db, user, req.session_id)
    args: dict[str, Any] = {
        "url": req.url,
        "local_path": req.local_path,
        "follow_redirects": req.follow_redirects,
    }
    if req.headers:
        args["headers"] = req.headers
    if req.session_managed is not None:
        args["session_managed"] = req.session_managed
    if req.max_bytes is not None:
        args["max_bytes"] = req.max_bytes
    if req.chunked:
        args["chunked"] = True
    if req.chunk_size is not None:
        args["chunk_size"] = req.chunk_size
    if req.chunk_index is not None:
        args["chunk_index"] = req.chunk_index
    if req.merge_chunks is not None:
        args["merge_chunks"] = req.merge_chunks
    if req.delete_parts is not None:
        args["delete_parts"] = req.delete_parts
    if req.timeout is not None:
        args["timeout"] = req.timeout
    out = await _tool_json("http_download", args, user, session_id=sid)
    out["session_id"] = sid
    return out


@router.post("/http-download-merge", dependencies=[Depends(require_mcp_client)])
async def mcp_http_download_merge(req: HttpDownloadMergeBody, user=Depends(get_current_user)):
    """MCP：合并 HTTP 分块下载文件。"""
    db = await get_db()
    sid = await _ensure_mcp_runtime_session(db, user, req.session_id)
    args: dict[str, Any] = {
        "local_path": req.local_path,
        "delete_parts": req.delete_parts,
    }
    if req.part_paths:
        args["part_paths"] = req.part_paths
    out = await _tool_json("http_download_merge", args, user, session_id=sid)
    out["session_id"] = sid
    return out


@router.post("/http-upload", dependencies=[Depends(require_mcp_client)])
async def mcp_http_upload(req: HttpUploadBody, user=Depends(get_current_user)):
    """MCP：从用户 web/fs 上传到 HTTP/HTTPS URL。"""
    db = await get_db()
    sid = await _ensure_mcp_runtime_session(db, user, req.session_id)
    args: dict[str, Any] = {
        "url": req.url,
        "local_path": req.local_path,
        "method": req.method,
        "field_name": req.field_name,
        "multipart": req.multipart,
        "follow_redirects": req.follow_redirects,
    }
    if req.headers:
        args["headers"] = req.headers
    if req.form_fields:
        args["form_fields"] = req.form_fields
    if req.content_type:
        args["content_type"] = req.content_type
    if req.max_bytes is not None:
        args["max_bytes"] = req.max_bytes
    if req.timeout is not None:
        args["timeout"] = req.timeout
    out = await _tool_json("http_upload", args, user, session_id=sid)
    out["session_id"] = sid
    return out


@router.post("/scp-push", dependencies=[Depends(require_mcp_client)])
async def mcp_scp_push(req: ScpPushBody, user=Depends(get_current_user)):
    """MCP：SFTP 推送到主机（与 AI 工具 scp_push 同一实现；大文件用 local_path）。"""
    if not (req.local_path or "").strip() and req.content is None:
        raise HTTPException(status_code=400, detail="需要 local_path 或 content")
    db = await get_db()
    sid = await _ensure_mcp_runtime_session(db, user, req.session_id)
    args: dict[str, Any] = {
        "host_id": req.host_id,
        "remote_path": req.remote_path,
        "recursive": req.recursive,
    }
    if (req.local_path or "").strip():
        args["local_path"] = req.local_path.strip()
    if req.content is not None:
        args["content"] = req.content
    if req.timeout is not None:
        args["timeout"] = req.timeout
    out = await _tool_json("scp_push", args, user, session_id=sid)
    out["session_id"] = sid
    return out


@router.post("/scp-pull", dependencies=[Depends(require_mcp_client)])
async def mcp_scp_pull(req: ScpPullBody, user=Depends(get_current_user)):
    """MCP：SFTP 从主机拉取到 web/fs（与 AI 工具 scp_pull 同一实现；默认不限制体积）。"""
    db = await get_db()
    sid = await _ensure_mcp_runtime_session(db, user, req.session_id)
    args: dict[str, Any] = {
        "host_id": req.host_id,
        "remote_path": req.remote_path,
        "local_path": req.local_path,
        "recursive": req.recursive,
    }
    if req.session_managed is not None:
        args["session_managed"] = req.session_managed
    if req.max_bytes is not None:
        args["max_bytes"] = req.max_bytes
    if req.timeout is not None:
        args["timeout"] = req.timeout
    out = await _tool_json("scp_pull", args, user, session_id=sid)
    out["session_id"] = sid
    return out


@router.post("/batch", dependencies=[Depends(require_mcp_client)])
async def mcp_batch_create(req: BatchCreateBody, user=Depends(get_current_user)):
    """MCP：创建批量任务（含 scp_push/scp_pull），与 Web/AI batch_create 同一实现。"""
    from api.auth import _is_admin_role
    from api.batch import _create_batch_and_start
    from services.batch_executor import BATCH_OP_TYPES

    op = (req.operation_type or "").strip()
    if op not in BATCH_OP_TYPES:
        raise HTTPException(
            status_code=400,
            detail="operation_type 须为 run_command / scp_push / scp_pull / run_script / restart",
        )
    try:
        batch_id = await _create_batch_and_start(
            op,
            (req.scope_type or "").strip(),
            list(req.scope_value or []),
            dict(req.params or {}),
            user["id"],
            req.tag_match_mode,
            _is_admin_role(user.get("role")),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    db = await get_db()
    cur = await db.execute("SELECT total_count FROM batch_operations WHERE id = ?", (batch_id,))
    total = (await cur.fetchone())[0]
    return {
        "success": True,
        "batch_id": batch_id,
        "total": total,
        "message": f"已创建批量任务 #{batch_id}；请用 GET /api/batch/{batch_id} 或 edgeops_get_batch_job 轮询状态",
    }


@router.post("/batch/{batch_id}/cancel", dependencies=[Depends(require_mcp_client)])
async def mcp_batch_cancel(batch_id: int, user=Depends(get_current_user)):
    from api.batch import _can_access_batch, _cancel_batch

    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, created_by FROM batch_operations WHERE id = ?", (batch_id,)
    )
    if not rows or not _can_access_batch(dict(rows[0]), user):
        raise HTTPException(status_code=404, detail="批量操作不存在")
    await _cancel_batch(batch_id)
    return {"success": True}


@router.post("/batch/{batch_id}/retry", dependencies=[Depends(require_mcp_client)])
async def mcp_batch_retry(batch_id: int, user=Depends(get_current_user)):
    from api.batch import _can_access_batch, _retry_batch

    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, created_by FROM batch_operations WHERE id = ?", (batch_id,)
    )
    if not rows or not _can_access_batch(dict(rows[0]), user):
        raise HTTPException(status_code=404, detail="批量操作不存在")
    await _retry_batch(batch_id)
    return {"success": True}
