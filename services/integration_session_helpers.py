"""集成/MCP 会话只读查询（ClawOps / MCP 共用）。"""

from __future__ import annotations

from fastapi import HTTPException

INTEGRATION_READABLE_SCOPES = frozenset(
    {"integration", "mcp_orchestrate", "mcp_runtime"}
)


async def list_integration_scope_messages(
    db,
    user: dict,
    session_id: int,
    *,
    limit: int = 50,
) -> dict:
    limit = max(1, min(200, int(limit or 50)))
    rows = await db.execute_fetchall(
        """SELECT id, COALESCE(session_scope,'default') AS session_scope
           FROM ai_chat_sessions WHERE id=? AND user_id=?""",
        (session_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="会话不存在")
    scope = (dict(rows[0]).get("session_scope") or "default").strip().lower()
    if scope not in INTEGRATION_READABLE_SCOPES:
        raise HTTPException(status_code=403, detail="仅可读取集成/MCP 会话消息")
    msg_rows = await db.execute_fetchall(
        """SELECT id, role, content, created_at FROM ai_chat_messages
           WHERE session_id=? ORDER BY id DESC LIMIT ?""",
        (session_id, limit),
    )
    messages = [dict(r) for r in reversed(msg_rows)]
    return {
        "success": True,
        "session_id": session_id,
        "session_scope": scope,
        "messages": messages,
    }
