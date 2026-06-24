-- 毛竹 fresh install SQL snapshot
-- schema_version target after apply: 35
-- generated_at_utc: 2026-06-24T04:50:40Z
-- Regenerate: python scripts/regenerate_fresh_install_sql.py

BEGIN TRANSACTION;
CREATE TABLE ai_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_id INTEGER REFERENCES ai_chat_sessions(id) ON DELETE SET NULL,
            message_id INTEGER REFERENCES ai_chat_messages(id) ON DELETE SET NULL,
            title TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'bundle',     -- 'single_file' | 'bundle'
            storage_subdir TEXT NOT NULL DEFAULT '', -- ARTIFACT_SUBDIR/ 下的相对目录，如 '2026/04/22/abcd1234'
            entry_file TEXT NOT NULL DEFAULT '',     -- bundle 首页 (index.html / report.md ...) 或单文件名
            file_count INTEGER NOT NULL DEFAULT 0,
            total_bytes INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
CREATE TABLE ai_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES ai_chat_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE ai_chat_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
    title TEXT DEFAULT '新会话',
    session_prompt TEXT DEFAULT '',
    session_scope TEXT DEFAULT 'default',
    low_interaction_mode TEXT DEFAULT 'false',
    session_runtime_json TEXT NOT NULL DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE ai_host_knowledge (
    host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT DEFAULT '',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (host_id, user_id)
);
CREATE TABLE ai_host_prompts (
    host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT DEFAULT '',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (host_id, user_id)
);
CREATE TABLE ai_workflow_templates (
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
CREATE TABLE anonymous_messages (
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
CREATE TABLE api_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL DEFAULT '',
            token_hash TEXT NOT NULL UNIQUE,
            token_prefix TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_used_at DATETIME
        );
CREATE TABLE batch_operation_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL REFERENCES batch_operations(id) ON DELETE CASCADE,
            host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
            status TEXT DEFAULT 'pending',
            result TEXT,
            started_at DATETIME,
            completed_at DATETIME
        );
CREATE TABLE batch_operations (
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
        );
CREATE TABLE best_practices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT DEFAULT '',
    content TEXT NOT NULL,
    source TEXT DEFAULT 'manual',
    created_by INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO "best_practices" VALUES(1,'新安装必做：安全与可用性','系统运维','## 账号安全
- 默认管理员 **admin** / **admin123** 仅用于首次登录，请立刻在界面中修改密码。

## AI（可选）
- 在侧栏「模型配置」（`/model-config`）填写 Base URL / API Key / 模型；管理员可在系统设置写全局默认值。
- **系统提示词（ai_system_prompt）留空** 时，产品使用代码内置的完整毛竹（Moso）主助手提示词（与版本同步更新）；仅当您需要**完全自定义**主助手行为时，再在设置中填写全文覆盖。

## 邮件与其它
- 配置 SMTP 后可使用找回密码、通知管理员等功能；时区键 **site_timezone** 默认为 Asia/Shanghai（可在设置中调整）。','system_seed',1,'2026-06-24 04:50:40','2026-06-24 04:50:40');
INSERT INTO "best_practices" VALUES(2,'内置 Skills 与数据库 skills 表','系统运维','毛竹 的大量能力由代码内 `ai_skills` 注册；数据库 **skills** 表中的条目为历史/辅助用途，与界面「技能」列表相关。
新装库会预置少量示例行；若列表为空或行为以代码为准，属正常现象。','system_seed',1,'2026-06-24 04:50:40','2026-06-24 04:50:40');
CREATE TABLE chat_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_id INTEGER REFERENCES ai_chat_sessions(id) ON DELETE SET NULL,
            message_id INTEGER REFERENCES ai_chat_messages(id) ON DELETE SET NULL,
            original_name TEXT DEFAULT '',
            mime_type TEXT DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL DEFAULT 'binary',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        , storage_subdir TEXT NOT NULL DEFAULT '', ai_description TEXT NOT NULL DEFAULT '', ai_description_model TEXT NOT NULL DEFAULT '', ai_description_updated_at DATETIME);
CREATE TABLE credentials (
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
CREATE TABLE email_verification_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            purpose TEXT NOT NULL,
            expires_at DATETIME NOT NULL,
            used_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
CREATE TABLE host_group_members (
    host_id INTEGER REFERENCES hosts(id) ON DELETE CASCADE,
    group_id INTEGER REFERENCES host_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (host_id, group_id)
);
CREATE TABLE host_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    parent_id INTEGER REFERENCES host_groups(id),
    created_by INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE host_service_credentials (
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
        );
CREATE TABLE host_shares (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    shared_with_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    revoked_at DATETIME
);
CREATE TABLE host_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    color TEXT DEFAULT '',
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE host_user_tags (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES host_tags(id) ON DELETE CASCADE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, host_id, tag_id)
);
CREATE TABLE hosts (
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
, host_type TEXT DEFAULT '未知', host_version TEXT DEFAULT '未知', host_shell TEXT DEFAULT NULL, host_package_manager TEXT DEFAULT NULL, aliases TEXT DEFAULT '[]', remark TEXT DEFAULT '');
CREATE TABLE jwt_nonces (
            jti TEXT PRIMARY KEY,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
CREATE TABLE local_shell_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES local_shell_sessions(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            content TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
CREATE TABLE local_shell_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT DEFAULT '本机会话',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
CREATE TABLE mcp_agent_task_controls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES mcp_agent_tasks(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            consumed INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
CREATE TABLE mcp_agent_tasks (
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
        );
CREATE TABLE operation_logs (
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
CREATE TABLE password_reset_tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            expires_at DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
CREATE TABLE scheduled_task_run_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES scheduled_task_runs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE scheduled_task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    run_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'running',
    session_snapshot_id TEXT,
    log_summary TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE scheduled_tasks (
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
, enabled INTEGER NOT NULL DEFAULT 1, notify_email_to TEXT DEFAULT '');
CREATE TABLE schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 0
);
INSERT INTO "schema_version" VALUES(1,35);
CREATE TABLE server_maintenance_history (
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
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO "settings" VALUES('ai_output_locale','','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('site_url','','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('site_timezone','Asia/Shanghai','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('smtp_host','','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('smtp_port','587','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('smtp_use_tls','true','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('smtp_user','','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('smtp_password','','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('smtp_from','','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('smtp_use_ssl','','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('email_notification_subject','毛竹通知','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('email_template_lock_body','{{username}}用户您好，您的毛竹账号已锁定，可以通过登录页面找回功能解锁。

毛竹','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('email_template_unlock_body','{{username}}用户您好，你的毛竹账号解锁成功。

毛竹','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('email_template_suspend_body','{{username}}用户您好，你的毛竹账号被管理员暂时停止使用。请回复邮件以沟通解决。

毛竹','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('email_template_restore_body','{{username}}用户您好，你的毛竹账号恢复使用。

毛竹','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('notify_admin_on_user_feedback','false','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('self_register','true','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('ai_api_key','','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('ai_base_url','https://dashscope.aliyuncs.com/compatible-mode/v1','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('ai_model','qwen3.6-plus','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('ai_system_prompt','','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('ai_auto_approve','false','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('ai_assistant_enabled','false','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('ai_context_size','0','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('ai_agent_max_steps','100','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('ai_assistant_max_rounds','100','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('login_announcement_md','','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('login_widget_message_board_enabled','true','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('login_widget_public_messages_enabled','true','2026-06-24 04:50:40');
INSERT INTO "settings" VALUES('credentials_vault_enabled','false','2026-06-24 04:50:40');
CREATE TABLE skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    parameters_schema TEXT,
    enabled INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO "skills" VALUES(1,'ssh_exec','SSH 执行命令','在指定主机上执行一条 Shell 命令','{}',1,'2026-06-24 04:50:40');
INSERT INTO "skills" VALUES(2,'scp_push','SCP 推送文件','通过 SCP 向主机推送文件或目录','{}',1,'2026-06-24 04:50:40');
INSERT INTO "skills" VALUES(3,'batch_create','批量操作','向多台主机下发命令/上传/脚本/重启；脚本与资源放在文件系统 web/fs（如 scripts/）','{}',1,'2026-06-24 04:50:40');
CREATE TABLE ssh_channels (
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
CREATE TABLE triggered_task_expose (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES triggered_tasks(id) ON DELETE CASCADE,
    expose_code TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE triggered_task_run_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES triggered_task_runs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE triggered_task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES triggered_tasks(id) ON DELETE CASCADE,
    triggered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    triggered_by_type TEXT,
    triggered_by_id TEXT,
    status TEXT DEFAULT 'pending',
    instruction TEXT,
    log_summary TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
, caller_task_name TEXT DEFAULT '');
CREATE TABLE triggered_tasks (
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
CREATE TABLE user_ai_config (
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
        , ai_output_locale TEXT DEFAULT '', active_profile_id INTEGER REFERENCES user_ai_model_profiles(id));
CREATE TABLE user_ai_model_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
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
            ai_output_locale TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, name)
        );
CREATE TABLE user_feedback (
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
CREATE TABLE user_feedback_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_id INTEGER NOT NULL REFERENCES user_feedback(id) ON DELETE CASCADE,
    admin_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    is_ai_drafted INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE user_login_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    login_type TEXT DEFAULT 'password',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE user_mail_config (
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
        );
CREATE TABLE user_mcp_servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            transport TEXT NOT NULL DEFAULT 'stdio',
            config_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            chat_enabled INTEGER NOT NULL DEFAULT 1,
            tool_count INTEGER NOT NULL DEFAULT 0,
            last_test_ok INTEGER,
            last_test_at DATETIME,
            last_error TEXT NOT NULL DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, chat_scope_web INTEGER NOT NULL DEFAULT 1, chat_scope_host INTEGER NOT NULL DEFAULT 1, chat_scope_integration INTEGER NOT NULL DEFAULT 1,
            UNIQUE(user_id, name)
        );
CREATE TABLE user_search_config (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    api_key TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    extra TEXT DEFAULT '{}',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, provider)
);
CREATE TABLE user_skills (
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
        );
CREATE TABLE user_system_ai_usage (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            call_count INTEGER NOT NULL DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'user',
    status TEXT DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_login DATETIME
, email TEXT DEFAULT '', failed_login_attempts INTEGER DEFAULT 0, locked_until DATETIME, skills_enabled INTEGER NOT NULL DEFAULT 0);
INSERT INTO "users" VALUES(1,'admin','管理员','$2b$12$NX.4bzp76zhk9CRoleHVX.bYIu437VZXdXfR4aI8KKgwPzQdRlWiW','admin','active','2026-06-24 04:50:40','2026-06-24 04:50:40',NULL,'',0,NULL,0);
CREATE UNIQUE INDEX idx_credentials_code ON credentials(code);
CREATE INDEX idx_hosts_credential ON hosts(credential_id);
CREATE INDEX idx_operation_logs_created_at ON operation_logs(created_at);
CREATE INDEX idx_operation_logs_user ON operation_logs(user_id);
CREATE INDEX idx_maintenance_host ON server_maintenance_history(host);
CREATE INDEX idx_ai_sessions_user ON ai_chat_sessions(user_id);
CREATE INDEX idx_ai_sessions_host ON ai_chat_sessions(host_id);
CREATE INDEX idx_ai_messages_session ON ai_chat_messages(session_id);
CREATE UNIQUE INDEX idx_host_shares_unique ON host_shares(host_id, shared_with_user_id);
CREATE INDEX idx_host_shares_owner ON host_shares(owner_user_id);
CREATE INDEX idx_host_shares_receiver ON host_shares(shared_with_user_id);
CREATE UNIQUE INDEX idx_host_tags_user_name ON host_tags(created_by, name);
CREATE INDEX idx_host_tags_user ON host_tags(created_by);
CREATE INDEX idx_host_user_tags_user_host ON host_user_tags(user_id, host_id);
CREATE INDEX idx_host_user_tags_tag ON host_user_tags(tag_id);
CREATE INDEX idx_user_login_events_created_at ON user_login_events(created_at);
CREATE INDEX idx_user_login_events_user ON user_login_events(user_id);
CREATE INDEX idx_ai_host_knowledge_user ON ai_host_knowledge(user_id);
CREATE INDEX idx_ai_host_prompts_user ON ai_host_prompts(user_id);
CREATE INDEX idx_ai_workflow_templates_owner ON ai_workflow_templates(owner_user_id);
CREATE UNIQUE INDEX idx_ai_workflow_templates_owner_name ON ai_workflow_templates(owner_user_id, name);
CREATE INDEX idx_anon_msg_parent ON anonymous_messages(parent_id);
CREATE INDEX idx_anon_msg_status ON anonymous_messages(status);
CREATE INDEX idx_anon_msg_show ON anonymous_messages(show_on_login, status);
CREATE INDEX idx_user_feedback_user ON user_feedback(user_id, status);
CREATE INDEX idx_user_feedback_status ON user_feedback(status, admin_read_at);
CREATE INDEX idx_user_feedback_created ON user_feedback(created_at);
CREATE INDEX idx_user_feedback_replies_fb ON user_feedback_replies(feedback_id, created_at);
CREATE INDEX idx_user_search_config_provider ON user_search_config(provider);
CREATE INDEX idx_best_practices_category ON best_practices(category);
CREATE INDEX idx_user_ai_model_profiles_user ON user_ai_model_profiles(user_id);
CREATE INDEX idx_pwd_reset_user ON password_reset_tokens(user_id);
CREATE INDEX idx_pwd_reset_expires ON password_reset_tokens(expires_at);
CREATE INDEX idx_email_ver_user ON email_verification_codes(user_id);
CREATE INDEX idx_email_ver_expires ON email_verification_codes(expires_at);
CREATE INDEX idx_local_shell_sessions_user ON local_shell_sessions(user_id);
CREATE INDEX idx_local_shell_logs_session ON local_shell_logs(session_id);
CREATE INDEX idx_batch_details_batch ON batch_operation_details(batch_id);
CREATE INDEX idx_ssh_channels_owner ON ssh_channels(owner_type, owner_id);
CREATE INDEX idx_ssh_channels_user ON ssh_channels(user_id);
CREATE INDEX idx_triggered_tasks_user ON triggered_tasks(user_id);
CREATE INDEX idx_triggered_task_runs_task ON triggered_task_runs(task_id);
CREATE UNIQUE INDEX idx_triggered_task_expose_task_code ON triggered_task_expose(task_id, expose_code);
CREATE INDEX idx_scheduled_tasks_user ON scheduled_tasks(user_id);
CREATE INDEX idx_scheduled_task_runs_task ON scheduled_task_runs(task_id);
CREATE INDEX idx_scheduled_task_run_messages_run ON scheduled_task_run_messages(run_id);
CREATE INDEX idx_triggered_task_run_messages_run ON triggered_task_run_messages(run_id);
CREATE INDEX idx_api_tokens_user ON api_tokens(user_id);
CREATE INDEX idx_chat_attachments_user ON chat_attachments(user_id, created_at);
CREATE INDEX idx_chat_attachments_session ON chat_attachments(session_id);
CREATE INDEX idx_chat_attachments_message ON chat_attachments(message_id);
CREATE INDEX idx_ai_artifacts_user ON ai_artifacts(user_id, created_at);
CREATE INDEX idx_ai_artifacts_session ON ai_artifacts(session_id);
CREATE INDEX idx_ai_artifacts_message ON ai_artifacts(message_id);
CREATE INDEX idx_jwt_nonces_created ON jwt_nonces(created_at);
CREATE INDEX idx_mcp_agent_tasks_user ON mcp_agent_tasks(user_id, status);
CREATE INDEX idx_mcp_agent_tasks_session ON mcp_agent_tasks(session_id);
CREATE INDEX idx_mcp_agent_task_controls_task ON mcp_agent_task_controls(task_id, consumed);
CREATE INDEX idx_user_mcp_servers_user ON user_mcp_servers(user_id, enabled);
CREATE INDEX idx_user_skills_user ON user_skills(user_id, enabled);
CREATE INDEX idx_hsc_user ON host_service_credentials(user_id);
CREATE INDEX idx_hsc_user_lookup ON host_service_credentials(user_id, service, address, port, service_username);
CREATE INDEX idx_hsc_user_last_access ON host_service_credentials(user_id, last_accessed_at);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('users',1);
INSERT INTO "sqlite_sequence" VALUES('skills',3);
INSERT INTO "sqlite_sequence" VALUES('best_practices',2);
COMMIT;
