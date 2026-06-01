"""MCP 配置（环境变量 + config.py）。"""

from dataclasses import dataclass

import config
from services.edgeops_mcp.context import resolve_api_base_url


@dataclass(frozen=True)
class McpSettings:
    api_base_url: str
    host: str
    port: int


def load_settings() -> McpSettings:
    """启动 MCP 进程用；Token 可在每次工具调用 / HTTP 请求中传入。"""
    return McpSettings(
        api_base_url=resolve_api_base_url(),
        host=config.MCP_HOST,
        port=config.MCP_PORT,
    )
