"""P2：Hook 事件 / allowed_tools / tools_registry / run_skill_script 路径约束。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from services.tools_registry import list_extra_tools, merge_tools, register_tool
from services.user_skills_hooks import check_allowed_tools, run_hooks_for_skills, tool_matches


def test_tool_matches_glob():
    assert tool_matches("ssh_*", "ssh_execute")
    assert tool_matches(["list_hosts", "*channel*"], "send_to_terminal_channel")
    assert not tool_matches("list_hosts", "ssh_execute")


def test_check_allowed_tools():
    assert check_allowed_tools("", "ssh_execute")["allowed"] is True
    assert check_allowed_tools("ssh_*,list_hosts", "ssh_execute")["allowed"] is True
    assert check_allowed_tools("list_hosts", "ssh_execute")["allowed"] is False


def test_before_mcp_hook_deny():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "hooks.json").write_text(
            json.dumps({"beforeMCPExecution": {"decision": "deny", "matcher": "user_mcp_*"}}),
            encoding="utf-8",
        )
        skills = [{"name": "s1", "hooks_enabled": True, "skill_dir": str(p)}]
        dec = run_hooks_for_skills(
            skills, "beforeMCPExecution", tool_name="user_mcp_1__ping", args={}
        )
        assert dec.get("decision") == "deny"


def test_session_start_allow_default():
    dec = run_hooks_for_skills([], "sessionStart")
    assert dec.get("decision") == "allow"


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
