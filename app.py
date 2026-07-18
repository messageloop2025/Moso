"""毛竹 应用入口 - 以 SSH 为操作方式的远程 AI 运维系统"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

import config
from database import init_db, connect_db, close_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("edgeops")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("毛竹 启动中...")
    await init_db()
    logger.info("数据库初始化完成")
    from database import get_db
    from services.ssh_channel_service import (
        ssh_channel_db_sync_loop,
        startup_close_stale_open_channels,
    )

    _db = await get_db()
    await startup_close_stale_open_channels(_db)
    _ssh_channel_sync_task = asyncio.create_task(ssh_channel_db_sync_loop())
    from services.scheduler import scheduler_loop
    _scheduler_task = asyncio.create_task(scheduler_loop())
    from services.edgeops_mcp.mount import mcp_http_lifespan

    async with mcp_http_lifespan():
        yield
    _ssh_channel_sync_task.cancel()
    try:
        await _ssh_channel_sync_task
    except asyncio.CancelledError:
        pass
    _scheduler_task.cancel()
    try:
        await _scheduler_task
    except asyncio.CancelledError:
        pass
    try:
        await close_db()
        logger.info("毛竹 已关闭")
    except Exception:
        pass


app = FastAPI(
    title=config.PRODUCT_NAME_ZH,
    description="以 SSH 为操作方式的远程 AI 运维系统",
    version=config.VERSION,
    lifespan=lifespan,
)

# MCP：/mcp 内部规范化 + 反代 HTTPS 头（须在路由注册前）
from services.edgeops_mcp.mount import install_mcp_http_middleware

install_mcp_http_middleware(app)


@app.exception_handler(Exception)
async def api_exception_handler(request, exc):
    """保证 /api 下任何未捕获异常都返回 JSON，避免前端 resp.json() 解析失败。"""
    if request.url.path.startswith("/api/"):
        logger.exception("API 未捕获异常: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"success": False, "detail": str(exc)},
        )
    raise exc


# ── 注册 API 路由 ──
from api.auth import router as auth_router
from api.users import router as users_router
from api.credentials import router as credentials_router
from api.service_credentials import router as service_credentials_router
from api.hosts import router as hosts_router
from api.host_groups import router as host_groups_router
from api.host_tags import router as host_tags_router
from api.maintenance_history import router as maintenance_router
from api.skills import router as skills_router
from api.terminal import router as terminal_router
from api.ai_agent import router as ai_router
from api.chat_attachments import router as chat_attachments_router
from api.ai_artifacts import router as ai_artifacts_router
from api.settings import router as settings_router, public_router as settings_public_router
from api.best_practices import router as best_practices_router
from api.filesystem import router as filesystem_router
from api.remote_fs import router as remote_fs_router
from api.batch import router as batch_router
from api.local_host import router as local_host_router
from api.dashboard import router as dashboard_router
from api.ssh_channel import router as ssh_channel_router
from api.triggered_tasks import router as triggered_tasks_router
from api.scheduled_tasks import router as scheduled_tasks_router
from api.api_tokens import router as api_tokens_router
from api.integration_ops import router as integration_ops_router
from api.integration_mcp import router as integration_mcp_router
from api.integration_claw_ops import router as integration_claw_ops_router
from api.user_mail import router as user_mail_router
from api.search_config import router as search_config_router
from api.user_mcp_servers import router as user_mcp_servers_router
from api.user_skills import router as user_skills_router
from api.org_skills import router as org_skills_router
from api.security_audit import router as security_audit_router
from api.aihelp import router as aihelp_router
from api.login_board import public_router as login_board_public_router, admin_router as login_board_admin_router
from api.feedback import user_router as feedback_user_router, admin_router as feedback_admin_router
app.include_router(auth_router)
app.include_router(api_tokens_router)
app.include_router(user_mail_router)
app.include_router(search_config_router)
app.include_router(user_mcp_servers_router)
app.include_router(user_skills_router)
app.include_router(org_skills_router)
app.include_router(security_audit_router)
app.include_router(aihelp_router)
app.include_router(login_board_public_router)
app.include_router(login_board_admin_router)
app.include_router(feedback_user_router)
app.include_router(feedback_admin_router)
app.include_router(integration_ops_router)
app.include_router(integration_mcp_router)
app.include_router(integration_claw_ops_router)
app.include_router(users_router)
app.include_router(credentials_router)
app.include_router(service_credentials_router)
app.include_router(hosts_router)
app.include_router(host_groups_router)
app.include_router(host_tags_router)
app.include_router(maintenance_router)
app.include_router(skills_router)
app.include_router(terminal_router)
app.include_router(ai_router)
app.include_router(chat_attachments_router)
app.include_router(ai_artifacts_router)
app.include_router(settings_router)
app.include_router(settings_public_router)
app.include_router(best_practices_router)
app.include_router(filesystem_router)
app.include_router(remote_fs_router)
app.include_router(batch_router)
app.include_router(local_host_router)
app.include_router(dashboard_router)
app.include_router(ssh_channel_router)
app.include_router(triggered_tasks_router)
app.include_router(scheduled_tasks_router)

# ── MCP（与主 Web 同端口，默认 /mcp）──
from services.edgeops_mcp.mount import mount_mcp_on_app

mount_mcp_on_app(app)

# ── 静态文件 ──
app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
# 产品介绍站点：无需登录即可访问，html=True 使 /intro/ 自动落到 intro/index.html
app.mount(
    "/intro",
    StaticFiles(directory=f"{config.WEB_DIR}/intro", html=True),
    name="intro",
)


@app.get("/api/version")
async def get_version():
    return {"success": True, "version": config.VERSION}


# ── 前端 SPA 路由 ──
# 注：/intro 与 /intro/ 由上方 StaticFiles(html=True) 挂载接管，无需在此处声明
@app.get("/")
@app.get("/login")
@app.get("/register")
@app.get("/forgot-password")
@app.get("/reset-password")
@app.get("/unlock")
@app.get("/dashboard")
@app.get("/hosts")
@app.get("/hosts/{host_id:int}")
@app.get("/credentials")
@app.get("/host-groups")
@app.get("/maintenance-history")
@app.get("/best-practices")
@app.get("/files")
@app.get("/batch")
@app.get("/triggered-tasks")
@app.get("/scheduled-tasks")
@app.get("/ai")
@app.get("/ai-mobile")
@app.get("/settings")
@app.get("/settings/model-config")
@app.get("/mcp-servers")
@app.get("/model-config")
@app.get("/skills")
@app.get("/users")
@app.get("/logs")
@app.get("/local")
@app.get("/feedback")
@app.get("/feedback/admin")
@app.get("/feedback/admin/login-board")
async def serve_spa():
    return FileResponse(f"{config.WEB_DIR}/index.html")


if __name__ == "__main__":
    import asyncio
    import sys
    import uvicorn
    workers = config.WORKERS
    # Windows 下 Uvicorn 多进程共享 socket 会触发 sock.listen() WSAEINVAL，只能单进程
    if sys.platform == "win32" and workers > 1:
        logger.info("您已设置 EDGEOPS_WORKERS=%s，但 Windows 下多进程不可用（Uvicorn 限制），已改为 1 个 worker", workers)
        workers = 1
    reload = workers <= 0  # 多 worker 时禁用 reload
    if workers > 1 and sys.platform != "win32":
        logger.warning(
            "EDGEOPS_WORKERS=%s：多进程下每个 worker 都会启动定时任务调度，可能导致同一 cron 重复执行；SQLite 亦不适合高并发写。无明确需求请使用单进程（EDGEOPS_WORKERS=0）。",
            workers,
        )
    # 多 worker 时由主进程先完成数据库初始化与迁移，再 fork 子进程，避免多进程同时跑迁移产生竞态
    if workers > 0:
        async def _init_then_close():
            await init_db()
            await close_db()
        logger.info("多进程模式：先执行数据库初始化与迁移…")
        asyncio.run(_init_then_close())
        logger.info("数据库初始化完成，即将启动 %s 个 worker", workers)
    reload_excludes = [
        "build/*",
        "build/**",
        "dist/*",
        "dist/**",
        "**/__pycache__/**",
        ".venv/**",
        "web/fs/**",
    ]
    uvicorn.run(
        "app:app",
        host=config.HOST,
        port=config.PORT,
        workers=workers if workers > 0 else 1,
        reload=reload,
        reload_excludes=reload_excludes if reload else None,
        forwarded_allow_ips=config.TRUSTED_PROXY_HOSTS if config.TRUST_PROXY_HEADERS else None,
    )
