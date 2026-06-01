"""新增主机标签：每用户私有标签 + 按用户维度的主机标签关联。"""


async def upgrade(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS host_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            color TEXT DEFAULT '',
            created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_host_tags_user_name ON host_tags(created_by, name)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_host_tags_user ON host_tags(created_by)"
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS host_user_tags (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES host_tags(id) ON DELETE CASCADE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, host_id, tag_id)
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_host_user_tags_user_host ON host_user_tags(user_id, host_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_host_user_tags_tag ON host_user_tags(tag_id)"
    )
    await db.commit()
