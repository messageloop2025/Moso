"""Event Hook Engine 双源合一决策测试。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from services.event_hook_engine import resolve_hook_decision


# ── helpers ──

def _hooks_json_skill(temp_dir: Path, event_name: str, rule: dict | list[dict]) -> dict:
    """创建一个 skill dict，其 skill_dir 指向含 hooks.json 的临时目录。"""
    skill_dir = temp_dir / "test-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "hooks.json").write_text(
        json.dumps({event_name: rule}), encoding="utf-8"
    )
    return {
        "name": "test-skill",
        "hooks_enabled": True,
        "skill_dir": str(skill_dir),
    }


# ──────────────────────────────────────────
# 1. DB event_rules 表规则
# ──────────────────────────────────────────


class TestDBEventRules:
    @pytest.mark.asyncio
    async def test_db_rule_deny_matched(self):
        """DB event_rules 中 deny 规则命中 → 返回 deny"""
        # resolve_hook_decision 会在 DB 中查 event_rules，这里需要跳过真实 DB
        # 当前模块通过 logger 输出跳过 DB 错误，实际测试需要 mock DB
        # 此测试验证 mock DB 场景下规则优先级
        pass  # 需要 mock DB 连接，单独处理的专项测试

    @pytest.mark.asyncio
    async def test_db_rule_ask_matched(self):
        pass  # 需要 mock DB


# ──────────────────────────────────────────
# 2. hooks.json 文件规则（Skill 级别）
# ──────────────────────────────────────────


class TestHooksJsonDecision:
    @pytest.mark.asyncio
    async def test_pre_tool_use_deny_via_hooks_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            skill = _hooks_json_skill(p, "preToolUse", {
                "matcher": "ssh_*",
                "decision": "deny",
                "reason": "SSH blocked",
            })
            result = await resolve_hook_decision(
                event="preToolUse",
                tool_name="ssh_execute",
                args={},
                hook_skills=[skill],
                user_id=1,
                chat_mode="normal",
            )
            assert result is not None
            assert result["decision"] == "deny"
            assert "SSH blocked" in result.get("reason", "")

    @pytest.mark.asyncio
    async def test_pre_tool_use_ask_via_hooks_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            skill = _hooks_json_skill(p, "preToolUse", {
                "matcher": "*",
                "decision": "ask",
                "reason": "confirm this",
            })
            result = await resolve_hook_decision(
                event="preToolUse",
                tool_name="list_hosts",
                args={},
                hook_skills=[skill],
                user_id=1,
                chat_mode="normal",
            )
            assert result is not None
            assert result["decision"] == "ask"

    @pytest.mark.asyncio
    async def test_pre_tool_use_no_matcher_match_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            skill = _hooks_json_skill(p, "preToolUse", {
                "matcher": "ssh_*",
                "decision": "deny",
            })
            result = await resolve_hook_decision(
                event="preToolUse",
                tool_name="list_hosts",  # 不匹配 ssh_*
                args={},
                hook_skills=[skill],
                user_id=1,
                chat_mode="normal",
            )
            # matcher 不命中 → 返回 allow（默认放行）
            assert result is not None
            assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_hooks_json_list_of_rules(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            skill_dir = p / "multi"
            skill_dir.mkdir()
            (skill_dir / "hooks.json").write_text(json.dumps({
                "preToolUse": [
                    {"matcher": "ssh_*", "decision": "ask", "reason": "check"},
                    {"matcher": "list_hosts", "decision": "deny", "reason": "blocked"},
                ]
            }), encoding="utf-8")
            skill = {"name": "multi", "hooks_enabled": True, "skill_dir": str(skill_dir)}

            # 第一条匹配（ssh_execute）
            r1 = await resolve_hook_decision(
                event="preToolUse", tool_name="ssh_execute", args={},
                hook_skills=[skill], user_id=1, chat_mode="normal",
            )
            assert r1["decision"] == "ask"
            assert r1["reason"] == "check"

            # 第二条匹配（list_hosts）
            r2 = await resolve_hook_decision(
                event="preToolUse", tool_name="list_hosts", args={},
                hook_skills=[skill], user_id=1, chat_mode="normal",
            )
            assert r2["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_new_event_format_agent_tool_pre(self):
        """新格式 agent:tool:pre 应被映射到 preToolUse"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            skill = _hooks_json_skill(p, "preToolUse", {
                "matcher": "ssh_*", "decision": "deny", "reason": "block"
            })
            result = await resolve_hook_decision(
                event="agent:tool:pre",  # 新格式
                tool_name="ssh_execute",
                args={},
                hook_skills=[skill],
                user_id=1,
                chat_mode="normal",
            )
            assert result is not None
            assert result["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_before_mcp_execution_via_hooks_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            skill = _hooks_json_skill(p, "beforeMCPExecution", {
                "matcher": "user_mcp_*", "decision": "deny", "reason": "no MCP"
            })
            result = await resolve_hook_decision(
                event="beforeMCPExecution",
                tool_name="user_mcp_1__ping",
                args={},
                hook_skills=[skill],
                user_id=1,
                chat_mode="normal",
            )
            assert result is not None
            assert result["decision"] == "deny"


# ──────────────────────────────────────────
# 3. 无 hooks.json 时默认放行（DB matcher 已删除）
# ──────────────────────────────────────────


class TestNoHooksJsonFallback:
    """DB matcher 已删除：无 hooks.json 时默认放行"""

    @pytest.mark.asyncio
    async def test_no_hooks_json_allow(self):
        """无 hooks.json → 返回 allow（默认放行）"""
        skill = {
            "name": "guard",
            "hooks_enabled": True,
            "skill_dir": "",
        }
        result = await resolve_hook_decision(
            event="preToolUse",
            tool_name="ssh_execute",
            args={},
            hook_skills=[skill],
            user_id=1,
            chat_mode="normal",
        )
        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_no_hooks_json_allow_list_hosts(self):
        """list_hosts 无 hooks.json → 返回 allow"""
        skill = {
            "name": "guard",
            "hooks_enabled": True,
            "skill_dir": "",
        }
        result = await resolve_hook_decision(
            event="preToolUse",
            tool_name="list_hosts",
            args={},
            hook_skills=[skill],
            user_id=1,
            chat_mode="normal",
        )
        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_no_hooks_json_session_event(self):
        """sessionStart 事件无 hooks.json → 返回 allow"""
        skill = {
            "name": "s",
            "hooks_enabled": True,
            "skill_dir": "",
        }
        result = await resolve_hook_decision(
            event="sessionStart",
            tool_name="",
            args={},
            hook_skills=[skill],
            user_id=1,
            chat_mode="normal",
        )
        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_no_hooks_json_empty_skill_dir(self):
        """空 skill_dir → 默认放行"""
        skill = {
            "name": "empty",
            "hooks_enabled": True,
            "skill_dir": "",
        }
        result = await resolve_hook_decision(
            event="preToolUse",
            tool_name="ssh_execute",
            args={},
            hook_skills=[skill],
            user_id=1,
            chat_mode="normal",
        )
        # matcher 为空 → 返回 allow（默认放行）
        assert result is not None
        assert result["decision"] == "allow"


# ──────────────────────────────────────────
# 4. 全局
# ──────────────────────────────────────────


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_no_hook_skills_no_rules(self):
        result = await resolve_hook_decision(
            event="preToolUse",
            tool_name="list_hosts",
            args={},
            hook_skills=[],
            user_id=1,
            chat_mode="normal",
        )
        # hook_skills 为空 → 返回 allow（默认放行）
        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_hook_skills_is_none(self):
        result = await resolve_hook_decision(
            event="preToolUse",
            tool_name="list_hosts",
            args={},
            hook_skills=None,
            user_id=1,
            chat_mode="normal",
        )
        # hook_skills=None → 返回 allow（默认放行）
        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_hooks_json_deny(self):
        """hooks.json deny 应正常生效"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            skill = _hooks_json_skill(p, "preToolUse", {
                "matcher": "ssh_*", "decision": "deny", "reason": "blocked"
            })

            result = await resolve_hook_decision(
                event="preToolUse",
                tool_name="ssh_execute",
                args={},
                hook_skills=[skill],
                user_id=1,
                chat_mode="normal",
            )
            assert result is not None
            assert result["decision"] == "deny"
