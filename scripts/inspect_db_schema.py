#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查当前 毛竹 数据库表结构，与 database/models.py 中的定义对照。
在项目根目录执行：python scripts/inspect_db_schema.py
或指定数据库路径：python scripts/inspect_db_schema.py /path/to/edgeops.db
"""
import asyncio
import sys
from pathlib import Path

# 保证项目根在 path 中
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aiosqlite
import config

# 与 database/models.py 一致的预期表及关键列（用于缺列提示）
EXPECTED_TABLE_COLUMNS = {
    "users": ["id", "username", "password_hash", "role", "status", "email", "failed_login_attempts", "locked_until"],
    "hosts": ["id", "name", "host", "port", "credential_id", "username", "auth_type", "host_type", "host_version", "created_by"],
    "operation_logs": ["id", "user_id", "operation", "params", "result", "source", "details", "created_at"],
    "ai_chat_sessions": ["id", "user_id", "host_id", "title", "session_prompt", "session_scope", "low_interaction_mode"],
    "user_ai_config": [
        "user_id", "api_key", "base_url", "model", "provider",
        "vision_enabled", "ai_output_locale", "active_profile_id",
    ],
    "user_ai_model_profiles": [
        "id", "user_id", "name", "api_key", "base_url", "model",
        "provider", "vision_enabled", "ai_output_locale",
    ],
    "credentials": ["id", "type", "code", "name", "username", "password_enc", "private_key_enc", "created_by"],
    "host_groups": ["id", "name", "parent_id", "created_by"],
    "host_tags": ["id", "name", "color", "created_by"],
    "host_user_tags": ["user_id", "host_id", "tag_id"],
    "settings": ["key", "value"],
    "skills": ["id", "code", "name", "enabled"],
    "server_maintenance_history": ["id", "host", "port", "category", "content", "details", "created_by"],
    "ai_host_knowledge": ["host_id", "user_id", "content"],
    "ai_host_prompts": ["host_id", "user_id", "content"],
    "best_practices": ["id", "title", "category", "content", "created_by"],
    "password_reset_tokens": ["token", "user_id", "kind", "expires_at"],
    "email_verification_codes": ["id", "user_id", "email", "code", "purpose", "expires_at", "used_at"],
    "local_shell_sessions": ["id", "user_id", "title"],
    "local_shell_logs": ["id", "session_id", "kind", "content"],
    "batch_operations": ["id", "operation_type", "scope_type", "status", "created_by"],
    "batch_operation_details": ["id", "batch_id", "host_id", "status"],
    "ai_chat_messages": ["id", "session_id", "role", "content"],
    "host_group_members": ["host_id", "group_id"],
}


async def main(db_path: str | None = None):
    path = db_path or config.DATABASE_PATH
    if not Path(path).exists():
        print(f"数据库文件不存在: {path}")
        return
    print(f"数据库: {path}\n" + "=" * 60)
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY type, name"
        )
        rows = await cursor.fetchall()
        tables = sorted({r[0] for r in rows if r[1] == "table" and not r[0].startswith("sqlite_")})
        indexes = [(r[0], r[1]) for r in rows if r[1] == "index" and not r[0].startswith("sqlite_")]

        table_columns = {}
        for table in tables:
            cursor = await db.execute(f"PRAGMA table_info({table})")
            cols = await cursor.fetchall()
            table_columns[table] = [c[1] for c in cols]
            print(f"\n表: {table}")
            print("-" * 40)
            for c in cols:
                cid, name, typ, notnull, dflt, pk = c
                nn = " NOT NULL" if notnull else ""
                default = f" DEFAULT {dflt}" if dflt is not None else ""
                pk_str = " PRIMARY KEY" if pk else ""
                print(f"  {name}: {typ}{nn}{default}{pk_str}")
            for idx_name, _ in indexes:
                cursor = await db.execute(
                    "SELECT tbl_name FROM sqlite_master WHERE name = ? AND type = 'index'",
                    (idx_name,),
                )
                trow = await cursor.fetchone()
                if not trow or trow[0] != table:
                    continue
                cursor = await db.execute(f"PRAGMA index_info(\"{idx_name}\")")
                info = await cursor.fetchall()
                cols_idx = [r[2] for r in info if r[2] is not None]
                print(f"  索引: {idx_name} ({', '.join(cols_idx)})")

        print("\n" + "=" * 60)
        missing = []
        for tbl, expected_cols in EXPECTED_TABLE_COLUMNS.items():
            if tbl not in table_columns:
                missing.append((tbl, None, f"缺少表 {tbl}"))
                continue
            for col in expected_cols:
                if col not in table_columns[tbl]:
                    missing.append((tbl, col, f"{tbl}.{col} 缺失"))
        if missing:
            print("与预期结构差异（可由迁移自动修复）:")
            for _t, _c, msg in missing:
                print("  -", msg)
            print("\n建议: 启动应用一次（或执行 database.init_db）以应用迁移。")
        else:
            print("表结构与 database/models.py 预期一致。")
        print("可与 database/models.py 中 SCHEMA_SQL 及各 _migrate_* 对照。")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(main(path))
