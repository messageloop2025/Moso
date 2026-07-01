"""SSH Channel 业务逻辑：创建/查询/读写的共享实现（REST API、AI 工具、OpenClaw 集成共用）。"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import config
from api.hosts import _resolve_host_auth
from services.host_access import can_access_host_with_shares
from services.ssh_channel_manager import (
    DEFAULT_LINE_WIDTH,
    DEFAULT_MAX_LINES,
    SSHChannelManager,
)

logger = logging.getLogger("edgeops.ssh_channel_service")

SSH_CHANNEL_WEB_IDLE_CLOSE_SEC = int(getattr(config, "SSH_CHANNEL_WEB_IDLE_CLOSE_SEC", 1800))
SSH_CHANNEL_INTEGRATION_IDLE_CLOSE_SEC = int(
    getattr(config, "SSH_CHANNEL_INTEGRATION_IDLE_CLOSE_SEC", 3600)
)
SSH_CHANNEL_OUTPUT_SPILL_MIN_CHARS = int(
    getattr(config, "SSH_CHANNEL_OUTPUT_SPILL_MIN_CHARS", 8000)
)
SSH_CHANNEL_READ_PREVIEW_CHARS = int(getattr(config, "SSH_CHANNEL_READ_PREVIEW_CHARS", 4000))


async def mark_channel_closed_in_db(
    db,
    channel_id: int,
    user_id: int | None = None,
) -> bool:
    """将通道在库中标为 closed；返回是否更新到行。"""
    if user_id is not None:
        cur = await db.execute(
            "UPDATE ssh_channels SET status = 'closed', updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ? AND status != 'closed'",
            (channel_id, user_id),
        )
    else:
        cur = await db.execute(
            "UPDATE ssh_channels SET status = 'closed', updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status != 'closed'",
            (channel_id,),
        )
    await db.commit()
    return (cur.rowcount or 0) > 0


async def startup_close_stale_open_channels(db) -> int:
    """进程启动：内存通道已清空，将库中仍为 open 的记录标为 closed。"""
    cur = await db.execute(
        "UPDATE ssh_channels SET status = 'closed', updated_at = CURRENT_TIMESTAMP WHERE status = 'open'"
    )
    await db.commit()
    n = cur.rowcount or 0
    if n:
        logger.info("启动时已关闭 %d 条 stale open SSH 通道记录（进程重启）", n)
    return n


def memory_connected(channel_id: int) -> bool:
    return SSHChannelManager.get_instance().has_channel(channel_id)


CHANNEL_SESSION_STATUS_KEYS = (
    "connected",
    "exists",
    "pending",
    "session_state",
    "buffer_idle",
    "ready_for_input",
    "can_send",
    "can_send_command",
    "can_read_buffer",
    "last_line",
    "busy_reason",
    "waiting_password",
    "waiting_interactive",
    "disconnect_reason",
    "buffer_chars",
    "memory_connected",
    "db_status",
)


def get_channel_session_state(
    channel_id: int,
    *,
    db_status: str = "open",
    host_type: str | None = None,
) -> dict[str, Any]:
    """从 DB 状态 + 内存 TTY 缓冲推断 ssh_channel 通/断与闲/忙（与 Web 控制台 terminal_state 同源启发式）。"""
    from services.terminal_state import analyze_terminal_buffer, merge_connection_flags

    manager = SSHChannelManager.get_instance()
    mem = manager.has_channel(channel_id)
    db_st = (db_status or "").strip().lower()
    db_open = db_st == "open"
    connected = db_open and mem

    tail_text = manager.get_tail_text(channel_id, last_n=40) or ""
    pending = manager.get_pending_partial(channel_id) or ""
    buffer = tail_text
    if pending:
        buffer = f"{buffer}{pending}" if buffer else pending

    analysis = analyze_terminal_buffer(buffer, connected=connected, host_type=host_type)
    merged = merge_connection_flags(
        analysis,
        connected=connected,
        exists=db_st in ("open", "closed", "failed"),
        pending=False,
        can_read_buffer=connected,
        disconnect_reason=None if connected else ("channel_closed" if not db_open else "memory_disconnected"),
    )
    merged["buffer_chars"] = len(buffer)
    merged["memory_connected"] = mem
    merged["db_status"] = db_status
    return merged


def channel_session_status_payload(state: dict | None) -> dict[str, Any]:
    state = state or {}
    return {k: state.get(k) for k in CHANNEL_SESSION_STATUS_KEYS}


def enrich_channel_session_fields(channel: dict) -> dict:
    """为通道 dict 附加 connected / buffer_idle / session_state 等状态字段。"""
    cid = int(channel.get("id") or channel.get("channel_id") or 0)
    if not cid:
        return channel
    st = get_channel_session_state(
        cid,
        db_status=str(channel.get("status") or "closed"),
        host_type=(channel.get("host_type") or "").strip() or None,
    )
    channel.update(channel_session_status_payload(st))
    return channel


async def reconcile_channel_if_stale(db, user: dict, channel_id: int) -> bool:
    """DB 为 open 但内存无连接时标 closed。返回是否执行了 reconcile。"""
    rows = await db.execute_fetchall(
        "SELECT status FROM ssh_channels WHERE id = ? AND user_id = ?",
        (channel_id, user["id"]),
    )
    if not rows:
        return False
    if (rows[0][0] or "").strip().lower() != "open":
        return False
    if memory_connected(channel_id):
        return False
    await mark_channel_closed_in_db(db, channel_id, user["id"])
    return True


async def close_channel_full(db, user: dict, channel_id: int) -> bool:
    """关闭内存 TTY 并同步写库。"""
    rows = await db.execute_fetchall(
        "SELECT id FROM ssh_channels WHERE id = ? AND user_id = ?",
        (channel_id, user["id"]),
    )
    if not rows:
        return False
    SSHChannelManager.get_instance().close_channel(channel_id)
    await mark_channel_closed_in_db(db, channel_id, user["id"])
    return True


async def close_channels_by_owner(
    db,
    user: dict,
    *,
    owner_type: str,
    owner_id: str,
) -> dict[str, int]:
    """按 owner 批量关闭当前用户 open 通道（内存 + DB）。"""
    otype = (owner_type or "session").strip() or "session"
    oid = owner_id or ""
    rows = await db.execute_fetchall(
        """SELECT id FROM ssh_channels
           WHERE user_id = ? AND owner_type = ? AND owner_id = ? AND status = 'open'""",
        (user["id"], otype, oid),
    )
    manager = SSHChannelManager.get_instance()
    closed = 0
    for r in rows:
        cid = int(r[0])
        manager.close_channel(cid)
        if await mark_channel_closed_in_db(db, cid, user["id"]):
            closed += 1
    return {"closed_count": closed, "owner_type": otype, "owner_id": oid}


def resolve_channel_owner(
    *,
    owner_type: str | None,
    owner_id: str | None,
    terminal_scope_id: str | None,
    session_id: int | None,
    task_id: int | None,
) -> tuple[str, str]:
    """解析通道归属：task > 显式 owner > terminal_scope > session_id。"""
    if task_id is not None:
        oid = (owner_id or "").strip() or str(task_id)
        return "task", oid
    otype = (owner_type or "session").strip() or "session"
    oid = (owner_id or "").strip()
    if otype == "session":
        if oid:
            return otype, oid
        if terminal_scope_id:
            return otype, str(terminal_scope_id)
        if session_id is not None:
            return otype, str(session_id)
        return otype, ""
    return otype, oid


def default_idle_close_sec(
    *,
    explicit: int | None,
    session_id: int | None,
    terminal_scope_id: str | None,
    integration_context: bool = False,
) -> int:
    if explicit is not None:
        return max(30, min(int(explicit), 86400))
    if integration_context or (session_id is not None and not terminal_scope_id):
        return SSH_CHANNEL_INTEGRATION_IDLE_CLOSE_SEC
    return SSH_CHANNEL_WEB_IDLE_CLOSE_SEC


async def _fetch_host_prompt_snippet(db, user_id: int, host_id: int, max_chars: int = 400) -> str:
    try:
        rows = await db.execute_fetchall(
            "SELECT COALESCE(content, '') AS content FROM ai_host_prompts WHERE host_id = ? AND user_id = ?",
            (host_id, user_id),
        )
        if not rows:
            return ""
        text = (rows[0][0] or "").strip()
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1] + "…"
    except Exception:
        return ""


def _parse_aliases(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    except Exception:
        pass
    return []


async def enrich_channel_row(db, user: dict, row: dict) -> dict:
    """在通道记录上附加别名、用途、主机提示词摘要。"""
    out = dict(row)
    host_id = out.get("host_id")
    if host_id is not None:
        try:
            hrows = await db.execute_fetchall(
                "SELECT aliases, remark FROM hosts WHERE id = ?",
                (int(host_id),),
            )
            if hrows:
                hr = dict(hrows[0])
                out["host_aliases"] = _parse_aliases(hr.get("aliases"))
                out["host_remark"] = (hr.get("remark") or "").strip()
        except Exception:
            out.setdefault("host_aliases", [])
            out.setdefault("host_remark", "")
        out["host_prompt_snippet"] = await _fetch_host_prompt_snippet(
            db, int(user["id"]), int(host_id)
        )
    hi = out.get("host_info")
    if isinstance(hi, str) and hi.strip():
        try:
            out["host_info_parsed"] = json.loads(hi)
        except Exception:
            pass
    return out


async def create_channel_and_open(
    db,
    user: dict,
    *,
    host_id: int,
    owner_type: str,
    owner_id: str,
    input_timeout_sec: int | None = None,
    output_timeout_sec: int | None = None,
    idle_close_sec: int | None = None,
    session_id: int | None = None,
    terminal_scope_id: str | None = None,
    integration_context: bool = False,
) -> dict[str, Any]:
    """插入 ssh_channels 并建立真实 TTY 连接。成功返回 channel 元数据；失败抛 ValueError。"""
    host_rows = await db.execute_fetchall(
        "SELECT id, name, host, port, host_type, host_version, host_shell, credential_id, username, auth_type "
        "FROM hosts WHERE id = ?",
        (host_id,),
    )
    if not host_rows:
        raise ValueError("主机不存在")
    host = dict(host_rows[0])
    if not await can_access_host_with_shares(db, host, user):
        raise ValueError("无权在该主机上创建 SSH 通道")
    auth = await _resolve_host_auth(db, host)
    if not auth or not auth.get("username"):
        raise ValueError("主机未配置有效登录凭证")

    idle = default_idle_close_sec(
        explicit=idle_close_sec,
        session_id=session_id,
        terminal_scope_id=terminal_scope_id,
        integration_context=integration_context,
    )
    host_info = json.dumps(
        {
            "host_ip": host.get("host"),
            "host_name": host.get("name"),
            "host_type": host.get("host_type") or "未知",
            "host_version": host.get("host_version") or "未知",
            "host_shell": host.get("host_shell") or "",
        },
        ensure_ascii=False,
    )
    await db.execute(
        """INSERT INTO ssh_channels (owner_type, owner_id, user_id, host_id, input_timeout_sec, output_timeout_sec, idle_close_sec, status, host_info)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
        (
            owner_type,
            owner_id,
            user["id"],
            host_id,
            input_timeout_sec,
            output_timeout_sec,
            idle,
            host_info,
        ),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    cid = (await cur.fetchone())[0]

    manager = SSHChannelManager.get_instance()
    err = await manager.open_channel(
        channel_id=cid,
        host=host.get("host", ""),
        port=int(host.get("port") or 22),
        auth=auth,
        max_lines=DEFAULT_MAX_LINES,
        line_width=DEFAULT_LINE_WIDTH,
        idle_close_sec=idle,
        input_timeout_sec=input_timeout_sec,
        output_timeout_sec=output_timeout_sec,
    )
    if err:
        await db.execute(
            "UPDATE ssh_channels SET status = 'failed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (cid,),
        )
        await db.commit()
        raise ValueError(err)

    channel_row = {
        "id": cid,
        "channel_id": cid,
        "owner_type": owner_type,
        "owner_id": owner_id,
        "host_id": host_id,
        "idle_close_sec": idle,
        "status": "open",
        "host_info": host_info,
        "host_name": host.get("name"),
        "host_ip": host.get("host"),
        "host_type": host.get("host_type"),
        "host_version": host.get("host_version"),
        "host_shell": host.get("host_shell"),
    }
    enriched = await enrich_channel_row(db, user, channel_row)
    rng = manager.get_line_range(cid)
    if rng:
        enriched["oldest_line_no"], enriched["latest_line_no"] = rng
    else:
        enriched["oldest_line_no"] = enriched["latest_line_no"] = 0
    enrich_channel_session_fields(enriched)
    return enriched


async def list_channels_for_user(
    db,
    user: dict,
    *,
    owner_type: str | None = None,
    owner_id: str | None = None,
    all_open: bool = False,
) -> list[dict]:
    if all_open:
        rows = await db.execute_fetchall(
            """SELECT c.id, c.owner_type, c.owner_id, c.host_id, c.input_timeout_sec, c.output_timeout_sec,
                      c.idle_close_sec, c.status, c.host_info, c.created_at, c.updated_at,
                      h.name AS host_name, h.host AS host_ip, h.host_type, h.host_version, h.host_shell,
                      h.aliases AS host_aliases_raw, h.remark AS host_remark
               FROM ssh_channels c
               JOIN hosts h ON h.id = c.host_id
               WHERE c.user_id = ? AND c.status = 'open'
               ORDER BY c.created_at DESC""",
            (user["id"],),
        )
    else:
        otype = (owner_type or "session").strip() or "session"
        oid = owner_id or ""
        rows = await db.execute_fetchall(
            """SELECT c.id, c.owner_type, c.owner_id, c.host_id, c.input_timeout_sec, c.output_timeout_sec,
                      c.idle_close_sec, c.status, c.host_info, c.created_at, c.updated_at,
                      h.name AS host_name, h.host AS host_ip, h.host_type, h.host_version, h.host_shell,
                      h.aliases AS host_aliases_raw, h.remark AS host_remark
               FROM ssh_channels c
               JOIN hosts h ON h.id = c.host_id
               WHERE c.user_id = ? AND c.owner_type = ? AND c.owner_id = ? AND c.status = 'open'
               ORDER BY c.created_at DESC""",
            (user["id"], otype, oid),
        )
    out = []
    manager = SSHChannelManager.get_instance()
    for r in rows:
        d = dict(r)
        cid = int(d.get("id") or 0)
        # 列表接口只读：勿因当前 worker 内存中无连接就把 DB 标 closed（多 worker / 刚创建后立即 list 会误杀通道）
        d["memory_connected"] = (
            manager.has_channel(cid) if d.get("status") == "open" else False
        )
        if "host_aliases_raw" in d:
            d["host_aliases"] = _parse_aliases(d.pop("host_aliases_raw"))
            d["host_remark"] = (d.get("host_remark") or "").strip()
        d["host_prompt_snippet"] = await _fetch_host_prompt_snippet(
            db, int(user["id"]), int(d.get("host_id") or 0)
        )
        enrich_channel_session_fields(d)
        out.append(d)
    return out


async def get_channel_detail(db, user: dict, channel_id: int) -> dict | None:
    rows = await db.execute_fetchall(
        """SELECT c.id, c.owner_type, c.owner_id, c.host_id, c.status, c.host_info, c.created_at,
                  c.idle_close_sec, c.input_timeout_sec, c.output_timeout_sec,
                  h.name AS host_name, h.host AS host_ip, h.host_type, h.host_version, h.host_shell, h.port,
                  h.aliases AS host_aliases_raw, h.remark AS host_remark
           FROM ssh_channels c
           JOIN hosts h ON h.id = c.host_id
           WHERE c.id = ? AND c.user_id = ?""",
        (channel_id, user["id"]),
    )
    if not rows:
        return None
    d = dict(rows[0])
    cid = int(d.get("id") or 0)
    if d.get("status") == "open" and not memory_connected(cid):
        await mark_channel_closed_in_db(db, cid, user["id"])
        d["status"] = "closed"
    d["memory_connected"] = memory_connected(cid) if d.get("status") == "open" else False
    d["host_aliases"] = _parse_aliases(d.pop("host_aliases_raw", None))
    d["host_remark"] = (d.get("host_remark") or "").strip()
    d["host_prompt_snippet"] = await _fetch_host_prompt_snippet(
        db, int(user["id"]), int(d.get("host_id") or 0)
    )
    manager = SSHChannelManager.get_instance()
    if d.get("status") == "open":
        rng = manager.get_line_range(channel_id)
        if rng:
            d["oldest_line_no"], d["latest_line_no"] = rng
        else:
            d["oldest_line_no"] = d["latest_line_no"] = 0
    else:
        d["oldest_line_no"] = d["latest_line_no"] = 0
    enrich_channel_session_fields(d)
    return d


def format_lines_as_text(lines: list[dict]) -> str:
    parts = []
    for ln in lines:
        no = ln.get("line_no", ln.get("line_index", "?"))
        content = ln.get("content", "")
        wrap = " [soft]" if ln.get("is_soft_wrap") else ""
        parts.append(f"{no}{wrap}: {content}")
    return "\n".join(parts)


def maybe_spill_channel_text(
    user: dict,
    session_id: int | None,
    channel_id: int,
    full_text: str,
    *,
    tool_suffix: str = "read",
) -> dict[str, Any]:
    """输出过大时落盘，返回带 preview / spill 元数据的 dict。"""
    text = full_text or ""
    base = {
        "channel_id": channel_id,
        "char_length": len(text),
        "spilled": False,
    }
    if len(text) <= SSH_CHANNEL_OUTPUT_SPILL_MIN_CHARS:
        base["content"] = text
        return base
    from services.chat_tool_spill import write_chat_tool_spill_sync

    spill = write_chat_tool_spill_sync(
        user,
        session_id,
        f"ssh_channel_{tool_suffix}",
        str(channel_id),
        text,
    )
    if not spill:
        preview_cap = SSH_CHANNEL_READ_PREVIEW_CHARS
        base["content"] = text[:preview_cap] + (
            f"\n…（共 {len(text)} 字符，落盘失败，已截断预览）" if len(text) > preview_cap else ""
        )
        return base
    preview = text[:SSH_CHANNEL_READ_PREVIEW_CHARS]
    if len(text) > SSH_CHANNEL_READ_PREVIEW_CHARS:
        preview += f"\n…（共 {len(text)} 字符，完整内容已落盘）"
    base.update(
        {
            "spilled": True,
            "preview": preview,
            "spill_id": spill["spill_id"],
            "storage_subdir": spill["storage_subdir"],
            "relative_path": spill.get("relative_path"),
            "read_hint": (
                "完整输出已落盘；请用 read_chat_data(spill_id, date_subdir=storage_subdir) 分段读取。"
            ),
        }
    )
    return base


async def dump_channel_buffer_to_file(
    db,
    user: dict,
    channel_id: int,
    session_id: int | None = None,
    *,
    max_chars: int = 2_000_000,
) -> dict[str, Any]:
    """将通道当前缓冲全文导出到用户 chats/spill 目录。"""
    rows = await db.execute_fetchall(
        "SELECT id FROM ssh_channels WHERE id = ? AND user_id = ?",
        (channel_id, user["id"]),
    )
    if not rows:
        raise ValueError("通道不存在")
    manager = SSHChannelManager.get_instance()
    result = manager.get_content_length(channel_id, max_chars)
    if result is None:
        raise ValueError("通道已关闭或无缓冲")
    content, oldest, latest = result
    spill_result = maybe_spill_channel_text(
        user, session_id, channel_id, content, tool_suffix="dump"
    )
    spill_result["oldest_line_no"] = oldest
    spill_result["latest_line_no"] = latest
    if not spill_result.get("spilled"):
        spill_result["message"] = "缓冲未超过落盘阈值，已在 content 字段返回"
    else:
        spill_result["message"] = "通道缓冲已导出到文件"
    return spill_result


async def sync_pending_channel_closes_to_db(db) -> int:
    """将看门狗/读线程触发的内存关断同步到数据库。"""
    ids = SSHChannelManager.get_instance().drain_pending_db_close_ids()
    if not ids:
        return 0
    n = 0
    for cid in ids:
        if await mark_channel_closed_in_db(db, cid, user_id=None):
            n += 1
    return n


async def ssh_channel_db_sync_loop() -> None:
    """后台任务：定期把内存已关、库仍 open 的通道标为 closed。"""
    import asyncio
    from database import get_db

    while True:
        await asyncio.sleep(2)
        try:
            db = await get_db()
            await sync_pending_channel_closes_to_db(db)
        except Exception as e:
            logger.warning("ssh_channel DB 同步失败: %s", e)

