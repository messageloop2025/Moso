from services.agent_optimize import (
    agent_can_parallel_read_tools,
    build_system_prompt_for_step,
    compact_turn_tool_messages,
    effective_llm_timeout_retries,
    filter_tools_for_message,
    fold_tool_content_to_ref,
    is_lightweight_chat_message,
    should_force_full_chat_prompts,
    message_needs_html_artifact,
    message_needs_ssh_terminal_rules,
    resolve_tools_tier,
    resolve_weak_network_mode,
    should_enrich_tool_images,
    should_skip_assistant_after_chat,
    should_skip_assistant_ai,
)
from services.terminal_poll import resolve_terminal_poll_seconds


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


def test_build_system_prompt_for_step_strips_terminal_and_hosts():
    full = "## 当前用户控制台最近输出\nline1\nline2\n\n## 当前主机列表\nhost-A\nhost-B\n\n## 主机分组\ng1"
    slim = build_system_prompt_for_step(full, 1)
    assert "line1" not in slim
    assert "省略滚动缓冲" in slim
    assert "host-A" not in slim
    assert "省略主机列表" in slim


def test_effective_llm_timeout_retries_default_tight():
    assert effective_llm_timeout_retries(True) <= 2
    assert effective_llm_timeout_retries(False) <= 2


def test_should_skip_assistant_when_weak():
    assert should_skip_assistant_ai(True, True) is True
    assert should_skip_assistant_ai(False, True) is False
    assert should_skip_assistant_ai(True, False) is True


def test_lightweight_chat_disabled():
    """轻量寒暄快路径已弃用：任何消息都不得清空 tools。"""
    from services.agent_optimize import resolve_tools_tier

    for msg in ("在吗", "你好", "hello", "你没发起 toolcall", "真实 curl", "访问一下 moss.pinglan.cc"):
        assert is_lightweight_chat_message(msg) is False
    assert "http" in resolve_tools_tier("访问一下 moss.pinglan.cc")


def test_lightweight_no_longer_empties_tools():
    tools = [
        {"type": "function", "function": {"name": "list_hosts", "parameters": {}}},
        {"type": "function", "function": {"name": "get_current_time", "parameters": {}}},
    ]
    # 即使误传 lightweight=True，也不得返回空工具集
    kept = filter_tools_for_message(tools, "在吗", lightweight=True)
    assert {t["function"]["name"] for t in kept} >= {"list_hosts", "get_current_time"}
    filtered = filter_tools_for_message(tools, "列出主机", lightweight=False)
    names = {t["function"]["name"] for t in filtered}
    assert "list_hosts" in names
    assert "get_current_time" in names


def test_tools_tier_core_vs_terminal():
    tools = [
        {"type": "function", "function": {"name": "list_hosts", "parameters": {}}},
        {"type": "function", "function": {"name": "send_to_terminal", "parameters": {}}},
        {"type": "function", "function": {"name": "fs_list", "parameters": {}}},
        {"type": "function", "function": {"name": "http_request", "parameters": {}}},
        {"type": "function", "function": {"name": "batch_create", "parameters": {}}},
        {"type": "function", "function": {"name": "get_host_detail", "parameters": {}}},
        {"type": "function", "function": {"name": "search_hosts", "parameters": {}}},
        {"type": "function", "function": {"name": "ask_user_choice", "parameters": {}}},
        {"type": "function", "function": {"name": "get_terminal_buffer", "parameters": {}}},
        {"type": "function", "function": {"name": "ssh_execute", "parameters": {}}},
    ]
    assert resolve_tools_tier("列出有哪些主机") == "core"
    core = filter_tools_for_message(tools, "列出有哪些主机", lightweight=False)
    core_names = {t["function"]["name"] for t in core}
    assert "list_hosts" in core_names
    assert "send_to_terminal" not in core_names
    assert "batch_create" not in core_names

    tier_t = resolve_tools_tier("在终端执行 df -h")
    assert "terminal" in tier_t
    term = filter_tools_for_message(tools, "在终端执行 df -h", lightweight=False)
    term_names = {t["function"]["name"] for t in term}
    assert "send_to_terminal" in term_names or "ssh_execute" in term_names

    # 未写「终端」但要查磁盘 → 仍应带 terminal 层
    assert "terminal" in resolve_tools_tier("查一下 55 号主机的磁盘")

    assert resolve_tools_tier("创建批量任务 batch") == "full"


def test_skip_assistant_after_greeting():
    assert should_skip_assistant_after_chat(
        assistant_enabled=True,
        weak_network=False,
        round_had_tool_call=False,
        user_message="在吗",
        actionable_user_request=False,
    ) is True
    # 即使误标 actionable，闲聊仍应跳过辅助 AI
    assert should_skip_assistant_after_chat(
        assistant_enabled=True,
        weak_network=False,
        round_had_tool_call=False,
        user_message="在吗",
        actionable_user_request=True,
    ) is True
    assert should_skip_assistant_after_chat(
        assistant_enabled=True,
        weak_network=False,
        round_had_tool_call=True,
        user_message="部署一下",
        actionable_user_request=True,
        tool_trace=[],
    ) is False


def test_skip_assistant_after_delivery_tools():
    trace = [
        {"type": "tool", "event": "finished", "action": "completed", "tool": "list_hosts"},
    ]
    assert should_skip_assistant_after_chat(
        assistant_enabled=True,
        weak_network=False,
        round_had_tool_call=True,
        user_message="列出主机",
        actionable_user_request=True,
        tool_trace=trace,
        assistant_content="当前共有 3 台主机。",
    ) is True
    assert should_skip_assistant_after_chat(
        assistant_enabled=True,
        weak_network=False,
        round_had_tool_call=True,
        user_message="列出主机",
        actionable_user_request=True,
        tool_trace=trace,
        assistant_content="已查到主机，接下来我会连接终端执行检查。",
    ) is False


def test_enrich_images_whitelist_default():
    assert should_enrich_tool_images(False, tool_name="list_hosts", tool_result='{"success":true}') is False
    assert should_enrich_tool_images(
        False, tool_name="mcp_draw", tool_result='{"url":"x"}'
    ) is True
    assert should_enrich_tool_images(
        False, tool_name="list_hosts", tool_result='{"image_url":"data:image/png;base64,xx"}'
    ) is True
    assert should_enrich_tool_images(True, tool_name="mcp_draw", tool_result='{"x":1}') is False


def test_poll_complete_forces_zero_even_with_explicit():
    buf = "done\nroot@host:~# "
    assert resolve_terminal_poll_seconds(explicit=30, send_hint=5, buffer=buf) == 0


def test_message_intent_helpers():
    assert message_needs_html_artifact("生成一个 HTML 报表") is True
    assert message_needs_html_artifact("列出主机") is False
    assert message_needs_ssh_terminal_rules("在终端执行 ls") is True



def test_should_force_full_chat_prompts():
    assert should_force_full_chat_prompts(session_host_id=40) is True
    assert should_force_full_chat_prompts(session_scope="local") is True
    assert should_force_full_chat_prompts(session_prompt="只做巡检") is True
    assert should_force_full_chat_prompts(context_host_id=3) is True
    assert should_force_full_chat_prompts() is False
