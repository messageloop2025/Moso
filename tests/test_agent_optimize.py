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
        {"type": "function", "function": {"name": "scp_pull", "parameters": {}}},
        {"type": "function", "function": {"name": "scp_push", "parameters": {}}},
    ]
    assert resolve_tools_tier("列出有哪些主机") == "core"
    core = filter_tools_for_message(tools, "列出有哪些主机", lightweight=False)
    core_names = {t["function"]["name"] for t in core}
    assert "list_hosts" in core_names
    assert "send_to_terminal" not in core_names
    assert "batch_create" not in core_names
    assert "scp_pull" not in core_names

    tier_t = resolve_tools_tier("在终端执行 df -h")
    assert "terminal" in tier_t
    term = filter_tools_for_message(tools, "在终端执行 df -h", lightweight=False)
    term_names = {t["function"]["name"] for t in term}
    assert "send_to_terminal" in term_names or "ssh_execute" in term_names
    # 终端层必须带主机文件转运，避免提示词写了 scp_* 但 tools 里没有
    assert "scp_pull" in term_names and "scp_push" in term_names

    # 未写「终端」但要查磁盘 → 仍应带 terminal 层
    assert "terminal" in resolve_tools_tier("查一下 55 号主机的磁盘")

    assert resolve_tools_tier("创建批量任务 batch") == "full"


def test_create_host_in_core_tier_for_add_server_requests():
    """「添加服务器到某组」不得因分层漏掉 create_host / add_hosts_to_group。"""
    tools = [
        {"type": "function", "function": {"name": "list_hosts", "parameters": {}}},
        {"type": "function", "function": {"name": "create_host", "parameters": {}}},
        {"type": "function", "function": {"name": "add_hosts_to_group", "parameters": {}}},
        {"type": "function", "function": {"name": "create_group", "parameters": {}}},
        {"type": "function", "function": {"name": "list_host_groups", "parameters": {}}},
        {"type": "function", "function": {"name": "ssh_execute", "parameters": {}}},
        {"type": "function", "function": {"name": "ask_user_choice", "parameters": {}}},
        {"type": "function", "function": {"name": "batch_create", "parameters": {}}},
    ]
    msg = "添加服务器 10.0.0.30 user/user 到 HC组"
    names = {t["function"]["name"] for t in filter_tools_for_message(tools, msg, lightweight=False)}
    assert "create_host" in names
    assert "add_hosts_to_group" in names
    assert "create_group" in names or "list_host_groups" in names
    assert "batch_create" not in names
    # 纯 core 查询也要带上 CRUD，避免模型只见 list 不见 create
    core_names = {
        t["function"]["name"]
        for t in filter_tools_for_message(tools, "列出有哪些主机", lightweight=False)
    }
    assert "create_host" in core_names


def test_force_full_followup_when_user_insists_to_call_tool():
    from services.agent_optimize import resolve_tools_tier

    assert resolve_tools_tier("你调用一下试试") == "full"
    assert resolve_tools_tier("工具列表没有添加主机？再调用试试") == "full"


def test_scp_tools_available_for_host_file_tasks_without_saying_scp():
    """用户说解压/分析主机文件时，不应因未写 scp 而从 tools 列表拿掉 scp_pull/scp_push。"""
    tools = [
        {"type": "function", "function": {"name": "list_hosts", "parameters": {}}},
        {"type": "function", "function": {"name": "ssh_execute", "parameters": {}}},
        {"type": "function", "function": {"name": "fs_list", "parameters": {}}},
        {"type": "function", "function": {"name": "fs_read_file", "parameters": {}}},
        {"type": "function", "function": {"name": "scp_pull", "parameters": {}}},
        {"type": "function", "function": {"name": "scp_push", "parameters": {}}},
        {"type": "function", "function": {"name": "http_request", "parameters": {}}},
        {"type": "function", "function": {"name": "batch_create", "parameters": {}}},
    ]
    msg = "把 ocserv.20260722.tgz 放到 /tmp，解压后分析比对"
    tier = resolve_tools_tier(msg)
    assert "fs" in tier or "terminal" in tier or "http" in tier
    names = {t["function"]["name"] for t in filter_tools_for_message(tools, msg, lightweight=False)}
    assert "scp_pull" in names
    assert "scp_push" in names
    assert "batch_create" not in names


def test_expand_allow_for_tools_minimal_capabilities():
    from services.agent_optimize import (
        CORE_TOOL_NAMES,
        HOST_FILE_TRANSFER_TOOL_NAMES,
        TERMINAL_TOOL_NAMES,
        expand_allow_for_tools,
        filter_tools_by_allow,
    )

    catalog = set(CORE_TOOL_NAMES) | set(TERMINAL_TOOL_NAMES) | set(HOST_FILE_TRANSFER_TOOL_NAMES)
    plan = expand_allow_for_tools(["scp_push", "ssh_execute"], catalog_names=catalog)
    assert plan["recoverable"] is True
    assert "terminal" in plan["capabilities"]
    assert "host_transfer" in plan["capabilities"]
    assert plan["allow"] is not None
    assert "scp_push" in plan["allow"] and "ssh_execute" in plan["allow"]
    tools = [
        {"type": "function", "function": {"name": n, "parameters": {}}}
        for n in ["list_hosts", "scp_push", "ssh_execute", "batch_create"]
    ]
    names = {
        t["function"]["name"]
        for t in filter_tools_by_allow(tools, plan["allow"], tier_label=plan["tier_label"])
    }
    assert "scp_push" in names and "ssh_execute" in names
    assert "batch_create" not in names

    bad = expand_allow_for_tools(["totally_fake_tool_xyz"], catalog_names=catalog)
    assert bad["recoverable"] is False
    assert "totally_fake_tool_xyz" in bad["missing_in_catalog"]


def test_detect_missing_tools_from_text_requires_negative_context():
    from services.agent_optimize import (
        CORE_TOOL_NAMES,
        HOST_FILE_TRANSFER_TOOL_NAMES,
        TERMINAL_TOOL_NAMES,
        detect_missing_tools_from_text,
    )

    catalog = set(CORE_TOOL_NAMES) | set(TERMINAL_TOOL_NAMES) | set(HOST_FILE_TRANSFER_TOOL_NAMES)
    apology = "抱歉，当前允许的可用工具中缺少远程文件传输 (scp_push) 和 SSH 执行 (ssh_execute)。"
    found = detect_missing_tools_from_text(
        apology, available_names={"list_hosts", "ask_user_choice"}, catalog_names=catalog
    )
    assert "scp_push" in found and "ssh_execute" in found
    # 正向提及不应触发
    assert (
        detect_missing_tools_from_text(
            "已用 scp_push 上传完成",
            available_names=set(),
            catalog_names=catalog,
        )
        == []
    )


def test_upgrade_and_short_confirm_get_terminal_transfer_tools():
    """「升级」与确认短句「是」不得掉到纯 core（缺 scp_push/ssh_execute）。"""
    tools = [
        {"type": "function", "function": {"name": "list_hosts", "parameters": {}}},
        {"type": "function", "function": {"name": "ssh_execute", "parameters": {}}},
        {"type": "function", "function": {"name": "scp_push", "parameters": {}}},
        {"type": "function", "function": {"name": "scp_pull", "parameters": {}}},
        {"type": "function", "function": {"name": "ask_user_choice", "parameters": {}}},
        {"type": "function", "function": {"name": "batch_create", "parameters": {}}},
    ]
    assert "terminal" in resolve_tools_tier("升级")
    up_names = {t["function"]["name"] for t in filter_tools_for_message(tools, "升级", lightweight=False)}
    assert "scp_push" in up_names and "ssh_execute" in up_names

    # 短确认：需 recent_context 回看「升级」
    tier_yes = resolve_tools_tier("是", recent_context="升级到最新版")
    assert "terminal" in tier_yes
    yes_names = {
        t["function"]["name"]
        for t in filter_tools_for_message(
            tools, "是", lightweight=False, recent_context="升级到最新版"
        )
    }
    assert "scp_push" in yes_names and "ssh_execute" in yes_names

    # 主机详情会话：即便只问列表，也默认带 terminal/转运
    tier_host = resolve_tools_tier("列出有哪些主机", session_host_id=12)
    assert "terminal" in tier_host
    host_names = {
        t["function"]["name"]
        for t in filter_tools_for_message(
            tools, "列出有哪些主机", lightweight=False, session_host_id=12
        )
    }
    assert "scp_push" in host_names and "ssh_execute" in host_names
    assert "batch_create" not in host_names


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
