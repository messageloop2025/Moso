#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全新安装：一键创建最新版 毛竹 数据库（与应用启动共用同一套迁移 + 安全网）。

在项目根目录执行::

    python scripts/bootstrap_fresh_db.py
    python scripts/bootstrap_fresh_db.py --database /data/edgeops.db --force

- 默认数据库路径：环境变量 ``EDGEOPS_DB``，否则为项目根目录下的 ``edgeops.db``（与 config 一致）。
- 目标文件若已存在，必须使用 ``--force``（将先删除该文件再建库）。
- 建库完成后含默认管理员 ``admin`` / ``admin123``；生产环境请立即改密。
- **与依次升级的关系**：已有实例请继续用 ``python scripts/migrate_db.py`` 或正常启动应用；本脚本仅面向空安装。
- **迁移变更后**：本脚本无需修改；流水线与 ``init_db()`` 相同。若团队需要单文件 SQL 离线交付，
  请在变更迁移后运行 ``python scripts/regenerate_fresh_install_sql.py`` 并提交/分发生成的 SQL。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="毛竹 全新安装一键建库")
    p.add_argument(
        "--database",
        "-d",
        default=os.environ.get("EDGEOPS_DB", str(ROOT / "edgeops.db")),
        help="SQLite 文件路径（默认：环境变量 EDGEOPS_DB 或 ./edgeops.db）",
    )
    p.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="目标文件已存在时先删除再建库（否则报错退出）",
    )
    return p.parse_args()


async def _main_async(db_path: Path, force: bool) -> None:
    if db_path.exists():
        if not force:
            print(f"错误: 数据库文件已存在: {db_path}", file=sys.stderr)
            print("全新安装请先备份后使用 --force，或使用 migrate_db.py 升级已有库。", file=sys.stderr)
            sys.exit(1)
        try:
            db_path.unlink()
        except OSError as e:
            print(f"错误: 无法删除已有文件: {e}", file=sys.stderr)
            sys.exit(1)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    from database import init_db

    await init_db(database_path=str(db_path.resolve()))
    print(f"已创建数据库（最新 schema）: {db_path.resolve()}")
    print("默认管理员: admin / admin123 （请尽快修改密码）")


def main() -> None:
    args = _parse_args()
    asyncio.run(_main_async(Path(args.database), args.force))


if __name__ == "__main__":
    main()
