"""EventBus 核心功能测试：pub/sub、异常隔离、wildcard 匹配、emit_async。"""
from __future__ import annotations

import pytest

from services.event_bus import EventBus, TraceContext


def _fresh_bus():
    b = EventBus()
    b._max_listeners = 50
    return b


class TestEventBusOnOff:
    def test_register_and_emit_sync(self):
        bus = _fresh_bus()
        called = []

        def cb(event, **payload):
            called.append((event, payload))

        bus.on("agent:start", cb)
        bus.emit("agent:start", user_id=1, session_id=10)
        assert len(called) == 1
        assert called[0][0] == "agent:start"
        assert called[0][1]["user_id"] == 1

    def test_off_removes_listener(self):
        bus = _fresh_bus()
        called = []

        def cb(**kw):
            called.append(1)

        bus.on("agent:start", cb)
        bus.off("agent:start", cb)
        bus.emit("agent:start")
        assert not called

    def test_clear_single_event(self):
        bus = _fresh_bus()
        bus.on("agent:start", lambda **kw: None)
        bus.on("agent:complete", lambda **kw: None)
        bus.clear("agent:start")
        assert not bus._listeners.get("agent:start")
        assert bus._listeners.get("agent:complete")

    def test_clear_all(self):
        bus = _fresh_bus()
        bus.on("agent:start", lambda **kw: None)
        bus.on("agent:complete", lambda **kw: None)
        bus.clear()
        assert not bus._listeners

    def test_on_empty_event_raises(self):
        bus = _fresh_bus()
        with pytest.raises(ValueError):
            bus.on("", lambda **kw: None)

    def test_on_over_max_listeners_raises(self):
        bus = _fresh_bus()
        bus._max_listeners = 1
        bus.on("agent:start", lambda **kw: None)
        with pytest.raises(RuntimeError):
            bus.on("agent:start", lambda **kw: None)


class TestEventBusEmitFailOpen:
    def test_single_callback_exception_does_not_block_others(self):
        bus = _fresh_bus()
        results = []

        def fail(**kw):
            raise ValueError("boom")

        def ok(**kw):
            results.append("ok")

        bus.on("agent:start", fail)
        bus.on("agent:start", ok)
        bus.emit("agent:start")
        assert results == ["ok"]

    def test_emit_no_listeners_no_error(self):
        bus = _fresh_bus()
        bus.emit("nonexistent")


class TestEventBusWildcard:
    def test_wildcard_match(self):
        bus = _fresh_bus()
        wildcards = []

        def wc(event, **kw):
            wildcards.append(event)

        bus.on("agent:tool:*", wc)
        bus.emit("agent:tool:pre", tool_name="test")
        bus.emit("agent:tool:post", tool_name="test")
        assert wildcards == ["agent:tool:pre", "agent:tool:post"]

    def test_wildcard_and_exact_both_fire(self):
        bus = _fresh_bus()
        exact = []
        wild = []

        bus.on("agent:tool:pre", lambda event, **kw: exact.append(event))
        bus.on("agent:tool:*", lambda event, **kw: wild.append(event))
        bus.emit("agent:tool:pre")
        assert exact == ["agent:tool:pre"]
        assert wild == ["agent:tool:pre"]


class TestEventBusEmitAsync:
    @pytest.mark.asyncio
    async def test_emit_async_collects_sync_results(self):
        bus = _fresh_bus()

        def deny(event, **kw):
            return {"decision": "deny", "reason": "block"}

        bus.on("agent:tool:pre", deny)
        result = await bus.emit_async("agent:tool:pre", tool_name="ssh_execute")
        assert result["decision"] == "deny"
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_emit_async_highest_priority_decision(self):
        bus = _fresh_bus()

        def allow_cb(event, **kw):
            return {"decision": "allow"}

        def ask_cb(event, **kw):
            return {"decision": "ask", "reason": "confirm"}

        bus.on("agent:tool:pre", allow_cb)
        bus.on("agent:tool:pre", ask_cb)
        result = await bus.emit_async("agent:tool:pre", tool_name="test")
        assert result["decision"] in ("ask", "deny")

    @pytest.mark.asyncio
    async def test_emit_async_empty_listeners_defaults_allow(self):
        bus = _fresh_bus()
        result = await bus.emit_async("nonexistent")
        assert result["decision"] == "allow"


class TestTraceContext:
    def test_basic_fields(self):
        tc = TraceContext(user_id=1, session_id=10, step_num=3)
        assert tc.user_id == 1
        assert tc.session_id == 10
        assert tc.step_num == 3
        assert len(tc.trace_id) == 12
        assert tc.timestamp > 0

    def test_to_dict(self):
        tc = TraceContext(user_id=2, session_id=20)
        d = tc.to_dict()
        assert d["user_id"] == 2
        assert d["session_id"] == 20
        assert "trace_id" in d
        assert "timestamp" in d
