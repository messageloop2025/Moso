"""毛竹（Moso）配置文件 / Moso configuration (env-driven defaults)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 产品显示名（仓库目录/文件名仍为 EdgeOps / edgeops_*）
PRODUCT_NAME_ZH = "毛竹"
PRODUCT_NAME_EN = "Moso"
PRODUCT_DISPLAY = f"{PRODUCT_NAME_ZH}（{PRODUCT_NAME_EN}）"  # AI 提示词中的产品指称

VERSION = os.getenv("EDGEOPS_VERSION", "1.3.8-sp5")

# 数据库 / Database
DATABASE_PATH = os.getenv("EDGEOPS_DB", str(BASE_DIR / "edgeops.db"))
# SQLite 多人并发：WAL 模式允许多连接并发读、写与读并行；锁冲突时等待（毫秒），减轻 SQLITE_BUSY
# SQLite concurrency: WAL allows concurrent reads + one writer; busy timeout (ms) reduces SQLITE_BUSY errors.
SQLITE_BUSY_TIMEOUT_MS = max(0, int(os.getenv("EDGEOPS_SQLITE_BUSY_TIMEOUT_MS", "5000")))
SQLITE_WAL = os.getenv("EDGEOPS_SQLITE_WAL", "true").strip().lower() in ("1", "true", "yes")

# JWT / Authentication tokens
SECRET_KEY = os.getenv("EDGEOPS_SECRET", "edgeops-dev-secret-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.getenv("EDGEOPS_JWT_EXPIRE_MINUTES", str(60 * 24 * 7)))  # 默认 7 天
JWT_REFRESH_GRACE_MINUTES = int(os.getenv("EDGEOPS_JWT_REFRESH_GRACE_MINUTES", str(60 * 24)))  # 过期后仍可刷新窗口（默认 24h）
JWT_SLIDING_REFRESH_INTERVAL_MINUTES = int(os.getenv("EDGEOPS_JWT_SLIDING_REFRESH_INTERVAL_MINUTES", "30"))  # 前端定时续期间隔

# 服务器 / HTTP server
HOST = os.getenv("EDGEOPS_HOST", "0.0.0.0")
PORT = int(os.getenv("EDGEOPS_PORT", "8010"))
# 并发 worker 数：0=单进程（默认，推荐：SQLite + 内置定时调度器仅适合单进程）
# Worker count: 0 = single process (default; SQLite + built-in scheduler suit single process).
# 设 ≥1 为多进程（仅建议在 Linux、且理解定时任务可能重复、数据库并发限制时使用）
# ≥1 = multi-process (Linux only; understand possible duplicate cron & DB limits).
WORKERS = max(0, int(os.getenv("EDGEOPS_WORKERS", "0")))

# AI 配置（远程 AI 运维助手，兼容 DashScope / Ollama / OpenAI）
# AI defaults (remote ops assistant; DashScope / Ollama / OpenAI-compatible API).
# 阿里云：AI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1  AI_MODEL=qwen3.5-plus
# Aliyun: AI_BASE_URL=…compatible-mode/v1  AI_MODEL=qwen3.5-plus
# Ollama： AI_BASE_URL=http://localhost:11434/v1  AI_MODEL=qwen3.5:latest（可留空 API Key）
# Ollama:  AI_BASE_URL=http://localhost:11434/v1  AI_MODEL=qwen3.5:latest (API key optional)
# OpenAI： AI_BASE_URL=https://api.openai.com/v1  AI_MODEL=gpt-4o-mini
# OpenAI:  AI_BASE_URL=https://api.openai.com/v1  AI_MODEL=gpt-4o-mini
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
AI_MODEL = os.getenv("AI_MODEL", "qwen3.5-plus")  # 阿里 compatible-mode 推荐 / Aliyun compatible-mode default
# Agent 内层（单轮内最多调用工具/思考步数）默认 100，硬上限 1000
# Inner agent loop: max tool/thinking steps per model round (default 100, cap 1000).
AGENT_MAX_STEPS = int(os.getenv("EDGEOPS_AGENT_MAX_STEPS", "100"))
# 辅助 AI 外层（连续助手轮次）默认 100，硬上限 1000
# Outer assistant rounds: max consecutive assistant follow-up rounds (default 100, cap 1000).
ASSISTANT_MAX_ROUNDS = int(os.getenv("EDGEOPS_ASSISTANT_MAX_ROUNDS", "100"))
# 上限常量：用户/管理员设置时同步校验，避免任何入口溢出
# Hard caps: validated when users/admins change settings to prevent overflow.
AGENT_MAX_STEPS_CAP = int(os.getenv("EDGEOPS_AGENT_MAX_STEPS_CAP", "1000"))
# Agent 轮询等待（get_terminal_buffer next_poll_in_seconds）分片间隔（秒），便于 SSE 心跳与用户中断
AGENT_POLL_WAIT_CHUNK_SEC = max(1, min(5, int(os.getenv("EDGEOPS_AGENT_POLL_WAIT_CHUNK_SEC", "1"))))
# 模型未传 next_poll 时：sudo/输出稳定后再读、长命令默认等待、buffer 出现进度条时的等待（秒）
AGENT_TERMINAL_POLL_SHORT = max(1, min(30, int(os.getenv("EDGEOPS_AGENT_TERMINAL_POLL_SHORT", "3"))))
AGENT_TERMINAL_POLL_DEFAULT = max(1, min(600, int(os.getenv("EDGEOPS_AGENT_TERMINAL_POLL_DEFAULT", "12"))))
AGENT_TERMINAL_POLL_PROGRESS = max(1, min(600, int(os.getenv("EDGEOPS_AGENT_TERMINAL_POLL_PROGRESS", "15"))))
AGENT_TERMINAL_POLL_MAX = max(1, min(3600, int(os.getenv("EDGEOPS_AGENT_TERMINAL_POLL_MAX", "120"))))
# 会话 session_runtime_json：已完成 ssh 后台任务在库中保留给 AI 参考的时长（秒）
SESSION_RUNTIME_FINISHED_TTL_SEC = int(os.getenv("EDGEOPS_SESSION_RUNTIME_FINISHED_TTL_SEC", "3600"))
SESSION_RUNTIME_MAX_ITEMS = int(os.getenv("EDGEOPS_SESSION_RUNTIME_MAX_ITEMS", "20"))
SESSION_RUNTIME_MAX_FINISHED_KEEP = int(os.getenv("EDGEOPS_SESSION_RUNTIME_MAX_FINISHED_KEEP", "5"))
ASSISTANT_MAX_ROUNDS_CAP = int(os.getenv("EDGEOPS_ASSISTANT_MAX_ROUNDS_CAP", "1000"))
# SSH 连接：优先现代算法；失败时自动回退 ssh-rsa（OpenWrt / 老旧 dropbear）。设为 false 则禁用回退。
SSH_LEGACY_RSA = os.getenv("EDGEOPS_SSH_LEGACY_RSA", "true").strip().lower() in ("1", "true", "yes")
# 优先使用 legacy ssh-rsa（已知 OpenWrt/dropbear 仅支持 ssh-rsa 时可设为 true）
SSH_TRY_LEGACY_RSA_FIRST = os.getenv("EDGEOPS_SSH_TRY_LEGACY_RSA_FIRST", "false").strip().lower() in ("1", "true", "yes")
# SSH Channel：Web 浏览器会话默认空闲关断（秒）；集成/OpenClaw 默认 600（10 分钟）
SSH_CHANNEL_WEB_IDLE_CLOSE_SEC = int(os.getenv("EDGEOPS_SSH_CHANNEL_WEB_IDLE_CLOSE_SEC", "300"))
SSH_CHANNEL_INTEGRATION_IDLE_CLOSE_SEC = int(os.getenv("EDGEOPS_SSH_CHANNEL_INTEGRATION_IDLE_CLOSE_SEC", "600"))
SSH_CHANNEL_OUTPUT_SPILL_MIN_CHARS = int(os.getenv("EDGEOPS_SSH_CHANNEL_OUTPUT_SPILL_MIN_CHARS", "8000"))
SSH_CHANNEL_READ_PREVIEW_CHARS = int(os.getenv("EDGEOPS_SSH_CHANNEL_READ_PREVIEW_CHARS", "4000"))
# 未配置个人 API Key 的用户使用系统 KEY 时的调用次数上限；达上限后提示联系管理员或配置自己的 AI
# Shared system-key usage limit per user without their own key; then prompt admin or add own key.
SYSTEM_AI_USAGE_LIMIT = int(os.getenv("EDGEOPS_SYSTEM_AI_USAGE_LIMIT", "2000"))
# 聊天上下文总字符数上限（0 表示不限制）；超出时按比例分段截断主机列表、分组、主机知识、终端输出、历史消息
# Max chat context chars (0 = unlimited); overflow trims hosts, groups, KB, terminal, history proportionally.
AI_CONTEXT_SIZE = int(os.getenv("EDGEOPS_AI_CONTEXT_SIZE", "0"))
# 未显式设置输出上限时，给 chat/completions 传递的默认 max_tokens（防止不同 provider 默认值差异过大）
# Default max_tokens for chat/completions when not set (normalizes provider defaults).
AI_DEFAULT_MAX_OUTPUT_TOKENS = int(os.getenv("EDGEOPS_AI_DEFAULT_MAX_OUTPUT_TOKENS", "16384"))
# 后台定时/触发任务 Agent 单次 LLM 输出 max_tokens（默认与 AI_DEFAULT_MAX_OUTPUT_TOKENS 一致；原硬编码 4096 易截断长报告）
TASK_AGENT_MAX_OUTPUT_TOKENS = int(os.getenv("EDGEOPS_TASK_AGENT_MAX_OUTPUT_TOKENS", str(AI_DEFAULT_MAX_OUTPUT_TOKENS)))
# 任务运行记录列表中的 log_summary 最大字符（仅 UI 摘要，不是邮件正文）
TASK_RUN_LOG_SUMMARY_MAX_CHARS = int(os.getenv("EDGEOPS_TASK_RUN_LOG_SUMMARY_MAX_CHARS", "2000"))
# 定时任务 notify_email_to 自动通知邮件正文最大字符（发送完整 AI 输出，非 log_summary）
SCHEDULED_TASK_NOTIFY_EMAIL_MAX_CHARS = int(os.getenv("EDGEOPS_SCHEDULED_TASK_NOTIFY_EMAIL_MAX_CHARS", str(500_000)))
# send_email 工具正文软上限（0=不限制；超大时截断并在工具返回中提示）
USER_SEND_EMAIL_BODY_MAX_CHARS = int(os.getenv("EDGEOPS_USER_SEND_EMAIL_BODY_MAX_CHARS", str(500_000)))
USER_SEND_EMAIL_HTML_MAX_CHARS = int(os.getenv("EDGEOPS_USER_SEND_EMAIL_HTML_MAX_CHARS", str(500_000)))
# send_email 附件：单文件 / 总大小 / 数量上限
USER_SEND_EMAIL_ATTACHMENT_MAX_BYTES = int(os.getenv("EDGEOPS_USER_SEND_EMAIL_ATTACHMENT_MAX_BYTES", str(25 * 1024 * 1024)))
USER_SEND_EMAIL_ATTACHMENT_MAX_TOTAL_BYTES = int(os.getenv("EDGEOPS_USER_SEND_EMAIL_ATTACHMENT_MAX_TOTAL_BYTES", str(50 * 1024 * 1024)))
USER_SEND_EMAIL_ATTACHMENT_MAX_FILES = int(os.getenv("EDGEOPS_USER_SEND_EMAIL_ATTACHMENT_MAX_FILES", "10"))
# Web 聊天：上游 chat/completions 的 HTTP 读超时（秒）。生成长 HTML、大段代码时 120s 易触发超时。
# Read timeout for browser chat LLM HTTP calls; raise if the model streams slowly for large artifacts.
AI_CHAT_HTTP_READ_TIMEOUT_SEC = float(os.getenv("EDGEOPS_AI_CHAT_HTTP_READ_TIMEOUT_SEC", "240"))
# LLM 单次请求遇 httpx 超时后的**额外**重试次数（不含首次）。例如 3 表示最多共 4 次尝试。
# Extra attempts after a timeout (not counting the first request). 3 => up to 4 tries total.
AI_CHAT_LLM_TIMEOUT_RETRIES = max(0, int(os.getenv("EDGEOPS_AI_CHAT_LLM_TIMEOUT_RETRIES", "3")))
# ClawOps / 集成聊天（/api/integration/ops-chat/complete）保护项：
# Integration ops-chat safeguards:
# 为防止因自动压缩导致能力下降，可单独提高最小上下文与工具结果可见长度。
# Raise min context & tool-result visibility so auto-compression hurts capability less.
AI_INTEGRATION_ENFORCE_MIN_CONTEXT = os.getenv("EDGEOPS_AI_INTEGRATION_ENFORCE_MIN_CONTEXT", "true").strip().lower() in ("1", "true", "yes")
AI_INTEGRATION_CONTEXT_MIN = int(os.getenv("EDGEOPS_AI_INTEGRATION_CONTEXT_MIN", "48000"))
AI_INTEGRATION_TOOL_RESULT_LIMIT_MIN = int(os.getenv("EDGEOPS_AI_INTEGRATION_TOOL_RESULT_LIMIT_MIN", "6000"))
# 默认关闭“旧助手消息摘要压缩”，优先保持多轮决策链完整性。
# Default off: avoid summarizing old assistant messages to preserve multi-step decision chains.
AI_INTEGRATION_SUMMARIZE_OLD_ASSISTANT = os.getenv("EDGEOPS_AI_INTEGRATION_SUMMARIZE_OLD_ASSISTANT", "false").strip().lower() in ("1", "true", "yes")
# .edgeops 持久化白名单（按 host_type 关键词，逗号分隔；默认仅 Linux/macOS/Windows）
# .edgeops persistence allowlist by host_type keyword (comma-separated; default Linux/macOS/Windows).
EDGEOPS_PERSIST_HOST_TYPE_WHITELIST = [
    s.strip().lower()
    for s in os.getenv("EDGEOPS_PERSIST_HOST_TYPE_WHITELIST", "linux,macos,windows").split(",")
    if s.strip()
]
# AI 源类型：仅与调用方式绑定（请求头、URL 规范等），不绑定 URL；空表示按 base_url 自动探测
# Provider type: transport/auth only, not tied to URL; empty = infer from base_url.
MODEL_TYPES = [
    {"id": "aliyun", "name": "Aliyun DashScope"},
    {"id": "ollama", "name": "Ollama (local)"},
    {"id": "openai", "name": "OpenAI / compatible API"},
]
# AI 会话：与 UI 语言无关的机器标题前缀（自动生成的默认会话名；可据此判断是否需要自动总结标题）
# Session title prefix (locale-neutral machine default; used to decide auto-title summarization).
EDGEOPS_TEMP_SESSION_PREFIX = "edgeops:temp:"
# 客户端传入以下占位 title 时由服务端生成 EDGEOPS_TEMP_SESSION_PREFIX + 时间戳（含历史「新会话」）
# Client placeholders replaced server-side with EDGEOPS_TEMP_SESSION_PREFIX + timestamp (incl. legacy “新会话”).
EDGEOPS_SESSION_TITLE_CLIENT_PLACEHOLDERS = frozenset(("", "default", "新会话"))

# 上下文长度可选值（字符数上限）；0=不限制；支持到 8MB，前端可手工输入
# Context size presets (char cap); 0 = unlimited; up to 8MB; UI may enter custom value.
CONTEXT_SIZE_OPTIONS = [0, 4000, 8000, 16000, 32000, 64000, 128000, 262144, 524288, 1048576, 2097152, 4194304, 8388608]
CONTEXT_SIZE_MAX = 8 * 1024 * 1024  # 8MB 上限，手工输入时校验 / 8MB cap for manual input validation
# 系统提示词默认在代码中构建，可被设置中的 ai_system_prompt 覆盖
# System prompt built in code unless overridden by settings ai_system_prompt.

# Web 静态文件 / Static web assets
WEB_DIR = str(BASE_DIR / "web")

# 文件系统根目录（缓存、与节点上传下载等），位于 web/fs
# Filesystem root (cache, node upload/download) under web/fs
FS_DIR = BASE_DIR / "web" / "fs"

# AI 帮助文档目录（Markdown，管理员可编辑，用户只读）
# AI help docs (Markdown); admin-editable, users read-only
AIHELP_DIR = BASE_DIR / "web" / "aihelp"

# AI 聊天附件：落盘在每个用户的文件系统根目录下的 chats 子目录，即 web/fs/<username>/chats/<uuid>.<ext>
# Chat attachments: stored under web/fs/<username>/chats/<uuid>.<ext> so users see them in /api/fs.
# 这样用户在 /api/fs 文件系统面板里也能看到自己的聊天附件；跨用户/父目录访问由 filesystem._safe_username 与路径边界校验阻断。
# Cross-user/parent escape blocked by filesystem._safe_username and path checks.
CHAT_ATTACHMENT_SUBDIR = "chats"
# 单个聊天附件最大大小（字节）。默认 20 MB，可通过环境变量覆盖。
# Max single attachment bytes (default 20 MB; override via env).
CHAT_ATTACHMENT_MAX_BYTES = int(os.getenv("EDGEOPS_CHAT_ATTACHMENT_MAX_BYTES", str(20 * 1024 * 1024)))
# 单会话可累计的附件总字节上限（软限），避免单会话爆存储。默认 500 MB。
# Soft quota per session total attachment bytes (default 500 MB).
CHAT_ATTACHMENT_SESSION_QUOTA_BYTES = int(os.getenv("EDGEOPS_CHAT_ATTACHMENT_SESSION_QUOTA_BYTES", str(500 * 1024 * 1024)))

# MarkItDown：聊天附件中的 Office/PDF 等转为 Markdown 供 AI 分析（见 services/markitdown_convert.py）
MARKITDOWN_ENABLED = os.getenv("EDGEOPS_MARKITDOWN_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# 单次转换写入/返回的最大字符数（旁路缓存亦按此截断）
MARKITDOWN_MAX_OUTPUT_CHARS = int(os.getenv("EDGEOPS_MARKITDOWN_MAX_OUTPUT_CHARS", str(500_000)))

# AI 工具大结果溢出：超过字符阈值则写入 chats/<date>/spill/<uuid>.data，上下文仅保留哨兵与压缩预览。
CHAT_TOOL_SPILL_MIN_CHARS = int(os.getenv("EDGEOPS_CHAT_TOOL_SPILL_MIN_CHARS", "2500"))
CHAT_TOOL_SPILL_READ_MAX_CHARS = int(os.getenv("EDGEOPS_CHAT_TOOL_SPILL_READ_MAX_CHARS", str(500_000)))
# 历史消息按字符预算裁剪时，单条配额低于此值则 tool 消息中的溢出块可收缩为仅哨兵行（引导 read_chat_data）。
CHAT_HISTORY_TOOL_SPILL_SHRINK_THRESHOLD = int(os.getenv("EDGEOPS_CHAT_HISTORY_TOOL_SPILL_SHRINK_THRESHOLD", "900"))

# AI scp_pull：从远端 SFTP 拉到用户 web/fs 的单文件上限（字节）。
SCP_PULL_MAX_BYTES = int(os.getenv("EDGEOPS_SCP_PULL_MAX_BYTES", str(200 * 1024 * 1024)))

# AI 成果物（artifacts）：AI 生成的报告 / 数据包 / HTML 可视化 等，落盘在 web/fs/<username>/<ARTIFACT_SUBDIR>/YYYY/MM/DD/<id>/
# Artifacts (reports, bundles, HTML viz): web/fs/<username>/<ARTIFACT_SUBDIR>/YYYY/MM/DD/<id>/
# 注意：与 CHAT_ATTACHMENT_SUBDIR 共用同一个 "chats" 根，按日期子目录区分；
# Same "chats" root as attachments; date subdirs separate namespaces.
# 附件落盘为文件（<uuid>.<ext>），artifact 落盘为子目录（<slug>-<shortid>/），命名空间互不冲突。
# Attachments = files; artifacts = subdirs — no name collision.
ARTIFACT_SUBDIR = "chats"
# 单个 artifact 文件数上限。防止 AI 一次写入成千上万个小文件。
# Max files per artifact (avoid huge file counts).
ARTIFACT_MAX_FILES = int(os.getenv("EDGEOPS_ARTIFACT_MAX_FILES", "200"))

# 每用户 Agent Skills 根目录：web/fs/<username>/skills/<skill-name>/SKILL.md
USER_SKILLS_SUBDIR = os.getenv("EDGEOPS_USER_SKILLS_SUBDIR", "skills").strip("/\\") or "skills"
# 注入 AI system 的单 skill 正文上限（字符）
USER_SKILLS_BODY_MAX_CHARS = int(os.getenv("EDGEOPS_USER_SKILLS_BODY_MAX_CHARS", str(12_000)))
USER_SKILLS_TOTAL_MAX_CHARS = int(os.getenv("EDGEOPS_USER_SKILLS_TOTAL_MAX_CHARS", str(48_000)))
# 渐进式披露：默认仅注入 name+description 目录；always-apply / disable-model-invocation:false 才内联正文
USER_SKILLS_PROGRESSIVE_DISCLOSURE = os.getenv(
    "EDGEOPS_USER_SKILLS_PROGRESSIVE_DISCLOSURE", "true"
).strip().lower() in ("1", "true", "yes")
# frontmatter description 上限（对齐 Cursor Agent Skills 约定）
USER_SKILLS_DESC_MAX_CHARS = int(os.getenv("EDGEOPS_USER_SKILLS_DESC_MAX_CHARS", "1024"))
# read_user_skill_file 单文件读取上限
USER_SKILLS_RESOURCE_MAX_CHARS = int(os.getenv("EDGEOPS_USER_SKILLS_RESOURCE_MAX_CHARS", str(32_000)))

# Markdown 章节工具：单次返回正文上限 / 解析文件上限 / 硬顶
MARKDOWN_SECTIONS_MAX_CHARS = int(os.getenv("EDGEOPS_MARKDOWN_SECTIONS_MAX_CHARS", str(32_000)))
MARKDOWN_SECTIONS_MAX_CHARS_HARD = int(os.getenv("EDGEOPS_MARKDOWN_SECTIONS_MAX_CHARS_HARD", str(200_000)))
MARKDOWN_SECTIONS_MAX_FILE_CHARS = int(os.getenv("EDGEOPS_MARKDOWN_SECTIONS_MAX_FILE_CHARS", str(2_000_000)))
MARKDOWN_SECTIONS_SEARCH_MAX_HITS = int(os.getenv("EDGEOPS_MARKDOWN_SECTIONS_SEARCH_MAX_HITS", "50"))
MARKDOWN_SECTIONS_SEARCH_MAX_FILES = int(os.getenv("EDGEOPS_MARKDOWN_SECTIONS_SEARCH_MAX_FILES", "100"))
# 单个 artifact 中单个文件最大字节数。默认 50 MB。
# Max bytes per file inside an artifact (default 50 MB).
ARTIFACT_MAX_FILE_BYTES = int(os.getenv("EDGEOPS_ARTIFACT_MAX_FILE_BYTES", str(50 * 1024 * 1024)))
# 单个 artifact 总字节数上限。默认 200 MB。
# Max total bytes per artifact (default 200 MB).
ARTIFACT_MAX_TOTAL_BYTES = int(os.getenv("EDGEOPS_ARTIFACT_MAX_TOTAL_BYTES", str(200 * 1024 * 1024)))

# 本机管理（仅管理员）文件系统根目录；未设置时使用 BASE_DIR，禁止 .. 逃逸
# Local admin FS root (admin only); default BASE_DIR; path traversal blocked.
# 当 LOCAL_MANAGE_FULL_FS=True 时，可访问整个系统任意路径（仍仅管理员）；路径可为绝对路径。
# LOCAL_MANAGE_FULL_FS=True allows any path on the machine (admin still required).
LOCAL_MANAGE_ROOT = os.getenv("EDGEOPS_LOCAL_MANAGE_ROOT", str(BASE_DIR))
LOCAL_MANAGE_FULL_FS = os.getenv("EDGEOPS_LOCAL_MANAGE_FULL_FS", "true").strip().lower() in ("1", "true", "yes")
# Windows 本机终端后端：auto=优先 pywinpty/ConPTY，异常频繁时自动降级 PIPE；pywinpty=强制 ConPTY；pipe=强制 PIPE
# Windows local terminal: auto=pywinpty/ConPTY with PIPE fallback; pywinpty=force ConPTY; pipe=force PIPE.
LOCAL_TERMINAL_WINDOWS_BACKEND = os.getenv("EDGEOPS_LOCAL_TERMINAL_WINDOWS_BACKEND", "auto").strip().lower()

# MCP（Python，与 claw-ops 同 REST / Bearer；默认随主 Web 同进程挂载 /mcp；设 EDGEOPS_MCP_ENABLED=false 可关闭）
MCP_ENABLED = os.getenv("EDGEOPS_MCP_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# 同进程挂载路径（默认 /mcp，与主 Web 共用 EDGEOPS_PORT）
_mcp_http_path = os.getenv("EDGEOPS_MCP_HTTP_PATH", "/mcp").strip() or "/mcp"
MCP_HTTP_PATH = _mcp_http_path if _mcp_http_path.startswith("/") else f"/{_mcp_http_path}"
# 仅 python -m services.edgeops_mcp --http 独立进程时使用
MCP_HOST = os.getenv("EDGEOPS_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("EDGEOPS_MCP_PORT", "8011"))
# MCP 工具回连 毛竹 REST 的根地址；空则 http://127.0.0.1:{PORT}
MCP_API_BASE_URL = os.getenv("EDGEOPS_MCP_API_BASE_URL", "").strip()
# MCP fallback Bearer（JWT 或 eop_）；HTTP 客户端 headers/env 优先；stdio 亦可用 EDGEOPS_ACCESS_TOKEN
MCP_ACCESS_TOKEN = os.getenv("EDGEOPS_MCP_ACCESS_TOKEN", os.getenv("EDGEOPS_ACCESS_TOKEN", "")).strip()
# MCP 工具结果中的临时外链（如 DashScope OSS 签名 URL）自动拉取为聊天附件
MCP_REMOTE_FETCH_ENABLED = os.getenv("EDGEOPS_MCP_REMOTE_FETCH_ENABLED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)
MCP_REMOTE_FETCH_MAX_BYTES = int(
    os.getenv("EDGEOPS_MCP_REMOTE_FETCH_MAX_BYTES", str(20 * 1024 * 1024))
)
MCP_REMOTE_FETCH_MAX_URLS = int(os.getenv("EDGEOPS_MCP_REMOTE_FETCH_MAX_URLS", "5"))
# 反向代理后保留 X-Forwarded-Proto（避免 /mcp 等重定向降级为 http）
TRUST_PROXY_HEADERS = os.getenv("EDGEOPS_TRUST_PROXY_HEADERS", "true").strip().lower() in ("1", "true", "yes")
TRUSTED_PROXY_HOSTS = os.getenv("EDGEOPS_TRUSTED_PROXY_HOSTS", "*").strip() or "*"
