"""工具结果溢出落盘：大号 tool 返回体写入 chats/.../spill/<uuid>.data，上下文中只保留哨兵行 + 压缩预览。

AI 通过 read_chat_data 按片段读取全量，避免对话上下文被单条工具输出撑爆。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from api.chat_attachments import CHAT_SUBDIR, _get_user_chats_root, _sanitize_subdir

logger = logging.getLogger("edgeops.chat_tool_spill")

_SPILL_SENTINEL_RE = re.compile(
    r"^\[\[EDGEOPS_CHAT_DATA ref=(?P<ref>[0-9a-fA-F-]{36}) "
    r"subdir=(?P<subdir>[\d/]+) chars=(?P<chars>\d+) "
    r"tool=(?P<tool>\S+) session=(?P<session>\S+)\]\]\s*$",
    re.MULTILINE,
)

CHAT_TOOL_SPILL_MIN_CHARS = int(getattr(config, "CHAT_TOOL_SPILL_MIN_CHARS", 2500))
CHAT_TOOL_SPILL_READ_MAX_CHARS = int(getattr(config, "CHAT_TOOL_SPILL_READ_MAX_CHARS", 500_000))
CHAT_HISTORY_TOOL_SPILL_SHRINK_THRESHOLD = int(getattr(config, "CHAT_HISTORY_TOOL_SPILL_SHRINK_THRESHOLD", 900))


def _today_subdir_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y/%m/%d")


def _spill_dir(user_chats_root: Path, storage_subdir: str) -> Path:
    sub = _sanitize_subdir(storage_subdir)
    base = (user_chats_root / sub) if sub else user_chats_root
    d = base / "spill"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_chat_tool_spill_sync(
    user: dict,
    session_id: int | None,
    tool_name: str,
    tool_call_id: str | None,
    full_text: str,
) -> dict[str, Any] | None:
    """将完整工具输出写入磁盘。过短则跳过。返回 spill 元数据 dict。"""
    raw = full_text or ""
    if len(raw) < CHAT_TOOL_SPILL_MIN_CHARS:
        return None
    spill_id = str(uuid.uuid4())
    storage_subdir = _today_subdir_utc()
    root = _get_user_chats_root(user)
    sdir = _spill_dir(root, storage_subdir)
    data_path = (sdir / f"{spill_id}.data").resolve()
    meta_path = (sdir / f"{spill_id}.meta.json").resolve()
    try:
        data_path.write_text(raw, encoding="utf-8", errors="surrogateescape")
    except OSError as e:
        logger.warning("chat tool spill write failed: %s", e)
        return None
    meta = {
        "user_id": int(user["id"]),
        "session_id": int(session_id) if session_id is not None else None,
        "tool_name": (tool_name or "").strip(),
        "tool_call_id": (tool_call_id or "").strip(),
        "spill_id": spill_id,
        "storage_subdir": storage_subdir,
        "char_length": len(raw),
        "byte_length": data_path.stat().st_size,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("chat tool spill meta write failed: %s", e)
        try:
            data_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    rel_data = f"{CHAT_SUBDIR}/{storage_subdir}/spill/{spill_id}.data"
    return {
        **meta,
        "relative_path": rel_data,
    }


def format_tool_message_with_spill(spill: dict[str, Any], compact_inner: str) -> str:
    sess = str(spill["session_id"]) if spill.get("session_id") is not None else "none"
    tool_s = (spill.get("tool_name") or "").replace(" ", "_")[:80] or "tool"
    line = (
        f"[[EDGEOPS_CHAT_DATA ref={spill['spill_id']} subdir={spill['storage_subdir']} "
        f"chars={spill['char_length']} tool={tool_s} session={sess}]]"
    )
    rel = spill.get("relative_path") or ""
    return (
        f"{line}\n"
        f"【说明】完整输出已落盘（UTF-8）：用户文件根下 `{rel}`。\n"
        f"需要全量或片段时请调用 **read_chat_data**（spill_id、date_subdir=subdir、mode=head_tail|head|tail|range）。\n"
        f"以下为供当前轮参考的压缩预览（不可替代全量）：\n\n"
        f"{compact_inner}"
    )


async def spill_and_wrap_tool_message(
    user: dict,
    session_id: int | None,
    tool_name: str,
    tool_call_id: str | None,
    full_result: str,
    compact_for_llm: str,
) -> str:
    """必要时落盘并包装 tool 消息正文；read_chat_attachment 豁免落盘（调用方保证不传或在外层跳过）。"""
    if (tool_name or "").strip() == "read_chat_attachment":
        return compact_for_llm
    spill = await asyncio.to_thread(
        write_chat_tool_spill_sync, user, session_id, tool_name, tool_call_id, full_result
    )
    if not spill:
        return compact_for_llm
    return format_tool_message_with_spill(spill, compact_for_llm)


def _resolve_spill_paths(user: dict, storage_subdir: str, spill_id: str) -> tuple[Path, Path]:
    sid = (spill_id or "").strip()
    try:
        uuid.UUID(sid)
    except Exception as exc:
        raise ValueError("spill_id 非法") from exc
    root = _get_user_chats_root(user)
    sdir = _spill_dir(root, storage_subdir)
    data_path = (sdir / f"{sid}.data").resolve()
    meta_path = (sdir / f"{sid}.meta.json").resolve()
    try:
        data_path.relative_to(root.resolve())
        meta_path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("路径越界") from exc
    return data_path, meta_path


def _load_and_check_meta(meta_path: Path, user: dict, session_id: int | None) -> dict[str, Any]:
    if not meta_path.is_file():
        raise FileNotFoundError("spill 元数据不存在")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("spill 元数据损坏") from exc
    if int(meta.get("user_id") or 0) != int(user["id"]):
        raise PermissionError("无权访问此 spill")
    meta_sid = meta.get("session_id")
    if session_id is not None and meta_sid is not None and int(meta_sid) != int(session_id):
        raise PermissionError("spill 不属于当前会话")
    return meta


def read_chat_data_slice_sync(
    user: dict,
    session_id: int | None,
    spill_id: str,
    date_subdir: str,
    mode: str,
    *,
    head_chars: int = 8000,
    tail_chars: int = 8000,
    range_start: int = 0,
    max_chars: int = 16_000,
) -> dict[str, Any]:
    """从落盘文件读取片段（字符偏移，UTF-8 解码后）。"""
    data_path, meta_path = _resolve_spill_paths(user, date_subdir, spill_id)
    meta = _load_and_check_meta(meta_path, user, session_id)
    if not data_path.is_file():
        return {"success": False, "error": "数据文件不存在"}
    try:
        raw = data_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"success": False, "error": f"读取失败: {e}"}
    n = len(raw)
    mode = (mode or "head_tail").strip().lower()
    head_chars = max(0, min(int(head_chars or 0), CHAT_TOOL_SPILL_READ_MAX_CHARS))
    tail_chars = max(0, min(int(tail_chars or 0), CHAT_TOOL_SPILL_READ_MAX_CHARS))
    range_start = max(0, int(range_start or 0))
    max_chars = max(256, min(int(max_chars or 0), CHAT_TOOL_SPILL_READ_MAX_CHARS))

    if mode == "head":
        chunk = raw[:head_chars]
        return {
            "success": True,
            "mode": "head",
            "total_chars": n,
            "returned_chars": len(chunk),
            "range": [0, len(chunk)],
            "content": chunk,
            "meta": {"tool": meta.get("tool_name"), "spill_id": spill_id, "subdir": date_subdir},
        }
    if mode == "tail":
        chunk = raw[-tail_chars:] if tail_chars else ""
        return {
            "success": True,
            "mode": "tail",
            "total_chars": n,
            "returned_chars": len(chunk),
            "range": [max(0, n - len(chunk)), n],
            "content": chunk,
            "meta": {"tool": meta.get("tool_name"), "spill_id": spill_id, "subdir": date_subdir},
        }
    if mode == "range":
        end = min(n, range_start + max_chars)
        chunk = raw[range_start:end]
        return {
            "success": True,
            "mode": "range",
            "total_chars": n,
            "returned_chars": len(chunk),
            "range": [range_start, end],
            "truncated": end < n,
            "content": chunk,
            "meta": {"tool": meta.get("tool_name"), "spill_id": spill_id, "subdir": date_subdir},
        }
    # head_tail
    if n <= head_chars + tail_chars + 8:
        return {
            "success": True,
            "mode": "head_tail",
            "total_chars": n,
            "returned_chars": n,
            "range": [0, n],
            "content": raw,
            "meta": {"tool": meta.get("tool_name"), "spill_id": spill_id, "subdir": date_subdir},
        }
    head = raw[:head_chars]
    tail = raw[-tail_chars:]
    omitted = n - head_chars - tail_chars
    body = head + f"\n…（省略中间 {omitted} 字符；可用 mode=range 指定 offset）…\n" + tail
    return {
        "success": True,
        "mode": "head_tail",
        "total_chars": n,
        "returned_chars": len(body),
        "range": [0, n],
        "content": body,
        "meta": {"tool": meta.get("tool_name"), "spill_id": spill_id, "subdir": date_subdir},
    }


def extract_spill_sentinel_line(content: str) -> str | None:
    m = _SPILL_SENTINEL_RE.search(content or "")
    if not m:
        return None
    return m.group(0).strip()


def shrink_tool_message_for_history_budget(
    raw_content: str,
    per_msg: int,
    *,
    role: str,
) -> str | None:
    """若 tool 消息含 EDGEOPS 哨兵，则在历史预算极紧时保留哨兵 + 提示，去掉大段预览。"""
    if (role or "") != "tool" or not raw_content or "[[EDGEOPS_CHAT_DATA" not in raw_content:
        return None
    line = extract_spill_sentinel_line(raw_content)
    if not line:
        return None
    # 预算够时仍走统一截断；只在明显不够时替换
    if len(raw_content) <= per_msg:
        return None
    if per_msg >= CHAT_HISTORY_TOOL_SPILL_SHRINK_THRESHOLD:
        return None
    m = _SPILL_SENTINEL_RE.search(raw_content)
    if not m:
        return None
    ref = m.group("ref")
    subdir = m.group("subdir")
    return (
        f"{line}\n"
        f"【上下文压缩】下方预览已从历史中省略（{len(raw_content)} 字符→仅保留哨兵）。"
        f"完整内容仍在磁盘，请调用 read_chat_data(spill_id=\"{ref}\", date_subdir=\"{subdir}\", mode=\"head_tail\" 或 range)。"
    )


async def read_chat_data_slice_async(
    user: dict,
    session_id: int | None,
    spill_id: str,
    date_subdir: str,
    mode: str,
    **kwargs: Any,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        read_chat_data_slice_sync, user, session_id, spill_id, date_subdir, mode, **kwargs
    )
