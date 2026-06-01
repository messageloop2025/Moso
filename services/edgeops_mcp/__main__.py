"""毛竹 MCP 独立入口（stdio 或可选 --http 调试进程）。"""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(description="毛竹 MCP sub-service")
    parser.add_argument("--http", action="store_true", help="Streamable HTTP transport")
    parser.add_argument("--host", default=None, help="HTTP bind host")
    parser.add_argument("--port", type=int, default=None, help="HTTP bind port")
    args = parser.parse_args(argv)

    import config
    from services.edgeops_mcp.mount import build_standalone_uvicorn_app
    from services.edgeops_mcp.server import mcp
    from services.edgeops_mcp.settings import load_settings

    settings = load_settings()
    host = args.host or settings.host
    port = args.port or settings.port

    log = logging.getLogger("edgeops.mcp")
    log.info(
        "毛竹 MCP 启动 transport=%s api=%s (per-request Bearer supported)",
        "streamable-http" if args.http else "stdio",
        settings.api_base_url,
    )

    if args.http:
        import uvicorn

        app = build_standalone_uvicorn_app()
        mount_path = config.MCP_HTTP_PATH.rstrip("/") or "/mcp"
        log.info("HTTP 监听 http://%s:%s%s", host, port, mount_path)
        log.info("多用户：请求头 Authorization: Bearer <token>；多会话：X-EdgeOps-Session-Id")
        log.info("提示：随 毛竹 主进程启动时无需本命令，主服务同路径即可")
        uvicorn.run(app, host=host, port=port, log_level="info")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
