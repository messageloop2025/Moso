"""会话级瞬时运行态：读写 ai_chat_sessions.session_runtime_json。

设计原则：
- JSON 可扩展（items 列表 + kind 字段），不锁死 schema；
- 仅保存「当前仍有参考价值」的状态（running 或刚结束不久）；
- 历史会话列缺失或为空时视为无状态（完全兼容旧库）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import config as _config

logger = logging.getLogger("edgeops.session_runtime")

# 已完成条目保留时长（秒），超时从 AI 参考上下文中剔除（库中可仍保留直至 prune）
_FINISHED_TTL_SEC = int(getattr(_config, "SESSION_RUNTIME_FINISHED_TTL_SEC", 3600))
_MAX_ITEMS = int(getattr(_config, "SESSION_RUNTIME_MAX_ITEMS", 20))
_MAX_FINISHED_KEEP = int(getattr(_config, "SESSION_RUNTIME_MAX_FINISHED_KEEP", 5))

_KIND_SSH_BG = "ssh_bg"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def empty_runtime_document() -> dict[str, Any]:
    return {"v": 1, "items": []}


def parse_runtime_json(raw: str | None) -> dict[str, Any]:
    """解析列值；非法或旧空值返回空文档。"""
    if not raw or not str(raw).strip():
        return empty_runtime_document()
    try:
        data = json.loads(raw)
    except Exception:
        return empty_runtime_document()
    if not isinstance(data, dict):
        return empty_runtime_document()
    if "items" not in data or not isinstance(data.get("items"), list):
        # 允许未来顶层扩展，但 items 为列表容器
        data.setdefault("v", 1)
        data.setdefault("items", [])
    return data


def serialize_runtime_document(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


async def load_session_runtime(db, session_id: int) -> dict[str, Any]:
    if not session_id:
        return empty_runtime_document()
    try:
        rows = await db.execute_fetchall(
            "SELECT COALESCE(session_runtime_json, '') AS session_runtime_json "
            "FROM ai_chat_sessions WHERE id = ?",
            (session_id,),
        )
        if not rows:
            return empty_runtime_document()
        return parse_runtime_json(rows[0]["session_runtime_json"])
    except Exception as exc:
        # 列尚未迁移时 SQLite 会报错 — 兼容旧库
        if "session_runtime_json" in str(exc).lower():
            return empty_runtime_document()
        raise


async def save_session_runtime(db, session_id: int, data: dict[str, Any]) -> None:
    if not session_id:
        return
    body = serialize_runtime_document(data)
    try:
        await db.execute(
            "UPDATE ai_chat_sessions SET session_runtime_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (body, session_id),
        )
        await db.commit()
    except Exception as exc:
        if "session_runtime_json" in str(exc).lower():
            logger.debug("session_runtime_json 列不存在，跳过保存 session_id=%s", session_id)
            return
        raise


def prune_runtime_document(data: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """剔除过期 finished 项并限制条数。"""
    now = now or datetime.now(timezone.utc)
    items = [x for x in (data.get("items") or []) if isinstance(x, dict)]
    kept: list[dict] = []
    finished: list[dict] = []
    for it in items:
        st = (it.get("status") or "").strip().lower()
        if st == "running":
            kept.append(it)
            continue
        if st in ("finished", "failed", "unknown"):
            finished_at = _parse_iso(it.get("finished_at") or it.get("updated_at"))
            if finished_at and (now - finished_at).total_seconds() > _FINISHED_TTL_SEC:
                continue
            finished.append(it)
            continue
        # 未识别 status：保留但靠后
        finished.append(it)
    finished.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    finished = finished[:_MAX_FINISHED_KEEP]
    merged = kept + finished
    merged = merged[-_MAX_ITEMS:]
    out = empty_runtime_document()
    out["items"] = merged
    return out


def _find_ssh_item(
    data: dict[str, Any],
    host_id: int | None,
    log_path: str | None,
) -> dict | None:
    lp = (log_path or "").strip()
    hid = int(host_id) if host_id is not None else None
    for it in reversed(data.get("items") or []):
        if (it.get("kind") or "") != _KIND_SSH_BG:
            continue
        if lp and (it.get("log_path") or "").strip() == lp:
            return it
        if not lp and hid is not None and int(it.get("host_id") or 0) == hid and (it.get("status") or "") == "running":
            return it
    return None


def upsert_ssh_background_job(
    data: dict[str, Any],
    *,
    host_id: int,
    log_path: str,
    pid: int | None = None,
    command_preview: str = "",
    status: str = "running",
) -> dict[str, Any]:
    """detach 成功后写入/更新 ssh 后台任务。"""
    data = prune_runtime_document(data)
    now = _utc_now_iso()
    lp = (log_path or "").strip()
    item = _find_ssh_item(data, host_id, lp) or {
        "kind": _KIND_SSH_BG,
        "host_id": int(host_id),
        "log_path": lp,
    }
    item["pid"] = pid
    item["command_preview"] = (command_preview or "")[:240]
    item["status"] = status
    item["started_at"] = item.get("started_at") or now
    item["updated_at"] = now
    items = [x for x in data.get("items") or [] if not (
        isinstance(x, dict)
        and x.get("kind") == _KIND_SSH_BG
        and int(x.get("host_id") or 0) == int(host_id)
        and (x.get("log_path") or "").strip() == lp
    )]
    items.append(item)
    data["items"] = items[-_MAX_ITEMS:]
    return data


def update_ssh_background_from_poll(
    data: dict[str, Any],
    *,
    host_id: int,
    log_path: str,
    job_running: bool,
    job_finished: bool,
    exit_code: int | None = None,
    log_tail_preview: str = "",
) -> dict[str, Any]:
    data = prune_runtime_document(data)
    now = _utc_now_iso()
    lp = (log_path or "").strip()
    item = _find_ssh_item(data, host_id, lp)
    if not item:
        item = {
            "kind": _KIND_SSH_BG,
            "host_id": int(host_id),
            "log_path": lp,
            "started_at": now,
        }
        data.setdefault("items", []).append(item)
    item["updated_at"] = now
    if log_tail_preview:
        item["log_tail_preview"] = (log_tail_preview or "")[-1200:]
    if job_running:
        item["status"] = "running"
    elif job_finished:
        item["status"] = "finished"
        item["finished_at"] = now
        if exit_code is not None:
            item["last_exit_code"] = int(exit_code)
    else:
        item["status"] = item.get("status") or "unknown"
    return data


def resolve_ssh_log_path(
    data: dict[str, Any],
    *,
    host_id: int | None,
    explicit: str | None,
) -> str | None:
    """poll_log 未传 log_path 时，解析本会话该主机上最近的 running 任务。"""
    exp = (explicit or "").strip()
    if exp:
        return exp
    hid = int(host_id) if host_id is not None else None
    data = prune_runtime_document(data)
    for it in reversed(data.get("items") or []):
        if (it.get("kind") or "") != _KIND_SSH_BG:
            continue
        if hid is not None and int(it.get("host_id") or 0) != hid:
            continue
        if (it.get("status") or "") == "running":
            lp = (it.get("log_path") or "").strip()
            if lp:
                return lp
    return None


def list_active_items(data: dict[str, Any], *, focus_host_id: int | None = None) -> list[dict]:
    data = prune_runtime_document(data)
    out = []
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        if (it.get("status") or "") != "running":
            continue
        if focus_host_id is not None and int(it.get("host_id") or 0) != int(focus_host_id):
            continue
        out.append(it)
    return out


def build_runtime_context_section(
    data: dict[str, Any],
    *,
    focus_host_id: int | None = None,
    output_locale: str = "zh-CN",
) -> str:
    """生成注入 system 的短说明；无 active 项则返回空串。"""
    active = list_active_items(data, focus_host_id=focus_host_id)
    if not active:
        return ""
    lines: list[str] = []
    loc = (output_locale or "").strip().lower()
    if loc == "en":
        lines.append("## Session runtime state (ephemeral — may be stale after jobs end; prefer latest tool results)")
        lines.append(
            "Background SSH jobs below are **in progress**. Use `ssh_execute(poll_log=true, log_path=..., host_id=...)` "
            "to read log tails; `log_path` may be omitted if it matches an entry here."
        )
    else:
        lines.append("## 会话运行态（瞬时信息，任务结束后会失效；以最新工具返回为准）")
        lines.append(
            "下列 **ssh 后台任务仍在进行**。请用 `ssh_execute(poll_log=true, log_path=..., host_id=...)` 读日志末尾；"
            "若 log_path 与下表一致可省略 log_path。"
        )
    for it in active:
        hid = it.get("host_id")
        lp = it.get("log_path") or ""
        prev = (it.get("command_preview") or "")[:120]
        pid = it.get("pid")
        tail = (it.get("log_tail_preview") or "")[-200:]
        lines.append(f"- host_id={hid} log_path={lp} pid={pid} status=running preview={prev!r}")
        if tail:
            lines.append(f"  last_log_tail: …{tail}")
    return "\n".join(lines) + "\n"


async def record_ssh_detach(
    db,
    session_id: int | None,
    *,
    host_id: int,
    log_path: str,
    pid: int | None,
    command_preview: str,
) -> None:
    if not session_id:
        return
    data = await load_session_runtime(db, session_id)
    data = upsert_ssh_background_job(
        data,
        host_id=host_id,
        log_path=log_path,
        pid=pid,
        command_preview=command_preview,
        status="running",
    )
    await save_session_runtime(db, session_id, data)


async def record_ssh_poll(
    db,
    session_id: int | None,
    *,
    host_id: int,
    log_path: str,
    job_running: bool,
    job_finished: bool,
    exit_code: int | None,
    log_tail_preview: str,
) -> None:
    if not session_id:
        return
    data = await load_session_runtime(db, session_id)
    data = update_ssh_background_from_poll(
        data,
        host_id=host_id,
        log_path=log_path,
        job_running=job_running,
        job_finished=job_finished,
        exit_code=exit_code,
        log_tail_preview=log_tail_preview,
    )
    data = prune_runtime_document(data)
    await save_session_runtime(db, session_id, data)


async def resolve_log_path_for_session(
    db,
    session_id: int | None,
    *,
    host_id: int | None,
    explicit: str | None,
) -> str | None:
    if not session_id:
        return (explicit or "").strip() or None
    data = await load_session_runtime(db, session_id)
    return resolve_ssh_log_path(data, host_id=host_id, explicit=explicit)


async def get_runtime_context_for_session(
    db,
    session_id: int | None,
    *,
    focus_host_id: int | None = None,
    output_locale: str = "zh-CN",
) -> str:
    if not session_id:
        return ""
    data = await load_session_runtime(db, session_id)
    data = prune_runtime_document(data)
    return build_runtime_context_section(data, focus_host_id=focus_host_id, output_locale=output_locale)
