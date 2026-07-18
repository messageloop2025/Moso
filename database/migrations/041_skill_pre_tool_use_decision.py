"""user_skills.pre_tool_use_decision：DB matcher 命中时的 allow/deny/ask（默认 ask）。"""


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
    if await _table_exists(db, "user_skills") and not await _has_column(
        db, "user_skills", "pre_tool_use_decision"
    ):
        await db.execute(
            "ALTER TABLE user_skills ADD COLUMN pre_tool_use_decision TEXT NOT NULL DEFAULT 'ask'"
        )
    await db.commit()
