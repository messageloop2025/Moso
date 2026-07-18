"""Prompt Skills（skills 表）标记 deprecated，供只读视图/治理。"""


async def _has_column(db, table: str, column: str) -> bool:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    return any(r[1] == column for r in rows)


async def upgrade(db):
    if not await _has_column(db, "skills", "deprecated"):
        await db.execute(
            "ALTER TABLE skills ADD COLUMN deprecated INTEGER NOT NULL DEFAULT 0"
        )
    await db.commit()
