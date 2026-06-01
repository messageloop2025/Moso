"""从版本 7 升级到 8：每用户发信配置（user_mail_config）；定时任务结果通知邮箱（notify_email_to）。"""


async def upgrade(db):
    await db.execute("""
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
    """)

    async def has_column(table: str, column: str) -> bool:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return any(r[1] == column for r in rows)

    if not await has_column("scheduled_tasks", "notify_email_to"):
        await db.execute("ALTER TABLE scheduled_tasks ADD COLUMN notify_email_to TEXT DEFAULT ''")
    await db.commit()
