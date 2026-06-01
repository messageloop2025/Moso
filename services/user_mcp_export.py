"""将 user_mcp_servers 导出为 Cursor / Claude Desktop 风格 mcp.json。"""

from __future__ import annotations

import json
from typing import Any

from services.user_mcp_registry import parse_config_json


def _row_to_mcp_entry(row: dict, cfg: dict[str, Any]) -> dict[str, Any]:
    transport = (row.get("transport") or "stdio").strip().lower()
    entry: dict[str, Any] = {}
    display = (row.get("display_name") or "").strip()
    name = row.get("name") or ""
    if display and display != name:
        entry["displayName"] = display[:120]
    if transport == "stdio":
        entry["command"] = (cfg.get("command") or "").strip()
        args = cfg.get("args") or []
        if args:
            entry["args"] = [str(a) for a in args]
        env = cfg.get("env") or {}
        if env:
            entry["env"] = {str(k): str(v) for k, v in env.items()}
    else:
        entry["url"] = (cfg.get("url") or "").strip()
        headers = cfg.get("headers") or {}
        if headers:
            entry["headers"] = {str(k): str(v) for k, v in headers.items()}
        env = cfg.get("env") or {}
        if env:
            entry["env"] = {str(k): str(v) for k, v in env.items()}
    return entry


def _edgeops_meta_row(row: dict) -> dict[str, Any]:
    return {
        "display_name": row.get("display_name") or row.get("name") or "",
        "transport": row.get("transport") or "stdio",
        "enabled": bool(row.get("enabled", 1)),
        "chat_enabled": bool(row.get("chat_enabled", 1)),
        "chat_scope_web": bool(row.get("chat_scope_web", 1)),
        "chat_scope_host": bool(row.get("chat_scope_host", 1)),
        "chat_scope_integration": bool(row.get("chat_scope_integration", 1)),
    }


async def export_user_mcp_servers(
    db,
    user_id: int,
    *,
    include_disabled: bool = True,
    include_edgeops_meta: bool = True,
) -> dict[str, Any]:
    rows = await db.execute_fetchall(
        """SELECT * FROM user_mcp_servers WHERE user_id=? ORDER BY id ASC""",
        (user_id,),
    )
    mcp_servers: dict[str, dict[str, Any]] = {}
    edgeops_servers: dict[str, dict[str, Any]] = {}
    for r in rows:
        row = dict(r)
        if not include_disabled and not bool(row.get("enabled", 1)):
            continue
        cfg = parse_config_json(row.get("config_json"))
        slug = row.get("name") or ""
        if not slug:
            continue
        mcp_servers[slug] = _row_to_mcp_entry(row, cfg)
        if include_edgeops_meta:
            edgeops_servers[slug] = _edgeops_meta_row(row)
    out: dict[str, Any] = {"mcpServers": mcp_servers}
    if include_edgeops_meta and edgeops_servers:
        out["_edgeops"] = {"version": 1, "servers": edgeops_servers}
    return out


def export_user_mcp_servers_json(
    db_result: dict[str, Any],
    *,
    indent: int = 2,
) -> str:
    return json.dumps(db_result, ensure_ascii=False, indent=indent) + "\n"
