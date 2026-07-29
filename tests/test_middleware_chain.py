"""MiddlewareChain 管线注册、执行、短路测试。"""
from __future__ import annotations

import asyncio

import pytest

from services.middleware_chain import MiddlewareChain, MiddlewareContext


def _ctx(**kw):
    return MiddlewareContext(
        user={"id": 1},
        session_id=100,
        tool_name=kw.pop("tool_name", "ssh_execute"),
        chat_mode=kw.pop("chat_mode", "normal"),
        **kw,
    )


class TestMiddlewareChain:
    @pytest.mark.asyncio
    async def test_empty_chain_runs_executor(self):
        chain = MiddlewareChain(session_id=100)
        ctx = _ctx()
        result = None

        async def executor(c):
            nonlocal result
            result = c.tool_name

        await chain.run(ctx, executor)
        assert result == "ssh_execute"

    @pytest.mark.asyncio
    async def test_chain_preserves_order(self):
        chain = MiddlewareChain(session_id=100)
        order = []

        async def mw1(ctx, nxt):
            order.append(1)
            return await nxt()

        async def mw2(ctx, nxt):
            order.append(2)
            return await nxt()

        chain.use(mw1)
        chain.use(mw2)
        await chain.run(_ctx(), lambda c: "done")
        assert order == [1, 2]

    @pytest.mark.asyncio
    async def test_buffer_preserves_order(self):
        chain = MiddlewareChain(session_id=100)
        order = []

        async def mw1(ctx, nxt):
            order.append(10)
            return await nxt()

        async def mw2(ctx, nxt):
            order.append(20)
            return await nxt()

        chain.use(mw1)
        chain.use(mw2)
        await chain.run(_ctx(), lambda c: "ok")
        assert order == [10, 20]

    @pytest.mark.asyncio
    async def test_middleware_can_short_circuit(self):
        chain = MiddlewareChain(session_id=100)
        called = []

        async def block_mw(ctx, nxt):
            return "blocked"

        chain.use(block_mw)
        result = await chain.run(_ctx(), lambda c: called.append(1) or "ok")
        assert result == "blocked"
        assert not called

    @pytest.mark.asyncio
    async def test_remove_middleware(self):
        chain = MiddlewareChain(session_id=100)
        order = []

        async def mw1(ctx, nxt):
            order.append(1)
            return await nxt()

        async def mw2(ctx, nxt):
            order.append(2)
            return await nxt()

        chain.use(mw1)
        chain.use(mw2)
        chain.remove(mw2)
        await chain.run(_ctx(), lambda c: "ok")
        assert order == [1]

    @pytest.mark.asyncio
    async def test_remove_non_existent_no_error(self):
        chain = MiddlewareChain(session_id=100)

        async def dummy(ctx, nxt):
            return await nxt()

        chain.remove(dummy)  # 不抛异常


class TestMiddlewareContext:
    def test_default_values(self):
        ctx = MiddlewareContext()
        assert ctx.user == {}
        assert ctx.session_id is None
        assert ctx.tool_name == ""
        assert ctx.chat_mode == "normal"

    def test_to_dict(self):
        ctx = _ctx()
        d = ctx.to_dict()
        assert d["user_id"] == 1
        assert d["session_id"] == 100
        assert d["tool_name"] == "ssh_execute"
        assert d["chat_mode"] == "normal"

    def test_extra_fields(self):
        ctx = MiddlewareContext(extra={"foo": "bar"})
        assert ctx.extra["foo"] == "bar"
