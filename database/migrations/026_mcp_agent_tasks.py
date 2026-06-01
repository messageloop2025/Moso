"""MCP 编排式 ops：后台子 Agent 任务表（仅 MCP 通道，不影响 integration / 网页 AI）。"""


async def upgrade(db):
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
