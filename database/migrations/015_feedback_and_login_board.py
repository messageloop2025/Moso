"""新增「登录页留言板」与「系统内反馈」相关表。

设计：
- anonymous_messages：匿名留言与管理员回复混在一张表里，用 parent_id 区分（NULL=留言，非空=回复）。
  show_on_login 控制是否在登录页公开展示；status 标记审核状态。
- user_feedback：登录用户提交的反馈主体；is_ai_submitted 区分是否 AI 代提交；status 表达生命周期。
- user_feedback_replies：管理员对反馈的回复；一条反馈可有多条回复。

约定：用户撤回 = 物理删除；管理员忽略 = status='ignored'；管理员撤回回复 = 物理删除 reply 行。
"""


async def upgrade(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS anonymous_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER REFERENCES anonymous_messages(id) ON DELETE CASCADE,
            author_type TEXT NOT NULL DEFAULT 'guest',
            author_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            nickname TEXT DEFAULT '',
            content TEXT NOT NULL,
            ip_address TEXT DEFAULT '',
            user_agent TEXT DEFAULT '',
            show_on_login INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_anon_msg_parent ON anonymous_messages(parent_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_anon_msg_status ON anonymous_messages(status)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_anon_msg_show ON anonymous_messages(show_on_login, status)"
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT DEFAULT '',
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            status TEXT NOT NULL DEFAULT 'open',
            is_ai_submitted INTEGER NOT NULL DEFAULT 0,
            admin_read_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_user_feedback_user ON user_feedback(user_id, status)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_user_feedback_status ON user_feedback(status, admin_read_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_user_feedback_created ON user_feedback(created_at)")

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_feedback_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feedback_id INTEGER NOT NULL REFERENCES user_feedback(id) ON DELETE CASCADE,
            admin_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            is_ai_drafted INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_feedback_replies_fb ON user_feedback_replies(feedback_id, created_at)"
    )

    # 默认配置：用户反馈是否邮件通知所有管理员
    await db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('notify_admin_on_user_feedback', 'false')"
    )
    await db.commit()
