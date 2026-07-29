"""数据库迁移 043：新增 event_rules 表、user_middleware_config 表，
扩展 ai_chat_sessions 的 state_history_json 和 token_usage_json 字段。"""


async def upgrade(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS event_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            skill_id INTEGER REFERENCES user_skills(id) ON DELETE SET NULL,
            event_name TEXT NOT NULL,
            matcher TEXT NOT NULL DEFAULT '*',
            decision TEXT NOT NULL DEFAULT 'allow',
            reason TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 0,
            action_config TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_middleware_config (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            middleware_name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            config_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (user_id, middleware_name)
        );
        """
    )

    # 为 ai_chat_sessions 添加新列（IF NOT EXISTS 包装检查）
    col_checks = [
        ("ai_chat_sessions", "state_history_json",
         "ALTER TABLE ai_chat_sessions ADD COLUMN state_history_json TEXT NOT NULL DEFAULT '[]'"),
        ("ai_chat_sessions", "token_usage_json",
         "ALTER TABLE ai_chat_sessions ADD COLUMN token_usage_json TEXT NOT NULL DEFAULT '{}'"),
    ]
    for table, col, sql in col_checks:
        try:
            cur = await db.execute(f"PRAGMA table_info({table})")
            rows = await cur.fetchall()
            await cur.close()
            if any(r[1] == col for r in rows):
                continue
            await db.execute(sql)
        except Exception:
            pass

    await db.commit()
