"""数据库结构版本管理：单行表记录当前 schema 版本，供升级模块使用。"""
import aiosqlite

SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 0);
"""


async def ensure_schema_version_table(db: aiosqlite.Connection) -> None:
    """确保 schema_version 表存在且有一行（version=0 表示未初始化）。"""
    await db.executescript(SCHEMA_VERSION_TABLE)
    await db.commit()


async def get_schema_version(db: aiosqlite.Connection) -> int:
    """返回当前数据库结构版本号；表不存在或无行时视为 0。"""
    try:
        cur = await db.execute("SELECT version FROM schema_version WHERE id = 1")
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0


async def set_schema_version(db: aiosqlite.Connection, version: int) -> None:
    """将当前数据库结构版本号设为 version。"""
    await db.execute("UPDATE schema_version SET version = ? WHERE id = 1", (version,))
    await db.commit()
