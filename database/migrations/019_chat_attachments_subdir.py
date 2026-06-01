"""为 chat_attachments 增加 storage_subdir 列，并按 created_at 把已有附件按 YYYY/MM/DD 组织目录。

背景：之前所有附件都直接落在 web/fs/<username>/chats/ 根下，当聊天附件变多时不便于按日期归档；
改成 web/fs/<username>/chats/YYYY/MM/DD/<uuid>.<ext>，同时在 db 里记录子目录，避免读取时靠猜。

升级动作：
1. ALTER TABLE 添加 storage_subdir TEXT NOT NULL DEFAULT ''（SQLite 支持）；
2. 为每条 storage_subdir='' 的已有行按 created_at 推出 'YYYY/MM/DD'，UPDATE 回写；
3. 尝试把磁盘上的旧文件（web/fs/<username>/chats/<uuid>.<ext>）移动到新子目录；
   文件不存在或移动失败仅记录日志，不中断升级。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("edgeops.database.migrations.019")


async def _column_exists(db, table: str, col: str) -> bool:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    return any((r[1] == col) for r in rows)


def _parse_date(created_at) -> str:
    """把 SQLite created_at 解析为 'YYYY/MM/DD'；失败时回退为今天（UTC）。"""
    s = created_at
    try:
        if isinstance(s, datetime):
            dt = s
        else:
            text = str(s or "").strip()
            if not text:
                raise ValueError("empty")
            # SQLite CURRENT_TIMESTAMP 形如 '2026-04-22 08:56:12'
            if "T" in text:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            else:
                dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y/%m/%d")
    except (TypeError, ValueError):
        return datetime.utcnow().strftime("%Y/%m/%d")


def _safe_username(name) -> str:
    if not name or not isinstance(name, str):
        return "default"
    s = "".join(c for c in name.strip() if c.isalnum() or c in "._-")[:64]
    return s or "default"


async def upgrade(db):
    if not await _column_exists(db, "chat_attachments", "storage_subdir"):
        await db.execute(
            "ALTER TABLE chat_attachments ADD COLUMN storage_subdir TEXT NOT NULL DEFAULT ''"
        )
        await db.commit()

    # 本 migration 对磁盘文件也要操作；延迟 import 以避免 database 层对 api 层的循环依赖。
    import config  # noqa: WPS433

    fs_dir = Path(getattr(config, "FS_DIR", Path(config.BASE_DIR) / "web" / "fs"))
    subdir_name = str(getattr(config, "CHAT_ATTACHMENT_SUBDIR", "chats") or "chats").strip().strip("/\\") or "chats"

    cur = await db.execute(
        """SELECT ca.id, ca.uuid, ca.storage_subdir, ca.original_name, ca.mime_type,
                  ca.created_at, u.username
             FROM chat_attachments ca
             LEFT JOIN users u ON u.id = ca.user_id
            WHERE COALESCE(ca.storage_subdir, '') = ''"""
    )
    rows = await cur.fetchall()
    await cur.close()

    import mimetypes

    def _guess_ext(name: str, mime: str) -> str:
        n = (name or "").lower()
        if "." in n:
            dot = n.rfind(".")
            e = n[dot:]
            if 2 <= len(e) <= 8 and e.isascii():
                return e
        guess = mimetypes.guess_extension(mime or "") or ""
        if guess and guess.isascii() and len(guess) <= 8:
            return guess
        return ".bin"

    updated = 0
    moved = 0
    for r in rows:
        # aiosqlite.Row 支持按索引或 key 访问；这里用 dict 转换避免歧义
        rd = dict(r) if not isinstance(r, dict) else r
        subdir = _parse_date(rd.get("created_at"))
        uname = _safe_username(rd.get("username") or "default")
        ext = _guess_ext(rd.get("original_name") or "", rd.get("mime_type") or "")
        user_chats_root = fs_dir / uname / subdir_name
        legacy_path = user_chats_root / f"{rd['uuid']}{ext}"
        new_dir = user_chats_root / subdir
        new_path = new_dir / f"{rd['uuid']}{ext}"
        try:
            if legacy_path.exists() and legacy_path.is_file():
                new_dir.mkdir(parents=True, exist_ok=True)
                if not new_path.exists():
                    legacy_path.replace(new_path)
                    moved += 1
        except OSError as exc:
            logger.warning(
                "迁移聊天附件到日期子目录失败 uuid=%s err=%s", rd.get("uuid"), exc
            )
        await db.execute(
            "UPDATE chat_attachments SET storage_subdir = ? WHERE id = ?",
            (subdir, rd["id"]),
        )
        updated += 1

    await db.commit()
    if updated or moved:
        logger.info(
            "chat_attachments.storage_subdir 回填 %d 条，磁盘迁移 %d 个文件",
            updated,
            moved,
        )
