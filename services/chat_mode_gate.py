"""会话聊天模式门禁：qa / strict / normal。"""
from __future__ import annotations

import json
import re
from typing import Any

CHAT_MODES = frozenset({"qa", "strict", "normal"})

# 问答模式：禁止执行（改变远端/本机状态）
QA_BLOCKED_TOOLS: frozenset[str] = frozenset({
    "ssh_execute",
    "send_to_terminal",
    "terminal_send_and_read",
    "send_service_password",
    "ssh_channel_create",
    "ssh_channel_send",
    "ssh_channel_close",
    "ssh_channel_close_batch",
    "connect_terminal",
    "create_console",
    "close_console",
    "close_local_console",
    "create_local_console",
    "scp_push",
    "scp_pull",
    "relay_file_between_hosts",
    "transfer_file_between_hosts",
    "git_clone_on_host",
    "batch_create",
    "batch_cancel",
    "batch_retry",
    "fs_write_file",
    "fs_write_binary",
    "fs_delete",
    "fs_mkdir",
    "fs_copy",
    "fs_pack_tgz",
    "fs_unpack_tgz",
    "fs_truncate",
    "local_exec",
    "local_run_script",
    "local_fs_write",
    "local_fs_write_file",
    "local_fs_write_binary",
    "local_fs_delete",
    "local_fs_mkdir",
    "local_fs_rename",
    "local_fs_truncate",
    "local_chat_write_file",
    "local_chat_write_binary",
    "process_start",
    "process_stdin_write",
    "process_stdin_close",
    "process_terminate",
    "create_host",
    "update_host",
    "delete_host",
    "create_group",
    "update_group",
    "delete_group",
    "add_hosts_to_group",
    "remove_host_from_group",
    "create_credential",
    "update_credential",
    "delete_credential",
    "create_maintenance",
    "update_maintenance",
    "delete_maintenance",
    "share_host",
    "revoke_host_share",
    "create_host_tag",
    "update_host_tag",
    "delete_host_tag",
    "set_host_tags",
    "update_host_knowledge",
    "append_host_knowledge",
    "update_host_prompt",
    "append_host_prompt",
    "http_upload",
    "delegate_to_cli_agent",
    "delegate_chain",
    "delegate_to_edgeops_ai",
    "delegate_sub_tasks_batch",
    "run_workflow_template",
    "triggered_task_create",
    "triggered_task_update",
    "triggered_task_delete",
    "triggered_task_trigger",
    "scheduled_task_create",
    "scheduled_task_update",
    "scheduled_task_delete",
    "scheduled_task_run_now",
})

# 严格模式：创建/打开终端与通道 —— 不弹确认（便于连上后再对真正执行命令确认）
STRICT_CONFIRM_EXEMPT_TOOLS: frozenset[str] = frozenset({
    "connect_terminal",
    "create_console",
    "create_local_console",
    "ssh_channel_create",
})

# 严格模式：执行前需确认的写类工具（默认含问答禁止集，再减去终端创建/打开豁免）
STRICT_CONFIRM_TOOLS: frozenset[str] = frozenset(
    (set(QA_BLOCKED_TOOLS) - set(STRICT_CONFIRM_EXEMPT_TOOLS))
    | {
        "create_chat_artifact",
        "update_chat_artifact",
        "write_user_skill_file",
        "save_user_skill",
        "delete_user_skill",
        "http_download",
        "http_download_merge",
        "http_request",
    }
)

_WS_RE = re.compile(r"\s+")


def normalize_chat_mode(raw: str | None) -> str:
    m = (raw or "normal").strip().lower()
    if m in ("qa", "ask", "readonly", "read_only"):
        return "qa"
    if m in ("strict", "confirm", "safe"):
        return "strict"
    return "normal"


def is_qa_blocked(tool_name: str) -> bool:
    name = (tool_name or "").strip()
    if name in QA_BLOCKED_TOOLS:
        return True
    if name.startswith("ssh_channel_") and name not in (
        "ssh_channel_list",
        "ssh_channel_info",
        "ssh_channel_get_status",
        "ssh_channel_read_lines",
        "ssh_channel_read_length",
        "ssh_channel_has_new",
        "ssh_channel_dump_output",
    ):
        return True
    return False


def needs_strict_confirm(tool_name: str) -> bool:
    name = (tool_name or "").strip()
    if not name or name in STRICT_CONFIRM_EXEMPT_TOOLS:
        return False
    if name in STRICT_CONFIRM_TOOLS:
        return True
    # ssh_channel_create 已豁免；send/close 等仍需确认
    if name.startswith("ssh_channel_") and name not in (
        "ssh_channel_create",
        "ssh_channel_list",
        "ssh_channel_info",
        "ssh_channel_get_status",
        "ssh_channel_read_lines",
        "ssh_channel_read_length",
        "ssh_channel_has_new",
        "ssh_channel_dump_output",
    ):
        return True
    if name.startswith("user_mcp_"):
        return True
    return False


def normalize_command_text(text: str | None) -> str:
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    s = _WS_RE.sub(" ", s)
    return s


def extract_command_payload(tool_name: str, args: dict | None) -> tuple[str, str]:
    """返回 (target_key, command_text)。"""
    a = args or {}
    name = (tool_name or "").strip()
    target_parts: list[str] = []
    for k in ("host_id", "channel_id", "slot"):
        if a.get(k) is not None:
            target_parts.append(f"{k}={a.get(k)}")
    target = ",".join(target_parts) if target_parts else "-"
    cmd = ""
    if name in ("send_to_terminal", "terminal_send_and_read", "ssh_channel_send"):
        cmd = str(a.get("text") or a.get("command") or "")
    elif name == "ssh_execute":
        cmd = str(a.get("command") or "")
    elif name in ("scp_push", "scp_pull"):
        cmd = json.dumps(
            {k: a.get(k) for k in ("local_path", "remote_path", "host_id", "content") if k in a},
            ensure_ascii=False,
            sort_keys=True,
        )
    else:
        try:
            cmd = json.dumps(a, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            cmd = str(a)
    return target, normalize_command_text(cmd)


def strict_allow_cache_key(tool_name: str, args: dict | None = None) -> str:
    """
    「总是」缓存键：当前会话内按**工具函数名**放行（同工具后续不再弹确认）。
    args 保留参数仅为兼容旧调用签名，不参与键计算。
    """
    _ = args
    return (tool_name or "").strip()


def strict_allow_cache_key_legacy(tool_name: str, args: dict | None) -> str:
    """历史键：tool|target|cmd（精确命令）；读缓存时仍识别，新写入只用工具名。"""
    target, cmd = extract_command_payload(tool_name, args)
    return f"{(tool_name or '').strip()}|{target}|{cmd}"


def parse_strict_allow_cache(raw: str | None) -> list[str]:
    s = (raw or "").strip()
    if not s:
        return []
    try:
        data = json.loads(s)
        if isinstance(data, list):
            return [str(x) for x in data if x]
        if isinstance(data, dict) and isinstance(data.get("keys"), list):
            return [str(x) for x in data["keys"] if x]
    except Exception:
        pass
    return []


def dump_strict_allow_cache(keys: list[str]) -> str:
    # 去重保序，上限 200
    seen: list[str] = []
    for k in keys:
        if k and k not in seen:
            seen.append(k)
        if len(seen) >= 200:
            break
    return json.dumps({"keys": seen}, ensure_ascii=False)


def is_strict_allow_cached(
    cache_raw: str | None,
    tool_name: str,
    args: dict | None = None,
    *,
    allow_glob: bool = False,
) -> bool:
    """
    会话「总是」匹配：
    - 优先：工具名整键命中（新语义）；
    - 兼容：历史 tool|target|cmd；allow_glob 时命令段可 glob/子串。
    """
    import fnmatch

    name = (tool_name or "").strip()
    if not name:
        return False
    keys = parse_strict_allow_cache(cache_raw)
    if name in keys:
        return True
    # 历史精确键 tool|target|cmd：同工具任一命中 → 视为该函数已「总是」
    prefix = name + "|"
    if any(k.startswith(prefix) for k in keys):
        return True
    if not allow_glob:
        return False
    # 可选 glob：仅在策略开启时对历史键的命令段做通配
    legacy = strict_allow_cache_key_legacy(tool_name, args)
    parts = legacy.split("|", 2)
    if len(parts) < 3:
        return False
    t_name, t_target, t_cmd = parts[0], parts[1], parts[2]
    for k in keys:
        kp = k.split("|", 2)
        if len(kp) < 3:
            continue
        if kp[0] != t_name or kp[1] != t_target:
            continue
        pat = kp[2]
        if pat == t_cmd:
            return True
        if "*" in pat or "?" in pat:
            if fnmatch.fnmatch(t_cmd, pat):
                return True
        if pat and pat in t_cmd:
            return True
    return False


def build_strict_confirm_body(
    tool_name: str,
    args: dict | None,
    *,
    assistant_note: str = "",
) -> str:
    """严格确认卡正文：命令内容 + 原因/意图（纯文本，供 textContent 展示）。"""
    name = (tool_name or "").strip() or "?"
    target, cmd = extract_command_payload(name, args)
    reason = (assistant_note or "").strip()
    cmd_show = (cmd or "").strip() or "（无命令正文）"
    if len(cmd_show) > 1200:
        cmd_show = cmd_show[:1200] + "…"
    lines = [
        f"工具: {name}",
        f"目标: {target}",
        "命令:",
        cmd_show,
    ]
    if reason:
        if len(reason) > 800:
            reason = reason[:800] + "…"
        lines.extend(["", "原因/意图:", reason])
    return "\n".join(lines)


# 问答模式：可把「命令/脚本正文」做成可复制卡片的工具（不暴露其它 tool 参数）
QA_COMMAND_EXPORT_TOOLS: frozenset[str] = frozenset({
    "ssh_execute",
    "send_to_terminal",
    "terminal_send_and_read",
    "ssh_channel_send",
    "local_exec",
    "local_run_script",
})


def build_command_intent(tool_name: str, args: dict | None, assistant_note: str = "") -> str:
    note = (assistant_note or "").strip()
    if note:
        return note[:800]
    name = (tool_name or "").strip()
    if name not in QA_COMMAND_EXPORT_TOOLS:
        return f"需要调用工具 `{name}`，但当前为问答模式，无法执行具体动作。"
    target, cmd = extract_command_payload(tool_name, args)
    preview = cmd[:400] + ("…" if len(cmd) > 400 else "")
    return f"推荐在目标环境执行（目标 {target}）：\n{preview or '（无命令正文）'}"


def qa_redacted_args_for_ui(tool_name: str, args: dict | None) -> dict[str, Any]:
    """问答模式拦截写类工具时，前端/轨迹不展示调用参数。"""
    name = (tool_name or "").strip()
    if not is_qa_blocked(name):
        return args if isinstance(args, dict) else {}
    return {
        "_qa_redacted": True,
        "note": "问答模式无法执行该工具，调用参数已隐藏",
    }


def qa_blocked_tool_result(
    tool_name: str,
    args: dict | None,
    *,
    assistant_note: str = "",
) -> dict[str, Any]:
    name = (tool_name or "").strip()
    target, cmd = extract_command_payload(tool_name, args)
    intent = build_command_intent(tool_name, args, assistant_note)
    is_cmd = name in QA_COMMAND_EXPORT_TOOLS and bool((cmd or "").strip())
    # 非命令类工具：绝不把 args JSON 塞进 suggested_command / 复制卡
    suggested = (cmd or "").strip() if is_cmd else ""
    base_err = (
        "当前为问答模式：只能基于已有信息做分析与执行推荐，不能完成具体动作。"
        "请向用户说明这一点；若其要实现功能，请在回复中直接给出可复制的 bash / Python 等脚本或命令正文，"
        "并提示可切换到「普通」或「严格」模式后再由系统代为执行。"
        "不要向用户复述或展示 tool call 参数。"
    )
    out: dict[str, Any] = {
        "success": False,
        "mode": "qa",
        "error": base_err,
        "blocked_tool": name,
        "suggested_command": suggested,
        "intent": intent,
        "target": target if is_cmd else "-",
        "copy_card": bool(is_cmd and suggested),
        "hide_tool_args": True,
        "assistant_guidance": (
            "用自然语言回复用户：说明当前是问答模式、无法执行；"
            + (
                "可附上推荐命令/脚本（勿再调用写类工具）。"
                if is_cmd
                else f"说明「{name}」这类操作需要调用工具，但当前模式无法执行；可给出等价的手工步骤或脚本，勿展示工具参数。"
            )
        ),
    }
    if is_cmd and suggested:
        out["ui_action"] = {
            "type": "pending_command",
            "tool": name,
            "command": suggested,
            "intent": intent,
            "target": target,
            "mode": "qa",
        }
        out["error"] = (
            "当前为问答模式：禁止代为执行。已提取推荐命令供用户复制；"
            "请在正文中说明问答模式限制，并给出命令用途说明（勿展示 tool 参数）。"
        )
    return out


def chat_mode_system_section(mode: str) -> str:
    m = normalize_chat_mode(mode)
    if m == "qa":
        return (
            "\n\n**【会话聊天模式 · 问答模式】**\n"
            "- 你只能做**分析与执行推荐**，**禁止主动执行任何命令**（含向终端发送按键/文本）。\n"
            "- **允许只读**：list_hosts、get_host_detail、list_terminals、get_terminal_status、"
            "**get_terminal_buffer**、ssh_channel_list / ssh_channel_read_lines / ssh_channel_info 等，"
            "用于检查用户已手工打开的控制台输出并据此分析。\n"
            "- **禁止写操作**：ssh_execute、send_to_terminal、terminal_send_and_read、"
            "ssh_channel_create / ssh_channel_send、connect_terminal、create_console、scp、写文件、"
            "建改删主机/凭证、本地执行等。用户应自行打开终端并粘贴执行。\n"
            "- 当用户需要操作时：\n"
            "  1) 说明当前为问答模式，不能代为执行；\n"
            "  2) 用 Markdown **围栏代码块**给出 bash / Python 等（界面会提供一键复制）；\n"
            "  3) 可提示用户：打开控制台 → 复制命令 → 粘贴执行；你再只读缓冲区核对结果；\n"
            "  4) 勿发起写类 tool call，勿展示 tool 参数 JSON。\n"
            "- 若用户要系统代为执行，请提示切换到可代为执行的聊天模式（非问答）。\n"
        )
    if m == "strict":
        # 严格确认完全在平台门禁完成：不对模型注入任何「严格模式」说明，避免其编造确认/假执行
        return chat_mode_system_section("normal")
    return (
        "\n\n**【会话聊天模式 · 普通模式】**：沿用默认执行策略（含用户 auto_approve / ask_user_choice 约定）。\n"
    )


def suggest_command_from_tool(tool_name: str, args: dict | None) -> str:
    _, cmd = extract_command_payload(tool_name, args)
    return cmd
