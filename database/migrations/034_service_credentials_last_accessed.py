"""从 schema 34 升级到 35：服务凭证增加 last_accessed_at（最近使用时间）。

脚本编号 034。
"""
from __future__ import annotations


async def upgrade(db) -> None:
    async def has_column(table: str, column: str) -> bool:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return any(r[1] == column for r in rows)

    if not await has_column("host_service_credentials", "last_accessed_at"):
        await db.execute(
            "ALTER TABLE host_service_credentials ADD COLUMN last_accessed_at DATETIME"
        )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_hsc_user_last_access "
        "ON host_service_credentials(user_id, last_accessed_at)"
    )
    await db.commit()
