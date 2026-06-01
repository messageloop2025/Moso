"""从版本 9 升级到 10：新增主机分享表 host_shares。"""


async def upgrade(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS host_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
            owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            shared_with_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE SET NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            revoked_at DATETIME
        )
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_host_shares_unique ON host_shares(host_id, shared_with_user_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_host_shares_owner ON host_shares(owner_user_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_host_shares_receiver ON host_shares(shared_with_user_id)"
    )
    await db.commit()
