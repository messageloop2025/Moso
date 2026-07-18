"""ai_chat_sessions.chat_mode + strict_allow_cache_json；user_skills slash/hook 元数据列。"""


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
    if await _table_exists(db, "ai_chat_sessions"):
        if not await _has_column(db, "ai_chat_sessions", "chat_mode"):
            await db.execute(
                "ALTER TABLE ai_chat_sessions ADD COLUMN chat_mode TEXT NOT NULL DEFAULT 'normal'"
            )
        if not await _has_column(db, "ai_chat_sessions", "strict_allow_cache_json"):
            await db.execute(
                "ALTER TABLE ai_chat_sessions ADD COLUMN strict_allow_cache_json TEXT NOT NULL DEFAULT ''"
            )
    if await _table_exists(db, "user_skills"):
        if not await _has_column(db, "user_skills", "slash_name"):
            await db.execute(
                "ALTER TABLE user_skills ADD COLUMN slash_name TEXT NOT NULL DEFAULT ''"
            )
        if not await _has_column(db, "user_skills", "hooks_enabled"):
            await db.execute(
                "ALTER TABLE user_skills ADD COLUMN hooks_enabled INTEGER NOT NULL DEFAULT 0"
            )
        if not await _has_column(db, "user_skills", "pre_tool_use_matcher"):
            await db.execute(
                "ALTER TABLE user_skills ADD COLUMN pre_tool_use_matcher TEXT NOT NULL DEFAULT ''"
            )
    await db.commit()
