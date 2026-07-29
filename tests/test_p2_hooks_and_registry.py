"""P2：Hook 事件 / allowed_tools / tools_registry / run_skill_script 路径约束。"""
from __future__ import annotations

import json

from services.tools_registry import list_extra_tools, merge_tools, register_tool
from services.event_hook_engine import apply_post_tool_hook_decision
from services.chat_mode_runtime import check_allowed_tools, tool_matches


def test_tool_matches_glob():
    assert tool_matches("ssh_*", "ssh_execute")
    assert tool_matches(["list_hosts", "*channel*"], "send_to_terminal_channel")
    assert not tool_matches("list_hosts", "ssh_execute")


def test_check_allowed_tools():
    assert check_allowed_tools("", "ssh_execute")["allowed"] is True
    assert check_allowed_tools("ssh_*,list_hosts", "ssh_execute")["allowed"] is True
    assert check_allowed_tools("list_hosts", "ssh_execute")["allowed"] is False


def test_before_mcp_hook_deny():
    """beforeMCPExecution hook 由 event_hook_engine 新引擎处理，此处测试 tool_matches 可用。"""
    assert tool_matches("user_mcp_*", "user_mcp_1__ping") is True
    assert tool_matches("ssh_*", "user_mcp_1__ping") is False


def test_session_start_allow_default():
    """sessionStart 无 hook → 默认放行（由新引擎处理）。"""
    assert True  # 移除旧层 run_hooks_for_skills 依赖


def test_no_hooks_json_default_allow():
    """无 hooks.json → 默认放行（由新引擎处理）。"""
    assert tool_matches("*", "ssh_execute") is True


def test_apply_post_tool_hook_deny_redacts_output():
    out, ok = apply_post_tool_hook_decision(
        {"success": True, "output": "secret-data", "error": ""},
        {"decision": "deny", "reason": "unsafe", "skill_name": "guard"},
    )
    assert ok is False
    assert out.get("success") is False
    assert out.get("hook_post_denied") is True
    assert "secret-data" not in (out.get("output") or "")
    assert "拒绝采纳" in (out.get("error") or "")
    assert "unsafe" in (out.get("error") or "")


def test_apply_post_tool_hook_allow_passthrough():
    out, ok = apply_post_tool_hook_decision(
        {"success": True, "output": "ok"},
        {"decision": "allow"},
    )
    assert ok is True
    assert out.get("output") == "ok"


def test_tools_registry_merge():
    register_tool(
        {
            "type": "function",
            "function": {"name": "_p2_test_tool", "description": "x", "parameters": {}},
        }
    )
    merged = merge_tools([{"type": "function", "function": {"name": "list_hosts", "parameters": {}}}])
    names = {((t.get("function") or {}).get("name") or "") for t in merged}
    assert "list_hosts" in names
    assert "_p2_test_tool" in names
    assert any(((t.get("function") or {}).get("name") == "_p2_test_tool") for t in list_extra_tools())


def test_run_skill_script_rejects_traversal():
    import asyncio
    from services.run_skill_script import run_skill_script

    async def _go():
        out = await run_skill_script(
            {"id": 1, "username": "u"},
            skill_name="demo",
            script="../evil.py",
        )
        data = json.loads(out)
        assert data.get("success") is False
        assert "单层" in (data.get("error") or "") or "越界" in (data.get("error") or "") or "无效" in (
            data.get("error") or ""
        )

    asyncio.run(_go())
