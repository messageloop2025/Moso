"""进程内 EventBus：发布/订阅，异常隔离，TraceContext 携带用户/会话维度。"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections import defaultdict
from typing import Any, Callable

import config

logger = logging.getLogger("edgeops.event_bus")

EventCallback = Callable[..., Any]


class TraceContext:
    """每次 emit 携带的追踪上下文，供回调按用户/会话过滤。"""

    __slots__ = ("trace_id", "user_id", "session_id", "step_num", "timestamp", "extra")

    def __init__(
        self,
        *,
        user_id: int | None = None,
        session_id: int | None = None,
        step_num: int = 0,
        extra: dict[str, Any] | None = None,
    ):
        self.trace_id: str = uuid.uuid4().hex[:12]
        self.user_id: int | None = user_id
        self.session_id: int | None = session_id
        self.step_num: int = step_num
        self.timestamp: float = time.time()
        self.extra: dict[str, Any] = extra or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "step_num": self.step_num,
            "timestamp": self.timestamp,
        }


class EventBus:
    """进程内单例事件总线。

    - 支持同步与异步回调（对异步回调用 inspect.iscoroutinefunction 区分）。
    - 单个回调异常不影响其余回调（fail-open）。
    - 多用户/会话隔离由回调自行根据 trace_ctx 过滤。
    - 每个事件最大监听数受 EDGEOPS_EVENTBUS_MAX_LISTENERS 控制。
    """

    def __init__(self):
        self._listeners: dict[str, list[EventCallback]] = defaultdict(list)
        self._max_listeners: int = max(1, int(getattr(config, "EVENTBUS_MAX_LISTENERS", 50)))

    def on(self, event: str, callback: EventCallback) -> None:
        """注册事件监听器。"""
        ev = str(event).strip()
        if not ev:
            raise ValueError("event 不能为空")
        if len(self._listeners[ev]) >= self._max_listeners:
            raise RuntimeError(f"事件 {ev} 监听数已达上限 {self._max_listeners}")
        self._listeners[ev].append(callback)

    def off(self, event: str, callback: EventCallback) -> None:
        """移除事件监听器。"""
        ev = str(event).strip()
        try:
            self._listeners[ev].remove(callback)
        except (ValueError, KeyError):
            pass

    def clear(self, event: str | None = None) -> None:
        """清除指定事件的所有监听器；event=None 则清除全部。"""
        if event is None:
            self._listeners.clear()
        else:
            self._listeners.pop(str(event).strip(), None)

    def emit(
        self,
        event: str,
        /,
        **payload: Any,
    ) -> None:
        """同步发送事件（fire-and-forget；不支持返回值收集）。

        调用链：
        1. 查找 `event` 的监听器列表
        2. 对同步回调直接调用；对异步回调创建 asyncio.ensure_future
        3. 单个回调异常不中断后续
        """
        ev = str(event).strip()
        callbacks = list(self._listeners.get(ev, ()))
        # 同时尝试模糊匹配（如 "agent:tool:*" 匹配 "agent:tool:pre"）
        for pattern_key, cb_list in self._listeners.items():
            if pattern_key == ev:
                continue
            if _wildcard_match(pattern_key, ev):
                callbacks.extend(cb_list)

        if not callbacks:
            return

        for cb in callbacks:
            try:
                if inspect.iscoroutinefunction(cb):
                    asyncio.ensure_future(cb(event=ev, **payload))
                else:
                    cb(event=ev, **payload)
            except Exception:
                logger.debug("EventBus 回调异常（fail-open，event=%s）", ev, exc_info=True)

    async def emit_async(
        self,
        event: str,
        /,
        **payload: Any,
    ) -> dict[str, Any]:
        """异步发送事件并收集回调结果（用于需要等待决策的场景如 preToolUse）。

        返回 {"decision": "allow"|"deny"|"ask", "reason": str, "results": [...]}
        deny 优先，其次 ask，否则 allow。
        """
        ev = str(event).strip()
        callbacks: list[EventCallback] = list(self._listeners.get(ev, ()))
        for pattern_key, cb_list in self._listeners.items():
            if pattern_key == ev:
                continue
            if _wildcard_match(pattern_key, ev):
                callbacks.extend(cb_list)

        if not callbacks:
            return {"decision": "allow", "reason": "no_listeners", "results": []}

        results: list[dict[str, Any]] = []
        ask_hit: dict[str, Any] | None = None
        for cb in callbacks:
            try:
                if inspect.iscoroutinefunction(cb):
                    r = await cb(event=ev, **payload)
                else:
                    r = cb(event=ev, **payload)
                if isinstance(r, dict):
                    results.append(r)
                    dec = str(r.get("decision") or "allow").strip().lower()
                    if dec == "deny":
                        return {"decision": "deny", "reason": r.get("reason", ""), "results": results}
                    if dec == "ask" and ask_hit is None:
                        ask_hit = r
            except Exception:
                logger.debug("EventBus async 回调异常（fail-open，event=%s）", ev, exc_info=True)
                continue

        if ask_hit:
            return {"decision": "ask", "reason": ask_hit.get("reason", ""), "results": results}
        return {"decision": "allow", "reason": "all_allow", "results": results}


def _wildcard_match(pattern: str, event_name: str) -> bool:
    """fnmatch 风格通配：* 匹配任意，** 匹配含 : 的层级。"""
    import fnmatch
    return fnmatch.fnmatch(event_name, pattern)


# 全局单例
event_bus = EventBus()
