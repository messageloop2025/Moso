"""将已绑定 session 的附件/成果物迁入 chats/sessions/<id>/（幂等）。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger("edgeops.migration.037")


async def _has_column(db, table: str, column: str) -> bool:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    return any(r[1] == column for r in rows)


async def _table_exists(db, table: str) -> bool:
    cur = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    )
    row = await cur.fetchone()
    await cur.close()
    return row is not None


def _user_fs_root(username: str) -> Path:
    import config
    from api.filesystem import FS_DIR, _safe_username

    return Path(FS_DIR) / _safe_username(username or "default")


async def upgrade(db):
    """按 session_id 迁移磁盘 + 更新 storage_subdir；已是 sessions/ 前缀则跳过。"""
    moved = 0
    updated = 0
    if await _table_exists(db, "chat_attachments") and await _has_column(
        db, "chat_attachments", "storage_subdir"
    ):
        cur = await db.execute(
            """SELECT ca.id, ca.uuid, ca.storage_subdir, ca.original_name, ca.mime_type,
                      ca.session_id, u.username
               FROM chat_attachments ca
               JOIN users u ON u.id = ca.user_id
               WHERE ca.session_id IS NOT NULL AND ca.session_id > 0
                 AND COALESCE(ca.storage_subdir, '') NOT LIKE 'sessions/%'"""
        )
        rows = await cur.fetchall()
        await cur.close()
        for r in rows:
            row = dict(r)
            sid = int(row["session_id"])
            old_sub = (row.get("storage_subdir") or "").strip().strip("/")
            new_sub = f"sessions/{sid}"
            root = _user_fs_root(row.get("username") or "") / "chats"
            # 推断扩展
            name = row.get("original_name") or ""
            ext = Path(name).suffix if "." in name else ".bin"
            uuid_s = (row.get("uuid") or "").strip()
            if not uuid_s:
                continue
            old_path = (root / old_sub / f"{uuid_s}{ext}") if old_sub else (root / f"{uuid_s}{ext}")
            # 尝试常见扩展
            if not old_path.is_file():
                for e in (".png", ".jpg", ".jpeg", ".md", ".txt", ".pdf", ".bin", ""):
                    cand = (root / old_sub / f"{uuid_s}{e}") if old_sub else (root / f"{uuid_s}{e}")
                    if cand.is_file():
                        old_path = cand
                        ext = e or ext
                        break
            new_dir = root / new_sub
            new_dir.mkdir(parents=True, exist_ok=True)
            new_path = new_dir / old_path.name
            if old_path.is_file() and old_path.resolve() != new_path.resolve():
                try:
                    if not new_path.exists():
                        shutil.move(str(old_path), str(new_path))
                        moved += 1
                except OSError as e:
                    logger.warning("move attachment %s: %s", uuid_s, e)
                    continue
            await db.execute(
                "UPDATE chat_attachments SET storage_subdir = ? WHERE id = ?",
                (new_sub, int(row["id"])),
            )
            updated += 1

    if await _table_exists(db, "ai_artifacts") and await _has_column(
        db, "ai_artifacts", "storage_subdir"
    ):
        cur = await db.execute(
            """SELECT a.id, a.uuid, a.storage_subdir, a.session_id, u.username
               FROM ai_artifacts a
               JOIN users u ON u.id = a.user_id
               WHERE a.session_id IS NOT NULL AND a.session_id > 0
                 AND COALESCE(a.storage_subdir, '') NOT LIKE 'sessions/%'"""
        )
        rows = await cur.fetchall()
        await cur.close()
        for r in rows:
            row = dict(r)
            sid = int(row["session_id"])
            old_sub = (row.get("storage_subdir") or "").strip().strip("/")
            if not old_sub:
                continue
            leaf = Path(old_sub).name
            new_sub = f"sessions/{sid}/{leaf}"
            root = _user_fs_root(row.get("username") or "") / "chats"
            old_dir = root / old_sub
            new_dir = root / new_sub
            if old_dir.is_dir() and old_dir.resolve() != new_dir.resolve():
                try:
                    new_dir.parent.mkdir(parents=True, exist_ok=True)
                    if not new_dir.exists():
                        shutil.move(str(old_dir), str(new_dir))
                        moved += 1
                except OSError as e:
                    logger.warning("move artifact %s: %s", row.get("uuid"), e)
                    continue
            await db.execute(
                "UPDATE ai_artifacts SET storage_subdir = ? WHERE id = ?",
                (new_sub, int(row["id"])),
            )
            updated += 1

    await db.commit()
    logger.info("037 session chats layout: updated=%s moved=%s", updated, moved)
