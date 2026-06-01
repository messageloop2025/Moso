"""新增「AI 成果物」表 ai_artifacts：AI 根据用户指令生成/收集的结构化产物。

设计要点：
- 每个 artifact 有一个唯一 uuid，对应一个目录 web/fs/<username>/<ARTIFACT_SUBDIR>/<YYYY>/<MM>/<DD>/<shortid>/；
  目录内可以是单文件（csv/md/html/json/...），也可以是目录结构（html + 图片 + js/css/json）。
- 下载时：单文件直接返流；bundle 按 tar.gz 打包返回（服务端临时流式打包）。
- 数据库只保存元信息；真实文件落盘在 fs 用户目录下，方便用户在文件面板里也能看到/导出。
- session_id / message_id 允许为空；AI 生成后可再绑定到具体消息。
"""

from __future__ import annotations


async def upgrade(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_id INTEGER REFERENCES ai_chat_sessions(id) ON DELETE SET NULL,
            message_id INTEGER REFERENCES ai_chat_messages(id) ON DELETE SET NULL,
            title TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'bundle',     -- 'single_file' | 'bundle'
            storage_subdir TEXT NOT NULL DEFAULT '', -- ARTIFACT_SUBDIR/ 下的相对目录，如 '2026/04/22/abcd1234'
            entry_file TEXT NOT NULL DEFAULT '',     -- bundle 首页 (index.html / report.md ...) 或单文件名
            file_count INTEGER NOT NULL DEFAULT 0,
            total_bytes INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_artifacts_user ON ai_artifacts(user_id, created_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_artifacts_session ON ai_artifacts(session_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_artifacts_message ON ai_artifacts(message_id)"
    )
    await db.commit()
