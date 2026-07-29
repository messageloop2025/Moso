"""Agent 运行时控制：整合现有 runtime_control 并新增会话级速率限制、步超时、状态查询。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from database import get_db
from services.agent_state_machine import AgentState, get_state_machine

logger = logging.getLogger("edgeops.agent_runtime_control")

# 会话级工具调用计数器（供 rate_limit 中间件使用）
_session_counter: dict[str, int] = {}


class RuntimeControlSignal:
    """运行时控制信号枚举"""
    SUPPLEMENT = "supplement"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    WAKE = "wake"


async def check_runtime_signal(session_id: int) -> dict[str, Any] | None:
    """检查运行时控制信号（supplement / pause / resume / stop）。

    返回 None 表示无信号；
    返回 {"action": "pause"|"stop", "message": "..."} 表示需要中断。

    整合现有 mcp_agent_task_controls 表的信号检查。
    """
    try:
        db = await get_db()
        rows = await db.execute_fetchall(
            "SELECT id, action, message FROM mcp_agent_task_controls "
            "WHERE task_id IN (SELECT id FROM mcp_agent_tasks WHERE session_id = ?) "
            "AND consumed = 0 ORDER BY id ASC LIMIT 1",
            (session_id,),
        )
        if rows:
            row = rows[0]
            await db.execute("UPDATE mcp_agent_task_controls SET consumed = 1 WHERE id = ?", (row["id"],))
            await db.commit()
            return {
                "action": str(row["action"] or "").strip().lower(),
                "message": str(row["message"] or ""),
            }
    except Exception:
        pass
    return None


async def apply_runtime_signal(
    session_id: int,
    signal: dict[str, Any],
) -> bool:
    """应用运行时信号到状态机。返回 True 表示需中断当前 Agent 循环。"""
    action = str(signal.get("action") or "").strip().lower()
    sm = get_state_machine(session_id)

    if action == RuntimeControlSignal.STOP:
        await sm.transition(AgentState.CANCELLED, reason="user_stop")
        return True

    elif action == RuntimeControlSignal.PAUSE:
        await sm.transition(AgentState.PAUSED, reason="user_pause")
        return True

    elif action == RuntimeControlSignal.RESUME or action == RuntimeControlSignal.WAKE:
        if sm.state == AgentState.PAUSED:
            await sm.transition(AgentState.THINKING, reason="user_resume")
        return False

    elif action == RuntimeControlSignal.SUPPLEMENT:
        # supplement 不改变状态，只是消息补充
        return False

    return False


async def get_agent_runtime_info(session_id: int) -> dict[str, Any]:
    """获取当前会话的 Agent 运行时信息（供 AI 工具查询）。"""
    sm = get_state_machine(session_id)
    return {
        "state": sm.state.value,
        "step_num": sm.step_num,
        "total_steps": sm.total_steps,
        "elapsed_ms": sm.elapsed_ms,
        "token_usage": sm.token_usage,
        "state_history": sm.state_history[-10:],  # 最近 10 条状态变更
    }
