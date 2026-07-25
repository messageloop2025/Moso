"""本会话文件资源索引：聊天中访问/创建的文件，供后续轮次自动注入与找回。

来源合并：
1. 会话区索引文件 `chats/sessions/<session_id>/.edgeops_session_files.json`（工具写/读时登记）
2. DB：`ai_artifacts`（本会话成果物）
3. DB：`chat_attachments`（本会话上传附件）

正文不会自动回灌；清单只提供 uuid / 路径 / 标题，模型用 read_* / update_* / fs_read 取内容。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger("edgeops.session_file_resources")

INDEX_FILENAME = ".edgeops_session_files.json"
_MAX_INDEX_ITEMS = max(20, min(500, int(os.getenv("EDGEOPS_SESSION_FILE_INDEX_MAX", "120"))))
_MAX_SECTION_ITEMS = max(8, min(80, int(os.getenv("EDGEOPS_SESSION_FILE_SECTION_MAX", "36"))))
_MAX_WORKSPACE_SCAN = max(10, min(100, int(os.getenv("EDGEOPS_SESSION_FILE_WORKSPACE_SCAN", "40"))))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _session_dir(username: str, session_id: int) -> Path | None:
    try:
        from api.filesystem import get_user_fs_root

        root = get_user_fs_root({"username": username})
        chat_sub = str(getattr(config, "CHAT_ATTACHMENT_SUBDIR", "chats") or "chats").strip().strip("/\\") or "chats"
        d = (root / chat_sub / "sessions" / str(int(session_id))).resolve()
        d.relative_to(root.resolve())
        return d
    except Exception as exc:
        logger.debug("session_dir resolve failed sid=%s: %s", session_id, exc)
        return None


def _index_path(username: str, session_id: int) -> Path | None:
    d = _session_dir(username, session_id)
    if not d:
        return None
    return d / INDEX_FILENAME


def _load_index(username: str, session_id: int) -> list[dict[str, Any]]:
    path = _index_path(username, session_id)
    if not path or not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = raw.get("files") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for it in items:
        if isinstance(it, dict) and (it.get("path") or it.get("ref") or it.get("uuid")):
            out.append(it)
    return out


def _save_index(username: str, session_id: int, items: list[dict[str, Any]]) -> None:
    path = _index_path(username, session_id)
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 新的在前；截断上限
        trimmed = items[:_MAX_INDEX_ITEMS]
        path.write_text(
            json.dumps({"version": 1, "files": trimmed}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("session file index save failed sid=%s: %s", session_id, exc)


def record_session_file_resource(
    *,
    username: str,
    session_id: int | None,
    kind: str,
    path: str = "",
    uuid: str = "",
    title: str = "",
    entry_file: str = "",
    note: str = "",
) -> None:
    """登记本会话访问/创建的文件（幂等：同 kind+uuid 或 kind+path 覆盖更新）。"""
    if not session_id or not (username or "").strip():
        return
    kind_s = (kind or "fs").strip().lower() or "fs"
    path_s = (path or "").strip().replace("\\", "/")
    uuid_s = (uuid or "").strip()
    if not path_s and not uuid_s:
        return
    ref = uuid_s or path_s
    items = _load_index(username, int(session_id))
    now = _utc_now_iso()
    new_item = {
        "kind": kind_s,
        "ref": ref,
        "uuid": uuid_s,
        "path": path_s,
        "title": (title or "").strip()[:200],
        "entry_file": (entry_file or "").strip()[:200],
        "note": (note or "").strip()[:200],
        "updated_at": now,
    }
    rest = [
        it
        for it in items
        if not (
            (uuid_s and (it.get("uuid") or "") == uuid_s and (it.get("kind") or "") == kind_s)
            or (not uuid_s and path_s and (it.get("path") or "") == path_s and (it.get("kind") or "") == kind_s)
        )
    ]
    created = next(
        (
            it.get("created_at")
            for it in items
            if (uuid_s and it.get("uuid") == uuid_s)
            or (not uuid_s and path_s and it.get("path") == path_s)
        ),
        None,
    )
    new_item["created_at"] = created or now
    _save_index(username, int(session_id), [new_item] + rest)


async def _load_session_artifacts(db, user_id: int, session_id: int, limit: int) -> list[dict[str, Any]]:
    try:
        rows = await db.execute_fetchall(
            """SELECT uuid, title, kind, storage_subdir, entry_file, file_count, total_bytes, created_at
                 FROM ai_artifacts WHERE user_id = ? AND session_id = ?
                 ORDER BY id DESC LIMIT ?""",
            (user_id, session_id, limit),
        )
    except Exception as exc:
        logger.debug("load session artifacts failed: %s", exc)
        return []
    from api.ai_artifacts import _workspace_relpath_for_artifact

    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        sub = d.get("storage_subdir") or ""
        entry = d.get("entry_file") or ""
        uuid_s = d.get("uuid") or ""
        out.append({
            "kind": "artifact",
            "uuid": uuid_s,
            "title": d.get("title") or "成果物",
            "path": _workspace_relpath_for_artifact(sub, entry),
            "entry_file": entry,
            "file_count": int(d.get("file_count") or 0),
            "created_at": d.get("created_at"),
            "markdown_link": f"[{d.get('title') or '成果物'}](artifact:{uuid_s})",
        })
    return out


async def _load_session_attachments(db, user_id: int, session_id: int, limit: int) -> list[dict[str, Any]]:
    try:
        rows = await db.execute_fetchall(
            """SELECT uuid, original_name, kind, size_bytes, storage_subdir, created_at
                 FROM chat_attachments WHERE user_id = ? AND session_id = ?
                 ORDER BY id DESC LIMIT ?""",
            (user_id, session_id, limit),
        )
    except Exception as exc:
        logger.debug("load session attachments failed: %s", exc)
        return []
    try:
        from api.chat_attachments import attachment_relative_path
    except Exception:
        attachment_relative_path = None  # type: ignore
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        uuid_s = d.get("uuid") or ""
        name = d.get("original_name") or "附件"
        path = ""
        if attachment_relative_path:
            try:
                path = attachment_relative_path(d) or ""
            except Exception:
                path = ""
        out.append({
            "kind": "attachment",
            "uuid": uuid_s,
            "title": name,
            "path": path,
            "mime_kind": d.get("kind") or "",
            "created_at": d.get("created_at"),
        })
    return out


def _scan_session_workspace(username: str, session_id: int, limit: int) -> list[dict[str, Any]]:
    d = _session_dir(username, session_id)
    if not d or not d.is_dir():
        return []
    chat_sub = str(getattr(config, "CHAT_ATTACHMENT_SUBDIR", "chats") or "chats").strip().strip("/\\") or "chats"
    prefix = f"{chat_sub}/sessions/{int(session_id)}"
    files: list[tuple[float, Path]] = []
    try:
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            if p.name == INDEX_FILENAME or p.name.startswith("."):
                continue
            try:
                files.append((p.stat().st_mtime, p))
            except OSError:
                continue
    except Exception:
        return []
    files.sort(key=lambda x: x[0], reverse=True)
    out: list[dict[str, Any]] = []
    for _mt, p in files[:limit]:
        try:
            rel = p.relative_to(d).as_posix()
        except ValueError:
            continue
        out.append({
            "kind": "workspace",
            "path": f"{prefix}/{rel}",
            "title": p.name,
        })
    return out


def _merge_resources(*groups: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """按 uuid / path 去重，保留先出现的（优先 DB artifacts → attachments → index → workspace）。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for group in groups:
        for it in group:
            uuid_s = (it.get("uuid") or "").strip()
            path_s = (it.get("path") or "").strip()
            key = f"u:{uuid_s}" if uuid_s else f"p:{path_s}"
            if not uuid_s and not path_s:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
            if len(out) >= limit:
                return out
    return out


async def build_session_file_resources_section(
    db,
    *,
    user_id: int,
    session_id: int,
    username: str,
) -> str:
    """生成注入 system 的「本会话文件资源」段；无资源时返回空串。"""
    if not session_id or not user_id:
        return ""
    uname = (username or "").strip() or "default"
    artifacts = await _load_session_artifacts(db, user_id, int(session_id), _MAX_SECTION_ITEMS)
    attachments = await _load_session_attachments(db, user_id, int(session_id), _MAX_SECTION_ITEMS)
    indexed = _load_index(uname, int(session_id))
    workspace = _scan_session_workspace(uname, int(session_id), _MAX_WORKSPACE_SCAN)
    merged = _merge_resources(artifacts, attachments, indexed, workspace, limit=_MAX_SECTION_ITEMS)
    if not merged:
        return f"""## 本会话文件资源（session_id={int(session_id)}）
当前会话尚无已登记的附件 / 成果物 / 工作区文件。
- 用户上传附件会出现在消息末尾「📎 附件」清单，并用 `read_chat_attachment` 读取。
- 交付报告用 `create_chat_artifact`；之后修订必须 `list_chat_artifacts` → `read_chat_artifact_file` → `update_chat_artifact`（同一 UUID），**禁止**因「正文不在上下文」就整份 recreate。
"""

    lines = [
        f"## 本会话文件资源（session_id={int(session_id)}）",
        "以下为本会话已创建或登记的文件句柄（**仅元信息，不含文件正文**）。修订已有成果时：",
        "1. 从本清单取 `uuid` / `path`（或 `markdown_link`）；",
        "2. 成果物：`read_chat_artifact_file` → `update_chat_artifact`（**同一 UUID**）；",
        "3. 附件：`read_chat_attachment`；工作区文件：`fs_read_file` / `fs_write_file`（精确 path）。",
        "**禁止**因「HTML/内容不在当前上下文」就再 `create_chat_artifact` 整份重建。",
        "",
    ]
    art_n = att_n = other_n = 0
    for it in merged:
        kind = (it.get("kind") or "fs").strip().lower()
        title = (it.get("title") or it.get("path") or it.get("uuid") or "未命名").strip()
        uuid_s = (it.get("uuid") or "").strip()
        path_s = (it.get("path") or "").strip()
        entry = (it.get("entry_file") or "").strip()
        if kind == "artifact":
            art_n += 1
            link = it.get("markdown_link") or (f"[{title}](artifact:{uuid_s})" if uuid_s else title)
            bit = f"- **成果物** {link}"
            if entry:
                bit += f" · entry `{entry}`"
            if path_s:
                bit += f" · `{path_s}`"
            if uuid_s:
                bit += f" · uuid `{uuid_s}`"
            lines.append(bit)
        elif kind == "attachment":
            att_n += 1
            bit = f"- **附件** `{title}`"
            if it.get("mime_kind"):
                bit += f" · {it.get('mime_kind')}"
            if uuid_s:
                bit += f" · uuid `{uuid_s}`"
            if path_s:
                bit += f" · `{path_s}`"
            lines.append(bit)
        else:
            other_n += 1
            label = "工作区" if kind == "workspace" else kind
            bit = f"- **{label}** `{title}`"
            if path_s:
                bit += f" · `{path_s}`"
            if uuid_s:
                bit += f" · uuid `{uuid_s}`"
            lines.append(bit)

    lines.append("")
    lines.append(
        f"（清单共 {len(merged)} 项：成果物 {art_n} · 附件 {att_n} · 其它 {other_n}；"
        f"完整列表可用 `list_chat_artifacts(session_id={int(session_id)})`。）"
    )
    return "\n".join(lines)
