"""Agent 交互性能优化：弱网策略、上下文折叠、工具并行、system 分层。"""
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
    "get_current_time",
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


def resolve_weak_network_mode(settings: dict | None = None) -> bool:
    if getattr(config, "AI_WEAK_NETWORK_MODE", False):
        return True
    if not settings:
        return False
    raw = (settings.get("ai_weak_network") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def effective_llm_timeout_retries(weak_network: bool) -> int:
    if weak_network:
        return max(0, int(getattr(config, "AI_CHAT_LLM_TIMEOUT_RETRIES_WEAK", 1)))
    return max(0, int(getattr(config, "AI_CHAT_LLM_TIMEOUT_RETRIES", 3)))


def should_skip_assistant_ai(weak_network: bool, assistant_enabled: bool) -> bool:
    if not assistant_enabled:
        return True
    if weak_network and getattr(config, "AI_WEAK_NETWORK_SKIP_ASSISTANT", True):
        return True
    return False


def should_enrich_tool_images(weak_network: bool) -> bool:
    if weak_network and getattr(config, "AI_WEAK_NETWORK_SKIP_TOOL_IMAGE_ENRICH", True):
        return False
    return True


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
    strip_after = int(getattr(config, "AGENT_SYSTEM_STRIP_AFTER_STEP", 1))
    if step_index < strip_after:
        return full_system
    text = full_system or ""
    replacements = [
        (
            "## 当前用户控制台最近输出",
            "## 当前用户控制台最近输出\n"
            "（为减少弱网往返，本轮后续步骤已省略滚动缓冲全文；"
            "请用 get_terminal_status / get_terminal_buffer / terminal_send_and_read 按需读取。）\n",
        ),
    ]
    if session_host_id is not None:
        replacements.append((
            "## 主机分组",
            "## 主机分组\n（单主机会话，分组列表已省略以减负。）\n",
        ))
    if weak_network:
        replacements.append((
            "## 当前主机列表",
            "## 当前主机列表\n（弱网模式：主机列表已省略；请用 get_host_detail(host_id=…) 按需查询。）\n",
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
