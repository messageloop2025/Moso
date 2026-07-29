"""
Agent 执行循环集成——EventBus / Hook Engine / Middleware / StateMachine 接入点

本模块提供一组轻量函数，在 Agent 执行循环的关键位置调用：
- 启动/停止 / 每步开始/结束：EventBus + StateMachine
- 工具执行前/后：Hook Engine 评估 + EventBus + Middleware 包装
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ── 工具执行前 Hook ──

async def agent_tool_pre(
    *,
    tool_name: str,
    args: dict[str, Any] | None = None,
    session_id: int | None = None,
    user: dict[str, Any] | None = None,
    chat_mode: str = "normal",
    assistant_note: str = "",
    hook_skills: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """在工具执行前调用：EventBus 事件 + Hook Engine 规则评估。

    返回: {'decision': 'allow'|'deny'|'ask', 'reason': str, 'tool_result': dict|None}
    fail-open: 异常时返回 allow。

    hook_skills: 当前会话激活的 Skill hooks 列表（含 hooks.json 信息）。
    """
    result: dict[str, Any] = {"decision": "allow", "reason": "", "tool_result": None}
    args = args or {}
    uid = int(user["id"]) if user else 0

    # 1) EventBus agent:tool:pre
    try:
        from services.event_bus import event_bus, TraceContext
        from services.event_types import AgentEvent

        trace = TraceContext(user_id=uid, session_id=session_id, step_num=0)
        event_bus.emit(
            AgentEvent.TOOL_PRE,
            trace_ctx=trace,
            tool_name=tool_name,
            args=args,
            chat_mode=chat_mode,
        )
    except Exception:
        pass

    # 2) Hook Engine 规则评估（DB event_rules + hooks.json）
    try:
        from services.event_hook_engine import resolve_hook_decision

        hook_result = await resolve_hook_decision(
            event=AgentEvent.TOOL_PRE,
            tool_name=tool_name,
            args=args,
            hook_skills=hook_skills,   # 关键：传入 Skill hooks 信息
            user_id=uid,
            chat_mode=chat_mode,
            session_id=session_id,
        )
        decision = hook_result.get("decision", "allow") if hook_result else "allow"
        if decision == "deny":
            result["decision"] = "deny"
            result["reason"] = hook_result.get("reason", "Hook engine 拒绝")
            result["tool_result"] = {
                "success": False,
                "error": result["reason"],
                "decision": "deny",
                "source": hook_result.get("source", "event_hook_engine"),
            }
        elif decision == "ask":
            result["decision"] = "ask"
            result["reason"] = hook_result.get("reason", "Hook engine 请求确认")
            result["tool_result"] = {
                "success": False,
                "pending_confirmation": True,
                "message": result["reason"],
                "tool": tool_name,
                "source": hook_result.get("source", "event_hook_engine"),
            }
    except Exception as e:
        logger.debug("agent_tool_pre hook engine: %s", e)

    return result


# ── 工具执行后 Hook ──

def agent_tool_post(
    *,
    tool_name: str,
    success: bool,
    args: dict[str, Any] | None = None,
    result: str | None = None,
    error: str | None = None,
    session_id: int | None = None,
    user: dict[str, Any] | None = None,
    chat_mode: str = "normal",
) -> None:
    """在工具执行后调用：EventBus 事件。fail-open。"""
    args = args or {}
    uid = int(user["id"]) if user else 0
    try:
        from services.event_bus import event_bus, TraceContext
        from services.event_types import AgentEvent

        trace = TraceContext(user_id=uid, session_id=session_id, step_num=0)
        event_name = AgentEvent.TOOL_POST if success else AgentEvent.TOOL_ERROR
        event_bus.emit(
            event_name,
            trace_ctx=trace,
            tool_name=tool_name,
            args=args,
            result=result,
            error=error,
            chat_mode=chat_mode,
        )
    except Exception:
        pass


async def agent_tool_post_hook(
    *,
    tool_name: str,
    success: bool,
    args: dict[str, Any] | None = None,
    result_obj: dict[str, Any] | None = None,
    session_id: int | None = None,
    user: dict[str, Any] | None = None,
    chat_mode: str = "normal",
    hook_skills: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """工具执行后 Hook 引擎评估：EventBus + DB event_rules（postToolUse / postToolUseFailure）。

    与 agent_tool_post 的区别：本函数异步评估 Hook 规则并可改造结果。
    返回 (result_obj, is_success)。"""
    if result_obj is None:
        return None, success
    obj = dict(result_obj)
    is_ok = success
    uid = int(user["id"]) if user else 0

    # 1) EventBus
    try:
        from services.event_bus import event_bus, TraceContext
        from services.event_types import AgentEvent

        trace = TraceContext(user_id=uid, session_id=session_id, step_num=0)
        event_name = AgentEvent.TOOL_POST if success else AgentEvent.TOOL_ERROR
        event_bus.emit(
            event_name,
            trace_ctx=trace,
            tool_name=tool_name,
            args=args or {},
            chat_mode=chat_mode,
        )
    except Exception:
        pass

    # 2) DB event_rules 评估（新引擎 postToolUse / postToolUseFailure）
    try:
        from services.event_hook_engine import resolve_hook_decision
        from services.event_hook_engine import apply_post_tool_hook_decision as _apply_post

        _post_ev = "postToolUse" if success else "postToolUseFailure"
        _post_hook_dec = await resolve_hook_decision(
            event=_post_ev,
            tool_name=tool_name,
            args=args,
            hook_skills=hook_skills,
            user_id=uid,
            chat_mode=chat_mode,
            session_id=session_id,
            result_obj=obj,
        )
        if _post_hook_dec and _post_hook_dec.get("decision") in ("deny", "ask"):
            obj, is_ok = _apply_post(obj, _post_hook_dec)
    except Exception as e:
        logger.debug("agent_tool_post_hook engine: %s", e)

    return obj, is_ok


# ── Agent 状态转换 ──

def agent_step_start(
    *,
    session_id: int | None = None,
    user: dict[str, Any] | None = None,
    round_idx: int = 0,
    chat_mode: str = "normal",
) -> None:
    """每步开始：EventBus + StateMachine 状态转换。fail-open。"""
    uid = int(user["id"]) if user else 0
    try:
        from services.event_bus import event_bus, TraceContext
        from services.event_types import AgentEvent

        trace = TraceContext(user_id=uid, session_id=session_id, step_num=round_idx + 1)
        event_bus.emit(
            AgentEvent.STEP_START,
            trace_ctx=trace,
            round_idx=round_idx,
            chat_mode=chat_mode,
        )
    except Exception:
        pass

    try:
        from services.agent_state_machine import (
            AgentState,
            get_state_machine,
        )
        sm = get_state_machine(session_id, user)
        import asyncio
        asyncio.create_task(sm.transition(AgentState.THINKING, reason=f"Step {round_idx + 1}"))
    except Exception:
        pass


def agent_step_end(
    *,
    session_id: int | None = None,
    user: dict[str, Any] | None = None,
    round_idx: int = 0,
    chat_mode: str = "normal",
    had_tool_call: bool = False,
) -> None:
    """每步结束：EventBus + StateMachine。fail-open。"""
    uid = int(user["id"]) if user else 0
    try:
        from services.event_bus import event_bus, TraceContext
        from services.event_types import AgentEvent

        trace = TraceContext(user_id=uid, session_id=session_id, step_num=round_idx + 1)
        event_bus.emit(
            AgentEvent.STEP_END,
            trace_ctx=trace,
            round_idx=round_idx,
            chat_mode=chat_mode,
            had_tool_call=had_tool_call,
        )
    except Exception:
        pass

    try:
        from services.agent_state_machine import (
            AgentState,
            get_state_machine,
        )
        sm = get_state_machine(session_id, user)
        new_state = AgentState.OBSERVING if had_tool_call else AgentState.IDLE
        import asyncio
        asyncio.create_task(sm.transition(new_state, reason=f"Step {round_idx + 1} done"))
    except Exception:
        pass


def agent_start(
    *,
    session_id: int | None = None,
    user: dict[str, Any] | None = None,
    chat_mode: str = "normal",
) -> None:
    """Agent 启动：EventBus + StateMachine。fail-open。"""
    uid = int(user["id"]) if user else 0
    try:
        from services.event_bus import event_bus, TraceContext
        from services.event_types import AgentEvent

        trace = TraceContext(user_id=uid, session_id=session_id, step_num=0)
        event_bus.emit(
            AgentEvent.START,
            trace_ctx=trace,
            chat_mode=chat_mode,
        )
    except Exception:
        pass

    try:
        from services.agent_state_machine import (
            AgentState,
            get_state_machine,
        )
        sm = get_state_machine(session_id, user)
        import asyncio
        asyncio.create_task(sm.transition(AgentState.THINKING, reason="Agent started"))
    except Exception:
        pass


def agent_complete(
    *,
    session_id: int | None = None,
    user: dict[str, Any] | None = None,
    chat_mode: str = "normal",
    reason: str = "",
) -> None:
    """Agent 完成：EventBus + StateMachine。fail-open。"""
    uid = int(user["id"]) if user else 0
    try:
        from services.event_bus import event_bus, TraceContext
        from services.event_types import AgentEvent

        trace = TraceContext(user_id=uid, session_id=session_id, step_num=0)
        event_bus.emit(
            AgentEvent.COMPLETE,
            trace_ctx=trace,
            chat_mode=chat_mode,
            reason=reason,
        )
    except Exception:
        pass

    try:
        from services.agent_state_machine import (
            AgentState,
            get_state_machine,
        )
        sm = get_state_machine(session_id, user)
        import asyncio
        asyncio.create_task(sm.transition(AgentState.IDLE, reason=reason or "Agent completed"))
    except Exception:
        pass


def agent_error(
    *,
    session_id: int | None = None,
    user: dict[str, Any] | None = None,
    chat_mode: str = "normal",
    error_str: str = "",
) -> None:
    """Agent 异常：EventBus + StateMachine。fail-open。"""
    uid = int(user["id"]) if user else 0
    try:
        from services.event_bus import event_bus, TraceContext
        from services.event_types import AgentEvent

        trace = TraceContext(user_id=uid, session_id=session_id, step_num=0)
        event_bus.emit(
            AgentEvent.ERROR,
            trace_ctx=trace,
            error=error_str,
            chat_mode=chat_mode,
        )
    except Exception:
        pass

    try:
        from services.agent_state_machine import (
            AgentState,
            get_state_machine,
        )
        sm = get_state_machine(session_id, user)
        import asyncio
        asyncio.create_task(sm.transition(AgentState.IDLE, reason=f"Error: {error_str}"[:100]))
    except Exception:
        pass


# ── Middleware Chain 工具执行包装 ──

async def wrap_tool_execution(
    tool_executor,  # async callable: (fn_name, fn_args, user, ...) -> str
    *,
    tool_name: str,
    args: dict[str, Any] | None = None,
    session_id: int | None = None,
    user: dict[str, Any] | None = None,
    chat_mode: str = "normal",
    session_scope: str | None = None,
) -> str:
    """通过 Middleware Chain 包装工具执行。如果 chain 为空则直接调用 executor。

    executor 签名: async executor(fn_name, fn_args, user, ...) -> str
    """
    args = args or {}
    uid = int(user["id"]) if user and user.get("id") else 0

    try:
        from services.middleware_chain import get_middleware_chain, MiddlewareContext

        chain = get_middleware_chain(session_id, uid)
        ctx = MiddlewareContext(
            user=user or {},
            session_id=session_id,
            tool_name=tool_name,
            args=args,
            chat_mode=chat_mode,
            session_scope=session_scope,
        )

        async def _exec(_ctx: MiddlewareContext) -> str:
            return await tool_executor()

        return await chain.run(ctx, _exec)
    except ImportError:
        return await tool_executor()
    except Exception as e:
        logger.warning("wrap_tool_execution 异常（fail-open）: %s", e)
        return await tool_executor()
