"""user_skill_groups 分组 CRUD / 批量移组 / 整组启停。"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

import aiosqlite

from database.models import _migrate_user_skill_groups
from services.user_skills_registry import (
    bulk_assign_skills_to_group,
    bulk_set_group_skills_enabled,
    create_user_skill_group,
    delete_user_skill_group,
)

class TestUserSkillGroupsRegistry(unittest.TestCase):
    def test_bulk_assign_by_ids_and_all_ungrouped(self):
        async def _run():
            fd, path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                async with aiosqlite.connect(path) as db:
                    db.row_factory = aiosqlite.Row
                    await db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
                    await db.execute("INSERT INTO users (id, username) VALUES (1, 'u1'), (2, 'u2')")
                    await db.execute(
                        """CREATE TABLE user_skills (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        group_id INTEGER,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, name))"""
                    )
                    await db.commit()
                    await _migrate_user_skill_groups(db)
                    await db.execute(
                        "INSERT INTO user_skills (user_id, name, enabled, group_id) VALUES (1, 'a', 1, NULL)"
                    )
                    await db.execute(
                        "INSERT INTO user_skills (user_id, name, enabled, group_id) VALUES (1, 'b', 0, NULL)"
                    )
                    await db.execute(
                        "INSERT INTO user_skills (user_id, name, enabled, group_id) VALUES (2, 'x', 1, NULL)"
                    )
                    await db.commit()

                    grp = await create_user_skill_group(db, 1, name="Work")
                    gid = int(grp["id"])

                    r1 = await bulk_assign_skills_to_group(db, 1, group_id=gid, skill_ids=[1])
                    self.assertEqual(r1["updated"], 1)
                    cur = await db.execute(
                        "SELECT group_id FROM user_skills WHERE id=1 AND user_id=1"
                    )
                    self.assertEqual((await cur.fetchone())[0], gid)
                    await cur.close()

                    r2 = await bulk_assign_skills_to_group(db, 1, group_id=gid, all_ungrouped=True)
                    self.assertEqual(r2["updated"], 1)
                    cur = await db.execute(
                        "SELECT COUNT(*) FROM user_skills WHERE user_id=1 AND group_id IS NULL"
                    )
                    self.assertEqual((await cur.fetchone())[0], 0)
                    await cur.close()

                    cur = await db.execute(
                        "SELECT group_id FROM user_skills WHERE user_id=2 AND name='x'"
                    )
                    self.assertIsNone((await cur.fetchone())[0])
                    await cur.close()
            finally:
                os.unlink(path)

        asyncio.run(_run())

    def test_bulk_assign_requires_target_for_all_ungrouped(self):
        async def _run():
            fd, path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                async with aiosqlite.connect(path) as db:
                    db.row_factory = aiosqlite.Row
                    await db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
                    await db.execute(
                        """CREATE TABLE user_skills (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        group_id INTEGER,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, name))"""
                    )
                    await db.commit()
                    await _migrate_user_skill_groups(db)
                    with self.assertRaises(ValueError):
                        await bulk_assign_skills_to_group(db, 1, group_id=None, all_ungrouped=True)
            finally:
                os.unlink(path)

        asyncio.run(_run())

    def test_bulk_enable_ungrouped_and_delete_group(self):
        async def _run():
            fd, path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                async with aiosqlite.connect(path) as db:
                    db.row_factory = aiosqlite.Row
                    await db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
                    await db.execute(
                        """CREATE TABLE user_skills (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        group_id INTEGER,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, name))"""
                    )
                    await db.commit()
                    await _migrate_user_skill_groups(db)
                    await db.execute(
                        "INSERT INTO user_skills (user_id, name, enabled, group_id) VALUES (1, 'a', 0, NULL)"
                    )
                    await db.execute(
                        "INSERT INTO user_skills (user_id, name, enabled, group_id) VALUES (1, 'b', 1, NULL)"
                    )
                    await db.commit()

                    r = await bulk_set_group_skills_enabled(db, 1, group_id=None, enabled=True)
                    self.assertEqual(r["updated"], 2)
                    cur = await db.execute(
                        "SELECT MIN(enabled), MAX(enabled) FROM user_skills WHERE user_id=1"
                    )
                    row = await cur.fetchone()
                    await cur.close()
                    self.assertEqual(row[0], 1)
                    self.assertEqual(row[1], 1)

                    grp = await create_user_skill_group(db, 1, name="G")
                    gid = int(grp["id"])
                    await bulk_assign_skills_to_group(db, 1, group_id=gid, all_ungrouped=True)

                    ok = await delete_user_skill_group(db, 1, gid)
                    self.assertTrue(ok)
                    cur = await db.execute(
                        "SELECT COUNT(*) FROM user_skills WHERE user_id=1 AND group_id IS NULL"
                    )
                    self.assertEqual((await cur.fetchone())[0], 2)
                    await cur.close()
            finally:
                os.unlink(path)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
