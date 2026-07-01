"""user_skill_groups 表 + user_skills.group_id。"""


async def _has_column(db, table: str, column: str) -> bool:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    return any(r[1] == column for r in rows)


async def _table_exists(db, table: str) -> bool:
    cur = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    )
    row = await cur.fetchone()
    await cur.close()
    return row is not None


async def upgrade(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_skill_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, name)
        )
        """
    )
    if await _table_exists(db, "user_skills") and not await _has_column(db, "user_skills", "group_id"):
        await db.execute(
            "ALTER TABLE user_skills ADD COLUMN group_id INTEGER "
            "REFERENCES user_skill_groups(id) ON DELETE SET NULL"
        )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_skill_groups_user "
        "ON user_skill_groups(user_id, sort_order)"
    )
    if await _table_exists(db, "user_skills") and await _has_column(db, "user_skills", "group_id"):
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_skills_group ON user_skills(user_id, group_id)"
        )
    await db.commit()
