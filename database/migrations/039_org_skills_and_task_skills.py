"""组织 Skill 库 org_skills；scheduled_tasks / triggered_tasks 可选注入 User Skills。"""


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
        CREATE TABLE IF NOT EXISTS org_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            slash_name TEXT NOT NULL DEFAULT '',
            allowed_tools TEXT NOT NULL DEFAULT '',
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_org_skills_enabled ON org_skills(enabled)"
    )
    for table in ("scheduled_tasks", "triggered_tasks"):
        if await _table_exists(db, table) and not await _has_column(
            db, table, "inject_user_skills"
        ):
            await db.execute(
                f"ALTER TABLE {table} ADD COLUMN inject_user_skills INTEGER NOT NULL DEFAULT 0"
            )
    await db.commit()
