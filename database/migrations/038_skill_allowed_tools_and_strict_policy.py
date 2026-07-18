"""user_skills.allowed_tools；settings 强制严格模式键（若无则插入空）。"""


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
        db, "user_skills", "allowed_tools"
    ):
        await db.execute(
            "ALTER TABLE user_skills ADD COLUMN allowed_tools TEXT NOT NULL DEFAULT ''"
        )
    # 组织策略 / 强制严格：settings 键（值 JSON 或 true/false）
    if await _table_exists(db, "settings"):
        cur = await db.execute(
            "SELECT 1 FROM settings WHERE key = ? LIMIT 1",
            ("chat_mode_force_strict",),
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                ("chat_mode_force_strict", "false"),
            )
        cur = await db.execute(
            "SELECT 1 FROM settings WHERE key = ? LIMIT 1",
            ("chat_mode_strict_policy_json",),
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (
                    "chat_mode_strict_policy_json",
                    '{"always_allow_mode":"exact","allow_glob":false}',
                ),
            )
    await db.commit()
