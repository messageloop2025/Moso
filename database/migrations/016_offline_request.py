"""新增「离线版本申请」相关表。

设计：
- offline_request_applications：登录页公开浮窗提交的申请，含联系方式与申请理由。
  status 状态机：'pending' → 'approved' → 'sent'  ↘ 'rejected'
- offline_request_replies：管理员对某条申请的邮件回复历史（每发一封邮件入一行，
  记录 SMTP 是否真的发出 / 失败原因，便于追溯）。

约定：
- 申请只能由匿名访客提交；提交后无法自助修改/撤回（避免冒名修改）。
- 派发限额由业务层常量 OFFLINE_QUOTA_TOTAL 控制（默认 100 份），仅以 status='sent' 计数。
"""


async def upgrade(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_request_applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            admin_note TEXT NOT NULL DEFAULT '',
            ip_address TEXT DEFAULT '',
            user_agent TEXT DEFAULT '',
            replies_count INTEGER NOT NULL DEFAULT 0,
            last_reply_at DATETIME,
            last_reply_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_offline_req_status ON offline_request_applications(status, created_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_offline_req_email ON offline_request_applications(email)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_offline_req_created ON offline_request_applications(created_at)"
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_request_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL REFERENCES offline_request_applications(id) ON DELETE CASCADE,
            admin_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            email_to TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            email_sent INTEGER NOT NULL DEFAULT 0,
            email_error TEXT NOT NULL DEFAULT '',
            is_ai_drafted INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_offline_reply_req ON offline_request_replies(request_id, created_at)"
    )

    # 默认配置：是否在用户提交申请时邮件通知所有管理员（沿用 user_feedback 的模式）
    await db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('notify_admin_on_offline_request', 'true')"
    )
    await db.commit()
