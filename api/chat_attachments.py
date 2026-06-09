"""AI 聊天附件 API：用户在聊天中上传图片/文本/Markdown/Office/PDF 等作为参考材料。

存储路径：web/fs/<username>/chats/<YYYY>/<MM>/<DD>/<uuid>.<ext>
- 复用文件系统的每用户根目录（web/fs/<username>），把附件放在 chats/ 子目录；再按 UTC 上传日期
  三级分片，防止单目录文件过多；用户在文件系统面板里也能按日期浏览自己的聊天附件。
- 数据库 chat_attachments 的 storage_subdir 字段记录该附件对应的日期子目录（如 '2026/04/22'），
  读取时直接从 db 拼路径，不依赖文件系统扫描。
- AI 助手通过工具 `read_chat_attachment(uuid=...)` 读取文本内容或图片 data URL（见 services/ai_skills.py）。
- Office/PDF 等富文档（kind=document）由 MarkItDown 转为 Markdown 后返回 content（见 services/markitdown_convert.py）。
"""

from __future__ import annotations

import logging
import mimetypes
import re
import uuid as _uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config
from api.auth import (
    _is_admin_role,
    assert_user_active,
    authenticate_bearer_credentials,
    get_current_user,
)
from api.filesystem import FS_DIR, _safe_username, get_user_fs_root
from database import get_db
from services.markitdown_convert import is_markitdown_convertible, remove_markdown_sidecar

logger = logging.getLogger("edgeops.chat_attachments")

router = APIRouter(prefix="/api/ai/attachments", tags=["AI 助手·附件"])


# 附件落盘在 web/fs/<username>/<CHAT_ATTACHMENT_SUBDIR>/，复用文件系统的每用户根目录，
# 让用户在 /api/fs 文件面板里也能看到自己上传的聊天附件。
CHAT_SUBDIR = str(getattr(config, "CHAT_ATTACHMENT_SUBDIR", "chats") or "chats").strip().strip("/\\") or "chats"

MAX_BYTES = int(getattr(config, "CHAT_ATTACHMENT_MAX_BYTES", 20 * 1024 * 1024))
SESSION_QUOTA_BYTES = int(
    getattr(config, "CHAT_ATTACHMENT_SESSION_QUOTA_BYTES", 500 * 1024 * 1024)
)

# 允许上传的 kind 判定：以 MIME type + 扩展名为准
_IMAGE_MIME_PREFIX = "image/"
_TEXT_MIME_PREFIXES = ("text/",)
_MARKDOWN_EXTS = {".md", ".markdown", ".mdx"}
_TEXT_EXTS = {
    ".txt", ".log", ".md", ".markdown", ".mdx", ".json", ".yaml", ".yml",
    ".xml", ".csv", ".tsv", ".ini", ".conf", ".cfg", ".env", ".properties",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".rb", ".php", ".sh", ".bash", ".zsh", ".ps1",
    ".bat", ".cmd", ".sql", ".toml", ".lock", ".gitignore", ".dockerfile",
    ".html", ".htm", ".css", ".scss", ".less", ".svg", ".patch", ".diff",
}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}

# 文件名安全：仅保留可见 ascii / 常见中文、替换其它为下划线，防止落盘路径或返回头异常
_UNSAFE_NAME_RE = re.compile(r"[\x00-\x1f\x7f\\/:*?\"<>|]+")


def _get_user_chats_root(user: dict) -> Path:
    """返回当前用户的聊天附件根目录：web/fs/<safe_username>/<CHAT_SUBDIR>/。"""
    fs_root = get_user_fs_root(user)
    root = fs_root / CHAT_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


_DATE_SUBDIR_RE = re.compile(r"^\d{4}(/\d{2}){0,2}$")


def _today_subdir() -> str:
    return datetime.utcnow().strftime("%Y/%m/%d")


def _sanitize_subdir(subdir: str) -> str:
    s = (subdir or "").strip().strip("/\\").replace("\\", "/")
    if not s:
        return ""
    return s if _DATE_SUBDIR_RE.match(s) else ""


def _detect_kind(mime: str, ext: str, original_name: str = "") -> str:
    mime = (mime or "").lower()
    ext_l = (ext or "").lower()
    if mime.startswith(_IMAGE_MIME_PREFIX) or ext_l in _IMAGE_EXTS:
        return "image"
    if ext_l in _MARKDOWN_EXTS:
        return "markdown"
    if any(mime.startswith(p) for p in _TEXT_MIME_PREFIXES) or ext_l in _TEXT_EXTS:
        return "text"
    if is_markitdown_convertible(original_name or f"file{ext_l}", mime):
        return "document"
    return "binary"


def _safe_filename(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "upload"
    name = _UNSAFE_NAME_RE.sub("_", name)
    # 再去掉路径分隔（冗余防御），并限制长度
    name = name.replace("/", "_").replace("\\", "_")[:120]
    return name or "upload"


def _ext_from_name_or_mime(name: str, mime: str) -> str:
    """推断扩展名：先看原文件名，再按 MIME 回退。"""
    n = (name or "").lower()
    if "." in n:
        dot = n.rfind(".")
        ext = n[dot:]
        if 2 <= len(ext) <= 8 and ext.isascii():
            return ext
    # 回退：通过 mimetypes 根据 MIME 取一个合理扩展名
    guess = mimetypes.guess_extension(mime or "") or ""
    if guess and guess.isascii() and len(guess) <= 8:
        return guess
    return ".bin"


def attachment_relative_path(row: dict) -> str:
    """相对用户 web/fs 根目录的路径，如 chats/2026/06/09/<uuid>.png。"""
    subdir = _sanitize_subdir(row.get("storage_subdir") or "")
    ext = _ext_from_name_or_mime(
        row.get("original_name") or row.get("name") or "",
        row.get("mime_type") or row.get("mime") or "",
    )
    uuid_s = (row.get("uuid") or "").strip()
    if subdir:
        return f"{CHAT_SUBDIR}/{subdir}/{uuid_s}{ext}"
    return f"{CHAT_SUBDIR}/{uuid_s}{ext}"


def _resolve_attachment_path(user: dict, uuid_str: str, ext: str, subdir: str = "") -> Path:
    """按 uuid + ext + 可选日期子目录组装落盘路径，并强制落在当前用户 chats 根下。

    subdir 只允许 'YYYY/MM/DD' 样式；非法值会被当成空串，落回 chats/ 根，保证永远不越界。
    """
    root = _get_user_chats_root(user)
    sub = _sanitize_subdir(subdir)
    # UUID 自身即可用作文件名；扩展名做一次清洗（避免 uuid.ext 外带 / 或 ..）
    ext_clean = ext.strip().lower()
    if not ext_clean:
        ext_clean = ".bin"
    if not ext_clean.startswith("."):
        ext_clean = "." + ext_clean
    ext_clean = re.sub(r"[^a-z0-9.]+", "", ext_clean)[:8] or ".bin"
    base = (root / sub) if sub else root
    path = (base / f"{uuid_str}{ext_clean}").resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="附件路径不合法") from exc
    return path


async def _load_attachment_row(db, uuid_str: str) -> Optional[dict]:
    rows = await db.execute_fetchall(
        """SELECT id, uuid, user_id, session_id, message_id, original_name,
                  mime_type, size_bytes, kind, storage_subdir, created_at
             FROM chat_attachments WHERE uuid = ?""",
        (uuid_str,),
    )
    return dict(rows[0]) if rows else None


def _attachment_row_to_dict(row: dict) -> dict:
    return {
        "uuid": row.get("uuid"),
        "name": row.get("original_name") or "",
        "mime": row.get("mime_type") or "",
        "size": int(row.get("size_bytes") or 0),
        "kind": row.get("kind") or "binary",
        "session_id": row.get("session_id"),
        "message_id": row.get("message_id"),
        "storage_subdir": row.get("storage_subdir") or "",
        "created_at": row.get("created_at"),
        "url": f"/api/ai/attachments/{row.get('uuid')}",
        "fs_path": attachment_relative_path(row),
    }


def read_image_pixel_size(path: Path) -> tuple[int, int] | None:
    """读取图片文件像素尺寸 (width, height)。"""
    try:
        from PIL import Image

        with Image.open(path) as im:
            im.load()
            w, h = im.size
            return int(w), int(h)
    except Exception:
        return None


def enrich_image_attachment_meta(row: dict, username: str) -> dict:
    """为 image 附件补充 width/height 与识图内联估算尺寸（写入 row 副本字段）。"""
    if (row.get("kind") or "").lower() != "image":
        return row
    try:
        path = resolve_attachment_file(row, username)
    except Exception:
        return row
    if not path.exists() or not path.is_file():
        return row
    dims = read_image_pixel_size(path)
    if dims:
        row["image_width"], row["image_height"] = dims
        row["width"], row["height"] = dims
    try:
        from services.vision_image import inline_vision_dimension_info

        info = inline_vision_dimension_info(path.read_bytes(), mime=row.get("mime_type") or "")
        row.update({k: v for k, v in info.items() if v is not None})
    except OSError:
        pass
    return row


async def save_bytes_as_chat_attachment(
    user: dict,
    raw: bytes,
    *,
    original_name: str = "remote-fetch.bin",
    mime: str = "",
    session_id: int | None = None,
    source_url: str = "",
) -> dict:
    """将远程拉取的字节保存为聊天附件（供 MCP 等内部调用）。"""
    size = len(raw or b"")
    if size <= 0:
        raise ValueError("附件内容为空")
    if size > MAX_BYTES:
        raise ValueError(f"文件超过附件大小上限（{MAX_BYTES // (1024 * 1024)} MB）")

    db = await get_db()
    sid = int(session_id) if session_id is not None else None
    if sid is not None:
        await _ensure_session_owned(db, user["id"], sid)
        used = await _session_total_bytes(db, user["id"], sid)
        if used + size > SESSION_QUOTA_BYTES:
            raise ValueError("本会话附件累计大小超过限制")

    name = _safe_filename(original_name or "remote-fetch")
    if source_url and name == "remote-fetch":
        try:
            from urllib.parse import unquote, urlparse

            path_part = unquote(urlparse(source_url).path or "")
            tail = Path(path_part).name
            if tail and "." in tail:
                name = _safe_filename(tail.split("?")[0])
        except Exception:
            pass

    mime_val = (mime or "").strip() or (mimetypes.guess_type(name)[0] or "")
    ext = _ext_from_name_or_mime(name, mime_val)
    kind = _detect_kind(mime_val, ext, name)

    uuid_str = _uuid.uuid4().hex
    subdir = _today_subdir()
    path = _resolve_attachment_path(user, uuid_str, ext, subdir=subdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)

    await db.execute(
        """INSERT INTO chat_attachments
               (uuid, user_id, session_id, original_name, mime_type, size_bytes, kind, storage_subdir)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (uuid_str, user["id"], sid, name, mime_val, size, kind, subdir),
    )
    await db.commit()
    row = await _load_attachment_row(db, uuid_str)
    out = _attachment_row_to_dict(row or {
        "uuid": uuid_str,
        "original_name": name,
        "mime_type": mime_val,
        "size_bytes": size,
        "kind": kind,
        "session_id": sid,
        "storage_subdir": subdir,
    })
    if source_url:
        out["source_url"] = source_url
    return out


async def _session_total_bytes(db, user_id: int, session_id: Optional[int]) -> int:
    if not session_id:
        return 0
    rows = await db.execute_fetchall(
        "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM chat_attachments "
        "WHERE user_id = ? AND session_id = ?",
        (user_id, session_id),
    )
    return int(rows[0]["total"] if rows else 0)


async def _resolve_user_via_header_or_query(request: Request) -> dict:
    """下载/预览端点鉴权：优先 Authorization header；退化到 ?token=xxx query 参数。

    用于支持浏览器 `<img src="/api/ai/attachments/<uuid>?token=...">` 直接加载附件图片。
    """
    raw = ""
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth_header:
        parts = auth_header.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            raw = parts[1].strip()
        else:
            raw = auth_header.strip()
    if not raw:
        raw = (request.query_params.get("token") or "").strip()
    if not raw:
        raise HTTPException(status_code=401, detail="未登录")
    user = await authenticate_bearer_credentials(raw)
    if not user:
        raise HTTPException(status_code=401, detail="Token已过期或无效")
    assert_user_active(user)
    return user


async def _ensure_session_owned(db, user_id: int, session_id: int) -> None:
    rows = await db.execute_fetchall(
        "SELECT id FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")


# ── API: 上传 ──
@router.post("")
async def upload_attachment(
    file: UploadFile = File(...),
    session_id: Optional[int] = Form(None),
    user=Depends(get_current_user),
):
    """上传一个附件到 chats/<username>/<uuid>.<ext>；可选绑定到指定 session_id。"""
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="未提供文件")
    raw = await file.read()
    size = len(raw)
    if size <= 0:
        raise HTTPException(status_code=400, detail="附件内容为空")
    if size > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"单个附件大小超过限制（{MAX_BYTES // (1024 * 1024)} MB）",
        )

    db = await get_db()
    if session_id is not None:
        try:
            session_id = int(session_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="session_id 不合法")
        await _ensure_session_owned(db, user["id"], session_id)
        used = await _session_total_bytes(db, user["id"], session_id)
        if used + size > SESSION_QUOTA_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"本会话附件累计大小超过限制（{SESSION_QUOTA_BYTES // (1024 * 1024)} MB），请清理后再试",
            )

    original_name = _safe_filename(file.filename or "upload")
    mime = (file.content_type or "").strip() or (
        mimetypes.guess_type(original_name)[0] or ""
    )
    ext = _ext_from_name_or_mime(original_name, mime)
    kind = _detect_kind(mime, ext, original_name)

    uuid_str = _uuid.uuid4().hex
    subdir = _today_subdir()
    path = _resolve_attachment_path(user, uuid_str, ext, subdir=subdir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    except OSError as exc:
        logger.exception("写入附件失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"保存附件失败: {exc}")

    await db.execute(
        """INSERT INTO chat_attachments
               (uuid, user_id, session_id, original_name, mime_type, size_bytes, kind, storage_subdir)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (uuid_str, user["id"], session_id, original_name, mime, size, kind, subdir),
    )
    await db.commit()
    row = await _load_attachment_row(db, uuid_str)
    return {"success": True, "attachment": _attachment_row_to_dict(row or {
        "uuid": uuid_str,
        "original_name": original_name,
        "mime_type": mime,
        "size_bytes": size,
        "kind": kind,
        "session_id": session_id,
        "storage_subdir": subdir,
    })}


# ── API: 元信息 ──
@router.get("/{uuid_str}/meta")
async def attachment_meta(uuid_str: str, user=Depends(get_current_user)):
    db = await get_db()
    row = await _load_attachment_row(db, uuid_str)
    if not row:
        raise HTTPException(status_code=404, detail="附件不存在")
    if row["user_id"] != user["id"] and not _is_admin_role(user.get("role")):
        raise HTTPException(status_code=403, detail="无权访问该附件")
    return {"success": True, "attachment": _attachment_row_to_dict(row)}


# ── API: 列出当前会话的附件 ──
@router.get("")
async def list_attachments(
    session_id: Optional[int] = None,
    user=Depends(get_current_user),
):
    db = await get_db()
    if session_id is not None:
        await _ensure_session_owned(db, user["id"], session_id)
        rows = await db.execute_fetchall(
            """SELECT id, uuid, user_id, session_id, message_id, original_name,
                      mime_type, size_bytes, kind, storage_subdir, created_at
                 FROM chat_attachments
                WHERE user_id = ? AND session_id = ?
                ORDER BY id ASC""",
            (user["id"], session_id),
        )
    else:
        rows = await db.execute_fetchall(
            """SELECT id, uuid, user_id, session_id, message_id, original_name,
                      mime_type, size_bytes, kind, storage_subdir, created_at
                 FROM chat_attachments
                WHERE user_id = ? ORDER BY id DESC LIMIT 200""",
            (user["id"],),
        )
    return {
        "success": True,
        "attachments": [_attachment_row_to_dict(dict(r)) for r in rows],
    }


# ── API: 下载/预览 ──
# 浏览器通过 <img src="/api/ai/attachments/<uuid>?token=..."> 直接加载时，无法带 Authorization header，
# 因此这里既支持 header 也支持 ?token= query 参数（参考 ws 端点的做法）。
@router.get("/{uuid_str}")
async def download_attachment(uuid_str: str, request: Request):
    user = await _resolve_user_via_header_or_query(request)
    db = await get_db()
    row = await _load_attachment_row(db, uuid_str)
    if not row:
        raise HTTPException(status_code=404, detail="附件不存在")
    if row["user_id"] != user["id"] and not _is_admin_role(user.get("role")):
        raise HTTPException(status_code=403, detail="无权访问该附件")
    # 关键：必须走 resolve_attachment_file，它会用 row.storage_subdir 拼日期子目录；
    # 之前直接调 _resolve_attachment_path 且不传 subdir，会误落到 chats/ 根找文件，
    # 表现为浏览器拿到 404 "附件文件已丢失"，其实文件在 chats/YYYY/MM/DD/ 下。
    username = await _load_username(db, row["user_id"])
    path = resolve_attachment_file(row, username)
    ext = _ext_from_name_or_mime(row.get("original_name") or "", row.get("mime_type") or "")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="附件文件已丢失")
    filename = row.get("original_name") or f"{uuid_str}{ext}"
    return FileResponse(
        str(path),
        media_type=row.get("mime_type") or "application/octet-stream",
        filename=filename,
    )


async def _load_username(db, user_id: int) -> str:
    rows = await db.execute_fetchall("SELECT username FROM users WHERE id = ?", (user_id,))
    return (rows[0]["username"] if rows else "default") or "default"


class BindBody(BaseModel):
    uuids: list[str] = []
    session_id: int
    message_id: Optional[int] = None


@router.post("/bind")
async def bind_attachments(body: BindBody, user=Depends(get_current_user)):
    """把上传时未绑定会话的附件绑定到指定 session_id（及可选 message_id）。"""
    db = await get_db()
    if not body.uuids:
        return {"success": True, "updated": 0}
    await _ensure_session_owned(db, user["id"], body.session_id)
    updated = 0
    for u in body.uuids:
        u = (u or "").strip()
        if not u:
            continue
        row = await _load_attachment_row(db, u)
        if not row or row["user_id"] != user["id"]:
            continue
        await db.execute(
            "UPDATE chat_attachments SET session_id = ?, message_id = COALESCE(?, message_id) "
            "WHERE uuid = ? AND user_id = ?",
            (body.session_id, body.message_id, u, user["id"]),
        )
        updated += 1
    await db.commit()
    return {"success": True, "updated": updated}


# ── API: 删除 ──
@router.delete("/{uuid_str}")
async def delete_attachment(uuid_str: str, user=Depends(get_current_user)):
    db = await get_db()
    row = await _load_attachment_row(db, uuid_str)
    if not row:
        raise HTTPException(status_code=404, detail="附件不存在")
    if row["user_id"] != user["id"] and not _is_admin_role(user.get("role")):
        raise HTTPException(status_code=403, detail="无权删除该附件")
    # 同 download：必须用 resolve_attachment_file 才能命中日期子目录里的真实文件
    username = await _load_username(db, row["user_id"])
    path = resolve_attachment_file(row, username)
    try:
        if path.exists():
            remove_markdown_sidecar(path)
            path.unlink()
    except OSError as exc:
        logger.warning("删除附件文件失败 uuid=%s err=%s", uuid_str, exc)
    await db.execute("DELETE FROM chat_attachments WHERE uuid = ?", (uuid_str,))
    await db.commit()
    return {"success": True}


# ── 对外工具：供 AI / chat_stream 注入使用 ──
async def load_attachments_for_user(db, user_id: int, uuids: list[str]) -> list[dict]:
    """按 UUID 批量读取属于该用户的附件元信息（保持顺序）。未找到或非本人的忽略。"""
    if not uuids:
        return []
    seen = set()
    ordered: list[dict] = []
    for u in uuids:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        rows = await db.execute_fetchall(
            """SELECT id, uuid, user_id, session_id, message_id, original_name,
                      mime_type, size_bytes, kind, storage_subdir, created_at,
                      ai_description, ai_description_model, ai_description_updated_at
                 FROM chat_attachments WHERE uuid = ? AND user_id = ?""",
            (u, user_id),
        )
        if rows:
            ordered.append(dict(rows[0]))
    return ordered


async def save_attachment_ai_description(
    db,
    *,
    user_id: int,
    uuid: str,
    description: str,
    model: str = "",
) -> bool:
    """把 AI 对某张图片的文本识别结果写回附件行；仅允许写自己的附件。

    - `description` 空字符串视为"清空描述"；后续视觉路径会重新内联原图。
    - 成功返回 True；未命中（uuid 不存在或跨用户）返回 False。
    """
    uuid_s = (uuid or "").strip()
    if not uuid_s:
        return False
    cur = await db.execute(
        """UPDATE chat_attachments
              SET ai_description = ?,
                  ai_description_model = ?,
                  ai_description_updated_at = CURRENT_TIMESTAMP
            WHERE uuid = ? AND user_id = ?""",
        ((description or "").strip(), (model or "").strip()[:120], uuid_s, user_id),
    )
    changed = cur.rowcount or 0
    await db.commit()
    return changed > 0


def resolve_attachment_file(db_row: dict, username: str) -> Path:
    """给定 chat_attachments 行与用户名，返回磁盘上的附件路径（保持沙箱限制）。

    使用 db_row.storage_subdir 拼出日期子目录；老数据迁移前为空时回落到 chats/ 根。
    """
    owner_user = {"username": username}
    ext = _ext_from_name_or_mime(db_row.get("original_name") or "", db_row.get("mime_type") or "")
    subdir = _sanitize_subdir(db_row.get("storage_subdir") or "")
    return _resolve_attachment_path(owner_user, db_row.get("uuid") or "", ext, subdir=subdir)


def humanize_size(size: int) -> str:
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        return "0 B"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.2f} MB"


def build_attachment_message_suffix(attachments: list[dict]) -> str:
    """把附件元信息格式化为追加到用户消息末尾的 Markdown 段，供 AI 读取。

    对用户可见部分保持简短：文件名、类型、大小、uuid、路径、图片尺寸等关键字段。
    详细用法说明见系统提示与工具 schema，不重复写在清单里。
    """
    if not attachments:
        return ""
    lines = [
        "",
        "---",
        "**📎 附件**",
    ]
    for a in attachments:
        name = a.get("original_name") or a.get("name") or "(未命名)"
        kind = a.get("kind") or "binary"
        size = humanize_size(a.get("size_bytes") or a.get("size") or 0)
        uuid_s = a.get("uuid") or ""
        fs_path = attachment_relative_path(a)
        line = f"- `{name}` · {kind} · {size} · uuid `{uuid_s}` · `{fs_path}`"
        if (kind or "").lower() == "image":
            ow = a.get("image_width") or a.get("width") or a.get("original_width")
            oh = a.get("image_height") or a.get("height") or a.get("original_height")
            if ow and oh:
                dim = f"{int(ow)}×{int(oh)}"
                mw = a.get("model_view_width")
                mh = a.get("model_view_height")
                vw = a.get("vision_width")
                vh = a.get("vision_height")
                if mw and mh and (int(mw) != int(ow) or int(mh) != int(oh)):
                    dim += f" (视图 {int(mw)}×{int(mh)})"
                elif vw and vh and (int(vw) != int(ow) or int(vh) != int(oh)):
                    dim += f" (识图 {int(vw)}×{int(vh)})"
                line += f" · {dim}"
        lines.append(line)
        desc = (a.get("ai_description") or "").strip()
        if desc and (kind or "").lower() == "image":
            short = desc if len(desc) <= 1500 else (desc[:1500] + " …")
            for ln in short.splitlines() or [short]:
                lines.append(f"  > {ln}")
    return "\n".join(lines)


def _migrate_legacy_chats_dir() -> None:
    """把旧版 <BASE_DIR>/chats/<username>/* 中的附件迁移到 web/fs/<username>/<CHAT_SUBDIR>/。

    - 早期版本的附件落盘在仓库根目录的 chats/ 下；迁到每用户 fs 根目录后，需要把存量文件搬过去，
      否则历史会话里的图片/文本附件会 404。
    - 迁移策略：逐文件 rename；目标已存在则跳过（不覆盖）。全部迁完后若 <username>/ 为空则删目录，
      再若 chats/ 整个根目录也空就一并删除。异常一律吞掉、只记录日志，避免阻塞模块导入。
    """
    legacy_root = Path(config.BASE_DIR) / "chats"
    if not legacy_root.exists() or not legacy_root.is_dir():
        return
    try:
        for user_dir in legacy_root.iterdir():
            if not user_dir.is_dir():
                continue
            safe_name = _safe_username(user_dir.name)
            target_dir = FS_DIR / safe_name / CHAT_SUBDIR
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("创建聊天附件目录失败 user=%s err=%s", safe_name, exc)
                continue
            moved = 0
            for f in user_dir.iterdir():
                if not f.is_file():
                    continue
                dest = target_dir / f.name
                if dest.exists():
                    continue  # 不覆盖已有文件
                try:
                    f.replace(dest)
                    moved += 1
                except OSError as exc:
                    logger.warning("迁移聊天附件失败 src=%s dst=%s err=%s", f, dest, exc)
            if moved:
                logger.info("已迁移 %d 个旧聊天附件 → %s", moved, target_dir)
            try:
                if not any(user_dir.iterdir()):
                    user_dir.rmdir()
            except OSError:
                pass
        try:
            if not any(legacy_root.iterdir()):
                legacy_root.rmdir()
        except OSError:
            pass
    except OSError as exc:
        logger.warning("扫描旧聊天附件目录失败: %s", exc)


# 模块导入时执行一次迁移；幂等，已在新路径的文件不会被覆盖。
_migrate_legacy_chats_dir()
