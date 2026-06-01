"""从版本 6 升级到 7：hosts 表增加别名（JSON 数组字符串）与用途备注。"""


async def upgrade(db):
    async def has_column(table: str, column: str) -> bool:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return any(r[1] == column for r in rows)

    if not await has_column("hosts", "aliases"):
        await db.execute("ALTER TABLE hosts ADD COLUMN aliases TEXT DEFAULT '[]'")
    if not await has_column("hosts", "remark"):
        await db.execute("ALTER TABLE hosts ADD COLUMN remark TEXT DEFAULT ''")
    await db.commit()
