"""问答 / 严格模式：execute_tool 入口硬门禁。"""
from __future__ import annotations

import asyncio
import json

from services.chat_mode_enforce import (
    clear_strict_tool_approval,
    enforce_qa_tool_block,
    enforce_strict_tool_block,
    grant_strict_tool_approval,
)
from services.chat_mode_gate import dump_strict_allow_cache, is_qa_blocked
from services.chat_mode_runtime import evaluate_pre_tool_gate


def test_enforce_blocks_send_to_terminal():
    async def _go():
        out = await enforce_qa_tool_block(
            "send_to_terminal",
            {"text": "uptime", "host_id": 1},
            session_id=None,
            chat_mode="qa",
        )
        assert out is not None
        data = json.loads(out)
        assert data.get("success") is False
        assert data.get("mode") == "qa"
        assert data.get("enforced_at") == "execute_tool"
        assert "uptime" in (data.get("suggested_command") or "")

    asyncio.run(_go())


def test_enforce_allows_list_hosts_in_qa():
    async def _go():
        out = await enforce_qa_tool_block(
            "list_hosts",
            {},
            chat_mode="qa",
        )
        assert out is None

    asyncio.run(_go())


def test_enforce_noop_in_normal():
    async def _go():
        out = await enforce_qa_tool_block(
            "send_to_terminal",
            {"text": "ls"},
            chat_mode="normal",
        )
        assert out is None

    asyncio.run(_go())


def test_qa_blocked_covers_channel_and_ssh():
    assert is_qa_blocked("ssh_execute")
    assert is_qa_blocked("send_to_terminal")
    assert is_qa_blocked("ssh_channel_create")
    assert is_qa_blocked("ssh_channel_send")
    assert is_qa_blocked("connect_terminal")
    assert is_qa_blocked("create_console")
    assert is_qa_blocked("terminal_send_and_read")


def test_strict_enforce_blocks_without_approval():
    async def _go():
        clear_strict_tool_approval()
        out = await enforce_strict_tool_block(
            "send_to_terminal",
            {"text": "ls", "host_id": 1},
            session_id=None,
            chat_mode="strict",
            strict_allow_cache_json="",
        )
        assert out is not None
        data = json.loads(out)
        assert data.get("mode") == "strict"
        assert data.get("enforced_at") == "execute_tool"

    asyncio.run(_go())


def test_strict_enforce_allows_after_grant():
    async def _go():
        clear_strict_tool_approval()
        grant_strict_tool_approval("ssh_execute")
        out = await enforce_strict_tool_block(
            "ssh_execute",
            {"command": "uptime", "host_id": 1},
            chat_mode="strict",
            strict_allow_cache_json="",
        )
        assert out is None
        clear_strict_tool_approval("ssh_execute")

    asyncio.run(_go())


def test_strict_enforce_allows_always_cache():
    async def _go():
        clear_strict_tool_approval()
        cache = dump_strict_allow_cache(["send_to_terminal"])
        out = await enforce_strict_tool_block(
            "send_to_terminal",
            {"text": "anything", "host_id": 2},
            chat_mode="strict",
            strict_allow_cache_json=cache,
        )
        assert out is None

    asyncio.run(_go())


def test_evaluate_pre_tool_gate_strict_confirm():
    async def _go():
        g = await evaluate_pre_tool_gate(
            chat_mode="strict",
            tool_name="ssh_channel_send",
            args={"channel_id": "c1", "text": "cat /etc/hosts"},
            strict_allow_cache_json="",
            assistant_note="查看 hosts 文件",
        )
        assert g.get("action") == "confirm"
        ua = g.get("ui_action") or {}
        assert ua.get("action") == "strict_command_confirm"
        assert ua.get("kind") == "strict_command_confirm"
        assert ua.get("command") == "cat /etc/hosts"
        assert "cat /etc/hosts" in (ua.get("question") or "")
        assert "查看 hosts" in (ua.get("reason") or ua.get("question") or "")

    asyncio.run(_go())
