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
    "get_session_prompt",
    "memory_list",
    "memory_search",
    "memory_read",
    "memory_ensure",
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

# 主机/分组/标签 CRUD：必须进 core。否则「添加服务器到某组」只开 core+terminal 时
# tools 里没有 create_host，模型会误称「没有添加主机能力」（提示词却提到了创建主机）。
HOST_MGMT_TOOL_NAMES: frozenset[str] = frozenset({
    "create_host",
    "update_host",
    "delete_host",
    "create_group",
    "update_group",
    "delete_group",
    "add_hosts_to_group",
    "remove_host_from_group",
    "share_host",
    "revoke_host_share",
    "list_host_shares",
    "list_received_host_shares",
    "create_host_tag",
    "update_host_tag",
    "delete_host_tag",
    "set_host_tags",
    "list_credentials",
    "create_credential",
    "update_credential",
    "delete_credential",
})

# 任意分层（含纯 core）都始终下发：联网检索 / HTTP 客户端 / 高频读写与 SSH，
# 避免「去网上查」「查 GitHub」时模型看不见 search_* / http_request 却去 ssh+curl。
ALWAYS_ON_TOOL_NAMES: frozenset[str] = frozenset({
    # 内置搜索（阿里云 IQS / GitHub）
    "search_web",
    "search_github",
    "list_search_providers",
    # HTTP 客户端（平台侧直连，勿默认改走主机 curl）
    "http_request",
    "http_download",
    "http_upload",
    "http_download_merge",
    # 高频运维基础（减少 ensure_chat_tools 往返）
    "ssh_execute",
    "fs_list",
    "fs_search",
    "fs_read_file",
    "fs_write_file",
    "get_chats_workspace_dir",
    "read_chat_data",
    "list_chat_attachments",
    "read_chat_attachment",
    "list_chat_artifacts",
    "read_chat_artifact_file",
    "create_chat_artifact",
    "update_chat_artifact",
})

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
    "ensure_chat_tools",
    "get_session_prompt",
    "update_session_prompt",
    "memory_ensure",
    "memory_list",
    "memory_search",
    "memory_read",
    "memory_write",
    "memory_rebuild_index",
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
}) | HOST_MGMT_TOOL_NAMES | ALWAYS_ON_TOOL_NAMES

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
    "memory_ensure",
    "memory_list",
    "memory_search",
    "memory_read",
    "memory_write",
    "memory_rebuild_index",
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

# 能力集：工具恢复的单一真相源（keyword 分层仅作优化，正确性靠扩层恢复）
CAPABILITY_CORE = "core"
CAPABILITY_TERMINAL = "terminal"
CAPABILITY_FS = "fs"
CAPABILITY_HTTP = "http"
CAPABILITY_HOST_TRANSFER = "host_transfer"
CAPABILITY_FULL = "full"

CAPABILITY_TOOL_SETS: dict[str, frozenset[str]] = {
    CAPABILITY_CORE: CORE_TOOL_NAMES,
    CAPABILITY_TERMINAL: TERMINAL_TOOL_NAMES,
    CAPABILITY_FS: FS_TOOL_NAMES,
    CAPABILITY_HTTP: HTTP_TOOL_NAMES - HOST_FILE_TRANSFER_TOOL_NAMES,
    CAPABILITY_HOST_TRANSFER: HOST_FILE_TRANSFER_TOOL_NAMES,
}

KNOWN_CAPABILITIES: frozenset[str] = frozenset(CAPABILITY_TOOL_SETS) | {CAPABILITY_FULL}


def _build_tool_capability_index() -> dict[str, str]:
    """tool_name → capability（后写覆盖：transfer 优先于 http 并集中的同名项）。"""
    idx: dict[str, str] = {}
    for cap in (
        CAPABILITY_CORE,
        CAPABILITY_TERMINAL,
        CAPABILITY_FS,
        CAPABILITY_HTTP,
        CAPABILITY_HOST_TRANSFER,
    ):
        for name in CAPABILITY_TOOL_SETS[cap]:
            idx[name] = cap
    # 前缀类（未枚举全名时）
    for name in TERMINAL_TOOL_NAMES:
        idx.setdefault(name, CAPABILITY_TERMINAL)
    return idx


TOOL_CAPABILITY_INDEX: dict[str, str] = _build_tool_capability_index()


def capability_for_tool(name: str) -> str | None:
    """解析工具所属能力集；未知前缀按约定推断。"""
    n = (name or "").strip()
    if not n:
        return None
    if n in TOOL_CAPABILITY_INDEX:
        return TOOL_CAPABILITY_INDEX[n]
    if n.startswith(TERMINAL_TOOL_PREFIXES):
        return CAPABILITY_TERMINAL
    if n.startswith(FS_TOOL_PREFIXES):
        return CAPABILITY_FS
    return None


def catalog_tool_names_from_tools(tools: list[dict[str, Any]] | None) -> set[str]:
    out: set[str] = set()
    for t in tools or []:
        try:
            out.add(t["function"]["name"])
        except Exception:
            continue
    return out


def available_tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    return catalog_tool_names_from_tools(tools)


def resolve_capabilities_for_tools(tool_names: list[str] | None) -> set[str]:
    caps: set[str] = set()
    for name in tool_names or []:
        cap = capability_for_tool(name)
        if cap:
            caps.add(cap)
    return caps


def allow_set_for_capabilities(capabilities: set[str] | frozenset[str] | list[str]) -> frozenset[str] | None:
    """None = full（不过滤）。"""
    caps = {str(c or "").strip().lower() for c in (capabilities or []) if str(c or "").strip()}
    if CAPABILITY_FULL in caps or "all" in caps:
        return None
    names: set[str] = set(CORE_TOOL_NAMES)
    for cap in caps:
        if cap == CAPABILITY_CORE:
            continue
        if cap == "ops":
            names |= TERMINAL_TOOL_NAMES
            names |= HOST_FILE_TRANSFER_TOOL_NAMES
            continue
        toolset = CAPABILITY_TOOL_SETS.get(cap)
        if toolset is not None:
            names |= toolset
        if cap in (CAPABILITY_TERMINAL, CAPABILITY_FS, CAPABILITY_HTTP):
            names |= HOST_FILE_TRANSFER_TOOL_NAMES
    return frozenset(names)


def expand_allow_for_tools(
    needed_names: list[str] | None,
    *,
    current_allow: frozenset[str] | set[str] | None = None,
    catalog_names: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """按能力集最小扩层。

    返回:
      recoverable: bool — 所需工具在 catalog 中且可映射能力（或需 full）
      missing_in_catalog: 全量也没有的名字
      capabilities: 需要并入的能力
      allow: 扩层后的 allow（None=full）
      tier_label: 便于日志/stats
    """
    needed = [str(n).strip() for n in (needed_names or []) if str(n).strip()]
    catalog = set(catalog_names or []) if catalog_names is not None else None
    missing_in_catalog: list[str] = []
    known_needed: list[str] = []
    for n in needed:
        if catalog is not None and n not in catalog:
            # 前缀匹配：catalog 里可能有 ssh_channel_* 等
            if any(c == n or c.startswith(n + "_") for c in catalog):
                known_needed.append(n)
            else:
                missing_in_catalog.append(n)
        else:
            known_needed.append(n)

    if not known_needed and missing_in_catalog:
        return {
            "recoverable": False,
            "missing_in_catalog": missing_in_catalog,
            "capabilities": [],
            "allow": frozenset(current_allow) if current_allow is not None else frozenset(CORE_TOOL_NAMES),
            "tier_label": "unrecoverable",
            "needed": needed,
        }

    caps = resolve_capabilities_for_tools(known_needed)
    unmapped = [n for n in known_needed if capability_for_tool(n) is None]
    if unmapped:
        # 未知工具但在 catalog → 回退 full
        return {
            "recoverable": True,
            "missing_in_catalog": missing_in_catalog,
            "capabilities": [CAPABILITY_FULL],
            "allow": None,
            "tier_label": "full",
            "needed": known_needed,
            "unmapped": unmapped,
        }

    # terminal/fs 任务附带转运
    if caps & {CAPABILITY_TERMINAL, CAPABILITY_FS, CAPABILITY_HTTP}:
        caps.add(CAPABILITY_HOST_TRANSFER)

    allow = allow_set_for_capabilities(caps)
    if current_allow is not None and allow is not None:
        allow = frozenset(set(allow) | set(current_allow))

    # 标签：core+… 或 full
    if allow is None:
        tier_label = "full"
    else:
        parts = ["core"] + sorted(c for c in caps if c != CAPABILITY_CORE)
        tier_label = "+".join(parts) if len(parts) > 1 else "core"

    return {
        "recoverable": True,
        "missing_in_catalog": missing_in_catalog,
        "capabilities": sorted(caps),
        "allow": allow,
        "tier_label": tier_label,
        "needed": known_needed,
    }


_MISSING_TOOL_CONTEXT_RE = re.compile(
    r"(缺少|没有|不可用|无法使用|无法调用|无法完成|不能调用|不能使用|做不到|"
    r"不在|未提供|未装载|未加载|未包含|未启用|不具备|无权|权限不足|"
    r"工具集中没有|工具列表没有|当前工具|可用工具|"
    r"allowed tools|not available|not included|unable to|can't|cannot|"
    r"don't have|do not have|does not have|missing|no access)",
    re.IGNORECASE,
)
_TOOL_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,64}")

# 正文只抱怨「没能力」却不点英文工具名时，按语义推断应扩的能力集
_CAPABILITY_LACK_HINTS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(
            r"(文件传输|传文件|传输文件|上传到主机|从主机下载|远程拷贝|拷贝到|"
            r"scp|sftp|没有.*传输|无法.*传输|不具备.*传输)",
            re.IGNORECASE,
        ),
        (CAPABILITY_HOST_TRANSFER, CAPABILITY_TERMINAL),
    ),
    (
        re.compile(
            r"(远程执行|SSH\b|ssh执行|登录主机|主机终端|发命令|执行命令|"
            r"没有.*终端|无法.*SSH|不具备.*SSH|不能.*ssh)",
            re.IGNORECASE,
        ),
        (CAPABILITY_TERMINAL, CAPABILITY_HOST_TRANSFER),
    ),
    (
        re.compile(
            r"(本地文件|工作区文件|读写文件|写文件|读文件|文件系统|"
            r"没有.*fs_|无法.*文件|不具备.*文件)",
            re.IGNORECASE,
        ),
        (CAPABILITY_FS,),
    ),
    (
        re.compile(
            r"(HTTP\b|下载网址|拉取URL|curl\b|webhook|外网请求)",
            re.IGNORECASE,
        ),
        (CAPABILITY_HTTP,),
    ),
    (
        re.compile(
            r"(批量任务|定时任务|触发任务|MCP\b|委托子|工作流|Agent Skills|skills/)",
            re.IGNORECASE,
        ),
        (CAPABILITY_FULL,),
    ),
)


def infer_capabilities_from_lack_text(content: str) -> list[str]:
    """负向语境下，从业务话术推断应扩层的 capabilities（可不含英文工具名）。"""
    text = content or ""
    if not text.strip() or not _MISSING_TOOL_CONTEXT_RE.search(text):
        return []
    caps: list[str] = []
    seen: set[str] = set()
    for pat, needed in _CAPABILITY_LACK_HINTS:
        if not pat.search(text):
            continue
        for c in needed:
            if c not in seen:
                seen.add(c)
                caps.append(c)
    return caps


def expand_allow_for_capabilities(
    capabilities: list[str] | set[str] | frozenset[str] | None,
    *,
    current_allow: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """按能力名直接扩层（正文只抱怨能力、未点工具名时用）。"""
    caps = {str(c or "").strip().lower() for c in (capabilities or []) if str(c or "").strip()}
    if not caps:
        return {
            "recoverable": False,
            "missing_in_catalog": [],
            "capabilities": [],
            "allow": frozenset(current_allow) if current_allow is not None else frozenset(CORE_TOOL_NAMES),
            "tier_label": "unrecoverable",
            "needed": [],
        }
    if CAPABILITY_FULL in caps or "all" in caps:
        return {
            "recoverable": True,
            "missing_in_catalog": [],
            "capabilities": [CAPABILITY_FULL],
            "allow": None,
            "tier_label": "full",
            "needed": [],
        }
    if caps & {CAPABILITY_TERMINAL, CAPABILITY_FS, CAPABILITY_HTTP}:
        caps.add(CAPABILITY_HOST_TRANSFER)
    allow = allow_set_for_capabilities(caps)
    if current_allow is not None and allow is not None:
        allow = frozenset(set(allow) | set(current_allow))
    if allow is None:
        tier_label = "full"
    else:
        parts = ["core"] + sorted(c for c in caps if c != CAPABILITY_CORE)
        tier_label = "+".join(parts) if len(parts) > 1 else "core"
    return {
        "recoverable": True,
        "missing_in_catalog": [],
        "capabilities": sorted(caps),
        "allow": allow,
        "tier_label": tier_label,
        "needed": sorted(caps),
    }


def detect_missing_tools_from_text(
    content: str,
    *,
    available_names: set[str] | frozenset[str] | None = None,
    catalog_names: set[str] | frozenset[str] | None = None,
) -> list[str]:
    """从助手正文检测「声称缺少的工具名」（工具名 + 负向语境）。"""
    text = content or ""
    if not text.strip() or not _MISSING_TOOL_CONTEXT_RE.search(text):
        return []
    catalog = set(catalog_names or TOOL_CAPABILITY_INDEX.keys())
    if not catalog:
        return []
    avail = set(available_names or [])
    # 优先扫 catalog 中的已知名（长名优先，避免短前缀误伤）
    ordered = sorted(catalog, key=len, reverse=True)
    found: list[str] = []
    lower = text
    for name in ordered:
        if name in avail:
            continue
        if name in lower or f"`{name}`" in lower or f"({name})" in lower:
            found.append(name)
    if found:
        # 去重保序
        seen: list[str] = []
        for n in found:
            if n not in seen:
                seen.append(n)
        return seen
    # 回退：负向句附近的标识符 token
    tokens = _TOOL_NAME_TOKEN_RE.findall(text)
    for tok in tokens:
        if tok in catalog and tok not in avail and tok not in found:
            found.append(tok)
    return found


def plan_tools_recovery_from_assistant_text(
    content: str,
    *,
    available_names: set[str] | frozenset[str] | None = None,
    catalog_names: set[str] | frozenset[str] | None = None,
    current_allow: frozenset[str] | set[str] | None = None,
) -> dict[str, Any] | None:
    """综合「点名缺工具」与「能力语义抱怨」，产出 expand 计划；无需恢复时返回 None。"""
    text = (content or "").strip()
    if not text or not _MISSING_TOOL_CONTEXT_RE.search(text):
        return None
    missing = detect_missing_tools_from_text(
        text,
        available_names=available_names,
        catalog_names=catalog_names,
    )
    if missing:
        plan = expand_allow_for_tools(
            missing,
            current_allow=current_allow,
            catalog_names=catalog_names,
        )
        if plan.get("recoverable") and not plan.get("missing_in_catalog"):
            plan["via"] = "tool_names"
            plan["needed_tools"] = missing
            return plan
        # 点名了但都不在 catalog：再试能力语义
    caps = infer_capabilities_from_lack_text(text)
    if not caps:
        return None
    plan = expand_allow_for_capabilities(caps, current_allow=current_allow)
    if not plan.get("recoverable"):
        return None
    plan["via"] = "capability_hint"
    plan["needed_tools"] = list(caps)
    return plan


def filter_tools_by_allow(
    tools: list[dict[str, Any]],
    allow: frozenset[str] | set[str] | None,
    *,
    tier_label: str = "full",
) -> list[dict[str, Any]]:
    """按 allow 过滤；allow is None 表示 full。"""
    if allow is None:
        return list(tools or [])
    allow_set = frozenset(allow)
    out = []
    for t in tools or []:
        try:
            name = t["function"]["name"]
        except Exception:
            continue
        if _tool_allowed_in_tier(name, allow_set, tier_label):
            out.append(t)
    if not out and tools:
        return list(tools or [])
    return out


_FULL_HINT_RE = re.compile(
    r"(批量|batch_|定时|触发|scheduled|triggered|mcp|skill|凭证库|模型.?profile|"
    r"ai_model_profile|工作流|workflow|委托|delegate_|子任务|用户管理|管理员|"
    r"best_practice|最佳实践.?写|写.?最佳实践)",
    re.IGNORECASE,
)
_TERMINAL_HINT_RE = re.compile(
    r"(终端|控制台|ssh|sudo|命令|执行|通道|channel|连接|登录|重启|安装|部署|"
    r"升级|升到|升版|更新版本|版本更新|打补丁|patch|upgrade|update\b|"
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
    r"github|gitlab|gitee|网页|网上|联网|搜网|开源|搜索引擎|查一下.*网|"
    r"search_web|search_github|"
    r"[a-z0-9][a-z0-9.-]*\.(com|cn|cc|io|net|org|local|dev|xyz|me|top)\b)",
    re.IGNORECASE,
)
_CORE_HINT_RE = re.compile(
    r"(主机|服务器|分组|标签|列表|搜索|查一下|有哪些|详情|提示词|知识库|"
    r"添加|新建|加入|加组|纳管|录入|create_host|add_hosts|"
    r"list_hosts|search_hosts|get_host)",
    re.IGNORECASE,
)
# 虽未写「终端」但明显要上机执行/排查 → 并入 terminal 层
_EXEC_SOFT_RE = re.compile(
    r"(执行|运行|安装|部署|升级|升到|升版|更新版本|打补丁|upgrade|patch|"
    r"重启|登录|连接|终端|控制台|通道|sudo|ssh|命令|"
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
    r"(主机|服务器|终端|控制台|ssh|sudo|scp|上传|下载|部署|安装|升级|升到|升版|"
    r"更新版本|打补丁|upgrade|patch|重启|日志|报错|错误|"
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
    r"(tool.?call|工具调用|自己执行|真实执行|真的去|自己跑|发起调用|假执行|"
    r"调用一下|试试调用|你调用|再调用|没有.*工具|工具集中没有|工具列表没有)",
    re.IGNORECASE,
)


# 短确认句：自身不含运维词，但常接在「升级/执行…」之后；需结合 recent_context 分层
_SHORT_CONFIRM_RE = re.compile(
    r"^(是|是的|对|对的|好|好的|行|可以|确认|同意|嗯|嗯嗯|ok|okay|yes|y|继续|就这样|按这个来)$",
    re.IGNORECASE,
)


def _is_short_confirm_message(user_message: str) -> bool:
    raw = (user_message or "").strip()
    if not raw or len(raw) > 24:
        return False
    text = re.sub(r"[\s\U0001F300-\U0001FAFF]+", "", raw)
    text = text.strip("！!。.~～…、，,")
    return bool(text) and bool(_SHORT_CONFIRM_RE.match(text))


def resolve_tools_tier(
    user_message: str,
    *,
    lightweight: bool | None = None,
    force_full: bool = False,
    session_scope: str | None = None,
    session_host_id: int | None = None,
    recent_context: str | None = None,
) -> str:
    """返回 lightweight / core / terminal / fs / http / ops / full。

    ops = core∪terminal（常见运维默认）；多意图用 '+' 拼接（如 core+terminal+fs）。

    - session_host_id：主机详情 AI 运维会话默认带 terminal（含 scp_*）。
    - recent_context：短确认（「是」「好的」）时并入近期用户话，避免确认轮掉到纯 core。
    """
    if force_full or not getattr(config, "AGENT_TOOL_TIERING", True):
        return "full"
    # 轻量路径已弃用：忽略 lightweight 入参
    msg = user_message or ""
    hint_src = msg
    if recent_context and (_is_short_confirm_message(msg) or len(msg.strip()) <= 8):
        hint_src = f"{recent_context}\n{msg}"
    # 用户催促「真正发起 toolcall」：升 full，避免分层漏掉上一轮所需工具
    if _FORCE_FULL_FOLLOWUP_RE.search(msg) or _FORCE_FULL_FOLLOWUP_RE.search(hint_src):
        return "full"
    scope = (session_scope or "").strip().lower()
    try:
        host_bound = session_host_id is not None and int(session_host_id) > 0
    except (TypeError, ValueError):
        host_bound = False
    if scope in ("local",) and _FS_HINT_RE.search(hint_src):
        # 本机管理会话读文件仍走 fs 层
        pass
    if _FULL_HINT_RE.search(hint_src):
        return "full"
    parts: list[str] = ["core"]
    # 主机详情页 / host|ssh scope：默认具备远程执行与文件转运能力
    if (
        _TERMINAL_HINT_RE.search(hint_src)
        or scope in ("host", "ssh")
        or host_bound
    ):
        parts.append("terminal")
    if _FS_HINT_RE.search(hint_src) or scope == "local":
        parts.append("fs")
    if _HTTP_HINT_RE.search(hint_src):
        parts.append("http")
    # 运维语义：纯列表/搜索可留 core；带执行/排查意味则并入 terminal
    if parts == ["core"] and _OPS_HINT_RE.search(hint_src):
        if _EXEC_SOFT_RE.search(hint_src) or not _CORE_HINT_RE.search(hint_src):
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
    session_host_id: int | None = None,
    recent_context: str | None = None,
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
            session_host_id=session_host_id,
            recent_context=recent_context,
        )
    # 轻量路径已弃用：即使调用方误传 lightweight/tier=lightweight，也不清空 tools
    if tier == "lightweight" or lightweight:
        lightweight = False
        tier = resolve_tools_tier(
            user_message,
            lightweight=False,
            force_full=force_full,
            session_scope=session_scope,
            session_host_id=session_host_id,
            recent_context=recent_context,
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
        "看板", "大屏", "dashboard", "create_chat_artifact",
        "三维", "3d", "three", "three-scene", "物理", "cannon", "立体",
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
