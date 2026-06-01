"""每用户自定义 MCP 服务器配置（stdio / SSE / Streamable HTTP）。"""


async def upgrade(db):
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
