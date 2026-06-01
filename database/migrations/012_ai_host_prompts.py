"""新增主机级 AI 提示词（按 用户 × 主机 维度独立保存，主机分享给其它用户时提示词不共用）。"""


async def upgrade(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_host_prompts (
            host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content TEXT DEFAULT '',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (host_id, user_id)
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_host_prompts_user ON ai_host_prompts(user_id)"
    )
    await db.commit()
