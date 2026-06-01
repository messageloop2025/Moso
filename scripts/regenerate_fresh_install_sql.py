#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从当前代码的迁移链生成「空库直装」SQL 快照，便于离线一次性执行或审计 diff。

在项目根目录执行::

    python scripts/regenerate_fresh_install_sql.py

输出：``database/bundles/fresh_install.sql``（覆盖写入）。

**维护约定**：在 ``database/migrations/`` 下新增或修改迁移脚本后，应重新运行本脚本，
使快照与 ``run_upgrades`` 最终结果一致；若仓库内跟踪该文件，请一并提交。

注意：快照含 schema_version、默认 settings、admin 用户等数据，与 ``bootstrap_fresh_db.py``
使用迁移建库的结果一致；升级已有库仍须使用应用启动或 ``migrate_db.py``。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_PATH = ROOT / "database" / "bundles" / "fresh_install.sql"


async def _build_temp_db(tmp_path: str) -> None:
    from database import init_db

    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
    await init_db(database_path=tmp_path)


def main() -> None:
    from database.migrations import get_current_schema_version

    target_ver = get_current_schema_version()
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        asyncio.run(_build_temp_db(tmp))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = (
        "-- 毛竹 fresh install SQL snapshot\n"
        f"-- schema_version target after apply: {target_ver}\n"
        f"-- generated_at_utc: {stamp}\n"
        "-- Regenerate: python scripts/regenerate_fresh_install_sql.py\n"
        "\n"
    )
    parts = [header]
    con = sqlite3.connect(tmp)
    try:
        for line in con.iterdump():
            parts.append(line + "\n")
    finally:
        con.close()
    try:
        os.unlink(tmp)
    except OSError:
        pass

    OUT_PATH.write_text("".join(parts), encoding="utf-8")
    print(f"已写入: {OUT_PATH} (schema_version={target_ver})")


if __name__ == "__main__":
    main()
