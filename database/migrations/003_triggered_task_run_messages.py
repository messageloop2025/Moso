"""
数据库升级脚本：从版本 3 升级到版本 4。

新增：triggered_task_run_messages 表，用于存储触发任务单次执行的会话式历史（与 scheduled_task_run_messages 对称）。
"""


async def upgrade(db):
    """将数据库从版本 3 升级到版本 4。"""
    await db.executescript("""
CREATE TABLE IF NOT EXISTS triggered_task_run_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES triggered_task_runs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_triggered_task_run_messages_run ON triggered_task_run_messages(run_id);
""")
    await db.commit()
