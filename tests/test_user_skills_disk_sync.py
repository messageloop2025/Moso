"""scan_user_skills_from_disk 磁盘↔库双向同步测试。"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from database.models import _migrate_user_skill_groups
from services.user_skills_registry import (
    render_skill_markdown,
    scan_user_skills_from_disk,
)


def _skill_md(name: str, desc: str = "Test skill for sync.") -> str:
    return render_skill_markdown(name=name, description=desc)


class TestUserSkillsDiskSync(unittest.TestCase):
    def test_scan_removes_db_row_when_directory_deleted(self):
        async def _run():
            root = Path(tempfile.mkdtemp())
            try:
                user = {"id": 1, "username": "_sync_test_user"}
                skills_root = root / "skills"
                skills_root.mkdir(parents=True)
                slug_dir = skills_root / "alpha-skill"
                slug_dir.mkdir()
                (slug_dir / "SKILL.md").write_text(
                    _skill_md("alpha-skill"), encoding="utf-8"
                )

                fd, db_path = tempfile.mkstemp(suffix=".db")
                os.close(fd)
                try:
                    async with aiosqlite.connect(db_path) as db:
                        db.row_factory = aiosqlite.Row
                        await db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
                        await db.execute(
                            """CREATE TABLE user_skills (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER NOT NULL,
                            name TEXT NOT NULL,
                            display_name TEXT,
                            description TEXT,
                            skill_path TEXT,
                            enabled INTEGER NOT NULL DEFAULT 1,
                            group_id INTEGER,
                            file_mtime REAL,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            UNIQUE(user_id, name))"""
                        )
                        await db.commit()
                        await _migrate_user_skill_groups(db)

                        with patch(
                            "services.user_skills_registry.get_user_skills_root",
                            return_value=skills_root,
                        ):
                            r1 = await scan_user_skills_from_disk(db, 1, user)
                            self.assertEqual(r1["imported"], 1)
                            self.assertEqual(r1["removed"], 0)

                            shutil.rmtree(slug_dir)
                            r2 = await scan_user_skills_from_disk(db, 1, user)
                            self.assertEqual(r2["removed"], 1)
                            self.assertIn("alpha-skill", r2["removed_names"])

                            cur = await db.execute(
                                "SELECT COUNT(*) FROM user_skills WHERE user_id=1"
                            )
                            self.assertEqual((await cur.fetchone())[0], 0)
                            await cur.close()
                finally:
                    os.unlink(db_path)
            finally:
                shutil.rmtree(root, ignore_errors=True)

        asyncio.run(_run())

    def test_scan_rename_is_delete_old_and_import_new(self):
        async def _run():
            root = Path(tempfile.mkdtemp())
            try:
                user = {"id": 1, "username": "_sync_test_user"}
                skills_root = root / "skills"
                skills_root.mkdir(parents=True)
                old_dir = skills_root / "old-name"
                old_dir.mkdir()
                (old_dir / "SKILL.md").write_text(
                    _skill_md("old-name"), encoding="utf-8"
                )

                fd, db_path = tempfile.mkstemp(suffix=".db")
                os.close(fd)
                try:
                    async with aiosqlite.connect(db_path) as db:
                        db.row_factory = aiosqlite.Row
                        await db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
                        await db.execute(
                            """CREATE TABLE user_skills (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER NOT NULL,
                            name TEXT NOT NULL,
                            display_name TEXT,
                            description TEXT,
                            skill_path TEXT,
                            enabled INTEGER NOT NULL DEFAULT 1,
                            group_id INTEGER,
                            file_mtime REAL,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            UNIQUE(user_id, name))"""
                        )
                        await db.commit()
                        await _migrate_user_skill_groups(db)

                        with patch(
                            "services.user_skills_registry.get_user_skills_root",
                            return_value=skills_root,
                        ):
                            await db.execute(
                                "INSERT INTO user_skill_groups (user_id, name) VALUES (1, 'G1')"
                            )
                            await db.commit()
                            await scan_user_skills_from_disk(db, 1, user)
                            await db.execute(
                                """UPDATE user_skills SET group_id=(
                                   SELECT id FROM user_skill_groups WHERE user_id=1 LIMIT 1),
                                   enabled=0 WHERE name='old-name'"""
                            )
                            await db.commit()

                            new_dir = skills_root / "new-name"
                            new_dir.mkdir()
                            (new_dir / "SKILL.md").write_text(
                                _skill_md("new-name"), encoding="utf-8"
                            )
                            shutil.rmtree(old_dir)

                            r = await scan_user_skills_from_disk(db, 1, user)
                            self.assertEqual(r["imported"], 1)
                            self.assertEqual(r["removed"], 1)
                            self.assertIn("old-name", r["removed_names"])

                            cur = await db.execute(
                                "SELECT name, enabled, group_id FROM user_skills WHERE user_id=1 ORDER BY name"
                            )
                            rows = await cur.fetchall()
                            await cur.close()
                            self.assertEqual(len(rows), 1)
                            self.assertEqual(rows[0][0], "new-name")
                            self.assertEqual(rows[0][1], 1)
                            self.assertIsNone(rows[0][2])
                finally:
                    os.unlink(db_path)
            finally:
                shutil.rmtree(root, ignore_errors=True)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
