"""
数据库升级脚本：从版本 0 升级到版本 1。

- 版本 0：空库或仅有 schema_version 表。
- 版本 1：完整业务表结构 + 历史迁移补列/补表 + 默认管理员与配置。
- 本脚本仅负责 0 -> 1 这一档，后续结构变更请新增 001_xxx.py、002_xxx.py...
"""

from database.models import run_initial_schema


async def upgrade(db):
    """将数据库从版本 0 升级到版本 1。"""
    await run_initial_schema(db)
