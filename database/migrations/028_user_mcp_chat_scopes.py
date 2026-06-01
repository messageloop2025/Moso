"""用户 MCP：按场景启用（网页全局 / 主机 AI / 集成通道）。"""


async def upgrade(db):
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
