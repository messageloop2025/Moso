"""Agent 状态机：显式状态转换、超时控制、生命周期事件广播。"""
from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any

from services.event_bus import event_bus, TraceContext
from services.event_types import AgentEvent

logger = logging.getLogger("edgeops.agent_state_machine")


class AgentState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    THINKING = "thinking"
    ACTING = "acting"
    OBSERVING = "observing"
    REPLYING = "replying"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


# 状态转换表：合法转换目标
_STATE_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.IDLE: {AgentState.STARTING},
    AgentState.STARTING: {AgentState.THINKING, AgentState.DONE, AgentState.ERROR},
    AgentState.THINKING: {AgentState.ACTING, AgentState.REPLYING, AgentState.WAITING_USER, AgentState.ERROR, AgentState.CANCELLED},
    AgentState.ACTING: {AgentState.OBSERVING, AgentState.ERROR, AgentState.CANCELLED},
    AgentState.OBSERVING: {AgentState.THINKING, AgentState.DONE, AgentState.ERROR, AgentState.CANCELLED},
    AgentState.REPLYING: {AgentState.DONE, AgentState.THINKING, AgentState.WAITING_USER, AgentState.ERROR},
    AgentState.WAITING_USER: {AgentState.THINKING, AgentState.ACTING, AgentState.DONE, AgentState.PAUSED, AgentState.CANCELLED},
    AgentState.PAUSED: {AgentState.THINKING, AgentState.ACTING, AgentState.CANCELLED},
    AgentState.DONE: set(),
    AgentState.ERROR: {AgentState.THINKING, AgentState.CANCELLED},
    AgentState.CANCELLED: set(),
}


class AgentStateMachine:
    """Agent 状态机：按会话管理状态，广播事件，支持超时控制。"""

    def __init__(
        self,
        *,
        session_id: int | None = None,
        user: dict[str, Any] | None = None,
        step_timeout_sec: float = 300.0,
        total_timeout_sec: float = 3600.0,
    ):
        self.session_id = session_id
        self.user = user or {}
        self._state: AgentState = AgentState.IDLE
        self.step_num: int = 0
        self.total_steps: int = 0
        self._started_at: float = 0.0
        self._state_entered_at: float = 0.0
        self._step_timeout_sec: float = step_timeout_sec
        self._total_timeout_sec: float = total_timeout_sec
        self._token_usage: dict[str, int] = {"prompt": 0, "completion": 0}
        self._history: list[dict[str, Any]] = []
        self._cancel_event: asyncio.Event | None = None
        self._enabled: bool = True

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def elapsed_ms(self) -> float:
        if self._started_at <= 0:
            return 0.0
        return (time.time() - self._started_at) * 1000

    @property
    def token_usage(self) -> dict[str, int]:
        return dict(self._token_usage)

    @property
    def state_history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def _make_trace(self) -> TraceContext:
        return TraceContext(
            user_id=self.user.get("id"),
            session_id=self.session_id,
            step_num=self.step_num,
        )

    async def transition(self, new_state: AgentState, reason: str = "") -> None:
        """状态转换：校验合法性，记录历史，广播事件。"""
        if not self._enabled:
            self._state = new_state
            return

        allowed = _STATE_TRANSITIONS.get(self._state, set())
        if new_state not in allowed and self._state != new_state:
            logger.warning(
                "非法的状态转换 sid=%s %s→%s（允许: %s）",
                self.session_id, self._state.value, new_state.value,
                [s.value for s in allowed],
            )
            # fail-safe: 仍允许到 ERROR/CANCELLED
            if new_state not in (AgentState.ERROR, AgentState.CANCELLED):
                return

        old_state = self._state
        self._state = new_state
        self._state_entered_at = time.time()

        # 记录历史
        entry = {
            "from": old_state.value,
            "to": new_state.value,
            "step": self.step_num,
            "elapsed_ms": self.elapsed_ms,
            "reason": reason,
        }
        self._history.append(entry)

        # 异步持久化到 DB
        asyncio.ensure_future(self._persist_to_db(entry))

        # 广播事件
        event_name: str | None = self._state_event_map().get(new_state)
        if event_name:
            trace = self._make_trace()
            event_bus.emit(
                event_name,
                trace_ctx=trace,
                old_state=old_state.value,
                new_state=new_state.value,
                reason=reason,
                step_num=self.step_num,
            )

        # 超时重置
        if new_state == AgentState.THINKING and self._step_timeout_sec > 0:
            asyncio.ensure_future(self._step_timeout_watchdog(self.step_num))

    def _state_event_map(self) -> dict[AgentState, str]:
        return {
            AgentState.STARTING: AgentEvent.START,
            AgentState.THINKING: AgentEvent.STEP_START,
            AgentState.OBSERVING: AgentEvent.STEP_END,
            AgentState.DONE: AgentEvent.COMPLETE,
            AgentState.ERROR: AgentEvent.ERROR,
            AgentState.CANCELLED: AgentEvent.CANCEL,
            AgentState.PAUSED: AgentEvent.PAUSE,
            AgentState.IDLE: AgentEvent.RESUME,
        }

    async def _step_timeout_watchdog(self, step_at_start: int) -> None:
        """单步超时监控：若状态未变化，自动转 ERROR。"""
        await asyncio.sleep(self._step_timeout_sec)
        if self._state == AgentState.THINKING and self.step_num == step_at_start:
            logger.warning("Agent 单步超时 sid=%s step=%d", self.session_id, step_at_start)
            await self.transition(AgentState.ERROR, reason=f"step_timeout_{step_at_start}")

    def add_token_usage(self, prompt: int = 0, completion: int = 0) -> None:
        """累计 token 用量。"""
        self._token_usage["prompt"] += prompt
        self._token_usage["completion"] += completion
        # 异步持久化
        asyncio.ensure_future(self._persist_token_usage())

    async def _persist_to_db(self, entry: dict[str, Any]) -> None:
        """将状态历史持久化到 ai_chat_sessions 表。"""
        if not self.session_id:
            return
        try:
            from database import get_db
            db = await get_db()
            import json
            history_json = json.dumps(self._history, ensure_ascii=False)
            await db.execute(
                "UPDATE ai_chat_sessions SET state_history_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (history_json, self.session_id),
            )
            await db.commit()
        except Exception as e:
            logger.debug("persist state_history 失败 sid=%s: %s", self.session_id, e)

    async def _persist_token_usage(self) -> None:
        """将 token 用量持久化到 ai_chat_sessions 表。"""
        if not self.session_id:
            return
        try:
            from database import get_db
            db = await get_db()
            import json
            usage_json = json.dumps(self._token_usage, ensure_ascii=False)
            await db.execute(
                "UPDATE ai_chat_sessions SET token_usage_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (usage_json, self.session_id),
            )
            await db.commit()
        except Exception as e:
            logger.debug("persist token_usage 失败 sid=%s: %s", self.session_id, e)

    def is_terminal(self) -> bool:
        return self._state in (AgentState.DONE, AgentState.CANCELLED, AgentState.ERROR)

    def disable(self) -> None:
        """禁用状态机（降级为直通模式）。"""
        self._enabled = False

    def enable(self) -> None:
        self._enabled = True


# 会话级状态机缓存
_session_state_machines: dict[int, AgentStateMachine] = {}


def get_state_machine(
    session_id: int,
    user: dict[str, Any] | None = None,
) -> AgentStateMachine:
    """获取或创建会话级 Agent 状态机。"""
    if session_id not in _session_state_machines:
        _session_state_machines[session_id] = AgentStateMachine(
            session_id=session_id,
            user=user,
        )
    return _session_state_machines[session_id]


def remove_state_machine(session_id: int) -> None:
    """会话结束时清理状态机。"""
    _session_state_machines.pop(session_id, None)
