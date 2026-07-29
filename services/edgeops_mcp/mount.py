"""将 毛竹 MCP 挂载到主 FastAPI（同端口 /mcp）或独立 HTTP 服务。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

import config
from mcp.server.transport_security import TransportSecuritySettings
from services.edgeops_mcp.server import mcp

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("edgeops.mcp.mount")


def _normalize_mount_path(path: str) -> str:
    p = (path or "/mcp").strip() or "/mcp"
    if not p.startswith("/"):
        p = f"/{p}"
    return p.rstrip("/") or "/mcp"


class McpPathNormalizeMiddleware:
    """将 ``/mcp`` 内部改写为 ``/mcp/``，避免 Starlette 307 重定向丢 POST body。

    配合 ``ProxyHeadersMiddleware`` 使用时，即便其它层产生重定向也会保留 HTTPS scheme。
    """

    def __init__(self, app: ASGIApp, *, mcp_path: str = "/mcp"):
        self.app = app
        self.mcp_path = _normalize_mount_path(mcp_path)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == self.mcp_path:
            scope = dict(scope)
            scope["path"] = f"{self.mcp_path}/"
        await self.app(scope, receive, send)


def install_mcp_http_middleware(app) -> None:
    """注册 MCP 路径规范化与反向代理头处理（HTTPS 不降级）。"""
    if not config.MCP_ENABLED:
        return
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app.add_middleware(McpPathNormalizeMiddleware, mcp_path=config.MCP_HTTP_PATH)
    if config.TRUST_PROXY_HEADERS:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=config.TRUSTED_PROXY_HOSTS)
        logger.info(
            "毛竹 已启用 ProxyHeaders（trusted_hosts=%r），MCP 路径规范化：%s -> %s/",
            config.TRUSTED_PROXY_HOSTS,
            _normalize_mount_path(config.MCP_HTTP_PATH),
            _normalize_mount_path(config.MCP_HTTP_PATH),
        )


def _ensure_session_manager() -> None:
    """懒初始化 StreamableHTTP session manager（MCPServer 要求先调 streamable_http_app）。"""
    mcp.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


def get_mcp_http_handler_app() -> Starlette:
    """返回 MCP Streamable HTTP 子应用（内部路由为 `/`，供 Mount 使用）。"""
    _ensure_session_manager()
    return mcp.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


def build_standalone_http_app(*, mount_path: str | None = None) -> Starlette:
    """独立 HTTP 进程用：在 mount_path 下暴露 MCP（默认 /mcp）。"""
    from starlette.applications import Starlette
    from starlette.routing import Mount

    path = _normalize_mount_path(mount_path or config.MCP_HTTP_PATH)
    inner = get_mcp_http_handler_app()
    if path == "/":
        return inner
    return Starlette(routes=[Mount(path, app=inner)])


@asynccontextmanager
async def standalone_http_lifespan() -> AsyncIterator[None]:
    """独立 `python -m services.edgeops_mcp --http` 进程的 lifespan。"""
    _ensure_session_manager()
    async with mcp.session_manager.run():
        yield


def build_standalone_uvicorn_app(*, mount_path: str | None = None) -> Starlette:
    """带 lifespan 的独立 HTTP Starlette 应用。"""
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    path = _normalize_mount_path(mount_path or config.MCP_HTTP_PATH)
    inner = get_mcp_http_handler_app()
    routes = inner.routes if path == "/" else [Mount(path, app=inner)]
    app = Starlette(routes=routes, lifespan=standalone_http_lifespan)
    app.add_middleware(McpPathNormalizeMiddleware, mcp_path=path)
    if config.TRUST_PROXY_HEADERS:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=config.TRUSTED_PROXY_HOSTS)
    return app


@asynccontextmanager
async def mcp_http_lifespan() -> AsyncIterator[None]:
    """主 FastAPI lifespan 内启动/停止 MCP session manager。"""
    if not config.MCP_ENABLED:
        yield
        return
    _ensure_session_manager()
    host = config.HOST if config.HOST not in ("0.0.0.0", "::") else "127.0.0.1"
    logger.info(
        "毛竹 MCP 已挂载（同进程）http://%s:%s%s",
        host,
        config.PORT,
        _normalize_mount_path(config.MCP_HTTP_PATH),
    )
    async with mcp.session_manager.run():
        yield


def mount_mcp_on_app(app) -> None:
    """将 MCP 挂到主 FastAPI 应用（EDGEOPS_MCP_ENABLED 默认 true）。"""
    if not config.MCP_ENABLED:
        return
    path = _normalize_mount_path(config.MCP_HTTP_PATH)
    app.mount(path, get_mcp_http_handler_app())
    logger.info("毛竹 MCP mount 已注册: %s", path)
