"""
数据库升级脚本：从版本 2 升级到版本 3。

为 triggered_task_runs 增加 caller_task_name（定时任务名，触发参数）。
"""


async def upgrade(db):
    """将数据库从版本 2 升级到版本 3。"""
    await db.execute(
        "ALTER TABLE triggered_task_runs ADD COLUMN caller_task_name TEXT DEFAULT ''"
    )
    await db.commit()
