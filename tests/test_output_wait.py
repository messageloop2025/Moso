"""until_contains / output_wait 单元测试。"""
import asyncio

from services.output_wait import (
    clamp_until_wait_seconds,
    find_literal_match,
    normalize_until_contains,
    poll_until_contains,
)
from services.terminal_poll import TerminalPollBatchState, apply_terminal_poll_tool_result


def test_normalize_until_contains():
    assert normalize_until_contains(None) == ""
    assert normalize_until_contains("  ") == ""
    assert normalize_until_contains(" password: ") == "password:"
    assert normalize_until_contains(123) == "123"


def test_clamp_until_wait_seconds():
    assert clamp_until_wait_seconds(None, default=30, max_sec=30) == 30
    assert clamp_until_wait_seconds(10, default=30, max_sec=30) == 10
    assert clamp_until_wait_seconds(999, default=30, max_sec=30) == 30
    assert clamp_until_wait_seconds(0, default=30, max_sec=3600) == 1
    assert clamp_until_wait_seconds("45", default=30, max_sec=3600) == 45
    assert clamp_until_wait_seconds("x", default=30, max_sec=3600) == 30


def test_find_literal_match():
    assert find_literal_match("", "a") is None
    assert find_literal_match("hello", "") is None
    snip = find_literal_match("abc password: xyz", "password:")
    assert snip is not None
    assert "password:" in snip


def test_poll_until_contains_matched_delta():
    chunks = ["start\n", "start\nmid\n", "start\nmid\nDONE_MARK\n"]

    async def fetch():
        text = chunks.pop(0) if chunks else "start\nmid\nDONE_MARK\n"
        return text, {}

    async def _go():
        return await poll_until_contains(
            fetch_raw=fetch,
            needle="DONE_MARK",
            timeout_sec=2,
            interval_sec=0.2,
            match_mode="delta",
        )

    reason, snippet, text, _ = asyncio.run(_go())
    assert reason == "matched"
    assert snippet and "DONE_MARK" in snippet
    assert "DONE_MARK" in text


def test_poll_until_contains_timeout():
    async def fetch():
        return "still running…\n", {}

    async def _go():
        return await poll_until_contains(
            fetch_raw=fetch,
            needle="NEVER",
            timeout_sec=0.6,
            interval_sec=0.2,
            match_mode="full",
        )

    reason, snippet, _, _ = asyncio.run(_go())
    assert reason == "timeout"
    assert snippet is None


def test_poll_until_contains_initial_window():
    """调用时已在尾部的 password 提示应立即命中。"""

    async def fetch():
        return "…\n[sudo] password for user: ", {}

    async def _go():
        return await poll_until_contains(
            fetch_raw=fetch,
            needle="password",
            timeout_sec=5,
            interval_sec=0.5,
            match_mode="delta",
        )

    reason, snippet, _, _ = asyncio.run(_go())
    assert reason == "matched"
    assert snippet and "password" in snippet


def test_batch_poll_skips_when_until_wait_done():
    state = TerminalPollBatchState()
    poll, obj = apply_terminal_poll_tool_result(
        state,
        "get_terminal_buffer",
        {"until_contains": "MARK", "next_poll_in_seconds": 60},
        {
            "success": True,
            "buffer": "MARK",
            "until_wait_done": True,
            "wait_done_in_tool": True,
            "next_poll_in_seconds": 60,
        },
        success=True,
    )
    assert poll == 0
    assert "next_poll_in_seconds" not in obj

    poll2, obj2 = apply_terminal_poll_tool_result(
        state,
        "ssh_channel_read_lines",
        {"wait_seconds": 20, "until_contains": "password:"},
        {
            "success": True,
            "until_wait_done": True,
            "wait_seconds": 20,
            "next_poll_in_seconds": 20,
        },
        success=True,
    )
    assert poll2 == 0
    assert "wait_seconds" not in obj2
    assert "next_poll_in_seconds" not in obj2
