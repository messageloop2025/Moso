from services.agent_optimize import (
    agent_can_parallel_read_tools,
    build_system_prompt_for_step,
    compact_turn_tool_messages,
    effective_llm_timeout_retries,
    fold_tool_content_to_ref,
    resolve_weak_network_mode,
    should_enrich_tool_images,
    should_skip_assistant_ai,
)


def test_resolve_weak_network_from_settings():
    assert resolve_weak_network_mode({"ai_weak_network": "true"}) is True
    assert resolve_weak_network_mode({"ai_weak_network": "false"}) is False
    assert resolve_weak_network_mode({}) is False


def test_parallel_read_tools():
    assert agent_can_parallel_read_tools(["fs_list", "get_host_detail"]) is True
    assert agent_can_parallel_read_tools(["fs_list", "send_to_terminal"]) is False
    assert agent_can_parallel_read_tools(["fs_list"]) is False


def test_fold_tool_content_to_ref_keeps_spill():
    raw = "[[EDGEOPS_CHAT_DATA ref=abc subdir=2026/07/17 chars=9000 tool=x session=1]]\n" + ("x" * 5000)
    folded = fold_tool_content_to_ref(raw)
    assert "[[EDGEOPS_CHAT_DATA" in folded
    assert "上下文折叠" in folded
    assert len(folded) < len(raw)


def test_compact_turn_tool_messages():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "1", "content": "a" * 3000},
        {"role": "tool", "tool_call_id": "2", "content": "b" * 3000},
        {"role": "tool", "tool_call_id": "3", "content": "c" * 100},
    ]
    saved = compact_turn_tool_messages(messages, turn_start=1, keep_pairs=1)
    assert saved > 0
    assert "上下文折叠" in messages[2]["content"]
    assert messages[4]["content"] == "c" * 100


def test_build_system_prompt_for_step_strips_terminal():
    full = "## 当前用户控制台最近输出\nline1\nline2\n\n## 当前主机列表\nhosts"
    slim = build_system_prompt_for_step(full, 1)
    assert "line1" not in slim
    assert "省略滚动缓冲" in slim


def test_effective_llm_timeout_retries_weak():
    assert effective_llm_timeout_retries(True) <= 2
    assert effective_llm_timeout_retries(False) >= 1


def test_should_skip_assistant_when_weak():
    assert should_skip_assistant_ai(True, True) is True
    assert should_skip_assistant_ai(False, True) is False
    assert should_skip_assistant_ai(True, False) is True
