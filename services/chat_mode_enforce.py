"""聊天模式硬门禁：在 execute_tool 入口强制拦截（不依赖提示词、不依赖 agent 循环是否接线）。"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any

from services.chat_mode_gate import (
    is_qa_blocked,
    is_strict_allow_cached,
    needs_strict_confirm,
    normalize_chat_mode,
    qa_blocked_tool_result,
)

logger = logging.getLogger("edgeops.chat_mode_enforce")

# agent 循环在用户「允许/总是」后、调用 execute_tool 前登记；供入口硬门禁放行本次调用
_strict_granted_tools: ContextVar[set[str] | None] = ContextVar(
    "edgeops_strict_granted_tools", default=None
)


def grant_strict_tool_approval(tool_name: str) -> None:
    """标记当前异步上下文中该工具已获严格确认（一次性放行，直至 clear）。"""
    name = (tool_name or "").strip()
    if not name:
        return
    cur = _strict_granted_tools.get()
    if cur is None:
        cur = set()
        _strict_granted_tools.set(cur)
    cur.add(name)


def clear_strict_tool_approval(tool_name: str | None = None) -> None:
    """清除严格确认放行标记。"""
    cur = _strict_granted_tools.get()
    if cur is None:
        return
    if tool_name is None:
        cur.clear()
        return
    cur.discard((tool_name or "").strip())


def is_strict_tool_approved(tool_name: str) -> bool:
    name = (tool_name or "").strip()
    if not name:
        return False
    cur = _strict_granted_tools.get()
    return bool(cur and name in cur)


async def resolve_session_chat_mode(
    session_id: int | None,
    explicit: str | None = None,
) -> str:
    """优先用调用方传入的 mode；否则按 session_id 读库；无会话则 normal（不拦截）。"""
    if explicit is not None and str(explicit).strip() != "":
        return normalize_chat_mode(explicit)
    if session_id is None:
        return "normal"
    try:
        from database import get_db

        db = await get_db()
        rows = await db.execute_fetchall(
            "SELECT COALESCE(chat_mode, 'normal') AS chat_mode FROM ai_chat_sessions WHERE id = ?",
            (int(session_id),),
        )
        if rows:
            return normalize_chat_mode(rows[0]["chat_mode"])
    except Exception as e:
        logger.warning("resolve_session_chat_mode failed session_id=%s: %s", session_id, e)
    return "normal"


async def load_session_strict_allow_cache(session_id: int | None) -> str:
    if session_id is None:
        return ""
    try:
        from database import get_db

        db = await get_db()
        rows = await db.execute_fetchall(
            """SELECT COALESCE(strict_allow_cache_json, '') AS strict_allow_cache_json
               FROM ai_chat_sessions WHERE id = ?""",
            (int(session_id),),
        )
        if rows:
            return str(rows[0]["strict_allow_cache_json"] or "")
    except Exception as e:
        logger.warning("load_session_strict_allow_cache failed session_id=%s: %s", session_id, e)
    return ""


async def enforce_qa_tool_block(
    tool_name: str,
    arguments: dict | None,
    *,
    session_id: int | None = None,
    chat_mode: str | None = None,
) -> str | None:
    """
    若当前为问答模式且工具在禁止列表，返回 JSON 错误字符串；否则返回 None（放行）。
    供 execute_tool 入口与嵌套调用共用。
    """
    mode = await resolve_session_chat_mode(session_id, chat_mode)
    name = (tool_name or "").strip()
    if mode != "qa" or not is_qa_blocked(name):
        return None
    blocked = qa_blocked_tool_result(
        name,
        arguments if isinstance(arguments, dict) else {},
    )
    blocked["enforced_at"] = "execute_tool"
    logger.info(
        "QA hard-block tool=%s session_id=%s",
        name,
        session_id,
    )
    return json.dumps(blocked, ensure_ascii=False)


async def enforce_strict_tool_block(
    tool_name: str,
    arguments: dict | None,
    *,
    session_id: int | None = None,
    chat_mode: str | None = None,
    strict_allow_cache_json: str | None = None,
) -> str | None:
    """
    严格模式：需确认的工具在 execute_tool 入口硬拦，除非：
    - 本上下文已 grant_strict_tool_approval（agent 循环用户点了允许/总是），或
    - 会话「总是」缓存命中（同工具名）。
    返回 JSON 错误字符串；放行则 None。
    """
    mode = await resolve_session_chat_mode(session_id, chat_mode)
    name = (tool_name or "").strip()
    if mode != "strict" or not needs_strict_confirm(name):
        return None
    if is_strict_tool_approved(name):
        return None
    cache_raw = strict_allow_cache_json
    if cache_raw is None:
        cache_raw = await load_session_strict_allow_cache(session_id)
    if is_strict_allow_cached(
        cache_raw,
        name,
        arguments if isinstance(arguments, dict) else {},
    ):
        return None
    args = arguments if isinstance(arguments, dict) else {}
    blocked: dict[str, Any] = {
        "success": False,
        "error": "本次工具调用未获用户批准，已取消执行。请根据需要重新发起调用或改问用户。",
        "mode": "strict",
        "decision": "strict_enforce_block",
        "user_decision": "deny",
        "user_decision_note": "用户未批准本次工具调用，操作未执行。",
        "tool": name,
        "enforced_at": "execute_tool",
        "args_preview": {
            k: args.get(k)
            for k in ("host_id", "channel_id", "slot", "command", "text")
            if k in args
        },
    }
    logger.info(
        "Strict hard-block tool=%s session_id=%s",
        name,
        session_id,
    )
    return json.dumps(blocked, ensure_ascii=False)
