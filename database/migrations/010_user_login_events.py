"""从版本 10 升级到 11：新增用户登录事件表 user_login_events。"""


async def upgrade(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_login_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            login_type TEXT DEFAULT 'password',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_login_events_created_at ON user_login_events(created_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_login_events_user ON user_login_events(user_id)"
    )
    await db.commit()
