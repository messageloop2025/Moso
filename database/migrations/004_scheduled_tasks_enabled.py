"""定时任务：增加 enabled（启用/停用）"""

async def upgrade(db):
    await db.execute(
        "ALTER TABLE scheduled_tasks ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
    )
    await db.commit()
