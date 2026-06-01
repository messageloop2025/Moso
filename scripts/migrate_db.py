#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按版本号将 毛竹 数据库升级到最新（与启动时逻辑一致）。

在项目根目录执行：python scripts/migrate_db.py
或指定数据库路径：EDGEOPS_DB=/path/to/edgeops.db python scripts/migrate_db.py

**全新空库安装**（一键建库、含默认 admin）：请使用 ``python scripts/bootstrap_fresh_db.py``。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import init_db, close_db


async def main():
    await init_db()
    await close_db()
    print("数据库升级已完成（已按版本执行至最新）。")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
