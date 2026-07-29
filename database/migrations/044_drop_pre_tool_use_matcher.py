"""数据库迁移 044：删除 user_skills 表的 pre_tool_use_matcher 和 pre_tool_use_decision 列。
hooks.json 完全替代了这两个 DB 字段的 Hook 功能。"""


async def upgrade(db) -> None:
    # 检查列是否存在再删除（兼容新安装的空库）
    for col in ("pre_tool_use_matcher", "pre_tool_use_decision"):
        cur = await db.execute("PRAGMA table_info(user_skills)")
        rows = await cur.fetchall()
        await cur.close()
        if any(r[1] == col for r in rows):
            await db.execute(f"ALTER TABLE user_skills DROP COLUMN {col}")
