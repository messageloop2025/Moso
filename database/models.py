"""数据库初始化和模型定义（依设计.md：凭证与主机分离、多级分组、维护历史、Skills）"""
import logging
import os
import sqlite3

import aiosqlite
import config

logger = logging.getLogger("edgeops.database")

_db: aiosqlite.Connection | None = None


async def apply_sqlite_concurrency_settings(db: aiosqlite.Connection) -> None:
    """多人并发访问时减轻锁冲突：busy 等待、WAL 多读单写、synchronous 与 WAL 搭配。"""
    try:
        await db.execute(f"PRAGMA busy_timeout = {int(config.SQLITE_BUSY_TIMEOUT_MS)}")
        await db.execute("PRAGMA synchronous=NORMAL")
        if config.SQLITE_WAL:
            rows = await db.execute_fetchall("PRAGMA journal_mode=WAL")
            mode = (rows[0][0] if rows else "") or ""
            if str(mode).lower() != "wal":
                logger.warning(
                    "SQLite journal_mode=%s（期望 wal）。只读盘或特殊环境可忽略；否则并发写易 SQLITE_BUSY。",
                    mode,
                )
        logger.info(
            "SQLite 并发: busy_timeout=%sms, WAL=%s",
            config.SQLITE_BUSY_TIMEOUT_MS,
            config.SQLITE_WAL,
        )
    except Exception as e:
        logger.warning("SQLite 并发参数设置失败: %s", e)

SCHEMA_SQL = """
-- 用户表
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'user',
    status TEXT DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_login DATETIME
);

-- 凭证表（与主机分离：密码型 / 密钥型）
CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    username TEXT,
    password_enc TEXT,
    key_type TEXT,
    key_bits INTEGER,
    public_key TEXT,
    private_key_enc TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_by INTEGER REFERENCES users(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_credentials_code ON credentials(code);

-- SSH 主机表（仅引用凭证；username/auth_type/password_enc/key_path 保留兼容旧数据，新主机仅用 credential_id）
CREATE TABLE IF NOT EXISTS hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER DEFAULT 22,
    credential_id INTEGER REFERENCES credentials(id),
    username TEXT,
    auth_type TEXT DEFAULT 'password',
    password_enc TEXT,
    key_path TEXT,
    description TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_hosts_credential ON hosts(credential_id);

-- 主机分组（多级：parent_id；按用户隔离，管理员可见全部）
CREATE TABLE IF NOT EXISTS host_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    parent_id INTEGER REFERENCES host_groups(id),
    created_by INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS host_group_members (
    host_id INTEGER REFERENCES hosts(id) ON DELETE CASCADE,
    group_id INTEGER REFERENCES host_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (host_id, group_id)
);

-- 系统设置
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 操作日志（所有关键操作：谁、何时、操作类型、参数、结果、来源）
CREATE TABLE IF NOT EXISTS operation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    host_id INTEGER,
    operation TEXT NOT NULL,
    params TEXT,
    result TEXT,
    details TEXT,
    source TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_operation_logs_created_at ON operation_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_operation_logs_user ON operation_logs(user_id);

-- 服务器维护历史（按 IP/主机标识）
CREATE TABLE IF NOT EXISTS server_maintenance_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    port INTEGER DEFAULT 22,
    category TEXT NOT NULL,
    content TEXT,
    file_path TEXT,
    details TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_maintenance_host ON server_maintenance_history(host);

-- Skills（AI 可调用）
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    parameters_schema TEXT,
    enabled INTEGER DEFAULT 1,
    deprecated INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- AI 聊天会话（host_id 为空=全局 AI 助手；非空=该主机的 AI 运维会话；session_scope=local 为本机管理会话）
CREATE TABLE IF NOT EXISTS ai_chat_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
    title TEXT DEFAULT '新会话',
    session_prompt TEXT DEFAULT '',
    session_scope TEXT DEFAULT 'default',
    low_interaction_mode TEXT DEFAULT 'false',
    chat_mode TEXT NOT NULL DEFAULT 'normal',
    strict_allow_cache_json TEXT NOT NULL DEFAULT '',
    session_runtime_json TEXT NOT NULL DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ai_sessions_user ON ai_chat_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_ai_sessions_host ON ai_chat_sessions(host_id);

-- AI 聊天消息
CREATE TABLE IF NOT EXISTS ai_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES ai_chat_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ai_messages_session ON ai_chat_messages(session_id);

-- 主机分享（所有者将主机授权给其他用户）
CREATE TABLE IF NOT EXISTS host_shares (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    shared_with_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    revoked_at DATETIME
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_host_shares_unique ON host_shares(host_id, shared_with_user_id);
CREATE INDEX IF NOT EXISTS idx_host_shares_owner ON host_shares(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_host_shares_receiver ON host_shares(shared_with_user_id);

-- 主机标签（用户私有）
CREATE TABLE IF NOT EXISTS host_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    color TEXT DEFAULT '',
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_host_tags_user_name ON host_tags(created_by, name);
CREATE INDEX IF NOT EXISTS idx_host_tags_user ON host_tags(created_by);

-- 主机标签关联（按用户维度隔离）
CREATE TABLE IF NOT EXISTS host_user_tags (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES host_tags(id) ON DELETE CASCADE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, host_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_host_user_tags_user_host ON host_user_tags(user_id, host_id);
CREATE INDEX IF NOT EXISTS idx_host_user_tags_tag ON host_user_tags(tag_id);

-- 用户登录事件（用于登录次数统计）
CREATE TABLE IF NOT EXISTS user_login_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    login_type TEXT DEFAULT 'password',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_user_login_events_created_at ON user_login_events(created_at);
CREATE INDEX IF NOT EXISTS idx_user_login_events_user ON user_login_events(user_id);

-- 以主机为维度的 AI 知识（账户、密码、数据路径等，供 AI 在操作该主机时使用）
CREATE TABLE IF NOT EXISTS ai_host_knowledge (
    host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT DEFAULT '',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (host_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_host_knowledge_user ON ai_host_knowledge(user_id);

-- 以主机为维度的 AI 提示词（规则/能力/配置描述，按用户独立；主机分享给其它用户时提示词不共用）
CREATE TABLE IF NOT EXISTS ai_host_prompts (
    host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT DEFAULT '',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (host_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_host_prompts_user ON ai_host_prompts(user_id);

-- AI 编排工作流模板：保存 delegate_chain 的 payload 供复用（名称按 owner 唯一）
CREATE TABLE IF NOT EXISTS ai_workflow_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    kind TEXT DEFAULT 'delegate_chain',
    payload TEXT NOT NULL,
    tags TEXT DEFAULT '',
    visibility TEXT DEFAULT 'private',
    last_run_at DATETIME,
    run_count INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ai_workflow_templates_owner ON ai_workflow_templates(owner_user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_workflow_templates_owner_name ON ai_workflow_templates(owner_user_id, name);

-- 登录页匿名留言板（留言 + 管理员回复同表，parent_id 区分）
CREATE TABLE IF NOT EXISTS anonymous_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES anonymous_messages(id) ON DELETE CASCADE,
    author_type TEXT NOT NULL DEFAULT 'guest',
    author_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    nickname TEXT DEFAULT '',
    content TEXT NOT NULL,
    ip_address TEXT DEFAULT '',
    user_agent TEXT DEFAULT '',
    show_on_login INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_anon_msg_parent ON anonymous_messages(parent_id);
CREATE INDEX IF NOT EXISTS idx_anon_msg_status ON anonymous_messages(status);
CREATE INDEX IF NOT EXISTS idx_anon_msg_show ON anonymous_messages(show_on_login, status);

-- 系统内用户反馈（任何登录用户均可提交；多管理员可见可回）
CREATE TABLE IF NOT EXISTS user_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT DEFAULT '',
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    status TEXT NOT NULL DEFAULT 'open',
    is_ai_submitted INTEGER NOT NULL DEFAULT 0,
    admin_read_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_user_feedback_user ON user_feedback(user_id, status);
CREATE INDEX IF NOT EXISTS idx_user_feedback_status ON user_feedback(status, admin_read_at);
CREATE INDEX IF NOT EXISTS idx_user_feedback_created ON user_feedback(created_at);

CREATE TABLE IF NOT EXISTS user_feedback_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_id INTEGER NOT NULL REFERENCES user_feedback(id) ON DELETE CASCADE,
    admin_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    is_ai_drafted INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_user_feedback_replies_fb ON user_feedback_replies(feedback_id, created_at);

-- 用户搜索服务配置（GitHub / Aliyun IQS / 后续 Bing 等；按 (user_id, provider) 一行）
CREATE TABLE IF NOT EXISTS user_search_config (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    api_key TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    extra TEXT DEFAULT '{}',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, provider)
);
CREATE INDEX IF NOT EXISTS idx_user_search_config_provider ON user_search_config(provider);

-- 最佳实践（AI 与用户归纳的推荐实现方法，可被 AI 查询与写入）
CREATE TABLE IF NOT EXISTS best_practices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT DEFAULT '',
    content TEXT NOT NULL,
    source TEXT DEFAULT 'manual',
    created_by INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_best_practices_category ON best_practices(category);
"""


async def _migrate_add_columns(db: aiosqlite.Connection):
    """为已有表增加新列（兼容旧库）。"""
    async def has_column(table: str, column: str) -> bool:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return any(r[1] == column for r in rows)
    if not await has_column("hosts", "credential_id"):
        await db.execute("ALTER TABLE hosts ADD COLUMN credential_id INTEGER REFERENCES credentials(id)")
        await db.commit()
    if not await has_column("host_groups", "parent_id"):
        await db.execute("ALTER TABLE host_groups ADD COLUMN parent_id INTEGER REFERENCES host_groups(id)")
        await db.commit()
    if not await has_column("ai_chat_sessions", "host_id"):
        await db.execute("ALTER TABLE ai_chat_sessions ADD COLUMN host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL")
        await db.commit()
    try:
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_sessions_host ON ai_chat_sessions(host_id)")
        await db.commit()
    except Exception:
        pass
    if not await has_column("hosts", "host_type"):
        await db.execute("ALTER TABLE hosts ADD COLUMN host_type TEXT DEFAULT '未知'")
        await db.commit()
    if not await has_column("hosts", "host_version"):
        await db.execute("ALTER TABLE hosts ADD COLUMN host_version TEXT DEFAULT '未知'")
        await db.commit()
    if not await has_column("hosts", "host_shell"):
        await db.execute("ALTER TABLE hosts ADD COLUMN host_shell TEXT DEFAULT NULL")
        await db.commit()
    if not await has_column("hosts", "host_package_manager"):
        await db.execute("ALTER TABLE hosts ADD COLUMN host_package_manager TEXT DEFAULT NULL")
        await db.commit()
    if not await has_column("ai_chat_sessions", "session_prompt"):
        try:
            await db.execute("ALTER TABLE ai_chat_sessions ADD COLUMN session_prompt TEXT DEFAULT ''")
            await db.commit()
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise
            # 多进程同时启动时另一 worker 可能已添加该列，忽略
            await db.rollback()
    if not await has_column("host_groups", "created_by"):
        await db.execute("ALTER TABLE host_groups ADD COLUMN created_by INTEGER REFERENCES users(id)")
        await db.commit()
    if not await has_column("ai_chat_sessions", "session_scope"):
        try:
            await db.execute("ALTER TABLE ai_chat_sessions ADD COLUMN session_scope TEXT DEFAULT 'default'")
            await db.commit()
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise
            await db.rollback()
    if not await has_column("ai_chat_sessions", "low_interaction_mode"):
        try:
            await db.execute("ALTER TABLE ai_chat_sessions ADD COLUMN low_interaction_mode TEXT DEFAULT 'false'")
            await db.commit()
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise
            await db.rollback()
    if not await has_column("ai_chat_sessions", "session_runtime_json"):
        try:
            await db.execute(
                "ALTER TABLE ai_chat_sessions ADD COLUMN session_runtime_json TEXT NOT NULL DEFAULT ''"
            )
            await db.commit()
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise
            await db.rollback()
    if not await has_column("ai_chat_sessions", "chat_mode"):
        try:
            await db.execute(
                "ALTER TABLE ai_chat_sessions ADD COLUMN chat_mode TEXT NOT NULL DEFAULT 'normal'"
            )
            await db.commit()
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise
            await db.rollback()
    if not await has_column("ai_chat_sessions", "strict_allow_cache_json"):
        try:
            await db.execute(
                "ALTER TABLE ai_chat_sessions ADD COLUMN strict_allow_cache_json TEXT NOT NULL DEFAULT ''"
            )
            await db.commit()
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise
            await db.rollback()


async def _migrate_owner_backfill(db: aiosqlite.Connection):
    """将 hosts、credentials、host_groups 中 created_by 为 NULL 的置为第一个管理员，保留现有数据。"""
    cursor = await db.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
    row = await cursor.fetchone()
    if not row:
        return
    admin_id = row[0]
    for table, col in [("hosts", "created_by"), ("credentials", "created_by"), ("host_groups", "created_by")]:
        try:
            cursor = await db.execute(f"PRAGMA table_info({table})")
            info = await cursor.fetchall()
            if not any(c[1] == col for c in info):
                continue
            await db.execute(f"UPDATE {table} SET {col} = ? WHERE {col} IS NULL", (admin_id,))
            await db.commit()
        except Exception:
            await db.rollback()


async def _migrate_ai_host_knowledge(db: aiosqlite.Connection):
    """确保 ai_host_knowledge 表存在（兼容旧库）。"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ai_host_knowledge (
            host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content TEXT DEFAULT '',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (host_id, user_id)
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_host_knowledge_user ON ai_host_knowledge(user_id)")
    await db.commit()


async def _migrate_ai_host_prompts(db: aiosqlite.Connection):
    """确保 ai_host_prompts 表存在（兼容旧库）。主机级 AI 提示词，按 (host_id, user_id) 独立存储。"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ai_host_prompts (
            host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content TEXT DEFAULT '',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (host_id, user_id)
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_host_prompts_user ON ai_host_prompts(user_id)")
    await db.commit()


async def _migrate_best_practices(db: aiosqlite.Connection):
    """确保 best_practices 表存在（兼容旧库）。"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS best_practices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT DEFAULT '',
            content TEXT NOT NULL,
            source TEXT DEFAULT 'manual',
            created_by INTEGER REFERENCES users(id),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_best_practices_category ON best_practices(category)")
    await db.commit()


async def _migrate_user_ai_config(db: aiosqlite.Connection):
    """每用户 AI 配置表：每个用户独立配置 api_key、base_url、model 等。"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_ai_config (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            api_key TEXT DEFAULT '',
            base_url TEXT DEFAULT '',
            model TEXT DEFAULT '',
            system_prompt TEXT DEFAULT '',
            auto_approve TEXT DEFAULT 'false',
            assistant_enabled TEXT DEFAULT 'false',
            context_size TEXT DEFAULT '0',
            agent_max_steps TEXT DEFAULT '',
            assistant_max_rounds TEXT DEFAULT '',
            provider TEXT DEFAULT '',
            vision_enabled TEXT DEFAULT 'true',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.commit()


async def _migrate_user_ai_config_provider(db: aiosqlite.Connection):
    """user_ai_config 增加 provider：AI 源类型，仅绑定调用方式；空表示按 base_url 自动探测。"""
    async def has_column(table: str, column: str) -> bool:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return any(r[1] == column for r in rows)
    if not await has_column("user_ai_config", "provider"):
        await db.execute("ALTER TABLE user_ai_config ADD COLUMN provider TEXT DEFAULT ''")
        await db.commit()


async def _migrate_user_ai_config_output_locale(db: aiosqlite.Connection):
    """user_ai_config：AI 回复默认语言（空=按站点/浏览器链路与检测）。"""
    async def has_column(table: str, column: str) -> bool:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return any(r[1] == column for r in rows)

    if not await has_column("user_ai_config", "ai_output_locale"):
        await db.execute(
            "ALTER TABLE user_ai_config ADD COLUMN ai_output_locale TEXT DEFAULT ''"
        )
        await db.commit()


async def _migrate_user_ai_model_profiles(db: aiosqlite.Connection):
    """多组模型 Profile + active_profile_id；旧 user_ai_config 迁移为「默认配置」Profile。"""
    from services.ai_model_profiles import ensure_profiles_schema

    await ensure_profiles_schema(db)


async def _migrate_user_ai_model_profiles_default_name(db: aiosqlite.Connection):
    """将历史 Profile 名「默认」统一为「默认配置」。"""
    from services.ai_model_profiles import normalize_default_profile_names

    await normalize_default_profile_names(db)


async def _migrate_settings_ai_output_locale(db: aiosqlite.Connection):
    """站点级默认 AI 输出语言（空=不强制，回退到浏览器/兜底）。"""
    try:
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("ai_output_locale", ""),
        )
        await db.commit()
    except Exception:
        pass


async def _migrate_user_ai_config_vision(db: aiosqlite.Connection):
    """user_ai_config 增加 vision_enabled：指示该用户所配模型是否支持图像识别。

    缺省 'true'（即默认把图以 OpenAI 多模态 image_url 段内联到 user 消息 content）；
    非视觉模型/不兼容网关可以关掉，改走 read_chat_attachment(as_data_url) 兜底。
    """
    async def has_column(table: str, column: str) -> bool:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return any(r[1] == column for r in rows)
    if not await has_column("user_ai_config", "vision_enabled"):
        await db.execute(
            "ALTER TABLE user_ai_config ADD COLUMN vision_enabled TEXT DEFAULT 'true'"
        )
    await db.execute(
        "UPDATE user_ai_config SET vision_enabled = 'true' "
        "WHERE vision_enabled IS NULL OR TRIM(COALESCE(vision_enabled, '')) = ''"
    )
    await db.commit()


async def _migrate_user_system_ai_usage(db: aiosqlite.Connection):
    """未配置个人 API Key 的用户使用系统 KEY 时的调用次数统计表。"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_system_ai_usage (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            call_count INTEGER NOT NULL DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.commit()


async def _migrate_users_login_lockout(db: aiosqlite.Connection):
    """users 表增加 email、failed_login_attempts、locked_until，用于邮件找回密码与登录锁定。"""
    async def has_column(table: str, column: str) -> bool:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return any(r[1] == column for r in rows)
    for col, sql in [
        ("email", "ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''"),
        ("failed_login_attempts", "ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER DEFAULT 0"),
        ("locked_until", "ALTER TABLE users ADD COLUMN locked_until DATETIME"),
    ]:
        if not await has_column("users", col):
            await db.execute(sql)
            await db.commit()


async def _migrate_settings_smtp_site_url(db: aiosqlite.Connection):
    """确保 settings 表中有邮件与 site_url 配置项（供管理员在系统设置中编辑）。"""
    defaults = [
        ("site_url", ""),
        ("site_timezone", "Asia/Shanghai"),
        ("smtp_host", ""),
        ("smtp_port", "587"),
        ("smtp_use_tls", "true"),
        ("smtp_user", ""),
        ("smtp_password", ""),
        ("smtp_from", ""),
        ("smtp_use_ssl", ""),  # 留空或 false=STARTTLS(587)，true=SSL 直连(465)
    ]
    for k, v in defaults:
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (k, v),
        )
    await db.commit()


async def _migrate_email_templates(db: aiosqlite.Connection):
    """邮件通知模板：锁定/解锁/暂停/恢复，标题统一为 毛竹 通知，正文支持 {{username}}。"""
    defaults = [
        ("email_notification_subject", "毛竹通知"),
        (
            "email_template_lock_body",
            "{{username}}用户您好，您的毛竹账号已锁定，可以通过登录页面找回功能解锁。\n\n毛竹",
        ),
        (
            "email_template_unlock_body",
            "{{username}}用户您好，你的毛竹账号解锁成功。\n\n毛竹",
        ),
        (
            "email_template_suspend_body",
            "{{username}}用户您好，你的毛竹账号被管理员暂时停止使用。请回复邮件以沟通解决。\n\n毛竹",
        ),
        (
            "email_template_restore_body",
            "{{username}}用户您好，你的毛竹账号恢复使用。\n\n毛竹",
        ),
    ]
    for k, v in defaults:
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (k, v),
        )
    await db.commit()


async def _migrate_password_reset_tokens(db: aiosqlite.Connection):
    """密码重置/账户解锁 token 表。"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            expires_at DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_pwd_reset_user ON password_reset_tokens(user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_pwd_reset_expires ON password_reset_tokens(expires_at)")
    await db.commit()


async def _migrate_email_verification_codes(db: aiosqlite.Connection):
    """邮箱验证码表：绑定邮箱、找回密码/解锁 用 6 位验证码，一次有效。"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS email_verification_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            purpose TEXT NOT NULL,
            expires_at DATETIME NOT NULL,
            used_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_email_ver_user ON email_verification_codes(user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_email_ver_expires ON email_verification_codes(expires_at)")
    await db.commit()


async def _migrate_local_shell_tables(db: aiosqlite.Connection):
    """本机管理：会话与日志（仅管理员）。"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS local_shell_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT DEFAULT '本机会话',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS local_shell_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES local_shell_sessions(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            content TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_local_shell_sessions_user ON local_shell_sessions(user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_local_shell_logs_session ON local_shell_logs(session_id)")
    await db.commit()


async def _migrate_batch_tables(db: aiosqlite.Connection):
    """批量操作表：command/script/upload/restart/scp_push/scp_pull 等（operation_type + params JSON，无需改列）。"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS batch_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_type TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            scope_value TEXT,
            total_count INTEGER DEFAULT 0,
            pending_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running',
            params TEXT,
            created_by INTEGER REFERENCES users(id),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS batch_operation_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL REFERENCES batch_operations(id) ON DELETE CASCADE,
            host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
            status TEXT DEFAULT 'pending',
            result TEXT,
            started_at DATETIME,
            completed_at DATETIME
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_batch_details_batch ON batch_operation_details(batch_id)")
    await db.commit()


async def _migrate_session_prompt_scp(db: aiosqlite.Connection):
    """将会话提示词中「仅支持文本/不支持二进制」的旧 SCP 说明替换为新说明（scp_push 支持 local_path 上传二进制）。"""
    import re
    cursor = await db.execute(
        "SELECT id, session_prompt FROM ai_chat_sessions WHERE session_prompt IS NOT NULL AND (session_prompt LIKE ? OR session_prompt LIKE ? OR session_prompt LIKE ?)",
        ("%仅支持文本%", "%不支持二进制%", "%content 参数只能接收文本%"),
    )
    rows = await cursor.fetchall()
    new_line = "scp_push 可用 local_path 上传 web/fs 中的二进制文件（如 .tgz）。"
    for row in rows:
        pid = row[0]
        prompt = row[1] or ""
        # 从「当前系统限制」或含「scp_push」「content」「只能」的行到「替代方案」或「##」或段落结束，整块替换
        new_prompt = re.sub(
            r"([^\n]*当前系统限制[^\n]*\n|[-*]*\s*scp_push[^\n]*content[^\n]*只能[^\n]*\n)[\s\S]*?(?=替代方案|##\s|\n\n\S|\Z)",
            new_line + "\n\n",
            prompt,
            count=1,
            flags=re.DOTALL,
        )
        # 若未匹配到整块，则做短语替换，避免残留「仅支持文本」等
        if "仅支持文本" in new_prompt:
            new_prompt = new_prompt.replace("仅支持文本", "支持文本；二进制请用 local_path（web/fs 相对路径）上传")
        if "不支持二进制文件" in new_prompt:
            new_prompt = new_prompt.replace("不支持二进制文件", "二进制可用 scp_push 的 local_path 参数上传")
        if "不支持二进制" in new_prompt:
            new_prompt = new_prompt.replace("不支持二进制", "二进制可用 scp_push 的 local_path 参数上传")
        if new_prompt != prompt:
            await db.execute("UPDATE ai_chat_sessions SET session_prompt = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_prompt, pid))
    await db.commit()


async def _migrate_operation_logs_source(db: aiosqlite.Connection):
    """为 operation_logs 增加 source、details 列及常用索引（兼容旧库）。"""
    async def has_column(table: str, column: str) -> bool:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return any(r[1] == column for r in rows)
    if not await has_column("operation_logs", "source"):
        await db.execute("ALTER TABLE operation_logs ADD COLUMN source TEXT DEFAULT ''")
        await db.commit()
    if not await has_column("operation_logs", "details"):
        await db.execute("ALTER TABLE operation_logs ADD COLUMN details TEXT")
        await db.commit()
    try:
        await db.execute("CREATE INDEX IF NOT EXISTS idx_operation_logs_created_at ON operation_logs(created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_operation_logs_user ON operation_logs(user_id)")
        await db.commit()
    except Exception:
        pass


def _hosts_username_is_not_null(db_schema_info: list) -> bool:
    """PRAGMA table_info(hosts) 返回的 rows 中，username 列是否 NOT NULL（notnull=1）。"""
    for r in db_schema_info:
        if r[1] == "username":
            return r[3] == 1  # 3 = notnull
    return False


def _has_column_in_info(table_info: list, column: str) -> bool:
    return any(r[1] == column for r in table_info)


async def _migrate_hosts_username_nullable(db: aiosqlite.Connection):
    """若 hosts.username 为旧版 NOT NULL，则重建表使 username 可空（与「主机仅存 credential_id」设计一致）。保留 host_type/host_version 等已存在列。"""
    cursor = await db.execute("PRAGMA table_info(hosts)")
    rows = await cursor.fetchall()
    table_info = list(rows)
    if not _hosts_username_is_not_null(table_info):
        return
    has_host_type = _has_column_in_info(table_info, "host_type")
    has_host_version = _has_column_in_info(table_info, "host_version")
    has_host_shell = _has_column_in_info(table_info, "host_shell")
    has_host_package_manager = _has_column_in_info(table_info, "host_package_manager")
    extra_cols = []
    if has_host_type:
        extra_cols.append("host_type TEXT DEFAULT '未知'")
    if has_host_version:
        extra_cols.append("host_version TEXT DEFAULT '未知'")
    if has_host_shell:
        extra_cols.append("host_shell TEXT DEFAULT NULL")
    if has_host_package_manager:
        extra_cols.append("host_package_manager TEXT DEFAULT NULL")
    extra_sql = (", " + ", ".join(extra_cols)) if extra_cols else ""
    await db.execute(
        """
        CREATE TABLE hosts_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER DEFAULT 22,
            credential_id INTEGER REFERENCES credentials(id),
            username TEXT,
            auth_type TEXT DEFAULT 'password',
            password_enc TEXT,
            key_path TEXT,
            description TEXT,
            created_by INTEGER REFERENCES users(id),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            """
        + extra_sql
        + """
        )
    """
    )
    sel_cols = "id, name, host, port, credential_id, username, auth_type, password_enc, key_path, description, created_by, created_at, updated_at"
    if has_host_type:
        sel_cols += ", host_type"
    if has_host_version:
        sel_cols += ", host_version"
    if has_host_shell:
        sel_cols += ", host_shell"
    if has_host_package_manager:
        sel_cols += ", host_package_manager"
    await db.execute(
        f"INSERT INTO hosts_new ({sel_cols}) SELECT {sel_cols} FROM hosts"
    )
    await db.execute("DROP TABLE hosts")
    await db.execute("ALTER TABLE hosts_new RENAME TO hosts")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_hosts_credential ON hosts(credential_id)")
    await db.commit()


async def _migrate_ssh_channels_and_tasks(db: aiosqlite.Connection) -> None:
    """幂等版「001 + 003」：SSH 通道、触发任务、定时任务及其执行历史。

    与 migrations/001_ssh_channel_and_tasks.py + 003_triggered_task_run_messages.py 同源；
    这里全是 IF NOT EXISTS，可在 safety-net 里反复调用。
    """
    await db.executescript(
        """
CREATE TABLE IF NOT EXISTS ssh_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    input_timeout_sec INTEGER,
    output_timeout_sec INTEGER,
    idle_close_sec INTEGER DEFAULT 300,
    status TEXT DEFAULT 'open',
    host_info TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ssh_channels_owner ON ssh_channels(owner_type, owner_id);
CREATE INDEX IF NOT EXISTS idx_ssh_channels_user ON ssh_channels(user_id);

CREATE TABLE IF NOT EXISTS triggered_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    intro TEXT DEFAULT '',
    trigger_conditions TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_run_at DATETIME,
    last_run_status TEXT,
    is_running INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_triggered_tasks_user ON triggered_tasks(user_id);

CREATE TABLE IF NOT EXISTS triggered_task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES triggered_tasks(id) ON DELETE CASCADE,
    triggered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    triggered_by_type TEXT,
    triggered_by_id TEXT,
    status TEXT DEFAULT 'pending',
    instruction TEXT,
    log_summary TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_triggered_task_runs_task ON triggered_task_runs(task_id);

CREATE TABLE IF NOT EXISTS triggered_task_expose (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES triggered_tasks(id) ON DELETE CASCADE,
    expose_code TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_triggered_task_expose_task_code ON triggered_task_expose(task_id, expose_code);

CREATE TABLE IF NOT EXISTS triggered_task_run_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES triggered_task_runs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_triggered_task_run_messages_run ON triggered_task_run_messages(run_id);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    cron_expr TEXT,
    next_run_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_run_at DATETIME,
    last_run_status TEXT,
    is_running INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_user ON scheduled_tasks(user_id);

CREATE TABLE IF NOT EXISTS scheduled_task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    run_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'running',
    session_snapshot_id TEXT,
    log_summary TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_task ON scheduled_task_runs(task_id);

CREATE TABLE IF NOT EXISTS scheduled_task_run_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES scheduled_task_runs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_scheduled_task_run_messages_run ON scheduled_task_run_messages(run_id);
"""
    )
    await db.commit()


async def _migrate_api_tokens(db: aiosqlite.Connection) -> None:
    """幂等版「005」：API 访问令牌表。"""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS api_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL DEFAULT '',
            token_hash TEXT NOT NULL UNIQUE,
            token_prefix TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_used_at DATETIME
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id)")
    await db.commit()


async def _migrate_user_mail_config_table(db: aiosqlite.Connection) -> None:
    """幂等版「007」：每用户发信配置 + scheduled_tasks.notify_email_to 列。"""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_mail_config (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            mail_enabled TEXT NOT NULL DEFAULT 'false',
            smtp_host TEXT DEFAULT '',
            smtp_port TEXT DEFAULT '587',
            smtp_user TEXT DEFAULT '',
            smtp_password TEXT DEFAULT '',
            smtp_from TEXT DEFAULT '',
            smtp_use_tls TEXT DEFAULT 'true',
            smtp_use_ssl TEXT DEFAULT '',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.commit()


async def _migrate_feedback_tables(db: aiosqlite.Connection) -> None:
    """幂等版「015」：登录页留言板 + 系统内反馈三张表 + 默认 settings。"""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS anonymous_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER REFERENCES anonymous_messages(id) ON DELETE CASCADE,
            author_type TEXT NOT NULL DEFAULT 'guest',
            author_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            nickname TEXT DEFAULT '',
            content TEXT NOT NULL,
            ip_address TEXT DEFAULT '',
            user_agent TEXT DEFAULT '',
            show_on_login INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_anon_msg_parent ON anonymous_messages(parent_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_anon_msg_status ON anonymous_messages(status)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_anon_msg_show ON anonymous_messages(show_on_login, status)"
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT DEFAULT '',
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            status TEXT NOT NULL DEFAULT 'open',
            is_ai_submitted INTEGER NOT NULL DEFAULT 0,
            admin_read_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_user_feedback_user ON user_feedback(user_id, status)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_feedback_status ON user_feedback(status, admin_read_at)"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_user_feedback_created ON user_feedback(created_at)")
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_feedback_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feedback_id INTEGER NOT NULL REFERENCES user_feedback(id) ON DELETE CASCADE,
            admin_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            is_ai_drafted INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_feedback_replies_fb ON user_feedback_replies(feedback_id, created_at)"
    )
    await db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('notify_admin_on_user_feedback', 'false')"
    )
    await db.commit()


async def _migrate_chat_attachments(db: aiosqlite.Connection) -> None:
    """幂等版「018 + 019」：AI 聊天附件元数据表 + storage_subdir 子目录列。

    背景：018 仅在 schema_version < 18 时执行，019 仅在 < 19 时执行。若数据库因人工
    干预 / 备份恢复 / 并发写竞争导致 schema_version 已 >= 19 但 `chat_attachments` 表
    实际缺失，原始脚本永不回头执行，应用就会抛 `no such table: chat_attachments`。
    这里做完全幂等的补建：CREATE TABLE IF NOT EXISTS + 按需 ALTER ADD COLUMN。
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_id INTEGER REFERENCES ai_chat_sessions(id) ON DELETE SET NULL,
            message_id INTEGER REFERENCES ai_chat_messages(id) ON DELETE SET NULL,
            original_name TEXT DEFAULT '',
            mime_type TEXT DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL DEFAULT 'binary',
            storage_subdir TEXT NOT NULL DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_attachments_user ON chat_attachments(user_id, created_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_attachments_session ON chat_attachments(session_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_attachments_message ON chat_attachments(message_id)"
    )
    # 对极少数通过本帮手先建表、但 018 后来又把 storage_subdir 列忘加进 CREATE 的分支做保险
    try:
        cur = await db.execute("PRAGMA table_info(chat_attachments)")
        rows = await cur.fetchall()
        await cur.close()
        col_names = {r[1] for r in rows}
        if "storage_subdir" not in col_names:
            await db.execute(
                "ALTER TABLE chat_attachments ADD COLUMN storage_subdir TEXT NOT NULL DEFAULT ''"
            )
        # 023：AI 识别结果缓存列。首次上线时 col 不存在则补齐；老库已存在则保留。
        if "ai_description" not in col_names:
            await db.execute(
                "ALTER TABLE chat_attachments ADD COLUMN ai_description TEXT NOT NULL DEFAULT ''"
            )
        if "ai_description_model" not in col_names:
            await db.execute(
                "ALTER TABLE chat_attachments ADD COLUMN ai_description_model TEXT NOT NULL DEFAULT ''"
            )
        if "ai_description_updated_at" not in col_names:
            await db.execute(
                "ALTER TABLE chat_attachments ADD COLUMN ai_description_updated_at DATETIME"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("safety-net: 补 chat_attachments 列失败: %s", exc)
    await db.commit()


async def _migrate_ai_artifacts(db: aiosqlite.Connection) -> None:
    """幂等版「020」：AI 成果物元数据表。

    与 `_migrate_chat_attachments` 同理——避免 schema_version 已 >= 20 但表不在时
    整个 artifact 下载/预览链路直接 500。
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_id INTEGER REFERENCES ai_chat_sessions(id) ON DELETE SET NULL,
            message_id INTEGER REFERENCES ai_chat_messages(id) ON DELETE SET NULL,
            title TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'bundle',
            storage_subdir TEXT NOT NULL DEFAULT '',
            entry_file TEXT NOT NULL DEFAULT '',
            file_count INTEGER NOT NULL DEFAULT 0,
            total_bytes INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_artifacts_user ON ai_artifacts(user_id, created_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_artifacts_session ON ai_artifacts(session_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_artifacts_message ON ai_artifacts(message_id)"
    )
    await db.commit()


async def _migrate_jwt_nonces(db: aiosqlite.Connection) -> None:
    """幂等版「025」：JWT 一次性 nonce 表（验证码/找回 token 等短效 JWT 防重放）。

    与 `_migrate_chat_attachments` 同理——若 schema_version 已 >= 26 但表缺失，
    `run_upgrades` 不会回头执行 025，登录时会抛 `no such table: jwt_nonces`。
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS jwt_nonces (
            jti TEXT PRIMARY KEY,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_jwt_nonces_created ON jwt_nonces(created_at)"
    )
    await db.commit()


async def _migrate_mcp_agent_tasks(db: aiosqlite.Connection) -> None:
    """幂等版「026」：MCP 编排后台子 Agent 任务表。"""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_agent_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_id INTEGER NOT NULL REFERENCES ai_chat_sessions(id) ON DELETE CASCADE,
            host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
            title TEXT NOT NULL DEFAULT '',
            instruction TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            result_text TEXT NOT NULL DEFAULT '',
            error_text TEXT NOT NULL DEFAULT '',
            progress_json TEXT NOT NULL DEFAULT '[]',
            callback_delivered INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            started_at DATETIME,
            finished_at DATETIME
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcp_agent_tasks_user ON mcp_agent_tasks(user_id, status)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcp_agent_tasks_session ON mcp_agent_tasks(session_id)"
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_agent_task_controls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES mcp_agent_tasks(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            consumed INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcp_agent_task_controls_task ON mcp_agent_task_controls(task_id, consumed)"
    )
    await db.commit()


async def _migrate_user_mcp_servers(db: aiosqlite.Connection) -> None:
    """幂等版「027」：每用户自定义 MCP 服务器配置。"""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_mcp_servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            transport TEXT NOT NULL DEFAULT 'stdio',
            config_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            chat_enabled INTEGER NOT NULL DEFAULT 1,
            chat_scope_web INTEGER NOT NULL DEFAULT 1,
            chat_scope_host INTEGER NOT NULL DEFAULT 1,
            chat_scope_integration INTEGER NOT NULL DEFAULT 1,
            tool_count INTEGER NOT NULL DEFAULT 0,
            last_test_ok INTEGER,
            last_test_at DATETIME,
            last_error TEXT NOT NULL DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, name)
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_mcp_servers_user ON user_mcp_servers(user_id, enabled)"
    )
    await db.commit()


async def _migrate_user_mcp_chat_scopes(db: aiosqlite.Connection) -> None:
    """幂等版「028」：user_mcp_servers 按场景启用列。"""
    for col, ddl in (
        ("chat_scope_web", "INTEGER NOT NULL DEFAULT 1"),
        ("chat_scope_host", "INTEGER NOT NULL DEFAULT 1"),
        ("chat_scope_integration", "INTEGER NOT NULL DEFAULT 1"),
    ):
        try:
            await db.execute(f"ALTER TABLE user_mcp_servers ADD COLUMN {col} {ddl}")
        except Exception:
            pass
    await db.commit()


async def _migrate_user_skills(db: aiosqlite.Connection) -> None:
    """幂等版「029」：users.skills_enabled + user_skills 表。"""
    try:
        await db.execute("ALTER TABLE users ADD COLUMN skills_enabled INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            skill_path TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            chat_enabled INTEGER NOT NULL DEFAULT 1,
            chat_scope_web INTEGER NOT NULL DEFAULT 1,
            chat_scope_host INTEGER NOT NULL DEFAULT 1,
            chat_scope_integration INTEGER NOT NULL DEFAULT 0,
            file_mtime REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, name)
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_skills_user ON user_skills(user_id, enabled)"
    )
    await db.commit()


async def _migrate_org_skills_and_task_skills(db: aiosqlite.Connection) -> None:
    """幂等版「039」：org_skills 表 + scheduled/triggered_tasks.inject_user_skills。"""

    async def has_col(table: str, column: str) -> bool:
        try:
            cur = await db.execute(f"PRAGMA table_info({table})")
            rows = await cur.fetchall()
            await cur.close()
            return any(r[1] == column for r in rows)
        except Exception:
            return False

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS org_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            slash_name TEXT NOT NULL DEFAULT '',
            allowed_tools TEXT NOT NULL DEFAULT '',
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_org_skills_enabled ON org_skills(enabled)"
    )
    for table in ("scheduled_tasks", "triggered_tasks"):
        try:
            cur = await db.execute(f"SELECT 1 FROM {table} LIMIT 1")
            await cur.close()
        except Exception:
            continue
        if not await has_col(table, "inject_user_skills"):
            try:
                await db.execute(
                    f"ALTER TABLE {table} ADD COLUMN inject_user_skills INTEGER NOT NULL DEFAULT 0"
                )
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    logger.warning("safety-net: 补列 %s.inject_user_skills 失败: %s", table, e)
    await db.commit()


async def _migrate_host_service_credentials(db: aiosqlite.Connection) -> None:
    """幂等版「032」：服务凭证表 + settings.credentials_vault_enabled。"""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS host_service_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            host_id INTEGER,
            service TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            port INTEGER,
            service_username TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            password_enc TEXT NOT NULL DEFAULT '',
            linked_credential_id INTEGER,
            linked_host_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_accessed_at DATETIME,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE SET NULL,
            FOREIGN KEY (linked_credential_id) REFERENCES credentials(id) ON DELETE SET NULL,
            FOREIGN KEY (linked_host_id) REFERENCES hosts(id) ON DELETE SET NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_hsc_user ON host_service_credentials(user_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_hsc_user_lookup "
        "ON host_service_credentials(user_id, service, address, port, service_username)"
    )
    await db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('credentials_vault_enabled', 'false')"
    )
    await db.commit()


async def _migrate_service_credentials_port(db: aiosqlite.Connection) -> None:
    """幂等版「033」：port 列 + host_id 可空（旧表重建）。"""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent / "migrations" / "033_service_credentials_port.py"
    if not path.is_file():
        return
    spec = importlib.util.spec_from_file_location("migration_033_sn", path)
    if not spec or not spec.loader:
        return
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    await mod.upgrade(db)


async def _migrate_service_credentials_last_accessed(db: aiosqlite.Connection) -> None:
    """幂等版「034」：last_accessed_at 列。"""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent / "migrations" / "034_service_credentials_last_accessed.py"
    if not path.is_file():
        return
    spec = importlib.util.spec_from_file_location("migration_034_sn", path)
    if not spec or not spec.loader:
        return
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    await mod.upgrade(db)


async def _migrate_user_skill_groups(db: aiosqlite.Connection) -> None:
    """幂等版「035」：user_skill_groups + user_skills.group_id。"""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_skill_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, name)
        )
        """
    )

    async def has_col(table: str, column: str) -> bool:
        try:
            cur = await db.execute(f"PRAGMA table_info({table})")
            rows = await cur.fetchall()
            await cur.close()
            return any(r[1] == column for r in rows)
        except Exception:
            return False

    async def tbl_exists(table: str) -> bool:
        try:
            cur = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (table,),
            )
            row = await cur.fetchone()
            await cur.close()
            return row is not None
        except Exception:
            return False

    if await tbl_exists("user_skills") and not await has_col("user_skills", "group_id"):
        await db.execute(
            "ALTER TABLE user_skills ADD COLUMN group_id INTEGER "
            "REFERENCES user_skill_groups(id) ON DELETE SET NULL"
        )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_skill_groups_user "
        "ON user_skill_groups(user_id, sort_order)"
    )
    if await tbl_exists("user_skills") and await has_col("user_skills", "group_id"):
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_skills_group ON user_skills(user_id, group_id)"
        )
    await db.commit()


async def _migrate_user_search_config(db: aiosqlite.Connection) -> None:
    """幂等版「014」：用户搜索服务配置表（每用户每 provider 一行）。"""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_search_config (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            api_key TEXT DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            extra TEXT DEFAULT '{}',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, provider)
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_search_config_provider ON user_search_config(provider)"
    )
    await db.commit()


async def _migrate_legacy_added_columns(db: aiosqlite.Connection) -> None:
    """幂等版「002 / 004 / 006 / 007 中的 ALTER TABLE 部分」：补齐历史迁移加过的列。

    原迁移脚本里的 ALTER 不全有 has_column 自检，重复运行会因 duplicate column 报错；
    这里统一封装成「按需添加」的幂等版本，专供 safety-net 使用。
    """
    async def has_col(table: str, column: str) -> bool:
        try:
            cur = await db.execute(f"PRAGMA table_info({table})")
            rows = await cur.fetchall()
            await cur.close()
            return any(r[1] == column for r in rows)
        except Exception:
            return False

    legacy_columns = [
        ("triggered_task_runs", "caller_task_name", "ALTER TABLE triggered_task_runs ADD COLUMN caller_task_name TEXT DEFAULT ''"),
        ("scheduled_tasks", "enabled", "ALTER TABLE scheduled_tasks ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"),
        ("scheduled_tasks", "notify_email_to", "ALTER TABLE scheduled_tasks ADD COLUMN notify_email_to TEXT DEFAULT ''"),
        ("scheduled_tasks", "inject_user_skills", "ALTER TABLE scheduled_tasks ADD COLUMN inject_user_skills INTEGER NOT NULL DEFAULT 0"),
        ("triggered_tasks", "inject_user_skills", "ALTER TABLE triggered_tasks ADD COLUMN inject_user_skills INTEGER NOT NULL DEFAULT 0"),
        ("hosts", "aliases", "ALTER TABLE hosts ADD COLUMN aliases TEXT DEFAULT '[]'"),
        ("hosts", "remark", "ALTER TABLE hosts ADD COLUMN remark TEXT DEFAULT ''"),
        ("users", "skills_enabled", "ALTER TABLE users ADD COLUMN skills_enabled INTEGER NOT NULL DEFAULT 0"),
        ("user_skills", "group_id", "ALTER TABLE user_skills ADD COLUMN group_id INTEGER REFERENCES user_skill_groups(id) ON DELETE SET NULL"),
        ("user_skills", "slash_name", "ALTER TABLE user_skills ADD COLUMN slash_name TEXT NOT NULL DEFAULT ''"),
        ("user_skills", "hooks_enabled", "ALTER TABLE user_skills ADD COLUMN hooks_enabled INTEGER NOT NULL DEFAULT 0"),
        ("user_skills", "allowed_tools", "ALTER TABLE user_skills ADD COLUMN allowed_tools TEXT NOT NULL DEFAULT ''"),
        ("skills", "deprecated", "ALTER TABLE skills ADD COLUMN deprecated INTEGER NOT NULL DEFAULT 0"),
        ("ai_chat_sessions", "chat_mode", "ALTER TABLE ai_chat_sessions ADD COLUMN chat_mode TEXT NOT NULL DEFAULT 'normal'"),
        ("ai_chat_sessions", "strict_allow_cache_json", "ALTER TABLE ai_chat_sessions ADD COLUMN strict_allow_cache_json TEXT NOT NULL DEFAULT ''"),
    ]
    for table, col, ddl in legacy_columns:
        # 父表都不在的话直接跳过；它们会被前面的 _migrate_ssh_channels_and_tasks 等先建出来
        try:
            cur = await db.execute(f"SELECT 1 FROM {table} LIMIT 1")
            await cur.close()
        except Exception:
            continue
        if not await has_col(table, col):
            try:
                await db.execute(ddl)
                await db.commit()
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    logger.warning("safety-net: 补列 %s.%s 失败: %s", table, col, e)


async def _ensure_default_admin_user(db: aiosqlite.Connection) -> None:
    """若 ``users`` 表存在且无任何行，补建预置管理员 ``admin``（初始密码 ``admin123``）。

    与 ``run_initial_schema`` 中插入逻辑一致；用于 Docker/恢复库在「仅有空壳表、
    或 000 曾被错误跳过」时仍能登录管理端。已有任意用户时不动作。
    """
    try:
        cur = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users' LIMIT 1"
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return
        cur = await db.execute("SELECT COUNT(*) AS c FROM users")
        row = await cur.fetchone()
        await cur.close()
        n = int(row[0]) if row and row[0] is not None else 0
        if n > 0:
            return
        import bcrypt as _bcrypt

        pw_hash = _bcrypt.hashpw(b"admin123", _bcrypt.gensalt()).decode()
        await db.execute(
            "INSERT INTO users (username, display_name, password_hash, role, status) VALUES (?, ?, ?, ?, ?)",
            ("admin", "管理员", pw_hash, "admin", "active"),
        )
        await db.commit()
        logger.info("users 表为空：已补建预置管理员 admin（请尽快修改初始密码）。")
    except sqlite3.IntegrityError:
        try:
            await db.rollback()
        except Exception:
            pass
    except Exception as e:
        logger.warning("safety-net: 补建预置管理员失败: %s", e)


async def _ensure_full_schema_safety_net(db: aiosqlite.Connection) -> None:
    """每次启动都跑的「幂等安全网」：对所有「应当存在」的表/列执行 IF NOT EXISTS / has_column 自检。

    背景与必要性：
    - 历史上有 8 张表（user_ai_config / user_system_ai_usage / password_reset_tokens /
      email_verification_codes / local_shell_sessions / local_shell_logs / batch_operations /
      batch_operation_details）只能通过 run_initial_schema 里的 _migrate_* 帮手创建；
      它们没有任何独立的 migrations/0NN_*.py。任何被 _is_original_database 识别为 version=1
      的旧库都会跳过 000_initial，于是这 8 张表永远不会被建。
      （``_is_original_database`` 仅当 ``users`` 内已有至少一行时才为真，避免空壳表误跳过 000。）
    - 另有 10 张表（ssh_channels / triggered_* / scheduled_* / api_tokens / user_mail_config）
      只在 migrations/001/003/005/007 里创建。一旦 schema_version 已超过这些编号但表却不存在
      （例如人工修过 schema_version、备份恢复、并发死锁部分提交等），run_upgrades 会跳过，
      tables 永远缺失。
    - 个别后加的 migrations/0NN_*.py（如 012_ai_host_prompts）同样可能因上述原因缺表。

    本函数对策（每次启动都做一遍，开销几毫秒，全部幂等）：
    1) 重放 SCHEMA_SQL（全是 IF NOT EXISTS / IF NOT EXISTS INDEX）。
    2) 依次调用所有「建表型 / 补列型」帮手，覆盖 SCHEMA_SQL 之外的所有应有表与历史新增列。
    3) 不调用「数据改写型」帮手（如 _migrate_session_prompt_scp / _migrate_owner_backfill），
       避免重复改用户数据。

    任一步抛错只记 warning，不打断启动；后续 _check_db_schema 会汇总仍缺失的表。
    """
    try:
        await db.executescript(SCHEMA_SQL)
        await db.commit()
    except Exception as e:
        logger.warning("safety-net: 重放 SCHEMA_SQL 失败（极旧库可能存在结构冲突）: %s", e)

    safety_steps: list[tuple[str, "callable"]] = [
        # SCHEMA_SQL 之外的表（来自 migrations/001/003/005/007）
        ("ssh_channels + triggered_*/scheduled_*", _migrate_ssh_channels_and_tasks),
        ("api_tokens", _migrate_api_tokens),
        ("user_mail_config", _migrate_user_mail_config_table),
        ("user_search_config", _migrate_user_search_config),
        ("anonymous_messages / user_feedback / user_feedback_replies", _migrate_feedback_tables),
        # AI 聊天附件与 AI 成果物：原始 migrations/018+019+020 只在 schema_version 跨过对应版本时
        # 执行；若用户因备份恢复 / 非官方 fork / 人工改库等原因把版本号提前推高，对应表会永久缺失，
        # 这里做幂等补建兜底（建表 + 索引 + 019 的 storage_subdir 列）。
        ("chat_attachments (+ storage_subdir)", _migrate_chat_attachments),
        ("ai_artifacts", _migrate_ai_artifacts),
        ("jwt_nonces", _migrate_jwt_nonces),
        ("mcp_agent_tasks", _migrate_mcp_agent_tasks),
        ("user_mcp_servers", _migrate_user_mcp_servers),
        ("user_mcp_servers chat_scope_*", _migrate_user_mcp_chat_scopes),
        ("user_skills", _migrate_user_skills),
        ("user_skill_groups", _migrate_user_skill_groups),
        ("org_skills + task inject_user_skills", _migrate_org_skills_and_task_skills),
        ("host_service_credentials", _migrate_host_service_credentials),
        ("host_service_credentials port + nullable host_id", _migrate_service_credentials_port),
        ("host_service_credentials last_accessed_at", _migrate_service_credentials_last_accessed),
        # SCHEMA_SQL 里也声明了、但保留作双保险
        ("ai_host_knowledge", _migrate_ai_host_knowledge),
        ("ai_host_prompts", _migrate_ai_host_prompts),
        ("best_practices", _migrate_best_practices),
        # 仅 _migrate_* 才建的表（含 user_ai_model_profiles 等）
        ("user_ai_config", _migrate_user_ai_config),
        ("user_ai_config.provider 列", _migrate_user_ai_config_provider),
        ("user_ai_config.vision_enabled 列", _migrate_user_ai_config_vision),
        ("user_ai_config.ai_output_locale 列", _migrate_user_ai_config_output_locale),
        ("user_ai_model_profiles", _migrate_user_ai_model_profiles),
        ("user_ai_model_profiles 默认配置名", _migrate_user_ai_model_profiles_default_name),
        ("settings: ai_output_locale 默认", _migrate_settings_ai_output_locale),
        ("user_system_ai_usage", _migrate_user_system_ai_usage),
        ("password_reset_tokens", _migrate_password_reset_tokens),
        ("email_verification_codes", _migrate_email_verification_codes),
        ("local_shell_*", _migrate_local_shell_tables),
        ("batch_operation*", _migrate_batch_tables),
        # 列补齐
        ("users 登录锁定列", _migrate_users_login_lockout),
        ("users: 预置管理员（仅表为空）", _ensure_default_admin_user),
        ("operation_logs.source/details", _migrate_operation_logs_source),
        ("hosts/host_groups/ai_chat_sessions 历史补列", _migrate_add_columns),
        ("triggered_task_runs/scheduled_tasks/hosts 旧迁移补列", _migrate_legacy_added_columns),
        # settings 默认项
        ("settings: 邮件/site_url", _migrate_settings_smtp_site_url),
        ("settings: 邮件模板", _migrate_email_templates),
        ("settings: AI/自注册/浮窗等应用默认键", _ensure_settings_application_defaults),
    ]
    for label, fn in safety_steps:
        try:
            await fn(db)
        except Exception as e:
            logger.warning("safety-net: %s 失败: %s", label, e)
    await _ensure_default_best_practice_seeds(db)


# ── 启动时进行表/列存在性校验所需的「权威清单」──
# 与 SCHEMA_SQL + 各 migrations/*.py + _migrate_* 帮手三者保持同源；新增表请同步添加。
_REQUIRED_TABLES: tuple[str, ...] = (
    # 用户 / 凭证 / 主机 / 分组
    "users", "credentials", "hosts", "host_groups", "host_group_members",
    # 系统设置 / 日志 / 版本
    "settings", "operation_logs", "schema_version",
    # 维护历史 / Skills
    "server_maintenance_history", "skills",
    # AI 聊天 / 历史
    "ai_chat_sessions", "ai_chat_messages",
    # 主机分享 / 标签 / 登录事件
    "host_shares", "host_tags", "host_user_tags", "user_login_events",
    # 主机维度 AI 知识 / 提示词
    "ai_host_knowledge", "ai_host_prompts",
    # AI 工作流模板 / 最佳实践
    "ai_workflow_templates", "best_practices",
    # 每用户 AI 配置 / 系统额度
    "user_ai_config", "user_ai_model_profiles", "user_system_ai_usage",
    # 邮件 / 找回 / 验证码
    "user_mail_config", "password_reset_tokens", "email_verification_codes",
    # 用户搜索服务配置（GitHub / IQS / ...）
    "user_search_config",
    # 留言板与反馈
    "anonymous_messages", "user_feedback", "user_feedback_replies",
    # AI 聊天附件（018+019）与 AI 成果物（020）
    "chat_attachments", "ai_artifacts",
    # 本机管理 / 批量操作
    "local_shell_sessions", "local_shell_logs",
    "batch_operations", "batch_operation_details",
    # SSH 通道 / 触发任务 / 定时任务
    "ssh_channels",
    "triggered_tasks", "triggered_task_runs", "triggered_task_expose",
    "triggered_task_run_messages",
    "scheduled_tasks", "scheduled_task_runs", "scheduled_task_run_messages",
    # API tokens
    "api_tokens",
    # JWT 一次性 nonce（025）
    "jwt_nonces",
    # MCP 编排子任务（026）
    "mcp_agent_tasks",
    "mcp_agent_task_controls",
    "user_mcp_servers",
    "user_skills",
    "user_skill_groups",
    "org_skills",
    "host_service_credentials",
    # EventBus / Middleware（043）
    "event_rules",
    "user_middleware_config",
)


async def _check_db_schema(db: aiosqlite.Connection) -> None:
    """启动后做最终结构校验：列出所有应有表，缺失时高亮日志。
    与 _ensure_full_schema_safety_net 配合：safety-net 已尽力补建，此处只做体检。"""
    missing: list[str] = []
    for table in _REQUIRED_TABLES:
        try:
            cur = await db.execute(f"SELECT 1 FROM {table} LIMIT 1")
            await cur.close()
        except Exception as e:
            missing.append(f"{table} ({e.__class__.__name__})")
    if missing:
        logger.error(
            "数据库检查：仍有 %d 张表缺失（safety-net 已重试，请检查权限/磁盘/迁移日志）：%s",
            len(missing),
            ", ".join(missing),
        )
    else:
        logger.info("数据库检查：%d 张表齐备", len(_REQUIRED_TABLES))

    async def has_col(conn: aiosqlite.Connection, table: str, column: str) -> bool:
        try:
            cur = await conn.execute(f"PRAGMA table_info({table})")
            rows = await cur.fetchall()
            await cur.close()
            return any(r[1] == column for r in rows)
        except Exception:
            return False

    critical_columns = [
        ("ai_chat_sessions", "session_scope"),
        ("ai_chat_sessions", "session_prompt"),
        ("ai_chat_sessions", "host_id"),
        ("ai_chat_sessions", "low_interaction_mode"),
        ("ai_chat_sessions", "chat_mode"),
        ("ai_chat_sessions", "strict_allow_cache_json"),
        ("ai_chat_sessions", "session_runtime_json"),
        ("hosts", "credential_id"),
        ("hosts", "host_type"),
        ("hosts", "aliases"),
        ("hosts", "remark"),
        ("operation_logs", "source"),
        ("operation_logs", "details"),
        ("user_ai_config", "provider"),
        ("user_ai_config", "vision_enabled"),
        ("user_ai_config", "ai_output_locale"),
        ("user_ai_config", "active_profile_id"),
        ("user_ai_model_profiles", "user_id"),
        ("user_ai_model_profiles", "name"),
        ("users", "email"),
        ("users", "failed_login_attempts"),
        ("users", "locked_until"),
        ("users", "skills_enabled"),
        ("skills", "deprecated"),
        ("user_skills", "allowed_tools"),
        ("scheduled_tasks", "inject_user_skills"),
        ("triggered_tasks", "inject_user_skills"),
    ]
    missing_cols = []
    for table, col in critical_columns:
        if not await has_col(db, table, col):
            missing_cols.append(f"{table}.{col}")
    if missing_cols:
        logger.error("数据库检查：关键列缺失：%s", ", ".join(missing_cols))


# 全新库写入 best_practices 的只读参考条目（source=system_seed，按 title+source 幂等去重）
_FRESH_INSTALL_BEST_PRACTICE_SEEDS: tuple[tuple[str, str, str, str], ...] = (
    (
        "新安装必做：安全与可用性",
        "系统运维",
        """## 账号安全
- 默认管理员 **admin** / **admin123** 仅用于首次登录，请立刻在界面中修改密码。

## AI（可选）
- 在侧栏「模型配置」（`/model-config`）填写 Base URL / API Key / 模型；管理员可在系统设置写全局默认值。
- **系统提示词（ai_system_prompt）留空** 时，产品使用代码内置的完整毛竹（Moso）主助手提示词（与版本同步更新）；仅当您需要**完全自定义**主助手行为时，再在设置中填写全文覆盖。

## 邮件与其它
- 配置 SMTP 后可使用找回密码、通知管理员等功能；时区键 **site_timezone** 默认为 Asia/Shanghai（可在设置中调整）。""",
        "system_seed",
    ),
    (
        "内置 Skills 与数据库 skills 表",
        "系统运维",
        """毛竹 的大量能力由代码内 `ai_skills` 注册；数据库 **skills** 表中的条目为历史/辅助用途，与界面「技能」列表相关。
新装库会预置少量示例行；若列表为空或行为以代码为准，属正常现象。""",
        "system_seed",
    ),
)


def default_application_settings_items() -> list[tuple[str, str]]:
    """全站 `settings` 默认值（与首次 `run_initial_schema` + 015/016/017 注入项对齐）。

    - 供 `run_initial_schema` 与安全网 `_ensure_settings_application_defaults` 共用。
    - 安全网仅 `INSERT OR IGNORE`：不覆盖管理员或用户已写入的键。
    """
    import config as cfg

    return list(
        {
            "self_register": "true",
            "ai_api_key": str(getattr(cfg, "AI_API_KEY", "") or ""),
            "ai_base_url": str(getattr(cfg, "AI_BASE_URL", "") or ""),
            "ai_model": str(getattr(cfg, "AI_MODEL", "") or ""),
            "ai_system_prompt": "",
            "ai_auto_approve": "false",
            "ai_assistant_enabled": "false",
            "ai_context_size": str(int(getattr(cfg, "AI_CONTEXT_SIZE", 0))),
            "ai_agent_max_steps": str(int(getattr(cfg, "AGENT_MAX_STEPS", 100))),
            "ai_assistant_max_rounds": str(int(getattr(cfg, "ASSISTANT_MAX_ROUNDS", 100))),
            "login_announcement_md": "",
            "site_url": "",
            "site_timezone": "Asia/Shanghai",
            "smtp_host": "",
            "smtp_port": "587",
            "smtp_use_tls": "true",
            "smtp_user": "",
            "smtp_password": "",
            "smtp_from": "",
            "smtp_use_ssl": "",
            "notify_admin_on_user_feedback": "false",
            "login_widget_message_board_enabled": "true",
            "login_widget_public_messages_enabled": "true",
            "ai_output_locale": "",
            "credentials_vault_enabled": "false",
            "system_ai_usage_limit": str(int(getattr(cfg, "SYSTEM_AI_USAGE_LIMIT", 2000))),
        }.items()
    )


async def _ensure_settings_application_defaults(db: aiosqlite.Connection) -> None:
    """补全缺失的 `settings` 行（不覆盖已有键）。

    背景：部分安装/恢复路径未完整执行 `000_initial`，安全网此前只补了 SMTP/模板等，
    未补 `ai_*` / `self_register` / 登录浮窗等，导致「系统设置」里缺少大模型相关键。
    """
    for k, v in default_application_settings_items():
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (k, str(v)),
        )
    await db.commit()


async def _upsert_fresh_install_best_practice_seeds(
    db: aiosqlite.Connection, *, admin_user_id: int | None
) -> None:
    """按 title + source 幂等插入参考「最佳实践」；一次查询已存在的 system_seed 标题，仅插入缺失项。

    仅在确有新插入时 commit，避免每次启动对只读路径做无意义提交。
    """
    try:
        cur = await db.execute(
            "SELECT title FROM best_practices WHERE source = 'system_seed'"
        )
        existing = {str(row[0]) for row in await cur.fetchall()}
    except Exception as e:
        logger.warning("读取 best_practices（system_seed）失败，跳过默认种子: %s", e)
        return
    inserted = False
    for title, category, content, source in _FRESH_INSTALL_BEST_PRACTICE_SEEDS:
        if title in existing:
            continue
        try:
            await db.execute(
                "INSERT INTO best_practices (title, category, content, source, created_by) VALUES (?, ?, ?, ?, ?)",
                (title, category, content, source, admin_user_id),
            )
            inserted = True
        except Exception as e:
            logger.warning("写入默认最佳实践失败（title=%s）: %s", title, e)
    if inserted:
        await db.commit()


async def _ensure_default_best_practice_seeds(db: aiosqlite.Connection) -> None:
    """启动安全网：老库若缺少任一 system_seed 参考条则补写（不覆盖、不删除用户内容）。

    具体缺失检测与幂等插入由 `_upsert_fresh_install_best_practice_seeds` 统一完成；无新插入时不 commit。
    """
    try:
        cur = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='best_practices' LIMIT 1"
        )
        if not await cur.fetchone():
            return
        cur = await db.execute(
            "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
        )
        row = await cur.fetchone()
        admin_id = int(row[0]) if row else None
        await _upsert_fresh_install_best_practice_seeds(db, admin_user_id=admin_id)
    except Exception as e:
        logger.warning("safety-net: 默认最佳实践种子补写失败: %s", e)


async def run_initial_schema(db: aiosqlite.Connection) -> None:
    """
    执行版本 0 -> 1 的完整初始化：建表、历史迁移补列/补表、默认管理员与配置。

    **数据种子（业务空库中的非空表）**：
    - ``users``：默认管理员 **admin**（密码 **admin123**）。
    - ``settings``：自注册开关、全局 AI 默认值（来自环境/config）、SMTP 占位、``ai_system_prompt`` 空串（空则运行时改用代码内置完整提示词）、步数/上下文等；与 ``default_application_settings_items()`` 同源，启动时安全网亦会 ``INSERT OR IGNORE`` 补漏。
    - ``skills``：少量示例技能行（更多能力以代码注册为准）。
    - ``best_practices``：若干 ``source=system_seed`` 的说明条目（幂等）。
    - 其余业务表初始为空；后续迁移（001…）会继续补 ``settings`` 键、登录浮窗开关等。

    仅由 database.migrations 中的 000_initial 脚本调用，普通启动请使用 init_db()。
    """
    await db.executescript(SCHEMA_SQL)
    await db.commit()
    await _migrate_add_columns(db)
    await _migrate_ai_host_knowledge(db)
    await _migrate_ai_host_prompts(db)
    await _migrate_best_practices(db)
    await _migrate_operation_logs_source(db)
    await _migrate_hosts_username_nullable(db)
    await _migrate_user_ai_config(db)
    await _migrate_user_ai_config_provider(db)
    await _migrate_user_ai_config_vision(db)
    await _migrate_user_ai_config_output_locale(db)
    await _migrate_user_ai_model_profiles(db)
    await _migrate_user_ai_model_profiles_default_name(db)
    await _migrate_settings_ai_output_locale(db)
    await _migrate_user_system_ai_usage(db)
    await _migrate_users_login_lockout(db)
    await _migrate_password_reset_tokens(db)
    await _migrate_email_verification_codes(db)
    await _migrate_settings_smtp_site_url(db)
    await _migrate_email_templates(db)
    await _migrate_local_shell_tables(db)
    await _migrate_batch_tables(db)
    await _migrate_session_prompt_scp(db)
    await _migrate_user_search_config(db)
    await _migrate_feedback_tables(db)
    await _migrate_owner_backfill(db)
    import bcrypt as _bcrypt
    try:
        pw_hash = _bcrypt.hashpw(b"admin123", _bcrypt.gensalt()).decode()
        await db.execute(
            "INSERT INTO users (username, display_name, password_hash, role, status) VALUES (?, ?, ?, ?, ?)",
            ("admin", "管理员", pw_hash, "admin", "active"),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        pass
    for k, v in default_application_settings_items():
        try:
            await db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
        except aiosqlite.IntegrityError:
            pass
    await db.commit()
    for code, name, desc in [
        ("ssh_exec", "SSH 执行命令", "在指定主机上执行一条 Shell 命令"),
        ("scp_push", "SCP 推送文件", "通过 SCP 向主机推送文件或目录"),
        ("batch_create", "批量操作", "向多台主机下发命令/上传/脚本/重启；脚本与资源放在文件系统 web/fs（如 scripts/）"),
    ]:
        try:
            await db.execute(
                """INSERT INTO skills (code, name, description, parameters_schema, enabled)
                   VALUES (?, ?, ?, ?, 1)""",
                (code, name, desc, "{}"),
            )
        except aiosqlite.IntegrityError:
            pass
    await db.commit()
    admin_id: int | None = None
    try:
        cur = await db.execute("SELECT id FROM users WHERE username = 'admin' ORDER BY id LIMIT 1")
        row = await cur.fetchone()
        if row:
            admin_id = int(row[0])
    except Exception:
        pass
    await _upsert_fresh_install_best_practice_seeds(db, admin_user_id=admin_id)


async def connect_db():
    """仅建立数据库连接（不执行迁移）。多 worker 时由各进程在 lifespan 中调用，迁移已在主进程完成。"""
    global _db
    if _db is not None:
        return
    _db = await aiosqlite.connect(config.DATABASE_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA foreign_keys = ON")
    await apply_sqlite_concurrency_settings(_db)


async def _classify_edgeops_db_file(path: str) -> str:
    """判断路径上的 SQLite 文件与 毛竹 的关系，供启动时选择日志与校验。

    返回：
    - ``absent``：路径不存在。
    - ``empty``：大小为 0、无法打开为 SQLite、或无法读取目录/无用户表（将走完整 0→最新 迁移；若文件损坏，后续迁移会再报错）。
    - ``edgeops``：存在 ``schema_version`` 或 ``users`` 表（含无版本号的旧库），视为本系统库并走升级流水线。
    - ``foreign``：已成功读出目录且存在业务表，但**既无** ``schema_version`` **也无** ``users``（极少见：其它应用恰好仅有同名表时会误判，见下）。

    说明：仅凭表名推断，若误将第三方库（也有 ``users``）指到 ``EDGEOPS_DB``，仍会被当作 edgeops；运维应保证路径专用于本系统。
    """
    if not path or not os.path.exists(path):
        return "absent"
    try:
        size = os.path.getsize(path)
    except OSError:
        return "absent"
    if size == 0:
        return "empty"
    try:
        async with aiosqlite.connect(path) as conn:
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            tables = {row[0] for row in await cur.fetchall()}
    except Exception as e:
        logger.warning("无法打开或读取 SQLite 目录（将按空库尝试完整初始化）: %s — %s", path, e)
        return "empty"
    if not tables:
        return "empty"
    if "schema_version" in tables or "users" in tables:
        return "edgeops"
    return "foreign"


async def _run_database_init_pipeline(conn: aiosqlite.Connection) -> None:
    """在已打开的 aiosqlite 连接上执行迁移、安全网与结构检查（与 init_db 核心逻辑一致）。"""
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await apply_sqlite_concurrency_settings(conn)
    from database.migrations import run_upgrades

    await run_upgrades(conn)
    await _ensure_full_schema_safety_net(conn)
    await _check_db_schema(conn)


async def init_db(*, database_path: str | None = None) -> None:
    """初始化数据库：按版本号执行升级脚本至最新，再跑一遍幂等安全网，最后做一次结构校验。

    - 默认：使用 ``config.DATABASE_PATH``，连接写入全局 ``_db``（应用启动路径）。
    - 若传入 ``database_path``：仅对该文件执行完整流水线，执行完毕后关闭连接且不修改全局 ``_db``，
      供 ``scripts/bootstrap_fresh_db.py`` 等 CLI 一键全新建库（不做路径归属校验）。

    **应用启动**（未传 ``database_path``）时：先检查 ``config.DATABASE_PATH`` —— 无文件、空文件或无表
    视为全新初始化；已存在且含 ``schema_version`` 或 ``users`` 则视为本系统库并仅按需升级；否则拒绝启动。

    ── 三段流水线说明 ──
    1) run_upgrades：按 schema_version 顺序跑 0NN_*.py，把版本号推到最新。
    2) _ensure_full_schema_safety_net：每次启动都跑的「补漏网」。处理两类历史漏洞：
       a. 部分表只在 run_initial_schema 里被 _migrate_* 帮手创建，没有独立 0NN_*.py，
          老库被识别为 version=1 后就再也补不上（如 user_ai_config / batch_operations 等）。
       b. 个别后加的迁移脚本若因人为干预 / 备份恢复导致 schema_version 已超过其编号但表却
          不存在（如 ai_host_prompts），仅靠 run_upgrades 永远不会回头补建。
       这一步全是 IF NOT EXISTS / has_column，幂等无副作用。
    3) _check_db_schema：体检报告，缺表/缺列时打 ERROR 日志，便于运维排查。
    """
    global _db
    path = database_path if database_path is not None else config.DATABASE_PATH
    if database_path is not None:
        conn = await aiosqlite.connect(path)
        try:
            await _run_database_init_pipeline(conn)
        finally:
            await conn.close()
        return

    kind = await _classify_edgeops_db_file(path)
    if kind == "foreign":
        raise RuntimeError(
            f"数据库文件已存在，但无法识别为 毛竹 库（缺少 schema_version / users 等标识表）: {path}。"
            "请检查 EDGEOPS_DB 是否指向错误文件；若需全新安装请更换空路径或先删除该文件后使用 scripts/bootstrap_fresh_db.py。"
        )
    if kind in ("absent", "empty"):
        logger.info("未检测到现有 毛竹 数据库或库为空，将执行完整初始化（迁移至当前最新版本）。")
    else:
        logger.info("已检测到 毛竹 数据库，将按需升级并执行结构检查。")

    _db = await aiosqlite.connect(path)
    await _run_database_init_pipeline(_db)


async def get_db() -> aiosqlite.Connection:
    if _db is None:
        await connect_db()
    return _db


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None
