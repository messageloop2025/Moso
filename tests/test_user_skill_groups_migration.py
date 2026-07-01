"""user_skill_groups 迁移与安全网测试。"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

import aiosqlite

from database.models import _ensure_full_schema_safety_net, _migrate_user_skill_groups
from database.migrations import get_current_schema_version, run_upgrades


class TestUserSkillGroupsMigration(unittest.TestCase):
    def test_safety_net_creates_groups_table(self):
        async def _run():
            fd, path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                async with aiosqlite.connect(path) as db:
                    await db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
                    await db.execute(
                        """CREATE TABLE user_skills (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        UNIQUE(user_id, name))"""
                    )
                    await db.commit()
                    await _migrate_user_skill_groups(db)
                    cur = await db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_skill_groups'"
                    )
                    self.assertIsNotNone(await cur.fetchone())
                    await cur.close()
                    cur = await db.execute("PRAGMA table_info(user_skills)")
                    cols = [r[1] for r in await cur.fetchall()]
                    await cur.close()
                    self.assertIn("group_id", cols)
            finally:
                os.unlink(path)

        asyncio.run(_run())

    def test_run_upgrades_from_v35(self):
        async def _run():
            fd, path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                async with aiosqlite.connect(path) as db:
                    await db.execute(
                        """CREATE TABLE schema_version (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        version INTEGER NOT NULL DEFAULT 0)"""
                    )
                    await db.execute("INSERT INTO schema_version (id, version) VALUES (1, 35)")
                    await db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
                    await db.execute(
                        """CREATE TABLE user_skills (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        UNIQUE(user_id, name))"""
                    )
                    await db.commit()
                    await run_upgrades(db)
                    cur = await db.execute("SELECT version FROM schema_version")
                    ver = (await cur.fetchone())[0]
                    await cur.close()
                    self.assertEqual(ver, get_current_schema_version())
                    cur = await db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_skill_groups'"
                    )
                    self.assertIsNotNone(await cur.fetchone())
                    await cur.close()
            finally:
                os.unlink(path)

        asyncio.run(_run())

    def test_safety_net_heals_missing_table_at_latest_version(self):
        async def _run():
            fd, path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                target = get_current_schema_version()
                async with aiosqlite.connect(path) as db:
                    await db.execute(
                        """CREATE TABLE schema_version (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        version INTEGER NOT NULL DEFAULT 0)"""
                    )
                    await db.execute("INSERT INTO schema_version (id, version) VALUES (1, ?)", (target,))
                    await db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
                    await db.execute(
                        """CREATE TABLE user_skills (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        UNIQUE(user_id, name))"""
                    )
                    await db.commit()
                    await _ensure_full_schema_safety_net(db)
                    cur = await db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_skill_groups'"
                    )
                    self.assertIsNotNone(await cur.fetchone())
                    await cur.close()
            finally:
                os.unlink(path)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
