"""用户 MCP 服务器配置读写（DB）。"""

from __future__ import annotations

import json
import re
from typing import Any

logger_name = "edgeops.user_mcp.registry"

VALID_TRANSPORTS = frozenset({"stdio", "sse", "streamable_http"})
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def normalize_server_name(name: str) -> str:
    raw = (name or "").strip().lower()
    raw = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-")
    if not raw or not _NAME_RE.match(raw):
        raise ValueError("名称须为小写字母开头，仅含 a-z、0-9、-、_，最长 64 字符")
    return raw


def _normalize_extra_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        key = str(k).strip()
        if key:
            out[key] = "" if v is None else str(v)
    return out


def build_config_json(
    *,
    command: str = "",
    args: list | None = None,
    env: dict | None = None,
    url: str = "",
    headers: dict | None = None,
) -> dict[str, Any]:
    return {
        "command": (command or "").strip(),
        "args": [str(a) for a in (args or [])],
        "env": _normalize_extra_dict(env),
        "url": (url or "").strip(),
        "headers": _normalize_extra_dict(headers),
    }


def parse_config_json(raw: str | dict | None) -> dict[str, Any]:
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw or "{}")
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    return build_config_json(
        command=data.get("command", ""),
        args=data.get("args") if isinstance(data.get("args"), list) else [],
        env=data.get("env"),
        url=data.get("url", ""),
        headers=data.get("headers"),
    )


_MASK_SUFFIX = "****"


def _mask_token_core(token: str) -> str:
    """保留常见 API Key 前缀，隐藏主体。"""
    token = (token or "").strip()
    if not token:
        return _MASK_SUFFIX
    if token.startswith("sk-"):
        return f"sk-{_MASK_SUFFIX}"
    if token.startswith("sk"):
        return f"sk{_MASK_SUFFIX}"
    if len(token) <= 4:
        return _MASK_SUFFIX
    return token[:4] + _MASK_SUFFIX


def mask_secret_value(value: str) -> str:
    """脱敏回显：如 Authorization 保留 Bearer sk-**** 形态。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    bearer = "Bearer "
    if raw.lower().startswith(bearer.lower()):
        token = raw[len(bearer) :].strip()
        return bearer + _mask_token_core(token)
    return _mask_token_core(raw)


def is_masked_secret_placeholder(value: str) -> bool:
    """更新 env/headers 时：占位符表示保留库内原值。"""
    val = (value or "").strip()
    if not val or val == "***":
        return True
    if val.endswith(_MASK_SUFFIX) or val.endswith("***"):
        return True
    return False


def _mask_map(values: dict[str, str]) -> dict[str, str]:
    return {k: mask_secret_value(v) for k, v in values.items()}


def public_server_row(row: dict) -> dict:
    cfg = parse_config_json(row.get("config_json"))
    env = cfg.get("env") or {}
    headers = cfg.get("headers") or {}
    return {
        "id": row["id"],
        "name": row.get("name") or "",
        "display_name": row.get("display_name") or row.get("name") or "",
        "transport": row.get("transport") or "stdio",
        "enabled": bool(row.get("enabled", 1)),
        "chat_enabled": bool(row.get("chat_enabled", 1)),
        "chat_scope_web": bool(row.get("chat_scope_web", 1)),
        "chat_scope_host": bool(row.get("chat_scope_host", 1)),
        "chat_scope_integration": bool(row.get("chat_scope_integration", 1)),
        "tool_count": int(row.get("tool_count") or 0),
        "last_test_ok": row.get("last_test_ok"),
        "last_test_at": row.get("last_test_at"),
        "last_error": row.get("last_error") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "config": {
            "command": cfg.get("command") or "",
            "args": cfg.get("args") or [],
            "url": cfg.get("url") or "",
            "env": _mask_map(env),
            "headers": _mask_map(headers),
            "env_keys": list(env.keys()),
            "header_keys": list(headers.keys()),
        },
    }


async def list_user_mcp_servers(db, user_id: int) -> list[dict]:
    rows = await db.execute_fetchall(
        """SELECT * FROM user_mcp_servers WHERE user_id=? ORDER BY id ASC""",
        (user_id,),
    )
    return [public_server_row(dict(r)) for r in rows]


async def get_user_mcp_server(db, user_id: int, server_id: int) -> dict | None:
    rows = await db.execute_fetchall(
        "SELECT * FROM user_mcp_servers WHERE id=? AND user_id=?",
        (server_id, user_id),
    )
    return public_server_row(dict(rows[0])) if rows else None


async def get_user_mcp_server_raw(db, user_id: int, server_id: int) -> dict | None:
    rows = await db.execute_fetchall(
        "SELECT * FROM user_mcp_servers WHERE id=? AND user_id=?",
        (server_id, user_id),
    )
    return dict(rows[0]) if rows else None


async def get_user_mcp_server_by_name(db, user_id: int, name: str) -> dict | None:
    slug = normalize_server_name(name)
    rows = await db.execute_fetchall(
        "SELECT * FROM user_mcp_servers WHERE user_id=? AND name=?",
        (user_id, slug),
    )
    return public_server_row(dict(rows[0])) if rows else None


async def get_user_mcp_server_raw_by_name(db, user_id: int, name: str) -> dict | None:
    slug = normalize_server_name(name)
    rows = await db.execute_fetchall(
        "SELECT * FROM user_mcp_servers WHERE user_id=? AND name=?",
        (user_id, slug),
    )
    return dict(rows[0]) if rows else None


async def create_user_mcp_server(
    db,
    user_id: int,
    *,
    name: str,
    display_name: str,
    transport: str,
    config: dict[str, Any],
    enabled: bool = True,
    chat_enabled: bool = True,
    chat_scope_web: bool = True,
    chat_scope_host: bool = True,
    chat_scope_integration: bool = True,
) -> dict:
    slug = normalize_server_name(name)
    transport_val = (transport or "stdio").strip().lower()
    if transport_val not in VALID_TRANSPORTS:
        raise ValueError(f"transport 须为 {', '.join(sorted(VALID_TRANSPORTS))}")
    cfg = parse_config_json(config)
    _validate_transport_config(transport_val, cfg)
    await db.execute(
        """INSERT INTO user_mcp_servers
           (user_id, name, display_name, transport, config_json, enabled, chat_enabled,
            chat_scope_web, chat_scope_host, chat_scope_integration)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            slug,
            (display_name or slug)[:120],
            transport_val,
            json.dumps(cfg, ensure_ascii=False),
            1 if enabled else 0,
            1 if chat_enabled else 0,
            1 if chat_scope_web else 0,
            1 if chat_scope_host else 0,
            1 if chat_scope_integration else 0,
        ),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    sid = int((await cur.fetchone())[0])
    row = await get_user_mcp_server(db, user_id, sid)
    return row or {}


async def update_user_mcp_server(
    db,
    user_id: int,
    server_id: int,
    *,
    name: str | None = None,
    display_name: str | None = None,
    transport: str | None = None,
    config_patch: dict[str, Any] | None = None,
    enabled: bool | None = None,
    chat_enabled: bool | None = None,
    chat_scope_web: bool | None = None,
    chat_scope_host: bool | None = None,
    chat_scope_integration: bool | None = None,
) -> dict:
    raw = await get_user_mcp_server_raw(db, user_id, server_id)
    if not raw:
        raise LookupError("MCP 服务器不存在")
    cfg = parse_config_json(raw.get("config_json"))
    if config_patch:
        if "command" in config_patch and config_patch["command"] is not None:
            cfg["command"] = str(config_patch["command"]).strip()
        if "args" in config_patch and config_patch["args"] is not None:
            cfg["args"] = [str(a) for a in config_patch["args"]]
        if "url" in config_patch and config_patch["url"] is not None:
            cfg["url"] = str(config_patch["url"]).strip()
        if "env" in config_patch and isinstance(config_patch["env"], dict):
            merged = dict(cfg.get("env") or {})
            for k, v in config_patch["env"].items():
                key = str(k).strip()
                if not key:
                    continue
                val = "" if v is None else str(v)
                if is_masked_secret_placeholder(val):
                    continue
                merged[key] = val
            cfg["env"] = merged
        if "headers" in config_patch and isinstance(config_patch["headers"], dict):
            merged = dict(cfg.get("headers") or {})
            for k, v in config_patch["headers"].items():
                key = str(k).strip()
                if not key:
                    continue
                val = "" if v is None else str(v)
                if is_masked_secret_placeholder(val):
                    continue
                merged[key] = val
            cfg["headers"] = merged
    transport_val = (transport or raw.get("transport") or "stdio").strip().lower()
    if transport_val not in VALID_TRANSPORTS:
        raise ValueError(f"transport 须为 {', '.join(sorted(VALID_TRANSPORTS))}")
    _validate_transport_config(transport_val, cfg)
    slug = normalize_server_name(name) if name is not None else raw["name"]
    disp = (display_name if display_name is not None else raw.get("display_name") or slug)[:120]
    await db.execute(
        """UPDATE user_mcp_servers SET
           name=?, display_name=?, transport=?, config_json=?,
           enabled=COALESCE(?, enabled), chat_enabled=COALESCE(?, chat_enabled),
           chat_scope_web=COALESCE(?, chat_scope_web),
           chat_scope_host=COALESCE(?, chat_scope_host),
           chat_scope_integration=COALESCE(?, chat_scope_integration),
           updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND user_id=?""",
        (
            slug,
            disp,
            transport_val,
            json.dumps(cfg, ensure_ascii=False),
            None if enabled is None else (1 if enabled else 0),
            None if chat_enabled is None else (1 if chat_enabled else 0),
            None if chat_scope_web is None else (1 if chat_scope_web else 0),
            None if chat_scope_host is None else (1 if chat_scope_host else 0),
            None if chat_scope_integration is None else (1 if chat_scope_integration else 0),
            server_id,
            user_id,
        ),
    )
    await db.commit()
    row = await get_user_mcp_server(db, user_id, server_id)
    return row or {}


async def delete_user_mcp_server(db, user_id: int, server_id: int) -> bool:
    cur = await db.execute(
        "DELETE FROM user_mcp_servers WHERE id=? AND user_id=?",
        (server_id, user_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def list_chat_enabled_servers_for_context(
    db,
    user_id: int,
    session_scope: str | None,
    session_host_id: int | None = None,
) -> list[dict]:
    scope = (session_scope or "default").strip().lower() or "default"
    if scope == "task":
        return []
    rows = await db.execute_fetchall(
        """SELECT * FROM user_mcp_servers
           WHERE user_id=? AND enabled=1 AND chat_enabled=1
           ORDER BY id ASC""",
        (user_id,),
    )
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        if scope in ("integration", "mcp_orchestrate", "mcp_runtime"):
            if not bool(row.get("chat_scope_integration", 1)):
                continue
        elif session_host_id:
            if not bool(row.get("chat_scope_host", 1)):
                continue
        else:
            if not bool(row.get("chat_scope_web", 1)):
                continue
        out.append(row)
    return out


async def list_chat_enabled_servers_raw(db, user_id: int) -> list[dict]:
    return await list_chat_enabled_servers_for_context(db, user_id, "default", None)


async def update_server_test_result(
    db,
    server_id: int,
    *,
    ok: bool,
    tool_count: int = 0,
    error: str = "",
) -> None:
    await db.execute(
        """UPDATE user_mcp_servers SET
           last_test_ok=?, last_test_at=CURRENT_TIMESTAMP, last_error=?,
           tool_count=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (1 if ok else 0, (error or "")[:2000], int(tool_count or 0), server_id),
    )
    await db.commit()


def _validate_transport_config(transport: str, cfg: dict[str, Any]) -> None:
    if transport == "stdio":
        if not (cfg.get("command") or "").strip():
            raise ValueError("stdio 传输须填写 command（如 npx、node、python）")
    else:
        if not (cfg.get("url") or "").strip():
            raise ValueError(f"{transport} 传输须填写 url")
        _validate_http_headers(cfg.get("headers") or {})


def _validate_http_headers(headers: dict[str, Any]) -> None:
    """HTTP 头 Key/Value 须为 ASCII，否则 httpx / MCP 客户端会 UnicodeEncodeError。"""
    for k, v in headers.items():
        ks = str(k)
        vs = "" if v is None else str(v)
        if any(ord(c) > 127 for c in ks):
            raise ValueError(f"HTTP 请求头名称须为 ASCII，不可含中文：{ks}")
        if any(ord(c) > 127 for c in vs):
            raise ValueError(
                f"HTTP 请求头「{ks}」的值须为 ASCII（如 Bearer Token），不可含中文等非 ASCII 字符"
            )
