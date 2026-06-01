"""毛竹 MCP 启动辅助（同进程挂载，不再另启子进程）。"""

from __future__ import annotations

import logging

import config

logger = logging.getLogger("edgeops.mcp.launcher")


def start_mcp_subprocess():
    """兼容旧调用：MCP 已随主 FastAPI 挂载在 MCP_HTTP_PATH，此处仅打日志。"""
    if not config.MCP_ENABLED:
        return None
    host = config.HOST if config.HOST not in ("0.0.0.0", "::") else "127.0.0.1"
    logger.info(
        "毛竹 MCP 将在主进程挂载（无需子进程）http://%s:%s%s",
        host,
        config.PORT,
        config.MCP_HTTP_PATH,
    )
    return None


def stop_mcp_subprocess() -> None:
    """兼容旧调用：session manager 由 app lifespan 管理。"""
    return None
