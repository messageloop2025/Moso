"""新增 AI 编排工作流模板表：用于把 delegate_chain 的 payload 保存为可复用模板。

模板独立于主机——payload 里自带 host_id（或依赖运行期覆盖），用户 / AI 可用
`list_workflow_templates` / `run_workflow_template(id)` 快速复用最近跑过的链。
"""


async def upgrade(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_workflow_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            kind TEXT DEFAULT 'delegate_chain',
            payload TEXT NOT NULL,
            tags TEXT DEFAULT '',
            visibility TEXT DEFAULT 'private',
            last_run_at DATETIME,
            run_count INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_workflow_templates_owner ON ai_workflow_templates(owner_user_id)"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_workflow_templates_owner_name ON ai_workflow_templates(owner_user_id, name)"
    )
    await db.commit()
