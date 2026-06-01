"""新增「聊天附件」表 chat_attachments：记录用户在 AI 聊天中上传的图片/文本/Markdown 等附件。

约定：
- 附件以 UUID 命名落盘在 chats/<username>/<uuid>.<ext>（由 api/chat_attachments.py 管理）；
  数据库仅保存元信息，便于权限校验、按会话/用户检索、删除等。
- kind 取值：'image' | 'text' | 'markdown' | 'binary'（AI 与前端据此决定是否直接渲染或调用 read_chat_attachment）。
- session_id 为空表示附件在「上传时尚未绑定会话」，由 chat 首次发送时绑定（或随会话清空一并清理）。
"""


async def upgrade(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_id INTEGER REFERENCES ai_chat_sessions(id) ON DELETE SET NULL,
            message_id INTEGER REFERENCES ai_chat_messages(id) ON DELETE SET NULL,
            original_name TEXT DEFAULT '',
            mime_type TEXT DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL DEFAULT 'binary',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_attachments_user ON chat_attachments(user_id, created_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_attachments_session ON chat_attachments(session_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_attachments_message ON chat_attachments(message_id)"
    )
    await db.commit()
