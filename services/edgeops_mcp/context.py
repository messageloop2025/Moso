"""MCP 调用上下文：按请求 Token + 默认 integration session_id（多用户 / 多会话）。"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING

import config

if TYPE_CHECKING:
    from mcp.server.mcpserver import Context
    from starlette.requests import Request


@dataclass(frozen=True)
class McpCallContext:
    access_token: str
    integration_session_id: int | None = None


_call_context: ContextVar[McpCallContext | None] = ContextVar(
    "edgeops_mcp_call_context",
    default=None,
)


def bind_call_context(
    access_token: str,
    integration_session_id: int | None = None,
) -> Token[McpCallContext | None]:
    token = (access_token or "").strip()
    if not token:
        raise ValueError("access_token 不能为空")
    return _call_context.set(
        McpCallContext(
            access_token=token,
            integration_session_id=integration_session_id,
        )
    )


def reset_call_context(token: Token[McpCallContext | None]) -> None:
    _call_context.reset(token)


def get_bound_context() -> McpCallContext | None:
    return _call_context.get()


def _starlette_request(ctx: Context | None) -> Request | None:
    if ctx is None:
        return None
    try:
        req = ctx.request_context.request
    except (ValueError, AttributeError):
        return None
    return req if req is not None else None


def _parse_bearer(header_value: str | None) -> str | None:
    raw = (header_value or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("bearer "):
        return raw[7:].strip() or None
    return raw


def _parse_session_header(raw: str | None) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        sid = int(text)
    except ValueError:
        return None
    return sid if sid > 0 else None


def resolve_api_base_url() -> str:
    base = (
        os.getenv("EDGEOPS_API_BASE_URL")
        or os.getenv("EDGEOPS_BASE_URL")
        or config.MCP_API_BASE_URL
        or ""
    ).strip()
    if base:
        return base.rstrip("/")
    host = config.HOST if config.HOST not in ("0.0.0.0", "::") else "127.0.0.1"
    return f"http://{host}:{config.PORT}"


def resolve_access_token(ctx: Context | None = None) -> str:
    """Token 优先级：HTTP Authorization > X-EdgeOps-Access-Token > bind 上下文 > 环境变量。"""
    req = _starlette_request(ctx)
    if req is not None:
        bearer = _parse_bearer(req.headers.get("authorization"))
        if bearer:
            return bearer
        hdr = (req.headers.get("x-edgeops-access-token") or "").strip()
        if hdr:
            return hdr

    bound = get_bound_context()
    if bound and bound.access_token:
        return bound.access_token

    env_token = (
        os.getenv("EDGEOPS_ACCESS_TOKEN")
        or os.getenv("EDGEOPS_MCP_ACCESS_TOKEN")
        or config.MCP_ACCESS_TOKEN
        or ""
    ).strip()
    if env_token:
        return env_token

    raise ValueError(
        "缺少 毛竹 Bearer Token：请在 MCP 客户端配置 HTTP 头 Authorization: Bearer <JWT|eop_…>，"
        "或 stdio 子进程的 EDGEOPS_ACCESS_TOKEN 环境变量（与 claw-ops accessToken 相同）"
    )


def resolve_integration_session_id(
    explicit: int | None = None,
    ctx: Context | None = None,
) -> int | None:
    """session_id 优先级：工具参数 > X-EdgeOps-Session-Id > bind 上下文。"""
    if explicit is not None:
        return explicit

    req = _starlette_request(ctx)
    if req is not None:
        sid = _parse_session_header(req.headers.get("x-edgeops-session-id"))
        if sid is not None:
            return sid

    bound = get_bound_context()
    if bound and bound.integration_session_id is not None:
        return bound.integration_session_id

    return None
