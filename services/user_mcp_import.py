"""解析 Cursor / Claude Desktop 风格 mcp.json 并批量导入 user_mcp_servers。"""

from __future__ import annotations

import json
import re
from typing import Any

from services.user_mcp_registry import (
    create_user_mcp_server,
    get_user_mcp_server_raw,
    normalize_server_name,
    update_user_mcp_server,
    VALID_TRANSPORTS,
)


def _guess_transport(entry: dict) -> str:
    if (entry.get("command") or "").strip():
        return "stdio"
    url = (entry.get("url") or entry.get("serverUrl") or "").strip()
    if not url:
        raise ValueError("条目须含 command 或 url")
    low = url.lower()
    if low.endswith("/sse") or "sse" in low:
        return "sse"
    return "streamable_http"


def _entry_to_fields(name: str, entry: dict) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError(f"{name}: 配置须为对象")
    transport = _guess_transport(entry)
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}
    if transport == "stdio":
        return {
            "name": name,
            "display_name": str(entry.get("displayName") or entry.get("title") or name)[:120],
            "transport": transport,
            "command": str(entry.get("command") or "").strip(),
            "args": [str(a) for a in (entry.get("args") or [])],
            "env": {str(k): str(v) for k, v in env.items()},
            "url": "",
            "headers": {},
        }
    url = (entry.get("url") or entry.get("serverUrl") or "").strip()
    return {
        "name": name,
        "display_name": str(entry.get("displayName") or entry.get("title") or name)[:120],
        "transport": transport,
        "command": "",
        "args": [],
        "env": {str(k): str(v) for k, v in env.items()},
        "url": url,
        "headers": {str(k): str(v) for k, v in headers.items()},
    }


def parse_mcp_servers_blob(raw: str | dict) -> dict[str, dict]:
    """支持 {mcpServers:{...}}、{servers:{...}} 或直接 {name: config}。"""
    if isinstance(raw, str):
        data = json.loads(raw)
    else:
        data = raw
    if not isinstance(data, dict):
        raise ValueError("JSON 根须为对象")
    servers = data.get("mcpServers") or data.get("servers") or data
    if not isinstance(servers, dict):
        raise ValueError("未找到 mcpServers 对象")
    out: dict[str, dict] = {}
    for key, entry in servers.items():
        slug = normalize_server_name(str(key))
        out[slug] = _entry_to_fields(slug, entry)
    if not out:
        raise ValueError("mcpServers 为空")
    return out


async def import_user_mcp_servers(
    db,
    user_id: int,
    raw: str | dict,
    *,
    overwrite: bool = False,
    chat_enabled: bool = True,
    chat_scope_web: bool = True,
    chat_scope_host: bool = True,
    chat_scope_integration: bool = True,
) -> dict:
    parsed = parse_mcp_servers_blob(raw)
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []
    for slug, fields in parsed.items():
        existing = await db.execute_fetchall(
            "SELECT id FROM user_mcp_servers WHERE user_id=? AND name=?",
            (user_id, slug),
        )
        config = {
            "command": fields.get("command", ""),
            "args": fields.get("args") or [],
            "url": fields.get("url", ""),
            "env": fields.get("env") or {},
            "headers": fields.get("headers") or {},
        }
        try:
            if existing:
                if not overwrite:
                    skipped.append(slug)
                    continue
                sid = int(dict(existing[0])["id"])
                await update_user_mcp_server(
                    db,
                    user_id,
                    sid,
                    display_name=fields.get("display_name"),
                    transport=fields.get("transport"),
                    config_patch=config,
                    enabled=True,
                    chat_enabled=chat_enabled,
                    chat_scope_web=chat_scope_web,
                    chat_scope_host=chat_scope_host,
                    chat_scope_integration=chat_scope_integration,
                )
                updated.append(slug)
            else:
                await create_user_mcp_server(
                    db,
                    user_id,
                    name=slug,
                    display_name=fields.get("display_name") or slug,
                    transport=fields.get("transport") or "stdio",
                    config=config,
                    enabled=True,
                    chat_enabled=chat_enabled,
                    chat_scope_web=chat_scope_web,
                    chat_scope_host=chat_scope_host,
                    chat_scope_integration=chat_scope_integration,
                )
                created.append(slug)
        except Exception as e:
            errors.append({"name": slug, "error": str(e)[:500]})
    return {
        "success": len(errors) == 0,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "total": len(parsed),
    }
