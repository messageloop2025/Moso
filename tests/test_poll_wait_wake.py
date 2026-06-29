"""终端轮询等待：wake 提前结束。"""
import asyncio
import json

import pytest

from api import ai_agent
from api.ai_agent import _poll_wait_blocking, _poll_wait_sse


def _parse_sse(line: str) -> dict:
    assert line.startswith("data: ")
    return json.loads(line[6:])


@pytest.mark.asyncio
async def test_poll_wait_sse_wake_skips_remaining():
    out_status = ["continue"]
    wake_sent = False

    async def consume():
        nonlocal wake_sent
        if not wake_sent:
            wake_sent = True
            return {"action": "wake", "message": ""}
        return None

    events = []
    async for line in _poll_wait_sse(
        60,
        http_request=None,
        consume_runtime_control=consume,
        out_status=out_status,
        wait_tool="get_terminal_buffer",
    ):
        events.append(_parse_sse(line))

    assert out_status[0] == "continue"
    assert any(e.get("action") == "waiting_woken" for e in events)
    assert any(
        e.get("runtime_control", {}).get("action") == "wake"
        for e in events
    )
    waiting = [e for e in events if e.get("action") == "waiting"]
    assert waiting
    assert waiting[0].get("wait_tool") == "get_terminal_buffer"
    woken = [e for e in events if e.get("action") == "waiting_woken"]
    assert woken and woken[0].get("wait_elapsed", 99) == 0


@pytest.mark.asyncio
async def test_poll_wait_blocking_wake_returns_continue():
    session_id = 900001
    await ai_agent._clear_runtime_control_queue(session_id)

    async def wait_then_wake():
        await asyncio.sleep(0.05)
        await ai_agent._push_runtime_control(session_id, "wake", "")

    wake_task = asyncio.create_task(wait_then_wake())
    try:
        status = await asyncio.wait_for(
            _poll_wait_blocking(30, session_id=session_id),
            timeout=5,
        )
    finally:
        await wake_task
        await ai_agent._clear_runtime_control_queue(session_id)

    assert status == "continue"
