"""ai_chat_sessions.session_runtime_json：会话级瞬时运行态（后台 SSH 任务 log_path 等）。"""
from __future__ import annotations


async def upgrade(db):
    async def _has_column(table: str, col: str) -> bool:
        cur = await db.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        await cur.close()
        return any(r[1] == col for r in rows)

    if not await _has_column("ai_chat_sessions", "session_runtime_json"):
        await db.execute(
            "ALTER TABLE ai_chat_sessions ADD COLUMN session_runtime_json TEXT NOT NULL DEFAULT ''"
        )
    await db.commit()
