"""
数据库升级脚本：从版本 1 升级到版本 2。

新增：SSH Channel（TTY 通道）、触发任务、定时任务及执行历史表。
详见 docs/SSH通道与后台任务设计.md
"""


async def upgrade(db):
    """将数据库从版本 1 升级到版本 2。"""
    await db.executescript("""
-- SSH 通道（TTY，按用户会话或后台任务隔离）
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

-- 触发任务
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

-- 触发任务执行历史
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

-- 触发任务暴露接口（供定时任务发现）
CREATE TABLE IF NOT EXISTS triggered_task_expose (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES triggered_tasks(id) ON DELETE CASCADE,
    expose_code TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_triggered_task_expose_task_code ON triggered_task_expose(task_id, expose_code);

-- 定时任务
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

-- 定时任务执行历史
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

-- 定时任务执行会话历史（类似 AI 会话消息，供 AI 查询当前次执行）
CREATE TABLE IF NOT EXISTS scheduled_task_run_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES scheduled_task_runs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_scheduled_task_run_messages_run ON scheduled_task_run_messages(run_id);
""")
    await db.commit()
