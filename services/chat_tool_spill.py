"""工具结果溢出落盘：大号 tool 返回体写入 chats/.../spill/<uuid>.data，上下文中仅保留哨兵行与读取指引。

AI 必须通过 read_chat_data 按片段读取全量，禁止依据压缩预览或推理自行补全工具结果。
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
from api.chat_attachments import (
    CHAT_SUBDIR,
    _get_user_chats_root,
    _sanitize_subdir,
    session_storage_subdir,
)

logger = logging.getLogger("edgeops.chat_tool_spill")

_SPILL_SENTINEL_RE = re.compile(
    r"^\[\[EDGEOPS_CHAT_DATA ref=(?P<ref>[0-9a-fA-F-]{36}) "
    r"subdir=(?P<subdir>[\w./-]+) chars=(?P<chars>\d+) "
    r"tool=(?P<tool>\S+) session=(?P<session>\S+)\]\]\s*$",
    re.MULTILINE,
)

CHAT_TOOL_SPILL_MIN_CHARS = int(getattr(config, "CHAT_TOOL_SPILL_MIN_CHARS", 2500))
CHAT_TOOL_SPILL_READ_MAX_CHARS = int(getattr(config, "CHAT_TOOL_SPILL_READ_MAX_CHARS", 500_000))
CHAT_TOOL_SPILL_READ_DEFAULT_HEAD_CHARS = int(getattr(config, "CHAT_TOOL_SPILL_READ_DEFAULT_HEAD_CHARS", 32_000))
CHAT_TOOL_SPILL_READ_DEFAULT_TAIL_CHARS = int(getattr(config, "CHAT_TOOL_SPILL_READ_DEFAULT_TAIL_CHARS", 32_000))
CHAT_TOOL_SPILL_READ_DEFAULT_RANGE_CHARS = int(getattr(config, "CHAT_TOOL_SPILL_READ_DEFAULT_RANGE_CHARS", 64_000))
CHAT_TOOL_SPILL_READ_MESSAGE_MAX_CHARS = int(getattr(config, "CHAT_TOOL_SPILL_READ_MESSAGE_MAX_CHARS", 128_000))
CHAT_HISTORY_TOOL_SPILL_SHRINK_THRESHOLD = int(getattr(config, "CHAT_HISTORY_TOOL_SPILL_SHRINK_THRESHOLD", 900))
CHAT_TOOL_SPILL_INCLUDE_PREVIEW = bool(
    getattr(config, "CHAT_TOOL_SPILL_INCLUDE_PREVIEW", False)
)

_SPILL_READ_TOOL_NAMES = frozenset({"read_chat_data", "fs_read_file"})


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
    storage_subdir = session_storage_subdir(session_id)
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
    spill_id = spill["spill_id"]
    subdir = spill["storage_subdir"]
    chars = spill.get("char_length") or 0
    line = (
        f"[[EDGEOPS_CHAT_DATA ref={spill_id} subdir={subdir} "
        f"chars={chars} tool={tool_s} session={sess}]]"
    )
    rel = spill.get("relative_path") or ""
    if CHAT_TOOL_SPILL_INCLUDE_PREVIEW:
        body = (
            f"以下为供当前轮参考的压缩预览（不可替代全量，禁止据此枚举/制表）：\n\n"
            f"{compact_inner}"
        )
    else:
        body = (
            "【强制】预览已省略。完整 UTF-8 文本仅存在于落盘文件，"
            "你**不得**根据记忆、推理、历史摘要或压缩片段自行补全任何列表、表格、字段或数量。\n"
            "在输出枚举/清单/统计类答复前，**必须先**调用 read_chat_data 分段读取；"
            "JSON/设备/资产类优先 mode=head 或 range（必要时多次 range 直至覆盖全部字符）。\n"
            f"示例：read_chat_data(spill_id=\"{spill_id}\", date_subdir=\"{subdir}\", "
            f"mode=\"head\", head_chars={CHAT_TOOL_SPILL_READ_DEFAULT_HEAD_CHARS}) — 文件共 {chars} 字符。"
        )
    return (
        f"{line}\n"
        f"【说明】完整输出已落盘：用户文件根下 `{rel}`。\n"
        f"需要全量或片段时调用 **read_chat_data**（spill_id=ref、date_subdir=subdir、"
        f"mode=head_tail|head|tail|range）。\n"
        f"{body}"
    )


# 这些工具本身用于读取已落盘/附件内容，其返回不应再次 spill（否则递归嵌套、文件越读越大）
_NO_RESPILL_TOOL_NAMES = frozenset({"read_chat_attachment", "read_chat_data"})


async def spill_and_wrap_tool_message(
    user: dict,
    session_id: int | None,
    tool_name: str,
    tool_call_id: str | None,
    full_result: str,
    compact_for_llm: str,
) -> str:
    """必要时落盘并包装 tool 消息正文；read_chat_attachment / read_chat_data 豁免落盘。"""
    if (tool_name or "").strip() in _NO_RESPILL_TOOL_NAMES:
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
    head_chars: int | None = None,
    tail_chars: int | None = None,
    range_start: int = 0,
    max_chars: int | None = None,
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
    head_chars = max(
        0,
        min(int(head_chars if head_chars is not None else CHAT_TOOL_SPILL_READ_DEFAULT_HEAD_CHARS), CHAT_TOOL_SPILL_READ_MAX_CHARS),
    )
    tail_chars = max(
        0,
        min(int(tail_chars if tail_chars is not None else CHAT_TOOL_SPILL_READ_DEFAULT_TAIL_CHARS), CHAT_TOOL_SPILL_READ_MAX_CHARS),
    )
    range_start = max(0, int(range_start or 0))
    max_chars = max(
        256,
        min(int(max_chars if max_chars is not None else CHAT_TOOL_SPILL_READ_DEFAULT_RANGE_CHARS), CHAT_TOOL_SPILL_READ_MAX_CHARS),
    )

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


def parse_spill_sentinel_fields(content: str) -> dict[str, str] | None:
    m = _SPILL_SENTINEL_RE.search(content or "")
    if not m:
        return None
    return {
        "ref": m.group("ref"),
        "subdir": m.group("subdir"),
        "chars": m.group("chars"),
        "tool": m.group("tool"),
        "session": m.group("session"),
    }


def _parse_tool_call_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _extract_read_spill_refs_from_assistant_message(message: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for tc in message.get("tool_calls") or []:
        fn = (tc.get("function") or {})
        name = (fn.get("name") or "").strip()
        if name not in _SPILL_READ_TOOL_NAMES:
            continue
        args = _parse_tool_call_args(fn.get("arguments"))
        if name == "read_chat_data":
            sid = (args.get("spill_id") or "").strip()
            if sid:
                refs.add(sid)
            continue
        path = (args.get("path") or "").replace("\\", "/")
        m = re.search(r"/spill/([0-9a-fA-F-]{36})\.data", path)
        if m:
            refs.add(m.group(1))
    return refs


def list_unresolved_spill_refs(messages: list[dict[str, Any]], *, since_last_user: bool = True) -> list[dict[str, str]]:
    """返回当前轮次中已落盘但尚未通过 read_chat_data/fs_read_file 读取过的 spill ref。"""
    if not messages:
        return []
    scan = messages
    if since_last_user:
        last_user_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if (messages[i] or {}).get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx >= 0:
            scan = messages[last_user_idx:]
    pending: dict[str, dict[str, str]] = {}
    read_refs: set[str] = set()
    for m in scan:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "tool":
            fields = parse_spill_sentinel_fields(m.get("content") or "")
            if fields:
                pending[fields["ref"]] = fields
        elif role == "assistant":
            read_refs |= _extract_read_spill_refs_from_assistant_message(m)
    return [pending[r] for r in pending if r not in read_refs]


def build_force_read_spill_user_message(unresolved: list[dict[str, str]]) -> str:
    lines = [
        "【系统强制约束】上一条或多条工具结果已溢出落盘（[[EDGEOPS_CHAT_DATA ...]]），"
        "你尚未调用 read_chat_data 读取完整内容就试图直接作答。",
        "禁止根据预览、推理、对话记忆或历史轮次摘要自行补全列表、表格、字段、设备名或数量。",
        "凡涉及枚举、清单、统计、对比，必须先 read_chat_data 分段读取落盘文件（JSON/设备/资产类优先 mode=head 或 range），"
        "必要时多次 range 直至覆盖 total_chars，再整理答复。",
    ]
    for item in unresolved[:6]:
        lines.append(
            f'- spill_id="{item.get("ref", "")}" date_subdir="{item.get("subdir", "")}" '
            f'total_chars={item.get("chars", "?")} tool={item.get("tool", "?")}'
        )
    lines.append("请立即调用 read_chat_data，勿向用户输出未从落盘文件验证的完整表格或清单。")
    return "\n".join(lines)


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
