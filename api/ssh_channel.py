"""SSH Channel（TTY 通道）API — 供 AI、OpenClaw 集成与后台任务使用，按会话/任务隔离

设计见 docs/SSH通道与后台任务设计.md。真实 TTY 与行缓冲由 ssh_channel_manager 提供。
"""
import asyncio
import logging
import socket
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from typing import Optional

from database import get_db
from api.auth import get_current_user, user_dict_for_websocket_from_token
from services.ssh_channel_manager import SSHChannelManager
from services.ssh_channel_service import (
    close_channel_full,
    close_channels_by_owner,
    create_channel_and_open,
    default_idle_close_sec,
    dump_channel_buffer_to_file,
    format_lines_as_text,
    get_channel_detail,
    list_channels_for_user,
    maybe_spill_channel_text,
    reconcile_channel_if_stale,
)

logger = logging.getLogger("edgeops.ssh_channel")
router = APIRouter(prefix="/api/ssh-channel", tags=["SSH Channel"])


def _check_host_alive(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


class SSHChannelCreate(BaseModel):
    host_id: int
    owner_type: str = "session"
    owner_id: str = ""
    session_id: Optional[int] = Field(
        default=None,
        description="集成/OpenClaw 会话 ID；设置后 owner 绑定为该 session，默认空闲关断 600s",
    )
    input_timeout_sec: Optional[int] = None
    output_timeout_sec: Optional[int] = None
    idle_close_sec: Optional[int] = Field(
        default=None,
        description="空闲自动关断秒数；省略时 Web 终端会话默认 300，集成 session_id 默认 600",
    )


class SSHChannelSend(BaseModel):
    content: str = ""


class SSHChannelDumpBody(BaseModel):
    session_id: Optional[int] = Field(default=None, description="可选，落盘路径与会话 spill 关联")
    max_chars: Optional[int] = Field(default=2_000_000, ge=1024, le=4_000_000)


class SSHChannelCloseBatchBody(BaseModel):
    owner_type: str = Field(default="session", description="session 或 task")
    owner_id: str = Field(default="", description="会话 ID 或任务 ID")
    session_id: Optional[int] = Field(
        default=None,
        description="若提供则等价 owner_type=session, owner_id=str(session_id)",
    )


@router.get("")
async def list_channels(
    owner_type: str = Query("session"),
    owner_id: str = Query(""),
    all_open: bool = Query(False, description="为 true 时列出当前用户全部 open 通道"),
    user=Depends(get_current_user),
):
    db = await get_db()
    channels = await list_channels_for_user(
        db,
        user,
        owner_type=owner_type,
        owner_id=owner_id,
        all_open=all_open,
    )
    return {"success": True, "channels": channels, "count": len(channels)}


@router.post("")
async def create_channel(body: SSHChannelCreate, user=Depends(get_current_user)):
    db = await get_db()
    if body.session_id is not None:
        owner_type = "session"
        owner_id = str(body.session_id)
        integration_ctx = True
    else:
        owner_type = (body.owner_type or "session").strip() or "session"
        owner_id = (body.owner_id or "").strip()
        integration_ctx = False
    idle = default_idle_close_sec(
        explicit=body.idle_close_sec,
        session_id=body.session_id,
        terminal_scope_id=None,
        integration_context=integration_ctx,
    )
    try:
        channel = await create_channel_and_open(
            db,
            user,
            host_id=body.host_id,
            owner_type=owner_type,
            owner_id=owner_id,
            input_timeout_sec=body.input_timeout_sec,
            output_timeout_sec=body.output_timeout_sec,
            idle_close_sec=idle,
            session_id=body.session_id,
            integration_context=integration_ctx,
        )
    except ValueError as e:
        msg = str(e)
        if msg == "无权在该主机上创建 SSH 通道":
            code = 403
        elif msg in ("主机不存在", "主机未配置有效登录凭证"):
            code = 400
        else:
            code = 502
        raise HTTPException(status_code=code, detail=msg) from e
    return {"success": True, "channel_id": channel["id"], "channel": channel}


@router.get("/{channel_id}/host-alive")
async def channel_host_alive(channel_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT c.id, h.host, h.port FROM ssh_channels c
           JOIN hosts h ON h.id = c.host_id
           WHERE c.id = ? AND c.user_id = ?""",
        (channel_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="通道不存在")
    row = dict(rows[0])
    alive = await asyncio.to_thread(
        _check_host_alive, row.get("host", ""), int(row.get("port") or 22), 3.0
    )
    return {"success": True, "alive": alive}


@router.get("/{channel_id}")
async def get_channel(
    channel_id: int,
    check_alive: Optional[int] = Query(None, description="为 1 时在响应中附带 host_alive 探测结果"),
    user=Depends(get_current_user),
):
    db = await get_db()
    detail = await get_channel_detail(db, user, channel_id)
    if not detail:
        raise HTTPException(status_code=404, detail="通道不存在")
    port = detail.pop("port", None)
    out = {"success": True, "channel": detail}
    if check_alive == 1:
        out["host_alive"] = await asyncio.to_thread(
            _check_host_alive, detail.get("host_ip") or "", int(port or 22), 3.0
        )
    return out


@router.post("/close-batch")
async def close_channels_batch(body: SSHChannelCloseBatchBody, user=Depends(get_current_user)):
    """按 owner（或 session_id）批量关闭当前用户 open 通道。"""
    db = await get_db()
    if body.session_id is not None:
        otype, oid = "session", str(body.session_id)
    else:
        otype = (body.owner_type or "session").strip() or "session"
        oid = (body.owner_id or "").strip()
    result = await close_channels_by_owner(db, user, owner_type=otype, owner_id=oid)
    return {"success": True, **result}


@router.post("/{channel_id}/send")
async def send_to_channel(channel_id: int, body: SSHChannelSend, user=Depends(get_current_user)):
    db = await get_db()
    await reconcile_channel_if_stale(db, user, channel_id)
    rows = await db.execute_fetchall(
        "SELECT id FROM ssh_channels WHERE id = ? AND user_id = ? AND status = 'open'",
        (channel_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="通道不存在或已关闭")
    err = SSHChannelManager.get_instance().send(channel_id, body.content or "")
    if err:
        raise HTTPException(status_code=502, detail=err)
    return {"success": True, "message": "已发送"}


@router.get("/{channel_id}/lines")
async def read_lines(
    channel_id: int,
    from_line: Optional[int] = Query(None),
    to_line: Optional[int] = Query(None),
    last_n: Optional[int] = Query(None),
    since_line: Optional[int] = Query(None),
    spill: bool = Query(True, description="输出过大时自动落盘并返回 preview + spill_id"),
    session_id: Optional[int] = Query(None, description="落盘时关联的会话 ID（可选）"),
    user=Depends(get_current_user),
):
    db = await get_db()
    await reconcile_channel_if_stale(db, user, channel_id)
    rows = await db.execute_fetchall(
        "SELECT id FROM ssh_channels WHERE id = ? AND user_id = ?",
        (channel_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="通道不存在")
    result = SSHChannelManager.get_instance().get_lines(
        channel_id, from_line=from_line, to_line=to_line, last_n=last_n, since_line=since_line
    )
    if result is None:
        return {"success": True, "lines": [], "oldest_line_no": 0, "latest_line_no": 0}
    lines, oldest, latest = result
    payload = {"success": True, "lines": lines, "oldest_line_no": oldest, "latest_line_no": latest}
    if spill:
        text = format_lines_as_text(lines)
        spill_info = maybe_spill_channel_text(user, session_id, channel_id, text, tool_suffix="read_lines")
        if spill_info.get("spilled"):
            payload["spill"] = spill_info
            payload["text_preview"] = spill_info.get("preview", "")
        else:
            payload["text"] = spill_info.get("content", text)
    return payload


@router.get("/{channel_id}/read")
async def read_length(
    channel_id: int,
    max_chars: Optional[int] = Query(None, ge=1, le=1024 * 1024),
    spill: bool = Query(True),
    session_id: Optional[int] = Query(None),
    user=Depends(get_current_user),
):
    db = await get_db()
    await reconcile_channel_if_stale(db, user, channel_id)
    rows = await db.execute_fetchall(
        "SELECT id FROM ssh_channels WHERE id = ? AND user_id = ?",
        (channel_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="通道不存在")
    result = SSHChannelManager.get_instance().get_content_length(channel_id, max_chars or 8192)
    if result is None:
        return {"success": True, "content": "", "length": 0, "oldest_line_no": 0, "latest_line_no": 0}
    content, oldest, latest = result
    payload = {"success": True, "length": len(content), "oldest_line_no": oldest, "latest_line_no": latest}
    if spill:
        spill_info = maybe_spill_channel_text(user, session_id, channel_id, content, tool_suffix="read_length")
        if spill_info.get("spilled"):
            payload["spill"] = spill_info
            payload["content_preview"] = spill_info.get("preview", "")
        else:
            payload["content"] = spill_info.get("content", content)
    else:
        payload["content"] = content
    return payload


@router.post("/{channel_id}/dump")
async def dump_channel_output(
    channel_id: int,
    body: SSHChannelDumpBody | None = None,
    user=Depends(get_current_user),
):
    db = await get_db()
    body = body or SSHChannelDumpBody()
    try:
        result = await dump_channel_buffer_to_file(
            db,
            user,
            channel_id,
            session_id=body.session_id,
            max_chars=body.max_chars or 2_000_000,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"success": True, **result}


@router.get("/{channel_id}/has-new")
async def has_new(channel_id: int, after_line: Optional[int] = Query(0), user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM ssh_channels WHERE id = ? AND user_id = ?",
        (channel_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="通道不存在")
    result = SSHChannelManager.get_instance().has_new(channel_id, after_line or 0)
    if result is None:
        return {"success": True, "has_new": False, "latest_line_no": 0}
    has_new_val, latest = result
    return {"success": True, "has_new": has_new_val, "latest_line_no": latest}


@router.delete("/{channel_id}")
async def close_channel(channel_id: int, user=Depends(get_current_user)):
    db = await get_db()
    ok = await close_channel_full(db, user, channel_id)
    if not ok:
        raise HTTPException(status_code=404, detail="通道不存在")
    return {"success": True}


@router.websocket("/{channel_id}/ws")
async def channel_websocket(ws: WebSocket, channel_id: int):
    await ws.accept()
    token = ws.query_params.get("token") or (ws.query_params.get("Authorization") or "").replace("Bearer ", "")
    user = await user_dict_for_websocket_from_token(token)
    if not user:
        await ws.send_json({"type": "error", "message": "未登录或 Token 无效"})
        await ws.close()
        return
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM ssh_channels WHERE id = ? AND user_id = ? AND status = 'open'",
        (channel_id, user["id"]),
    )
    if not rows:
        await ws.send_json({"type": "error", "message": "通道不存在或已关闭"})
        await ws.close()
        return
    manager = SSHChannelManager.get_instance()
    last_line = 0
    try:
        await ws.send_json({"type": "ready", "channel_id": channel_id})
        while True:
            still_open = await asyncio.to_thread(manager.has_channel, channel_id)
            if not still_open:
                await ws.send_json({"type": "closed", "message": "通道已关闭"})
                break
            has_new_result = await asyncio.to_thread(manager.has_new, channel_id, last_line)
            if has_new_result is None:
                await ws.send_json({"type": "closed", "message": "通道已关闭"})
                break
            has_new_val, latest = has_new_result
            if has_new_val and latest > last_line:
                result = await asyncio.to_thread(
                    manager.get_lines, channel_id, None, None, None, last_line
                )
                if result:
                    lines_data, _, latest_no = result
                    last_line = latest_no
                    await ws.send_json({
                        "type": "lines",
                        "lines": lines_data,
                        "oldest_line_no": result[1],
                        "latest_line_no": latest_no,
                    })
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=0.25)
            except asyncio.TimeoutError:
                msg = None
            if msg is not None:
                still_open = await asyncio.to_thread(manager.has_channel, channel_id)
                if not still_open:
                    await ws.send_json({"type": "closed", "message": "通道已关闭"})
                    break
                err = await asyncio.to_thread(manager.send, channel_id, msg)
                if err:
                    await ws.send_json({"type": "error", "message": err})
            await asyncio.sleep(0.2)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("channel ws error channel_id=%s: %s", channel_id, e)
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass
