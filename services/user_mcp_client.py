"""毛竹 作为 MCP 客户端：连接用户配置的 MCP 服务器并在 AI 聊天中暴露工具。"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from database import get_db
from services.user_mcp_registry import (
    get_user_mcp_server_raw,
    get_user_mcp_server_raw_by_name,
    list_chat_enabled_servers_for_context,
    parse_config_json,
    update_server_test_result,
)

logger = logging.getLogger("edgeops.user_mcp.client")

TOOL_PREFIX = "user_mcp_"
_TOOL_NAME_RE = re.compile(r"^user_mcp_(\d+)__(.+)$")

_TOOLS_CACHE: dict[tuple[int, int], tuple[float, str, list[dict]]] = {}
_CACHE_TTL_SEC = 300.0

# 会话内 MCP 熔断：某 server 连接/调用失败后，本会话不再探测，直到用户要求重试
_SESSION_MCP_SKIP: dict[int, set[int]] = {}
_MCP_LIST_CONNECT_TIMEOUT_SEC = 5.0
_MCP_RETRY_USER_RE = re.compile(
    r"(mcp\s*恢复|恢复\s*mcp|重试\s*mcp|mcp\s*重试|重新\s*连接\s*mcp|mcp\s*重连|"
    r"retry\s*mcp|mcp\s*retry|reconnect\s*mcp)",
    re.IGNORECASE,
)


def user_requests_mcp_retry(message: str | None) -> bool:
    return bool(_MCP_RETRY_USER_RE.search((message or "").strip()))


def clear_session_mcp_skip(session_id: int | None, *, server_id: int | None = None) -> None:
    if session_id is None:
        return
    sid = int(session_id)
    if server_id is None:
        _SESSION_MCP_SKIP.pop(sid, None)
        return
    skipped = _SESSION_MCP_SKIP.get(sid)
    if not skipped:
        return
    skipped.discard(int(server_id))
    if not skipped:
        _SESSION_MCP_SKIP.pop(sid, None)


def mark_session_mcp_skip(session_id: int | None, server_id: int, *, reason: str = "") -> None:
    if session_id is None:
        return
    sid = int(session_id)
    bucket = _SESSION_MCP_SKIP.setdefault(sid, set())
    bucket.add(int(server_id))
    logger.info(
        "MCP session circuit open session_id=%s server_id=%s reason=%s",
        sid,
        server_id,
        (reason or "")[:200],
    )


def is_session_mcp_skipped(session_id: int | None, server_id: int) -> bool:
    if session_id is None:
        return False
    return int(server_id) in _SESSION_MCP_SKIP.get(int(session_id), set())


def _server_known_failed(row: dict) -> bool:
    """配置页「连接失败」后，聊天热路径不再反复连（除非用户要求重试）。"""
    ok = row.get("last_test_ok")
    if ok is None:
        return False
    try:
        return int(ok) == 0
    except (TypeError, ValueError):
        return False


def _format_mcp_error(exc: BaseException) -> str:
    """展开 TaskGroup / ExceptionGroup，避免 UI 只显示泛化错误。"""
    nested = getattr(exc, "exceptions", None)
    if nested:
        parts = [_format_mcp_error(e) for e in nested]
        joined = "; ".join(p for p in parts if p)
        if joined:
            return joined[:2000]
    msg = str(exc).strip()
    generic = "unhandled errors in a TaskGroup" in msg or "Exception Group" in msg
    cause = exc.__cause__ or exc.__context__
    if cause and (generic or not msg):
        inner = _format_mcp_error(cause) if isinstance(cause, BaseException) else str(cause)
        if inner:
            return inner[:2000]
    return (msg or repr(exc))[:2000]


def _cache_version_key(row: dict) -> str:
    return f"{row.get('updated_at') or ''}|{row.get('config_json') or ''}"


def make_prefixed_tool_name(server_id: int, original_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", (original_name or "").strip()) or "tool"
    return f"{TOOL_PREFIX}{server_id}__{safe}"


def parse_prefixed_tool_name(name: str) -> tuple[int, str] | None:
    m = _TOOL_NAME_RE.match((name or "").strip())
    if not m:
        return None
    return int(m.group(1)), m.group(2)


def invalidate_user_mcp_cache(user_id: int | None = None, server_id: int | None = None) -> None:
    if user_id is None and server_id is None:
        _TOOLS_CACHE.clear()
        return
    drop = []
    for key in _TOOLS_CACHE:
        uid, sid = key
        if user_id is not None and uid != user_id:
            continue
        if server_id is not None and sid != server_id:
            continue
        drop.append(key)
    for key in drop:
        _TOOLS_CACHE.pop(key, None)


@asynccontextmanager
async def open_mcp_session(
    row: dict,
    *,
    connect_timeout: float | None = None,
) -> AsyncIterator[ClientSession]:
    transport = (row.get("transport") or "stdio").strip().lower()
    cfg = parse_config_json(row.get("config_json"))
    timeout = float(connect_timeout) if connect_timeout is not None else 30.0
    timeout = max(1.0, min(120.0, timeout))
    if transport == "stdio":
        params = StdioServerParameters(
            command=cfg["command"],
            args=list(cfg.get("args") or []),
            env={k: v for k, v in (cfg.get("env") or {}).items() if v is not None},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif transport == "sse":
        headers = {k: v for k, v in (cfg.get("headers") or {}).items() if v}
        async with sse_client(
            cfg["url"], headers=headers or None, timeout=timeout, sse_read_timeout=120
        ) as (
            read,
            write,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif transport == "streamable_http":
        import httpx2

        headers = {k: v for k, v in (cfg.get("headers") or {}).items() if v}
        http_client = httpx2.AsyncClient(
            headers=headers or None,
            timeout=httpx2.Timeout(timeout, read=120.0),
        )
        async with streamable_http_client(
            cfg["url"], http_client=http_client,
        ) as (
            read,
            write,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        raise ValueError(f"不支持的 transport: {transport}")


def _mcp_tool_to_openai(row: dict, tool) -> dict:
    server_id = int(row["id"])
    display = row.get("display_name") or row.get("name") or f"server-{server_id}"
    original = getattr(tool, "name", None) or tool.get("name")  # type: ignore[union-attr]
    description = getattr(tool, "description", None) or tool.get("description") or ""  # type: ignore[union-attr]
    schema = getattr(tool, "inputSchema", None) or tool.get("inputSchema")  # type: ignore[union-attr]
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    prefixed = make_prefixed_tool_name(server_id, str(original))
    return {
        "type": "function",
        "function": {
            "name": prefixed,
            "description": f"[MCP · {display}] {description}".strip()[:4000],
            "parameters": schema,
            "_mcp_server_id": server_id,
            "_mcp_tool_name": str(original),
        },
    }


async def _list_server_openai_tools(
    row: dict,
    *,
    use_cache: bool = True,
    connect_timeout: float | None = None,
) -> list[dict]:
    user_id = int(row["user_id"])
    server_id = int(row["id"])
    cache_key = (user_id, server_id)
    now = time.time()
    version = _cache_version_key(row)
    if use_cache:
        cached = _TOOLS_CACHE.get(cache_key)
        if cached and cached[0] > now and cached[1] == version:
            return list(cached[2])
    async with open_mcp_session(row, connect_timeout=connect_timeout) as session:
        result = await session.list_tools()
        tools = getattr(result, "tools", None) or []
    openai_tools = [_mcp_tool_to_openai(row, t) for t in tools]
    _TOOLS_CACHE[cache_key] = (now + _CACHE_TTL_SEC, version, openai_tools)
    return openai_tools


async def test_user_mcp_server(db, user_id: int, row: dict) -> dict:
    server_id = int(row["id"])
    try:
        async with open_mcp_session(row) as session:
            result = await session.list_tools()
            tools = getattr(result, "tools", None) or []
        names = [getattr(t, "name", None) or t.get("name") for t in tools]  # type: ignore[union-attr]
        await update_server_test_result(db, server_id, ok=True, tool_count=len(names), error="")
        invalidate_user_mcp_cache(user_id=user_id, server_id=server_id)
        for sid in list(_SESSION_MCP_SKIP):
            clear_session_mcp_skip(sid, server_id=server_id)
        return {
            "success": True,
            "tool_count": len(names),
            "tools": names[:50],
            "message": f"连接成功，发现 {len(names)} 个工具",
        }
    except Exception as e:
        msg = _format_mcp_error(e)
        logger.warning("MCP test failed user=%s server=%s: %s", user_id, server_id, msg)
        await update_server_test_result(db, server_id, ok=False, tool_count=0, error=msg)
        invalidate_user_mcp_cache(user_id=user_id, server_id=server_id)
        return {"success": False, "error": msg}


async def refresh_user_mcp_server_tools(
    db,
    user_id: int,
    *,
    server_id: int | None = None,
    name: str | None = None,
) -> dict:
    if server_id is not None:
        row = await get_user_mcp_server_raw(db, user_id, int(server_id))
    elif name:
        row = await get_user_mcp_server_raw_by_name(db, user_id, name)
    else:
        invalidate_user_mcp_cache(user_id=user_id)
        return {"success": True, "message": "已清除全部 MCP 工具缓存"}
    if not row:
        return {"success": False, "error": "MCP 服务器不存在"}
    invalidate_user_mcp_cache(user_id=user_id, server_id=int(row["id"]))
    try:
        tools = await _list_server_openai_tools(row, use_cache=False)
        names = [t.get("function", {}).get("name") for t in tools]
        await update_server_test_result(db, int(row["id"]), ok=True, tool_count=len(names), error="")
        return {
            "success": True,
            "server_id": int(row["id"]),
            "server_name": row.get("name"),
            "tool_count": len(names),
            "tools": names[:50],
        }
    except Exception as e:
        msg = _format_mcp_error(e)
        await update_server_test_result(db, int(row["id"]), ok=False, tool_count=0, error=msg)
        return {"success": False, "error": msg, "server_id": int(row["id"]), "server_name": row.get("name")}


async def load_user_mcp_tools_for_llm(
    user_id: int,
    session_scope: str | None = None,
    session_host_id: int | None = None,
    *,
    session_id: int | None = None,
    force_retry: bool = False,
) -> list[dict]:
    if force_retry and session_id is not None:
        clear_session_mcp_skip(session_id)
        invalidate_user_mcp_cache(user_id=user_id)

    db = await get_db()
    servers = await list_chat_enabled_servers_for_context(
        db, user_id, session_scope, session_host_id
    )
    out: list[dict] = []
    for row in servers:
        server_id = int(row.get("id") or 0)
        if not server_id:
            continue
        if not force_retry and is_session_mcp_skipped(session_id, server_id):
            continue
        if not force_retry and _server_known_failed(row):
            # 配置页已标记连接失败：本会话直接跳过，避免每次聊天卡在 TCP 超时
            mark_session_mcp_skip(
                session_id,
                server_id,
                reason=(row.get("last_error") or "last_test_ok=0")[:200],
            )
            logger.info(
                "Skip known-failed MCP server id=%s name=%s session_id=%s",
                server_id,
                row.get("name"),
                session_id,
            )
            continue
        try:
            tools = await _list_server_openai_tools(
                row,
                use_cache=not force_retry,
                connect_timeout=_MCP_LIST_CONNECT_TIMEOUT_SEC,
            )
            for t in tools:
                fn = dict(t.get("function") or {})
                fn.pop("_mcp_server_id", None)
                fn.pop("_mcp_tool_name", None)
                out.append({"type": "function", "function": fn})
        except Exception as e:
            msg = _format_mcp_error(e)
            logger.warning(
                "Skip MCP server id=%s for user=%s session_id=%s: %s",
                server_id,
                user_id,
                session_id,
                msg,
            )
            mark_session_mcp_skip(session_id, server_id, reason=msg)
            try:
                await update_server_test_result(
                    db, server_id, ok=False, tool_count=0, error=msg
                )
            except Exception:
                pass
    return out


async def invoke_user_mcp_tool(
    user: dict,
    prefixed_name: str,
    arguments: dict | None,
    *,
    session_id: int | None = None,
) -> str:
    parsed = parse_prefixed_tool_name(prefixed_name)
    if not parsed:
        return json.dumps({"success": False, "error": f"非法 MCP 工具名: {prefixed_name}"}, ensure_ascii=False)
    server_id, original_tool = parsed
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM user_mcp_servers WHERE id=? AND user_id=? AND enabled=1",
        (server_id, user["id"]),
    )
    if not rows:
        return json.dumps({"success": False, "error": "MCP 服务器不存在或未启用"}, ensure_ascii=False)
    row = dict(rows[0])
    args = dict(arguments or {})
    if is_session_mcp_skipped(session_id, server_id):
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"MCP 服务器「{row.get('name') or server_id}」本会话已熔断（此前连接失败）。"
                    "若已恢复，请发送「重试 MCP」或「MCP 恢复」后再用。"
                ),
                "tool": original_tool,
                "server_id": server_id,
                "circuit_open": True,
            },
            ensure_ascii=False,
        )
    try:
        async with open_mcp_session(row) as session:
            result = await session.call_tool(original_tool, args)
        content_parts = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text is not None:
                content_parts.append(str(text))
            else:
                content_parts.append(str(block))
        payload: dict[str, Any] = {
            "success": not bool(getattr(result, "isError", False)),
            "tool": original_tool,
            "server_id": server_id,
            "server_name": row.get("name"),
            "content": "\n".join(content_parts).strip(),
        }
        if getattr(result, "structuredContent", None) is not None:
            payload["structured"] = result.structuredContent
        from services.mcp_result_fetch import enrich_mcp_tool_payload

        payload = await enrich_mcp_tool_payload(payload, user, session_id=session_id)
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception as e:
        msg = _format_mcp_error(e)
        logger.warning("MCP call_tool failed %s: %s", prefixed_name, msg)
        mark_session_mcp_skip(session_id, server_id, reason=msg)
        try:
            await update_server_test_result(db, server_id, ok=False, tool_count=0, error=msg)
        except Exception:
            pass
        return json.dumps(
            {
                "success": False,
                "error": msg,
                "tool": original_tool,
                "circuit_open": True,
                "hint": "本会话已暂时禁用该 MCP；恢复后请发送「重试 MCP」。",
            },
            ensure_ascii=False,
        )


async def resolve_chat_tools(
    base_tools: list,
    session_scope: str | None,
    user: dict,
    session_host_id: int | None = None,
    *,
    session_id: int | None = None,
    user_message: str | None = None,
) -> list:
    from services.credential_vault import filter_credential_vault_tools

    base_tools = await filter_credential_vault_tools(base_tools)
    scope_val = (session_scope or "default").strip().lower() or "default"
    if scope_val == "task":
        return base_tools
    force_retry = user_requests_mcp_retry(user_message)
    try:
        extra = await load_user_mcp_tools_for_llm(
            int(user["id"]),
            session_scope,
            session_host_id,
            session_id=session_id,
            force_retry=force_retry,
        )
    except Exception as e:
        logger.warning("load_user_mcp_tools_for_llm failed user=%s: %s", user.get("id"), e)
        extra = []
    if not extra:
        return base_tools
    return list(base_tools) + extra
