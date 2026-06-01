"""每用户 Skills：users.skills_enabled 开关 + user_skills 元数据表。"""


async def upgrade(db):
    try:
        await db.execute(
            "ALTER TABLE users ADD COLUMN skills_enabled INTEGER NOT NULL DEFAULT 0"
        )
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
