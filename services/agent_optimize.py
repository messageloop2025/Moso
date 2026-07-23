"""Agent 交互性能优化：工具分层、上下文折叠、工具并行、system 分层（不依赖弱网假设）。"""
from __future__ import annotations

import re
from typing import Any

import config

# 可并行执行的只读工具（无写操作、无 UI 等待、无流式进度）
PARALLEL_READ_TOOLS: frozenset[str] = frozenset({
    "fs_list",
    "fs_search",
    "fs_read_file",
    "get_host_detail",
    "list_hosts",
    "search_hosts",
    "search_hosts_by_prompt",
    "list_host_groups",
    "get_group_hosts",
    "get_host_prompt",
    "get_host_knowledge",
    "list_terminals",
    "get_terminal_status",
    "get_terminal_buffer",
    "ssh_channel_info",
    "ssh_channel_has_new",
    "ssh_channel_read_lines",
    "ssh_channel_read_length",
    "ssh_channel_list",
    "list_recent_tool_results",
    "get_recent_tool_result",
    "read_chat_data",
    "get_chats_workspace_dir",
    "list_best_practices",
    "get_best_practice",
    "get_best_practices",
    "list_maintenance_history",
    "get_maintenance_item",
    "list_operation_logs",
    "list_scheduled_tasks",
    "get_scheduled_task",
    "list_triggered_tasks",
    "get_triggered_task",
    "list_batch_jobs",
    "get_batch_job",
    "edgeops_list_hosts",
    "edgeops_get_host",
    "edgeops_get_host_prompt",
    "edgeops_search_hosts",
    "edgeops_search_hosts_by_prompt",
    "edgeops_list_host_tags",
    "edgeops_host_alive",
    "edgeops_host_stats",
    "edgeops_remote_fs_list",
    "edgeops_remote_fs_read",
    "edgeops_read_chat_data",
    "edgeops_list_maintenance_history",
    "edgeops_list_operation_logs",
    "math_compute",
    "math_calculate",
    "get_current_time",
    "get_server_time",
    "get_session_operations",
})

_STREAMING_OR_STATE_TOOLS: frozenset[str] = frozenset({
    "send_to_terminal",
    "terminal_send_and_read",
    "send_service_password",
    "ssh_channel_send",
    "connect_terminal",
    "create_console",
    "ask_user_choice",
    "delegate_to_edgeops_ai",
    "delegate_sub_tasks_batch",
    "scp_push",
    "scp_pull",
    "http_download",
    "http_upload",
})

# —— 工具分层 —— #

CORE_TOOL_NAMES: frozenset[str] = frozenset({
    "list_hosts",
    "search_hosts",
    "search_hosts_by_prompt",
    "get_host_detail",
    "get_host_prompt",
    "get_host_knowledge",
    "list_host_groups",
    "get_host_groups_tree",
    "get_group_hosts",
    "get_group_detail",
    "list_host_tags",
    "list_terminals",
    "get_terminal_status",
    "ask_user_choice",
    "get_best_practices",
    "list_prompt_skills",
    "get_prompt_skill",
    "list_maintenance_history",
    "get_maintenance_item",
    "host_stats",
    "detect_host_os",
    "get_host_capabilities",
    "probe_host_capabilities",
    "list_service_credentials",
    "get_session_operations",
    "get_session_chat_detail",
    "math_calculate",
    "regex_process",
    "string_process",
    "data_query",
    "markup_query",
    "crypto_toolkit",
    "get_me",
    "get_server_time",
    "get_current_time",
    "list_logs",
    "get_aihelp_index",
    "list_aihelp_files",
    "get_aihelp_file",
})

TERMINAL_TOOL_NAMES: frozenset[str] = frozenset({
    "send_to_terminal",
    "terminal_send_and_read",
    "get_terminal_buffer",
    "connect_terminal",
    "create_console",
    "close_console",
    "send_service_password",
    "ssh_execute",
    "ssh_channel_create",
    "ssh_channel_list",
    "ssh_channel_info",
    "ssh_channel_get_status",
    "ssh_channel_send",
    "ssh_channel_read_lines",
    "ssh_channel_read_length",
    "ssh_channel_has_new",
    "ssh_channel_close",
    "ssh_channel_close_batch",
    "ssh_channel_dump_output",
})

FS_TOOL_NAMES: frozenset[str] = frozenset({
    "fs_list",
    "fs_search",
    "fs_read_file",
    "fs_write_file",
    "fs_read_binary",
    "fs_write_binary",
    "fs_mkdir",
    "fs_delete",
    "fs_copy",
    "fs_pack_tgz",
    "fs_unpack_tgz",
    "fs_truncate",
    "get_chats_workspace_dir",
    "read_chat_data",
    "create_chat_artifact",
    "update_chat_artifact",
    "list_chat_artifacts",
    "read_chat_artifact_file",
    "list_chat_attachments",
    "read_chat_attachment",
    "markdown_list_sections",
    "markdown_read_section",
    "markdown_replace_section",
    "markdown_search_sections",
    "local_fs_list",
    "local_fs_read",
    "local_fs_write",
    "local_fs_write_file",
    "local_fs_read_binary",
    "local_fs_write_binary",
    "local_fs_mkdir",
    "local_fs_delete",
    "local_fs_rename",
    "local_fs_truncate",
    "local_chat_data_paths",
    "local_chat_write_file",
    "local_chat_write_binary",
})

# 主机↔工作区 / 主机↔主机文件转运：与「HTTP 下载」不同，运维对话常只触发 fs/terminal 层。
# 必须随 terminal/fs 一并下发，否则 system 提示写了 scp_pull/scp_push 但 tools 列表里没有。
HOST_FILE_TRANSFER_TOOL_NAMES: frozenset[str] = frozenset({
    "scp_push",
    "scp_pull",
    "relay_file_between_hosts",
    "transfer_file_between_hosts",
    "build_scp_transfer_script",
})

HTTP_TOOL_NAMES: frozenset[str] = frozenset({
    "http_request",
    "http_download",
    "http_upload",
    "http_download_merge",
    "git_clone_on_host",
    "search_web",
    "search_github",
}) | HOST_FILE_TRANSFER_TOOL_NAMES

FS_TOOL_PREFIXES: tuple[str, ...] = ("fs_", "local_fs_", "local_chat_")
TERMINAL_TOOL_PREFIXES: tuple[str, ...] = ("ssh_channel_",)

_FULL_HINT_RE = re.compile(
    r"(批量|batch_|定时|触发|scheduled|triggered|mcp|skill|凭证库|模型.?profile|"
    r"ai_model_profile|工作流|workflow|委托|delegate_|子任务|用户管理|管理员|"
    r"best_practice|最佳实践.?写|写.?最佳实践)",
    re.IGNORECASE,
)
_TERMINAL_HINT_RE = re.compile(
    r"(终端|控制台|ssh|sudo|命令|执行|通道|channel|连接|登录|重启|安装|部署|"
    r"编译|日志|排障|进程|docker|nginx|systemctl|apt|yum|dnf|"
    r"send_to_terminal|get_terminal|ssh_execute|ssh_channel)",
    re.IGNORECASE,
)
_FS_HINT_RE = re.compile(
    r"(文件|目录|工作区|fs_|artifact|报告|html|上传.?本地|下载.?本地|"
    r"读.?文件|写.?文件|搜索.?文件|打包|解压|tgz|tar\.gz|归档|备份|"
    r"落盘|/tmp|传到|拷到|拷贝|搬到|转运|比对|脚本.?落盘)",
    re.IGNORECASE,
)
_HTTP_HINT_RE = re.compile(
    r"(http|https|url|wget|curl|下载|上传|scp|sftp|拉取|推送|api.?请求|"
    r"http_request|http_download|scp_push|scp_pull|连通性|探测|"
    r"拉回|推到|中转|relay|transfer_file|"
    r"[a-z0-9][a-z0-9.-]*\.(com|cn|cc|io|net|org|local|dev|xyz|me|top)\b)",
    re.IGNORECASE,
)
_CORE_HINT_RE = re.compile(
    r"(主机|服务器|分组|标签|列表|搜索|查一下|有哪些|详情|提示词|知识库|"
    r"list_hosts|search_hosts|get_host)",
    re.IGNORECASE,
)
# 虽未写「终端」但明显要上机执行/排查 → 并入 terminal 层
_EXEC_SOFT_RE = re.compile(
    r"(执行|运行|安装|部署|重启|登录|连接|终端|控制台|通道|sudo|ssh|命令|"
    r"\bdf\b|磁盘|内存|负载|进程|服务|nginx|docker|排查|日志|uptime|top\b|systemctl)",
    re.IGNORECASE,
)

# 已有工具结果、可视为「本轮已交付」的工具（辅助 AI 可停）
_DELIVERY_TOOL_NAMES: frozenset[str] = frozenset({
    "get_terminal_buffer",
    "terminal_send_and_read",
    "ssh_execute",
    "ssh_channel_read_lines",
    "ssh_channel_read_length",
    "ssh_channel_dump_output",
    "fs_read_file",
    "fs_write_file",
    "fs_list",
    "fs_search",
    "list_hosts",
    "search_hosts",
    "get_host_detail",
    "create_chat_artifact",
    "update_chat_artifact",
    "http_request",
    "http_download",
    "scp_pull",
    "scp_push",
    "data_query",
    "read_chat_data",
    "get_best_practices",
})

_CONTINUE_USER_RE = re.compile(
    r"(继续|下一步|接着|再来|请继续|go on|continue|next\b)",
    re.IGNORECASE,
)
_PENDING_ASSISTANT_RE = re.compile(
    r"(接下来|下一步|继续执行|正在|稍后|还需|还要|随后|然后我|我会再|待会|马上)",
    re.IGNORECASE,
)


def resolve_weak_network_mode(settings: dict | None = None) -> bool:
    if getattr(config, "AI_WEAK_NETWORK_MODE", False):
        return True
    if not settings:
        return False
    raw = (settings.get("ai_weak_network") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def effective_llm_timeout_retries(weak_network: bool) -> int:
    """额外超时重试次数。全局默认已收紧；弱网可用独立更小值。"""
    if weak_network:
        return max(0, int(getattr(config, "AI_CHAT_LLM_TIMEOUT_RETRIES_WEAK", 1)))
    return max(0, int(getattr(config, "AI_CHAT_LLM_TIMEOUT_RETRIES", 1)))


def should_skip_assistant_ai(weak_network: bool, assistant_enabled: bool) -> bool:
    if not assistant_enabled:
        return True
    if weak_network and getattr(config, "AI_WEAK_NETWORK_SKIP_ASSISTANT", True):
        return True
    return False


def should_enrich_tool_images(
    weak_network: bool,
    *,
    tool_name: str = "",
    tool_result: str = "",
) -> bool:
    """默认不 enrich；仅当结果含图或 MCP/生图类工具时才处理。弱网强制跳过。"""
    if weak_network and getattr(config, "AI_WEAK_NETWORK_SKIP_TOOL_IMAGE_ENRICH", True):
        return False
    raw = tool_result or ""
    if "data:image" in raw or "image_url" in raw:
        return True
    name = (tool_name or "").strip().lower()
    if not name:
        return False
    if name.startswith("mcp") or "_mcp_" in name or name.endswith("_mcp"):
        return True
    if any(k in name for k in ("image", "screenshot", "annotate", "vision", "edit_chat_attachment")):
        return True
    return False


def agent_can_parallel_read_tools(tool_names: list[str]) -> bool:
    if not getattr(config, "AGENT_PARALLEL_READ_TOOLS", True):
        return False
    if len(tool_names) < 2:
        return False
    for name in tool_names:
        if name not in PARALLEL_READ_TOOLS:
            return False
        if name in _STREAMING_OR_STATE_TOOLS:
            return False
    return True


def fold_tool_content_to_ref(content: str, *, max_ref_chars: int = 280) -> str:
    """将 tool 消息折叠为引用摘要，保留 spill 哨兵与 cache id 线索。"""
    raw = content or ""
    if len(raw) <= max_ref_chars:
        return raw
    spill_line = ""
    for line in raw.splitlines():
        if "[[EDGEOPS_CHAT_DATA" in line:
            spill_line = line.strip()
            break
    cache_m = re.search(r'"result_cache_id"\s*:\s*(\d+)', raw)
    cache_hint = f" result_cache_id={cache_m.group(1)}" if cache_m else ""
    if spill_line:
        return (
            f"{spill_line}\n"
            f"【上下文折叠】完整工具输出已从本轮 messages 省略（原 {len(raw)} 字符）。"
            f"需要细节请 read_chat_data 或 get_recent_tool_result。{cache_hint}"
        )
    preview = raw[:120].replace("\n", " ")
    return (
        f"【上下文折叠】工具输出摘要：{preview}…（原 {len(raw)} 字符）。"
        f"需要完整内容请 get_recent_tool_result。{cache_hint}"
    )


def compact_turn_tool_messages(
    messages: list[dict[str, Any]],
    turn_start: int,
    *,
    keep_pairs: int | None = None,
) -> int:
    """折叠当前 turn 内较早的 tool 消息，返回约节省字符数。"""
    keep = keep_pairs
    if keep is None:
        keep = int(getattr(config, "AGENT_TURN_TOOL_KEEP_PAIRS", 2))
    keep = max(1, keep)
    tool_indices = [
        i for i in range(max(0, turn_start), len(messages))
        if (messages[i].get("role") or "") == "tool"
    ]
    if len(tool_indices) <= keep:
        return 0
    saved = 0
    for idx in tool_indices[:-keep]:
        old = messages[idx].get("content") or ""
        new = fold_tool_content_to_ref(old)
        if new != old:
            saved += max(0, len(old) - len(new))
            messages[idx]["content"] = new
    return saved


def build_system_prompt_for_step(
    full_system: str,
    step_index: int,
    *,
    session_host_id: int | None = None,
    weak_network: bool = False,
) -> str:
    """Agent 第 2 步起裁剪 system 中低时效大块，降低每轮上传体积。"""
    del weak_network  # 主机列表裁剪已默认，不再依赖弱网
    strip_after = int(getattr(config, "AGENT_SYSTEM_STRIP_AFTER_STEP", 1))
    if step_index < strip_after:
        return full_system
    text = full_system or ""
    replacements = [
        (
            "## 当前用户控制台最近输出",
            "## 当前用户控制台最近输出\n"
            "（后续步骤已省略滚动缓冲全文；"
            "请用 get_terminal_status / get_terminal_buffer / terminal_send_and_read 按需读取。）\n",
        ),
        (
            "## 当前主机列表",
            "## 当前主机列表\n（后续步骤已省略主机列表全文；请用 search_hosts / get_host_detail 按需查询。）\n",
        ),
    ]
    if session_host_id is not None:
        replacements.append((
            "## 主机分组",
            "## 主机分组\n（单主机会话，分组列表已省略以减负。）\n",
        ))
    for marker, stub in replacements:
        start = text.find(marker)
        if start < 0:
            continue
        nxt = text.find("\n## ", start + len(marker))
        if nxt < 0:
            text = text[:start] + stub
        else:
            text = text[:start] + stub + text[nxt + 1:]
    return text


# —— 轻量对话快路径（问候/闲聊）：避免每次上传 200+ tools 与 70K+ system —— #

_LIGHTWEIGHT_EXACT = frozenset({
    "在吗", "在么", "在不在", "你好", "您好", "嗨", "哈喽", "hello", "hi", "hey",
    "早上好", "下午好", "晚上好", "早安", "晚安", "谢谢", "感谢", "多谢", "thanks", "thank you",
    "ok", "okay", "好的", "好", "嗯", "嗯嗯", "收到", "明白", "了解", "行",
    "测试", "test", "ping", "?", "？",
})

_OPS_HINT_RE = re.compile(
    r"(主机|服务器|终端|控制台|ssh|sudo|scp|上传|下载|部署|安装|重启|日志|报错|错误|"
    r"脚本|文件|目录|fs_|批量|任务|定时|触发|凭证|密码|分组|标签|mcp|skill|"
    r"执行|命令|连接|通道|docker|nginx|mysql|排查|运维|配置|备份|还原|"
    r"list_|get_|search_|send_|create_|delete_|update_)",
    re.IGNORECASE,
)

# 轻量快路径排除：HTTP/探测/催促 toolcall 等（勿与寒暄共用短句规则）
_LIGHTWEIGHT_BLOCK_RE = re.compile(
    r"(http|https|url|wget|curl|连通|探测|测一下|访问一下|打开一下|"
    r"tool.?call|工具调用|发起调用|自己执行|真实执行|真的去|真去|"
    r"不要只|别只|别贴|不要贴|代码块|假执行|自己跑|实际执行|"
    r"[a-z0-9][a-z0-9.-]*\.(com|cn|cc|io|net|org|local|dev|xyz|me|top)\b)",
    re.IGNORECASE,
)

# 轻量模式仍可能需要的极小工具集（默认不传任何 tools）
LIGHTWEIGHT_FALLBACK_TOOLS: frozenset[str] = frozenset({
    "get_current_time",
    "ask_user_choice",
})


def should_force_full_chat_prompts(
    *,
    session_host_id: int | None = None,
    session_scope: str | None = None,
    session_prompt: str | None = None,
    context_host_id: int | None = None,
) -> bool:
    """主机/本机运维会话、已有会话提示词、或显式 context_host 时禁用轻量快路径。"""
    if session_host_id:
        return True
    if (session_scope or "").strip().lower() == "local":
        return True
    if (session_prompt or "").strip():
        return True
    try:
        if context_host_id is not None and int(context_host_id) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


def is_lightweight_chat_message(user_message: str) -> bool:
    """轻量寒暄快路径已弃用：恒返回 False，始终走完整提示词与工具装载。"""
    return False


def conversation_blocks_lightweight(conversation: list | None) -> bool:
    """轻量路径已弃用；保留函数签名供旧调用方兼容。"""
    return False


def is_greeting_chat_message(user_message: str) -> bool:
    """纯问候/确认短句（仅用于跳过辅助 AI，不再清空 tools / 缩短 system）。"""
    raw = (user_message or "").strip()
    if not raw:
        return True
    text = re.sub(r"[\s\U0001F300-\U0001FAFF]+", "", raw)
    text = text.strip("！!。.~～…、，,")
    if not text:
        return True
    if len(text) > 40:
        return False
    if _OPS_HINT_RE.search(text) or _HTTP_HINT_RE.search(text) or _LIGHTWEIGHT_BLOCK_RE.search(text):
        return False
    low = text.lower()
    if low in _LIGHTWEIGHT_EXACT or text in _LIGHTWEIGHT_EXACT:
        return True
    if len(text) <= 12 and not re.search(r"[/\\:=@]", text):
        return True
    return False


def _tool_trace_has_delivery(tool_trace: list | None) -> bool:
    for step in tool_trace or []:
        if not isinstance(step, dict) or step.get("type") != "tool":
            continue
        if step.get("event") != "finished":
            continue
        if (step.get("action") or "") == "failed":
            continue
        name = (step.get("tool") or "").strip()
        if name in _DELIVERY_TOOL_NAMES or name.startswith("fs_") or name.startswith("ssh_channel_read"):
            return True
    return False


def should_skip_assistant_after_chat(
    *,
    assistant_enabled: bool,
    weak_network: bool,
    round_had_tool_call: bool,
    user_message: str,
    actionable_user_request: bool,
    tool_trace: list | None = None,
    assistant_content: str = "",
) -> bool:
    """闲聊/无工具/已交付结果时跳过辅助 AI，避免多打一轮 LLM。"""
    if should_skip_assistant_ai(weak_network, assistant_enabled):
        return True
    if not getattr(config, "AGENT_SKIP_ASSISTANT_ON_CHAT", True):
        return False
    um = user_message or ""
    # 纯问候优先于 actionable：避免「在吗」仍打辅助 AI（不再走清空 tools 的轻量路径）
    if is_greeting_chat_message(um) and not round_had_tool_call:
        return True
    if _CONTINUE_USER_RE.search(um.strip()):
        return False
    if round_had_tool_call and _tool_trace_has_delivery(tool_trace):
        # 已有交付类结果；若正文仍像「还要接着干」则交给辅助 AI
        if assistant_content and _PENDING_ASSISTANT_RE.search(assistant_content):
            return False
        return True
    if round_had_tool_call:
        return False
    if actionable_user_request:
        return False
    return True


_FORCE_FULL_FOLLOWUP_RE = re.compile(
    r"(tool.?call|工具调用|自己执行|真实执行|真的去|自己跑|发起调用|假执行)",
    re.IGNORECASE,
)


def resolve_tools_tier(
    user_message: str,
    *,
    lightweight: bool | None = None,
    force_full: bool = False,
    session_scope: str | None = None,
) -> str:
    """返回 lightweight / core / terminal / fs / http / ops / full。

    ops = core∪terminal（常见运维默认）；多意图用 '+' 拼接（如 core+terminal+fs）。
    """
    if force_full or not getattr(config, "AGENT_TOOL_TIERING", True):
        return "full"
    # 轻量路径已弃用：忽略 lightweight 入参
    msg = user_message or ""
    # 用户催促「真正发起 toolcall」：升 full，避免分层漏掉上一轮所需工具
    if _FORCE_FULL_FOLLOWUP_RE.search(msg):
        return "full"
    scope = (session_scope or "").strip().lower()
    if scope in ("local",) and _FS_HINT_RE.search(msg):
        # 本机管理会话读文件仍走 fs 层
        pass
    if _FULL_HINT_RE.search(msg):
        return "full"
    parts: list[str] = ["core"]
    if _TERMINAL_HINT_RE.search(msg) or scope in ("host", "ssh"):
        parts.append("terminal")
    if _FS_HINT_RE.search(msg) or scope == "local":
        parts.append("fs")
    if _HTTP_HINT_RE.search(msg):
        parts.append("http")
    # 运维语义：纯列表/搜索可留 core；带执行/排查意味则并入 terminal
    if parts == ["core"] and _OPS_HINT_RE.search(msg):
        if _EXEC_SOFT_RE.search(msg) or not _CORE_HINT_RE.search(msg):
            parts.append("terminal")
    # 去重保序
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    if seen == ["core"]:
        return "core"
    return "+".join(seen)


def _allow_set_for_tier(tier: str) -> frozenset[str] | None:
    """None 表示 full（不过滤）；lightweight 由调用方单独处理。"""
    if tier in ("full", "lightweight"):
        return None
    names: set[str] = set(CORE_TOOL_NAMES)
    parts = [p for p in tier.split("+") if p]
    for part in parts:
        if part == "terminal":
            names |= TERMINAL_TOOL_NAMES
        elif part == "fs":
            names |= FS_TOOL_NAMES
        elif part == "http":
            names |= HTTP_TOOL_NAMES
        elif part == "core":
            pass
        elif part == "ops":
            names |= TERMINAL_TOOL_NAMES
    # 主机侧文件任务几乎总会开 terminal/fs；此时必须带上 scp_pull/scp_push 等转运工具
    if any(p in ("terminal", "fs", "http", "ops") for p in parts):
        names |= HOST_FILE_TRANSFER_TOOL_NAMES
    return frozenset(names)


def _tool_allowed_in_tier(name: str, allow: frozenset[str], tier: str) -> bool:
    if name in allow:
        return True
    if "terminal" in tier.split("+") or tier == "ops":
        for p in TERMINAL_TOOL_PREFIXES:
            if name.startswith(p):
                return True
    if "fs" in tier.split("+"):
        for p in FS_TOOL_PREFIXES:
            if name.startswith(p):
                return True
    return False


def filter_tools_for_message(
    tools: list[dict[str, Any]],
    user_message: str,
    *,
    lightweight: bool | None = None,
    tier: str | None = None,
    force_full: bool = False,
    session_scope: str | None = None,
) -> list[dict[str, Any]]:
    """按消息意图裁剪 tools；轻量对话默认空列表；运维按 tier 分层。"""
    if lightweight is None:
        lightweight = is_lightweight_chat_message(user_message)
    if tier is None:
        tier = resolve_tools_tier(
            user_message,
            lightweight=lightweight,
            force_full=force_full,
            session_scope=session_scope,
        )
    # 轻量路径已弃用：即使调用方误传 lightweight/tier=lightweight，也不清空 tools
    if tier == "lightweight" or lightweight:
        lightweight = False
        tier = resolve_tools_tier(
            user_message,
            lightweight=False,
            force_full=force_full,
            session_scope=session_scope,
        )
    if tier == "full" or force_full:
        return list(tools or [])
    allow = _allow_set_for_tier(tier)
    if allow is None:
        return list(tools or [])
    out = []
    for t in tools or []:
        try:
            name = t["function"]["name"]
        except Exception:
            continue
        if _tool_allowed_in_tier(name, allow, tier):
            out.append(t)
    # 分层结果为空时回退 full，避免误伤能力
    if not out and tools:
        return list(tools or [])
    return out


def tools_need_full_upgrade(
    requested_names: list[str],
    available_tools: list[dict[str, Any]],
) -> bool:
    """模型点名了存在于全量但不在当前子集中的工具 → 应升 full。"""
    if not requested_names:
        return False
    avail = set()
    for t in available_tools or []:
        try:
            avail.add(t["function"]["name"])
        except Exception:
            continue
    for name in requested_names:
        if name and name not in avail:
            return True
    return False


def slim_context_for_lightweight(
    hosts_ctx: str,
    groups_ctx: str,
    host_knowledge_ctx: str,
    terminal_ctx: str,
) -> tuple[str, str, str, str]:
    """轻量对话：清空大块资产上下文。"""
    return (
        "（轻量对话：主机列表已省略）",
        "（轻量对话：分组已省略）",
        "",
        "（轻量对话：终端缓冲已省略；需要时请直接说明运维任务）",
    )


def build_lightweight_system_prompt(
    *,
    product_display: str,
    session_id: int,
    model_runtime_ctx: str,
    output_lang_block: str,
    system_prompt_head: str = "",
) -> str:
    """轻量对话专用短 system，避免把 70K+ 运维上下文塞进「在吗」。"""
    head = (system_prompt_head or "").strip()
    if len(head) > 1200:
        head = head[:1200] + "\n…（系统提示词已截断，完整运维上下文将在具体任务时注入）"
    return f"""{head}

你是 {product_display} 的 AI 运维助手「毛竹」。当前用户消息为简短寒暄/确认。
请用一两句自然回复即可；**不要**主动罗列主机、终端或工具；用户提出具体运维任务后再执行。
当前会话 ID：{session_id}

{output_lang_block}
{model_runtime_ctx}
""".strip()


def message_needs_html_artifact(user_message: str) -> bool:
    q = (user_message or "").lower()
    keys = (
        "html", "报表", "报告", "artifact", "页面", "可视化", "echarts",
        "天气页", "看板", "dashboard", "create_chat_artifact",
    )
    return any(k in q for k in keys)


def message_needs_ssh_terminal_rules(user_message: str) -> bool:
    tier = resolve_tools_tier(user_message or "", lightweight=False)
    if tier == "full" or "terminal" in tier.split("+"):
        return True
    if _HTTP_HINT_RE.search(user_message or "") and re.search(
        r"(scp|主机|服务器|上传到|下载到)", user_message or "", re.I
    ):
        return True
    return False
