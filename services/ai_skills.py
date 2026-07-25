"""毛竹（Moso）AI Skills — 覆盖本系统所有接口，供 AI 全面控制

主机、分组、凭证、维护历史、Skills、终端、设置等。
execute_tool(name, args, user) 需传入当前 user（权限与 send_to_terminal）。
"""

import asyncio
import ast
import base64
import hashlib
import json
import logging
import math
import re
import secrets
import shlex
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote
from uuid import uuid4
from xml.etree import ElementTree as ET
from database import get_db
from api.auth import hash_api_access_token
from api.hosts import (
    HOST_LIST_OWNER_JOIN,
    HOST_LIST_SELECT_COLS,
    HOST_TREE_SELECT_COLS,
    _attach_user_tags_to_hosts,
    _make_inline_credential_code,
    _resolve_host_auth,
    normalize_host_aliases_in_dict,
    parse_host_aliases_cell,
    serialize_host_aliases_for_db,
)
from services.host_duplicate import (
    find_duplicate_host_for_owner,
    host_duplicate_error_detail,
    normalize_host_address,
    normalize_host_port,
)
from services.user_mail import (
    USER_MAIL_SETUP_HINT_ZH,
    effective_scheduled_task_notify_email_to,
    load_user_mail_config,
    public_mail_config_for_api,
    resolve_user_mail_attachments,
    send_mail_as_user,
    upsert_user_mail_from_patch,
)
from services.site_time import (
    SETTINGS_KEY_SITE_TZ,
    build_server_time_payload,
    get_effective_site_timezone,
    validate_iana_timezone,
)
from api.terminal import (
    send_to_user_terminal,
    get_terminal_buffer_for_user,
    get_terminal_session_state,
    get_terminals_for_user,
    get_terminal_session_meta_for_user,
    add_pending_console_creation,
    close_console as terminal_close_console,
    find_ai_terminal_for_host,
    find_preferred_ai_terminal_for_host,
    format_terminal_tab_label,
    normalize_terminal_scope_id,
    resolve_ai_slot as resolve_ai_slot_for_user,
    terminals_snapshot_for_ai,
    wait_for_terminal_session_ready,
)
from api.filesystem import (
    coerce_fs_relative_path,
    get_user_fs_root,
    resolve_fs_path,
    fs_list_dir_async,
    fs_search_files_async,
    fs_read_file_async,
    fs_write_file_async,
    fs_read_binary_async,
    fs_write_binary_async,
    fs_truncate_async,
    fs_mkdir_async,
    fs_pack_tgz_async,
    fs_unpack_tgz_async,
    fs_delete_async,
    fs_copy_or_move_async,
)
from services.ssh_client import run_ssh_command, sftp_put_content
from services.sftp_transfer import run_sftp_pull_async, run_sftp_push_async
from services.keygen import generate_rsa_key, generate_ecc_key
from services.credential_utils import normalize_private_key_pem
from services.chat_utils import assistant_content_for_summary
from services.aihelp_paths import (
    list_aihelp_md_paths_sync,
    read_aihelp_text_async,
    resolve_aihelp_path,
)
from services.markdown_sections import (
    get_markdown_section,
    list_markdown_sections,
    read_markdown_document,
    replace_markdown_section,
    search_markdown_corpus,
    search_markdown_sections,
)
from services.ssh_channel_manager import SSHChannelManager
from services.ssh_channel_service import (
    channel_session_status_payload,
    close_channel_full,
    close_channels_by_owner,
    create_channel_and_open,
    dump_channel_buffer_to_file,
    format_lines_as_text,
    get_channel_detail,
    get_channel_session_state,
    list_channels_for_user,
    maybe_spill_channel_text,
    reconcile_channel_if_stale,
    resolve_channel_owner,
)
from services.host_capability import (
    run_probe_on_host as _probe_host_capabilities_run,
    format_profile_markdown as _profile_markdown,
    merge_profile_into_prompt as _merge_profile,
    extract_profile_block as _extract_profile_block,
    probed_at_of_block as _profile_probed_at,
)
from services.cli_agent_delegate import (
    AGENT_SPECS as _AGENT_SPECS,
    delegate_to_agent as _delegate_to_agent,
    pick_agent_auto as _pick_agent_auto,
    truncate_middle as _truncate_middle,
    run_chain as _run_delegate_chain,
    HostConnInfo as _HostConnInfoCls,
)
from services.text_abbrev import abbreviate_terminal_buffer
from services.output_wait import (
    clamp_until_wait_seconds,
    normalize_until_contains,
    poll_until_contains,
)
from services.terminal_poll import attach_ssh_channel_wait_fields, resolve_terminal_poll_seconds
from services.workflow_templates import (
    save_template as _save_workflow_template,
    list_templates as _list_workflow_templates,
    get_template as _get_workflow_template,
    mark_run as _mark_workflow_template_run,
    apply_variables as _apply_workflow_variables,
    extract_declared_variables as _workflow_declared_vars,
)
import paramiko
import config
from config import EDGEOPS_SESSION_TITLE_CLIENT_PLACEHOLDERS, EDGEOPS_TEMP_SESSION_PREFIX
from bs4 import BeautifulSoup

try:
    import yaml
except ImportError:  # pragma: no cover - depends on optional runtime dependency
    yaml = None

logger = logging.getLogger("edgeops.ai_skills")

_SKILL_FS_GUARD_ERROR = (
    "Agent Skills 文件禁止用 fs_write_file/fs_mkdir/fs_write_binary/fs_delete（会被归位到 chats/ 日期目录或误操作）。"
    "请改用：save_user_skill（SKILL.md）、write_user_skill_file（reference.md 等附属文件）、"
    "read_user_skill_file、list_user_skill_files、delete_user_skill_file。"
    "正确根路径：web/fs/<用户>/skills/<name>/，不是 chats/.../skills/。"
)


def _skill_fs_path_guard(path: str) -> str | None:
    from services.user_skills_registry import looks_like_agent_skill_fs_path

    if looks_like_agent_skill_fs_path(path):
        return _SKILL_FS_GUARD_ERROR
    return None


def _is_admin(user: dict) -> bool:
    r = (user.get("role") or "").strip().lower()
    return r in ("admin", "manager") or user.get("role") == "管理员"


_TERMINAL_STATUS_KEYS = (
    "connected", "exists", "pending", "session_state", "buffer_idle", "ready_for_input",
    "can_send", "can_send_command", "can_read_buffer", "last_line", "busy_reason",
    "waiting_password", "waiting_interactive", "disconnect_reason", "buffer_chars",
)


def _terminal_status_payload(state: dict | None) -> dict:
    state = state or {}
    return {k: state.get(k) for k in _TERMINAL_STATUS_KEYS}


def _terminal_send_guard_message(state: dict, text: str) -> str | None:
    """仅因未连接/连接中而拒绝 send_to_terminal；buffer_idle/busy 不拦截。"""
    if state.get("pending"):
        return (
            "控制台仍在连接中（session_state=pending）。"
            "请 get_terminal_status 或 get_terminal_buffer(next_poll_in_seconds=2～5) 后再试。"
        )
    if not state.get("connected") or state.get("session_state") == "disconnected":
        msg = (
            f"终端已断开（connected=false，disconnect_reason={state.get('disconnect_reason') or 'unknown'}），"
            "无法 send_to_terminal；断开一般不可恢复，请 connect_terminal/create_console 新建。"
        )
        if state.get("can_read_buffer"):
            msg += " 仍可用 get_terminal_buffer 读取会话缓冲区内最后输出。"
        return msg
    return None


def _terminal_busy_advisory(state: dict) -> str | None:
    """终端判 busy 时的参考说明（不阻止发送）。"""
    if not state or state.get("can_send_command") or not state.get("connected"):
        return None
    ss = state.get("session_state") or "busy"
    reason = state.get("busy_reason") or "running"
    last = (state.get("last_line") or "")[:120]
    tail = f" 末行: {last!r}。" if last else ""
    parts = [
        f"参考：buffer_idle=否（session_state={ss}, busy_reason={reason}）。"
        f"已照常发送；{tail}"
    ]
    if state.get("waiting_password"):
        parts.append("末尾可能有密码提示，发完后请 get_terminal_buffer 确认。")
    elif state.get("waiting_interactive"):
        parts.append("末尾可能有 yes/no 等交互，发完后请 read buffer。")
    else:
        try:
            from services.terminal_state import maybe_false_busy_hint
            fb = maybe_false_busy_hint(state)
            if fb:
                parts.append(fb)
            else:
                parts.append("长任务中可 get_terminal_buffer 轮询；需中断再用 <Ctrl+C>。")
        except Exception:
            parts.append("长任务中可 get_terminal_buffer 轮询。")
    return " ".join(parts)


def _attach_terminal_send_advisory(payload: dict, state: dict) -> None:
    adv = _terminal_busy_advisory(state)
    if adv:
        payload["terminal_advisory"] = adv


def _attach_false_busy_hint(out: dict, state: dict) -> None:
    from services.terminal_state import maybe_false_busy_hint
    hint = maybe_false_busy_hint(state)
    if hint:
        out["false_busy_hint"] = hint


def _attach_channel_false_busy_hint(out: dict, state: dict) -> None:
    from services.terminal_state import maybe_false_busy_hint
    hint = maybe_false_busy_hint(state)
    if hint:
        out["channel_advisory"] = (
            hint.replace("get_terminal_buffer", "ssh_channel_read_lines")
            .replace("send_to_terminal", "ssh_channel_send")
        )


def _channel_busy_advisory(state: dict) -> str | None:
    if not state or state.get("can_send_command") or not state.get("connected"):
        return None
    ss = state.get("session_state") or "busy"
    reason = state.get("busy_reason") or "running"
    last = (state.get("last_line") or "")[:120]
    tail = f" 末行: {last!r}。" if last else ""
    parts = [
        f"参考：buffer_idle=否（session_state={ss}, busy_reason={reason}）。"
        f"已照常发送；{tail}"
    ]
    if state.get("waiting_password"):
        parts.append("末尾可能有密码提示，发完后请 ssh_channel_read_lines 确认。")
    elif state.get("waiting_interactive"):
        parts.append("末尾可能有 yes/no 等交互，发完后请 read_lines。")
    else:
        try:
            from services.terminal_state import maybe_false_busy_hint
            fb = maybe_false_busy_hint(state)
        except Exception:
            fb = None
        if fb:
            parts.append(fb.replace("get_terminal_buffer", "ssh_channel_read_lines"))
        else:
            parts.append(
                "长任务中可 ssh_channel_read_lines(until_contains=标记或 password, wait_seconds=超时) "
                "或 has_new(wait_seconds=1～30) 减少空转；需中断再用 <Ctrl+C>。"
            )
    return " ".join(parts)


def _attach_channel_send_advisory(payload: dict, state: dict) -> None:
    adv = _channel_busy_advisory(state)
    if adv:
        payload["channel_advisory"] = adv


async def _ssh_channel_status_for_id(db, user: dict, channel_id: int) -> tuple[dict | None, dict | None, str | None]:
    """返回 (channel_row, session_state, error)。"""
    rows = await db.execute_fetchall(
        """SELECT c.id, c.status, h.host_type
           FROM ssh_channels c
           JOIN hosts h ON h.id = c.host_id
           WHERE c.id = ? AND c.user_id = ?""",
        (channel_id, user["id"]),
    )
    if not rows:
        return None, None, "通道不存在"
    row = dict(rows[0])
    await reconcile_channel_if_stale(db, user, channel_id)
    refreshed = await db.execute_fetchall(
        "SELECT status FROM ssh_channels WHERE id = ? AND user_id = ?",
        (channel_id, user["id"]),
    )
    if refreshed:
        row["status"] = refreshed[0][0] or "closed"
    st = get_channel_session_state(
        channel_id,
        db_status=str(row.get("status") or "closed"),
        host_type=(row.get("host_type") or "").strip() or None,
    )
    return row, st, None


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_hosts",
            "description": "列出 SSH 主机；可按分组与标签筛选，也可按关键字快速搜索（名称、IP/域名、端口、描述、用途备注 remark、别名 aliases JSON、系统类型、标签名、或纯数字主机 id）。无关键字时返回全量（仍受权限过滤）。每条主机含所有者 created_by、created_by_username、created_by_display_name。找机时优先传 q 缩小范围。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "可选，仅返回该分组下的主机"},
                    "tag_ids": {"type": "array", "items": {"type": "integer"}, "description": "可选，按当前用户标签 ID 过滤（匹配任一标签）"},
                    "q": {"type": "string", "description": "可选，搜索关键字（与 search 二选一）；不区分大小写，匹配名称、host、端口、描述、remark、aliases、host_type、标签名；纯数字时同时匹配主机 id"},
                    "search": {"type": "string", "description": "同 q"},
                    "limit": {"type": "integer", "description": "有 q/search 时最多返回条数，默认 100，最大 500"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_hosts",
            "description": "高效搜索 SSH 主机。先用 SQL 在名称/IP/端口/描述/用途/别名/标签中预筛，再可选用正则二次过滤，适合快速精确定位目标主机用于后续批量或执行操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键字（必填）"},
                    "group_id": {"type": "integer", "description": "可选，仅搜索该分组内可见主机"},
                    "tag_ids": {"type": "array", "items": {"type": "integer"}, "description": "可选，仅搜索命中这些标签（任一）的主机"},
                    "regex": {"type": "string", "description": "可选，正则表达式；提供后会在 SQL 结果上再次精筛"},
                    "case_sensitive": {"type": "boolean", "description": "正则是否区分大小写，默认 false"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 50，最大 200"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_host_tags",
            "description": "列出当前用户自己的主机标签（id、name、color、host_count）。用于后续按标签筛选主机或批量操作。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_host_tag",
            "description": "创建当前用户自己的主机标签。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "标签名"},
                    "color": {"type": "string", "description": "可选，颜色，格式 #RRGGBB"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_host_tag",
            "description": "更新当前用户自己的标签名称或颜色。",
            "parameters": {
                "type": "object",
                "properties": {
                    "tag_id": {"type": "integer", "description": "标签 ID"},
                    "name": {"type": "string", "description": "可选，新标签名"},
                    "color": {"type": "string", "description": "可选，新颜色，格式 #RRGGBB；空串可清空颜色"},
                },
                "required": ["tag_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_host_tag",
            "description": "删除当前用户自己的标签（会移除该标签在主机上的关联）。",
            "parameters": {
                "type": "object",
                "properties": {"tag_id": {"type": "integer", "description": "标签 ID"}},
                "required": ["tag_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_host_tags",
            "description": "设置当前用户给某台主机打的标签（可多选，整表覆盖）。共享主机也仅影响当前用户自己的标签视图。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "tag_ids": {"type": "array", "items": {"type": "integer"}, "description": "标签 ID 列表；传 [] 清空"},
                },
                "required": ["host_id", "tag_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_host_detail",
            "description": "获取单台主机的详细信息（id、name、host、port、认证方式、描述、aliases 别名列表、remark 用途说明、host_type、host_version、host_shell、host_package_manager、所属分组等）。不包含密码等敏感信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_host_os",
            "description": "检测主机的操作系统类型、版本、Shell（bash/zsh/sh 等）、包管理器（apt/yum/apk 等），并自动写回主机信息，供后续命令与脚本策略使用。无论主机当前是否已有信息，都可再次执行检测；若有变化会更新。用于用户要求「识别主机类型」「检查这台主机是什么系统」或「重新检测类型」时。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "要检测的主机 ID"},
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "probe_host_capabilities",
            "description": (
                "**主机能力画像**：一次 SSH 自检目标主机的 OS / 硬件 / 已安装 CLI 工具"
                "（docker/kubectl/terraform/ansible/云 CLI、cursor-agent/opencode/aider 等 AI CLI、"
                "nmap/nikto/sqlmap/msfconsole 等安全渗透工具、语言运行时、数据库客户端），"
                "结果格式化为 Markdown 写入**主机级提示词**（`ai_host_prompts`）的专属哨兵块内，"
                "用户在哨兵之外手写的规则不会被覆盖。\n\n"
                "**AI 何时调用**：进入一台新主机/分享主机的第一次操作前、用户问「这台机器装了啥」「能做什么」、"
                "用户要你在主机上调用 cursor/opencode/云 CLI/安全工具之前。默认 24 小时内已有缓存则直接返回缓存，"
                "传 `refresh=true` 可强制重新探测。\n\n"
                "**返回**：`{success, cached, probed_at, os, hardware, tools_by_group, profile_markdown, prompt_content_length}`。"
                "AI 可以直接读 `tools_by_group` 的键值（如 `ai_cli.cursor-agent` 的版本号）做决策，"
                "也可让用户通过 `get_host_prompt` 看到可读的画像 Markdown。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "要画像的主机 ID"},
                    "refresh": {
                        "type": "boolean",
                        "description": "强制重新探测；默认 false（24 小时内有缓存则复用）",
                    },
                    "max_age_hours": {
                        "type": "integer",
                        "description": "缓存有效期小时数，默认 24；refresh=true 时忽略",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "SSH 总超时秒数，默认 40，范围 10–120",
                    },
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_host_capabilities",
            "description": (
                "读取指定主机已缓存的**能力画像**（结构化结果）。若尚未画像过，返回 `profile_exists=false`，"
                "此时应先调用 `probe_host_capabilities`。比 `get_host_prompt` 更适合程序消费——"
                "返回解析后的 JSON（`os / hardware / tools_by_group / tools`），不用 AI 自行从 Markdown 里抠。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_cli_agent",
            "description": (
                "**把一段任务委派给远端主机上的另一个 AI 代理 CLI**（子 AI），"
                "例如 cursor-agent / opencode / aider / claude / codex / goose / cline / llm。"
                "主 AI 负责「想清楚要做什么、挑谁做、审核结果」；子 AI 负责真正在目标机上读写代码、"
                "调云 CLI、跑渗透、总结等。通过 SSH 一次性非交互调用，收集子 AI 的 stdout / stderr / "
                "退出码；若 `workdir` 是 git 仓库，调用前后自动抓取 HEAD 并产出 **diff 概要**"
                "（增删文件数、名称、前 4000 字 unified diff）。\\n\\n"
                "**调用前必备**：\\n"
                "1) 先用 `get_host_capabilities(host_id)` 或 `probe_host_capabilities` 确认目标 agent 已安装；\\n"
                "2) 明确 `workdir`（通常是用户当前关心的 git 仓库根），否则 aider/cursor-agent 可能无上下文可改；\\n"
                "3) **子 AI 会修改文件**（llm 除外）——执行前必须用 `ask_user_choice` 让用户确认任务描述、"
                "目标 agent、workdir；用户确认后再调。\\n\\n"
                "**参数**：\\n"
                "- `agent`：cursor-agent / opencode / aider / claude / codex / goose / cline / llm / auto（auto 按画像自动挑一个）\\n"
                "- `task`：自然语言任务，会作为子 AI 的 prompt 透传\\n"
                "- `workdir`：子 AI 的工作目录（绝对路径）\\n"
                "- `model`：可选，覆盖子 AI 模型（部分 agent 支持）\\n"
                "- `extra_args`：可选，追加到 CLI 命令末尾的原样参数字符串（比如 `--file src/app.py`）\\n"
                "- `command_template`：可选，完全自定义调用模板（字符串，支持 `{task_q}`/`{extra}` 占位）\\n"
                "- `env`：可选，追加的环境变量（如 `CURSOR_API_KEY`/`OPENAI_API_KEY`），审计日志只记 key 不记 value\\n"
                "- `timeout`：总超时秒，默认 300，范围 10–900\\n"
                "- `max_output_chars`：stdout 截断长度，默认 20000\\n\\n"
                "**返回**：`{success, agent, cmd_used, exit_code, duration_sec, stdout_preview, stdout_truncated, stdout_full_length, stderr_preview, git_diff(files_changed/insertions/deletions/files/diff_preview), message}`。"
                "主 AI 收到结果后**应**：总结子 AI 做了什么、列出改动文件、必要时提示用户复查/回滚。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "要执行的主机 ID"},
                    "agent": {
                        "type": "string",
                        "enum": [
                            "auto", "cursor-agent", "opencode", "aider", "claude",
                            "codex", "goose", "cline", "llm",
                        ],
                        "description": "子 AI 代理名；auto 按画像自动挑选",
                    },
                    "task": {"type": "string", "description": "给子 AI 的任务 prompt（自然语言）"},
                    "workdir": {"type": "string", "description": "子 AI 的工作目录（绝对路径，通常是 git 仓库根）"},
                    "model": {"type": "string", "description": "可选，覆盖子 AI 模型"},
                    "extra_args": {"type": "string", "description": "可选，原样追加到 CLI 末尾的参数字符串"},
                    "command_template": {"type": "string", "description": "可选，完全自定义调用模板，含 `{task_q}` 和 `{extra}` 占位符"},
                    "env": {
                        "type": "object",
                        "description": "可选，追加给子 AI 的环境变量（key=value），审计日志只记 key",
                        "additionalProperties": {"type": "string"},
                    },
                    "output_format": {"type": "string", "description": "部分 agent（cursor-agent/claude）支持的输出格式：text/json/stream-json 等"},
                    "timeout": {"type": "integer", "description": "总超时秒，默认 300，范围 10–900"},
                    "max_output_chars": {"type": "integer", "description": "stdout 截断长度，默认 20000"},
                    "confirmed": {"type": "boolean", "description": "用户已通过 ask_user_choice 确认执行，设为 true 才会真正发起（llm agent 例外）"},
                },
                "required": ["host_id", "agent", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_chain",
            "description": (
                "**多步编排**：一次声明一条由 `delegate`（子 AI 委派） / `ssh`（普通命令） / `sleep`（等待）"
                "三种步骤组成的链，按顺序在同一台主机上跑，每步可根据前一步结果决定是否执行与插值"
                "（`{prev_stdout}` / `{prev_stderr}` / `{prev_exit_code}` / `{prev_cmd}` / `{prev_agent}` / "
                "`{prev_files_changed}` / `{prev_insertions}` / `{prev_deletions}`）。典型场景：\\n"
                "1) **改→测→自愈**：`cursor-agent 改 JWT` → `pytest` → 失败则 `cursor-agent 根据 {prev_stderr} 修复`；\\n"
                "2) **扫描→分析**：`nmap -sV` → `llm 总结 {prev_stdout}`；\\n"
                "3) **画像→验证**：`apt install foo` → `sleep 2s` → `foo --version`。\\n\\n"
                "**相对 delegate_to_cli_agent 的优势**：(a) 一次 tool_call 跑完整个流程，避免多轮往返延迟；"
                "(b) 后台定时任务（task scope）首选——直接把整个工作流写死在任务里；"
                "(c) 失败分支 `when=on_failure` 让自愈流程天然表达。\\n\\n"
                "**安全**：链里任何 `delegate` 步用的是写类 agent 时，整个链要求 `confirmed=true`（通过 `ask_user_choice` 整体确认）；"
                "task scope 下视为已授权。各步 delegate 内部**不再**单独做画像与确认校验——主调用者要为整条链负责。\\n\\n"
                "**跨主机**：每一步可选 `host_id` 字段覆盖默认主机；顶层 `host_id` 为默认。典型流：A 机改代码 → B 机跑测试 → C 机部署。所有涉及的主机都会各自做画像校验、访问控制、凭证解析与审计。"
                "**返回**：`{success, total_steps, executed, failed_at, steps: [{index, name, kind, success, skipped, skip_reason, host_id, host_label, cmd, agent, exit_code, stdout_preview, stderr_preview, git_diff, duration_sec, error}], summary}`。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "默认主机 ID；每一步可用 step.host_id 覆盖"},
                    "steps": {
                        "type": "array",
                        "description": "2–10 步；每步是一个 object",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": ["delegate", "ssh", "sleep"], "description": "步骤类型"},
                                "host_id": {"type": "integer", "description": "可选，本步在哪台主机执行；不传则用顶层 host_id"},
                                "name": {"type": "string", "description": "可选，步骤名（用于日志）"},
                                "when": {"type": "string", "enum": ["always", "on_success", "on_failure"], "description": "何时执行：第 1 步默认 always，之后默认 on_success"},
                                "agent": {"type": "string", "description": "kind=delegate 时必填；同 delegate_to_cli_agent.agent"},
                                "task": {"type": "string", "description": "kind=delegate 时必填；支持 {prev_*} 模板变量"},
                                "workdir": {"type": "string", "description": "工作目录（delegate/ssh 共用，通常是 git 仓库根）"},
                                "model": {"type": "string", "description": "kind=delegate 可选"},
                                "extra_args": {"type": "string", "description": "kind=delegate 可选，支持 {prev_*}"},
                                "command_template": {"type": "string", "description": "kind=delegate 可选，完全自定义调用模板"},
                                "env": {"type": "object", "description": "kind=delegate 可选环境变量"},
                                "output_format": {"type": "string", "description": "kind=delegate 可选"},
                                "command": {"type": "string", "description": "kind=ssh 必填；支持 {prev_*} 模板变量"},
                                "timeout": {"type": "integer", "description": "单步超时秒，delegate 默认 300、ssh 默认 60"},
                                "seconds": {"type": "integer", "description": "kind=sleep 的等待秒数，范围 0–600"},
                                "max_output_chars": {"type": "integer", "description": "单步 stdout 截断长度"},
                            },
                            "required": ["kind"],
                        },
                        "minItems": 1,
                        "maxItems": 10,
                    },
                    "stop_on_failure": {"type": "boolean", "description": "任一步失败则立刻停止；默认 true"},
                    "confirmed": {"type": "boolean", "description": "链里有写类 delegate 步时必须为 true；task scope 下视为已授权"},
                    "task_dir_name": {"type": "string", "description": "可选，任务目录名，高风险链会自动写任务日志"},
                },
                "required": ["host_id", "steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_workflow_template",
            "description": (
                "**保存一条 `delegate_chain` 编排为可复用模板**（持久化到 `ai_workflow_templates` 表）。"
                "典型用法：用户跑完一条复杂的跨机链觉得以后还想再跑时说「把这条链存下来叫 daily-deploy」，"
                "主 AI 应调该技能把刚才的 payload 原样存库。payload 里的字符串字段（task/command/workdir 等）"
                "可以写占位符 `${var}`（注意跟 chain 内部的 `{prev_*}` 区分），"
                "后续 `run_workflow_template` 时通过 `variable_overrides` 传值。"
                "按 `(owner_user_id, name)` 唯一，重名默认拒绝，可传 `overwrite=true` 覆盖。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "模板名，用户视角的标识（如 `deploy-prod`）"},
                    "description": {"type": "string", "description": "简介（用户友好；支持 Markdown）"},
                    "payload": {
                        "type": "object",
                        "description": "完整的 `delegate_chain` 参数字典：`{host_id, steps, stop_on_failure?, ...}`。steps 内可用 `${var}` 占位符",
                    },
                    "tags": {"type": "string", "description": "逗号分隔标签，便于检索"},
                    "visibility": {
                        "type": "string",
                        "enum": ["private", "org"],
                        "description": "private=仅本人可见可跑（默认），org=同实例的其他用户也能查看复用（管理员推荐）",
                    },
                    "overwrite": {"type": "boolean", "description": "同名已存在时是否覆盖，默认 false"},
                    "kind": {"type": "string", "description": "模板类型，默认 `delegate_chain`；为后续扩展预留"},
                },
                "required": ["name", "payload"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_workflow_templates",
            "description": (
                "**列出可用的编排模板**。默认返回当前用户创建的 + visibility=org 的公共模板，按 `updated_at` 倒序。"
                "可用 `query` 在 name/description/tags 里模糊搜索。不返回 payload 详情；需要详情用 `run_workflow_template(dry_run=true)` 预览。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，空串=列全部"},
                    "include_org": {"type": "boolean", "description": "是否包含其他用户的 org 可见模板，默认 true"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 50，范围 1–200"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_workflow_template",
            "description": (
                "**跑一条已保存的编排模板**。内部会取出 payload，用 `variable_overrides` 对 `${var}` 做字符串替换，"
                "然后**以该 payload 内联执行 `delegate_chain`**（不是再次调用 delegate_chain 工具，而是复用其后端引擎），"
                "因此所有安全门禁（画像校验、写类 delegate 的 `confirmed`、访问控制）全部生效：\\n"
                "- `dry_run=true` 仅返回 `{payload, declared_variables, steps_preview}`，供主 AI 在 `ask_user_choice` 里展示给用户看\\n"
                "- `confirmed=true` 才会真实执行；含写类 delegate 必须传\\n"
                "- 运行成功自动 `run_count+1, last_run_at=now`"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template_id": {"type": "integer", "description": "要跑的模板 ID"},
                    "variable_overrides": {
                        "type": "object",
                        "description": "`${var}` 占位符的替换值（字符串 dict）。用 `list_workflow_templates` + `dry_run=true` 可看出模板声明了哪些变量",
                        "additionalProperties": {"type": "string"},
                    },
                    "host_id_override": {"type": "integer", "description": "可选，覆盖 payload 顶层默认主机"},
                    "dry_run": {"type": "boolean", "description": "只预览不执行；默认 false"},
                    "confirmed": {"type": "boolean", "description": "含写类 delegate 的模板真实执行前须为 true；task scope 视为已授权"},
                    "stop_on_failure": {"type": "boolean", "description": "覆盖 payload 里的 stop_on_failure"},
                },
                "required": ["template_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_edgeops_ai",
            "description": (
                "**内部 AI 递归**：起一个 毛竹（Moso）子 AI 对话（独立 system_prompt + 工具白名单 + 短生命周期），"
                "跑完把最终 Markdown 回复返回给主 AI。和 `delegate_to_cli_agent` 不一样——那个是把任务甩给"
                "远端主机上的 cursor-agent / opencode 等 CLI；这个**不出本机**，走的是主 AI 自己的 LLM 账号，"
                "只是换了一套 system_prompt 和工具集。\\n\\n"
                "典型用法：\\n"
                f"- 「把这些日志整理成运维报告」：起一个只有读工具的子 AI，system_prompt 写清『你是毛竹（Moso）的报告撰写员』\\n"
                "- 「让另一个 AI 审查你刚才写的脚本」：起一个 reviewer 子 AI\\n"
                "- 「聚合 3 个分析任务并给结论」：用 `delegate_sub_tasks_batch` 一次并发多个子 AI，或串行多次 `delegate_to_edgeops_ai`\\n\\n"
                "进度：执行期间通过 SSE 推送 `sub_ai_step` / `sub_ai_tool` / `sub_ai_done` 事件到 CoT 面板。\\n"
                "安全：**递归深度硬限制为 2**（主→子→孙即拒），子 AI 不允许再调 `delegate_to_edgeops_ai` 或 `delegate_sub_tasks_batch`；"
                "工具白名单必须显式声明，默认不给任何工具（纯推理）；建议只给读类工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "给子 AI 的用户消息（明确的任务描述 / 输入材料）"},
                    "system_prompt": {
                        "type": "string",
                        "description": "子 AI 的 system prompt。主 AI 要清晰写明子 AI 的身份、输出格式、边界（如只用中文、输出 Markdown、禁止闲聊）",
                    },
                    "allowed_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "子 AI 可调用的工具名白名单（不传或空数组=不给任何工具，纯推理）。强烈建议只给读类工具，如 `list_hosts` / `get_host_prompt` / `get_recent_tool_result`",
                    },
                    "max_steps": {"type": "integer", "description": "子 AI 最多 LLM 轮次，默认 10，上限 30"},
                    "max_depth": {"type": "integer", "description": "递归深度上限，默认 2，上限 5"},
                    "timeout_sec": {"type": "integer", "description": "子 AI 墙钟超时秒数，默认 120，上限 600"},
                    "context_hint": {"type": "string", "description": "附加贴到子 AI system 末尾的上下文片段（例如先前的关键工具输出摘要）"},
                },
                "required": ["task", "system_prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_sub_tasks_batch",
            "description": (
                "**内部子 AI 批量并发**：一次发起 1–8 个独立的 `delegate_to_edgeops_ai` 子任务，"
                "受 `max_parallel`（默认 3，上限 5）限制并发度，全部完成后返回按 index 排序的聚合结果。"
                "适合 Map-Reduce：主 AI 把大日志/多主机/多维度分析拆成 N 个子任务并行跑，最后汇总 `final_text`。"
                "\\n\\n"
                "每个子任务可单独指定 `task` / `system_prompt` / `allowed_tools` / `context_hint`；"
                "也可在顶层设 `shared_system_prompt` 与 `default_allowed_tools` 作为默认值。"
                "大数据请用 spill 引用 + 子任务内 `read_chat_data`，不要把全文塞进 `context_hint`。"
                "\\n\\n"
                "进度：SSE 推送 `sub_ai_batch_start` / `sub_ai_step` / `sub_ai_tool` / `sub_ai_batch_end`。"
                "子 AI 内禁止再调本工具或 `delegate_to_edgeops_ai`。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "子任务列表（1–8 项）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "可选，子任务标识（前端进度与结果索引）"},
                                "task": {"type": "string", "description": "给该子 AI 的用户消息"},
                                "system_prompt": {"type": "string", "description": "可选；省略则用 shared_system_prompt"},
                                "allowed_tools": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "可选；省略则用 default_allowed_tools",
                                },
                                "context_hint": {"type": "string", "description": "可选，贴到该子 AI system 末尾的上下文摘要"},
                                "max_steps": {"type": "integer", "description": "可选，覆盖顶层 max_steps"},
                                "timeout_sec": {"type": "integer", "description": "可选，覆盖顶层 timeout_sec"},
                            },
                            "required": ["task"],
                        },
                    },
                    "shared_system_prompt": {
                        "type": "string",
                        "description": "所有子任务共用的 system prompt（单任务未写 system_prompt 时使用）",
                    },
                    "default_allowed_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "默认工具白名单；建议 read_chat_data / get_recent_tool_result 等只读工具",
                    },
                    "max_parallel": {"type": "integer", "description": "最大并发子 AI 数，默认 3，上限 5"},
                    "max_steps": {"type": "integer", "description": "默认每子任务 LLM 轮次上限，默认 10，上限 30"},
                    "max_depth": {"type": "integer", "description": "递归深度上限，默认 2"},
                    "timeout_sec": {"type": "integer", "description": "默认每子任务墙钟超时秒数，默认 120，上限 600"},
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_execute",
            "description": (
                "在指定主机上**执行一条** SSH 命令（一次 tool_call = 一条 command）。返回 stdout、stderr 和退出码。"
                "**多条顺序命令、安装向导、需多次输入（sudo 密码、yes/no）** 不要用本工具连发多次，应改用 **ssh_channel_***。"
                "排障时以 stdout/stderr **末尾**为准。"
                "**长且无交互的任务**可用 detach：detach=true 时后台运行，输出写入 log_path，之后 poll_log=true 轮询尾部。"
                "AI 助手/主机详情/集成/MCP 均可用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "command": {"type": "string", "description": "要执行的 shell 命令（detach 或 poll_log 时 poll 模式可省略）"},
                    "timeout": {"type": "integer", "description": "超时秒数；同步命令默认 45（5–300）；detach 启动默认 20"},
                    "task_dir_name": {"type": "string", "description": "可选，任务目录名；危险命令执行后自动追加任务日志"},
                    "detach": {
                        "type": "boolean",
                        "description": "为 true 时后台运行 command，stdout/stderr 写入 log_path，立即返回 pid/log_path",
                    },
                    "poll_log": {
                        "type": "boolean",
                        "description": "为 true 时读取 log_path 末尾并报告 job_running/job_finished（需配合先前 detach 的 log_path）",
                    },
                    "log_path": {
                        "type": "string",
                        "description": "远端日志路径；detach 时可省略（自动生成）；poll_log 时必填",
                    },
                    "tail_lines": {
                        "type": "integer",
                        "description": "poll_log 时返回日志尾部行数，默认 40，范围 10–200",
                    },
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_to_terminal",
            "description": (
                "向指定 **Web 界面 AI 控制台**注入输入（用户可在 tab 中实时看到）。"
                "仅可操作 AI 创建的 SSH 控制台。**仅 connected=false / pending 时服务端会拒绝发送**；"
                "buffer_idle、session_state、can_send_command **仅为参考**（见 terminal_advisory），不拦截命令。"
                "busy 时仍可直接 send；发完后用 get_terminal_buffer 看结果，长任务可轮询，需中断用 <Ctrl+C>。"
                "同一 host 可有多个 slot；并行任务可 create_console 新开。"
                "**多条顺序/交互任务**且用户不必看界面时，优先 ssh_channel_*，不必强开 Web tab。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "命令或输入；可含 <Ctrl+C>、<Enter>（空回车探测假 busy）；勿与 sudo 同次发密码"},
                    "slot": {"type": "integer", "description": "控制台槽位（0、1、2…）；不传则按 host_id 或默认 AI slot"},
                    "host_id": {"type": "integer", "description": "可选：按主机 ID 自动选择 AI 控制台 slot"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_send_and_read",
            "description": (
                "向 **Web AI 控制台**发送命令并**在同一工具调用内等待后读取缓冲**（减少弱网下的 LLM 往返）。"
                "等价于 send_to_terminal + 等待 wait_seconds + get_terminal_buffer。"
                "适合 sudo 交互、短命令、安装脚本单步执行。**密码仍用 send_service_password**，勿在 text 里发明文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要发送的命令或输入"},
                    "slot": {"type": "integer", "description": "控制台槽位"},
                    "host_id": {"type": "integer", "description": "按主机 ID 选择 slot"},
                    "wait_seconds": {"type": "integer", "description": "发送后等待秒数再读缓冲，默认 1，范围 0～30"},
                    "max_lines": {"type": "integer", "description": "读取缓冲最大行数，默认 40"},
                    "tail_only": {"type": "boolean", "description": "默认 true，仅返回末尾行"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "connect_terminal",
            "description": (
                "在 **Web 界面**打开/连接指定主机的 **SSH 控制台 tab**（用户能在「控制台」里看到）。"
                "用户说「打开终端」「打开某主机」「连上 XX」且**未提「通道」**时用本工具。"
                "若该 host 已有 AI 控制台，优先切到 buffer_idle=是的空闲 slot；"
                "仅当尚无该 host 的 AI 控制台时首连（预分配 slot 并等待就绪）。"
                "**禁止**在用户要「SSH 通道/打开通道」时用本工具——那种情况用 ssh_channel_create。"
                "用户要求「再开一个/新开终端」或现有终端被长期任务占用时，请用 create_console 而非本工具。"
                "connect_terminal 后若 get_terminal_buffer 暂不可用，请 list_terminals 并带 next_poll_in_seconds 重试，勿立刻 create_console。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "要连接的主机 ID（与主机列表中的 id 一致）"},
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_terminals",
            "description": "查询当前聊天区域内 **Web SSH 控制台** tab 列表（含 tab_label、connected、session_state、buffer_idle、can_send）。用户说「列出终端/控制台」时用本工具；**不是** ssh_channel_list。只返回 AI 创建的控制台。多控制台/多主机场景下**必须先调用**再 send_to_terminal。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_console",
            "description": (
                "在 AI 助手页**新建并连接**一个 **Web 控制台 tab**（界面「控制台」里可见）。"
                "用户说「再开一个终端/新开控制台」时用本工具；**不是** ssh_channel。"
                "现有终端被长期任务占用、要在新 session 执行命令、或用户明确要求再开一个终端时，必须调用本工具。"
                "不要以「每台主机只能一个控制台」为由拒绝；list_terminals 后仍无 buffer_idle=是的 slot 时也应 create_console。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "新控制台要连接的主机 ID"},
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_console",
            "description": "关闭由 AI 创建的控制台。仅可关闭 created_by 为 ai 的控制台，关闭后前端会移除该控制台 tab。slot 为要关闭的控制台槽位（0、1、2…）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "slot": {"type": "integer", "description": "要关闭的控制台槽位（0、1、2、3…）"},
                },
                "required": ["slot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_host_knowledge",
            "description": "获取指定主机的 AI 知识（该主机上你或用户记录过的账户、密码、数据路径、配置位置等）。以 host_id 为维度，返回当前用户在该主机下的知识内容。用于执行命令或填写配置时引用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_host_knowledge",
            "description": "设置或覆盖指定主机的 AI 知识。用于记录用户提供的该主机上的账户、密码、数据路径等信息；内容会按主机维度保存，后续在该主机上操作时可被 get_host_knowledge 或系统上下文使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "content": {"type": "string", "description": "要保存的完整知识内容（可多行，如：sudo密码: xxx\\nMySQL root密码: xxx\\n数据目录: /data/app）"},
                },
                "required": ["host_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_host_knowledge",
            "description": "向指定主机的 AI 知识末尾追加一段内容。用于在已有知识基础上补充新信息（如用户新告知的数据库密码、路径等），不覆盖原有内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "text": {"type": "string", "description": "要追加的文本（可多行）"},
                },
                "required": ["host_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_service_credentials",
            "description": "搜索/列出服务凭证**元数据**（绝不返回密码）。**本机 sudo（必传 host_id）**：仅返回绑定该主机的凭证；查凭证**不等于**要注入——须先执行 sudo 并 read，**仅有密码提示时**才 `send_service_password`（可用 `use_host_login=true`）。无提示（免密）勿注入。**跨机**按 IP+service 查找（scp→ssh）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command_hint": {"type": "string", "description": "待执行命令，推断 service+address（scp→ssh）；不因 user@ 隐藏其它用户凭证"},
                    "service": {"type": "string", "description": "服务类型：ssh、mysql、sudo 等（scp 请填 ssh）"},
                    "address": {"type": "string", "description": "目标 IP/域名；本机 sudo 通常留空"},
                    "host_id": {"type": "integer", "description": "当前控制台主机 ID；本机 sudo/su **必须**传入，仅匹配 host_id/linked_host_id=该主机 的凭证"},
                    "keyword": {"type": "string", "description": "模糊搜索：id、address、service_username、label、notes、service"},
                    "port": {"type": "integer", "description": "按端口过滤"},
                    "service_username": {"type": "string", "description": "仅当已确定用户名时过滤；选凭证阶段通常留空"},
                    "sort_by": {
                        "type": "string",
                        "enum": ["last_accessed_at", "created_at", "updated_at", "service", "address", "service_username", "id"],
                        "description": "排序字段，默认 last_accessed_at",
                    },
                    "sort_order": {"type": "string", "enum": ["asc", "desc"], "description": "排序方向，默认 desc"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 50，最大 200"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_service_credential",
            "description": "新增服务凭证（密码写入后**不可查询**）。典型：本机 sudo（务必 `linked_host_id=当前host_id` 复用 SSH 登录密码）、MySQL、跨机 SSH。同一 service+address 下不同用户用 service_username 区分。",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "服务类型：sudo、ssh、mysql、postgres、redis、ftp、other 等"},
                    "password": {"type": "string", "description": "密码（写入后不可查询；设 linked_* 时可省略）"},
                    "address": {"type": "string", "description": "目标地址/IP；本机 sudo 留空"},
                    "port": {"type": "integer", "description": "端口；省略则用该 service 默认（ssh=22, mysql=3306 等）"},
                    "service_username": {"type": "string", "description": "服务账户名（SSH 用户、DB 用户等）"},
                    "label": {"type": "string", "description": "简短标签"},
                    "notes": {"type": "string", "description": "备注"},
                    "linked_host_id": {"type": "integer", "description": "本机 sudo 强烈推荐：填当前 host_id，复用该主机 SSH 登录密码（无需再填 password）"},
                    "linked_credential_id": {"type": "integer", "description": "可选；引用「凭证管理」中 credentials.id 的登录密码"},
                    "host_id": {"type": "integer", "description": "可选；绑定操作主机。本机 sudo 若传 linked_host_id，服务端会同步写入 host_id"},
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_service_credential",
            "description": "更新服务凭证元数据或密码（按 id）。**不可查询原密码**；若需改密码请传新 password。",
            "parameters": {
                "type": "object",
                "properties": {
                    "credential_id": {"type": "integer", "description": "凭证 id（来自 list_service_credentials）"},
                    "service": {"type": "string"},
                    "password": {"type": "string", "description": "新密码（可选）"},
                    "address": {"type": "string"},
                    "port": {"type": "integer"},
                    "service_username": {"type": "string"},
                    "label": {"type": "string"},
                    "notes": {"type": "string"},
                    "linked_host_id": {"type": "integer"},
                    "linked_credential_id": {"type": "integer"},
                },
                "required": ["credential_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_service_credential",
            "description": "删除一条服务凭证（按 id）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "credential_id": {"type": "integer", "description": "凭证 id"},
                },
                "required": ["credential_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_service_password",
            "description": "向 PTY stdin 注入密码（结果不含明文）。**sudo/su 不总是要密码**：须先 read 确认尾部有 password 提示再调用；无提示则勿调用（免密成功）。默认服务端校验密码提示，无提示会拒绝。方式：① `credential_id`（本机 sudo 须绑定当前 host）；② 本机 sudo/su 推荐 `use_host_login=true`+`host_id`。禁止 send 发明文，禁止 sudo 后默认注入。",
            "parameters": {
                "type": "object",
                "properties": {
                    "credential_id": {"type": "integer", "description": "凭证 id；与 use_host_login 二选一。本机 sudo 时该凭证必须绑定当前 host_id"},
                    "use_host_login": {"type": "boolean", "description": "true=注入 host_id 对应主机的 SSH 登录密码（本机 sudo/su 在确认有提示后首选）"},
                    "target": {"type": "string", "enum": ["terminal", "ssh_channel", "local_terminal"], "description": "注入目标"},
                    "host_id": {"type": "integer", "description": "当前操作主机 ID。target=terminal 或 use_host_login 时必填"},
                    "channel_id": {"type": "integer", "description": "target=ssh_channel 时必填"},
                    "slot": {"type": "integer", "description": "控制台槽位（terminal/local_terminal 可选）"},
                    "require_password_prompt": {"type": "boolean", "description": "默认 true：校验尾部有密码提示才注入。仅特殊情况可显式 false（不推荐用于 sudo）"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_host_prompt",
            "description": "获取指定主机的「主机级 AI 提示词」（当前用户维度）。主机级提示词用于描述该主机**独有的规则 / 能力 / 工具链 / 配置**（例如：已安装 gh cli、cursor cli、opencode、nvm、docker；数据目录、端口约定；禁止重启 nginx 等）。按 (host_id, user_id) 独立存储；即使主机被分享给其它用户，不同用户的提示词互不可见。与 get_host_knowledge 区别：knowledge 偏机密/运维凭据（密码、token 等，严禁展示给用户）；prompt 偏可展示的规则 / 能力说明。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_host_prompt",
            "description": "设置或覆盖指定主机的「主机级 AI 提示词」（当前用户维度）。content 建议使用 Markdown 格式，归纳该主机独有的规则/能力/工具链/约束；不要把密码 / token / 私钥等机密写入（机密请用 update_host_knowledge）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "content": {"type": "string", "description": "完整的主机级提示词内容（可多行 Markdown）"},
                },
                "required": ["host_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_host_prompt",
            "description": "向指定主机的「主机级 AI 提示词」末尾追加一段内容（当前用户维度），不覆盖原内容。适合补充新发现的工具链、能力或规则。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "text": {"type": "string", "description": "要追加的文本（可多行 Markdown）"},
                },
                "required": ["host_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_hosts_by_prompt",
            "description": "根据**主机级 AI 提示词内容**搜索主机：在当前用户可访问且存在主机级提示词的主机中，返回 prompt 命中关键字/正则的主机列表（含匹配片段）。用于定位「具备某能力/工具链」的主机，例如用户问「帮我找一下装了 gh cli 的主机」「哪些机器配了 cursor cli」等。可用 group_id、tag_ids 限定范围。返回条目含 host_id、name、host、port、tags、提示词片段，便于后续让用户选择或直接在该主机执行操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键字（必填）。将在主机级提示词文本中按 LIKE 匹配；可用空格分隔多个关键字（任一命中即算匹配）。"},
                    "group_id": {"type": "integer", "description": "可选，仅搜索该分组内主机"},
                    "tag_ids": {"type": "array", "items": {"type": "integer"}, "description": "可选，仅搜索命中这些标签（任一）的主机"},
                    "regex": {"type": "string", "description": "可选，正则表达式；提供后在预筛结果上二次精筛"},
                    "case_sensitive": {"type": "boolean", "description": "正则是否区分大小写，默认 false"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 30，最大 100"},
                    "snippet_chars": {"type": "integer", "description": "每条返回的命中片段长度（前后文合计），默认 200，最大 600"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edgeops_init_workspace",
            "description": "在目标主机用户目录下初始化 .edgeops 工作区：创建 .edgeops/scripts、.edgeops/tasks、.edgeops/info、.edgeops/rules，并生成 scripts/index.md 与目录说明文档；可选创建一个按时间戳命名的任务目录并写入任务记录。若主机为 ESXi/嵌入式/设备专用系统，会自动切换为“主机知识库优先”并尽量不落盘。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "task_title": {"type": "string", "description": "可选，任务标题；传入后会在 tasks 下创建时间戳任务目录并记录"},
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edgeops_save_script",
            "description": "将脚本保存到 ~/.edgeops/scripts，并自动创建同名 .md 说明文档（用途、参数、输出、用法），同时更新 scripts/index.md，便于后续复用。设备型/专用系统会优先建议写入主机知识库而非落盘。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "script_name": {"type": "string", "description": "脚本文件名（如 backup_db.sh / check.ps1 / sync.py）"},
                    "script_content": {"type": "string", "description": "脚本内容"},
                    "purpose": {"type": "string", "description": "脚本用途简述"},
                    "parameters_desc": {"type": "string", "description": "参数说明（可多行）"},
                    "output_desc": {"type": "string", "description": "输出说明（可多行）"},
                    "usage_example": {"type": "string", "description": "用法示例（可多行）"},
                    "doc_content": {"type": "string", "description": "可选，完整说明文档内容；提供后将优先使用"},
                    "task_dir_name": {"type": "string", "description": "可选，任务目录名；传入后会在对应 task.md 追加“保存脚本”日志"},
                },
                "required": ["host_id", "script_name", "script_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edgeops_read_workspace_context",
            "description": "读取 ~/.edgeops 下的 scripts/index、rules、info 和最近任务目录概要，帮助 AI 判断是否已有可复用逻辑与注意事项。设备型/专用系统会返回“知识库优先”的替代上下文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "max_lines_per_file": {"type": "integer", "description": "每个文件最多读取行数，默认 120，范围 20-400"},
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edgeops_append_task_log",
            "description": "向 ~/.edgeops/tasks/<任务目录>/task.md 追加一条任务日志（时间、阶段、动作、结果），用于持续记录 AI 与用户在该主机上的执行过程。设备型/专用系统默认减少落盘，建议改记主机知识库。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "task_dir_name": {"type": "string", "description": "任务目录名（通常为 YYYYMMDDHHMMSS）"},
                    "phase": {"type": "string", "description": "阶段，如 诊断/变更/验证/回滚"},
                    "action": {"type": "string", "description": "执行动作简述"},
                    "result": {"type": "string", "description": "结果简述"},
                    "details": {"type": "string", "description": "可选，补充细节（可多行）"},
                },
                "required": ["host_id", "task_dir_name", "action", "result"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edgeops_write_rule",
            "description": "在 ~/.edgeops/rules 下创建或更新规则文档（.md），用于记录用户定义的操作注意事项与禁忌。设备型/专用系统默认减少落盘，建议改记主机知识库。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "rule_file": {"type": "string", "description": "规则文件名（如 safety.md，不带后缀也可）"},
                    "content": {"type": "string", "description": "规则文档完整内容"},
                },
                "required": ["host_id", "rule_file", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edgeops_write_info",
            "description": "在 ~/.edgeops/info 下创建或更新信息文档（.md），用于维护系统资源、运行环境、目录约定与操作背景。设备型/专用系统默认减少落盘，建议改记主机知识库。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "info_file": {"type": "string", "description": "信息文件名（如 runtime.md，不带后缀也可）"},
                    "content": {"type": "string", "description": "信息文档完整内容"},
                },
                "required": ["host_id", "info_file", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_maintenance_history",
            "description": "列出服务器维护历史记录。可按 host_id 或 host 字符串筛选，限制条数。用于查看某台主机的历史维护操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "可选，主机 ID，按该主机的 host 地址筛选"},
                    "host": {"type": "string", "description": "可选，按主机地址筛选"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 50"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_host_groups",
            "description": "列出所有主机分组（扁平列表，含 id、name、description、parent_id）。用于了解分组结构后再用 list_hosts(group_id=?) 查该组内主机。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_host_groups_tree",
            "description": "获取主机分组的树形结构（含每组的 children 与 hosts），与界面「服务器树」一致；树中每台主机含所有者 created_by、created_by_username、created_by_display_name。可选 host_q：只保留名称/IP/端口/描述/用途备注/别名/类型或 id 匹配的主机（各分组下 hosts 数组被过滤，便于按条件快速定位）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_q": {
                        "type": "string",
                        "description": "可选，按主机名、IP/域名、端口、描述、remark、aliases、host_type 或数字 id 过滤各节点下的 hosts；不区分大小写",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_group_detail",
            "description": "获取单个分组的详情（id、name、description、parent_id 及该组下的 host_ids）。",
            "parameters": {
                "type": "object",
                "properties": {"group_id": {"type": "integer", "description": "分组 ID"}},
                "required": ["group_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_host",
            "description": (
                "新建 SSH 主机（任意登录用户可用，非仅管理员）。"
                "必须提供 credential_id（已有凭证）或 new_credential（新建凭证并自动保存、关联）。"
                "用户给出「IP + 用户名/密码」时：用 new_credential={code?, name?, username, type:password, password}。"
                "不在其它字段内联账号密码。重复判定：同一所有者下地址（忽略大小写）+ 端口（默认 22）相同视为重复；"
                "若返回 duplicate 且未传 allow_duplicate，需用户确认后再传 allow_duplicate=true。"
                "创建成功后若要加入分组，再调 add_hosts_to_group（可先 list_host_groups / create_group）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "主机名称"},
                    "host": {"type": "string", "description": "主机地址（IP 或域名）"},
                    "port": {"type": "integer", "description": "SSH 端口，默认 22；仅当与已有记录端口相同才算重复"},
                    "allow_duplicate": {"type": "boolean", "description": "为 true 时即使与当前用户已有 host+port 重复也仍创建"},
                    "credential_id": {"type": "integer", "description": "可选，已有凭证 ID"},
                    "new_credential": {"type": "object", "description": "可选，新建凭证并关联。含 code、name、username、type。type 必须与认证方式一致：使用公钥认证时填 type=key_pair 并填 private_key（必填）与 public_key（可选）；使用密码时填 type=password 并填 password。不可提供私钥/公钥却填 type=password，否则认证会失败。"},
                    "description": {"type": "string", "description": "描述/备注（与 remark 用途说明可同时使用）"},
                    "aliases": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选，主机别名列表（口语称呼、便于搜索）；传入则整表替换，传 [] 清空",
                    },
                    "remark": {"type": "string", "description": "可选，服务器用途说明（如：生产 Nginx 入口）"},
                },
                "required": ["name", "host"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_host",
            "description": "更新主机信息（名称、地址、端口、凭证、认证方式、描述、aliases 别名、remark 用途说明等）。传入 host_id 与要修改的字段；aliases 传入则整列表替换，传 [] 可清空别名。在主机详情会话中可为当前绑定主机更新 aliases/remark 以便用户与 OpenClaw 通过昵称定位该机。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                    "credential_id": {"type": "integer"},
                    "username": {"type": "string"},
                    "auth_type": {"type": "string"},
                    "password": {"type": "string"},
                    "key_path": {"type": "string"},
                    "description": {"type": "string"},
                    "aliases": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "传入则替换整表；[] 清空",
                    },
                    "remark": {"type": "string", "description": "服务器用途说明"},
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_host",
            "description": "删除一台主机。主机所有者/管理员会执行真实删除；接收分享的用户调用时会解除自己收到的分享，不会删除真实主机。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer"},
                    "task_dir_name": {"type": "string", "description": "可选，任务目录名；删除/解除分享后自动追加任务日志"},
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "share_host",
            "description": "将主机分享给指定用户。仅主机所有者可分享。可用 user_id 或 username 指定接收方。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "user_id": {"type": "integer", "description": "接收分享的用户 ID（与 username 二选一）"},
                    "username": {"type": "string", "description": "接收分享的用户名（与 user_id 二选一）"},
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "revoke_host_share",
            "description": "撤销主机分享。主机所有者可撤销指定用户；接收方可撤销自己的分享（相当于解除接收）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "target_user_id": {"type": "integer", "description": "要撤销分享的用户 ID"},
                },
                "required": ["host_id", "target_user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_host_shares",
            "description": "查看某台主机当前分享清单（分享给了谁）。仅主机所有者可查看。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_received_host_shares",
            "description": "查看当前用户收到的主机分享列表（来自谁、是哪台主机）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "host_stats",
            "description": "获取主机统计（如总数量）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_group",
            "description": "新建主机分组（归属当前用户）。任意登录用户可用；非仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "parent_id": {"type": "integer", "description": "父分组 ID，可选"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_group",
            "description": "更新分组名称、描述或父分组。须对目标分组有操作权（分组创建者为当前用户，或管理员）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "parent_id": {"type": "integer"},
                },
                "required": ["group_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_group",
            "description": "删除一个分组（会移除组内关联，不删主机）。须对目标分组有操作权（创建者本人或管理员）。",
            "parameters": {
                "type": "object",
                "properties": {"group_id": {"type": "integer"}},
                "required": ["group_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_group_hosts",
            "description": "获取某分组下的主机列表。",
            "parameters": {
                "type": "object",
                "properties": {"group_id": {"type": "integer"}},
                "required": ["group_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_hosts_to_group",
            "description": "将多台主机加入指定分组。须对该分组有操作权（分组创建者为当前用户或管理员）；每台主机须对当前用户可访问（自有主机或他人 share_host 分享给你的）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer"},
                    "host_ids": {"type": "array", "items": {"type": "integer"}, "description": "主机 ID 列表"},
                },
                "required": ["group_id", "host_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_host_from_group",
            "description": "将一台主机从分组中移除。须对该分组有操作权（创建者本人或管理员）。",
            "parameters": {
                "type": "object",
                "properties": {"group_id": {"type": "integer"}, "host_id": {"type": "integer"}},
                "required": ["group_id", "host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_credentials",
            "description": "列出所有凭证（id、type、code、name、username 等，不含密码/私钥）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_credential_detail",
            "description": "获取单条凭证详情（脱敏）。",
            "parameters": {
                "type": "object",
                "properties": {"credential_id": {"type": "integer"}},
                "required": ["credential_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_credential",
            "description": "新建凭证（归属当前用户）。type 须与认证方式一致：password 填 username+password；key_pair 填 username+private_key（public_key 可选）。提供私钥/公钥时 type 必须为 key_pair。",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["password", "key_pair"]},
                    "code": {"type": "string", "description": "唯一编号"},
                    "name": {"type": "string", "description": "显示名称"},
                    "description": {"type": "string"},
                    "username": {"type": "string"},
                    "password": {"type": "string", "description": "type=password 时必填"},
                    "key_type": {"type": "string", "description": "type=key_pair 时，RSA 或 ECC"},
                    "key_bits": {"type": "integer", "description": "type=key_pair 时，如 2048"},
                    "public_key": {"type": "string"},
                    "private_key": {"type": "string"},
                },
                "required": ["type", "code", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_credential",
            "description": "更新本人凭证（code、name、description、username、password 或公钥私钥）。管理员可改任意凭证。",
            "parameters": {
                "type": "object",
                "properties": {
                    "credential_id": {"type": "integer"},
                    "code": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                    "public_key": {"type": "string"},
                    "private_key": {"type": "string"},
                },
                "required": ["credential_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_credential",
            "description": "删除凭证。仅可删除本人创建的凭证（管理员可删任意）。若已被主机引用则不可删。",
            "parameters": {
                "type": "object",
                "properties": {"credential_id": {"type": "integer"}},
                "required": ["credential_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cleanup_orphan_credentials",
            "description": "清理孤立凭证：批量删除所有**不被任何主机引用**（credential_id 指向它的 hosts 行数为 0）的凭证。默认只清理当前用户创建的；管理员可传 scope='all' 清理全库。建议先用 dry_run=true 预览。凭证删除后不可恢复。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["mine", "all"],
                        "description": "mine=仅清理当前用户创建的（默认）；all=清理全库所有用户的（仅管理员）。",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "true=只返回将被删除的凭证列表，不实际删除；默认 false。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_key",
            "description": "生成 RSA 或 ECC 密钥对。返回 public_key 与 private_key（PEM），可用于创建 key_pair 凭证。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key_type": {"type": "string", "enum": ["RSA", "ECC"], "description": "RSA 或 ECC"},
                    "key_bits": {"type": "integer", "description": "RSA 常用 2048/4096，ECC 256/384/521"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_maintenance_item",
            "description": "获取单条维护历史记录详情。",
            "parameters": {
                "type": "object",
                "properties": {"item_id": {"type": "integer"}},
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_maintenance",
            "description": "新建一条维护历史记录（host、port、category、content、file_path、details）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                    "category": {"type": "string"},
                    "content": {"type": "string"},
                    "file_path": {"type": "string"},
                    "details": {"type": "string"},
                },
                "required": ["host", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_maintenance",
            "description": "更新维护历史记录（category、content、file_path、details）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "category": {"type": "string"},
                    "content": {"type": "string"},
                    "file_path": {"type": "string"},
                    "details": {"type": "string"},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_maintenance",
            "description": "删除一条维护历史记录。",
            "parameters": {
                "type": "object",
                "properties": {"item_id": {"type": "integer"}},
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_prompt_skills",
            "description": "列出系统中配置的 Skills（AI 可调用的能力列表，含 code、name、description）。与主机/凭证等不同，此为能力模板。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_prompt_skill",
            "description": "获取单条 Skill 详情（id、code、name、description、parameters_schema）。",
            "parameters": {
                "type": "object",
                "properties": {"skill_id": {"type": "integer"}},
                "required": ["skill_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_channel_create",
            "description": (
                "创建 **SSH 通道**（后台 PTY，**不是** Web 控制台 tab）。"
                "用户说「打开通道」「打开 SSH 通道」「建通道」「用 ssh_channel」时用本工具。"
                "用户只说「打开终端/打开某主机」且未提「通道」时，应改用 connect_terminal/create_console，**禁止**用本工具。"
                "用于多条顺序命令、编译、安装、sudo 密码、菜单、vi、Ctrl+C 等。"
                "流程：create → send → read_lines/has_new → … → close；列表在侧栏「SSH通道管理」。"
                "Web 会话默认空闲 1800s 关断；集成会话默认 3600s。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "owner_type": {"type": "string", "description": "session 或 task，默认 session"},
                    "owner_id": {"type": "string", "description": "会话 ID 或任务 ID；省略时由系统绑定当前会话/终端 scope"},
                    "input_timeout_sec": {"type": "integer", "description": "输入超时秒数，可选"},
                    "output_timeout_sec": {"type": "integer", "description": "输出超时秒数，可选"},
                    "idle_close_sec": {"type": "integer", "description": "空闲多少秒后自动关闭；省略时 Web 1800 / 集成 3600"},
                },
                "required": ["host_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_channel_list",
            "description": (
                "列出 **ssh_channel 后台通道**（open 状态）。用户说「列出通道/SSH 通道」时用本工具（all_open=true）；"
                "用户说「列出终端/控制台」时用 list_terminals，二者不同。"
                "每条含 connected（通/断）、buffer_idle / session_state（闲/忙）、can_send_command 等状态。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner_type": {"type": "string", "description": "session 或 task"},
                    "owner_id": {"type": "string", "description": "会话 ID 或任务 ID"},
                    "all_open": {"type": "boolean", "description": "为 true 时忽略 owner，列出全部 open 通道"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_channel_info",
            "description": "获取指定 SSH 通道的详情（主机信息、别名、用途、提示词摘要、行号范围、通/断与闲/忙状态等）。",
            "parameters": {
                "type": "object",
                "properties": {"channel_id": {"type": "integer", "description": "通道 ID"}},
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_channel_get_status",
            "description": (
                "轻量查询 SSH 通道 **通/断**（connected）与 **闲/忙**（buffer_idle / session_state）。"
                "**仅 connected=false 时禁止 ssh_channel_send**；buffer_idle / can_send_command 仅供参考，不拦截发送。"
                "发命令前可先 list 或本工具；判 busy 时配合 ssh_channel_read_lines 看 tail_text / pending_partial。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "integer", "description": "通道 ID"},
                    "include_tail_lines": {
                        "type": "integer",
                        "description": "可选 0～20：附带末尾若干行文本预览",
                    },
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_channel_send",
            "description": "向 SSH 通道发送内容（命令、控制字符如 Ctrl+C）。**密码须用 send_service_password，勿发明文**。**禁止**用空行/回车「探测」是否等待密码（会被当成空密码提交）。在 channel 内再 SSH 到其它主机时，交互登录建议 `ssh -tt user@host`（强制内层 TTY）；读输出用 read_lines 的 tail_text/pending_partial 判断 password 提示。",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "integer"},
                    "content": {"type": "string", "description": "要发送的字符串"},
                },
                "required": ["channel_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_channel_read_lines",
            "description": (
                "按行读取通道输出；返回 **tail_text**（含无换行的 password: 提示）与 **pending_partial**，"
                "并附带 connected / buffer_idle / session_state 等状态。"
                "password 提示常无 \\n，勿只看 lines 为空就认为无输出。输出过大时自动落盘 spill。"
                "**wait_seconds=1～30**：无 until 时为 batch 末短等待；**0/省略=立即**。"
                "**until_contains**：超时内轮询 **tail_text+pending**（字面子串）命中则立即返回；"
                "超时仍返回当前内容。适合脚本标记串、password: 提示。带 until 时不再额外 batch 末 sleep。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "integer"},
                    "from_line": {"type": "integer", "description": "起始行号"},
                    "to_line": {"type": "integer", "description": "结束行号"},
                    "last_n": {"type": "integer", "description": "倒数 N 行"},
                    "since_line": {"type": "integer", "description": "自该行号以来的新行"},
                    "spill": {"type": "boolean", "description": "默认 true：过大时落盘"},
                    "wait_seconds": {
                        "type": "integer",
                        "description": "无 until_contains：batch 末等待 0～30。有 until_contains：轮询超时秒数（默认 30）",
                    },
                    "until_contains": {
                        "type": "string",
                        "description": "可选。轮询直到输出出现该字面量子串（如随机标记、password:）或超时",
                    },
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_channel_read_length",
            "description": (
                "按字符数读取通道输出；过大时自动落盘并返回 preview + spill_id。"
                "可选 wait_seconds=0～30、until_contains（同 ssh_channel_read_lines）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "integer"},
                    "max_chars": {"type": "integer", "description": "最多读取字符数，默认 8192"},
                    "wait_seconds": {
                        "type": "integer",
                        "description": "无 until：batch 末 0～30；有 until：轮询超时",
                    },
                    "until_contains": {
                        "type": "string",
                        "description": "可选。轮询直到输出出现该子串或超时",
                    },
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_channel_has_new",
            "description": (
                "查询通道是否有新输出（含无换行的 pending 尾部，如 password: 提示）。"
                "可配合 wait_seconds / until_contains；等标记或 password **优先用 read_lines + until_contains**"
                "（本工具 has_new 仍按 after_line；until 命中时也会把 has_new 置 true）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "integer"},
                    "after_line": {"type": "integer", "description": "行号，检查是否有比该行更新的内容"},
                    "wait_seconds": {
                        "type": "integer",
                        "description": "无 until：batch 末 0～30；有 until：轮询超时",
                    },
                    "until_contains": {
                        "type": "string",
                        "description": "可选。轮询直到 pending/tail 出现该子串或超时",
                    },
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_channel_close",
            "description": "关闭指定 SSH 通道。",
            "parameters": {
                "type": "object",
                "properties": {"channel_id": {"type": "integer"}},
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_channel_close_batch",
            "description": "按当前会话/任务 owner 批量关闭全部 open SSH 通道（集成会话结束清理用）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner_type": {"type": "string"},
                    "owner_id": {"type": "string"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_channel_dump_output",
            "description": "将通道当前缓冲全文导出到用户 chats/spill 目录，避免大输出占满上下文；返回 spill_id 供 read_chat_data 分段读取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "integer"},
                    "max_chars": {"type": "integer", "description": "最多导出字符数，默认 2000000"},
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "triggered_task_list",
            "description": "列出当前用户所有触发任务（含名称、介绍、触发条件、最后运行状态等）。供 AI 查看全量列表，便于决定在定时任务完成/失败时触发哪些触发任务，或主动调用哪些。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "triggered_task_list_exposed",
            "description": "列出本用户已配置「暴露接口」的触发任务（名称、介绍、暴露 code）。仅含可被定时任务发现并调用的那部分。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "triggered_task_status",
            "description": "查看触发任务运行状态（是否运行中、最后运行时间与结果）。不传参数则返回本用户全部触发任务的状态；传 task_id 或 task_name 则只返回该任务状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "触发任务 ID"},
                    "task_name": {"type": "string", "description": "或按任务名"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "triggered_task_get",
            "description": "获取单条触发任务详情（完整任务内容、介绍、触发条件等）。",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "integer", "description": "触发任务 ID"}},
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "triggered_task_create",
            "description": "创建触发任务。intro 供其它 AI 决策是否调用；trigger_conditions 可为 JSON，如 {\"on_scheduled_complete\":[1,2],\"on_scheduled_fail\":[3]} 表示定时任务 1/2 完成或 3 失败时触发。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "任务名"},
                    "content": {"type": "string", "description": "任务内容（提示词）"},
                    "intro": {"type": "string", "description": "介绍信息，供其它 AI 决策"},
                    "trigger_conditions": {"type": "string", "description": "触发条件 JSON 或说明"},
                },
                "required": ["name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "triggered_task_update",
            "description": "更新触发任务。仅传需要修改的字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "触发任务 ID"},
                    "name": {"type": "string", "description": "任务名"},
                    "content": {"type": "string", "description": "任务内容"},
                    "intro": {"type": "string", "description": "介绍信息"},
                    "trigger_conditions": {"type": "string", "description": "触发条件"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "triggered_task_delete",
            "description": "删除触发任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "触发任务 ID"},
                    "task_name": {"type": "string", "description": "或按任务名"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "triggered_task_trigger",
            "description": "触发执行一个触发任务。参数与触发接口一致，同用户才能触发。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "触发任务 ID"},
                    "task_name": {"type": "string", "description": "或按名称"},
                    "instruction": {"type": "string", "description": "定时任务 AI 提供的指令"},
                    "caller_task_id": {"type": "string", "description": "调用方定时任务 ID"},
                    "caller_task_name": {"type": "string", "description": "调用方定时任务名"},
                    "caller_status": {"type": "string", "description": "调用方状态，如 success/failed"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "triggered_task_current_run_history",
            "description": "查询同一用户、同一触发任务当前次执行的会话式历史（run 的消息列表）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "触发任务 ID"},
                    "run_id": {"type": "integer", "description": "可选，不传则取最近一次 run"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_list",
            "description": "列出当前用户的定时任务（含最后运行时间、状态、是否运行中）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_status",
            "description": "查看定时任务状态（是否运行中、最后运行时间与结果、下次运行时间）。不传参数则返回本用户全部任务的状态；传 task_id 或 task_name 则只返回该任务状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "定时任务 ID"},
                    "task_name": {"type": "string", "description": "或按任务名"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_get",
            "description": "获取单条定时任务详情（完整任务内容、cron、下次运行时间等）。",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "integer", "description": "定时任务 ID"}},
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_create",
            "description": "创建定时任务。cron_expr 为 cron 表达式（分 时 日 月 周），如 0 */2 * * * 表示每 2 小时。enabled=false 时仅创建不调度。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "任务名"},
                    "content": {"type": "string", "description": "任务内容（提示词）"},
                    "cron_expr": {"type": "string", "description": "cron 表达式，可选"},
                    "enabled": {"type": "boolean", "description": "是否启用定时调度，默认 true"},
                    "notify_email_to": {
                        "type": "string",
                        "description": "可选。每次执行结束后将**完整 AI 文字结论**发往这些邮箱（逗号分隔，非仅短摘要）。依赖用户在「我的发信设置」中启用个人 SMTP。若留空但在 content 中单独一行写「通知邮箱:」或「notify_email_to:」及地址，系统也会识别并用于发信。",
                    },
                },
                "required": ["name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_update",
            "description": "更新定时任务。仅传需要修改的字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "定时任务 ID"},
                    "name": {"type": "string", "description": "任务名"},
                    "content": {"type": "string", "description": "任务内容"},
                    "cron_expr": {"type": "string", "description": "cron 表达式"},
                    "enabled": {"type": "boolean", "description": "启用/停用定时调度"},
                    "notify_email_to": {"type": "string", "description": "结果通知邮箱，逗号分隔；清空可传空字符串"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_delete",
            "description": "删除定时任务及其全部执行历史与会话记录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "定时任务 ID"},
                    "task_name": {"type": "string", "description": "或按任务名"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_current_run_history",
            "description": "查询同一用户、同一定时任务当前次执行的会话式历史（供 AI 续写或复盘）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "run_id": {"type": "integer", "description": "可选，不传则取最近一次 run"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_run_now",
            "description": "立即执行一次定时任务（不等到 cron 时间）。可指定 task_id 或 task_name。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "定时任务 ID"},
                    "task_name": {"type": "string", "description": "或按任务名"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_terminal_buffer",
            "description": (
                "获取指定 AI 控制台的最近输出（滚动缓冲**末尾**即最新状态）。"
                "返回含 connected/session_state/buffer_idle/can_send/last_line 等（**均为参考，不拦截 send**）。"
                "**排障与 sudo 判断以 buffer 最后几行 + last_line 为准**。"
                "若 buffer_idle=否但 last_line 像提示符，见 false_busy_hint。"
                "connected=false 时仍可读缓冲但禁止 send_to_terminal。"
                "默认 tail_only=true：超长时仅返回最后 max_lines 行（默认 40）；"
                "需开头上下文时 tail_only=false 或 full_output=true。"
                "可用 next_poll_in_seconds 做 batch 末等待；亦可传 **until_contains**：在超时内轮询，"
                "**新输出出现该子串（或调用时近期尾部已有）则立即返回**，超时则照样返回（避免卡死）。"
                "脚本可故意 echo 随机标记串；sudo/password 提示也可用 until_contains 捕获。"
                "带 until_contains 时等待在工具内完成，不再额外 batch 末 sleep。"
                "轻量查状态用 get_terminal_status。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slot": {"type": "integer", "description": "控制台槽位（0、1、2…）；不传则按 host_id 或默认 AI slot"},
                    "host_id": {"type": "integer", "description": "可选：按主机 ID 自动选择 AI 控制台 slot"},
                    "full_output": {"type": "boolean", "description": "为 true 时返回完整输出，忽略 tail_only/max_lines。"},
                    "tail_only": {"type": "boolean", "description": "默认 true：超过 max_lines 时仅返回最后 max_lines 行（推荐日常轮询）。false 则保留前 2 行 + 后 33 行。"},
                    "max_lines": {"type": "integer", "description": "tail_only 或省略模式下的最大行数，默认 40，范围 10～200。"},
                    "next_poll_in_seconds": {"type": "integer", "description": "无 until_contains 时：batch 末等待秒数 1～3600。有 until_contains 时作为轮询超时（默认 30）。"},
                    "until_contains": {
                        "type": "string",
                        "description": "可选。在超时内轮询，输出中出现该字面量子串则立即返回（如随机标记、password:、Password:）。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_terminal_status",
            "description": (
                "查询 AI 控制台 **通/断**（connected）与 **闲/忙**（buffer_idle / session_state）。"
                "**仅 connected=false 时禁止 send_to_terminal**；buffer_idle / can_send_command **仅供参考**，不拦截发送。"
                "busy 时仍可 send，发完后 get_terminal_buffer 看是否生效；include_last_lines 可取末几行辅助判断。"
                "比 get_terminal_buffer 更轻，默认不含完整输出；include_last_lines 可取末几行辅助判断。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slot": {"type": "integer", "description": "控制台槽位；不传则按 host_id 或默认 AI slot"},
                    "host_id": {"type": "integer", "description": "可选：按主机 ID 自动选择 AI 控制台 slot"},
                    "include_last_lines": {
                        "type": "integer",
                        "description": "可选，附带 buffer 末尾行数（1～20），默认 0 不附带",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scp_push",
            "description": (
                "通过 **SFTP** 将文件或目录推送到主机（流式传输，支持大文件与目录树，调用卡会显示进度）。"
                "二选一：1) **content** 文本字符串（适合小脚本）；2) **local_path** **相对工作区根、且已存在**的路径（文件或目录，支持二进制）。"
                "目录上传需 **recursive=true**。大文件/安装包优先 local_path，勿用 content。**禁止** OS 绝对路径。\n"
                "**local_path 必守**：本工具**不会**自动补全、改写或猜测路径。"
                "**禁止**手拼 `chats/YYYY/MM/DD/…` 或臆造日期目录。"
                "必须使用：`fs_list` / 上一工具返回的 `path` / 用户给出的精确相对路径"
                "（如工作区根下的 `edgeops-v1.8.6-sp2.tgz`、`scripts/deploy.sh`、`chats/sessions/<id>/…`）。"
                "不确定时先 `fs_list` 再 push。\n"
                "**remote_path**：可为完整远程文件路径（如 `/tmp/moss.tgz`），或**目录**（如 `/tmp/`、`~/moss/`，"
                "会自动追加 local 文件名并创建父目录）。`~/…` 会展开为远端 home。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "remote_path": {
                        "type": "string",
                        "description": "远程路径：完整文件路径，或目录（末尾 / 或已存在目录，自动追加文件名），如 ~/moss/ 或 /tmp/pkg.tgz",
                    },
                    "content": {"type": "string", "description": "文本内容（与 local_path 二选一，仅适合小文本）"},
                    "local_path": {
                        "type": "string",
                        "description": (
                            "相对工作区根的**已存在**路径（与 content 二选一）。"
                            "例：edgeops-v1.8.6-sp2.tgz、scripts/deploy.sh。"
                            "禁止手拼 chats/日期目录；勿臆造路径。"
                        ),
                    },
                    "recursive": {"type": "boolean", "description": "local_path 为目录时必须 true"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 300，范围 30–3600"},
                },
                "required": ["host_id", "remote_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scp_pull",
            "description": (
                "通过 **SFTP** 从主机拉取**文件或目录**到**当前用户文件系统工作区**（流式落盘，调用卡显示进度）。"
                "适合大日志、安装包、归档；目录拉取需 **recursive=true**。\n\n"
                "**默认 session_managed（会话区）**：`local_path` 写逻辑名 → 归位 `chats/sessions/<session_id>/` 并加 UUID（无 session 时回退日期目录）。\n"
                "**精确路径**：`local_path` 为 `scripts/…`、`exchange/…`、`chats/sessions/…`、`reports/…` 等完整相对路径"
                "（或用户/主机/会话提示词指定）→ 按路径精确落盘；可显式 session_managed=false。"
                "**禁止**手拼臆造的 `chats/YYYY/MM/DD/`。\n"
                "**禁止** OS 绝对路径。"
                "**默认不限制体积**（`max_bytes=0` 或不传；系统 `SCP_PULL_MAX_BYTES=0` 时亦然）。"
                "若管理员配置了上限则生效（建议单文件 ≥ 2GiB）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "主机 ID"},
                    "remote_path": {"type": "string", "description": "远程绝对路径（文件或目录）"},
                    "local_path": {
                        "type": "string",
                        "description": "相对工作区路径；session_managed=false 时需完整相对路径",
                    },
                    "recursive": {"type": "boolean", "description": "远程为目录时必须 true"},
                    "session_managed": {
                        "type": "boolean",
                        "description": "默认 true：归位 chats/sessions/<session_id>/；false：按 local_path 精确落盘",
                    },
                    "max_bytes": {
                        "type": "integer",
                        "description": "可选单文件字节上限；0 或不传表示不限制（受系统配置约束时以系统为准）",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "SFTP 传输超时秒数，默认 300，范围 30–3600",
                    },
                },
                "required": ["host_id", "remote_path", "local_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_scp_transfer_script",
            "description": "生成在源主机上执行的 `scp -C` 传输脚本（A->B）。用于跨主机推送文件时先走直连方案。此工具只生成脚本，不会自动执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_host_id": {"type": "integer", "description": "源主机 ID（A）"},
                    "source_path": {"type": "string", "description": "A 上待传输文件路径"},
                    "target_host_id": {"type": "integer", "description": "目标主机 ID（B）"},
                    "target_path": {"type": "string", "description": "B 上目标路径"},
                    "compress": {"type": "boolean", "description": "是否启用 scp -C 压缩，默认 true"},
                },
                "required": ["source_host_id", "source_path", "target_host_id", "target_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_file_between_hosts",
            "description": "在两台主机间自动传输文件/目录：先检测 A->B 与 B->A 的 22 端口可达性，确定主动方；优先尝试 scp/rsync/sshfs（基于 SSH），若都失败则自动回退经 毛竹 web/fs 中转（服务端 SFTP 先拉到用户目录再推到目标机）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_host_id": {"type": "integer", "description": "源主机 ID（A）"},
                    "source_path": {"type": "string", "description": "A 上待传输路径（文件或目录）"},
                    "target_host_id": {"type": "integer", "description": "目标主机 ID（B）"},
                    "target_path": {"type": "string", "description": "B 上目标路径"},
                    "methods": {"type": "array", "items": {"type": "string", "enum": ["scp", "rsync", "sshfs"]}, "description": "直连优先方法顺序，默认 [scp, rsync, sshfs]"},
                    "edgeops_base_url": {"type": "string", "description": "回退中转时可选，毛竹（Moso）可被主机访问的地址"},
                    "ttl_seconds": {"type": "integer", "description": "回退中转临时 key 有效期（60~3600），默认 600"},
                    "keep_staging_for_multi_target": {"type": "boolean", "description": "回退中转时是否保留中转文件供多目标复用，默认 false"},
                    "auto_unpack_on_target": {"type": "boolean", "description": "回退中转时目录 tgz 是否在目标端自动解包，默认 true"},
                    "transfer_timeout_seconds": {"type": "integer", "description": "单次直连传输超时时间，默认 600 秒"},
                },
                "required": ["source_host_id", "source_path", "target_host_id", "target_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "relay_file_between_hosts",
            "description": "经 毛竹（Moso）web/fs 用户目录中转在两台主机间传文件/目录：服务端 SFTP 先从源主机拉到 web/fs/<用户>/staging，再 SFTP 推到目标主机（调用卡显示进度）。适合 A->B 直连失败或需统一经毛竹中转时。默认传输完成后删除中转文件；多目标分发时可保留 staging 供复用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_host_id": {"type": "integer", "description": "源主机 ID（A）"},
                    "source_path": {"type": "string", "description": "A 上待传输文件路径"},
                    "target_host_id": {"type": "integer", "description": "目标主机 ID（B）"},
                    "target_path": {"type": "string", "description": "B 上目标路径"},
                    "staging_path": {"type": "string", "description": "可选，中转路径（web/fs 下相对路径，默认 exchange/<时间戳>-<随机>/…）；不传自动生成。"},
                    "keep_staging_for_multi_target": {"type": "boolean", "description": "多目标分发模式：为后续多个目标复用同一中转文件。默认 false（单目标传输后自动删中转文件）。"},
                    "auto_unpack_on_target": {"type": "boolean", "description": "当 source_path 为目录时，是否在目标机 target_path 下保留源目录名（如 target_path/foo/）。默认 true。"},
                    "cleanup_staging": {"type": "boolean", "description": "完成后是否删除 web/fs 中转文件，默认 true"},
                    "transfer_timeout_seconds": {"type": "integer", "description": "单次 SFTP 拉取/推送超时（秒），默认 600"},
                },
                "required": ["source_host_id", "source_path", "target_host_id", "target_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "发起 **HTTP/HTTPS 出站请求**（GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS）。"
                "适合调用 REST API、Webhook、健康检查、提交 JSON 等。**非流式**，响应体有字节上限；超大响应请改用 `http_download` 落盘。\n\n"
                "**安全**：默认禁止访问内网/本机地址（SSRF 防护）；默认仅 HTTPS，明文 HTTP 需环境变量放行。\n"
                "**body_encoding**：`text`（默认）| `json` | `base64`（二进制）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                        "description": "HTTP 方法，默认 GET",
                    },
                    "url": {"type": "string", "description": "完整 URL（http:// 或 https://）"},
                    "headers": {"type": "object", "description": "请求头键值对"},
                    "query": {"type": "object", "description": "URL 查询参数（追加到 url）"},
                    "body": {"type": "string", "description": "请求体（GET/HEAD/OPTIONS 忽略）"},
                    "body_encoding": {
                        "type": "string",
                        "enum": ["text", "json", "base64"],
                        "description": "请求体编码，默认 text",
                    },
                    "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 600"},
                    "max_response_bytes": {"type": "integer", "description": "响应体字节上限，默认与系统配置一致"},
                    "follow_redirects": {"type": "boolean", "description": "是否跟随重定向，默认 true"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_download",
            "description": (
                "从 **HTTP/HTTPS URL 下载文件**到**当前用户文件系统工作区**（流式落盘，调用卡显示进度，用户可点停止取消）。\n\n"
                "**默认 session_managed**：`local_path` 写逻辑短名 → 归位 `chats/sessions/<session_id>/` 并加 UUID；"
                "完整相对路径（`scripts/…`、`exchange/…`）→ 精确落盘。\n"
                "**默认不限制体积**（`max_bytes=0` 或不传）。\n"
                "**分块下载**：设 `chunked=true` 或 `chunk_size`（字节）启用 HTTP Range 分块；"
                "默认下载全部分块后 **自动合并** 到 `local_path`（`merge_chunks=false` 仅保留 `.part000000` 等）。"
                "可用 `chunk_index` 只下指定块；合并用 `http_download_merge`。\n"
                "**禁止** OS 绝对路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "下载 URL"},
                    "local_path": {"type": "string", "description": "相对工作区根的保存路径（合并后的最终文件名）"},
                    "headers": {"type": "object", "description": "可选请求头"},
                    "session_managed": {
                        "type": "boolean",
                        "description": "默认 true：归位 chats/sessions/<session_id>/；false：按 local_path 精确落盘",
                    },
                    "max_bytes": {"type": "integer", "description": "可选字节上限；0 或不传表示不限制"},
                    "chunked": {"type": "boolean", "description": "启用 Range 分块下载（默认块大小见系统配置）"},
                    "chunk_size": {"type": "integer", "description": "分块大小（字节），如 67108864"},
                    "chunk_index": {"type": "integer", "description": "仅下载指定分块（0 起），落盘为 local_path.partNNNNNN"},
                    "merge_chunks": {"type": "boolean", "description": "下载全部分块后自动合并到 local_path，默认 true"},
                    "delete_parts": {"type": "boolean", "description": "合并后删除 .part 文件，默认 true"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 3600"},
                    "follow_redirects": {"type": "boolean", "description": "是否跟随重定向，默认 true"},
                },
                "required": ["url", "local_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_download_merge",
            "description": (
                "合并已下载的 HTTP 分块文件（`<local_path>.part000000`、`.part000001` …）为最终文件。\n"
                "不传 `part_paths` 时自动扫描 `local_path` 同目录下匹配的分块。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "local_path": {"type": "string", "description": "合并输出文件（相对工作区根）"},
                    "part_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选，显式指定分块相对路径列表（按顺序合并）",
                    },
                    "delete_parts": {"type": "boolean", "description": "合并后删除分块，默认 true"},
                },
                "required": ["local_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_upload",
            "description": (
                "从**用户文件系统工作区**上传文件到 **HTTP/HTTPS URL**（流式上传，调用卡显示进度，用户可点停止取消）。\n\n"
                "默认 **multipart/form-data**（`field_name` 默认 file）；`multipart=false` 时以原始 body 流上传。\n"
                "`local_path` 为**相对工作区根**的文件路径；**默认不限制体积**（`max_bytes=0` 或不传）。\n"
                "**禁止** OS 绝对路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "上传目标 URL"},
                    "local_path": {"type": "string", "description": "相对工作区根的文件路径"},
                    "method": {
                        "type": "string",
                        "enum": ["POST", "PUT", "PATCH"],
                        "description": "HTTP 方法，默认 POST",
                    },
                    "headers": {"type": "object", "description": "可选请求头"},
                    "field_name": {"type": "string", "description": "multipart 字段名，默认 file"},
                    "form_fields": {"type": "object", "description": "multipart 额外表单字段"},
                    "content_type": {"type": "string", "description": "文件 Content-Type；multipart 时可选"},
                    "multipart": {"type": "boolean", "description": "是否 multipart 上传，默认 true"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 3600"},
                    "max_bytes": {"type": "integer", "description": "可选字节上限；0 或不传表示不限制"},
                    "follow_redirects": {"type": "boolean", "description": "是否跟随重定向，默认 true"},
                },
                "required": ["url", "local_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_list",
            "description": (
                "列出**当前用户文件系统工作区**（侧栏「文件系统」）下的文件和子目录。"
                "path 为**相对工作区根**的路径，空表示根目录；**可列出任意子目录**（scripts/、exchange/、任意日期的 chats/… 等），"
                "不限于当日 chats。**禁止** OS 绝对路径或 web/fs/ 前缀。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区根的目录路径，空或 / 表示根目录；例 chats/2026/06/11"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_search",
            "description": (
                "在**当前用户文件系统工作区**内搜索文件（默认仅文件、递归子目录）。"
                "**所有筛选条件均为可选**，可**单独使用**任一条件，也可**任意组合**（多条件同时满足，AND 关系）。"
                "支持：文件名正则 name_regex、相对路径正则 path_regex、后缀 extensions、"
                "文件大小 min_bytes/max_bytes、修改时间 min_mtime/max_mtime 或 modified_after/modified_before。"
                "不传任何筛选条件时，返回 path 根目录下所有文件（受 limit 限制）。"
                "**返回 items 每项含 id**（本次搜索内从 1 递增）。向用户展示结果时**必须保留 id**；"
                "用户说「读 2 号」「处理 id=3」「删除第 1 项」时，根据 id 找到对应项的 **path**，"
                "再调用 fs_read_file / fs_delete / fs_copy / http_upload 等工具。"
                "path 为搜索根目录（相对工作区根，空表示根）；modified_after/modified_before 支持 Unix 秒或 ISO8601。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "搜索根目录相对路径，空表示工作区根"},
                    "name_regex": {"type": "string", "description": "可选。文件名正则（Python re，忽略大小写），如 \".*\\.log$\""},
                    "path_regex": {"type": "string", "description": "可选。相对路径正则（忽略大小写）"},
                    "extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选。后缀列表，如 [\".log\", \"txt\"]",
                    },
                    "min_bytes": {"type": "integer", "description": "可选。最小文件大小（字节，含）"},
                    "max_bytes": {"type": "integer", "description": "可选。最大文件大小（字节，含）"},
                    "min_mtime": {"type": "number", "description": "可选。最早修改时间（Unix 秒）"},
                    "max_mtime": {"type": "number", "description": "可选。最晚修改时间（Unix 秒）"},
                    "modified_after": {"type": "string", "description": "可选。修改时间不早于（Unix 秒或 ISO8601）"},
                    "modified_before": {"type": "string", "description": "可选。修改时间不晚于（Unix 秒或 ISO8601）"},
                    "recursive": {"type": "boolean", "description": "是否递归子目录，默认 true"},
                    "files_only": {"type": "boolean", "description": "仅返回文件，默认 true"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 200"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_chats_workspace_dir",
            "description": (
                "返回当前会话工作区目录前缀 `chats/sessions/<session_id>/`（与附件、工具 spill、默认 fs_write/scp_pull 归位一致；无 session 时回退日期目录）。旧 `chats/YYYY/MM/DD/` 仍可读取。报告请用 `create_chat_artifact`（`reports/…`），勿手拼路径。"
                "写**会话临时产物**（脚本、中间数据、scp 默认拉取）前若不确定日期路径可调用。"
                "读取或修改工作区**其它已有目录**（scripts/、exchange/、历史 chats/…）时直接用 fs_* 传完整相对路径，"
                "不必局限当日 chats。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_read_file",
            "description": (
                "读取**当前用户文件系统工作区**中的文本文件（UTF-8）。"
                "path 为**相对工作区根**的完整路径，**可读取任意子目录**"
                "（如 `scripts/deploy.sh`、`exchange/pkg.tgz` 旁路文本、`chats/2026/07/03/report.md`）；"
                "支持 offset/size 分段读取。**禁止** OS 绝对路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区根的文件路径，如 chats/2026/06/11/report.md 或 scripts/deploy.sh"},
                    "offset": {"type": "integer", "description": "可选，起始字符偏移（默认 0）"},
                    "size": {"type": "integer", "description": "可选，读取字符数；不传则读到末尾"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_write_file",
            "description": (
                "向**当前用户文件系统工作区**写入文本文件（UTF-8）。path 为**相对工作区根**；支持 overwrite/append 及按字符 offset 定位写。\n"
                "**默认 session_managed（会话区）**：用于 AI 临时脚本、中间数据、复杂内容落盘；"
                "path 写逻辑名如 `report.md`、`data/raw.csv` → 自动归位 `chats/sessions/<session_id>/` 并加 UUID。\n"
                "**精确路径（自动识别或 session_managed=false）**：path 以 `scripts/`、`exchange/`、"
                "`chats/YYYY/MM/DD/…` 等完整相对路径开头，或用户/主机/会话提示词指定路径 → 按 path 精确读写。\n"
                "本机管理会话且 session_managed=true 时归位到 `local/<UTC日期>/…`。\n"
                "**禁止** OS 绝对路径；**禁止**用本工具写 `skills/`（须 save_user_skill / write_user_skill_file）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对工作区路径；session_managed=true 时可为逻辑段；false 时需完整相对路径",
                    },
                    "content": {"type": "string", "description": "文件内容（文本）"},
                    "mode": {"type": "string", "description": "可选：overwrite|append|insert|replace，默认 overwrite"},
                    "offset": {"type": "integer", "description": "可选，字符偏移；配合 insert/replace 可定位写"},
                    "replace_length": {"type": "integer", "description": "可选，replace 模式下替换的字符数；默认 0"},
                    "session_managed": {
                        "type": "boolean",
                        "description": "省略时：逻辑短路径→归位 chats/sessions/<id>/；完整相对路径→精确读写。true 强制归位；false 强制精确",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_read_binary",
            "description": (
                "从**当前用户文件系统工作区**读取二进制文件，返回 base64 或 hex。"
                "path 为相对工作区根的**完整路径**，可读取任意子目录；支持 offset/size。**禁止** OS 绝对路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区根的文件路径"},
                    "offset": {"type": "integer", "description": "可选，字节偏移（默认 0）"},
                    "size": {"type": "integer", "description": "可选，读取字节数；不传则读到末尾"},
                    "encoding": {"type": "string", "description": "返回编码：base64 或 hex，默认 base64"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_write_binary",
            "description": (
                "向**当前用户文件系统工作区**写入二进制（content 为 base64 或 hex）。"
                "**session_managed** 与 fs_write_file 相同（省略时按 path 自动识别）。**禁止** OS 绝对路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区路径；session_managed=false 时需完整相对路径"},
                    "content": {"type": "string", "description": "二进制编码内容（base64 或 hex）"},
                    "offset": {"type": "integer", "description": "可选，写入字节偏移"},
                    "truncate": {"type": "boolean", "description": "可选，无 offset 时是否覆盖写（默认 false=追加）"},
                    "encoding": {"type": "string", "description": "content 编码：base64 或 hex，默认 base64"},
                    "session_managed": {
                        "type": "boolean",
                        "description": "默认 true：归位 chats/sessions/<session_id>/；false：按 path 精确写入",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_truncate",
            "description": "将**当前用户文件系统工作区**下文件截断或扩展到指定字节大小。path 为相对工作区根。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区根的文件路径"},
                    "size": {"type": "integer", "description": "目标字节大小；默认 0"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_mkdir",
            "description": (
                "在**当前用户文件系统工作区**创建目录。path 为相对工作区根的**完整路径**，"
                "可在任意子目录下创建（scripts/、exchange/、chats/任意日期/…）。"
                "会话临时目录可先 get_chats_workspace_dir。**禁止** OS 绝对路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区根的目录路径，如 chats/2026/06/11/scripts"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_pack_tgz",
            "description": "将**当前用户文件系统工作区**下某目录打包为 .tgz。path 为相对工作区根的目录路径，生成 path.tgz。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对 fs 根的目录路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_unpack_tgz",
            "description": "解压**当前用户文件系统工作区**下的 .tgz。path 为相对工作区根的 tgz 路径，dest 为解压目标目录（相对工作区根），空则解压到 tgz 同目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对 fs 根的 .tgz 文件路径"},
                    "dest": {"type": "string", "description": "解压目标目录（相对 fs 根），可选"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_delete",
            "description": "删除**当前用户文件系统工作区**下的文件或目录；目录会递归删除（含非空）。path 为相对工作区根。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区根的文件或目录路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_copy",
            "description": "在**当前用户文件系统工作区**内复制或移动：将 path 复制到 dest_dir；move 为 true 时移动。路径均为相对工作区根。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "源文件或目录相对路径"},
                    "dest_dir": {"type": "string", "description": "目标目录相对路径"},
                    "move": {"type": "boolean", "description": "true=移动，false=仅复制，默认 false"},
                },
                "required": ["path", "dest_dir"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_chat_attachment",
            "description": (
                "读取用户在**聊天框中上传的附件**（图片/文本/Markdown/Office/PDF 等）。附件由用户通过上传按钮或粘贴操作附带；"
                "UUID 通过「用户消息文末的 📎 附件清单」获得，形如 `uuid: abc123...`。\n\n"
                "- 文本/Markdown：返回 content 字段（若过长将按 max_chars 截断，默认 40_000）；\n"
                "- Office/PDF 等富文档（kind=document，如 .docx/.pptx/.xlsx/.pdf）：自动用 MarkItDown 转为 Markdown 后返回 content；"
                "响应含 `converted_from_markitdown: true`；若转换失败会返回 error 与 hint；\n"
                "- 图片：**若附件已有 `ai_description`（此前轮次 AI 已识别并保存的扩展信息），默认只返回这段描述文本，不再返回 data_url**，"
                "节省上下文；若你需要重新识别（比如用户追问局部/细节而描述未覆盖，或用户明确要求「看原图」），传 `force_reload=true` 强制返回 `data_url`；"
                "若附件尚无描述，则默认返回 `data_url`（已自动缩放压缩的 JPEG data URL，适配网关 input 长度上限）。\n"
                "- 大图/看不清/小目标：不要回答「图片太大/我看不到」后停止。先用 `force_reload=true` 读压缩整图粗识别；"
                "若整图仍看不清，可传 `tile_grid` 生成分块并按 `tile_id` 逐块读取，或传 `region` 裁剪局部高清图；返回 `region_meta`，"
                "后续精确标注时把局部坐标连同该 `region_meta` 传给 `edit_chat_attachment_image.source_region` 回填原图。\n"
                "- 首次识别图片后，请**主动调用 `save_image_description(uuid=..., description=...)`** 把提取到的内容（OCR 文本 + 主要视觉元素 + 结构化信息）"
                "写回附件行，后续轮次才能跳过重读原图。\n"
                "- 仅可读取**当前用户自己**的附件，其他用户或越权 UUID 一律拒绝。\n"
                "- **严禁**仅凭元信息（mime/size）就回答「看不清图」「我只能识别是 PNG」之类——"
                "若图已内联直接基于图像作答；若确实需要字节数据，调用本工具即可拿到 data_url。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uuid": {"type": "string", "description": "附件 UUID（从用户消息末尾的 📎 清单中获取）"},
                    "max_chars": {"type": "integer", "description": "文本内容最大返回字符数，默认 40000；超出会在末尾提示已截断"},
                    "as_data_url": {"type": "boolean", "description": "图片是否返回 base64 data URL，默认 true；传 false 时仅返回元信息（用于配合 prefer_description 只拿描述）"},
                    "prefer_description": {"type": "boolean", "description": "图片是否优先返回 `ai_description` 扩展信息（默认 true）；设 false 会同时返回 data_url + description（更耗 token）"},
                    "force_reload": {"type": "boolean", "description": "强制忽略已缓存的 `ai_description`，重新返回原图 data_url（默认 false）。仅在用户明确要求重新识别/看原图，或已有描述与当前问题严重不匹配时使用"},
                    "region": {"type": "object", "description": "可选局部区域 {x,y,width,height} 或 {left,top,right,bottom}；用于大图小目标精看局部"},
                    "region_coordinate_space": {
                        "type": "string",
                        "enum": ["auto", "pixel", "percent", "norm", "norm1000"],
                        "description": "region 的坐标系，默认 auto；percent=0–100 相对原图；norm=0–1；norm1000=0–1000；pixel=原图像素",
                    },
                    "pad_ratio": {"type": "number", "description": "裁剪 region 时向四周扩展比例，默认 0.08，避免粗定位框太紧"},
                    "tile_grid": {"type": "object", "description": "可选分块配置 {rows,cols,overlap_ratio}；用于不确定目标在哪时逐块查找"},
                    "tile_id": {"type": "integer", "description": "tile_grid 模式下要返回 data_url 的分块编号（1 起）；省略时默认第 1 块"},
                },
                "required": ["uuid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_chat_data",
            "description": (
                "读取**工具大结果溢出文件**。当某条 `role=tool` 消息以 `[[EDGEOPS_CHAT_DATA ref=<uuid> subdir=<YYYY/MM/DD> ...]]` 开头时，"
                "完整 UTF-8 文本已落在用户文件根下 `chats/<subdir>/spill/<ref>.data`（与聊天附件不同）。\n"
                "**硬性要求**：出现该哨兵后，在输出设备/资产/漏洞等**清单或表格**前必须先调用本工具读取落盘内容；"
                "禁止凭预览、推理或历史摘要自行补全工具结果。\n"
                "**参数**：`spill_id`=ref；`date_subdir`=subdir；`mode`：head_tail（默认）、head、tail、range。"
                " **选型**：终端/日志溢出用 tail 或 head_tail（tail_chars 宜大）；JSON/清单/配置用 head 或 range（宜多次 range 覆盖 total_chars）。\n"
                "需要全量核对、聚合、按行制表时，应分段调用本工具直至覆盖全部内容，勿仅凭上下文压缩片段下结论。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spill_id": {"type": "string", "description": "哨兵 ref= 后的 UUID"},
                    "date_subdir": {
                        "type": "string",
                        "description": "哨兵 subdir= 后的路径，如 2026/05/08（UTC 日期，与落盘一致）",
                    },
                    "mode": {
                        "type": "string",
                        "description": "head_tail | head | tail | range，默认 head_tail",
                    },
                    "head_chars": {"type": "integer", "description": "head / head_tail 时头部最大字符数，默认 32000（可 env 配置）"},
                    "tail_chars": {"type": "integer", "description": "tail / head_tail 时尾部最大字符数，默认 32000"},
                    "range_start": {"type": "integer", "description": "range 模式起始字符偏移，默认 0"},
                    "max_chars": {"type": "integer", "description": "range 模式最大返回字符数，默认 64000"},
                },
                "required": ["spill_id", "date_subdir"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_image_description",
            "description": (
                "把本次**对图片附件**的识别结果（OCR / 主要视觉元素 / 界面结构 / 关键数据 等）写回附件行，做为图片的扩展信息。\n\n"
                "**何时必须调用**：\n"
                "- 你首次看到某张图片（内联 image_url 或刚调 read_chat_attachment 取到 data_url）并完成分析后，"
                "**立即**调用本工具把你提取到的内容写回去；以后同一 uuid 的图在多轮对话里会默认以这段文本形式出现，你不再需要重读原图。\n"
                "- 用户明确要求「重新识别 / 再看一遍 / 看原图」并你已重新分析完时，也请更新描述。\n\n"
                "**写什么**：一段**自包含**的中文描述（建议 200–1500 字），覆盖：\n"
                "1. 图片类型（截图 / 照片 / 图表 / UI 界面 等）\n"
                "2. OCR 文字（按原位尽量完整誊写，代码/命令原样保留）\n"
                "3. 主要视觉元素（布局、色彩、标记、箭头、红框等）\n"
                "4. 结构化信息（表格、数值、曲线趋势、时间戳等）\n"
                "5. 可能与用户问题相关的异常/要点提示\n\n"
                "写回后无需再把大段描述复述给用户；直接基于它继续回答即可。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uuid": {"type": "string", "description": "附件 UUID（从 📎 清单或 read_chat_attachment 响应里取）"},
                    "description": {"type": "string", "description": "图片的文本化扩展信息；建议 200–1500 字的自包含中文描述"},
                },
                "required": ["uuid", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_chat_attachment_image",
            "description": (
                "对用户在聊天中上传的**图片附件**做简单编辑并保存为**新附件**（不覆盖原图）。"
                "可用于画红框、半透明高亮遮罩、画线、填色、插入文字，或旋转/裁剪/缩放。\n\n"
                "**标注定位（默认 1 次调用）——首选「找关键点 + 小标记 + 全局微调」**\n"
                "1. **最佳（强烈推荐）**：一次性标出**所有**目标，用 `coordinate_space=\"percent\"`，关键点只给 x/y（0–100）。"
                "UI 菜单/按钮/图标中心优先 `type=\"crosshair\"` 或 `type=\"callout\"`；通用图片关键点优先 `type=\"target\"`/`ring`。"
                "不要为单个目标估大矩形范围。\n"
                "2. **小文本目标**：用户要求选中/标记主机名、字段名、标题、菜单文字时，优先用 `crosshair` 或 `callout` 指文字中心/基线；"
                "不要用实心矩形遮罩。单个文字或矩形类标注会返回 `visual_review_required=true`，必须先看 data_url 确认再交付。\n"
                "3. **语义定位必须先确认**：用户要求标记狗/人/物体时，先确认内容区里真实可见的目标实例，标对象中心点；"
                "不要把搜索框、输入框、标题栏、搜索词或按钮误当作目标。若无法确定目标在哪，先问用户，不要乱标。\n"
                "4. **全局微调**：默认 `auto_global_transform=true`，后端会自动搜索整组 scale+offset；也可手动传 `global_transform`。"
                "网页截图左右白边大时用 `coordinate_space=\"percent_content\"`（相对内容区 0–100）。\n"
                "5. **兜底预览（首轮默认勿用）**：`grid_overlay` / `cell_grid` / `calibration_probe` 只在一次关键点标注后仍明显不准时使用，会增加轮次。\n"
                "6. **少遮挡**：需要区域时才用 `type=rect` 细框（只设 outline、不设 fill）；单行菜单/按钮 height 约 **3–5%**。"
                "如果你定位到的是目标**中心点**而不是左上角，必须传 `anchor=\"center\"`（或 `center_x/center_y`），"
                "后端会自动换算左上角；不要把中心点直接当 `x/y` 左上角。"
                "**禁止**用 highlight/overlay 实心大块遮罩标单个菜单项。默认 `tight_boxes=true` 会收紧过大框。\n"
                "7. 正常结果若返回 `deliver_now=true`，立即交付 `markdown_image`，不要再调工具；若 `visual_review_required=true` 先看 data_url，准确才交付；若 `should_retry=true`，仅原样复用 annotations 并微调 `global_transform` 一次。\n"
                "8. **小文字/图标必走局部精修**：整图发给模型会被压到约 768px，小目标在整图上估的 percent 不准。要标小文字/字段名/图标/按钮时，先在整图粗估 region，再用 read_chat_attachment(region=...) 取局部高清图（后端会自动放大局部图让你看清），在放大图里读出中心 percent；不确定位置时用 tile_grid 逐块看。"
                "局部图识别出的 annotations 连同 read 返回的 `region_meta` 作为 `source_region` 传回本工具，后端自动回填原图坐标（用 percent 不受放大影响）。\n"
                "9. **勿**传 `use_original_coordinates`、**勿**自己心算像素缩放、**勿**逐个目标单独修。\n\n"
                "**透明度**：每项可设 `opacity`（0~1 或 0~100）；颜色可用 `#RRGGBBAA`。\n\n"
                "**annotations 类型**：crosshair、target/ring、callout、pin/marker、rect/ellipse/polygon、overlay/mask/highlight、line、text。\n"
                "仅可编辑当前用户自己的 image 类附件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uuid": {"type": "string", "description": "源图片附件 UUID"},
                    "output_name": {"type": "string", "description": "新文件名，默认 edited-<原名>.png"},
                    "rotate": {"type": "number", "description": "顺时针旋转角度（度），默认 0"},
                    "crop": {
                        "type": "object",
                        "description": "裁剪区域：x/y/width/height 或 left/top/right/bottom",
                    },
                    "scale": {"type": "number", "description": "等比缩放倍数，如 0.5 或 2.0"},
                    "coordinate_space": {
                        "type": "string",
                        "enum": ["percent", "percent_content", "norm", "norm1000", "pixel"],
                        "description": (
                            "annotations 的坐标系。**首选 percent**（0–100 相对整图）；"
                            "网页/截图左右有大白边时用 **percent_content**（0–100 相对检测到的内容区）；"
                            "norm=0–1；norm1000=0–1000；pixel=原图像素。"
                            "该坐标系会作用于所有位置字段：x/y、x1/y1/x2/y2、anchor_x/anchor_y、label_x/label_y、left/top/right/bottom。"
                            "视觉尺寸字段 radius/arm_length/gap_radius/ring_width 仍按最终像素理解，用于控制标记大小。"
                        ),
                    },
                    "auto_global_transform": {
                        "type": "boolean",
                        "description": "默认 true：3 个及以上目标时后端自动搜索整组 scale+offset 宏观校正（相对布局对、整组偏时可免 AI 手调 global_transform）。1-2 个目标不会自动校正，传 false 可显式关闭。",
                    },
                    "grid_overlay": {
                        "type": "boolean",
                        "description": "兜底预览，首轮默认勿用。true 时仅输出一张 0–100 百分比刻度图，便于读取坐标（不做业务标注，会增加轮次）。",
                    },
                    "cell_grid": {
                        "type": "boolean",
                        "description": "兜底预览，首轮默认勿用。true 时仅输出编号小格图；下一轮用 annotations[].cells=[编号...] 出框（不适合关键点，增加轮次）。",
                    },
                    "cell_cols": {"type": "integer", "description": "编号网格列数（默认 12，2–40）；cell_grid 预览与带 cells 的标注必须用相同值"},
                    "cell_rows": {"type": "integer", "description": "编号网格行数（默认 8，2–40）"},
                    "global_transform": {
                        "type": "object",
                        "description": (
                            "对**所有** annotations 的宏观微调（在坐标系换算前统一应用），用于「相对位置准、但整组缩放/平移有偏差」的情形。"
                            "字段：scale（统一缩放）或 scale_x/scale_y，offset_x/offset_y（平移，单位同坐标系：percent 下为百分点）。"
                            "例：整组右移 8% 用 {\"offset_x\":8}；整组偏大用 {\"scale\":0.9}。看结果图后只调这一组旋钮即可整体对齐，不必逐个改框。"
                        ),
                    },
                    "tight_boxes": {
                        "type": "boolean",
                        "description": "默认 true：自动收紧 rect/highlight 等过大的 width/height（如单行菜单误标成三行高）。传 false 关闭。",
                    },
                    "max_box_height_percent": {
                        "type": "number",
                        "description": "tight_boxes 时 height 上限（percent 坐标下默认 5.5 百分点，约一行菜单高）",
                    },
                    "max_box_width_percent": {
                        "type": "number",
                        "description": "tight_boxes 时 width 上限（percent 坐标下默认 18 百分点）",
                    },
                    "reference_width": {
                        "type": "number",
                        "description": "标注坐标所依据的参考图宽度（通常为内联识图 vision_width）；与原图不同时用于换算",
                    },
                    "reference_height": {
                        "type": "number",
                        "description": "标注坐标所依据的参考图高度（通常为内联识图 vision_height）",
                    },
                    "source_region": {
                        "type": "object",
                        "description": (
                            "局部精识别回填原图用。传 read_chat_attachment(region=...) 返回的 region_meta；"
                            "此时 annotations 坐标相对该局部图，后端会按 source_region 自动映射回原图。"
                        ),
                    },
                    "source_region_vision_meta": {
                        "type": "object",
                        "description": (
                            "可选：read_chat_attachment(region=...) 返回的 region_vision_meta。"
                            "当局部 annotations 使用 pixel 坐标时，后端用其中 model_view_width/height 或 vision_width/height 把局部所见像素缩放回原图区域。"
                        ),
                    },
                    "offset_x": {
                        "type": "number",
                        "description": "参考图相对原图左上角的 X 偏移（裁剪/局部图时使用，默认 0）",
                    },
                    "offset_y": {
                        "type": "number",
                        "description": "参考图相对原图左上角的 Y 偏移（默认 0）",
                    },
                    "use_original_coordinates": {
                        "type": "boolean",
                        "description": "已废弃：标注请用 calibration_observations 校准，勿传 true",
                    },
                    "calibration_probe": {
                        "type": "boolean",
                        "description": "兜底预览，首轮默认勿用。true 时仅输出带绿色校准线的探测图 + calibration_reference，不做业务标注。",
                    },
                    "calibration_observations": {
                        "type": "array",
                        "description": (
                            "校准观测：每项 {id,x,y}，id 来自 calibration_reference，"
                            "x/y 为你所见画面中该校准线左上角的像素（与 annotations 同坐标系）。至少 2 条。"
                        ),
                        "items": {"type": "object"},
                    },
                    "annotations": {
                        "type": "array",
                        "description": (
                            "标注列表。**默认关键点模式**：一次列全所有目标，优先 `{\"type\":\"crosshair\",\"x\":12.5,\"y\":24}` "
                            "或 `{\"type\":\"target\",\"x\":50,\"y\":45}`。`callout` 用 anchor_x/anchor_y 指目标、label_x/label_y 放文字。"
                            "若 coordinate_space=\"percent\"，这些位置字段必须是 0–100 百分比；后端会统一转换成像素，不要自己把百分比当像素传。"
                            "标对象时 x/y 必须落在真实可见对象中心，不要落在搜索框、输入框、标题栏或搜索按钮上。"
                            "标主机名/字段名/标题等小文本时，x/y 或 anchor_x/anchor_y 应落在文字中心/基线，不要落到浏览器地址栏或页面上方空白。"
                            "需要区域时才用 rect 的 x/y/width/height 且只设 outline；勿用实心 highlight 标菜单。"
                            "兜底才用 `cells`：`{\"type\":\"rect\",\"cells\":[编号,...],\"outline\":\"#ff0000\"}`。"
                            "其它字段：type, x/y/anchor_x/anchor_y/label_x/label_y/width/height 或 points, fill/outline/color, opacity(0~1 或 0~100), line_width, radius, arm_length, gap_radius。"
                            "半透明遮罩用 type=overlay|mask|highlight + fill + opacity（勿用于单行菜单）。"
                            "侧边栏菜单等窄目标优先 crosshair/callout/target（x/y 为标点，无 width/height）或细框 rect 仅 outline。"
                            "设 allow_large=true 可跳过 tight_boxes 收紧。"
                        ),
                        "items": {"type": "object"},
                    },
                    "session_id": {"type": "integer", "description": "可选；绑定到新附件的会话 id"},
                },
                "required": ["uuid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_chat_attachments",
            "description": "列出当前用户已上传到 AI 聊天的附件（可按 session_id 过滤），便于 AI 了解可读取的参考材料；仅返回本人附件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "integer", "description": "可选；仅列该会话已绑定的附件"},
                    "limit": {"type": "integer", "description": "最大返回条数，默认 50，最大 200"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_chat_artifact",
            "description": (
                "创建一个**可下载的成果物**（artifact），用于把 AI 生成/整理好的报告、数据、"
                "可视化页面等一次性交付给用户。"
                "落盘布局（相对工作区根，系统自动分配，**勿手拼**）："
                "`reports/<UTC年>/<月>/<日>/<示意目录名>/<uuid>.<扩展名>`，"
                "依赖与其它资源在同目录下的 `libs/`、`images/` 等。"
                "返回的 `fs_path` / `storage_subdir` 可供后续引用。"
                "对话里生成**下载/预览卡片**（bundle 自动打包成 .tgz）。\n\n"
                "**何时使用**：\n"
                "- 用户要求导出报告（csv / markdown / json / html / pdf 等）；\n"
                "- 需要交付一份包含多个文件的结果（html + images/ + js/ + data.json 等）；\n"
                "- 希望结果持久保留、可再次下载，而不是仅在聊天里以文本/代码块展示。\n\n"
                "**首次创建**用本工具；用户要求**修改已有报告/成果物**时改用 **`update_chat_artifact`**（同一 UUID），"
                "不要重复 create。\n\n"
                "**参数（JSON 结构，勿把 HTML 字符串直接当作 files）**：\n"
                "- `title`（必填）：成果物标题；\n"
                "- `description`（可选）：一句话说明；\n"
                "- `files`（必填，**非空数组** `[{path, content}, ...]`）：\n"
                "    **正确**：`\"files\": [{\"path\": \"index.html\", \"content\": \"<!doctype html>...\"}]`\n"
                "    **错误**：`\"files\": \"<html>...\"`（字符串）、`\"files\": {\"path\":...}`（缺外层 []）、`\"files\": []`\n"
                "    每项字段：\n"
                "    - `path`：相对路径，支持子目录（如 `images/chart.png`）；必须带扩展名；\n"
                "    - `content`：文本字符串 / 或 base64 编码后的字符串 / 或 dict/list（自动 JSON 序列化）；\n"
                "    - `encoding`：`utf-8`（默认，文本）或 `base64`（二进制，如 png 图片）；\n"
                "- `entry_file`（可选）：推荐入口文件（如 `index.html` / `report.md`）；不传时自动挑一个。\n\n"
                "**限制**：单 artifact 最多 "
                f"{int(getattr(__import__('config'), 'ARTIFACT_MAX_FILES', 200))} 个文件，"
                "总大小≤"
                f"{int(getattr(__import__('config'), 'ARTIFACT_MAX_TOTAL_BYTES', 200 * 1024 * 1024)) // (1024 * 1024)} MB，"
                "允许扩展名：md / txt / csv / json / yaml / html / css / js / png / jpg / gif / svg / pdf 等常见类型。\n\n"
                "**HTML 自包含依赖**（重点）：需要 echarts / mermaid / markmap / d3 / html-to-image / **three** / cannon-es 时，"
                "**不要**在 files 里塞 vendor JS，也**禁止** `cdn.jsdelivr.net` / `unpkg` 等外网 CDN；"
                "调用时加 `libs: [\"three\", ...]`，后端从 `web/res/` 复制到 artifact 的 `libs/`。"
                "three：简单场景用 `./libs/three.min.js`（全局 THREE）；需要 OrbitControls/CSS2D/GLTF 时用 "
                "`importmap` 映射 `\"three\"`→`./libs/three.module.js`、`\"three/addons/\"`→`./libs/jsm/`，"
                "再 `import { OrbitControls } from 'three/addons/controls/OrbitControls.js'`。"
                "预览/新窗口由平台改写鉴权 URL，相对路径即可。snippet 见「本地资源包」或返回的 `libs_provided.snippets`。\n\n"
                "**调用结果**：成功后返回 `{success, artifact: {uuid, title, kind, download_url, markdown_link, "
                "fs_path, storage_subdir, entry_file, libs_provided?, ...}}`。"
                "请在随后的**最终答复里**把 `markdown_link` 字段**原样**贴出（形如 `[标题](artifact:UUID)`）；"
                "**禁止**在工具成功返回之前、或 success 为 false 时编造 `artifact:` 链接 / UUID。"
                "前端会自动把它渲染为带大小/入口文件的下载按钮；不要改写链接格式、也不要附加其它 URL。"
                "若需在工作区再次引用该报告，使用返回的 `fs_path`（勿臆造 chats 日期路径）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "成果物标题（必填；同时用于示意目录名）"},
                    "description": {"type": "string", "description": "可选的简短描述"},
                    "entry_file": {"type": "string", "description": "可选的入口文件名（在 files 的 path 集合中）"},
                    "files": {
                        "type": "array",
                        "description": (
                            "必填。非空 JSON 数组 [{path, content, encoding?}, ...]，至少 1 项。"
                            "禁止传字符串、禁止传单个对象代替数组、禁止 []。"
                            "单文件 HTML 示例：[{\"path\":\"index.html\",\"content\":\"<!doctype html>...\"}]"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": "相对路径（必带扩展名），允许子目录"},
                                "content": {
                                    "description": "文件内容：文本字符串 / base64 字符串 / 或 dict/list 自动序列化为 JSON",
                                },
                                "encoding": {"type": "string", "description": "'utf-8'（默认）或 'base64'（二进制）"},
                            },
                            "required": ["path", "content"],
                        },
                        "minItems": 1,
                    },
                    "libs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "可选；按 `web/res/manifest.json` 中的包名声明本 artifact 需要哪些前端依赖（如 "
                            "['echarts','mermaid','markmap']）。后端会把对应 .js/.css 复制到 artifact 的 "
                            "`libs/` 子目录（默认），HTML 用相对路径 `./libs/<file>` 引用即可，无需联网。"
                        ),
                    },
                    "libs_subdir": {
                        "type": "string",
                        "description": (
                            "依赖文件复制目标子目录名，默认 'libs'；传 '' 表示扁平复制到 artifact 根目录"
                            "（适合只有一个 HTML、想要 `./echarts.min.js` 同级引用的场景）。"
                        ),
                    },
                },
                "required": ["title", "files"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_chat_artifact",
            "description": (
                "**在原成果物上修订**（覆盖/追加文件），**不新建** artifact、**不换 UUID**。"
                "当用户说「改一下报告」「时间不对」「修个小问题」「在原来的 HTML 上改」时，"
                "**必须**用本工具，**禁止**再调 `create_chat_artifact` 整份重生成。\n\n"
                "**推荐流程**：\n"
                "1. `list_chat_artifacts`（或从上一轮 `artifact:UUID` 链接）定位 uuid；\n"
                "2. `read_chat_artifact_file(uuid, path)` 读现有 `index.html` 等；\n"
                "3. 只改必要片段后 `update_chat_artifact(uuid, files=[{path, content}])`；\n"
                "4. 答复里**仍用同一** `[标题](artifact:原UUID)` 链接，说明「已在原报告上更新」。\n\n"
                "若预览缺 `libs/jsm/...` 等 vendor：可 `update_chat_artifact(uuid, libs:[\"three\"])` 按当前 manifest 补拷（可与 files 同用；"
                "仅补依赖时 files 可省略）。\n"
                "小改动也可用 `fs_write_file` 写返回的 `fs_path`（`reports/…/<uuid>.<ext>`），但需已知精确路径；"
                "优先 uuid 流程。**禁止**手拼 chats 日期目录。\n"
                "参数：`uuid`（必填）；`files` 与 `libs` 至少其一；"
                "可选 `title` / `description` / `entry_file` / `libs_subdir`。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uuid": {"type": "string", "description": "要更新的 artifact UUID（与下载卡片相同）"},
                    "files": {
                        "type": "array",
                        "description": "要覆盖或新增的文件列表，格式同 create_chat_artifact；与 libs 至少提供其一",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                                "encoding": {"type": "string", "enum": ["utf-8", "base64"]},
                            },
                            "required": ["path", "content"],
                        },
                    },
                    "libs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "按 manifest 补拷/更新 vendor 到 libs/（如 [\"three\"]），可与 files 同用",
                    },
                    "libs_subdir": {
                        "type": "string",
                        "description": "依赖复制子目录，默认 libs；传空串表示扁平到 artifact 根",
                    },
                    "title": {"type": "string", "description": "可选，更新标题"},
                    "description": {"type": "string", "description": "可选，更新描述"},
                    "entry_file": {"type": "string", "description": "可选，更新入口文件路径（须已存在）"},
                },
                "required": ["uuid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_chat_artifacts",
            "description": (
                "列出已生成的 artifacts。在聊天 Agent 中**省略 session_id 时默认只列当前会话**；"
                "显式传 session_id=0 可查看本人全部会话成果。仅返回本人数据。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "integer",
                        "description": "可选；省略=当前会话；传 0=本人全部会话",
                    },
                    "limit": {"type": "integer", "description": "最大返回条数，默认 50，最大 200"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_chat_artifact_file",
            "description": (
                "读取某个 artifact 内部的一个文件文本内容（仅支持 text/markdown/json/csv/html/css/js 等文本扩展名），"
                "便于 AI **在原成果物上二次修订**（配合 `update_chat_artifact`）或复核生成结果。超长内容会按 max_chars 截断。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uuid": {"type": "string", "description": "artifact UUID"},
                    "path": {"type": "string", "description": "artifact 内相对文件路径"},
                    "max_chars": {"type": "integer", "description": "文本最大返回字符数，默认 20000"},
                },
                "required": ["uuid", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user_choice",
            "description": (
                "向用户提一道**选择题**并附带可点击的选项按钮（如 ABCD 多选、是/否同意、确认/取消、风险动作前的二次确认等），"
                "便于用户**直接点击作答**，同时仍允许用户用文字补充。\n\n"
                "**何时优先使用**：\n"
                "- **排障 / 分析结论后**：若你写出「方案 A / 方案 B」或列出多条**互斥**的下一步（改配置、重启服务、回滚、换路径等）并问用户「需要如何处理」「选哪一种」，在 毛竹（Moso）**网页**会话中**必须**用本工具呈现按钮，**禁止**只用 Markdown 列表代替；\n"
                "- 关键/破坏性操作前征求确认（rm -rf、重启服务、覆盖文件、回滚、执行已生成的脚本等）；\n"
                "- 在多个明确候选中让用户选择（多个候选主机、多个修复方案、多个版本、多种安装路径等）；\n"
                "- 是/否、同意/拒绝、确认/取消 这类二元决策；\n"
                "- 当问题可以在 ≤ 6 个明确选项中收敛时，**优先用本工具**而不是纯文本提问，可显著降低用户输入成本。\n\n"
                "**何时不要用**：\n"
                "- 当系统提示词明确告知当前是「API/OpenClaw 集成」「定时任务/触发任务」「无 UI 后台」等模式——"
                "这些环境无法渲染按钮，请改用纯文本列出选项 (A/B/C/D) 让用户文字回复，或在该上下文里自行判断；\n"
                "- 仅做信息展示而无须用户决策时；\n"
                "- 你已经有足够信息可以直接执行时（不要为\"形式\"而强行加确认）。\n\n"
                "**调用后必须遵守**：\n"
                "1. 立即**结束本轮回复**等待用户下一条消息；\n"
                "2. 不要在同一轮里继续调用别的工具或代用户作答；\n"
                "3. 用户的下一条消息可能是按钮回传文本，也可能是自由文本——按字面理解即可；\n"
                "4. 工具返回中带 `ui_action`，前端会自动渲染按钮；如果系统返回 `ui_capable=false` 字段，则按钮不会显示，"
                "请在你随后的文字回复里把选项以 [A] [B] … 形式列出。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "要呈现给用户的问题文本（必填，简明）"},
                    "options": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 12,
                        "description": "可点击选项（至少 2 项，建议 ≤ 6 项）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "选项标识；不传则自动用 A/B/C…"},
                                "label": {"type": "string", "description": "按钮显示文本（必填）"},
                                "description": {"type": "string", "description": "可选，按钮下方副文本，帮助用户理解该选项含义"},
                                "value": {"type": "string", "description": "用户点击该按钮回传给 AI 的文本，不传则自动用 label"},
                                "style": {
                                    "type": "string",
                                    "enum": ["default", "primary", "danger", "success"],
                                    "description": "按钮风格：danger=高风险动作（红）、success/primary=确认（绿/蓝）、default=普通灰",
                                },
                            },
                            "required": ["label"],
                        },
                    },
                    "allow_multiple": {"type": "boolean", "description": "是否允许多选（默认 false 单选）；多选时前端会渲染 checkbox + 提交按钮"},
                    "allow_text": {"type": "boolean", "description": "是否允许用户除点选外再用文本补充（默认 true）"},
                    "default_id": {"type": "string", "description": "可选，默认选中的选项 id（仅作前端高亮提示，不会自动提交）"},
                },
                "required": ["question", "options"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_server_time",
            "description": "获取 毛竹（Moso）服务器当前时间与站点显示时区。任意登录用户可调用。返回 site_timezone（IANA，默认 Asia/Shanghai）、server_time_local、server_time_utc。用于回答「现在几点」「系统时间」「什么时区」；勿编造时间。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "regex_process",
            "description": "通用正则处理工具。用于对文本做 regex search/findall/extract/split/replace/count，适合日志、配置、长文本、批量数据中的模式提取与替换预览。只返回结果，不修改任何文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["search", "findall", "extract", "split", "replace", "count"], "description": "处理动作"},
                    "text": {"type": "string", "description": "待处理文本"},
                    "pattern": {"type": "string", "description": "Python 正则表达式"},
                    "replacement": {"type": "string", "description": "replace 时使用的替换文本，可使用 \\1 或命名组引用"},
                    "flags": {"type": "array", "items": {"type": "string"}, "description": "可选：ignorecase/multiline/dotall/ascii"},
                    "max_results": {"type": "integer", "description": "最多返回多少项，默认 100，最大 1000"},
                    "count": {"type": "integer", "description": "replace/split 的最大次数，默认 0 表示不限制"},
                },
                "required": ["operation", "text", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "string_process",
            "description": "通用字符串处理工具。支持 trim/case/replace/split/join/substring/contains/count/line_stats/base64/url/hash 等，只返回结果，不修改文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["trim", "case", "replace", "split", "join", "substring", "contains", "count", "line_stats", "base64_encode", "base64_decode", "url_encode", "url_decode", "hash"], "description": "处理动作"},
                    "text": {"type": "string", "description": "待处理文本"},
                    "items": {"type": "array", "items": {"type": "string"}, "description": "join 时使用的字符串数组"},
                    "old": {"type": "string", "description": "replace/count/contains 的目标字符串"},
                    "new": {"type": "string", "description": "replace 的替换字符串"},
                    "sep": {"type": "string", "description": "split/join 分隔符，默认换行或空字符串按场景处理"},
                    "start": {"type": "integer", "description": "substring 起始位置"},
                    "end": {"type": "integer", "description": "substring 结束位置"},
                    "case": {"type": "string", "enum": ["lower", "upper", "title", "capitalize", "swap"], "description": "case 操作类型"},
                    "algorithm": {"type": "string", "enum": ["md5", "sha1", "sha256", "sha512"], "description": "hash 算法，默认 sha256"},
                },
                "required": ["operation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crypto_toolkit",
            "description": "密码与证书工具。支持哈希（MD5/SHA1/SHA256/SHA384/SHA512）、文本/HEX/二进制(base64)互转、AES/DES(3DES)加解密、RSA/ECC 签名验签与密钥生成、自签名证书生成、证书解析、证书签名校验、证书与私钥匹配校验。仅做计算，不改文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "description": "操作名，如 hash/text_to_hex/hex_to_text/bytes_to_hex/hex_to_bytes/aes_encrypt/aes_decrypt/des_encrypt/des_decrypt/rsa_generate_key/rsa_sign/rsa_verify/ecc_generate_key/ecc_sign/ecc_verify/x509_generate_self_signed/x509_parse/x509_verify_signature/x509_match_key"},
                    "text": {"type": "string", "description": "文本输入（按 UTF-8 编码）"},
                    "hex": {"type": "string", "description": "HEX 字符串输入"},
                    "data": {"type": "string", "description": "base64 数据输入（operation 需要二进制时）"},
                    "algorithm": {"type": "string", "description": "算法，如 md5/sha1/sha256/sha384/sha512/aes-cbc/aes-gcm/des-cbc/rsa/ecc"},
                    "key": {"type": "string", "description": "密钥；对称算法传 base64 或 hex（由 key_encoding 指定），非对称算法传 PEM"},
                    "key_encoding": {"type": "string", "description": "对称密钥编码：base64 或 hex，默认 base64"},
                    "iv": {"type": "string", "description": "IV/nonce（base64 或 hex）"},
                    "iv_encoding": {"type": "string", "description": "IV 编码：base64 或 hex，默认 base64"},
                    "aad": {"type": "string", "description": "AES-GCM 的 AAD（UTF-8）"},
                    "tag": {"type": "string", "description": "AES-GCM 解密时认证标签（base64 或 hex）"},
                    "encoding": {"type": "string", "description": "输入/输出编码偏好：base64 或 hex，默认 base64"},
                    "private_key_pem": {"type": "string", "description": "PEM 私钥"},
                    "public_key_pem": {"type": "string", "description": "PEM 公钥"},
                    "signature": {"type": "string", "description": "签名值（base64 或 hex）"},
                    "curve": {"type": "string", "description": "ECC 曲线，如 secp256r1/secp384r1/secp521r1"},
                    "key_size": {"type": "integer", "description": "RSA 位数，默认 2048"},
                    "certificate_pem": {"type": "string", "description": "PEM 证书"},
                    "issuer_cert_pem": {"type": "string", "description": "签发者 PEM 证书（用于验签）"},
                    "subject_cn": {"type": "string", "description": "证书主题 CN"},
                    "dns_names": {"type": "array", "items": {"type": "string"}, "description": "SAN DNS 列表"},
                    "days_valid": {"type": "integer", "description": "证书有效期天数，默认 365"},
                },
                "required": ["operation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "math_calculate",
            "description": (
                "安全数学/科学计算（math + **NumPy** + **SymPy**）。"
                "支持标量表达式、数组统计、单位换算、**数据集批量计算**与符号运算。\n\n"
                "**批量计算（推荐）**：`operation=batch`，提供 `dataset` + `expression`（或 `algorithm`）：\n"
                "- 行模式：`dataset=[{x:1,y:2,bonus:0.1}, …]`，`expression=\"x*y+bonus\"` → 返回 `results` 与 `dataset_with_results`\n"
                "- 列向量模式：`dataset={x:[1,2,3], y:[4,5,6]}`，同一表达式向量化求值（大数据更快）\n"
                "- 可选 `output_column` 指定结果列名\n\n"
                "**其它 operation**：`eval` 标量；`stats` 数组统计；`unit_convert` 单位；"
                "`numpy` 数组表达式（可传 `numbers`）；`symbolic` 符号（simplify/expand/factor/diff/integrate/solve/limit/subs）。\n"
                "勿执行任意 Python；复杂公式优先用本工具而非心算。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["eval", "stats", "unit_convert", "numpy", "batch", "batch_vector", "symbolic"],
                        "description": "eval|stats|unit_convert|numpy|batch|batch_vector|symbolic",
                    },
                    "expression": {"type": "string", "description": "数学/符号表达式，或 batch 的算法公式（引用 dataset 列名）"},
                    "algorithm": {"type": "string", "description": "同 expression，batch 时可用此别名"},
                    "dataset": {
                        "description": (
                            "batch 数据集：行数组 [{field: value}] 或列字典 {col: [v1,v2,...]}；"
                            "最多 10000 行 / 64 列"
                        ),
                    },
                    "data": {"description": "dataset 别名"},
                    "mode": {"type": "string", "enum": ["auto", "rows", "vector"], "description": "batch 模式，默认 auto"},
                    "output_column": {"type": "string", "description": "batch 结果列名，默认 result"},
                    "numbers": {"type": "array", "items": {"type": "number"}, "description": "stats/numpy 用数字数组"},
                    "value": {"type": "number", "description": "unit_convert 输入数值"},
                    "from_unit": {"type": "string", "description": "源单位"},
                    "to_unit": {"type": "string", "description": "目标单位"},
                    "symbolic_op": {
                        "type": "string",
                        "enum": ["simplify", "expand", "factor", "diff", "integrate", "solve", "limit", "subs"],
                        "description": "symbolic 子操作",
                    },
                    "variables": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "symbolic 符号变量名列表",
                    },
                    "wrt": {"type": "string", "description": "diff/integrate/limit 对哪个变量"},
                    "point": {"description": "limit 趋近点"},
                    "substitutions": {"type": "object", "description": "subs 代入，如 {\"x\": 1, \"y\": 2}"},
                },
                "required": ["operation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "data_query",
            "description": "JSON/YAML 数据搜索与分析工具。支持解析、结构摘要、按路径读取（如 users[0].name）、递归搜索 key/value、简单列表过滤。适合快速分析配置、接口返回、日志结构化数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["parse", "summary", "get_path", "search", "filter_list"], "description": "处理动作"},
                    "data": {"type": "string", "description": "JSON/YAML 原文"},
                    "format": {"type": "string", "enum": ["auto", "json", "yaml"], "description": "输入格式，默认 auto"},
                    "path": {"type": "string", "description": "get_path/filter_list 的路径，如 items[0].name 或 data.items"},
                    "query": {"type": "string", "description": "search 的关键字或正则"},
                    "regex": {"type": "boolean", "description": "search 是否按正则匹配，默认 false"},
                    "key": {"type": "string", "description": "filter_list 对列表元素字典的字段名"},
                    "op": {"type": "string", "enum": ["eq", "ne", "contains", "regex", "gt", "gte", "lt", "lte"], "description": "filter_list 比较方式"},
                    "value": {"description": "filter_list 比较值"},
                    "max_results": {"type": "integer", "description": "最多返回多少项，默认 100，最大 1000"},
                },
                "required": ["operation", "data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "markup_query",
            "description": "XML/HTML 搜索与提取工具。支持结构摘要、按标签查找、CSS 选择器（HTML）、文本搜索、属性提取、链接/图片提取。只返回结果，不修改文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["summary", "find_tags", "select", "search_text", "get_text", "extract_attrs", "extract_links"], "description": "处理动作"},
                    "data": {"type": "string", "description": "XML/HTML 原文"},
                    "format": {"type": "string", "enum": ["auto", "html", "xml"], "description": "输入格式，默认 auto"},
                    "tag": {"type": "string", "description": "find_tags/extract_attrs 的标签名，如 div、a、item"},
                    "selector": {"type": "string", "description": "HTML CSS 选择器，如 div.card a[href]；XML 下 select 支持 ElementTree 简单路径"},
                    "query": {"type": "string", "description": "search_text 的关键字或正则"},
                    "regex": {"type": "boolean", "description": "search_text 是否按正则匹配，默认 false"},
                    "attrs": {"type": "array", "items": {"type": "string"}, "description": "要提取的属性名，如 href、src、class"},
                    "max_results": {"type": "integer", "description": "最多返回多少项，默认 100，最大 1000"},
                },
                "required": ["operation", "data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_settings",
            "description": "获取系统设置键值列表（含 ai_api_key、ai_base_url、site_timezone（IANA 显示时区）、ai_model、ai_system_prompt、self_register、login_announcement_md 登录页公告等）。需管理员。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_setting",
            "description": "更新系统设置项（key、value）。需管理员。敏感键（如 ai_api_key）传空值不会覆盖原值。设置 **site_timezone** 时用合法 IANA 名称（如 Asia/Shanghai、Europe/Berlin、UTC），用于全站列表与界面按此时区显示时间。可用 **login_announcement_md** 写入登录页右侧顶部公告，内容支持 Markdown。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_logs",
            "description": "查询操作日志（谁、何时、操作、参数、结果）。普通用户仅看自己的；管理员可按 host_id/user_id 筛选。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "最多返回条数，默认 100"},
                    "host_id": {"type": "integer"},
                    "user_id": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_logs",
            "description": "清空操作日志。普通用户仅清空自己的；管理员可清空全部或指定 user_id 的日志。",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "integer", "description": "管理员可选：指定则仅清空该用户的日志"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_batches",
            "description": "清空批量任务记录。普通用户仅清空自己创建的；管理员清空全部。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_create",
            "description": (
                "创建批量操作并向多台主机下发执行（后台异步）。支持：run_command、scp_push、scp_pull、run_script、restart。"
                "脚本与资源放在 web/fs。创建后用 **list_batch_operations** / **get_batch_detail** 轮询状态，"
                "直至 status 为 completed/cancelled。普通用户可对自己可见主机（含收到分享）发起；scope_type=tag 可按标签筛选。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation_type": {
                        "type": "string",
                        "enum": ["run_command", "scp_push", "scp_pull", "run_script", "restart"],
                        "description": (
                            "run_command=固定命令；scp_push=工作区→多机；scp_pull=多机→工作区（按 host_id 分子目录）；"
                            "run_script=上传并执行脚本；restart=重启"
                        ),
                    },
                    "scope_type": {"type": "string", "enum": ["all", "group", "selected", "tag"], "description": "all=可见全部主机；group=指定分组；selected=指定主机 ID 列表；tag=指定标签 ID 列表"},
                    "scope_value": {"type": "array", "items": {"type": "integer"}, "description": "scope_type 为 group 时填分组 ID，为 selected 时填主机 ID，为 tag 时填标签 ID"},
                    "tag_match_mode": {"type": "string", "enum": ["any", "all"], "description": "仅 scope_type=tag 生效：any=命中任一标签，all=必须同时命中全部标签；默认 any"},
                    "params": {
                        "type": "object",
                        "description": (
                            "run_command: command, timeout?；"
                            "scp_push: remote_path, content? 或 local_path(相对工作区), recursive?, timeout?；"
                            "scp_pull: remote_path, local_path?(默认 batch_pulls/<batch_id>/，每机落 {local}/{host_id}/…), recursive?, timeout?, max_bytes?；"
                            "run_script: script_path, remote_path?, timeout?；restart: command?"
                        ),
                    },
                    "task_host_id": {"type": "integer", "description": "可选，任务日志写入所用主机 ID（用于定位 ~/.edgeops）"},
                    "task_dir_name": {"type": "string", "description": "可选，任务目录名；高风险批量任务创建后自动追加日志"},
                },
                "required": ["operation_type", "scope_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_batch_operations",
            "description": (
                "列出最近批量操作（id、operation_type、status、total/success/fail/pending、created_at）。"
                "用于在 AI 对话中查询批量任务总览；细节用 get_batch_detail。"
            ),
            "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "最多条数，默认 20"}}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_batch_detail",
            "description": (
                "获取单次批量操作详情与进度：batch.status、progress 摘要、每台主机 status/result。"
                "创建 batch 后应轮询本工具直至 completed/cancelled，再向用户汇报成败与 local_path。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"batch_id": {"type": "integer", "description": "批量操作 ID"}},
                "required": ["batch_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_cancel",
            "description": "取消正在运行的批量任务（将未执行项标为 skipped）。仅可操作本人创建的任务（管理员可操作任意）。",
            "parameters": {"type": "object", "properties": {"batch_id": {"type": "integer"}}, "required": ["batch_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_retry",
            "description": "将指定批量任务中失败项重置为 pending 以便重试。仅可操作本人创建的任务（管理员可操作任意）。",
            "parameters": {"type": "object", "properties": {"batch_id": {"type": "integer"}}, "required": ["batch_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ai_config",
            "description": "获取当前激活的 AI 模型配置（api_key 脱敏为 ***），并返回全部模型配置组列表 profiles 与 active_profile_id。新增/切换模型请用 create_ai_model_profile、activate_ai_model_profile；仅改当前激活项可用 update_ai_config 或 update_ai_model_profile。管理员可传 user_id。",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "integer", "description": "仅管理员：要查看的用户 ID，不传则当前用户"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_ai_config",
            "description": "更新当前激活的模型配置（不会新建配置组；未传字段保持原值）。若需添加新的候选模型但不切换，请用 create_ai_model_profile(set_active=false)；切换当前模型请用 activate_ai_model_profile。管理员可传 user_id。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "仅管理员：要更新的用户 ID，不传则当前用户"},
                    "api_key": {"type": "string"},
                    "base_url": {"type": "string"},
                    "model": {"type": "string"},
                    "system_prompt": {"type": "string"},
                    "auto_approve": {"type": "boolean"},
                    "assistant_enabled": {"type": "boolean", "description": "是否启用辅助 AI 驱动继续执行"},
                    "context_size": {"type": "integer", "description": "聊天上下文总字符数上限，0 表示不限制"},
                    "provider": {"type": "string", "enum": ["aliyun", "ollama", "openai"], "description": "AI 源类型，空表示按 base_url 自动探测"},
                    "agent_max_steps": {"type": "integer", "description": "Agent 内层最大步数（单轮内调用工具/思考次数）。0 表示沿用全局默认（默认 100），上限 1000。"},
                    "assistant_max_rounds": {"type": "integer", "description": "辅助 AI 外层最大轮次（连续助手轮次）。0 表示沿用全局默认（默认 100），上限 1000。"},
                    "vision_enabled": {"type": "boolean", "description": "所配模型是否支持图像识别（多模态视觉输入）。默认 true；关闭后后端不再把图片按 OpenAI image_url 段内联到 user 消息（改为只挂 📎 附件清单，让 AI 用 read_chat_attachment 拿 data_url 兜底）。"},
                    "output_locale": {"type": "string", "enum": ["", "en", "zh-CN"], "description": "无法从用户输入判断语言时使用的个人默认；空表示不设，走站点/界面语言链路。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_ai_model_profiles",
            "description": "列出用户的全部 AI 模型配置组（名称、model、base_url、provider、是否当前激活等；不含完整 api_key）。管理员可传 user_id。",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "integer", "description": "仅管理员：目标用户 ID"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_ai_model_profile",
            "description": "新建一组 AI 模型配置并加入模型列表。默认不切换为当前模型（set_active=false）；仅当用户尚无任何配置组时系统会自动激活首个。用于「添加候选模型但不立即使用」。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "仅管理员：目标用户 ID"},
                    "name": {"type": "string", "description": "配置组名称（用户内唯一，必填）"},
                    "set_active": {"type": "boolean", "description": "创建后是否立即设为当前模型，默认 false"},
                    "api_key": {"type": "string"},
                    "base_url": {"type": "string"},
                    "model": {"type": "string"},
                    "system_prompt": {"type": "string"},
                    "auto_approve": {"type": "boolean"},
                    "assistant_enabled": {"type": "boolean"},
                    "context_size": {"type": "integer"},
                    "provider": {"type": "string", "enum": ["aliyun", "ollama", "openai", ""]},
                    "agent_max_steps": {"type": "integer"},
                    "assistant_max_rounds": {"type": "integer"},
                    "vision_enabled": {"type": "boolean"},
                    "output_locale": {"type": "string", "enum": ["", "en", "zh-CN"]},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_ai_model_profile",
            "description": "按 profile_id 或 profile_name 更新指定模型配置组（未传字段不变）。不会自动切换为当前模型。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "仅管理员：目标用户 ID"},
                    "profile_id": {"type": "integer", "description": "配置组 ID（与 profile_name 二选一）"},
                    "profile_name": {"type": "string", "description": "配置组名称（与 profile_id 二选一）"},
                    "name": {"type": "string", "description": "重命名配置组"},
                    "api_key": {"type": "string"},
                    "base_url": {"type": "string"},
                    "model": {"type": "string"},
                    "system_prompt": {"type": "string"},
                    "auto_approve": {"type": "boolean"},
                    "assistant_enabled": {"type": "boolean"},
                    "context_size": {"type": "integer"},
                    "provider": {"type": "string", "enum": ["aliyun", "ollama", "openai", ""]},
                    "agent_max_steps": {"type": "integer"},
                    "assistant_max_rounds": {"type": "integer"},
                    "vision_enabled": {"type": "boolean"},
                    "output_locale": {"type": "string", "enum": ["", "en", "zh-CN"]},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "activate_ai_model_profile",
            "description": "将指定模型配置组设为当前使用的模型（切换模型）。按 profile_id 或 profile_name 指定。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "仅管理员：目标用户 ID"},
                    "profile_id": {"type": "integer", "description": "配置组 ID（与 profile_name 二选一）"},
                    "profile_name": {"type": "string", "description": "配置组名称（与 profile_id 二选一）"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_system_ai_config_to_user",
            "description": "将系统默认 AI 配置（全局设置中的 Key、URL、模型等）直接写入指定用户的 AI 配置。仅管理员可用。用于为某用户一键应用与系统相同的配置。",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "integer", "description": "要应用系统配置的用户 ID"}},
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_me",
            "description": "获取当前登录用户信息（id、username、display_name、role、email）及个人发信能力（mail_config）；同时返回 site_timezone、server_time_local、server_time_utc（站点配置时区下的当前时刻）。验证码绑定邮箱走 send_bind_email_code（使用系统 SMTP）；用户自己对外发邮件走 send_email（使用个人 SMTP）。问「现在几点」优先用 get_server_time。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_mail_settings",
            "description": "获取当前用户个人发信（SMTP）配置摘要：是否启用、各项是否已填写、是否具备发信条件（may_send_mail）。密码永不返回。用于回答「我能不能发邮件」「发信配了吗」等问题。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_user_mail_settings",
            "description": "更新当前用户的个人 SMTP 发信配置。默认邮件功能关闭；仅当 smtp_host、smtp_user、smtp_password、smtp_from、端口等填写完整时才可将 mail_enabled 设为 true。用于帮用户在对话中填写发信参数。个人发信与管理员「系统设置」里的全局 SMTP 无关；绑定邮箱验证码仍走系统邮件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "mail_enabled": {"type": "boolean", "description": "是否启用个人发信；true 前须配置完整"},
                    "smtp_host": {"type": "string"},
                    "smtp_port": {"type": "integer", "description": "如 587、465"},
                    "smtp_user": {"type": "string"},
                    "smtp_password": {"type": "string", "description": "敏感；若不想改密码可省略本字段"},
                    "smtp_from": {"type": "string", "description": "发件人地址，通常与邮箱一致"},
                    "smtp_use_tls": {"type": "boolean", "description": "STARTTLS，587 常用"},
                    "smtp_use_ssl": {"type": "boolean", "description": "SSL 直连，465 常用"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": (
                "使用**当前用户已启用且配置完整**的个人 SMTP 发送邮件。默认 **纯文本**（`body`）；"
                "可同时或单独提供 **HTML 正文**（`body_html`）以排版表格/标题/链接；可附 **多个附件**。"
                "若 may_send_mail 为 false 则拒绝并提示配置「我的发信设置」。多个收件人用英文逗号分隔。\n\n"
                "**推荐用法**：\n"
                "- 简单通知：仅 `body`（plain text）。\n"
                "- 运维报告/巡检结果：提供 `body`（纯文本摘要）+ `body_html`（带 `<table>`、`<h2>` 的完整 HTML）；"
                "大文件用 `attachments[].local_path` 指向 web/fs 下已生成的 csv/pdf/tgz。\n"
                "- 附件来源：优先 `local_path`（web/fs 相对路径，如 `chats/2026/05/19/xxx-report.csv`）；"
                "小文件可用 `content` + `encoding`（`utf-8` 或 `base64`）。\n"
                "- 同时提供 `body` 与 `body_html` 时，邮件客户端会显示 HTML 版，纯文本客户端仍可读 `body`。\n"
                "- 仅 HTML 时可只传 `body_html`，但建议仍给简短 `body` 作 fallback。\n"
                "正文软上限约 50 万字；附件默认单文件 25MB、合计 50MB、最多 10 个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "收件人邮箱，多个用逗号分隔"},
                    "subject": {"type": "string", "description": "邮件主题"},
                    "body": {
                        "type": "string",
                        "description": "纯文本正文（默认格式；与 body_html 至少填一项；建议作为 HTML 邮件的 plain 摘要）",
                    },
                    "body_html": {
                        "type": "string",
                        "description": "可选 HTML 正文。完整 HTML 片段或整页均可；勿依赖外网 CDN 图片/脚本。表格报告、彩色告警请用此项。",
                    },
                    "attachments": {
                        "type": "array",
                        "description": "可选附件列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "filename": {"type": "string", "description": "附件文件名（含扩展名）；local_path 时可省略"},
                                "local_path": {
                                    "type": "string",
                                    "description": "web/fs 下相对路径，读取该文件作为附件（推荐 csv/pdf/png/tgz 等）",
                                },
                                "content": {"type": "string", "description": "附件内容（与 local_path 二选一）"},
                                "encoding": {
                                    "type": "string",
                                    "description": "content 编码：utf-8（默认，文本）或 base64（二进制）",
                                },
                                "mime_type": {"type": "string", "description": "可选 MIME，如 text/csv、application/pdf"},
                            },
                        },
                    },
                },
                "required": ["to", "subject"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_bind_email_code",
            "description": "向指定邮箱发送绑定邮箱的 6 位验证码，仅对当前登录用户有效。用户需先提供邮箱，调用此工具后告知用户去收邮件，再让用户提供验证码并调用 verify_bind_email 完成绑定。",
            "parameters": {
                "type": "object",
                "properties": {"email": {"type": "string", "description": "要绑定的邮箱地址"}},
                "required": ["email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_bind_email",
            "description": "用户提供收到的 6 位验证码后，用此工具完成邮箱绑定。需与 send_bind_email_code 配合：先发验证码，再传邮箱与验证码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "与发送验证码时一致的邮箱"},
                    "code": {"type": "string", "description": "用户收到的 6 位数字验证码"},
                },
                "required": ["email", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unbind_email",
            "description": "解绑当前用户的邮箱。无需验证，直接解绑。解绑后无法通过该邮箱找回密码。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_exec",
            "description": "在 毛竹（Moso）本机（运行服务的机器）上执行一条 shell 命令。可用于 curl/wget 发网络请求、查看本机文件、运行脚本等。仅管理员可用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令，如 curl -s https://example.com、dir、ls -la、python -c \"print(1)\""},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 60"},
                    "cwd": {"type": "string", "description": "工作目录（相对项目根），可选"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_run_script",
            "description": "在本机执行 Python 代码或脚本文件。支持通过 requests/urllib 发 HTTP 请求、读写本机文件等。仅管理员可用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python 代码字符串，如 import requests; r=requests.get('https://api.example.com'); print(r.text)"},
                    "script_path": {"type": "string", "description": "或指定脚本文件路径（相对项目根），如 scripts/fetch.py"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 120"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_local_console",
            "description": "在本机管理页面上请求打开一个本机终端（由 AI 创建，显示为「控制台 N (AI)」）。可多次调用以创建多个终端，便于同时执行多条任务线。仅管理员。打开后可通过 send_to_terminal(slot, text) 向指定 slot 发命令；可先 list_terminals 查看当前有哪些控制台及对应 slot。默认保留最近使用的控制台，便于用户查看输出；只有用户明确要求关闭，或你确定不需要保留任何输出/交互状态时，才调用 close_local_console(slot)。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_local_console",
            "description": "关闭本机管理页面上指定槽位的本机终端。仅可关闭由 AI 创建的控制台（控制台 N (AI)），不会关闭用户手工创建的控制台。slot 为槽位（0、1、2…）。不要把本工具当作每次任务的固定收尾；默认保留最近控制台给用户查看输出。仅当用户要求关闭、你创建了多余临时控制台、或确认输出无需保留且无后续交互时再调用。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "slot": {"type": "integer", "description": "要关闭的本机控制台槽位（0、1、2…）"},
                },
                "required": ["slot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_fs_list",
            "description": "列出**本机操作系统**某目录下的文件和子目录（非 web/fs）。path 为空或 / 时：Windows 返回所有驱动器（C:、D: 等），Linux 返回根目录 /。支持绝对路径如 C:/Windows、/etc。仅管理员。用户要求「列本机目录」「看本机文件」时用此工具。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "本机目录路径，空或 / 表示根（Win=驱动器列表，Linux=/）；可为绝对路径"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_fs_read",
            "description": "读取本机上的文本文件内容（UTF-8）。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径，可为绝对路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_fs_write",
            "description": "向本机文件写入文本内容（覆盖，UTF-8）。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "要写入的内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_chat_write_file",
            "description": "本机管理：将文本写入 web/fs 下推荐结构 `local/YYYY/MM/DD/(自定子目录)/<uuid>-功能名.扩展`。**引导**生成脚本时通常先调 local_chat_data_paths 取输出目录，再把脚本中输出路径指到其下。用户明确要求其它路径/数据时按用户。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "仅写「功能性子路径」：如 weather/out.html、scripts/fetch.py。**不要**再带 local/年/月/日 或绝对盘符（系统已自动落到当日 local/… 下；若误带前缀会自动去重）。",
                    },
                    "content": {"type": "string", "description": "要写入的文本内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_fs_write_file",
            "description": "向本机文件写入文本内容（覆盖，UTF-8）。兼容别名，等价于 local_fs_write。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "要写入的内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_fs_mkdir",
            "description": "在本机创建目录（含父目录）。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录路径，可为绝对路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_fs_delete",
            "description": "删除本机上的文件或空目录；recursive=True 可递归删除非空目录。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件或目录路径"},
                    "recursive": {"type": "boolean", "description": "是否递归删除目录，默认 false"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_fs_rename",
            "description": "移动或重命名本机文件/目录。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "源路径"},
                    "dst": {"type": "string", "description": "目标路径"},
                },
                "required": ["src", "dst"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_fs_truncate",
            "description": "将本机文件截断为指定长度（字节）；size=0 表示清空文件。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "size": {"type": "integer", "description": "目标长度（字节），默认 0"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_fs_read_binary",
            "description": "从本机文件指定偏移处读取二进制内容，返回 base64 或 hex 字符串。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "offset": {"type": "integer", "description": "读取起始偏移（字节），默认 0"},
                    "size": {"type": "integer", "description": "读取字节数，不传则读到末尾"},
                    "encoding": {"type": "string", "description": "返回编码：base64 或 hex，默认 base64"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_fs_write_binary",
            "description": "向本机文件写入二进制内容（content 可为 base64 或 hex，由 encoding 指定）。offset 为空时 truncate=True 先清空再写否则追加；offset 指定时从该偏移写入。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "二进制编码内容（base64 或 hex）"},
                    "offset": {"type": "integer", "description": "写入偏移（字节），不传则追加或覆盖"},
                    "truncate": {"type": "boolean", "description": "无 offset 时是否先清空再写，默认 false"},
                    "encoding": {"type": "string", "description": "content 编码：base64 或 hex，默认 base64"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_chat_write_binary",
            "description": "本机管理：二进制（base64/hex）写入 web/fs 下与 local_chat_write_file 相同的推荐结构。生成脚本/输出大文件时通常先调 local_chat_data_paths 对齐目录。用户另有要求时按用户。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "仅功能性子路径，勿重复 local/年/月/日 前缀（与 local_chat_write_file 相同）。",
                    },
                    "content": {"type": "string", "description": "二进制编码内容（base64 或 hex）"},
                    "offset": {"type": "integer", "description": "可选，写入偏移（字节）"},
                    "truncate": {"type": "boolean", "description": "无 offset 时是否先清空再写，默认 false"},
                    "encoding": {"type": "string", "description": "content 编码：base64 或 hex，默认 base64"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "local_chat_data_paths",
            "description": "本机管理专用：返回当日**推荐**数据目录（含绝对路径、shell 的 cwd 建议等），用于**引导**生成脚本时把**输出/临时结果**落到 web/fs 下 `local/年/月/日/…`；其下**可自行**组织子目录与文件名。仅当用户**明确要求**读写或处理**其它位置/其它数据**时，再按用户要求；未指定时由 AI 权衡，**一般**使用本推荐目录。可选 preview_subdir 预览子路径。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "preview_subdir": {
                        "type": "string",
                        "description": "可选。预览用子目录，如 scripts/baidu_run，会净化后拼入示例路径（非强制落盘）。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_start",
            "description": "在本机启动子进程（shell 命令），返回 pid。可后续用 process_stdin_write/process_stdout_read/process_wait 等操作。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "cwd": {"type": "string", "description": "工作目录，可选"},
                    "env": {"type": "object", "description": "环境变量键值对，可选"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_terminate",
            "description": "终止本机托管进程。force=True 强制杀死。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "进程 ID"},
                    "force": {"type": "boolean", "description": "是否强制杀死，默认 false"},
                },
                "required": ["pid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_wait",
            "description": "等待本机托管进程结束，返回 returncode。可选 timeout 秒。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "进程 ID"},
                    "timeout": {"type": "number", "description": "超时秒数，不传则一直等"},
                },
                "required": ["pid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_stdin_write",
            "description": "向本机托管进程的标准输入写入文本。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "进程 ID"},
                    "data": {"type": "string", "description": "要写入的文本"},
                },
                "required": ["pid", "data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_stdin_close",
            "description": "关闭本机托管进程的标准输入（发送 EOF）。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {"pid": {"type": "integer", "description": "进程 ID"}},
                "required": ["pid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_stdout_read",
            "description": "读取本机托管进程至今的标准输出（已缓冲），返回 base64。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "进程 ID"},
                    "max_bytes": {"type": "integer", "description": "最多读取字节数，默认 65536"},
                },
                "required": ["pid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_stderr_read",
            "description": "读取本机托管进程至今的标准错误（已缓冲），返回 base64。仅管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "进程 ID"},
                    "max_bytes": {"type": "integer", "description": "最多读取字节数，默认 65536"},
                },
                "required": ["pid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_list",
            "description": "列出当前本机托管的所有进程（pid、command、是否存活）。仅管理员。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_users",
            "description": "列出所有用户（id、username、display_name、role、status、created_at、last_login）。status 三态：active 正常、locked 安全锁定（多次错密等）、suspended 管理员暂停；locked 时可能含 lock_expires_at。需管理员。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user",
            "description": "获取单个用户详情（id、username、display_name、role、status、created_at、last_login）。status 含义同 list_users。需管理员。",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "integer"}},
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_user",
            "description": "新建用户。需管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                    "display_name": {"type": "string"},
                    "role": {"type": "string", "enum": ["user", "admin", "manager"], "description": "默认 user"},
                },
                "required": ["username", "password"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_user",
            "description": "更新用户（display_name、role、status）。需管理员。status 仅可为 active 或 suspended（暂停）；不可设为 locked（安全锁定仅系统自动）。不可修改内置管理员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer"},
                    "display_name": {"type": "string"},
                    "role": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "suspended"], "description": "勿使用 locked"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_user",
            "description": "删除用户。需管理员。不可删除内置管理员。",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "integer"}},
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_user_password",
            "description": "重置指定用户的密码。需管理员。用于管理员为某用户设置新密码（如忘记密码时）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer"},
                    "password": {"type": "string"},
                },
                "required": ["user_id", "password"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "admin_unlock_user",
            "description": "管理员解除用户的安全锁定（status=locked，如多次密码错误）。暂停账户（suspended）不可用本工具，请用 update_user 将 status 设为 active。需管理员。",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "integer"}},
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_user_system_ai_usage",
            "description": "清零指定用户使用系统共享 Key 的调用计数，使其重新获得完整配额（默认 2000 次，以系统配置为准）。需管理员。用于用户尚未配置自有 Key、需继续使用共享 Key 时延长可用次数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "要重置的用户 ID，可用 list_users 或 get_user 查询"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "change_my_password",
            "description": "当前登录用户修改自己的密码。需提供旧密码验证和新密码。用于用户自助改密。",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_password": {"type": "string", "description": "当前密码"},
                    "new_password": {"type": "string", "description": "新密码，至少 6 个字符"},
                },
                "required": ["old_password", "new_password"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_ai_sessions",
            "description": "列出当前用户的 AI 聊天会话列表（id、title、created_at、updated_at）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ai_session",
            "description": "获取某条 AI 聊天会话详情及消息列表。注意：若会话消息中含密码、凭证等敏感信息，你在回复用户时不得引用或泄露。",
            "parameters": {
                "type": "object",
                "properties": {"session_id": {"type": "integer"}},
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_ai_session",
            "description": "新建一条 AI 聊天会话。",
            "parameters": {
                "type": "object",
                "properties": {"title": {"type": "string", "description": "会话标题，默认 新会话"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_ai_session",
            "description": "更新 AI 聊天会话标题。",
            "parameters": {
                "type": "object",
                "properties": {"session_id": {"type": "integer"}, "title": {"type": "string"}},
                "required": ["session_id", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_ai_session",
            "description": "删除一条 AI 聊天会话及其消息。",
            "parameters": {
                "type": "object",
                "properties": {"session_id": {"type": "integer"}},
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_ai_sessions",
            "description": "清空当前用户所有 AI 聊天会话。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_session_prompt",
            "description": "更新或追加当前会话的会话级提示词。当用户要求「更新会话提示词」「补充会话约束」「把上述要求记到会话里」或根据对话总结出会话级约束时调用。会话级提示词会在后续对话中高优先级约束 AI 行为。content 建议使用 Markdown 格式（如 ## 标题、- 列表、`代码`），便于查看。",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "integer", "description": "当前会话 ID（系统提示中已提供）"},
                    "content": {"type": "string", "description": "要设置或追加的提示词内容"},
                    "append": {"type": "boolean", "description": "true=在现有会话提示词后追加；false=覆盖为 content。默认 false"},
                },
                "required": ["session_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_session_prompt",
            "description": "读取指定会话的会话级提示词全文。已注入「会话级约束」时可不必重复调用；修改前可用本工具核对。",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "integer", "description": "当前会话 ID（系统提示中已提供）"},
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_ensure",
            "description": "确保用户 Memory 空间存在（memory/hosts|topics|journal + GUIDE.md + INDEX.md）。开始写入长期记忆前可调用一次。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_list",
            "description": "列出 Memory 条目（path/title/summary/host_id/tags）。优先于盲目 fs_list；记忆可能过时，重要操作仍须实机检查。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["host", "topic", "journal", "note"],
                        "description": "可选筛选",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "在 Memory 空间多文件搜索 Markdown 章节。用于快速定位主机环境/状态笔记；命中后 memory_read 精读，重要结论仍须实机核实。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "scope": {"type": "string", "enum": ["titles", "content", "all"]},
                    "regex": {"type": "boolean"},
                    "case_insensitive": {"type": "boolean"},
                    "max_hits": {"type": "integer"},
                    "kind": {"type": "string", "enum": ["host", "topic", "journal", "note"]},
                    "host_id": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_read",
            "description": "读取 Memory 文件全文或按章节精读。可用 path，或 host_id 读取该主机记忆文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区，如 memory/hosts/h1_web.md"},
                    "host_id": {"type": "integer"},
                    "host_name": {"type": "string"},
                    "section_path": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "章节路径，如 [\"Status\"]",
                    },
                    "heading": {"type": "string"},
                    "max_chars": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_write",
            "description": "写入/更新 Memory（路径、环境/状态、历史参考、用户习惯操作等）。用户开展新内容或已验证关键事实后应写入；状态变化时更新。传 host_id 时默认写入 memory/hosts/h{id}_{name}.md。默认重建 INDEX.md。勿写入密码/密钥。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Markdown 正文"},
                    "path": {"type": "string"},
                    "kind": {"type": "string", "enum": ["host", "topic", "journal", "note"]},
                    "title": {"type": "string"},
                    "summary": {"type": "string", "description": "一句话摘要，写入元数据与索引"},
                    "host_id": {"type": "integer"},
                    "host_name": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "append": {"type": "boolean", "description": "true=追加正文；false=覆盖"},
                    "rebuild_index": {"type": "boolean", "description": "默认 true"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_rebuild_index",
            "description": "扫描 Memory 下 md 文件，根据元数据/摘要重建 memory/INDEX.md。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_session_operations",
            "description": (
                "获取当前会话的「操作序列」：仅用户要求与助手指令/决策，不含程序输出与工具轨迹。"
                "适合生成会话提示词、归纳最佳实践。"
                "若要核对「调了哪些工具/结果依据」，请改用 get_session_chat_detail(include_tool_results=true)。"
                "返回按时间序列表，每项含 role、content、created_at。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "integer", "description": "当前会话 ID（系统提示中已提供）"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 50"},
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_session_chat_detail",
            "description": (
                "获取当前会话的聊天详情，用于自查「本会话做过什么、调了哪些工具」。"
                "include_tool_results=false 时与 get_session_operations 一致（仅用户与助手指令）；"
                "为 true 时：助手消息含可读正文，并附带解码后的 tool_trace（工具名/参数摘要/结果预览；"
                "界面「AI 思考与计划」折叠内容的同源数据）。"
                "当用户问「你怎么查到的/依据是什么/调了哪些工具」时优先调用本工具（include_tool_results=true）；"
                "勿因当前上下文看不到 tool_call 就声称「我是猜的」。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "integer", "description": "当前会话 ID"},
                    "include_tool_results": {
                        "type": "boolean",
                        "description": "是否含程序输出与工具轨迹；false=仅指令；true=正文+tool_trace",
                    },
                    "limit": {"type": "integer", "description": "最多返回条数，默认 50"},
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ensure_chat_tools",
            "description": (
                "【分层装载专用·始终可用】按需装载本轮尚未下发的工具。"
                "本轮 tools 是子集≠平台没有该能力。需要 scp_push / ssh_execute / fs_* 等但不在列表时，"
                "**必须先调用本工具**（tool_names 和/或 capabilities=terminal|fs|http|host_transfer|full），"
                "成功后再做业务 tool_call。禁止用「没有文件传输/SSH 工具」等文字道歉结束。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "需要装载的工具名列表，如 [\"scp_push\",\"ssh_execute\"]",
                    },
                    "capabilities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "能力集：terminal / fs / http / host_transfer / full（或 ops）",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_best_practices",
            "description": "查询最佳实践列表。用于在执行前参考已有推荐方法，或按分类/关键词筛选。来源包括用户指定方法、AI 成功解决问题后的归纳。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "可选，按分类筛选"},
                    "keyword": {"type": "string", "description": "可选，标题/内容/分类关键词模糊搜索"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_best_practice",
            "description": "添加一条最佳实践。当用户要求或指定了某实现方法、或你成功解决具体问题后，应将有用做法归纳到此（标题、分类、内容摘要），便于后续复用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "标题/主题"},
                    "category": {"type": "string", "description": "分类，如 SSH、MySQL、部署、备份 等"},
                    "content": {"type": "string", "description": "推荐实现方法或步骤说明"},
                    "source": {"type": "string", "description": "来源：user_request / ai_solved / manual，默认 ai_solved"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_best_practice",
            "description": "更新一条最佳实践（标题、分类、内容）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "记录 ID"},
                    "title": {"type": "string"},
                    "category": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_best_practice",
            "description": "删除一条最佳实践记录。",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer", "description": "记录 ID"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_aihelp_index",
            "description": (
                "读取 AI 帮助 index.md。大文档优先 sections_only=true 或 max_level 列章节，"
                "再 get_aihelp_file / markdown_search_sections 按需加载。支持 section_* / heading / max_chars。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sections_only": {"type": "boolean", "description": "true=仅章节清单"},
                    "max_level": {"type": "integer", "description": "章节清单展示到第 N 级标题"},
                    "section_index": {"type": "integer"},
                    "section_path": {"type": "array", "items": {"type": "string"}},
                    "heading": {"type": "string"},
                    "max_chars": {"type": "integer"},
                    "include_children": {"type": "boolean"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_aihelp_files",
            "description": "列出 web/aihelp 目录下所有帮助文档文件名（含 index.md）。用于确定有哪些帮助主题可读。所有用户只读。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_aihelp_file",
            "description": (
                "读取 web/aihelp 帮助文档。勿默认拉全文：先 sections_only 或 markdown_search_sections(scope=titles)，"
                "再 section_path/heading + max_chars 读单节。REST 同等：GET /api/aihelp/file?path=&sections_only=…"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如 hosts.md"},
                    "sections_only": {"type": "boolean"},
                    "max_level": {"type": "integer"},
                    "section_index": {"type": "integer"},
                    "section_path": {"type": "array", "items": {"type": "string"}},
                    "heading": {"type": "string"},
                    "max_chars": {"type": "integer"},
                    "include_heading": {"type": "boolean"},
                    "include_children": {"type": "boolean"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "markdown_search_sections",
            "description": (
                "在 Markdown 中按关键字搜索章节（scope=titles|content|all）。"
                "file_root=aihelp 且 path 空：搜全部帮助文档；"
                "file_root=fs 且 path 为目录（如 memory 或 memory/hosts）：多文件递归搜该目录下全部 .md；"
                "path 为单文件则只搜该文件。命中后再 markdown_read_section / memory_read / fs_read_file 精读。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键字"},
                    "file_root": {"type": "string", "enum": ["fs", "aihelp", "skill"]},
                    "path": {
                        "type": "string",
                        "description": "单文件，或 fs 下的目录（多文件搜索）；aihelp 空则搜全部",
                    },
                    "skill_name": {"type": "string"},
                    "scope": {"type": "string", "enum": ["titles", "content", "all"]},
                    "regex": {"type": "boolean"},
                    "case_insensitive": {"type": "boolean"},
                    "max_level": {"type": "integer"},
                    "max_hits": {"type": "integer"},
                    "snippet_chars": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "markdown_list_sections",
            "description": (
                "列出 Markdown 文件的 ATX 标题章节清单（可限制到第 N 级标题）。"
                "file_root=fs|aihelp|skill；大文档先调此工具再 markdown_read_section 按需加载。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_root": {
                        "type": "string",
                        "description": "fs=web/fs 用户目录；aihelp=帮助文档；skill=skills/<name>/ 下文件",
                        "enum": ["fs", "aihelp", "skill"],
                    },
                    "path": {"type": "string", "description": "相对路径，如 hosts.md、reference.md"},
                    "skill_name": {"type": "string", "description": "file_root=skill 时必填"},
                    "max_level": {"type": "integer", "description": "展示到的标题级别 1–6，默认 6"},
                    "include_preamble": {"type": "boolean", "description": "是否包含首个 # 标题前的序言块，默认 false"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "markdown_read_section",
            "description": (
                "读取 Markdown 指定章节正文（section_path / section_index / heading 三选一），"
                "可限 max_chars、include_children=false 仅直属段落不含子节。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_root": {"type": "string", "enum": ["fs", "aihelp", "skill"]},
                    "path": {"type": "string"},
                    "skill_name": {"type": "string"},
                    "section_path": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标题路径，如 [\"安装\", \"Docker\"]",
                    },
                    "section_index": {"type": "integer", "description": "扁平标题序号，与 list 返回的 index 一致"},
                    "heading": {"type": "string", "description": "按标题文本定位（不唯一时报错）"},
                    "case_insensitive": {"type": "boolean"},
                    "max_chars": {"type": "integer", "description": "返回正文最大字符数，默认见配置"},
                    "include_heading": {"type": "boolean", "description": "是否含 # 标题行，默认 true"},
                    "include_children": {"type": "boolean", "description": "是否含子章节，默认 true"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "markdown_replace_section",
            "description": (
                "定点替换 Markdown 某一节并写回文件。mode=replace_body 保留原标题行只换正文；"
                "replace_all 替换标题+整节（含子节）。aihelp 仅管理员可写。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_root": {"type": "string", "enum": ["fs", "aihelp", "skill"]},
                    "path": {"type": "string"},
                    "skill_name": {"type": "string"},
                    "section_path": {"type": "array", "items": {"type": "string"}},
                    "section_index": {"type": "integer"},
                    "heading": {"type": "string"},
                    "case_insensitive": {"type": "boolean"},
                    "new_content": {"type": "string", "description": "替换后的 Markdown 片段"},
                    "mode": {
                        "type": "string",
                        "enum": ["replace_body", "replace_all"],
                        "description": "默认 replace_body",
                    },
                },
                "required": ["path", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_aihelp_file",
            "description": "创建或覆盖 web/aihelp 下某一帮助文档（仅管理员）。写入后需由管理员自行维护 index.md 目录，或调用 update_aihelp_index。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对于 aihelp 的路径，如 hosts.md，建议 .md 后缀"},
                    "content": {"type": "string", "description": "Markdown 正文"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_aihelp_index",
            "description": "更新 web/aihelp/index.md 的完整内容（仅管理员）。用于在新增/删除帮助文档后维护目录索引，便于 get_aihelp_index 与用户查阅。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "index.md 的完整 Markdown 内容（目录、链接等）"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_search_providers",
            "description": (
                "列出所有可用的搜索服务（如 GitHub、阿里云 IQS）及当前用户在每个服务下的配置状态："
                "是否需要 API Key、是否已配置、是否启用。用于在调用 search_web / search_github 前自检，"
                "或回答用户「我配了哪些搜索服务」。返回不含任何密钥原值。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "configure_search_provider",
            "description": (
                "为当前用户配置一个搜索服务的 API Key / 启用开关。注意：传入的 api_key 会以密文方式保存，"
                "服务端永不回显原值。若用户未明确给出 Key 文本，请不要假造；可只传 enabled 切换启停。"
                "推荐流程：先用 list_search_providers 查看当前状态，必要时用 ask_user_choice 与用户确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "服务短码：github / iqs。可用值由 list_search_providers 给出。",
                    },
                    "api_key": {
                        "type": "string",
                        "description": "API Key 原文。空串或 \"***\" 表示「保持原 Key 不变」（仅修改其它字段时使用）。",
                    },
                    "enabled": {"type": "boolean", "description": "可选，是否启用该服务。"},
                    "extra": {
                        "type": "object",
                        "description": "可选，非密配置项（如 IQS 的 default_engine_type）。整体替换原 extra。",
                    },
                },
                "required": ["provider"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_github",
            "description": (
                "使用 GitHub 官方 REST API 搜索仓库 / 代码 / Issue / 用户。GitHub 即使未配置 PAT 也可用"
                "（匿名 60 次/小时），配置 PAT 后限速提升至 5000 次/小时且可搜私有仓库。"
                "查询语法支持 GitHub 高级搜索语法，例如 `language:python stars:>1000`、`org:openai`。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词，支持 GitHub 高级语法"},
                    "type": {
                        "type": "string",
                        "enum": ["repositories", "code", "issues", "users"],
                        "description": "搜索类型，默认 repositories",
                    },
                    "limit": {"type": "integer", "description": "最多返回条数，1-50，默认 10"},
                    "sort": {
                        "type": "string",
                        "description": "可选排序字段，例如 stars / forks / updated（按 type 不同含义不同）",
                    },
                    "order": {"type": "string", "enum": ["asc", "desc"], "description": "排序方向"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "通用网页搜索。当前由阿里云 IQS UnifiedSearch 提供（用户必须先配置个人 API Key）。"
                "用于查询新闻、技术资料、行业信息、官方文档等开放域内容；返回标题、链接、摘要等。"
                "未配置 IQS Key 时会返回明确的错误指引用户去「设置 / 搜索服务」配置。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词，1-500 字符；建议 30 字以内"},
                    "engine_type": {
                        "type": "string",
                        "enum": ["Generic", "GenericAdvanced", "LiteAdvanced", "Deep"],
                        "description": (
                            "引擎类型：Generic 标准版（默认 ~10 条，免费）；GenericAdvanced 增强版（~50 条，收费）；"
                            "LiteAdvanced 联网搜索极速版（语义化，1-50 条）；Deep 深度搜索（高时延，1-50 条）。"
                        ),
                    },
                    "time_range": {
                        "type": "string",
                        "enum": ["NoLimit", "OneDay", "OneWeek", "OneMonth", "OneYear"],
                        "description": "时间范围筛选，默认 NoLimit",
                    },
                    "limit": {"type": "integer", "description": "返回条数，1-50，默认 10（Generic 强制 10）"},
                    "with_main_text": {"type": "boolean", "description": "是否返回正文（最长 3000 字），默认 false"},
                    "with_markdown": {"type": "boolean", "description": "是否返回 Markdown 正文，默认 false"},
                    "with_summary": {"type": "boolean", "description": "是否返回 query 相关摘要（收费），默认 false"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_user_mcp_servers",
            "description": (
                "列出当前用户配置的个人 MCP 服务器（stdio / SSE / Streamable HTTP）。"
                "返回传输方式、启用状态、各场景聊天开关、最近测试结果与工具数量；不含 env/headers 等敏感配置原值。"
                "用户可在网页「MCP 配置」或对话中让你代为管理 MCP。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "configure_user_mcp_server",
            "description": (
                "按标识名新增或更新当前用户的 MCP 服务器（同名则 upsert）。"
                "stdio 须填 command（可选 args/env）；远程须填 url（可选 headers）。"
                "可设置 enabled、chat_enabled 及 chat_scope_web/host/integration 控制该 MCP 在哪些 AI 场景加载工具。"
                "配置后建议调用 test_user_mcp_server 验证连接。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "唯一标识（小写字母数字连字符），如 filesystem、notion"},
                    "display_name": {"type": "string", "description": "显示名称"},
                    "transport": {
                        "type": "string",
                        "enum": ["stdio", "sse", "streamable_http"],
                        "description": "传输方式，默认 stdio",
                    },
                    "command": {"type": "string", "description": "stdio 启动命令，如 npx、node、python"},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "stdio 参数列表"},
                    "url": {"type": "string", "description": "远程 MCP URL"},
                    "env": {"type": "object", "description": "stdio 环境变量 KEY->VALUE"},
                    "headers": {"type": "object", "description": "HTTP 请求头"},
                    "enabled": {"type": "boolean", "description": "是否启用该 MCP 服务器"},
                    "chat_enabled": {"type": "boolean", "description": "是否允许并入 AI 聊天工具列表"},
                    "chat_scope_web": {"type": "boolean", "description": "网页全局 AI 聊天是否加载"},
                    "chat_scope_host": {"type": "boolean", "description": "主机维度 AI 聊天是否加载"},
                    "chat_scope_integration": {
                        "type": "boolean",
                        "description": "OpenClaw / MCP 集成通道是否加载",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_user_mcp_server",
            "description": "按标识名或数字 id 删除当前用户的 MCP 服务器配置。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "服务器标识名"},
                    "server_id": {"type": "integer", "description": "数字 id（与 name 二选一）"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_user_mcp_server",
            "description": "测试 MCP 服务器连接并列出可用工具数量（按 name 或 server_id 指定）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "服务器标识名"},
                    "server_id": {"type": "integer", "description": "数字 id"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_user_mcp_config",
            "description": (
                "从 Cursor / Claude Desktop 风格 mcp.json 批量导入 MCP 服务器。"
                "接受 JSON 字符串或对象（含 mcpServers 字段）。overwrite=true 时覆盖同名配置。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "config": {
                        "type": "string",
                        "description": "JSON 文本，如 {\"mcpServers\":{\"foo\":{\"command\":\"npx\",...}}}",
                    },
                    "overwrite": {"type": "boolean", "description": "是否覆盖已存在的同名服务器"},
                    "chat_enabled": {"type": "boolean", "description": "导入项默认是否参与聊天"},
                    "chat_scope_web": {"type": "boolean"},
                    "chat_scope_host": {"type": "boolean"},
                    "chat_scope_integration": {"type": "boolean"},
                },
                "required": ["config"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refresh_user_mcp_tools",
            "description": "清除并重新拉取 MCP 工具 schema 缓存；可指定单个服务器或全部刷新。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "服务器标识名"},
                    "server_id": {"type": "integer", "description": "数字 id；均不传则刷新全部缓存"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_user_mcp_config",
            "description": (
                "导出当前用户 MCP 配置为 Cursor / Claude Desktop 风格 JSON（mcpServers 对象）。"
                "默认包含完整 env/headers；可选包含 _edgeops 场景开关元数据。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_disabled": {
                        "type": "boolean",
                        "description": "是否包含已禁用的服务器，默认 true",
                    },
                    "include_edgeops_meta": {
                        "type": "boolean",
                        "description": "是否附带 毛竹（Moso）场景开关 _edgeops 元数据，默认 true",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_user_skills",
            "description": (
                "列出当前用户的 Agent Skills（含 group_id/group_name 分组、启停、场景开关）。"
                "需管理员已在用户管理中开启 Skills 功能。"
                "管理分组用 list/create/update/delete_user_skill_group、assign_user_skills_to_group。"
                "创建新 Skill 请用 save_user_skill，勿只列出后口头描述。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_skill_script",
            "description": (
                "在指定 User Skill 的 scripts/ 目录内执行脚本（仅该目录、超时、清除代理环境变量；非内核级禁网）。"
                "skill_name 为 Skill 名；script 为 scripts/ 下单层文件名（如 check.py）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "Skill 名称（slug）"},
                    "script": {"type": "string", "description": "scripts/ 下文件名"},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选命令行参数",
                    },
                    "timeout_sec": {"type": "integer", "description": "超时秒数，默认 30，最大 120"},
                },
                "required": ["skill_name", "script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_user_skill_groups",
            "description": (
                "列出当前用户的 Skills 分组（含虚拟「未分组」摘要：skill_count、enabled_count）。"
                "分组仅存数据库，用于整理 Skills；与主机分组无关。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_user_skill_group",
            "description": (
                "新建 Skills 分组（仅当前用户）。用户要求「建 Skill 组/把 Skill 放进某组」时须调用，"
                "勿声称系统不支持分组。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "分组名称（如 test、工作流）"},
                    "sort_order": {"type": "integer", "description": "排序，越小越靠前，默认 0"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_user_skill_group",
            "description": "重命名 Skills 分组（group_id 与 group_name 二选一指定目标）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer"},
                    "group_name": {"type": "string", "description": "当前分组名（与 group_id 二选一）"},
                    "name": {"type": "string", "description": "新的分组名称"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_user_skill_group",
            "description": "删除 Skills 分组；组内 Skill 移入「未分组」，不删 Skill 本身。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer"},
                    "group_name": {"type": "string"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_user_skills_to_group",
            "description": (
                "将 Skill 批量移入指定分组（或 group_id=null/group_name 省略时移入「未分组」）。"
                "可传 skill_names 或 skill_ids；all_ungrouped=true 时移动全部未分组 Skill。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "目标分组 id；省略或 null 表示未分组"},
                    "group_name": {"type": "string", "description": "目标分组名（与 group_id 二选一）"},
                    "skill_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Skill 标识名列表",
                    },
                    "skill_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Skill 数字 id 列表",
                    },
                    "all_ungrouped": {
                        "type": "boolean",
                        "description": "true 时将所有未分组 Skill 移入目标分组",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_skill",
            "description": (
                "按标识名或数字 id 获取 Skill 详情（含 SKILL.md 正文与 resources 文件列表）。"
                "渐进式披露：目录匹配后先 get_user_skill 或 read_user_skill_file(sections_only)；大文档用 markdown_search_sections(file_root=skill) 定位后再读单节。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill 标识名"},
                    "skill_id": {"type": "integer", "description": "数字 id（与 name 二选一）"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_user_skill",
            "description": (
                "新增或更新 Agent Skill（同名 upsert，Cursor Agent Skills 格式）。"
                "路径：web/fs/<用户>/skills/<name>/SKILL.md（不是 chats/）。"
                "用户说「创建/编写 Skill」「加斜杠命令」「加 Hook 确认」时必须调用本工具落地。"
                "frontmatter：name、description（第三人称 WHAT+WHEN）；默认 disable-model-invocation: true。"
                "可同时配置斜杠名、Hook（matcher/decision 或 hooks_json）、allowed_tools。"
                "正文可用 {{arg}}/$ARGUMENTS 供用户 `/name 参数` 替换；子命令用 write_user_skill_file 写 commands/<alias>.md。"
                "若有固定子命令/常用参数：frontmatter 必写 slash-args 列表（或正文「## 斜杠参数」列表 / `/name xxx` 示例），"
                "以便聊天填参浮层显示可点选建议；勿只写 {{arg}} 而不声明建议值。"
                "可传完整 content，或 name+description+body。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "唯一标识（小写字母开头，a-z、0-9、-、_）"},
                    "display_name": {"type": "string", "description": "显示名称"},
                    "description": {"type": "string", "description": "简短描述（写入 frontmatter，供 AI 判断是否遵循）"},
                    "content": {
                        "type": "string",
                        "description": (
                            "完整 SKILL.md（与 body 二选一，可含 frontmatter）。"
                            "创建斜杠 Command 且有固定参数时，在 frontmatter 加 slash-args: [a, b]，"
                            "或正文写 ## 斜杠参数 列表"
                        ),
                    },
                    "body": {
                        "type": "string",
                        "description": (
                            "Markdown 正文（不含 frontmatter）；可含 {{arg}}/{{arg1}}/$ARGUMENTS。"
                            "有固定参数时写 ## 斜杠参数 列表；需要 frontmatter slash-args 时请用 content"
                        ),
                    },
                    "enabled": {"type": "boolean", "description": "是否启用"},
                    "chat_enabled": {"type": "boolean", "description": "是否参与 AI 聊天注入"},
                    "chat_scope_web": {"type": "boolean"},
                    "chat_scope_host": {"type": "boolean"},
                    "chat_scope_integration": {"type": "boolean"},
                    "group_id": {"type": "integer", "description": "所属分组 id；null 表示未分组"},
                    "group_name": {"type": "string", "description": "所属分组名（与 group_id 二选一）"},
                    "slash_name": {
                        "type": "string",
                        "description": "斜杠命令名（不含 /）；用户输入 /slash_name 强制加载；默认等于 name",
                    },
                    "hooks_enabled": {
                        "type": "boolean",
                        "description": "是否启用 Hook；写了 hooks_json 或 matcher 时建议 true",
                    },
                    "pre_tool_use_matcher": {
                        "type": "string",
                        "description": "preToolUse 工具 glob，逗号分隔，如 ssh_execute,send_to_terminal,*channel*",
                    },
                    "pre_tool_use_decision": {
                        "type": "string",
                        "description": "matcher 命中且 hooks.json 未覆盖时的决策：ask（确认）/ deny（拒绝）/ allow（放行），默认 ask",
                        "enum": ["ask", "deny", "allow"],
                    },
                    "allowed_tools": {
                        "type": "string",
                        "description": "工具白名单 glob（逗号分隔）；仅用户本轮斜杠唤起本 Skill 时强制",
                    },
                    "hooks_json": {
                        "description": (
                            "hooks.json 内容：JSON 字符串或对象。"
                            "例：{\"preToolUse\":{\"matcher\":\"ssh_*\",\"decision\":\"ask\",\"reason\":\"需确认\"}}"
                        ),
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_user_skill",
            "description": "按标识名或 skill_id 删除 Skill；remove_files=true 时同时删除磁盘目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "skill_id": {"type": "integer"},
                    "remove_files": {"type": "boolean", "description": "是否删除 skills/<name>/ 目录"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_user_skills",
            "description": (
                "扫描用户 skills/ 目录，与数据库双向同步：导入/更新磁盘上的 SKILL.md；"
                "删除或改名后磁盘不存在的 Skill 会从库中移除（改名=删旧+新增）。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_user_skill_file",
            "description": (
                "读取 skills/<name>/ 下文件（渐进式披露）。大 .md 先 sections_only 或 markdown_search_sections，"
                "再 section_path/heading + max_chars；勿用 fs_read_file 读 Skill。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill 标识名"},
                    "path": {"type": "string", "description": "相对路径，如 SKILL.md、reference.md"},
                    "sections_only": {"type": "boolean"},
                    "max_level": {"type": "integer"},
                    "section_index": {"type": "integer"},
                    "section_path": {"type": "array", "items": {"type": "string"}},
                    "heading": {"type": "string"},
                    "max_chars": {"type": "integer"},
                    "include_heading": {"type": "boolean"},
                    "include_children": {"type": "boolean"},
                },
                "required": ["name", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_user_skill_files",
            "description": (
                "列出 Skill 目录 skills/<name>/ 下的所有文件（含 SKILL.md、reference.md、scripts/ 等）。"
                "路径固定为用户 fs 根下的 skills/，不会落在 chats/ 日期目录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill 标识名"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_user_skill_file",
            "description": (
                "写入或追加 Skill 目录 skills/<name>/ 下的附属文件。"
                "常用：reference.md、examples.md、scripts/*.py；"
                "**Hook**：path=`hooks.json`（须合法 JSON；建议同时 save_user_skill hooks_enabled=true）；"
                "**斜杠子命令**：path=`commands/<alias>.md`（用户可用 /alias 唤起，正文支持 {{arg}}；"
                "有固定参数时同样写 slash-args 或 ## 斜杠参数，供填参浮层提示）。"
                "禁止写 SKILL.md（须 save_user_skill）。勿用 fs_write_file。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill 标识名"},
                    "path": {
                        "type": "string",
                        "description": "相对路径，如 reference.md、hooks.json、commands/check-disk.md",
                    },
                    "content": {"type": "string", "description": "文件内容（UTF-8 文本）"},
                    "append": {"type": "boolean", "description": "true 时在已有内容后追加，默认 false（覆盖）"},
                },
                "required": ["name", "path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_user_skill_file",
            "description": (
                "删除 Skill 目录 skills/<name>/ 下的单个附属文件。"
                "禁止删除 SKILL.md（须 delete_user_skill）；勿用 fs_delete。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill 标识名"},
                    "path": {"type": "string", "description": "相对 skills/<name>/ 的路径"},
                },
                "required": ["name", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_user_skills_config",
            "description": "导出当前用户 Agent Skills 为 JSON 包（含 SKILL.md 与附属文件、场景开关）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_disabled": {
                        "type": "boolean",
                        "description": "是否包含已禁用的 Skill，默认 true",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_user_skills_config",
            "description": "从 export_user_skills_config 格式的 JSON 批量导入 Agent Skills。",
            "parameters": {
                "type": "object",
                "properties": {
                    "data": {"type": "string", "description": "JSON 字符串或对象"},
                    "overwrite": {"type": "boolean", "description": "同名是否覆盖，默认 false"},
                },
                "required": ["data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_feedback",
            "description": (
                "代当前登录用户向 毛竹（Moso）管理员提交一条技术反馈或建议（任何已登录用户均可调用）。"
                "内容是 Markdown；title 可选；category 用于分类（bug / feature / tech / general）。"
                "提交后管理员可在「反馈」菜单看到；若管理员开启了邮件通知，会自动发邮件提醒。"
                "请在调用前先与用户确认大致内容，避免误提交。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "反馈标题（≤200 字），可不填"},
                    "content": {"type": "string", "description": "Markdown 反馈正文（必填）"},
                    "category": {
                        "type": "string",
                        "enum": ["bug", "feature", "tech", "general"],
                        "description": "分类：bug 缺陷 / feature 需求 / tech 技术问题 / general 其它",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_my_feedback",
            "description": (
                "列出当前登录用户自己提交过的反馈（含状态与管理员回复链）。任何已登录用户可调用，"
                "用于回答「我之前反馈的那个 bug 管理员回了吗」之类的问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "最多条数，1-200，默认 50"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_user_feedback_admin",
            "description": (
                "管理员视角列出用户反馈（仅管理员可用）。filter:"
                " all=全部, unread=未读（admin_read_at 为空）, open=待处理, replied=已回, ignored=已忽略。"
                "支持分页 limit/offset。返回会同时给出 unread_total 用于显示未读小红点。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "enum": ["all", "unread", "open", "replied", "ignored"],
                        "description": "默认 unread；想批量看新反馈用 unread，想看历史用 all",
                    },
                    "limit": {"type": "integer", "description": "1-500，默认 50"},
                    "offset": {"type": "integer", "description": "分页偏移，默认 0；分批查看时递增"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_feedback_detail",
            "description": "查看一条反馈的完整详情（含全部回复）。管理员可查看任意一条；普通用户只能查看自己的。调用时会顺带把它标为已读（仅管理员触发）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "feedback_id": {"type": "integer", "description": "反馈 id"},
                },
                "required": ["feedback_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reply_user_feedback_admin",
            "description": (
                "管理员对一条用户反馈发起回复（仅管理员可用）。回复内容是 Markdown；"
                "提交后该反馈状态会自动从 open 变为 replied，用户即不能再编辑该反馈。"
                "建议先用 get_user_feedback_detail 看清原文再回复；多次调用可发多条回复。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "feedback_id": {"type": "integer", "description": "反馈 id"},
                    "content": {"type": "string", "description": "Markdown 回复正文"},
                },
                "required": ["feedback_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ignore_user_feedback_admin",
            "description": "管理员忽略一条反馈（仅管理员可用）。状态置为 ignored 并标已读；不影响已有回复。如需重新处理，可用 reply_user_feedback_admin 回复（状态转为 replied）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "feedback_id": {"type": "integer", "description": "反馈 id"},
                },
                "required": ["feedback_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_all_user_feedback_read",
            "description": (
                "管理员一次性把所有「未读」反馈标记为已读（仅管理员可用）。"
                "用于「忽略所有已存在的反馈，只看后面新来的」场景。返回受影响的条数。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_clone_on_host",
            "description": (
                "在指定主机上 git clone 一个仓库到目标目录。用于把 GitHub / GitLab / 自建 Git 仓库的代码"
                "拉到目标机器；公开仓库不需要 GitHub Key，也不依赖本平台的搜索服务配置。"
                "前提：目标机已安装 git。建议先用 search_github 找到 clone_url，再用此工具下载。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host_id": {"type": "integer", "description": "目标主机 id"},
                    "repo_url": {
                        "type": "string",
                        "description": "git clone 用的 URL（https 或 ssh），如 https://github.com/owner/repo.git",
                    },
                    "target_dir": {
                        "type": "string",
                        "description": "可选，本地目标目录；不传则用仓库默认名（在当前用户家目录下）",
                    },
                    "depth": {"type": "integer", "description": "可选，--depth 浅克隆（仅取最近 N 次提交，加速大仓库）"},
                    "branch": {"type": "string", "description": "可选，--branch 指定分支或 tag"},
                },
                "required": ["host_id", "repo_url"],
            },
        },
    },
]

# 仅在本机管理会话中可用、且仅管理员可调用的工具（AI 助手与主机详情 AI 不可见、不可用）
LOCAL_ONLY_TOOLS = frozenset({
    "local_exec", "local_run_script", "create_local_console", "close_local_console",
    "local_fs_list", "local_fs_read", "local_fs_write", "local_fs_write_file", "local_chat_write_file", "local_fs_mkdir", "local_fs_delete",
    "local_fs_rename", "local_fs_truncate", "local_fs_read_binary", "local_fs_write_binary", "local_chat_write_binary",
    "local_chat_data_paths",
    "process_start", "process_terminate", "process_wait", "process_stdin_write", "process_stdin_close",
    "process_stdout_read", "process_stderr_read", "process_list",
})

INTERACTIVE_ONLY_TOOLS = frozenset({
    "ask_user_choice",
})

# 仅管理员可成功执行；从非管理员的 tools 列表中移除，避免模型误调后反复失败
ADMIN_ONLY_AI_TOOLS = frozenset({
    "get_settings",
    "update_setting",
    "list_users",
    "get_user",
    "create_user",
    "update_user",
    "delete_user",
    "reset_user_password",
    "admin_unlock_user",
    "reset_user_system_ai_usage",
    "apply_system_ai_config_to_user",
    "write_aihelp_file",
    "update_aihelp_index",
    "list_user_feedback_admin",
    "reply_user_feedback_admin",
    "ignore_user_feedback_admin",
    "mark_all_user_feedback_read",
})


USER_SKILLS_AI_TOOLS = frozenset({
    "list_user_skills",
    "list_user_skill_groups",
    "create_user_skill_group",
    "update_user_skill_group",
    "delete_user_skill_group",
    "assign_user_skills_to_group",
    "get_user_skill",
    "save_user_skill",
    "delete_user_skill",
    "scan_user_skills",
    "read_user_skill_file",
    "list_user_skill_files",
    "write_user_skill_file",
    "delete_user_skill_file",
    "export_user_skills_config",
    "import_user_skills_config",
    "run_skill_script",
})


def get_tools_for_scope(scope: str | None, user: dict) -> list:
    """按会话范围与用户权限返回可用的工具列表。仅本机管理会话(session_scope=local)且管理员可见/可用本机工具；AI 助手与主机详情不包含本机工具。integration（OpenClaw/API 集成会话）与普通 default 使用同一套非本机工具。task 范围下移除交互型工具（如 ask_user_choice），避免后台任务阻塞等待用户回复。非管理员不暴露仅管理员工具，减少跨权误调。"""
    from services.tools_registry import merge_tools

    scope_val = (scope or "default").strip().lower() or "default"
    is_task = scope_val == "task"
    if scope_val in ("integration", "mcp_orchestrate", "mcp_runtime"):
        scope_val = "default"
    allow_local = scope_val == "local" and _is_admin(user)
    if allow_local:
        base = list(TOOLS)
    else:
        base = [t for t in TOOLS if t["function"]["name"] not in LOCAL_ONLY_TOOLS]
    if not _is_admin(user):
        base = [t for t in base if t["function"]["name"] not in ADMIN_ONLY_AI_TOOLS]
    if is_task:
        base = [t for t in base if t["function"]["name"] not in INTERACTIVE_ONLY_TOOLS]
        base = [t for t in base if t["function"]["name"] not in USER_SKILLS_AI_TOOLS]
    if not user.get("skills_enabled"):
        base = [t for t in base if t["function"]["name"] not in USER_SKILLS_AI_TOOLS]
    return merge_tools(base)


async def _get_host_row(host_id: int) -> dict | None:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM hosts WHERE id = ?", (host_id,))
    return dict(rows[0]) if rows else None


def _can_access_host(host_row: dict, user: dict) -> bool:
    """程序逻辑校验：仅管理员或主机创建人可操作该主机。"""
    return _is_admin(user) or (host_row.get("created_by") == user["id"])


async def _can_access_host_with_shares(host_row: dict, user: dict) -> bool:
    """程序逻辑校验：管理员、主机创建人或被分享用户可操作该主机。"""
    if _can_access_host(host_row, user):
        return True
    hid = host_row.get("id")
    if hid is None:
        return False
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT 1 FROM host_shares
           WHERE host_id = ? AND shared_with_user_id = ? AND revoked_at IS NULL
           LIMIT 1""",
        (hid, user["id"]),
    )
    return bool(rows)


def _can_access_group(group_row: dict, user: dict) -> bool:
    """程序逻辑校验：仅管理员或分组创建人可操作该分组。"""
    return _is_admin(user) or (group_row.get("created_by") == user["id"])


async def _cleanup_shared_host_group_members(host_id: int, shared_user_id: int) -> None:
    """分享撤销后，清理接收方分组中的残留主机关联。"""
    db = await get_db()
    await db.execute(
        """DELETE FROM host_group_members
           WHERE host_id = ?
             AND group_id IN (SELECT id FROM host_groups WHERE created_by = ?)""",
        (host_id, shared_user_id),
    )


async def _log_share_audit(
    *,
    actor_user_id: int,
    host_id: int | None,
    operation: str,
    params: dict | None = None,
    result: str = "success",
    source: str = "ai_tool",
) -> None:
    try:
        db = await get_db()
        await db.execute(
            """INSERT INTO operation_logs (user_id, host_id, operation, params, result, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                actor_user_id,
                host_id,
                operation,
                json.dumps(params or {}, ensure_ascii=False),
                result,
                source,
            ),
        )
    except Exception:
        pass


async def _log_credential_usage_audit(
    *,
    actor_user_id: int,
    host_row: dict,
    auth: dict | None,
    operation: str,
    stage: str = "execute",
    result: str = "success",
    extra: dict | None = None,
    source: str = "ai_tool",
) -> None:
    """记录凭证使用审计（不包含密码/私钥明文）。"""
    try:
        if not host_row:
            return
        host_id = host_row.get("id")
        credential_id = host_row.get("credential_id")
        auth_type = (auth or {}).get("auth_type") or ""
        username = (auth or {}).get("username") or ""
        params = {
            "operation": operation,
            "stage": stage,
            "host_id": host_id,
            "credential_id": credential_id,
            "credential_source": "credential_table" if credential_id else "host_inline",
            "auth_type": auth_type,
            "username": username,
            "has_password": bool((auth or {}).get("password")),
            "has_private_key": bool((auth or {}).get("private_key_pem")),
        }
        if extra:
            params.update(extra)
        db = await get_db()
        await db.execute(
            """INSERT INTO operation_logs (user_id, host_id, operation, params, result, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                actor_user_id,
                host_id,
                "credential_usage_audit",
                json.dumps(params, ensure_ascii=False),
                result,
                source,
            ),
        )
        await db.commit()
    except Exception:
        pass


async def _resolve_host_for_ai_ops(host_id: int, user: dict) -> tuple[dict | None, dict | None, str | None]:
    host_row = await _get_host_row(host_id)
    if not host_row:
        return None, None, f"主机 ID={host_id} 不存在"
    if not await _can_access_host_with_shares(host_row, user):
        return None, None, "无权操作该主机"
    auth = await _resolve_host_auth(await get_db(), host_row)
    if not auth or not auth.get("username"):
        return None, None, "主机未配置有效登录凭证"
    return host_row, auth, None


async def _edgeops_home_dir(host_row: dict, auth: dict) -> tuple[str | None, str | None]:
    stdout, stderr, code = await run_ssh_command(
        host=host_row["host"],
        port=int(host_row.get("port") or 22),
        username=auth.get("username") or "",
        auth_type=auth.get("auth_type") or "password",
        password=auth.get("password"),
        key_path=auth.get("key_path"),
        private_key_pem=auth.get("private_key_pem"),
        command='printf "%s" "$HOME/.edgeops"',
        timeout=20,
    )
    if code != 0:
        return None, (stderr or stdout or "获取主目录失败")
    base_dir = (stdout or "").strip() or "~/.edgeops"
    return base_dir, None


def _edgeops_storage_policy(host_row: dict | None) -> dict:
    """判断该主机是否应减少 .edgeops 落盘（如 ESXi/嵌入式/设备专用系统）。"""
    row = host_row or {}
    ht = (row.get("host_type") or "").strip().lower()
    hv = (row.get("host_version") or "").strip().lower()
    hs = (row.get("host_shell") or "").strip().lower()
    text = f"{ht} {hv} {hs}"
    constrained_keywords = (
        "esxi",
        "vmkernel",
        "busybox",
        "openwrt",
        "embedded",
        "appliance",
        "iot",
        "router",
        "switch",
        "交互机",
        "嵌入式",
        "设备专用",
        "专用系统",
    )
    if any(k in text for k in constrained_keywords):
        return {
            "constrained": True,
            "mode": "host_knowledge_preferred",
            "reason": f"检测到设备型/专用系统（host_type={row.get('host_type') or '未知'}）",
        }
    # 持久化白名单：命中才允许正常落盘。默认 linux/macos/windows，可由配置覆盖。
    whitelist = getattr(config, "EDGEOPS_PERSIST_HOST_TYPE_WHITELIST", None) or ["linux", "macos", "windows"]
    if ht and not any(w in ht for w in whitelist):
        return {
            "constrained": True,
            "mode": "host_knowledge_preferred",
            "reason": f"host_type 未命中持久化白名单（host_type={row.get('host_type') or '未知'}）",
        }
    return {"constrained": False, "mode": "normal", "reason": ""}


async def _edgeops_constrained_context(host_id: int, user_id: int, reason: str) -> dict:
    """受限主机时返回主机知识库上下文，替代 .edgeops 大量落盘。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT content, updated_at FROM ai_host_knowledge WHERE host_id = ? AND user_id = ?",
        (host_id, user_id),
    )
    content = (rows[0]["content"] or "").strip() if rows else ""
    updated_at = rows[0]["updated_at"] if rows else None
    if content:
        preview = content[:8000]
        if len(content) > 8000:
            preview += "\n…（已截断）"
    else:
        preview = "（暂无主机知识；建议优先使用 update_host_knowledge / append_host_knowledge 记录关键信息）"
    return {
        "constrained_mode": True,
        "reason": reason,
        "knowledge_updated_at": updated_at,
        "knowledge_context": preview,
    }


def _safe_script_name(name: str) -> str:
    raw = (name or "").strip().replace("\\", "/").split("/")[-1]
    if not raw:
        return ""
    if ".." in raw:
        return ""
    if "/" in raw:
        return ""
    return raw


def _safe_md_filename(name: str) -> str:
    raw = (name or "").strip().replace("\\", "/").split("/")[-1]
    if not raw:
        return ""
    if ".." in raw or "/" in raw:
        return ""
    if not raw.endswith(".md"):
        raw += ".md"
    return raw


def _upsert_scripts_index_row(index_text: str, script_name: str, purpose: str) -> str:
    header = "# Scripts Index\n\n| 脚本 | 简述 |\n|---|---|\n"
    text = (index_text or "").strip()
    if "| 脚本 | 简述 |" not in text:
        text = header
    if not text.endswith("\n"):
        text += "\n"
    row = f"| {script_name} | {(purpose or '（待补充）').replace('|', '/')} |"
    lines = text.splitlines()
    out = []
    found = False
    for ln in lines:
        if ln.startswith("| " + script_name + " |"):
            out.append(row)
            found = True
        else:
            out.append(ln)
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.append(row)
    return "\n".join(out).rstrip() + "\n"


def _is_high_risk_command(command: str) -> bool:
    cmd = (command or "").lower()
    risky_tokens = [
        " rm ", "rm -", " rm\n", "reboot", "shutdown", " poweroff",
        "mkfs", "fdisk", "parted", "dd if=", "iptables", "firewall-cmd",
        "systemctl restart", "systemctl stop", "service ", " userdel ", " groupdel ",
    ]
    wrapped = " " + cmd + " "
    return any(tok in wrapped for tok in risky_tokens)


async def _edgeops_auto_append_task_log(
    host_row: dict,
    auth: dict,
    task_dir_name: str,
    *,
    phase: str,
    action: str,
    result: str,
    details: str = "",
) -> None:
    policy = _edgeops_storage_policy(host_row)
    if policy.get("constrained"):
        return
    task_dir_name = (task_dir_name or "").strip()
    if not task_dir_name:
        return
    if ".." in task_dir_name or "/" in task_dir_name or "\\" in task_dir_name:
        return
    base_dir, err = await _edgeops_home_dir(host_row, auth)
    if err or not base_dir:
        return
    task_file = f"{base_dir}/tasks/{task_dir_name}/task.md"
    read_stdout, _, _ = await run_ssh_command(
        host=host_row["host"],
        port=int(host_row.get("port") or 22),
        username=auth.get("username") or "",
        auth_type=auth.get("auth_type") or "password",
        password=auth.get("password"),
        key_path=auth.get("key_path"),
        private_key_pem=auth.get("private_key_pem"),
        command=f'cat "{task_file}" 2>/dev/null || true',
        timeout=20,
    )
    content = (read_stdout or "").strip()
    if not content:
        return
    if "## 过程记录" not in content:
        content += "\n\n## 过程记录\n\n"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    block = (
        f"\n### {ts} | {phase}\n"
        f"- 动作: {action}\n"
        f"- 结果: {result}\n"
    )
    if details:
        block += f"- 详情:\n\n{details}\n"
    content = content.rstrip() + block + "\n"
    _ = await sftp_put_content(
        host=host_row["host"],
        port=int(host_row.get("port") or 22),
        username=auth.get("username") or "",
        auth_type=auth.get("auth_type") or "password",
        password=auth.get("password"),
        key_path=auth.get("key_path"),
        private_key_pem=auth.get("private_key_pem"),
        remote_path=task_file,
        content=content.encode("utf-8"),
        timeout=30,
    )


async def _get_effective_site_url(db) -> str:
    rows = await db.execute_fetchall("SELECT value FROM settings WHERE key = 'site_url' LIMIT 1")
    val = (rows[0]["value"] if rows else "") or ""
    return val.strip().rstrip("/")


def _schedule_temp_api_token_revoke(token_id: int, ttl_seconds: int) -> None:
    delay = max(1, int(ttl_seconds))

    async def _worker():
        try:
            await asyncio.sleep(delay)
            db = await get_db()
            await db.execute("DELETE FROM api_tokens WHERE id = ?", (int(token_id),))
            await db.commit()
        except Exception:
            logger.warning("临时 API Token 自动回收失败: token_id=%s", token_id, exc_info=True)

    asyncio.create_task(_worker())


def _make_temp_api_token_name(user: dict, purpose: str, expires_at: datetime) -> str:
    uname = (user.get("username") or "").strip() or f"u{user.get('id')}"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"tmp:{purpose}:{uname}:{stamp}:exp={expires_at.strftime('%H%M%S')}"[:128]


async def _create_temp_api_token(db, user: dict, *, purpose: str, ttl_seconds: int) -> tuple[int, str, str, str]:
    plain = "eop_tmp_" + secrets.token_urlsafe(24)
    token_hash = hash_api_access_token(plain)
    prefix = plain[:14] + "…" if len(plain) > 14 else plain
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(1, int(ttl_seconds)))
    name = _make_temp_api_token_name(user, purpose, expires_at)
    cur = await db.execute(
        """INSERT INTO api_tokens (user_id, name, token_hash, token_prefix)
           VALUES (?, ?, ?, ?)""",
        (user["id"], name, token_hash, prefix),
    )
    await db.commit()
    token_id = int(cur.lastrowid)
    _schedule_temp_api_token_revoke(token_id, ttl_seconds)
    return token_id, plain, prefix, expires_at.isoformat()


def _build_tcp22_probe_command(remote_host: str) -> str:
    return (
        "set -euo pipefail\n"
        f"HOST={shlex.quote((remote_host or '').strip())}\n"
        "if [ -z \"$HOST\" ]; then\n"
        "  echo PROBE_FAIL\n"
        "  exit 0\n"
        "fi\n"
        "if command -v nc >/dev/null 2>&1; then\n"
        "  if nc -z -w 3 \"$HOST\" 22 >/dev/null 2>&1; then echo PROBE_OK; else echo PROBE_FAIL; fi\n"
        "elif command -v bash >/dev/null 2>&1; then\n"
        "  if bash -lc \"</dev/tcp/$HOST/22\" >/dev/null 2>&1; then echo PROBE_OK; else echo PROBE_FAIL; fi\n"
        "elif command -v python3 >/dev/null 2>&1; then\n"
        "  python3 - \"$HOST\" <<'PY'\n"
        "import socket, sys\n"
        "h=sys.argv[1]\n"
        "s=socket.socket(); s.settimeout(3)\n"
        "try:\n"
        "    s.connect((h,22)); print('PROBE_OK')\n"
        "except Exception:\n"
        "    print('PROBE_FAIL')\n"
        "finally:\n"
        "    s.close()\n"
        "PY\n"
        "elif command -v python >/dev/null 2>&1; then\n"
        "  python - \"$HOST\" <<'PY'\n"
        "import socket, sys\n"
        "h=sys.argv[1]\n"
        "s=socket.socket(); s.settimeout(3)\n"
        "try:\n"
        "    s.connect((h,22)); print('PROBE_OK')\n"
        "except Exception:\n"
        "    print('PROBE_FAIL')\n"
        "finally:\n"
        "    s.close()\n"
        "PY\n"
        "else\n"
        "  echo PROBE_FAIL\n"
        "fi\n"
    )


async def _probe_tcp22_from_host(prober_host_row: dict, prober_auth: dict, remote_host: str) -> tuple[bool, str, str, int]:
    cmd = _build_tcp22_probe_command(remote_host)
    stdout, stderr, code = await run_ssh_command(
        host=prober_host_row["host"],
        port=int(prober_host_row.get("port") or 22),
        username=prober_auth.get("username") or "",
        auth_type=prober_auth.get("auth_type") or "password",
        password=prober_auth.get("password"),
        key_path=prober_auth.get("key_path"),
        private_key_pem=prober_auth.get("private_key_pem"),
        command=cmd,
        timeout=30,
    )
    ok = "PROBE_OK" in (stdout or "")
    return ok, stdout or "", stderr or "", int(code or 1)


def _build_direct_transfer_command(
    *,
    method: str,
    mode: str,
    local_path: str,
    remote_path: str,
    remote_host: str,
    remote_port: int,
    remote_user: str,
    remote_auth: dict,
) -> str:
    local_q = shlex.quote(local_path)
    remote_q = shlex.quote(remote_path)
    host_q = shlex.quote(remote_host)
    user_q = shlex.quote(remote_user)
    ssh_opts = f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p {int(remote_port or 22)}"
    scp_opts = f"-C -r -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -P {int(remote_port or 22)}"
    use_password = (remote_auth.get("auth_type") or "").strip().lower() == "password"
    remote_spec = f"{user_q}@{host_q}:{remote_q}"
    if method == "scp":
        body = ""
        if mode == "push":
            body += f"scp {scp_opts} -- {local_q} {remote_spec}\n"
        else:
            body += f"mkdir -p $(dirname {local_q})\n"
            body += f"scp {scp_opts} -- {remote_spec} {local_q}\n"
    elif method == "rsync":
        ssh_cmd = f"ssh {ssh_opts}"
        if mode == "push":
            body = f"rsync -a -e {shlex.quote(ssh_cmd)} -- {local_q} {user_q}@{host_q}:{remote_q}\n"
        else:
            body = f"mkdir -p $(dirname {local_q})\n"
            body += f"rsync -a -e {shlex.quote(ssh_cmd)} -- {user_q}@{host_q}:{remote_q} {local_q}\n"
    elif method == "sshfs":
        body = (
            "if ! command -v sshfs >/dev/null 2>&1; then\n"
            "  echo 'sshfs not installed' >&2\n"
            "  exit 127\n"
            "fi\n"
            "MNT=$(mktemp -d /tmp/edgeops-sshfs-XXXXXX)\n"
            "cleanup(){ fusermount -u \"$MNT\" >/dev/null 2>&1 || umount \"$MNT\" >/dev/null 2>&1 || true; rm -rf \"$MNT\"; }\n"
            "trap cleanup EXIT\n"
            f"RPATH={remote_q}\n"
            "RDIR=$(dirname \"$RPATH\")\n"
            "RBASE=$(basename \"$RPATH\")\n"
            f"sshfs {ssh_opts} {user_q}@{host_q}:\"$RDIR\" \"$MNT\"\n"
        )
        if mode == "push":
            body += (
                f"LPATH={local_q}\n"
                "if [ -d \"$LPATH\" ]; then\n"
                "  rm -rf \"$MNT/$RBASE\"\n"
                "  cp -a \"$LPATH\" \"$MNT/$RBASE\"\n"
                "else\n"
                "  cp -f \"$LPATH\" \"$MNT/$RBASE\"\n"
                "fi\n"
            )
        else:
            body += (
                f"LPATH={local_q}\n"
                "if [ -d \"$MNT/$RBASE\" ]; then\n"
                "  mkdir -p \"$LPATH\"\n"
                "  cp -a \"$MNT/$RBASE\"/. \"$LPATH\"/\n"
                "else\n"
                "  mkdir -p \"$(dirname \"$LPATH\")\"\n"
                "  cp -f \"$MNT/$RBASE\" \"$LPATH\"\n"
                "fi\n"
            )
    else:
        return ""

    if use_password:
        pw = remote_auth.get("password") or ""
        if method in ("scp", "rsync", "sshfs"):
            return (
                "set -euo pipefail\n"
                "if ! command -v sshpass >/dev/null 2>&1; then\n"
                "  echo 'sshpass not installed' >&2\n"
                "  exit 127\n"
                "fi\n"
                f"export SSHPASS={shlex.quote(pw)}\n"
                + body
                + "unset SSHPASS\n"
            )
    key_pem = (remote_auth.get("private_key_pem") or "").strip()
    if not use_password and key_pem:
        ssh_key_opt = f"-i \"$TMP_KEY\" {ssh_opts}"
        scp_key_opt = f"-i \"$TMP_KEY\" {scp_opts}"
        if method == "scp":
            body = ""
            if mode == "push":
                body += f"scp {scp_key_opt} -- {local_q} {remote_spec}\n"
            else:
                body += f"mkdir -p $(dirname {local_q})\n"
                body += f"scp {scp_key_opt} -- {remote_spec} {local_q}\n"
        elif method == "rsync":
            ssh_cmd = f"ssh {ssh_key_opt}"
            if mode == "push":
                body = f"rsync -a -e {shlex.quote(ssh_cmd)} -- {local_q} {user_q}@{host_q}:{remote_q}\n"
            else:
                body = f"mkdir -p $(dirname {local_q})\n"
                body += f"rsync -a -e {shlex.quote(ssh_cmd)} -- {user_q}@{host_q}:{remote_q} {local_q}\n"
        elif method == "sshfs":
            body = (
                "if ! command -v sshfs >/dev/null 2>&1; then\n"
                "  echo 'sshfs not installed' >&2\n"
                "  exit 127\n"
                "fi\n"
                "MNT=$(mktemp -d /tmp/edgeops-sshfs-XXXXXX)\n"
                "cleanup(){ fusermount -u \"$MNT\" >/dev/null 2>&1 || umount \"$MNT\" >/dev/null 2>&1 || true; rm -rf \"$MNT\"; }\n"
                "trap cleanup EXIT\n"
                f"RPATH={remote_q}\n"
                "RDIR=$(dirname \"$RPATH\")\n"
                "RBASE=$(basename \"$RPATH\")\n"
                f"sshfs -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p {int(remote_port or 22)} -o IdentityFile=\"$TMP_KEY\" {user_q}@{host_q}:\"$RDIR\" \"$MNT\"\n"
            )
            if mode == "push":
                body += (
                    f"LPATH={local_q}\n"
                    "if [ -d \"$LPATH\" ]; then\n"
                    "  rm -rf \"$MNT/$RBASE\"\n"
                    "  cp -a \"$LPATH\" \"$MNT/$RBASE\"\n"
                    "else\n"
                    "  cp -f \"$LPATH\" \"$MNT/$RBASE\"\n"
                    "fi\n"
                )
            else:
                body += (
                    f"LPATH={local_q}\n"
                    "if [ -d \"$MNT/$RBASE\" ]; then\n"
                    "  mkdir -p \"$LPATH\"\n"
                    "  cp -a \"$MNT/$RBASE\"/. \"$LPATH\"/\n"
                    "else\n"
                    "  mkdir -p \"$(dirname \"$LPATH\")\"\n"
                    "  cp -f \"$MNT/$RBASE\" \"$LPATH\"\n"
                    "fi\n"
                )
        return (
            "set -euo pipefail\n"
            "TMP_KEY=$(mktemp /tmp/edgeops-direct-XXXXXX.key)\n"
            "cleanup(){ rm -f \"$TMP_KEY\"; }\n"
            "trap cleanup EXIT\n"
            "cat > \"$TMP_KEY\" <<'__EDGEOPS_KEY__'\n"
            f"{key_pem}\n"
            "__EDGEOPS_KEY__\n"
            "chmod 600 \"$TMP_KEY\"\n"
            + body
        )
    return "set -euo pipefail\n" + body


def _normalize_create_chat_artifact_files(files_arg):
    """将模型常见误传格式规整为 files 数组（仍要求每项含 path/content）。"""
    if files_arg is None:
        return None
    if isinstance(files_arg, list):
        return files_arg if files_arg else None
    if isinstance(files_arg, dict):
        if files_arg.get("path") and "content" in files_arg:
            return [files_arg]
        return None
    if isinstance(files_arg, str):
        s = files_arg.strip()
        if not s:
            return None
        if s.startswith("[") or s.startswith("{"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list) and parsed:
                    return parsed
                if isinstance(parsed, dict) and parsed.get("path") and "content" in parsed:
                    return [parsed]
            except json.JSONDecodeError:
                pass
        low = s.lstrip().lower()
        if low.startswith("<!doctype") or low.startswith("<html") or s.lstrip().startswith("<"):
            return [{"path": "index.html", "content": files_arg}]
    return None


def _truncate_value(value, max_chars: int = 12000):
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    if len(text) <= max_chars:
        return value
    return {
        "truncated": True,
        "chars": len(text),
        "preview": text[: max_chars // 2] + "\n...<truncated>...\n" + text[-max_chars // 2 :],
    }


def _regex_flags(flags_raw) -> int:
    flags = 0
    for item in flags_raw or []:
        key = str(item or "").strip().lower().replace("-", "_")
        if key in ("i", "ignorecase", "ignore_case"):
            flags |= re.IGNORECASE
        elif key in ("m", "multiline"):
            flags |= re.MULTILINE
        elif key in ("s", "dotall"):
            flags |= re.DOTALL
        elif key in ("a", "ascii"):
            flags |= re.ASCII
    return flags


_MATH_NAMES = {name: getattr(math, name) for name in dir(math) if not name.startswith("_")}
_MATH_NAMES.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow})


def _safe_math_eval(expression: str):
    allowed_nodes = (
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Load,
        ast.Constant, ast.Tuple, ast.List, ast.Add, ast.Sub, ast.Mult, ast.Div,
        ast.FloorDiv, ast.Mod, ast.Pow, ast.UAdd, ast.USub,
    )
    tree = ast.parse(expression or "", mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise ValueError(f"不支持的表达式节点: {type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _MATH_NAMES:
                raise ValueError("仅允许调用 math 常用函数与 abs/round/min/max/sum/pow")
            if node.keywords:
                raise ValueError("不支持关键字参数")
        if isinstance(node, ast.Name) and node.id not in _MATH_NAMES:
            raise ValueError(f"未知名称: {node.id}")
    return eval(compile(tree, "<math_calculate>", "eval"), {"__builtins__": {}}, _MATH_NAMES)


_LINEAR_UNIT_TO_BASE = {
    "mm": ("length", 0.001), "cm": ("length", 0.01), "m": ("length", 1.0), "km": ("length", 1000.0),
    "in": ("length", 0.0254), "ft": ("length", 0.3048), "yd": ("length", 0.9144), "mi": ("length", 1609.344),
    "mg": ("mass", 0.000001), "g": ("mass", 0.001), "kg": ("mass", 1.0), "t": ("mass", 1000.0),
    "oz": ("mass", 0.028349523125), "lb": ("mass", 0.45359237),
    "n": ("force", 1.0), "kn": ("force", 1000.0), "mn": ("force", 1000000.0), "lbf": ("force", 4.4482216152605),
    "ms": ("time", 0.001), "s": ("time", 1.0), "min": ("time", 60.0), "h": ("time", 3600.0), "day": ("time", 86400.0),
}


def _convert_unit(value: float, from_unit: str, to_unit: str) -> float:
    f = (from_unit or "").strip().lower()
    t = (to_unit or "").strip().lower()
    if f in ("c", "celsius", "°c") and t in ("f", "fahrenheit", "°f"):
        return value * 9 / 5 + 32
    if f in ("f", "fahrenheit", "°f") and t in ("c", "celsius", "°c"):
        return (value - 32) * 5 / 9
    if f in ("c", "celsius", "°c") and t in ("k", "kelvin"):
        return value + 273.15
    if f in ("k", "kelvin") and t in ("c", "celsius", "°c"):
        return value - 273.15
    if f in ("f", "fahrenheit", "°f") and t in ("k", "kelvin"):
        return (value - 32) * 5 / 9 + 273.15
    if f in ("k", "kelvin") and t in ("f", "fahrenheit", "°f"):
        return (value - 273.15) * 9 / 5 + 32
    if f not in _LINEAR_UNIT_TO_BASE or t not in _LINEAR_UNIT_TO_BASE:
        raise ValueError("暂不支持该单位换算")
    cat_f, factor_f = _LINEAR_UNIT_TO_BASE[f]
    cat_t, factor_t = _LINEAR_UNIT_TO_BASE[t]
    if cat_f != cat_t:
        raise ValueError(f"单位维度不一致: {from_unit} -> {to_unit}")
    return value * factor_f / factor_t


def _parse_structured_data(raw: str, fmt: str = "auto"):
    fmt = (fmt or "auto").strip().lower()
    if fmt in ("auto", "json"):
        try:
            return json.loads(raw), "json"
        except Exception:
            if fmt == "json":
                raise
    if fmt in ("auto", "yaml", "yml"):
        if yaml is None:
            raise RuntimeError("YAML 解析需要安装 PyYAML")
        return yaml.safe_load(raw), "yaml"
    raise ValueError("format 仅支持 auto/json/yaml")


def _parse_path(path: str) -> list:
    parts = []
    token = ""
    i = 0
    src = path or ""
    while i < len(src):
        ch = src[i]
        if ch == ".":
            if token:
                parts.append(token)
                token = ""
            i += 1
            continue
        if ch == "[":
            if token:
                parts.append(token)
                token = ""
            j = src.find("]", i + 1)
            if j < 0:
                raise ValueError("路径方括号未闭合")
            idx = src[i + 1:j].strip().strip("\"'")
            parts.append(int(idx) if re.fullmatch(r"-?\d+", idx) else idx)
            i = j + 1
            continue
        token += ch
        i += 1
    if token:
        parts.append(token)
    return parts


def _get_path_value(data, path: str):
    cur = data
    for part in _parse_path(path):
        if isinstance(part, int):
            if not isinstance(cur, list):
                raise KeyError(f"当前值不是列表，不能访问索引 {part}")
            cur = cur[part]
        else:
            if not isinstance(cur, dict):
                raise KeyError(f"当前值不是对象，不能访问键 {part}")
            cur = cur[part]
    return cur


def _data_summary(data, max_depth: int = 4):
    def walk(value, depth=0):
        if depth >= max_depth:
            return {"type": type(value).__name__, "truncated_depth": True}
        if isinstance(value, dict):
            keys = list(value.keys())
            return {
                "type": "object",
                "size": len(value),
                "keys_preview": keys[:30],
                "children": {str(k): walk(value[k], depth + 1) for k in keys[:10]},
            }
        if isinstance(value, list):
            return {"type": "array", "size": len(value), "items_preview": [walk(v, depth + 1) for v in value[:5]]}
        return {"type": type(value).__name__, "value_preview": str(value)[:200]}
    return walk(data)


def _search_data(data, query: str, use_regex: bool, max_results: int):
    results = []
    rx = re.compile(query, re.IGNORECASE) if use_regex else None

    def matched(text: str) -> bool:
        return bool(rx.search(text)) if rx else (query.lower() in text.lower())

    def walk(value, path="$"):
        if len(results) >= max_results:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                kp = f"{path}.{k}"
                if matched(str(k)):
                    results.append({"path": kp, "match": "key", "key": str(k), "value_preview": str(v)[:300]})
                walk(v, kp)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")
        else:
            text = str(value)
            if matched(text):
                results.append({"path": path, "match": "value", "value": value})

    walk(data)
    return results


def _detect_markup_format(raw: str, fmt: str) -> str:
    fmt = (fmt or "auto").strip().lower()
    if fmt in ("html", "xml"):
        return fmt
    s = (raw or "").lstrip()[:500].lower()
    if "<!doctype html" in s or "<html" in s or "<body" in s or re.search(r"<(div|span|a|p|img|script|style|table|ul|ol|li|section|article|header|footer|main|form|input|button)(\s|>|/)", s):
        return "html"
    return "xml"


def _html_node_payload(node, attrs: list[str] | None = None):
    attrs = attrs or []
    payload = {
        "tag": getattr(node, "name", None),
        "text": node.get_text(" ", strip=True)[:1000],
    }
    if attrs:
        payload["attrs"] = {a: node.get(a) for a in attrs if node.has_attr(a)}
    else:
        payload["attrs"] = dict(node.attrs)
    return payload


def _xml_node_payload(node, attrs: list[str] | None = None):
    attrs = attrs or []
    text = "".join(node.itertext()).strip()
    payload = {
        "tag": node.tag,
        "text": text[:1000],
    }
    payload["attrs"] = {a: node.attrib.get(a) for a in attrs if a in node.attrib} if attrs else dict(node.attrib)
    return payload


def _markup_summary(raw: str, fmt: str):
    if fmt == "html":
        soup = BeautifulSoup(raw, "html.parser")
        counts = {}
        for tag in soup.find_all(True):
            counts[tag.name] = counts.get(tag.name, 0) + 1
        return {"format": "html", "tag_counts": counts, "title": (soup.title.get_text(" ", strip=True) if soup.title else "")}
    root = ET.fromstring(raw)
    counts = {}
    for elem in root.iter():
        counts[elem.tag] = counts.get(elem.tag, 0) + 1
    return {"format": "xml", "root": root.tag, "tag_counts": counts}


def _markup_search_text(raw: str, fmt: str, query: str, use_regex: bool, max_results: int):
    rx = re.compile(query, re.IGNORECASE) if use_regex else None

    def matched(text: str) -> bool:
        return bool(rx.search(text)) if rx else (query.lower() in text.lower())

    results = []
    if fmt == "html":
        soup = BeautifulSoup(raw, "html.parser")
        for node in soup.find_all(string=True):
            text = str(node).strip()
            if not text or not matched(text):
                continue
            parent = node.parent
            results.append({"tag": getattr(parent, "name", None), "text": text[:1000]})
            if len(results) >= max_results:
                break
        return results
    root = ET.fromstring(raw)
    for elem in root.iter():
        text = "".join(elem.itertext()).strip()
        if text and matched(text):
            results.append(_xml_node_payload(elem))
            if len(results) >= max_results:
                break
    return results


def _crypto_toolkit_impl(arguments: dict) -> dict:
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, padding as sym_padding, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa, padding as asym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.x509.oid import NameOID
    import datetime as _dt

    op = str(arguments.get("operation") or "").strip().lower()
    enc = str(arguments.get("encoding") or "base64").strip().lower()
    if enc not in ("base64", "hex"):
        enc = "base64"

    def _hash_alg(name: str):
        n = (name or "sha256").strip().lower()
        m = {
            "md5": hashlib.md5,
            "sha1": hashlib.sha1,
            "sha256": hashlib.sha256,
            "sha384": hashlib.sha384,
            "sha512": hashlib.sha512,
        }
        if n not in m:
            raise ValueError("不支持的哈希算法")
        return n, m[n]

    def _decode_bin(s: str, encoding: str) -> bytes:
        v = (s or "").strip()
        e = (encoding or "base64").strip().lower()
        if e == "hex":
            return bytes.fromhex(v)
        if e == "base64":
            return base64.b64decode(v.encode("ascii"), validate=True)
        raise ValueError("编码仅支持 base64 或 hex")

    def _encode_bin(b: bytes, encoding: str) -> str:
        e = (encoding or "base64").strip().lower()
        if e == "hex":
            return b.hex()
        return base64.b64encode(b).decode("ascii")

    def _hash_obj(name: str):
        n = (name or "sha256").strip().lower()
        m = {
            "sha1": hashes.SHA1,
            "sha256": hashes.SHA256,
            "sha384": hashes.SHA384,
            "sha512": hashes.SHA512,
        }
        if n not in m:
            raise ValueError("不支持的签名哈希算法")
        return m[n]()

    if op == "hash":
        algo_name, algo = _hash_alg(str(arguments.get("algorithm") or "sha256"))
        text = str(arguments.get("text") or "")
        digest = algo(text.encode("utf-8")).hexdigest()
        return {"success": True, "algorithm": algo_name, "hex": digest}

    if op == "text_to_hex":
        text = str(arguments.get("text") or "")
        return {"success": True, "hex": text.encode("utf-8").hex()}
    if op == "hex_to_text":
        hx = str(arguments.get("hex") or "")
        b = bytes.fromhex(hx)
        return {"success": True, "text": b.decode("utf-8")}
    if op == "bytes_to_hex":
        data_b64 = str(arguments.get("data") or "")
        b = _decode_bin(data_b64, "base64")
        return {"success": True, "hex": b.hex()}
    if op == "hex_to_bytes":
        hx = str(arguments.get("hex") or "")
        b = bytes.fromhex(hx)
        return {"success": True, "data": _encode_bin(b, "base64"), "encoding": "base64"}

    if op in ("aes_encrypt", "aes_decrypt", "des_encrypt", "des_decrypt"):
        key_enc = str(arguments.get("key_encoding") or enc).strip().lower()
        iv_enc = str(arguments.get("iv_encoding") or enc).strip().lower()
        key = _decode_bin(str(arguments.get("key") or ""), key_enc)
        iv = _decode_bin(str(arguments.get("iv") or ""), iv_enc)
        data_in = _decode_bin(str(arguments.get("data") or ""), enc)
        algo_name = str(arguments.get("algorithm") or "").strip().lower()
        is_encrypt = op.endswith("_encrypt")
        if op.startswith("aes_"):
            if algo_name in ("aes-gcm", "gcm"):
                aad = str(arguments.get("aad") or "").encode("utf-8")
                aesgcm = AESGCM(key)
                if is_encrypt:
                    ct_tag = aesgcm.encrypt(iv, data_in, aad)
                    return {
                        "success": True,
                        "data": _encode_bin(ct_tag[:-16], enc),
                        "tag": _encode_bin(ct_tag[-16:], enc),
                        "encoding": enc,
                    }
                tag = _decode_bin(str(arguments.get("tag") or ""), enc)
                plain = aesgcm.decrypt(iv, data_in + tag, aad)
                return {"success": True, "data": _encode_bin(plain, enc), "encoding": enc}
            # 默认 CBC
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            if is_encrypt:
                padder = sym_padding.PKCS7(128).padder()
                padded = padder.update(data_in) + padder.finalize()
                enc_data = cipher.encryptor().update(padded) + cipher.encryptor().finalize()
                return {"success": True, "data": _encode_bin(enc_data, enc), "encoding": enc}
            dec = cipher.decryptor().update(data_in) + cipher.decryptor().finalize()
            unpad = sym_padding.PKCS7(128).unpadder()
            plain = unpad.update(dec) + unpad.finalize()
            return {"success": True, "data": _encode_bin(plain, enc), "encoding": enc}
        # DES: 使用 3DES(CBC) 实现
        cipher = Cipher(algorithms.TripleDES(key), modes.CBC(iv))
        if is_encrypt:
            padder = sym_padding.PKCS7(64).padder()
            padded = padder.update(data_in) + padder.finalize()
            enc_data = cipher.encryptor().update(padded) + cipher.encryptor().finalize()
            return {"success": True, "data": _encode_bin(enc_data, enc), "encoding": enc, "algorithm": "3des-cbc"}
        dec = cipher.decryptor().update(data_in) + cipher.decryptor().finalize()
        unpad = sym_padding.PKCS7(64).unpadder()
        plain = unpad.update(dec) + unpad.finalize()
        return {"success": True, "data": _encode_bin(plain, enc), "encoding": enc, "algorithm": "3des-cbc"}

    if op == "rsa_generate_key":
        key_size = int(arguments.get("key_size") or 2048)
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=max(1024, key_size))
        pub = private_key.public_key()
        prv_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        pub_pem = pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        return {"success": True, "private_key_pem": prv_pem, "public_key_pem": pub_pem}

    if op in ("rsa_sign", "rsa_verify", "ecc_sign", "ecc_verify"):
        data = _decode_bin(str(arguments.get("data") or ""), enc)
        sig = str(arguments.get("signature") or "")
        hash_name = str(arguments.get("algorithm") or "sha256")
        hobj = _hash_obj(hash_name)
        if op == "rsa_sign":
            prv = serialization.load_pem_private_key(str(arguments.get("private_key_pem") or "").encode("utf-8"), password=None)
            signature = prv.sign(data, asym_padding.PKCS1v15(), hobj)
            return {"success": True, "signature": _encode_bin(signature, enc), "encoding": enc}
        if op == "rsa_verify":
            pub = serialization.load_pem_public_key(str(arguments.get("public_key_pem") or "").encode("utf-8"))
            sig_b = _decode_bin(sig, enc)
            try:
                pub.verify(sig_b, data, asym_padding.PKCS1v15(), hobj)
                return {"success": True, "verified": True}
            except InvalidSignature:
                return {"success": True, "verified": False}
        if op == "ecc_sign":
            prv = serialization.load_pem_private_key(str(arguments.get("private_key_pem") or "").encode("utf-8"), password=None)
            signature = prv.sign(data, ec.ECDSA(hobj))
            return {"success": True, "signature": _encode_bin(signature, enc), "encoding": enc}
        pub = serialization.load_pem_public_key(str(arguments.get("public_key_pem") or "").encode("utf-8"))
        sig_b = _decode_bin(sig, enc)
        try:
            pub.verify(sig_b, data, ec.ECDSA(hobj))
            return {"success": True, "verified": True}
        except InvalidSignature:
            return {"success": True, "verified": False}

    if op == "ecc_generate_key":
        curve_name = str(arguments.get("curve") or "secp256r1").strip().lower()
        curve_map = {
            "secp256r1": ec.SECP256R1,
            "prime256v1": ec.SECP256R1,
            "secp384r1": ec.SECP384R1,
            "secp521r1": ec.SECP521R1,
        }
        c = curve_map.get(curve_name)
        if c is None:
            raise ValueError("不支持的曲线")
        private_key = ec.generate_private_key(c())
        pub = private_key.public_key()
        prv_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        pub_pem = pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        return {"success": True, "private_key_pem": prv_pem, "public_key_pem": pub_pem}

    if op == "x509_generate_self_signed":
        cn = str(arguments.get("subject_cn") or "localhost").strip() or "localhost"
        days = max(1, int(arguments.get("days_valid") or 365))
        dns_names = arguments.get("dns_names") if isinstance(arguments.get("dns_names"), list) else []
        dns_names = [str(x).strip() for x in dns_names if str(x).strip()]
        key_alg = str(arguments.get("algorithm") or "rsa").strip().lower()
        if key_alg == "ecc":
            prv = ec.generate_private_key(ec.SECP256R1())
        else:
            key_size = max(1024, int(arguments.get("key_size") or 2048))
            prv = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(prv.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5))
            .not_valid_after(_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=days))
        )
        if dns_names:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(n) for n in dns_names]),
                critical=False,
            )
        cert = builder.sign(private_key=prv, algorithm=hashes.SHA256())
        cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
        key_pem = prv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        pub_pem = prv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        return {"success": True, "certificate_pem": cert_pem, "private_key_pem": key_pem, "public_key_pem": pub_pem}

    if op == "x509_parse":
        cert = x509.load_pem_x509_certificate(str(arguments.get("certificate_pem") or "").encode("utf-8"))
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            san_dns = san.get_values_for_type(x509.DNSName)
        except Exception:
            san_dns = []
        return {
            "success": True,
            "subject": cert.subject.rfc4514_string(),
            "issuer": cert.issuer.rfc4514_string(),
            "serial_number": str(cert.serial_number),
            "not_before": cert.not_valid_before_utc.isoformat(),
            "not_after": cert.not_valid_after_utc.isoformat(),
            "san_dns": san_dns,
            "signature_algorithm_oid": cert.signature_algorithm_oid.dotted_string,
        }

    if op == "x509_verify_signature":
        cert = x509.load_pem_x509_certificate(str(arguments.get("certificate_pem") or "").encode("utf-8"))
        issuer_pem = str(arguments.get("issuer_cert_pem") or "").strip()
        issuer_cert = x509.load_pem_x509_certificate(issuer_pem.encode("utf-8")) if issuer_pem else cert
        pub = issuer_cert.public_key()
        try:
            if isinstance(pub, rsa.RSAPublicKey):
                pub.verify(cert.signature, cert.tbs_certificate_bytes, asym_padding.PKCS1v15(), cert.signature_hash_algorithm)
            elif isinstance(pub, ec.EllipticCurvePublicKey):
                pub.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm))
            else:
                raise ValueError("不支持的签名公钥类型")
            return {"success": True, "verified": True}
        except InvalidSignature:
            return {"success": True, "verified": False}

    if op == "x509_match_key":
        cert = x509.load_pem_x509_certificate(str(arguments.get("certificate_pem") or "").encode("utf-8"))
        prv = serialization.load_pem_private_key(str(arguments.get("private_key_pem") or "").encode("utf-8"), password=None)
        cert_pub = cert.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key_pub = prv.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return {"success": True, "matched": cert_pub == key_pub}

    raise ValueError("不支持的 operation")


def _scheduled_task_dict_for_tool(row) -> dict:
    d = dict(row)
    stored = d.get("notify_email_to") or ""
    content = d.get("content") or ""
    disp = effective_scheduled_task_notify_email_to(stored, content)
    d["notify_email_display"] = disp
    d["notify_email_inferred"] = bool(not (stored or "").strip() and disp)
    return d


def _arg_session_managed(arguments: dict) -> bool:
    """解析 session_managed：默认 True（会话产物归位 chats/sessions/<id>/）；False 则按 path 精确落盘。"""
    v = arguments.get("session_managed")
    if v is None:
        return True
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ("0", "false", "no", "off")


# 工作区根下常见「持久/指定」目录：path 以此开头且未显式 session_managed 时按精确路径读写
_EXPLICIT_FS_TOPLEVEL_DIRS = frozenset({
    "scripts", "exchange", "data", "backups", "backup", "templates", "template",
    "exports", "export", "imports", "import", "staging", "workspace", "projects",
    "project", "lib", "libs", "cache", "tmp", "temp", "public", "static", "tools",
    "bin", "configs", "config", "archive", "archives", "packages", "pkg", "repos",
    "repo", "output", "outputs", "downloads", "download", "upload", "uploads", "files",
    "assets", "media", "docs", "notes", "reports", "report", "build", "builds",
    "dist", "release", "releases", "vendor", "vendors", "share", "shared",
    "memory",  # 用户长期 Memory 空间（勿归位 chats）
})
_CHATS_DATE_PREFIX_RE = re.compile(r"^chats/\d{4}/\d{2}/\d{2}/", re.I)
_CHATS_SESSION_PREFIX_RE = re.compile(r"^chats/sessions/\d+/", re.I)
_LOCAL_DATE_PREFIX_RE = re.compile(r"^local/\d{4}/\d{2}/\d{2}/", re.I)


def _looks_like_explicit_workspace_path(path: str, *, base=None) -> bool:
    """path 是否像用户指定的完整相对路径（非会话临时逻辑名）。"""
    try:
        rel = coerce_fs_relative_path(path or "", base)
    except ValueError:
        return False
    if not rel:
        return False
    low = rel.lower()
    if (
        _CHATS_DATE_PREFIX_RE.match(low)
        or _CHATS_SESSION_PREFIX_RE.match(low)
        or _LOCAL_DATE_PREFIX_RE.match(low)
    ):
        return True
    first = low.split("/")[0]
    return first in _EXPLICIT_FS_TOPLEVEL_DIRS


def _effective_session_managed(arguments: dict, path: str, *, base=None) -> bool:
    """未传 session_managed 时：逻辑短路径 → 归位 chats/sessions/<id>/；完整/指定目录路径 → 精确读写。"""
    if arguments.get("session_managed") is not None:
        return _arg_session_managed(arguments)
    return not _looks_like_explicit_workspace_path(path, base=base)


def _fs_safe_suffix(path_like: str) -> str:
    raw_name = (path_like or "").replace("\\", "/").strip().split("/")[-1]
    p = Path(raw_name)
    suffix = p.suffix or ""
    if len(suffix) > 16:
        suffix = ""
    if suffix and not re.fullmatch(r"\.[A-Za-z0-9._-]+", suffix):
        suffix = ""
    return suffix


def _fs_safe_stem(path_like: str, fallback: str) -> str:
    raw_name = (path_like or "").replace("\\", "/").strip().split("/")[-1]
    stem = Path(raw_name).stem or fallback
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or fallback
    return stem[:48]


def _fs_safe_subdir(path_like: str) -> str:
    raw = (path_like or "").replace("\\", "/").strip().lstrip("/")
    if not raw:
        return ""
    parts = raw.split("/")
    if len(parts) <= 1:
        return ""
    cleaned: list[str] = []
    for seg in parts[:-1]:
        s = re.sub(r"[^A-Za-z0-9._-]+", "_", (seg or "").strip())
        s = s.strip("._-")
        if not s or s in (".", ".."):
            continue
        cleaned.append(s[:64])
    return "/".join(cleaned[:8])


def _chat_managed_relative_path(
    requested_path: str,
    *,
    local_scope: bool = False,
    fallback_ext: str = ".txt",
    session_id: int | None = None,
) -> str:
    """会话相关落盘：普通会话 → chats/sessions/<id>/；本机 → local/<UTC>/；文件名加 UUID 前缀。"""
    from api.chat_attachments import session_storage_subdir as _sess_sub

    date_dir = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    if local_scope:
        prefix = f"local/{date_dir}"
        strip_prefixes = [prefix, date_dir]
    else:
        sess_sub = _sess_sub(session_id)
        prefix = f"chats/{sess_sub}"
        strip_prefixes = [prefix, f"chats/{date_dir}", date_dir, sess_sub]
    rp = (requested_path or "").replace("\\", "/").strip().lstrip("/")
    for pref in strip_prefixes:
        low = rp.lower()
        pl = pref.lower()
        if low.startswith(pl + "/"):
            rp = rp[len(pref) + 1 :].lstrip("/")
            break
        if low == pl:
            rp = ""
            break
    if local_scope:
        rp = re.sub(r"^local/", "", rp, count=1, flags=re.I).lstrip("/")
    if not rp:
        rp = "output.txt"
    subdir = _fs_safe_subdir(rp)
    ext = _fs_safe_suffix(rp) or fallback_ext
    stem = _fs_safe_stem(rp, "file")
    uid = str(uuid4())
    filename = f"{uid}-{stem}{ext}"
    if subdir:
        return f"{prefix}/{subdir}/{filename}"
    return f"{prefix}/{filename}"


def _resolve_fs_write_relative_path(
    requested_path: str,
    *,
    session_managed: bool = True,
    local_scope: bool = False,
    fallback_ext: str = ".txt",
    base=None,
    session_id: int | None = None,
) -> str:
    """session_managed=True 归位 chats/sessions/<id>/ 或 local 日期目录；False 则精确路径。"""
    if session_managed:
        return _chat_managed_relative_path(
            requested_path,
            local_scope=local_scope,
            fallback_ext=fallback_ext,
            session_id=session_id,
        )
    rel = coerce_fs_relative_path(requested_path or "", base)
    if not rel:
        raise ValueError("缺少有效 path（session_managed=false 时需传完整相对路径）")
    return rel


def _normalize_sftp_pull_local_path(
    raw_local: str,
    remote_path: str,
    *,
    as_directory: bool,
    session_managed: bool = True,
    session_id: int | None = None,
) -> str:
    """session_managed=True 时归位 chats/sessions/<id>/ 并补 UUID；False 时按 local_path 精确落盘。"""
    if not session_managed:
        norm = coerce_fs_relative_path(raw_local or "")
        if not norm:
            name = Path(remote_path.replace("\\", "/")).name or "pull.bin"
            norm = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")[:96] or "pull.bin"
        return norm
    from api.chat_attachments import session_storage_subdir as _sess_sub

    sess_prefix = f"chats/{_sess_sub(session_id)}"
    norm = (raw_local or "").replace("\\", "/").strip().lstrip("/")
    if not norm.lower().startswith("chats/"):
        norm = f"{sess_prefix}/{norm}"
    p_part = Path(norm)
    stem = p_part.stem
    suf = p_part.suffix
    if as_directory:
        if not re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            stem,
            re.I,
        ):
            slug = re.sub(r"[^A-Za-z0-9._-]+", "_", stem or "").strip("._-")[:48] or "pull-dir"
            stem = f"{uuid4()}-{slug}"
        return str(p_part.parent / stem).replace("\\", "/")
    if not suf:
        suf = Path(remote_path.replace("\\", "/")).suffix or ".txt"
    if not re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        stem,
        re.I,
    ):
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", stem or "").strip("._-")[:48] or "pull"
        stem = f"{uuid4()}-{slug}"
    return str(p_part.parent / f"{stem}{suf}").replace("\\", "/")


def _sftp_timeout_from_args(arguments: dict, default: int = 300) -> int:
    try:
        timeout = int(arguments.get("timeout") or default)
    except (TypeError, ValueError):
        timeout = default
    return max(30, min(3600, timeout))


def _http_timeout_from_args(arguments: dict) -> int | None:
    if arguments.get("timeout") is None:
        return None
    try:
        timeout = int(arguments.get("timeout"))
    except (TypeError, ValueError):
        return None
    return max(5, min(timeout, int(getattr(config, "HTTP_TOOL_MAX_TIMEOUT_SEC", 3600))))


def _http_transfer_cap(arguments: dict, config_attr: str) -> int | None:
    from services.http_transfer import _resolve_transfer_cap

    raw = arguments.get("max_bytes")
    if raw is None:
        return _resolve_transfer_cap(None, config_attr)
    try:
        return _resolve_transfer_cap(int(raw), config_attr)
    except (TypeError, ValueError):
        return _resolve_transfer_cap(None, config_attr)


def _scp_pull_byte_caps(arguments: dict | None = None) -> tuple[int, int]:
    """返回 (单文件上限, 整树上限)；0 表示不限制。"""
    arguments = arguments or {}
    try:
        sys_file = int(getattr(config, "SCP_PULL_MAX_BYTES", 0) or 0)
    except (TypeError, ValueError):
        sys_file = 0
    try:
        sys_tree = int(getattr(config, "SCP_PULL_MAX_TREE_BYTES", 0) or 0)
    except (TypeError, ValueError):
        sys_tree = 0
    if sys_file < 0:
        sys_file = 0
    if sys_tree < 0:
        sys_tree = 0
    raw = arguments.get("max_bytes")
    if raw is None or raw == "":
        max_bytes = sys_file
    else:
        try:
            max_bytes = int(raw)
        except (TypeError, ValueError):
            max_bytes = sys_file
        if max_bytes < 0:
            max_bytes = 0
        # 系统配置了上限时：用户 0/不限 → 用系统上限；用户更大 → 钳到系统上限
        if sys_file > 0:
            max_bytes = sys_file if max_bytes <= 0 else min(max_bytes, sys_file)
    return max_bytes, sys_tree


def _http_result_payload(result) -> dict:
    out: dict = {
        "success": result.success,
        "url": result.url,
        "duration_sec": result.duration_sec,
    }
    if result.status_code is not None:
        out["status_code"] = result.status_code
    if result.response_headers:
        out["headers"] = result.response_headers
    if result.content_type:
        out["content_type"] = result.content_type
    if result.body_text is not None:
        out["body"] = result.body_text
    if result.body_base64 is not None:
        out["body_base64"] = result.body_base64
    if result.bytes_transferred:
        out["bytes_transferred"] = result.bytes_transferred
    if result.local_path:
        out["local_path"] = result.local_path
    if result.truncated:
        out["truncated"] = True
    if result.interrupted:
        out["interrupted"] = True
    if getattr(result, "content_length", None) is not None:
        out["content_length"] = result.content_length
    if getattr(result, "chunks_total", None) is not None:
        out["chunks_total"] = result.chunks_total
    if getattr(result, "chunk_index", None) is not None:
        out["chunk_index"] = result.chunk_index
    if getattr(result, "chunk_size", None) is not None:
        out["chunk_size"] = result.chunk_size
    if getattr(result, "chunk_paths", None):
        out["chunk_paths"] = result.chunk_paths
    if getattr(result, "merged", False):
        out["merged"] = True
    if getattr(result, "accept_ranges", False):
        out["accept_ranges"] = True
    if result.error:
        out["error"] = result.error
    return out


async def _probe_remote_path_kind(host_row: dict, auth: dict, remote_path: str) -> tuple[str | None, str, str, int]:
    """探测远端路径为 file 或 dir。返回 (kind, stdout, stderr, exit_code)。"""
    script = (
        "set -euo pipefail\n"
        f"SRC={shlex.quote(remote_path)}\n"
        "if [ -d \"$SRC\" ]; then echo TYPE=dir\n"
        "elif [ -f \"$SRC\" ]; then echo TYPE=file\n"
        "else echo \"路径不存在或不可读: $SRC\" >&2; exit 12\n"
        "fi\n"
    )
    out, err, code = await run_ssh_command(
        host=host_row["host"],
        port=int(host_row.get("port") or 22),
        username=auth.get("username") or "",
        auth_type=auth.get("auth_type") or "password",
        password=auth.get("password"),
        key_path=auth.get("key_path"),
        private_key_pem=auth.get("private_key_pem"),
        command=script,
        timeout=60,
    )
    kind = None
    if int(code or 1) == 0:
        for ln in (out or "").splitlines():
            line = ln.strip()
            if line.startswith("TYPE="):
                kind = line[5:].strip() or None
    return kind, out or "", err or "", int(code or 1)


def _relay_default_staging_path(source_path: str, *, is_dir: bool) -> str:
    name = Path(source_path.replace("\\", "/")).name or "payload.bin"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    token = secrets.token_hex(4)
    if is_dir:
        return f"exchange/{ts}-{token}/{name}"
    return f"exchange/{ts}-{token}-{name}"


def _profile_tool_target_uid(user: dict, arguments: dict) -> tuple[int | None, str | None]:
    uid = user["id"]
    if arguments.get("user_id") is not None:
        if not _is_admin(user):
            return None, "需要管理员权限"
        uid = int(arguments["user_id"])
    return uid, None


async def _profile_tool_resolve_row(db, uid: int, arguments: dict):
    from services.ai_model_profiles import get_profile_row, get_profile_row_by_name

    pid = arguments.get("profile_id")
    if pid is not None:
        row = await get_profile_row(db, uid, int(pid))
        if row:
            return row, None
        return None, "配置不存在"
    pname = (arguments.get("profile_name") or "").strip()
    if pname:
        row = await get_profile_row_by_name(db, uid, pname)
        if row:
            return row, None
        return None, f"未找到名为「{pname}」的配置"
    return None, "请提供 profile_id 或 profile_name"


def _profile_create_fields_from_tool_args(arguments: dict) -> dict:
    provider = (arguments.get("provider") or "").strip()
    if provider not in ("aliyun", "ollama", "openai"):
        provider = ""
    out_loc = (arguments.get("output_locale") or "").strip()
    if out_loc not in ("", "en", "zh-CN"):
        out_loc = ""
    try:
        ctx = max(0, int(arguments.get("context_size") or 0))
    except (TypeError, ValueError):
        ctx = 0
    try:
        steps = max(0, int(arguments.get("agent_max_steps") or 0))
    except (TypeError, ValueError):
        steps = 0
    try:
        rounds = max(0, int(arguments.get("assistant_max_rounds") or 0))
    except (TypeError, ValueError):
        rounds = 0
    return {
        "api_key": (arguments.get("api_key") or "").strip(),
        "base_url": (arguments.get("base_url") or "").strip().rstrip("/"),
        "model": (arguments.get("model") or "").strip(),
        "system_prompt": (arguments.get("system_prompt") or "").strip(),
        "auto_approve": bool(arguments.get("auto_approve")),
        "assistant_enabled": bool(arguments.get("assistant_enabled")),
        "context_size": ctx,
        "provider": provider,
        "agent_max_steps": steps,
        "assistant_max_rounds": rounds,
        "vision_enabled": arguments.get("vision_enabled", True) is not False,
        "output_locale": out_loc,
    }


def _profile_patch_from_tool_args(arguments: dict) -> dict:
    patch: dict = {}
    if "api_key" in arguments:
        ak = str(arguments.get("api_key") or "").strip()
        if ak not in ("", "***"):
            patch["api_key"] = ak
    for key in ("base_url", "model", "system_prompt"):
        if key in arguments and arguments.get(key) is not None:
            val = str(arguments.get(key) or "").strip()
            if key == "base_url":
                val = val.rstrip("/")
            patch[key] = val
    for key in ("auto_approve", "assistant_enabled", "vision_enabled"):
        if key in arguments and arguments.get(key) is not None:
            patch[key] = bool(arguments.get(key))
    if "context_size" in arguments and arguments.get("context_size") is not None:
        try:
            patch["context_size"] = max(0, int(arguments.get("context_size")))
        except (TypeError, ValueError):
            patch["context_size"] = 0
    if "provider" in arguments and arguments.get("provider") is not None:
        provider = str(arguments.get("provider") or "").strip()
        patch["provider"] = provider if provider in ("aliyun", "ollama", "openai") else ""
    for key in ("agent_max_steps", "assistant_max_rounds"):
        if key in arguments and arguments.get(key) is not None:
            try:
                patch[key] = max(0, int(arguments.get(key)))
            except (TypeError, ValueError):
                patch[key] = 0
    if "output_locale" in arguments and arguments.get("output_locale") is not None:
        out_loc = str(arguments.get("output_locale") or "").strip()
        patch["output_locale"] = out_loc if out_loc in ("", "en", "zh-CN") else ""
    return patch


async def execute_tool(name: str, arguments: dict, user: dict, scope: str | None = None, terminal_scope_id: str | None = None, default_terminal_slot: int | None = None, task_id: int | None = None, ui_capable: bool = True, stream_callback=None, session_id: int | None = None, ui_locale: str | None = None, transfer_cancel_event=None, chat_mode: str | None = None) -> str:
    """执行工具，返回 JSON 字符串。scope 由调用方传入：'local' 表示本机管理会话；'task' 表示后台任务（此时 task_id 为任务 ID，SSH 通道绑定到该任务）。
    ui_capable: 调用方是否带浏览器交互（False 表示 OpenClaw 集成 / 后台任务等无 UI 场景，
    `ask_user_choice` 等需要前端渲染的工具会改用纯文本回退）。task scope 自动视为 ui_capable=False。
    stream_callback: 可选的 async 回调 `fn(event: dict) -> None`。当工具支持流式进度时
    （`delegate_to_cli_agent` / `delegate_chain` / `run_workflow_template` / `delegate_to_edgeops_ai` /
    `delegate_sub_tasks_batch` / `scp_push` / `scp_pull`），
    工具会边跑边调该回调把增量事件推给调用方（例如 SSE 生成器），事件 dict 必含 `kind` 字段
    （sub_agent_line / chain_step_start / sub_ai_step / sub_ai_tool / sub_ai_done /
    sub_ai_batch_start / sub_ai_batch_end / chain_step_line / chain_step_end / chain_step_skip）。
    ui_locale: 可选；浏览器/界面 BCP-47（如 zh-CN、en），传入时子技能（如 delegate_to_edgeops_ai）可将回复语言策略与界面一致。
    chat_mode: 可选；会话聊天模式。问答模式下写类工具在本函数入口硬拒（含嵌套调用）；未传时按 session_id 读库。
    权限由本函数内程序逻辑强制校验，不依赖 AI 描述。本机类工具仅在本机管理会话中且仅管理员可调用。
    ADMIN_ONLY_AI_TOOLS 在入口统一拒绝非管理员（即使绕过 tools 列表）。"""
    try:
        # —— 管理员专属工具：代码门禁（不依赖提示词 / tools 列表过滤）——
        if name in ADMIN_ONLY_AI_TOOLS and not _is_admin(user):
            return json.dumps(
                {
                    "success": False,
                    "error": "需要管理员权限",
                    "code": "admin_required",
                    "tool": name,
                },
                ensure_ascii=False,
            )
        # —— 问答 / 严格模式硬门禁（execute_tool 入口，不可被 agent 循环旁路）——
        try:
            from services.chat_mode_enforce import (
                enforce_qa_tool_block,
                enforce_strict_tool_block,
            )

            _qa_block = await enforce_qa_tool_block(
                name,
                arguments if isinstance(arguments, dict) else {},
                session_id=session_id,
                chat_mode=chat_mode,
            )
            if _qa_block is not None:
                return _qa_block
            _strict_block = await enforce_strict_tool_block(
                name,
                arguments if isinstance(arguments, dict) else {},
                session_id=session_id,
                chat_mode=chat_mode,
            )
            if _strict_block is not None:
                return _strict_block
        except Exception as _qa_enf_exc:
            # 门禁自身异常时：对已知写类工具仍 fail-closed
            from services.chat_mode_gate import (
                is_qa_blocked,
                needs_strict_confirm,
                qa_blocked_tool_result,
                normalize_chat_mode,
            )

            _mode_guess = normalize_chat_mode(chat_mode) if chat_mode else "qa"
            if _mode_guess == "qa" and is_qa_blocked(name):
                logger.warning(
                    "QA enforce 异常仍硬拒 tool=%s: %s", name, _qa_enf_exc
                )
                return json.dumps(
                    qa_blocked_tool_result(
                        name, arguments if isinstance(arguments, dict) else {}
                    ),
                    ensure_ascii=False,
                )
            if _mode_guess == "strict" and needs_strict_confirm(name):
                logger.warning(
                    "Strict enforce 异常仍硬拒 tool=%s: %s", name, _qa_enf_exc
                )
                return json.dumps(
                    {
                        "success": False,
                        "error": "工具调用未获用户批准或安全检查异常，已取消执行。",
                        "mode": "strict",
                        "enforced_at": "execute_tool",
                        "user_decision": "deny",
                    },
                    ensure_ascii=False,
                )

        if (name or "").startswith("user_mcp_"):
            from services.user_mcp_client import invoke_user_mcp_tool

            return await invoke_user_mcp_tool(user, name, arguments, session_id=session_id)
        # Memory / get_session_prompt
        try:
            from services.user_memory import MEMORY_TOOL_NAMES, execute_memory_tool

            if name in MEMORY_TOOL_NAMES:
                _mem_out = await execute_memory_tool(name, arguments or {}, user, session_id=session_id)
                if _mem_out is not None:
                    return _mem_out
        except Exception as _mem_exc:
            return json.dumps(
                {"success": False, "error": f"memory 工具失败: {_mem_exc}", "tool": name},
                ensure_ascii=False,
            )
        # 插件注册表 handler（P2-7）
        try:
            from services.tools_registry import get_extra_handler

            _extra_fn = get_extra_handler(name)
            if _extra_fn is not None:
                if asyncio.iscoroutinefunction(_extra_fn):
                    return await _extra_fn(
                        arguments or {},
                        user=user,
                        scope=scope,
                        session_id=session_id,
                        task_id=task_id,
                    )
                return _extra_fn(
                    arguments or {},
                    user=user,
                    scope=scope,
                    session_id=session_id,
                    task_id=task_id,
                )
        except Exception:
            pass
        if name == "run_skill_script":
            from services.run_skill_script import run_skill_script as _run_skill_script

            return await _run_skill_script(
                user,
                skill_name=str((arguments or {}).get("skill_name") or ""),
                script=str((arguments or {}).get("script") or ""),
                args=(arguments or {}).get("args")
                if isinstance((arguments or {}).get("args"), list)
                else None,
                timeout_sec=int((arguments or {}).get("timeout_sec") or 30),
            )
        def _safe_optional_subdir_only(s: str) -> str:
            """本机管理 local_chat_data_paths：仅子目录段净化，不含文件名。"""
            clean: list[str] = []
            for seg in (s or "").replace("\\", "/").strip().strip("/").split("/"):
                t = re.sub(r"[^A-Za-z0-9._-]+", "_", (seg or "").strip())
                t = t.strip("._-")
                if t and t not in (".", ".."):
                    clean.append(t[:64])
            return "/".join(clean[:8])

        terminal_scope_id = normalize_terminal_scope_id(terminal_scope_id)
        if task_id is not None and scope != "task":
            scope = "task"
        if scope == "task" and task_id is not None and not terminal_scope_id:
            terminal_scope_id = str(task_id)
        if scope == "task":
            ui_capable = False
        if default_terminal_slot is not None:
            try:
                default_terminal_slot = max(0, min(31, int(default_terminal_slot)))
            except (TypeError, ValueError):
                default_terminal_slot = None

        def ssh_ai_terminals() -> list[dict]:
            items = get_terminals_for_user(user["id"], scope_id=terminal_scope_id)
            return [it for it in items if (it.get("created_by") or "") == "ai"]

        def terminal_meta_for_slot(slot: int | None) -> dict | None:
            return get_terminal_session_meta_for_user(user["id"], slot, terminal_scope_id)

        def attach_terminal_host_fields(out: dict, slot: int | None) -> dict:
            meta = terminal_meta_for_slot(slot)
            if meta:
                out["host_id"] = meta.get("host_id")
                out["host_name"] = meta.get("host_name") or ""
                out["host_ip"] = meta.get("host_ip") or ""
                out["host_port"] = meta.get("host_port") or 22
                out["host_aliases"] = meta.get("host_aliases") or []
                out["created_by"] = meta.get("created_by") or "ai"
                out["connected"] = meta.get("connected")
            return out

        def attach_terminals_snapshot(out: dict) -> dict:
            snap = terminals_snapshot_for_ai(user["id"], terminal_scope_id, default_terminal_slot)
            out["terminals"] = snap["ai_terminals"]
            out["terminal_scope_id"] = snap["scope_id"]
            out["user_terminals_readonly"] = snap["user_terminals"]
            return out

        def resolve_ai_slot(requested_slot, host_id_hint=None) -> tuple[int | None, str | None]:
            return resolve_ai_slot_for_user(
                user["id"],
                terminal_scope_id,
                requested_slot,
                host_id_hint,
                default_terminal_slot,
            )

        def resolve_local_slot(requested_slot) -> tuple[int | None, str | None]:
            from api import local_host

            return local_host.resolve_local_slot(
                user["id"], terminal_scope_id, requested_slot, default_terminal_slot
            )

        if name in LOCAL_ONLY_TOOLS:
            scope_val = (scope or "default").strip().lower() or "default"
            if scope_val != "local":
                return json.dumps({"success": False, "error": "该功能仅在本机管理会话中可用，当前为 AI 助手或主机详情会话"}, ensure_ascii=False)
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
        if name in (
            "list_service_credentials",
            "add_service_credential",
            "update_service_credential",
            "delete_service_credential",
            "send_service_password",
        ):
            from services.credential_vault import credentials_vault_enabled

            if not await credentials_vault_enabled():
                return json.dumps(
                    {
                        "success": False,
                        "error": "凭证库功能未启用。请管理员在系统设置中将 credentials_vault_enabled 设为 true。",
                    },
                    ensure_ascii=False,
                )
        if name == "list_hosts":
            db = await get_db()
            group_id = arguments.get("group_id")
            tag_ids_raw = arguments.get("tag_ids")
            q_raw = (arguments.get("q") or arguments.get("search") or "").strip()
            lim = min(500, max(1, int(arguments.get("limit") or 100))) if q_raw else None
            tag_ids: list[int] = []
            if isinstance(tag_ids_raw, (list, tuple)):
                for x in tag_ids_raw:
                    try:
                        if x is not None:
                            tag_ids.append(int(x))
                    except (TypeError, ValueError):
                        continue
                tag_ids = sorted(set(tag_ids))

            sel_cols = HOST_LIST_SELECT_COLS

            def _search_sql_h(alias: str = "h") -> tuple[str, list]:
                if not q_raw:
                    return "", []
                like = f"%{q_raw.lower()}%"
                parts = [
                    f"LOWER({alias}.name) LIKE ?",
                    f"LOWER({alias}.host) LIKE ?",
                    f"CAST({alias}.port AS TEXT) LIKE ?",
                    f"LOWER(IFNULL({alias}.description,'')) LIKE ?",
                    f"LOWER(IFNULL({alias}.remark,'')) LIKE ?",
                    f"LOWER(IFNULL({alias}.aliases,'')) LIKE ?",
                    f"LOWER(IFNULL({alias}.host_type,'')) LIKE ?",
                    f"""EXISTS (
                        SELECT 1 FROM host_user_tags hutq
                        JOIN host_tags tq ON tq.id = hutq.tag_id
                        WHERE hutq.user_id = ? AND hutq.host_id = {alias}.id
                          AND tq.created_by = ? AND LOWER(tq.name) LIKE ?
                    )""",
                ]
                bind = [like, like, like, like, like, like, like, user["id"], user["id"], like]
                if q_raw.isdigit():
                    parts.append(f"{alias}.id = ?")
                    try:
                        bind.append(int(q_raw))
                    except ValueError:
                        pass
                return "(" + " OR ".join(parts) + ")", bind

            search_where, search_params = _search_sql_h("h")
            tag_filter_where = ""
            tag_filter_params: list = []
            if tag_ids:
                ph = ",".join(["?"] * len(tag_ids))
                tag_filter_where = (
                    f"EXISTS (SELECT 1 FROM host_user_tags hutf WHERE hutf.user_id = ? AND hutf.host_id = h.id AND hutf.tag_id IN ({ph}))"
                )
                tag_filter_params = [user["id"], *tag_ids]

            def _combine_where(*parts: str) -> str:
                out = [p for p in parts if p]
                return " AND ".join(out)

            limit_sql = f" LIMIT {int(lim)}" if lim is not None else ""

            if group_id is not None:
                if _is_admin(user):
                    where_clause = _combine_where("m.group_id = ?", search_where, tag_filter_where)
                    params = [group_id, *search_params, *tag_filter_params]
                    if where_clause:
                        rows = await db.execute_fetchall(
                            f"""SELECT {sel_cols}
                           FROM hosts h {HOST_LIST_OWNER_JOIN} INNER JOIN host_group_members m ON h.id = m.host_id
                           WHERE {where_clause} ORDER BY h.name{limit_sql}""",
                            params,
                        )
                    else:
                        rows = await db.execute_fetchall(
                            f"""SELECT {sel_cols}
                           FROM hosts h {HOST_LIST_OWNER_JOIN} INNER JOIN host_group_members m ON h.id = m.host_id
                           WHERE m.group_id = ? ORDER BY h.name""",
                            (group_id,),
                        )
                else:
                    where_clause = _combine_where("m.group_id = ?", "(h.created_by = ? OR hs.id IS NOT NULL)", search_where, tag_filter_where)
                    params = [user["id"], user["id"], group_id, user["id"], *search_params, *tag_filter_params]
                    if where_clause:
                        rows = await db.execute_fetchall(
                            f"""SELECT {sel_cols}
                           FROM hosts h {HOST_LIST_OWNER_JOIN} INNER JOIN host_group_members m ON h.id = m.host_id
                           INNER JOIN host_groups hg ON hg.id = m.group_id AND hg.created_by = ?
                           LEFT JOIN host_shares hs
                             ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                           WHERE {where_clause} ORDER BY h.name{limit_sql}""",
                            params,
                        )
                    else:
                        params = [user["id"], user["id"], group_id, user["id"], *tag_filter_params]
                        rows = await db.execute_fetchall(
                            f"""SELECT {sel_cols}
                           FROM hosts h {HOST_LIST_OWNER_JOIN} INNER JOIN host_group_members m ON h.id = m.host_id
                           INNER JOIN host_groups hg ON hg.id = m.group_id AND hg.created_by = ?
                           LEFT JOIN host_shares hs
                             ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                           WHERE m.group_id = ? AND (h.created_by = ? OR hs.id IS NOT NULL){(' AND ' + tag_filter_where) if tag_filter_where else ''} ORDER BY h.name""",
                            params,
                        )
            else:
                if _is_admin(user):
                    where_clause = _combine_where(search_where, tag_filter_where)
                    params = [*search_params, *tag_filter_params]
                    if where_clause:
                        rows = await db.execute_fetchall(
                            f"""SELECT {sel_cols}
                           FROM hosts h {HOST_LIST_OWNER_JOIN} WHERE {where_clause} ORDER BY h.name{limit_sql}""",
                            params,
                        )
                    else:
                        rows = await db.execute_fetchall(
                            f"""SELECT {sel_cols}
                           FROM hosts h {HOST_LIST_OWNER_JOIN} ORDER BY h.name"""
                        )
                else:
                    where_clause = _combine_where("(h.created_by = ? OR hs.id IS NOT NULL)", search_where, tag_filter_where)
                    params = [user["id"], user["id"], *search_params, *tag_filter_params]
                    if where_clause:
                        rows = await db.execute_fetchall(
                            f"""SELECT DISTINCT {sel_cols}
                           FROM hosts h {HOST_LIST_OWNER_JOIN}
                           LEFT JOIN host_shares hs
                             ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                           WHERE {where_clause}
                           ORDER BY h.name{limit_sql}""",
                            params,
                        )
                    else:
                        params = [user["id"], user["id"], *tag_filter_params]
                        rows = await db.execute_fetchall(
                            f"""SELECT DISTINCT {sel_cols}
                           FROM hosts h {HOST_LIST_OWNER_JOIN}
                           LEFT JOIN host_shares hs
                             ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                           WHERE (h.created_by = ? OR hs.id IS NOT NULL){(' AND ' + tag_filter_where) if tag_filter_where else ''}
                           ORDER BY h.name""",
                            params,
                        )
            hosts = [normalize_host_aliases_in_dict(dict(r)) for r in rows]
            await _attach_user_tags_to_hosts(db, hosts, int(user["id"]))
            out = {"success": True, "hosts": hosts}
            if q_raw:
                out["search"] = q_raw
                if lim is not None:
                    out["limit"] = lim
            if tag_ids:
                out["tag_ids"] = tag_ids
            return json.dumps(out, ensure_ascii=False)

        if name == "search_hosts":
            db = await get_db()
            query = (arguments.get("query") or "").strip()
            if not query:
                return json.dumps({"success": False, "error": "query 必填"}, ensure_ascii=False)
            group_id = arguments.get("group_id")
            tag_ids_raw = arguments.get("tag_ids")
            regex_pattern = (arguments.get("regex") or "").strip()
            case_sensitive = bool(arguments.get("case_sensitive") or False)
            limit = min(200, max(1, int(arguments.get("limit") or 50)))

            tag_ids: list[int] = []
            if isinstance(tag_ids_raw, (list, tuple)):
                for x in tag_ids_raw:
                    try:
                        if x is not None:
                            tag_ids.append(int(x))
                    except (TypeError, ValueError):
                        continue
                tag_ids = sorted(set(tag_ids))

            sel_cols = HOST_LIST_SELECT_COLS

            like = f"%{query.lower()}%"
            search_where = """(
                LOWER(h.name) LIKE ?
                OR LOWER(h.host) LIKE ?
                OR CAST(h.port AS TEXT) LIKE ?
                OR LOWER(IFNULL(h.description,'')) LIKE ?
                OR LOWER(IFNULL(h.remark,'')) LIKE ?
                OR LOWER(IFNULL(h.aliases,'')) LIKE ?
                OR LOWER(IFNULL(h.host_type,'')) LIKE ?
                OR EXISTS (
                    SELECT 1 FROM host_user_tags hutq
                    JOIN host_tags tq ON tq.id = hutq.tag_id
                    WHERE hutq.user_id = ? AND hutq.host_id = h.id
                      AND tq.created_by = ? AND LOWER(tq.name) LIKE ?
                )
            )"""
            search_params = [like, like, like, like, like, like, like, user["id"], user["id"], like]
            if query.isdigit():
                search_where = "(" + search_where + " OR h.id = ?)"
                try:
                    search_params.append(int(query))
                except ValueError:
                    pass

            tag_filter_where = ""
            tag_filter_params: list = []
            if tag_ids:
                ph = ",".join(["?"] * len(tag_ids))
                tag_filter_where = f"EXISTS (SELECT 1 FROM host_user_tags hutf WHERE hutf.user_id = ? AND hutf.host_id = h.id AND hutf.tag_id IN ({ph}))"
                tag_filter_params = [user["id"], *tag_ids]

            def _combine_where(*parts: str) -> str:
                return " AND ".join([p for p in parts if p])

            if group_id is not None:
                if _is_admin(user):
                    where_clause = _combine_where("m.group_id = ?", search_where, tag_filter_where)
                    params = [group_id, *search_params, *tag_filter_params]
                    rows = await db.execute_fetchall(
                        f"""SELECT {sel_cols}
                           FROM hosts h {HOST_LIST_OWNER_JOIN}
                           INNER JOIN host_group_members m ON h.id = m.host_id
                           WHERE {where_clause}
                           ORDER BY h.name
                           LIMIT {limit}""",
                        params,
                    )
                else:
                    where_clause = _combine_where("m.group_id = ?", "(h.created_by = ? OR hs.id IS NOT NULL)", search_where, tag_filter_where)
                    params = [user["id"], user["id"], group_id, user["id"], *search_params, *tag_filter_params]
                    rows = await db.execute_fetchall(
                        f"""SELECT {sel_cols}
                           FROM hosts h {HOST_LIST_OWNER_JOIN}
                           INNER JOIN host_group_members m ON h.id = m.host_id
                           INNER JOIN host_groups hg ON hg.id = m.group_id AND hg.created_by = ?
                           LEFT JOIN host_shares hs
                             ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                           WHERE {where_clause}
                           ORDER BY h.name
                           LIMIT {limit}""",
                        params,
                    )
            else:
                if _is_admin(user):
                    where_clause = _combine_where(search_where, tag_filter_where)
                    params = [*search_params, *tag_filter_params]
                    rows = await db.execute_fetchall(
                        f"""SELECT {sel_cols}
                           FROM hosts h {HOST_LIST_OWNER_JOIN}
                           WHERE {where_clause}
                           ORDER BY h.name
                           LIMIT {limit}""",
                        params,
                    )
                else:
                    where_clause = _combine_where("(h.created_by = ? OR hs.id IS NOT NULL)", search_where, tag_filter_where)
                    params = [user["id"], user["id"], *search_params, *tag_filter_params]
                    rows = await db.execute_fetchall(
                        f"""SELECT DISTINCT {sel_cols}
                           FROM hosts h {HOST_LIST_OWNER_JOIN}
                           LEFT JOIN host_shares hs
                             ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                           WHERE {where_clause}
                           ORDER BY h.name
                           LIMIT {limit}""",
                        params,
                    )

            hosts = [normalize_host_aliases_in_dict(dict(r)) for r in rows]
            await _attach_user_tags_to_hosts(db, hosts, int(user["id"]))
            if regex_pattern:
                try:
                    flags = 0 if case_sensitive else re.IGNORECASE
                    reg = re.compile(regex_pattern, flags)
                except re.error as e:
                    return json.dumps({"success": False, "error": f"regex 非法: {e}"}, ensure_ascii=False)
                filtered = []
                for h in hosts:
                    blob = " ".join([
                        str(h.get("id") or ""),
                        str(h.get("name") or ""),
                        str(h.get("host") or ""),
                        str(h.get("port") or ""),
                        str(h.get("description") or ""),
                        str(h.get("remark") or ""),
                        " ".join(h.get("aliases") or []),
                        " ".join(h.get("tag_names") or []),
                    ])
                    if reg.search(blob):
                        filtered.append(h)
                hosts = filtered

            return json.dumps(
                {
                    "success": True,
                    "query": query,
                    "regex": regex_pattern or "",
                    "group_id": group_id,
                    "tag_ids": tag_ids,
                    "hosts": hosts,
                    "count": len(hosts),
                },
                ensure_ascii=False,
            )

        if name == "list_host_tags":
            db = await get_db()
            rows = await db.execute_fetchall(
                """SELECT t.id, t.name, t.color, t.created_at, t.updated_at,
                          COUNT(hut.host_id) AS host_count
                   FROM host_tags t
                   LEFT JOIN host_user_tags hut
                     ON hut.tag_id = t.id AND hut.user_id = ?
                   WHERE t.created_by = ?
                   GROUP BY t.id, t.name, t.color, t.created_at, t.updated_at
                   ORDER BY t.name COLLATE NOCASE, t.id""",
                (user["id"], user["id"]),
            )
            return json.dumps({"success": True, "tags": [dict(r) for r in rows]}, ensure_ascii=False)

        if name == "create_host_tag":
            db = await get_db()
            tag_name = (arguments.get("name") or "").strip()
            color = (arguments.get("color") or "").strip()
            if not tag_name:
                return json.dumps({"success": False, "error": "name 必填"}, ensure_ascii=False)
            if color and (not color.startswith("#")):
                color = "#" + color
            dup = await db.execute_fetchall(
                "SELECT id FROM host_tags WHERE created_by = ? AND lower(trim(name)) = lower(trim(?)) LIMIT 1",
                (user["id"], tag_name),
            )
            if dup:
                return json.dumps({"success": False, "error": "该标签名已存在"}, ensure_ascii=False)
            cur = await db.execute(
                "INSERT INTO host_tags (name, color, created_by) VALUES (?, ?, ?)",
                (tag_name, color, user["id"]),
            )
            await db.commit()
            return json.dumps({"success": True, "id": cur.lastrowid}, ensure_ascii=False)

        if name == "update_host_tag":
            db = await get_db()
            tag_id = arguments.get("tag_id")
            if tag_id is None:
                return json.dumps({"success": False, "error": "缺少 tag_id"}, ensure_ascii=False)
            rows = await db.execute_fetchall(
                "SELECT id FROM host_tags WHERE id = ? AND created_by = ?",
                (int(tag_id), user["id"]),
            )
            if not rows:
                return json.dumps({"success": False, "error": "标签不存在"}, ensure_ascii=False)
            updates, params = [], []
            if "name" in arguments and arguments.get("name") is not None:
                new_name = (arguments.get("name") or "").strip()
                if not new_name:
                    return json.dumps({"success": False, "error": "name 不能为空"}, ensure_ascii=False)
                dup = await db.execute_fetchall(
                    """SELECT id FROM host_tags
                       WHERE created_by = ? AND lower(trim(name)) = lower(trim(?)) AND id <> ?
                       LIMIT 1""",
                    (user["id"], new_name, int(tag_id)),
                )
                if dup:
                    return json.dumps({"success": False, "error": "该标签名已存在"}, ensure_ascii=False)
                updates.append("name = ?")
                params.append(new_name)
            if "color" in arguments and arguments.get("color") is not None:
                color = (arguments.get("color") or "").strip()
                if color and (not color.startswith("#")):
                    color = "#" + color
                updates.append("color = ?")
                params.append(color)
            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                params.extend([int(tag_id), user["id"]])
                await db.execute(
                    f"UPDATE host_tags SET {', '.join(updates)} WHERE id = ? AND created_by = ?",
                    params,
                )
                await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "delete_host_tag":
            db = await get_db()
            tag_id = arguments.get("tag_id")
            if tag_id is None:
                return json.dumps({"success": False, "error": "缺少 tag_id"}, ensure_ascii=False)
            await db.execute(
                "DELETE FROM host_tags WHERE id = ? AND created_by = ?",
                (int(tag_id), user["id"]),
            )
            await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "set_host_tags":
            db = await get_db()
            host_id = arguments.get("host_id")
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            host_rows = await db.execute_fetchall("SELECT id, created_by FROM hosts WHERE id = ?", (int(host_id),))
            if not host_rows:
                return json.dumps({"success": False, "error": "主机不存在"}, ensure_ascii=False)
            host_row = dict(host_rows[0])
            if not await _can_access_host_with_shares(host_row, user):
                return json.dumps({"success": False, "error": "主机不存在"}, ensure_ascii=False)
            tag_ids_raw = arguments.get("tag_ids")
            tag_ids: list[int] = []
            if isinstance(tag_ids_raw, (list, tuple)):
                for x in tag_ids_raw:
                    try:
                        if x is not None:
                            tag_ids.append(int(x))
                    except (TypeError, ValueError):
                        continue
            tag_ids = sorted(set(tag_ids))
            if tag_ids:
                ph = ",".join(["?"] * len(tag_ids))
                exist_rows = await db.execute_fetchall(
                    f"SELECT id FROM host_tags WHERE created_by = ? AND id IN ({ph})",
                    [user["id"], *tag_ids],
                )
                existing = {int(r["id"]) for r in exist_rows}
                if len(existing) != len(tag_ids):
                    return json.dumps({"success": False, "error": "存在无效标签 ID"}, ensure_ascii=False)
            await db.execute(
                "DELETE FROM host_user_tags WHERE user_id = ? AND host_id = ?",
                (user["id"], int(host_id)),
            )
            for tid in tag_ids:
                await db.execute(
                    "INSERT OR IGNORE INTO host_user_tags (user_id, host_id, tag_id) VALUES (?, ?, ?)",
                    (user["id"], int(host_id), tid),
                )
            await db.commit()
            rows = await db.execute_fetchall(
                """SELECT t.id, t.name, t.color
                   FROM host_user_tags hut
                   JOIN host_tags t ON t.id = hut.tag_id
                   WHERE hut.user_id = ? AND hut.host_id = ?
                   ORDER BY t.name COLLATE NOCASE, t.id""",
                (user["id"], int(host_id)),
            )
            return json.dumps({"success": True, "host_id": int(host_id), "tags": [dict(r) for r in rows]}, ensure_ascii=False)

        if name == "get_host_detail":
            host_id = arguments.get("host_id")
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            row = await _get_host_row(host_id)
            if not row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(row, user):
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            # 脱敏
            d = {k: v for k, v in row.items() if k not in ("password_enc",)}
            if d.get("password_enc"):
                d["password_enc"] = "***"
            d["aliases"] = parse_host_aliases_cell(d.get("aliases"))
            db = await get_db()
            members = await db.execute_fetchall("SELECT group_id FROM host_group_members WHERE host_id = ?", (host_id,))
            d["group_ids"] = [m["group_id"] for m in members]
            return json.dumps({"success": True, "host": d}, ensure_ascii=False)

        if name == "detect_host_os":
            host_id = arguments.get("host_id")
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            host_row = await _get_host_row(host_id)
            if not host_row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not _can_access_host(host_row, user):
                return json.dumps({"success": False, "error": "仅主机所有者可更新主机系统信息"}, ensure_ascii=False)
            auth = await _resolve_host_auth(await get_db(), host_row)
            if not auth or not auth.get("username"):
                return json.dumps({"success": False, "error": "主机未配置有效登录凭证"}, ensure_ascii=False)
            from services.host_detection import detect_host_env
            try:
                env = await detect_host_env(
                    host=host_row["host"],
                    port=int(host_row.get("port") or 22),
                    username=auth["username"],
                    auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"),
                    key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    timeout=15,
                )
            except Exception as e:
                return json.dumps({"success": False, "error": f"检测失败: {e}"}, ensure_ascii=False)
            host_type = env.get("host_type") or "未知"
            host_version = env.get("host_version") or "未知"
            host_shell = env.get("shell") or None
            host_package_manager = env.get("package_manager") or None
            db = await get_db()
            await db.execute(
                "UPDATE hosts SET host_type = ?, host_version = ?, host_shell = ?, host_package_manager = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (host_type, host_version, host_shell, host_package_manager, host_id),
            )
            await db.commit()
            return json.dumps({
                "success": True,
                "host_type": host_type,
                "host_version": host_version,
                "host_shell": host_shell,
                "host_package_manager": host_package_manager,
                "message": f"已检测并更新：类型={host_type}，版本={host_version}，Shell={host_shell or '-'}，包管理={host_package_manager or '-'}",
            }, ensure_ascii=False)

        if name == "probe_host_capabilities":
            host_id = arguments.get("host_id")
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            host_row = await _get_host_row(host_id)
            if not host_row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(host_row, user):
                return json.dumps({"success": False, "error": "无权操作该主机"}, ensure_ascii=False)
            refresh = bool(arguments.get("refresh", False))
            try:
                max_age_hours = max(0, int(arguments.get("max_age_hours") or 24))
            except Exception:
                max_age_hours = 24
            try:
                timeout = max(10, min(120, int(arguments.get("timeout") or 40)))
            except Exception:
                timeout = 40

            db = await get_db()
            cached_rows = await db.execute_fetchall(
                "SELECT content, updated_at FROM ai_host_prompts WHERE host_id = ? AND user_id = ?",
                (host_id, user["id"]),
            )
            existing_content = (cached_rows[0]["content"] if cached_rows else "") or ""
            existing_block = _extract_profile_block(existing_content)

            if existing_block and not refresh:
                probed_at = _profile_probed_at(existing_block)
                if probed_at is not None:
                    try:
                        age = datetime.now(timezone.utc) - probed_at
                        if age.total_seconds() < max_age_hours * 3600:
                            return json.dumps({
                                "success": True,
                                "host_id": host_id,
                                "cached": True,
                                "probed_at": probed_at.isoformat(timespec="seconds"),
                                "message": f"使用缓存画像（{int(age.total_seconds() // 60)} 分钟前采集，max_age_hours={max_age_hours}）。如需刷新请传 refresh=true。",
                                "profile_markdown": existing_block,
                            }, ensure_ascii=False)
                    except Exception:
                        pass

            auth = await _resolve_host_auth(db, host_row)
            if not auth or not auth.get("username"):
                return json.dumps({"success": False, "error": "主机未配置有效登录凭证"}, ensure_ascii=False)

            await _log_credential_usage_audit(
                actor_user_id=user["id"],
                host_row=host_row,
                auth=auth,
                operation="probe_host_capabilities",
                stage="prepare",
                extra={"refresh": refresh, "max_age_hours": max_age_hours},
            )

            try:
                data = await _probe_host_capabilities_run(
                    host=host_row["host"],
                    port=int(host_row.get("port") or 22),
                    username=auth["username"],
                    auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"),
                    key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    timeout=timeout,
                )
            except Exception as e:
                return json.dumps({"success": False, "error": f"画像探测失败：{e}"}, ensure_ascii=False)

            profile_md = _profile_markdown(data)
            merged = _merge_profile(existing_content, profile_md)[:50000]
            await db.execute(
                """INSERT INTO ai_host_prompts (host_id, user_id, content, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(host_id, user_id) DO UPDATE SET content = excluded.content, updated_at = CURRENT_TIMESTAMP""",
                (host_id, user["id"], merged),
            )
            await db.commit()

            total_tools = len(data.get("tools") or {})
            os_label = (data.get("os") or {}).get("pretty_name") or (data.get("os") or {}).get("id") or "未知"
            return json.dumps({
                "success": True,
                "host_id": host_id,
                "cached": False,
                "probed_at": data.get("probed_at"),
                "os": data.get("os"),
                "hardware": data.get("hardware"),
                "tools_by_group": data.get("tools_by_group"),
                "tools": data.get("tools"),
                "profile_markdown": profile_md,
                "prompt_content_length": len(merged),
                "message": f"已画像主机 ID={host_id}（{os_label}），探测到 {total_tools} 个 CLI 工具，画像已合并进主机级提示词（哨兵块外的用户内容保持不变）。",
            }, ensure_ascii=False)

        if name == "get_host_capabilities":
            host_id = arguments.get("host_id")
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            host_row = await _get_host_row(host_id)
            if not host_row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(host_row, user):
                return json.dumps({"success": False, "error": "无权访问该主机"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall(
                "SELECT content FROM ai_host_prompts WHERE host_id = ? AND user_id = ?",
                (host_id, user["id"]),
            )
            content = (rows[0]["content"] if rows else "") or ""
            block = _extract_profile_block(content)
            if not block:
                return json.dumps({
                    "success": True,
                    "host_id": host_id,
                    "profile_exists": False,
                    "message": "该主机尚未做过能力画像。请先调用 probe_host_capabilities(host_id=...) 采集。",
                }, ensure_ascii=False)
            # 从画像 Markdown 里反解出结构化信息；简单起见直接正则抠几个关键字段。
            probed_at = _profile_probed_at(block)
            tools: dict[str, str] = {}
            for line in block.splitlines():
                m = re.match(r"^- `([^`]+)`:\s*(.*)$", line.strip())
                if m:
                    tools[m.group(1)] = m.group(2).strip()
            return json.dumps({
                "success": True,
                "host_id": host_id,
                "profile_exists": True,
                "probed_at": probed_at.isoformat(timespec="seconds") if probed_at else None,
                "profile_markdown": block,
                "tools": tools,
                "tool_count": len(tools),
            }, ensure_ascii=False)

        if name == "delegate_to_cli_agent":
            host_id = arguments.get("host_id")
            agent = (arguments.get("agent") or "").strip().lower()
            task = (arguments.get("task") or "").strip()
            workdir = (arguments.get("workdir") or "").strip()
            model = (arguments.get("model") or "").strip() or None
            extra_args = (arguments.get("extra_args") or "").strip()
            command_template = (arguments.get("command_template") or "").strip() or None
            env = arguments.get("env") or {}
            if not isinstance(env, dict):
                env = {}
            output_format = (arguments.get("output_format") or "").strip()
            try:
                timeout = max(10, min(900, int(arguments.get("timeout") or 300)))
            except Exception:
                timeout = 300
            try:
                max_output_chars = max(2000, min(200000, int(arguments.get("max_output_chars") or 20000)))
            except Exception:
                max_output_chars = 20000
            confirmed = bool(arguments.get("confirmed", False))

            if host_id is None or not task or not agent:
                return json.dumps({
                    "success": False,
                    "error": "需要 host_id、agent 与 task",
                }, ensure_ascii=False)

            host_row = await _get_host_row(host_id)
            if not host_row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(host_row, user):
                return json.dumps({"success": False, "error": "无权操作该主机"}, ensure_ascii=False)

            # 读画像决定 agent 是否已安装
            db = await get_db()
            prompt_rows = await db.execute_fetchall(
                "SELECT content FROM ai_host_prompts WHERE host_id = ? AND user_id = ?",
                (host_id, user["id"]),
            )
            prompt_content = (prompt_rows[0]["content"] if prompt_rows else "") or ""
            profile_block = _extract_profile_block(prompt_content) or ""
            installed: dict[str, str] = {}
            for line in profile_block.splitlines():
                m = re.match(r"^- `([^`]+)`:\s*(.*)$", line.strip())
                if m:
                    installed[m.group(1)] = m.group(2).strip()

            if not profile_block:
                return json.dumps({
                    "success": False,
                    "error": "该主机尚未做能力画像，无法判断子 AI 是否已安装。请先调用 probe_host_capabilities(host_id=...)，确认目标 agent 可用后再委派。",
                    "needs_probe": True,
                }, ensure_ascii=False)

            if agent == "auto":
                picked = _pick_agent_auto(installed)
                if not picked:
                    return json.dumps({
                        "success": False,
                        "error": "画像里未发现任何已知子 AI CLI（cursor-agent/opencode/aider/claude/codex/goose/cline/llm），无法 auto 选择。请在主机上安装其中之一，或显式指定 agent。",
                        "installed_candidates": [],
                    }, ensure_ascii=False)
                agent = picked

            if agent not in _AGENT_SPECS and not command_template:
                return json.dumps({
                    "success": False,
                    "error": f"未知 agent：{agent}。支持：{', '.join(_AGENT_SPECS)} 或传 command_template 自定义。",
                }, ensure_ascii=False)

            if agent not in installed and not command_template:
                # 给主 AI 明确建议
                suggestions = [a for a in _AGENT_SPECS if a in installed]
                return json.dumps({
                    "success": False,
                    "error": f"主机画像显示未安装 {agent}。已安装的候选：{suggestions or '无'}。请选其中一个，或先在目标机安装再调用。",
                    "installed_candidates": suggestions,
                }, ensure_ascii=False)

            spec = _AGENT_SPECS.get(agent)
            modifies = bool(spec and spec.modifies_files)

            # 写操作需要用户通过 ask_user_choice 确认；task scope 下视为已被任务本体授权
            if (scope or "").strip().lower() == "task":
                confirmed = True
            if modifies and not confirmed:
                return json.dumps({
                    "success": False,
                    "error": f"{agent} 会修改主机文件，属于破坏性操作。请先通过 ask_user_choice 让用户确认任务、agent、workdir，再把 confirmed=true 重新调用。",
                    "needs_confirmation": True,
                }, ensure_ascii=False)

            auth = await _resolve_host_auth(db, host_row)
            if not auth or not auth.get("username"):
                return json.dumps({"success": False, "error": "主机未配置有效登录凭证"}, ensure_ascii=False)

            # 审计：只记 env 的 key
            await _log_credential_usage_audit(
                actor_user_id=user["id"],
                host_row=host_row,
                auth=auth,
                operation="delegate_to_cli_agent",
                stage="prepare",
                extra={
                    "agent": agent,
                    "task_preview": task[:180],
                    "workdir": workdir,
                    "timeout": timeout,
                    "env_keys": sorted(list(env.keys())) if env else [],
                    "model": model,
                },
            )

            # 流式回调：把子 AI 每行输出包成 sub_agent_line 事件推给调用方
            _delegate_on_line = None
            if stream_callback is not None:
                _host_label_for_stream = host_row.get("name") or host_row.get("host") or ""
                async def _delegate_on_line(stream: str, line: str,
                                            _hid=int(host_row["id"]), _hl=_host_label_for_stream, _ag=agent) -> None:
                    try:
                        await stream_callback({
                            "kind": "sub_agent_line",
                            "host_id": _hid, "host_label": _hl,
                            "agent": _ag,
                            "stream": stream, "line": line[:2000],
                        })
                    except Exception:
                        pass

            try:
                result = await _delegate_to_agent(
                    host=host_row["host"],
                    port=int(host_row.get("port") or 22),
                    username=auth.get("username") or "",
                    auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"),
                    key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    agent=agent,
                    task=task,
                    workdir=workdir,
                    model=model,
                    extra_args=extra_args,
                    output_format=output_format,
                    env={str(k): str(v) for k, v in (env or {}).items()},
                    command_template=command_template,
                    timeout=timeout,
                    max_output_chars=max_output_chars,
                    on_line=_delegate_on_line,
                )
            except Exception as e:
                return json.dumps({
                    "success": False,
                    "error": f"子 AI 委派失败：{type(e).__name__}: {e}",
                }, ensure_ascii=False)

            # 高风险操作自动写任务日志
            try:
                if modifies:
                    await _edgeops_auto_append_task_log(
                        host_row,
                        auth,
                        arguments.get("task_dir_name") or "",
                        phase="AI 委派",
                        action=f"delegate_to_cli_agent:{agent}",
                        result=f"exit_code={result.exit_code}, duration={result.duration_sec}s, diff_files={result.git_diff.get('files_changed', 0) if result.git_diff else 0}",
                        details=(
                            f"workdir: {workdir}\\n"
                            f"task: {task[:800]}\\n"
                            f"cmd: {result.cmd[:800]}\\n"
                            f"stderr: {(result.stderr or '').strip()[:400]}"
                        ),
                    )
            except Exception:
                pass

            # 构造返回：stdout/stderr 用截断版避免撑爆上下文
            payload = {
                "success": result.success,
                "host_id": host_id,
                "agent": agent,
                "cmd_used": result.cmd,
                "exit_code": result.exit_code,
                "duration_sec": result.duration_sec,
                "stdout_preview": result.stdout,
                "stdout_truncated": result.stdout_truncated,
                "stdout_full_length": result.stdout_full_length,
                "stderr_preview": result.stderr,
                "git_diff": result.git_diff or {},
                "error": result.error,
            }
            if result.success:
                diff = result.git_diff or {}
                msg_parts = [f"子 AI `{agent}` 在主机 ID={host_id} 上完成（耗时 {result.duration_sec}s,退出码 {result.exit_code}）。"]
                if diff:
                    fc = diff.get("files_changed", 0)
                    ins = diff.get("insertions", 0)
                    dele = diff.get("deletions", 0)
                    if fc or ins or dele:
                        msg_parts.append(f"git 改动：{fc} 文件，+{ins} / -{dele} 行。")
                    files = diff.get("files") or []
                    if files:
                        preview = ", ".join(files[:8]) + (" …" if len(files) > 8 else "")
                        msg_parts.append(f"文件：{preview}")
                payload["message"] = " ".join(msg_parts)
            else:
                payload["message"] = (
                    f"子 AI `{agent}` 执行失败（退出码 {result.exit_code}）。"
                    f"{('错误：' + result.error) if result.error else ''}"
                    f" 请检查 stderr_preview / cmd_used 后决定是否重试、换 agent 或修正参数。"
                )
            return json.dumps(payload, ensure_ascii=False)

        if name == "delegate_chain":
            default_host_id = arguments.get("host_id")
            steps_raw = arguments.get("steps") or []
            stop_on_failure = bool(arguments.get("stop_on_failure", True))
            confirmed = bool(arguments.get("confirmed", False))
            task_dir_name = (arguments.get("task_dir_name") or "").strip()

            if default_host_id is None or not isinstance(steps_raw, list) or len(steps_raw) == 0:
                return json.dumps({
                    "success": False,
                    "error": "需要 host_id（默认主机）与非空 steps 数组",
                }, ensure_ascii=False)
            if len(steps_raw) > 10:
                return json.dumps({
                    "success": False,
                    "error": f"steps 最多 10 步（当前 {len(steps_raw)}）。请拆成多条链或收敛步骤",
                }, ensure_ascii=False)

            db = await get_db()

            # 收集所有用到的 host_id（包括 default 与 step 级覆盖）
            def _step_host_id(step: dict) -> int:
                try:
                    hv = step.get("host_id")
                    return int(hv) if hv is not None else int(default_host_id)
                except Exception:
                    return int(default_host_id)

            unique_host_ids: list[int] = []
            for s in steps_raw:
                if isinstance(s, dict) and (s.get("kind") or "delegate").lower() != "sleep":
                    hid = _step_host_id(s)
                    if hid not in unique_host_ids:
                        unique_host_ids.append(hid)
            if int(default_host_id) not in unique_host_ids:
                unique_host_ids.append(int(default_host_id))

            # 每台主机：存在校验 / 访问控制 / 画像解析 / 凭证解析
            host_rows_map: dict[int, dict] = {}
            host_installed_map: dict[int, dict[str, str]] = {}
            host_has_profile: dict[int, bool] = {}
            auth_map: dict[int, dict] = {}
            host_conn_map: dict[int, _HostConnInfoCls] = {}
            for hid in unique_host_ids:
                hrow = await _get_host_row(hid)
                if not hrow:
                    return json.dumps({"success": False, "error": f"主机 ID={hid} 不存在"}, ensure_ascii=False)
                if not await _can_access_host_with_shares(hrow, user):
                    return json.dumps({"success": False, "error": f"无权操作主机 ID={hid}"}, ensure_ascii=False)
                host_rows_map[hid] = hrow
                # 画像
                prompt_rows = await db.execute_fetchall(
                    "SELECT content FROM ai_host_prompts WHERE host_id = ? AND user_id = ?",
                    (hid, user["id"]),
                )
                content = (prompt_rows[0]["content"] if prompt_rows else "") or ""
                block = _extract_profile_block(content) or ""
                host_has_profile[hid] = bool(block)
                inst: dict[str, str] = {}
                for line in block.splitlines():
                    m = re.match(r"^- `([^`]+)`:\s*(.*)$", line.strip())
                    if m:
                        inst[m.group(1)] = m.group(2).strip()
                host_installed_map[hid] = inst
                # 凭证（即使该步只是 ssh 也需要）
                a = await _resolve_host_auth(db, hrow)
                if not a or not a.get("username"):
                    return json.dumps({
                        "success": False,
                        "error": f"主机 ID={hid} 未配置有效登录凭证",
                    }, ensure_ascii=False)
                auth_map[hid] = a
                host_conn_map[hid] = _HostConnInfoCls(
                    host_id=hid,
                    host=hrow["host"],
                    port=int(hrow.get("port") or 22),
                    username=a.get("username") or "",
                    auth_type=a.get("auth_type") or "password",
                    password=a.get("password"),
                    key_path=a.get("key_path"),
                    private_key_pem=a.get("private_key_pem"),
                    installed_tools=inst,
                    label=str(hrow.get("name") or hrow.get("host") or f"host#{hid}"),
                )

            # 规范化 steps + 校验（每步按自己的 host 画像）
            steps: list[dict] = []
            has_write_delegate = False
            delegate_agents: list[str] = []
            for i, raw in enumerate(steps_raw):
                if not isinstance(raw, dict):
                    return json.dumps({
                        "success": False,
                        "error": f"第 {i + 1} 步不是对象",
                    }, ensure_ascii=False)
                kind = (raw.get("kind") or "delegate").strip().lower()
                if kind not in ("delegate", "ssh", "sleep"):
                    return json.dumps({
                        "success": False,
                        "error": f"第 {i + 1} 步 kind={kind} 非法（只支持 delegate/ssh/sleep）",
                    }, ensure_ascii=False)
                step_hid = _step_host_id(raw) if kind != "sleep" else 0
                if kind == "delegate":
                    step_installed = host_installed_map.get(step_hid, {})
                    step_has_profile = host_has_profile.get(step_hid, False)
                    agent = (raw.get("agent") or "").strip().lower()
                    if agent == "auto":
                        picked = _pick_agent_auto(step_installed)
                        if not picked:
                            return json.dumps({
                                "success": False,
                                "error": f"第 {i + 1} 步（主机 ID={step_hid}）agent=auto 但画像未发现任何已知子 AI CLI；请先 probe_host_capabilities 或显式指定 agent",
                            }, ensure_ascii=False)
                        agent = picked
                        raw = {**raw, "agent": agent}
                    if not agent:
                        return json.dumps({
                            "success": False,
                            "error": f"第 {i + 1} 步（delegate）缺少 agent",
                        }, ensure_ascii=False)
                    spec = _AGENT_SPECS.get(agent)
                    if not spec and not raw.get("command_template"):
                        return json.dumps({
                            "success": False,
                            "error": f"第 {i + 1} 步未知 agent={agent}；支持：{', '.join(_AGENT_SPECS)}（或传 command_template 自定义）",
                        }, ensure_ascii=False)
                    if agent not in step_installed and not raw.get("command_template"):
                        if not step_has_profile:
                            return json.dumps({
                                "success": False,
                                "error": f"主机 ID={step_hid} 尚未做能力画像，无法校验子 AI 是否已安装。请先对该主机 probe_host_capabilities。",
                                "needs_probe": True,
                                "needs_probe_host_id": step_hid,
                            }, ensure_ascii=False)
                        return json.dumps({
                            "success": False,
                            "error": f"第 {i + 1} 步 agent={agent} 在主机 ID={step_hid} 的画像里未发现已安装；该主机已安装候选：{[a for a in _AGENT_SPECS if a in step_installed] or '无'}",
                        }, ensure_ascii=False)
                    if not (raw.get("task") or "").strip():
                        return json.dumps({
                            "success": False,
                            "error": f"第 {i + 1} 步（delegate）缺少 task",
                        }, ensure_ascii=False)
                    delegate_agents.append(f"{agent}@{host_conn_map[step_hid].label}")
                    if spec and spec.modifies_files:
                        has_write_delegate = True
                elif kind == "ssh":
                    if not (raw.get("command") or "").strip():
                        return json.dumps({
                            "success": False,
                            "error": f"第 {i + 1} 步（ssh）缺少 command",
                        }, ensure_ascii=False)
                steps.append(raw)

            if (scope or "").strip().lower() == "task":
                confirmed = True
            if has_write_delegate and not confirmed:
                return json.dumps({
                    "success": False,
                    "error": (
                        "链中含会修改文件的子 AI 委派步骤（" + ", ".join(delegate_agents) + "），"
                        "属于破坏性操作。请先通过 ask_user_choice 让用户逐条确认整条链（agent/task/workdir/host/依赖步骤），再把 confirmed=true 重新调用。"
                    ),
                    "needs_confirmation": True,
                    "steps_preview": [
                        {
                            "index": i,
                            "kind": (s.get("kind") or "delegate"),
                            "host_id": _step_host_id(s) if (s.get("kind") or "delegate").lower() != "sleep" else None,
                            "host_label": (host_conn_map.get(_step_host_id(s)).label if (s.get("kind") or "delegate").lower() != "sleep" else ""),
                            "agent": s.get("agent") or "",
                            "task_preview": (s.get("task") or "")[:120],
                            "command_preview": (s.get("command") or "")[:120],
                            "when": s.get("when") or ("always" if i == 0 else "on_success"),
                        }
                        for i, s in enumerate(steps)
                    ],
                }, ensure_ascii=False)

            # 对每台涉及主机做一次审计
            for hid, hrow in host_rows_map.items():
                await _log_credential_usage_audit(
                    actor_user_id=user["id"],
                    host_row=hrow,
                    auth=auth_map[hid],
                    operation="delegate_chain",
                    stage="prepare",
                    extra={
                        "total_steps": len(steps),
                        "agents": delegate_agents,
                        "stop_on_failure": stop_on_failure,
                        "step_kinds": [s.get("kind") or "delegate" for s in steps],
                        "default_host_id": int(default_host_id),
                        "this_host_is_default": (hid == int(default_host_id)),
                    },
                )

            async def _chain_on_event(ev: dict) -> None:
                if stream_callback is None:
                    return
                try:
                    await stream_callback(ev)
                except Exception:
                    pass

            try:
                step_results = await _run_delegate_chain(
                    hosts=host_conn_map,
                    default_host_id=int(default_host_id),
                    steps=steps,
                    stop_on_failure=stop_on_failure,
                    on_event=(_chain_on_event if stream_callback is not None else None),
                )
            except Exception as e:
                return json.dumps({
                    "success": False,
                    "error": f"链执行失败：{type(e).__name__}: {e}",
                }, ensure_ascii=False)

            executed = [r for r in step_results if not r.skipped]
            failed_at = next((r.index for r in step_results if not r.success and not r.skipped), None)
            overall_success = all(r.success for r in step_results)

            # 写任务日志（对每台主机都写一条——便于查问这台机到底被动过什么）
            executed = [r for r in step_results if not r.skipped]
            failed_at = next((r.index for r in step_results if not r.success and not r.skipped), None)
            overall_success = all(r.success for r in step_results)
            try:
                if has_write_delegate:
                    used_host_ids = sorted({r.host_id for r in step_results if r.host_id})
                    for uhid in used_host_ids:
                        sub = [r for r in step_results if r.host_id == uhid]
                        sub_exec = [r for r in sub if not r.skipped]
                        await _edgeops_auto_append_task_log(
                            host_rows_map[uhid],
                            auth_map[uhid],
                            task_dir_name,
                            phase="AI 编排",
                            action=f"delegate_chain ({len(sub_exec)}/{len(sub)} 步在此机执行)",
                            result=("success" if all(r.success for r in sub) else "partial_failure"),
                            details=(
                                f"整链 {len(executed)}/{len(step_results)} 步执行，失败步 index={failed_at}\\n"
                                + "steps@this_host: "
                                + " → ".join(
                                    (f"{r.kind}:{r.agent or r.name}" + (" (SKIP)" if r.skipped else f" exit={r.exit_code}"))
                                    for r in sub
                                )
                            )[:1500],
                        )
            except Exception:
                pass

            # 构造 summary
            host_label_by_id = {hid: hi.label for hid, hi in host_conn_map.items()}
            distinct_hosts = sorted({r.host_id for r in step_results if r.host_id})
            summary_parts = [
                f"链共 {len(step_results)} 步，跨 {len(distinct_hosts)} 台主机，执行 {len(executed)} 步，跳过 {len(step_results) - len(executed)} 步。"
            ]
            if overall_success:
                summary_parts.append("**整体成功**。")
            else:
                summary_parts.append(f"**第 {failed_at + 1 if failed_at is not None else '?'} 步失败**。")
            total_files = sum((r.git_diff or {}).get("files_changed", 0) for r in step_results)
            total_ins = sum((r.git_diff or {}).get("insertions", 0) for r in step_results)
            total_del = sum((r.git_diff or {}).get("deletions", 0) for r in step_results)
            if total_files or total_ins or total_del:
                summary_parts.append(f"累计 git 改动：{total_files} 文件，+{total_ins} / -{total_del} 行。")
            if len(distinct_hosts) > 1:
                summary_parts.append(
                    "主机分布：" + ", ".join(f"{host_label_by_id.get(h, h)}(ID={h})" for h in distinct_hosts)
                )

            return json.dumps({
                "success": overall_success,
                "default_host_id": int(default_host_id),
                "distinct_host_ids": distinct_hosts,
                "total_steps": len(step_results),
                "executed": len(executed),
                "skipped": len(step_results) - len(executed),
                "failed_at": failed_at,
                "steps": [
                    {
                        "index": r.index,
                        "name": r.name,
                        "kind": r.kind,
                        "success": r.success,
                        "skipped": r.skipped,
                        "skip_reason": r.skip_reason,
                        "host_id": r.host_id or None,
                        "host_label": r.host_label,
                        "cmd": r.cmd,
                        "agent": r.agent,
                        "exit_code": r.exit_code,
                        "stdout_preview": r.stdout,
                        "stdout_truncated": r.stdout_truncated,
                        "stdout_full_length": r.stdout_full_length,
                        "stderr_preview": r.stderr,
                        "duration_sec": r.duration_sec,
                        "git_diff": r.git_diff or {},
                        "error": r.error,
                    }
                    for r in step_results
                ],
                "summary": " ".join(summary_parts),
            }, ensure_ascii=False)

        if name == "save_workflow_template":
            tpl_name = (arguments.get("name") or "").strip()
            payload = arguments.get("payload") or {}
            if not tpl_name:
                return json.dumps({"success": False, "error": "name 不能为空"}, ensure_ascii=False)
            if not isinstance(payload, dict) or not payload:
                return json.dumps({"success": False, "error": "payload 必须是非空 dict（delegate_chain 的参数字典）"}, ensure_ascii=False)
            # 粗校验：必须至少含 steps 数组
            if not isinstance(payload.get("steps"), list) or not payload["steps"]:
                return json.dumps({"success": False, "error": "payload.steps 必须是非空数组"}, ensure_ascii=False)
            try:
                res = await _save_workflow_template(
                    owner_user_id=int(user["id"]),
                    name=tpl_name,
                    payload=payload,
                    description=str(arguments.get("description") or ""),
                    kind=str(arguments.get("kind") or "delegate_chain"),
                    tags=str(arguments.get("tags") or ""),
                    visibility=str(arguments.get("visibility") or "private"),
                    overwrite=bool(arguments.get("overwrite", False)),
                )
            except Exception as e:
                return json.dumps({"success": False, "error": f"保存失败：{type(e).__name__}: {e}"}, ensure_ascii=False)
            if res.get("success"):
                res["declared_variables"] = _workflow_declared_vars(payload)
            return json.dumps(res, ensure_ascii=False)

        if name == "list_workflow_templates":
            query = str(arguments.get("query") or "")
            include_org = bool(arguments.get("include_org", True))
            limit = int(arguments.get("limit") or 50)
            try:
                rows = await _list_workflow_templates(
                    owner_user_id=int(user["id"]),
                    include_org=include_org,
                    query=query,
                    limit=limit,
                )
            except Exception as e:
                return json.dumps({"success": False, "error": f"查询失败：{type(e).__name__}: {e}"}, ensure_ascii=False)
            return json.dumps({
                "success": True,
                "count": len(rows),
                "templates": [
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "description": r["description"],
                        "kind": r["kind"],
                        "tags": r["tags"],
                        "visibility": r["visibility"],
                        "owner_user_id": r["owner_user_id"],
                        "owner_is_me": r["owner_user_id"] == int(user["id"]),
                        "last_run_at": r["last_run_at"],
                        "run_count": r["run_count"],
                        "updated_at": r["updated_at"],
                    }
                    for r in rows
                ],
            }, ensure_ascii=False)

        if name == "run_workflow_template":
            try:
                tid = int(arguments.get("template_id") or 0)
            except Exception:
                tid = 0
            if tid <= 0:
                return json.dumps({"success": False, "error": "template_id 无效"}, ensure_ascii=False)
            try:
                tpl = await _get_workflow_template(template_id=tid, user_id=int(user["id"]))
            except Exception as e:
                return json.dumps({"success": False, "error": f"读取模板失败：{type(e).__name__}: {e}"}, ensure_ascii=False)
            if not tpl:
                return json.dumps({"success": False, "error": f"模板 {tid} 不存在或无权访问"}, ensure_ascii=False)
            payload = tpl.get("payload_obj") or {}
            if not isinstance(payload, dict):
                return json.dumps({"success": False, "error": "模板 payload 已损坏"}, ensure_ascii=False)

            raw_overrides = arguments.get("variable_overrides") or {}
            overrides = {str(k): str(v) for k, v in raw_overrides.items()} if isinstance(raw_overrides, dict) else {}
            resolved = _apply_workflow_variables(payload, overrides)

            if arguments.get("host_id_override"):
                try:
                    resolved["host_id"] = int(arguments["host_id_override"])
                except Exception:
                    pass
            if arguments.get("stop_on_failure") is not None:
                resolved["stop_on_failure"] = bool(arguments["stop_on_failure"])

            declared = _workflow_declared_vars(payload)
            missing = [v for v in declared if v not in overrides]

            if arguments.get("dry_run"):
                return json.dumps({
                    "success": True,
                    "dry_run": True,
                    "template_id": tid,
                    "template_name": tpl.get("name"),
                    "declared_variables": declared,
                    "missing_variables": missing,
                    "resolved_payload": resolved,
                    "steps_preview": [
                        {
                            "name": s.get("name") or f"step{i + 1}",
                            "kind": s.get("kind") or "delegate",
                            "host_id": s.get("host_id"),
                            "agent": s.get("agent"),
                            "task": (s.get("task") or "")[:200],
                            "command": (s.get("command") or "")[:200],
                            "when": s.get("when"),
                        }
                        for i, s in enumerate(resolved.get("steps") or [])
                    ],
                }, ensure_ascii=False)

            if missing:
                return json.dumps({
                    "success": False,
                    "error": f"模板还声明了这些变量未提供：{missing}。请在 variable_overrides 里补齐或 dry_run=true 先预览",
                    "declared_variables": declared,
                    "missing_variables": missing,
                }, ensure_ascii=False)

            # 内联转交给 delegate_chain 引擎：直接递归调 execute_tool
            chain_args: dict[str, Any] = dict(resolved)
            if arguments.get("confirmed"):
                chain_args["confirmed"] = True
            # 把 task_dir_name 透传
            if arguments.get("task_dir_name"):
                chain_args["task_dir_name"] = str(arguments["task_dir_name"])

            chain_result_str = await execute_tool(
                "delegate_chain",
                chain_args,
                user,
                scope=scope,
                terminal_scope_id=terminal_scope_id,
                default_terminal_slot=default_terminal_slot,
                task_id=task_id,
                ui_capable=ui_capable,
                stream_callback=stream_callback,
                ui_locale=ui_locale,
            )
            # 成功才累计 run_count
            try:
                parsed = json.loads(chain_result_str) if isinstance(chain_result_str, str) else {}
                if parsed.get("success"):
                    await _mark_workflow_template_run(template_id=tid)
                parsed["_template_id"] = tid
                parsed["_template_name"] = tpl.get("name")
                return json.dumps(parsed, ensure_ascii=False)
            except Exception:
                return chain_result_str

        if name == "delegate_to_edgeops_ai":
            from services.sub_ai import run_sub_ai as _run_sub_ai, current_depth as _sub_ai_depth
            task = (arguments.get("task") or "").strip()
            sys_p = (arguments.get("system_prompt") or "").strip()
            if not task or not sys_p:
                return json.dumps({"success": False, "error": "task 与 system_prompt 都不能为空"}, ensure_ascii=False)
            allowed_tools = arguments.get("allowed_tools") or []
            if not isinstance(allowed_tools, list):
                return json.dumps({"success": False, "error": "allowed_tools 必须是字符串数组"}, ensure_ascii=False)
            try:
                max_steps_ = int(arguments.get("max_steps") or 10)
            except Exception:
                max_steps_ = 10
            try:
                max_depth_ = int(arguments.get("max_depth") or 2)
            except Exception:
                max_depth_ = 2
            try:
                timeout_sec = int(arguments.get("timeout_sec") or 120)
            except Exception:
                timeout_sec = 120
            context_hint = str(arguments.get("context_hint") or "")

            _on_step = None
            if stream_callback is not None:
                async def _on_step(ev: dict) -> None:
                    try:
                        await stream_callback(ev)
                    except Exception:
                        pass
                _on_step = _on_step

            # 外层拦截：当前已经是子 AI 环境时，再叫一次等于再深一层——depth+1 是否越上限由 run_sub_ai 判定
            cur_depth = _sub_ai_depth()
            try:
                result = await _run_sub_ai(
                    user=user,
                    scope=scope or "default",
                    task=task,
                    system_prompt=sys_p,
                    allowed_tools=[str(x) for x in allowed_tools],
                    max_steps=max_steps_,
                    max_depth=max_depth_,
                    timeout_sec=timeout_sec,
                    context_hint=context_hint,
                    browser_ui_locale=(ui_locale or "").strip() or None,
                    on_step=_on_step,
                    task_id=task_id,
                    session_id=session_id,
                )
            except Exception as e:
                return json.dumps({"success": False, "error": f"子 AI 运行异常：{type(e).__name__}: {e}"}, ensure_ascii=False)

            return json.dumps({
                "success": bool(result.get("success")),
                "depth": result.get("depth", cur_depth + 1),
                "steps_used": result.get("steps", 0),
                "duration_sec": result.get("duration_sec", 0),
                "final_text": result.get("final_text", ""),
                "truncated": result.get("truncated", False),
                "tool_calls_summary": result.get("tool_calls_summary", []),
                "error": result.get("error"),
            }, ensure_ascii=False)

        if name == "delegate_sub_tasks_batch":
            from services.sub_ai import run_sub_ai_batch as _run_sub_ai_batch

            raw_tasks = arguments.get("tasks")
            if not isinstance(raw_tasks, list) or not raw_tasks:
                return json.dumps({"success": False, "error": "tasks 必须是非空数组"}, ensure_ascii=False)
            shared_sys = str(arguments.get("shared_system_prompt") or "")
            default_allowed = arguments.get("default_allowed_tools") or []
            if default_allowed is not None and not isinstance(default_allowed, list):
                return json.dumps({"success": False, "error": "default_allowed_tools 必须是字符串数组"}, ensure_ascii=False)
            try:
                max_parallel_ = int(arguments.get("max_parallel") or 3)
            except Exception:
                max_parallel_ = 3
            try:
                max_steps_ = int(arguments.get("max_steps") or 10)
            except Exception:
                max_steps_ = 10
            try:
                max_depth_ = int(arguments.get("max_depth") or 2)
            except Exception:
                max_depth_ = 2
            try:
                timeout_sec = int(arguments.get("timeout_sec") or 120)
            except Exception:
                timeout_sec = 120

            _on_step = None
            if stream_callback is not None:
                async def _on_step(ev: dict) -> None:
                    try:
                        await stream_callback(ev)
                    except Exception:
                        pass
                _on_step = _on_step

            try:
                batch = await _run_sub_ai_batch(
                    user=user,
                    scope=scope or "default",
                    tasks=raw_tasks,
                    shared_system_prompt=shared_sys,
                    default_allowed_tools=[str(x) for x in default_allowed] if default_allowed else [],
                    max_parallel=max_parallel_,
                    max_steps=max_steps_,
                    max_depth=max_depth_,
                    timeout_sec=timeout_sec,
                    browser_ui_locale=(ui_locale or "").strip() or None,
                    on_step=_on_step,
                    task_id=task_id,
                    session_id=session_id,
                )
            except Exception as e:
                return json.dumps({"success": False, "error": f"子 AI 批量运行异常：{type(e).__name__}: {e}"}, ensure_ascii=False)

            return json.dumps(batch, ensure_ascii=False)

        if name == "ssh_execute":
            from services.ssh_background import (
                build_ssh_detach_command,
                build_ssh_poll_log_command,
                default_remote_log_path,
                parse_detach_stdout,
                parse_poll_stdout,
                sanitize_remote_log_path,
            )
            from services.session_runtime import record_ssh_detach, record_ssh_poll, resolve_log_path_for_session

            host_id = arguments.get("host_id")
            user_command = (arguments.get("command") or "").strip()
            command = user_command
            task_dir_name = (arguments.get("task_dir_name") or "").strip()
            detach = arguments.get("detach") is True
            poll_log = arguments.get("poll_log") is True
            log_path_arg = sanitize_remote_log_path(arguments.get("log_path"))
            log_path_from_session = False
            tail_lines = 40
            if arguments.get("tail_lines") is not None:
                try:
                    tail_lines = max(10, min(200, int(arguments.get("tail_lines"))))
                except (TypeError, ValueError):
                    tail_lines = 40
            if detach and poll_log:
                return json.dumps(
                    {"success": False, "error": "detach 与 poll_log 不能同时为 true"},
                    ensure_ascii=False,
                )
            if detach:
                if not command:
                    return json.dumps(
                        {"success": False, "error": "detach 模式需要 command"},
                        ensure_ascii=False,
                    )
                command = build_ssh_detach_command(command, log_path_arg or default_remote_log_path())
                timeout = max(5, min(30, int(arguments.get("timeout") or 20)))
            elif poll_log:
                if not log_path_arg and session_id:
                    _sess_db = await get_db()
                    _resolved_lp = await resolve_log_path_for_session(
                        _sess_db,
                        int(session_id),
                        host_id=int(host_id) if host_id is not None else None,
                        explicit=None,
                    )
                    log_path_arg = sanitize_remote_log_path(_resolved_lp)
                    log_path_from_session = bool(log_path_arg)
                if not log_path_arg:
                    return json.dumps(
                        {
                            "success": False,
                            "error": "poll_log 需要 log_path，或本会话该 host_id 上存在 running 的 ssh 后台任务（session_runtime_json）",
                        },
                        ensure_ascii=False,
                    )
                try:
                    command = build_ssh_poll_log_command(log_path_arg, tail_lines)
                except ValueError as ve:
                    return json.dumps({"success": False, "error": str(ve)}, ensure_ascii=False)
                timeout = max(5, min(60, int(arguments.get("timeout") or 45)))
            else:
                timeout = max(5, min(300, int(arguments.get("timeout") or 45)))
            if not host_id or not command:
                return json.dumps({"success": False, "error": "需要 host_id 和 command"}, ensure_ascii=False)
            host_row = await _get_host_row(host_id)
            if not host_row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(host_row, user):
                return json.dumps({"success": False, "error": "无权操作该主机"}, ensure_ascii=False)
            auth = await _resolve_host_auth(await get_db(), host_row)
            if not auth:
                return json.dumps({"success": False, "error": "主机认证信息无效（凭证不存在或未配置）"}, ensure_ascii=False)
            await _log_credential_usage_audit(
                actor_user_id=user["id"],
                host_row=host_row,
                auth=auth,
                operation="ssh_execute",
                stage="prepare",
                extra={"command_preview": command[:120]},
            )
            try:
                stdout, stderr, code = await run_ssh_command(
                    host=host_row["host"],
                    port=int(host_row.get("port") or 22),
                    username=auth.get("username") or "",
                    auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"),
                    key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    command=command,
                    timeout=timeout,
                )
                if _is_high_risk_command(command) and not detach and not poll_log:
                    await _edgeops_auto_append_task_log(
                        host_row,
                        auth,
                        task_dir_name,
                        phase="高风险操作",
                        action="执行命令",
                        result=f"exit_code={code}",
                        details=f"command: {command}\n\nstderr: {(stderr or '').strip()[:400]}",
                    )
                payload: dict = {
                    "success": True,
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": code,
                }
                if detach:
                    meta = parse_detach_stdout(stdout or "")
                    payload.update(meta)
                    if meta.get("detached"):
                        payload["message"] = (
                            f"已在后台启动，pid={meta.get('pid')}，日志 {meta.get('log_path')}。"
                            "请用 poll_log=true（log_path 可省略，已从会话运行态解析）轮询；job_running=false 后再读 exit_code。"
                        )
                        if session_id and meta.get("log_path"):
                            try:
                                await record_ssh_detach(
                                    await get_db(),
                                    int(session_id),
                                    host_id=int(host_id),
                                    log_path=str(meta.get("log_path")),
                                    pid=meta.get("pid"),
                                    command_preview=user_command,
                                )
                            except Exception as _rt_exc:
                                logger.warning("record_ssh_detach 失败: %s", _rt_exc)
                    else:
                        payload["success"] = code == 0
                        payload["error"] = payload.get("error") or "未能确认后台任务已启动，请检查 stdout"
                elif poll_log:
                    poll_meta = parse_poll_stdout(stdout or "", stderr or "")
                    payload.update(poll_meta)
                    payload["log_path"] = log_path_arg
                    payload["tail_lines"] = tail_lines
                    if log_path_from_session:
                        payload["log_path_resolved_from_session"] = True
                    if poll_meta.get("log_tail"):
                        payload["stdout"] = poll_meta["log_tail"]
                    if poll_meta.get("job_finished") and poll_meta.get("exit_code") is not None:
                        payload["exit_code"] = poll_meta["exit_code"]
                    if session_id and log_path_arg:
                        try:
                            await record_ssh_poll(
                                await get_db(),
                                int(session_id),
                                host_id=int(host_id),
                                log_path=log_path_arg,
                                job_running=bool(poll_meta.get("job_running")),
                                job_finished=bool(poll_meta.get("job_finished")),
                                exit_code=poll_meta.get("exit_code"),
                                log_tail_preview=poll_meta.get("log_tail") or "",
                            )
                        except Exception as _rp_exc:
                            logger.warning("record_ssh_poll 失败: %s", _rp_exc)
                return json.dumps(payload, ensure_ascii=False)
            except Exception as e:
                err_msg = str(e) or ""
                if isinstance(e, (TimeoutError, ConnectionRefusedError, OSError)):
                    err_msg = "SSH 连接超时或网络不可达，请检查主机地址、端口、网络与防火墙后重试。"
                elif isinstance(e, EOFError):
                    err_msg = "SSH 连接在握手或通信时被对方关闭，请检查网络与主机 SSH 服务后重试。"
                else:
                    if isinstance(e, paramiko.ssh_exception.SSHException):
                        if "banner" in err_msg.lower() or "protocol" in err_msg.lower():
                            err_msg = "SSH 握手失败（超时或连接被关闭），请检查端口是否为 SSH、网络是否稳定后重试。"
                        elif "auth" in err_msg.lower() or "authentic" in err_msg.lower():
                            err_msg = "SSH 认证失败，请检查主机凭证（用户名/密码或密钥）后重试。"
                        else:
                            err_msg = "SSH 连接失败：请检查主机地址、端口、凭证与网络后重试。"
                return json.dumps({"success": False, "error": err_msg}, ensure_ascii=False)

        if name == "terminal_send_and_read":
            text = arguments.get("text") or ""
            if not text:
                return json.dumps({"success": False, "error": "需要 text 参数"}, ensure_ascii=False)
            try:
                wait_seconds = max(0, min(30, int(arguments.get("wait_seconds") if arguments.get("wait_seconds") is not None else 1)))
            except (TypeError, ValueError):
                wait_seconds = 1
            send_args: dict = {"text": text}
            if arguments.get("slot") is not None:
                send_args["slot"] = arguments.get("slot")
            if arguments.get("host_id") is not None:
                send_args["host_id"] = arguments.get("host_id")
            send_raw = await execute_tool(
                "send_to_terminal",
                send_args,
                user,
                scope=scope,
                terminal_scope_id=terminal_scope_id,
                default_terminal_slot=default_terminal_slot,
                session_id=session_id,
                ui_locale=ui_locale,
                chat_mode=chat_mode,
            )
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            read_args: dict = {}
            if arguments.get("slot") is not None:
                read_args["slot"] = arguments.get("slot")
            if arguments.get("host_id") is not None:
                read_args["host_id"] = arguments.get("host_id")
            if arguments.get("max_lines") is not None:
                read_args["max_lines"] = arguments.get("max_lines")
            if arguments.get("tail_only") is not None:
                read_args["tail_only"] = arguments.get("tail_only")
            read_raw = await execute_tool(
                "get_terminal_buffer",
                read_args,
                user,
                scope=scope,
                terminal_scope_id=terminal_scope_id,
                default_terminal_slot=default_terminal_slot,
                session_id=session_id,
                ui_locale=ui_locale,
                chat_mode=chat_mode,
            )
            try:
                send_obj = json.loads(send_raw)
                read_obj = json.loads(read_raw)
            except Exception:
                return json.dumps(
                    {"success": False, "error": "terminal_send_and_read 解析子步骤结果失败"},
                    ensure_ascii=False,
                )
            ok = bool(send_obj.get("success")) and bool(read_obj.get("success", True))
            out = {
                "success": ok,
                "wait_seconds": wait_seconds,
                "send": send_obj,
                "read": read_obj,
                "message": "已发送并读取终端缓冲" if ok else "发送或读取未完全成功",
            }
            if read_obj.get("next_poll_in_seconds"):
                out["next_poll_in_seconds"] = read_obj.get("next_poll_in_seconds")
            return json.dumps(out, ensure_ascii=False)

        if name == "send_to_terminal":
            text = arguments.get("text") or ""
            if not text:
                return json.dumps({"success": False, "error": "需要 text 参数"}, ensure_ascii=False)
            slot = arguments.get("slot")
            if (scope or "").strip().lower() == "local":
                from api import local_host

                if slot is not None:
                    try:
                        slot = int(slot)
                    except (TypeError, ValueError):
                        slot = None
                slot, slot_err = resolve_local_slot(slot)
                if slot_err:
                    return json.dumps({"success": False, "error": slot_err, "terminal_scope_id": terminal_scope_id}, ensure_ascii=False)

                st = local_host.get_local_terminal_session_state(user["id"], slot, terminal_scope_id)
                guard = _terminal_send_guard_message(st, text)
                if guard:
                    return json.dumps({
                        "success": False,
                        "error": guard,
                        "slot": slot,
                        "terminal_scope_id": terminal_scope_id,
                        "status": _terminal_status_payload(st),
                    }, ensure_ascii=False)

                ok = await local_host.send_to_local_terminal(user["id"], slot, text, terminal_scope_id)
                if not ok:
                    await local_host.wait_for_local_terminal_ready(user["id"], slot, terminal_scope_id)
                    ok = await local_host.send_to_local_terminal(user["id"], slot, text, terminal_scope_id)
                if not ok:
                    return json.dumps({"success": False, "error": "本机控制台未就绪或已关闭，请先在本机管理页打开控制台（已等待连接就绪）"}, ensure_ascii=False)
                st_after = local_host.get_local_terminal_session_state(user["id"], slot, terminal_scope_id)
                payload = {
                    "success": True,
                    "message": "已发送到本机控制台",
                    "slot": slot,
                    "terminal_scope_id": terminal_scope_id,
                    "status": _terminal_status_payload(st_after),
                    "ui_action": {"action": "switch_console", "slot": slot, "scope": "local"},
                }
                _attach_terminal_send_advisory(payload, st_after)
                return json.dumps(payload, ensure_ascii=False)
            slot, slot_err = resolve_ai_slot(slot, arguments.get("host_id"))
            if slot_err:
                return json.dumps(attach_terminals_snapshot({"success": False, "error": slot_err}), ensure_ascii=False)

            st = get_terminal_session_state(user["id"], slot, terminal_scope_id)
            if not st.get("connected"):
                await wait_for_terminal_session_ready(user["id"], slot, terminal_scope_id)
                st = get_terminal_session_state(user["id"], slot, terminal_scope_id)
            guard = _terminal_send_guard_message(st, text)
            if guard:
                return json.dumps(
                    attach_terminals_snapshot(attach_terminal_host_fields({
                        "success": False,
                        "error": guard,
                        "slot": slot,
                        "status": _terminal_status_payload(st),
                    }, slot)),
                    ensure_ascii=False,
                )

            ok = send_to_user_terminal(user["id"], text, slot, scope_id=terminal_scope_id)
            if not ok:
                await wait_for_terminal_session_ready(user["id"], slot, terminal_scope_id)
                ok = send_to_user_terminal(user["id"], text, slot, scope_id=terminal_scope_id)
            if not ok:
                return json.dumps(
                    attach_terminals_snapshot({
                        "success": False,
                        "error": "该 AI 控制台未连接或已关闭（已等待约 12 秒仍未就绪）。请 list_terminals 查看 connected 状态，或 connect_terminal 后重试。",
                        "slot": slot,
                    }),
                    ensure_ascii=False,
                )
            st_after = get_terminal_session_state(user["id"], slot, terminal_scope_id)
            payload = attach_terminals_snapshot(attach_terminal_host_fields({
                "success": True,
                "message": "已发送到 AI 控制台",
                "slot": slot,
                "status": _terminal_status_payload(st_after),
                "ui_action": {"action": "switch_console", "slot": slot, "scope": "ai"},
            }, slot))
            _attach_terminal_send_advisory(payload, st_after)
            return json.dumps(payload, ensure_ascii=False)

        if name == "get_terminal_status":
            slot = arguments.get("slot")
            include_last = 0
            if arguments.get("include_last_lines") is not None:
                try:
                    include_last = max(0, min(20, int(arguments.get("include_last_lines"))))
                except (TypeError, ValueError):
                    include_last = 0
            if (scope or "").strip().lower() == "local":
                from api import local_host

                if slot is not None:
                    try:
                        slot = int(slot)
                        slot = max(0, min(slot, 31))
                    except (TypeError, ValueError):
                        slot = None
                slot, slot_err = resolve_local_slot(slot)
                if slot_err:
                    return json.dumps({"success": False, "error": slot_err, "terminal_scope_id": terminal_scope_id}, ensure_ascii=False)
                st = local_host.get_local_terminal_session_state(user["id"], slot, terminal_scope_id)
                if st.get("pending") or (st.get("exists") and not st.get("connected")):
                    await local_host.wait_for_local_terminal_ready(user["id"], slot, terminal_scope_id)
                    st = local_host.get_local_terminal_session_state(user["id"], slot, terminal_scope_id)
                out = {
                    "success": True,
                    "slot": slot,
                    "terminal_scope_id": terminal_scope_id,
                    "scope": "local",
                    **_terminal_status_payload(st),
                }
                _attach_false_busy_hint(out, st)
            else:
                slot, slot_err = resolve_ai_slot(slot, arguments.get("host_id"))
                if slot_err:
                    return json.dumps(attach_terminals_snapshot({"success": False, "error": slot_err}), ensure_ascii=False)
                st = get_terminal_session_state(user["id"], slot, terminal_scope_id)
                if st.get("pending") or (st.get("exists") and not st.get("connected")):
                    await wait_for_terminal_session_ready(user["id"], slot, terminal_scope_id)
                    st = get_terminal_session_state(user["id"], slot, terminal_scope_id)
                out = attach_terminals_snapshot(attach_terminal_host_fields({
                    "success": True,
                    "slot": slot,
                    **_terminal_status_payload(st),
                }, slot))
                _attach_false_busy_hint(out, st)
            if include_last > 0 and st.get("can_read_buffer"):
                if (scope or "").strip().lower() == "local":
                    buf, _ = local_host.get_local_terminal_buffer(user["id"], slot, terminal_scope_id)
                else:
                    buf, _ = get_terminal_buffer_for_user(user["id"], slot, scope_id=terminal_scope_id)
                lines = (buf or "").splitlines()
                out["buffer_tail_lines"] = lines[-include_last:] if lines else []
            return json.dumps(out, ensure_ascii=False)

        if name == "connect_terminal":
            host_id = arguments.get("host_id")
            if host_id is None:
                return json.dumps({"success": False, "error": "需要 host_id 参数"}, ensure_ascii=False)
            row = await _get_host_row(host_id)
            if not row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(row, user):
                return json.dumps({"success": False, "error": "无权操作该主机"}, ensure_ascii=False)
            existing = find_preferred_ai_terminal_for_host(
                user["id"], int(host_id), terminal_scope_id, prefer_idle=True
            )
            if existing:
                slot_id = int(existing["slot"])
                idle = existing.get("buffer_idle")
                idle_txt = "空闲" if idle else "可能仍有任务占用"
                connected = bool(existing.get("connected"))
                if not connected:
                    connected = await wait_for_terminal_session_ready(
                        user["id"], slot_id, terminal_scope_id
                    )
                return json.dumps(attach_terminals_snapshot({
                    "success": True,
                    "reused": True,
                    "message": (
                        f"主机 {host_id} 已有 AI 控制台 slot={slot_id}（tab={existing.get('tab_label')}，"
                        f"connected={connected}，{idle_txt}），已切到该 slot，未新建。"
                        f"若需并行第二个 session 请 create_console(host_id)。"
                    ),
                    "slot": slot_id,
                    "host_id": int(host_id),
                    "host_name": (row.get("name") or "").strip(),
                    "host_ip": (row.get("host") or "").strip(),
                    "host_port": int(row.get("port") or 22),
                    "connected": connected,
                    "buffer_idle": idle,
                    "ui_action": {"action": "switch_console", "slot": slot_id, "scope": "ai"},
                }), ensure_ascii=False)
            slot = add_pending_console_creation(
                user["id"], int(host_id), "ai", scope_id=terminal_scope_id
            )
            tab_label = format_terminal_tab_label({
                "host_id": int(host_id),
                "host_name": (row.get("name") or "").strip(),
                "host_ip": (row.get("host") or "").strip(),
                "slot": slot,
                "created_by": "ai",
            })
            ready = await wait_for_terminal_session_ready(
                user["id"], slot, terminal_scope_id
            )
            ready_msg = "已连接就绪" if ready else "前端仍在连接中（已等待约 12 秒，可 list_terminals 再查）"
            return json.dumps(attach_terminals_snapshot({
                "success": True,
                "first_connect": True,
                "slot": slot,
                "connected": ready,
                "tab_label": tab_label,
                "message": (
                    f"已为 host_id={host_id} 连接 AI 控制台 slot={slot}（tab={tab_label}），{ready_msg}。"
                    f"后续 send_to_terminal / get_terminal_buffer 请使用 slot={slot}。"
                    f"若 get_terminal_buffer 仍无输出，请带 next_poll_in_seconds 重试，勿立刻 create_console。"
                    f"若需并行第二个 session 请 create_console(host_id)。"
                ),
                "host_id": int(host_id),
                "host_name": (row.get("name") or "").strip(),
                "host_ip": (row.get("host") or "").strip(),
                "host_port": int(row.get("port") or 22),
                "ui_action": {
                    "action": "connect_terminal",
                    "host_id": int(host_id),
                    "slot": slot,
                    "created_by": "ai",
                },
            }), ensure_ascii=False)

        if name == "list_terminals":
            if (scope or "").strip().lower() == "local":
                from api import local_host
                items = local_host.get_local_terminals_for_user(user["id"], terminal_scope_id)
                return json.dumps({
                    "success": True,
                    "terminal_scope_id": terminal_scope_id,
                    "terminals": items,
                    "note": "仅列出当前页面 terminal_scope_id 下的本机控制台",
                }, ensure_ascii=False)
            snap = terminals_snapshot_for_ai(user["id"], terminal_scope_id, default_terminal_slot)
            return json.dumps({
                "success": True,
                "terminal_scope_id": snap["scope_id"],
                "terminals": snap["ai_terminals"],
                "user_terminals_readonly": snap["user_terminals"],
                "preferred_slot": snap.get("preferred_slot"),
                "note": "terminals 为 AI 可操作项；user_terminals_readonly 仅供对照，不可 send_to_terminal",
            }, ensure_ascii=False)

        if name == "create_console":
            host_id = arguments.get("host_id")
            if host_id is None:
                return json.dumps({"success": False, "error": "需要 host_id 参数"}, ensure_ascii=False)
            row = await _get_host_row(host_id)
            if not row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(row, user):
                return json.dumps({"success": False, "error": "无权操作该主机"}, ensure_ascii=False)
            slot = add_pending_console_creation(
                user["id"], int(host_id), "ai", scope_id=terminal_scope_id
            )
            tab_label = format_terminal_tab_label({
                "host_id": int(host_id),
                "host_name": (row.get("name") or "").strip(),
                "host_ip": (row.get("host") or "").strip(),
                "slot": slot,
                "created_by": "ai",
            })
            ready = await wait_for_terminal_session_ready(
                user["id"], slot, terminal_scope_id
            )
            ready_msg = "已连接就绪" if ready else "前端仍在连接中（已等待约 12 秒，可 list_terminals 再查）"
            return json.dumps(attach_terminals_snapshot({
                "success": True,
                "created_new": True,
                "slot": slot,
                "connected": ready,
                "tab_label": tab_label,
                "message": (
                    f"已为 host_id={host_id} 新建 AI 控制台 slot={slot}（tab={tab_label}），{ready_msg}。"
                    f"后续 send_to_terminal / get_terminal_buffer 请使用 slot={slot}。"
                ),
                "host_id": int(host_id),
                "host_name": (row.get("name") or "").strip(),
                "host_ip": (row.get("host") or "").strip(),
                "host_port": int(row.get("port") or 22),
                "ui_action": {
                    "action": "create_console",
                    "host_id": int(host_id),
                    "slot": slot,
                    "created_by": "ai",
                },
            }), ensure_ascii=False)

        if name == "close_console":
            slot = arguments.get("slot")
            if slot is None:
                return json.dumps({"success": False, "error": "需要 slot 参数"}, ensure_ascii=False)
            slot, slot_err = resolve_ai_slot(slot)
            if slot_err:
                return json.dumps({"success": False, "error": slot_err}, ensure_ascii=False)
            ok, msg = await terminal_close_console(user["id"], slot, scope_id=terminal_scope_id)
            if not ok:
                return json.dumps({"success": False, "error": msg}, ensure_ascii=False)
            return json.dumps({"success": True, "message": msg}, ensure_ascii=False)

        if name == "get_host_knowledge":
            host_id = arguments.get("host_id")
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            row = await _get_host_row(host_id)
            if not row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(row, user):
                return json.dumps({"success": False, "error": "无权访问该主机"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall(
                "SELECT content, updated_at FROM ai_host_knowledge WHERE host_id = ? AND user_id = ?",
                (host_id, user["id"]),
            )
            if not rows:
                return json.dumps({"success": True, "content": "", "updated_at": None}, ensure_ascii=False)
            r = rows[0]
            return json.dumps({"success": True, "content": (r["content"] or ""), "updated_at": r["updated_at"]}, ensure_ascii=False)

        if name == "update_host_knowledge":
            host_id = arguments.get("host_id")
            content = arguments.get("content") or ""
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            row = await _get_host_row(host_id)
            if not row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(row, user):
                return json.dumps({"success": False, "error": "无权操作该主机"}, ensure_ascii=False)
            db = await get_db()
            await db.execute(
                """INSERT INTO ai_host_knowledge (host_id, user_id, content, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(host_id, user_id) DO UPDATE SET content = excluded.content, updated_at = CURRENT_TIMESTAMP""",
                (host_id, user["id"], content),
            )
            await db.commit()
            return json.dumps({"success": True, "message": "已更新该主机的 AI 知识"}, ensure_ascii=False)

        if name == "append_host_knowledge":
            host_id = arguments.get("host_id")
            text = arguments.get("text") or ""
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            row = await _get_host_row(host_id)
            if not row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(row, user):
                return json.dumps({"success": False, "error": "无权操作该主机"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall(
                "SELECT content FROM ai_host_knowledge WHERE host_id = ? AND user_id = ?",
                (host_id, user["id"]),
            )
            existing = (rows[0]["content"] or "").strip() if rows else ""
            new_content = (existing + "\n" + text).strip() if existing else text
            await db.execute(
                """INSERT INTO ai_host_knowledge (host_id, user_id, content, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(host_id, user_id) DO UPDATE SET content = excluded.content, updated_at = CURRENT_TIMESTAMP""",
                (host_id, user["id"], new_content),
            )
            await db.commit()
            return json.dumps({"success": True, "message": "已追加到该主机的 AI 知识"}, ensure_ascii=False)

        if name == "list_service_credentials":
            from services.credential_vault import search_credentials_for_user

            result = await search_credentials_for_user(
                user["id"],
                service=arguments.get("service"),
                address=arguments.get("address"),
                port=int(arguments["port"]) if arguments.get("port") is not None else None,
                service_username=arguments.get("service_username"),
                host_id=int(arguments["host_id"]) if arguments.get("host_id") is not None else None,
                keyword=arguments.get("keyword"),
                command_hint=arguments.get("command_hint"),
                sort_by=arguments.get("sort_by"),
                sort_order=arguments.get("sort_order"),
                limit=int(arguments["limit"]) if arguments.get("limit") is not None else None,
            )
            return json.dumps({"success": True, **result}, ensure_ascii=False)

        if name == "add_service_credential":
            from services.credential_vault import add_credential

            service = arguments.get("service")
            password = arguments.get("password")
            if not service:
                return json.dumps({"success": False, "error": "需要 service"}, ensure_ascii=False)
            if not password and not arguments.get("linked_host_id") and not arguments.get("linked_credential_id"):
                return json.dumps(
                    {"success": False, "error": "需要 password，或设置 linked_host_id / linked_credential_id"},
                    ensure_ascii=False,
                )
            try:
                linked_hid = (
                    int(arguments["linked_host_id"]) if arguments.get("linked_host_id") else None
                )
                bind_hid = (
                    int(arguments["host_id"]) if arguments.get("host_id") is not None else None
                )
                # 本机 sudo：linked_host_id 同时写入 host_id，便于后续按主机过滤
                if bind_hid is None and linked_hid is not None and str(service).lower() == "sudo":
                    bind_hid = linked_hid
                item = await add_credential(
                    user,
                    service=str(service),
                    password=str(password) if password is not None else None,
                    address=str(arguments.get("address") or ""),
                    port=int(arguments["port"]) if arguments.get("port") is not None else None,
                    service_username=str(arguments.get("service_username") or ""),
                    label=str(arguments.get("label") or ""),
                    notes=str(arguments.get("notes") or ""),
                    linked_host_id=linked_hid,
                    linked_credential_id=int(arguments["linked_credential_id"]) if arguments.get("linked_credential_id") else None,
                    host_id=bind_hid,
                )
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            return json.dumps(
                {"success": True, "message": "凭证已保存（密码不可查询）", "credential": item},
                ensure_ascii=False,
            )

        if name == "update_service_credential":
            from services.credential_vault import update_credential

            cid = arguments.get("credential_id")
            if cid is None:
                return json.dumps({"success": False, "error": "缺少 credential_id"}, ensure_ascii=False)
            try:
                item = await update_credential(
                    user,
                    int(cid),
                    service=arguments.get("service"),
                    password=arguments.get("password"),
                    address=arguments.get("address"),
                    port=int(arguments["port"]) if arguments.get("port") is not None else None,
                    service_username=arguments.get("service_username"),
                    label=arguments.get("label"),
                    notes=arguments.get("notes"),
                    linked_host_id=int(arguments["linked_host_id"]) if arguments.get("linked_host_id") is not None else None,
                    linked_credential_id=int(arguments["linked_credential_id"]) if arguments.get("linked_credential_id") is not None else None,
                )
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            return json.dumps({"success": True, "message": "凭证已更新", "credential": item}, ensure_ascii=False)

        if name == "delete_service_credential":
            from services.credential_vault import delete_credential

            cid = arguments.get("credential_id")
            if cid is None:
                return json.dumps({"success": False, "error": "缺少 credential_id"}, ensure_ascii=False)
            try:
                await delete_credential(user, int(cid))
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            return json.dumps({"success": True, "message": "凭证已删除"}, ensure_ascii=False)

        if name == "send_service_password":
            from services.credential_vault import perform_service_password_injection

            host_id = arguments.get("host_id")
            target = (arguments.get("target") or "terminal").strip().lower()
            use_host_login = bool(arguments.get("use_host_login"))
            if target == "terminal" and host_id is None:
                return json.dumps({"success": False, "error": "target=terminal 时需要 host_id（控制台所在主机）"}, ensure_ascii=False)
            if use_host_login and host_id is None:
                return json.dumps(
                    {"success": False, "error": "use_host_login=true 时需要 host_id"},
                    ensure_ascii=False,
                )
            require_prompt = arguments.get("require_password_prompt")
            if require_prompt is None:
                require_prompt = True  # 默认必须检测到密码提示，避免 sudo 免密时误注入
            else:
                require_prompt = bool(require_prompt)

            if host_id is not None:
                row_h = await _get_host_row(host_id)
                if not row_h:
                    return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
                if not await _can_access_host_with_shares(row_h, user):
                    return json.dumps({"success": False, "error": "无权操作该主机"}, ensure_ascii=False)

            if arguments.get("credential_id") is None and not use_host_login:
                return json.dumps(
                    {
                        "success": False,
                        "error": (
                            "缺少 credential_id。本机 sudo/su 请传 use_host_login=true 与 host_id；"
                            "或先 list_service_credentials(service=sudo, host_id=…) 再注入。"
                        ),
                    },
                    ensure_ascii=False,
                )

            channel_id = arguments.get("channel_id")
            if target == "ssh_channel":
                if channel_id is None:
                    return json.dumps(
                        {"success": False, "error": "target=ssh_channel 时需要 channel_id"},
                        ensure_ascii=False,
                    )
                db = await get_db()
                await reconcile_channel_if_stale(db, user, int(channel_id))
                ch_rows = await db.execute_fetchall(
                    "SELECT id FROM ssh_channels WHERE id = ? AND user_id = ? AND status = 'open'",
                    (int(channel_id), user["id"]),
                )
                if not ch_rows:
                    return json.dumps(
                        {"success": False, "error": "SSH 通道不存在或已关闭"},
                        ensure_ascii=False,
                    )

            result = await perform_service_password_injection(
                user,
                credential_id=int(arguments["credential_id"]) if arguments.get("credential_id") is not None else None,
                target=target,
                host_id=int(host_id) if host_id is not None else None,
                slot=arguments.get("slot"),
                channel_id=int(channel_id) if channel_id is not None else None,
                terminal_scope_id=terminal_scope_id,
                require_password_prompt=require_prompt,
                use_host_login=use_host_login,
            )
            return json.dumps(result, ensure_ascii=False)

        if name == "get_host_prompt":
            host_id = arguments.get("host_id")
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            row = await _get_host_row(host_id)
            if not row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(row, user):
                return json.dumps({"success": False, "error": "无权访问该主机"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall(
                "SELECT content, updated_at FROM ai_host_prompts WHERE host_id = ? AND user_id = ?",
                (host_id, user["id"]),
            )
            if not rows:
                return json.dumps({"success": True, "host_id": host_id, "content": "", "updated_at": None}, ensure_ascii=False)
            r = rows[0]
            return json.dumps({"success": True, "host_id": host_id, "content": (r["content"] or ""), "updated_at": r["updated_at"]}, ensure_ascii=False)

        if name == "update_host_prompt":
            host_id = arguments.get("host_id")
            content = (arguments.get("content") or "")[:50000]
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            row = await _get_host_row(host_id)
            if not row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(row, user):
                return json.dumps({"success": False, "error": "无权操作该主机"}, ensure_ascii=False)
            db = await get_db()
            await db.execute(
                """INSERT INTO ai_host_prompts (host_id, user_id, content, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(host_id, user_id) DO UPDATE SET content = excluded.content, updated_at = CURRENT_TIMESTAMP""",
                (host_id, user["id"], content),
            )
            await db.commit()
            return json.dumps({"success": True, "message": "已更新该主机的主机级提示词", "host_id": host_id, "content_length": len(content)}, ensure_ascii=False)

        if name == "append_host_prompt":
            host_id = arguments.get("host_id")
            text = arguments.get("text") or ""
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            row = await _get_host_row(host_id)
            if not row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(row, user):
                return json.dumps({"success": False, "error": "无权操作该主机"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall(
                "SELECT content FROM ai_host_prompts WHERE host_id = ? AND user_id = ?",
                (host_id, user["id"]),
            )
            existing = (rows[0]["content"] or "").strip() if rows else ""
            new_content = ((existing + "\n\n" + text).strip() if existing else text)[:50000]
            await db.execute(
                """INSERT INTO ai_host_prompts (host_id, user_id, content, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(host_id, user_id) DO UPDATE SET content = excluded.content, updated_at = CURRENT_TIMESTAMP""",
                (host_id, user["id"], new_content),
            )
            await db.commit()
            return json.dumps({"success": True, "message": "已追加到该主机的主机级提示词", "host_id": host_id, "content_length": len(new_content)}, ensure_ascii=False)

        if name == "search_hosts_by_prompt":
            from services.host_prompt_search import search_hosts_by_prompt as _search_hosts_by_prompt

            db = await get_db()
            query = (arguments.get("query") or "").strip()
            if not query:
                return json.dumps({"success": False, "error": "query 必填"}, ensure_ascii=False)
            group_id = arguments.get("group_id")
            tag_ids_raw = arguments.get("tag_ids")
            tag_ids: list[int] = []
            if isinstance(tag_ids_raw, (list, tuple)):
                for x in tag_ids_raw:
                    try:
                        if x is not None:
                            tag_ids.append(int(x))
                    except (TypeError, ValueError):
                        continue
            try:
                result = await _search_hosts_by_prompt(
                    db,
                    user,
                    query=query,
                    group_id=int(group_id) if group_id is not None else None,
                    tag_ids=tag_ids,
                    regex=(arguments.get("regex") or "").strip(),
                    case_sensitive=bool(arguments.get("case_sensitive") or False),
                    limit=min(100, max(1, int(arguments.get("limit") or 30))),
                    snippet_chars=min(600, max(50, int(arguments.get("snippet_chars") or 200))),
                )
                return json.dumps(result, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "edgeops_init_workspace":
            host_id = arguments.get("host_id")
            task_title = (arguments.get("task_title") or "").strip()
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            host_row, auth, err = await _resolve_host_for_ai_ops(host_id, user)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            policy = _edgeops_storage_policy(host_row)
            if policy.get("constrained"):
                return json.dumps(
                    {
                        "success": True,
                        "skipped": True,
                        "constrained_mode": True,
                        "mode": policy.get("mode"),
                        "message": "该主机为设备型/专用系统，默认不初始化 .edgeops 落盘目录，建议优先使用主机知识库。",
                        "reason": policy.get("reason") or "",
                        "recommended_tools": ["get_host_knowledge", "update_host_knowledge", "append_host_knowledge"],
                    },
                    ensure_ascii=False,
                )
            base_dir, err = await _edgeops_home_dir(host_row, auth)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            init_cmd = (
                f'mkdir -p "{base_dir}/scripts" "{base_dir}/tasks" "{base_dir}/info" "{base_dir}/rules" '
                f'&& [ -f "{base_dir}/scripts/index.md" ] || printf "# Scripts Index\\n\\n| 脚本 | 简述 |\\n|---|---|\\n" > "{base_dir}/scripts/index.md" '
                f'&& [ -f "{base_dir}/scripts/README.md" ] || printf "# scripts\\n\\n保存可复用脚本与同名说明文档。\\n" > "{base_dir}/scripts/README.md" '
                f'&& [ -f "{base_dir}/tasks/index.md" ] || printf "# Tasks Index\\n\\n| 任务目录 | 标题 | 创建时间 |\\n|---|---|---|\\n" > "{base_dir}/tasks/index.md" '
                f'&& [ -f "{base_dir}/tasks/README.md" ] || printf "# tasks\\n\\n按时间戳目录记录任务过程。\\n" > "{base_dir}/tasks/README.md" '
                f'&& [ -f "{base_dir}/info/README.md" ] || printf "# info\\n\\n记录系统资源、运行环境、目录约定与注意事项。\\n" > "{base_dir}/info/README.md" '
                f'&& [ -f "{base_dir}/rules/README.md" ] || printf "# rules\\n\\n记录用户定义的操作规则与禁忌。\\n" > "{base_dir}/rules/README.md"'
            )
            stdout, stderr, code = await run_ssh_command(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                command=init_cmd,
                timeout=45,
            )
            if code != 0:
                return json.dumps({"success": False, "error": stderr or stdout or "初始化 .edgeops 目录失败"}, ensure_ascii=False)
            sys_info_cmd = (
                'set +e; '
                'echo "# 主机系统概览"; '
                'echo ""; '
                'echo "- 采集时间: $(date \'+%Y-%m-%d %H:%M:%S %z\')"; '
                'echo "- 当前用户: $(id -un 2>/dev/null || whoami)"; '
                'echo "- 主机名: $(hostname 2>/dev/null)"; '
                'echo "- 工作目录: $(pwd)"; '
                'echo ""; '
                'echo "## 系统信息"; '
                'uname -a 2>/dev/null || true; '
                'echo ""; '
                'echo "## 运行环境"; '
                'echo "- python: $(python3 --version 2>/dev/null || python --version 2>/dev/null || echo N/A)"; '
                'echo "- java: $(java -version 2>&1 | head -n 1 || echo N/A)"; '
                'echo "- node: $(node -v 2>/dev/null || echo N/A)"; '
                'echo "- docker: $(docker --version 2>/dev/null || echo N/A)"; '
                'echo ""; '
                'echo "## 常见目录建议"; '
                'echo "- 应用目录: （待补充）"; '
                'echo "- 数据目录: （待补充）"; '
                'echo "- 备份目录: （待补充）"; '
                'echo ""; '
                'echo "## 注意事项"; '
                'echo "- 请结合 ~/.edgeops/rules 下规则执行变更。"; '
                'echo "- 变更前先备份，变更后再验证。"; '
                'echo "- 生产环境优先使用最小化变更。"; '
                'true'
            )
            info_stdout, _, info_code = await run_ssh_command(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                command=sys_info_cmd,
                timeout=45,
            )
            if info_code == 0 and (info_stdout or "").strip():
                _ = await sftp_put_content(
                    host=host_row["host"],
                    port=int(host_row.get("port") or 22),
                    username=auth.get("username") or "",
                    auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"),
                    key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    remote_path=f"{base_dir}/info/system_overview.md",
                    content=(info_stdout.strip() + "\n").encode("utf-8"),
                    timeout=30,
                )
            out = {"success": True, "base_dir": base_dir}
            if task_title:
                task_stamp = datetime.now().strftime("%Y%m%d%H%M%S")
                task_dir = f"{base_dir}/tasks/{task_stamp}"
                mk_stdout, mk_stderr, mk_code = await run_ssh_command(
                    host=host_row["host"],
                    port=int(host_row.get("port") or 22),
                    username=auth.get("username") or "",
                    auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"),
                    key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    command=f'mkdir -p "{task_dir}"',
                    timeout=20,
                )
                if mk_code != 0:
                    return json.dumps({"success": False, "error": mk_stderr or mk_stdout or "创建任务目录失败"}, ensure_ascii=False)
                task_doc = (
                    "# 任务记录\n\n"
                    f"- 标题: {task_title}\n"
                    f"- 创建时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"- 执行用户ID: {user.get('id')}\n"
                    "- 说明: 记录本次用户与 AI 的运维过程、关键命令、结论与后续动作。\n\n"
                    "## 过程记录\n\n"
                    "- 待补充\n"
                )
                put_err = await sftp_put_content(
                    host=host_row["host"],
                    port=int(host_row.get("port") or 22),
                    username=auth.get("username") or "",
                    auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"),
                    key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    remote_path=f"{task_dir}/task.md",
                    content=task_doc.encode("utf-8"),
                    timeout=30,
                )
                if put_err:
                    return json.dumps({"success": False, "error": "任务文档写入失败: " + put_err}, ensure_ascii=False)
                idx_path = f"{base_dir}/tasks/index.md"
                idx_stdout, _, _ = await run_ssh_command(
                    host=host_row["host"],
                    port=int(host_row.get("port") or 22),
                    username=auth.get("username") or "",
                    auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"),
                    key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    command=f'cat "{idx_path}" 2>/dev/null || true',
                    timeout=20,
                )
                idx_content = (idx_stdout or "").strip()
                if "| 任务目录 | 标题 | 创建时间 |" not in idx_content:
                    idx_content = "# Tasks Index\n\n| 任务目录 | 标题 | 创建时间 |\n|---|---|---|\n"
                ts_human = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                row = f"| {task_stamp} | {task_title.replace('|', '/')} | {ts_human} |"
                if row not in idx_content:
                    if not idx_content.endswith("\n"):
                        idx_content += "\n"
                    idx_content += row + "\n"
                _ = await sftp_put_content(
                    host=host_row["host"],
                    port=int(host_row.get("port") or 22),
                    username=auth.get("username") or "",
                    auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"),
                    key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    remote_path=idx_path,
                    content=idx_content.encode("utf-8"),
                    timeout=30,
                )
                out["task_dir_name"] = task_stamp
                out["task_dir"] = task_dir
                out["task_file"] = f"{task_dir}/task.md"
            return json.dumps(out, ensure_ascii=False)

        if name == "edgeops_save_script":
            host_id = arguments.get("host_id")
            script_name = _safe_script_name(arguments.get("script_name") or "")
            script_content = arguments.get("script_content") or ""
            task_dir_name = (arguments.get("task_dir_name") or "").strip()
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            if not script_name:
                return json.dumps({"success": False, "error": "script_name 非法"}, ensure_ascii=False)
            if not script_content:
                return json.dumps({"success": False, "error": "script_content 不能为空"}, ensure_ascii=False)
            host_row, auth, err = await _resolve_host_for_ai_ops(host_id, user)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            policy = _edgeops_storage_policy(host_row)
            if policy.get("constrained"):
                return json.dumps(
                    {
                        "success": True,
                        "skipped": True,
                        "constrained_mode": True,
                        "mode": policy.get("mode"),
                        "message": "该主机为设备型/专用系统，已跳过 scripts 落盘。请改用主机知识库记录脚本思路与执行要点。",
                        "reason": policy.get("reason") or "",
                    },
                    ensure_ascii=False,
                )
            base_dir, err = await _edgeops_home_dir(host_row, auth)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            script_dir = f"{base_dir}/scripts"
            mk_stdout, mk_stderr, mk_code = await run_ssh_command(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                command=f'mkdir -p "{script_dir}"',
                timeout=20,
            )
            if mk_code != 0:
                return json.dumps({"success": False, "error": mk_stderr or mk_stdout or "创建 scripts 目录失败"}, ensure_ascii=False)
            purpose = (arguments.get("purpose") or "").strip() or "（待补充）"
            parameters_desc = (arguments.get("parameters_desc") or "").strip() or "（待补充）"
            output_desc = (arguments.get("output_desc") or "").strip() or "（待补充）"
            usage_example = (arguments.get("usage_example") or "").strip() or f"./{script_name}"
            doc_content = (arguments.get("doc_content") or "").strip()
            if not doc_content:
                doc_content = (
                    f"# {script_name}\n\n"
                    "## 用途\n"
                    f"{purpose}\n\n"
                    "## 参数\n"
                    f"{parameters_desc}\n\n"
                    "## 输出\n"
                    f"{output_desc}\n\n"
                    "## 用法\n"
                    f"```bash\n{usage_example}\n```\n"
                )
            script_path = f"{script_dir}/{script_name}"
            doc_path = f"{script_dir}/{script_name}.md"
            backup_tag = datetime.now().strftime("%Y%m%d%H%M%S")
            _ = await run_ssh_command(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                command=(
                    f'if [ -f "{script_path}" ]; then cp "{script_path}" "{script_path}.bak.{backup_tag}"; fi; '
                    f'if [ -f "{doc_path}" ]; then cp "{doc_path}" "{doc_path}.bak.{backup_tag}"; fi'
                ),
                timeout=20,
            )
            put_script_err = await sftp_put_content(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                remote_path=script_path,
                content=script_content.encode("utf-8"),
                timeout=30,
            )
            if put_script_err:
                return json.dumps({"success": False, "error": "脚本写入失败: " + put_script_err}, ensure_ascii=False)
            put_doc_err = await sftp_put_content(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                remote_path=doc_path,
                content=doc_content.encode("utf-8"),
                timeout=30,
            )
            if put_doc_err:
                return json.dumps({"success": False, "error": "脚本文档写入失败: " + put_doc_err}, ensure_ascii=False)
            if script_name.endswith(".sh") or script_name.endswith(".py"):
                await run_ssh_command(
                    host=host_row["host"],
                    port=int(host_row.get("port") or 22),
                    username=auth.get("username") or "",
                    auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"),
                    key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    command=f'chmod +x "{script_path}"',
                    timeout=10,
                )
            idx_path = f"{script_dir}/index.md"
            idx_stdout, _, idx_code = await run_ssh_command(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                command=f'cat "{idx_path}" 2>/dev/null || true',
                timeout=20,
            )
            idx_content = _upsert_scripts_index_row((idx_stdout or "") if idx_code == 0 else "", script_name, purpose)
            put_idx_err = await sftp_put_content(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                remote_path=idx_path,
                content=idx_content.encode("utf-8"),
                timeout=30,
            )
            if put_idx_err:
                return json.dumps({"success": False, "error": "index.md 更新失败: " + put_idx_err}, ensure_ascii=False)
            if task_dir_name:
                if ".." not in task_dir_name and "/" not in task_dir_name and "\\" not in task_dir_name:
                    task_file = f"{base_dir}/tasks/{task_dir_name}/task.md"
                    old_task, _, _ = await run_ssh_command(
                        host=host_row["host"],
                        port=int(host_row.get("port") or 22),
                        username=auth.get("username") or "",
                        auth_type=auth.get("auth_type") or "password",
                        password=auth.get("password"),
                        key_path=auth.get("key_path"),
                        private_key_pem=auth.get("private_key_pem"),
                        command=f'cat "{task_file}" 2>/dev/null || true',
                        timeout=20,
                    )
                    task_content = (old_task or "").strip() or "# 任务记录\n\n## 过程记录\n\n"
                    if "## 过程记录" not in task_content:
                        task_content += "\n\n## 过程记录\n\n"
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    task_content += (
                        f"\n### {ts} | 脚本沉淀\n"
                        f"- 动作: 保存脚本 {script_name}\n"
                        f"- 结果: 已更新脚本、同名文档与 scripts/index.md\n"
                    )
                    _ = await sftp_put_content(
                        host=host_row["host"],
                        port=int(host_row.get("port") or 22),
                        username=auth.get("username") or "",
                        auth_type=auth.get("auth_type") or "password",
                        password=auth.get("password"),
                        key_path=auth.get("key_path"),
                        private_key_pem=auth.get("private_key_pem"),
                        remote_path=task_file,
                        content=(task_content.rstrip() + "\n").encode("utf-8"),
                        timeout=30,
                    )
            return json.dumps(
                {
                    "success": True,
                    "script_path": script_path,
                    "doc_path": doc_path,
                    "index_path": idx_path,
                    "backup_tag": backup_tag,
                },
                ensure_ascii=False,
            )

        if name == "edgeops_read_workspace_context":
            host_id = arguments.get("host_id")
            max_lines = max(20, min(400, int(arguments.get("max_lines_per_file") or 120)))
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            host_row, auth, err = await _resolve_host_for_ai_ops(host_id, user)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            policy = _edgeops_storage_policy(host_row)
            if policy.get("constrained"):
                constrained_ctx = await _edgeops_constrained_context(
                    int(host_id),
                    int(user["id"]),
                    policy.get("reason") or "",
                )
                return json.dumps(
                    {
                        "success": True,
                        "base_dir": "~/.edgeops",
                        "exists": False,
                        "constrained_mode": True,
                        "message": "该主机为设备型/专用系统，优先使用主机知识库，已返回知识库上下文。",
                        **constrained_ctx,
                    },
                    ensure_ascii=False,
                )
            base_dir, err = await _edgeops_home_dir(host_row, auth)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            read_cmd = (
                f'BASE="{base_dir}"; '
                'if [ ! -d "$BASE" ]; then echo "__EDGEOPS_NOT_FOUND__"; exit 0; fi; '
                'echo "## scripts files"; ls -1 "$BASE/scripts" 2>/dev/null || true; '
                'echo "\n## scripts/index.md"; sed -n "1,' + str(max_lines) + 'p" "$BASE/scripts/index.md" 2>/dev/null || true; '
                'echo "\n## rules"; ls -1 "$BASE/rules" 2>/dev/null || true; '
                'for f in "$BASE"/rules/*.md; do [ -e "$f" ] || continue; echo "\n### $(basename "$f")"; sed -n "1,' + str(max_lines) + 'p" "$f"; done; '
                'echo "\n## info"; ls -1 "$BASE/info" 2>/dev/null || true; '
                'for f in "$BASE"/info/*.md; do [ -e "$f" ] || continue; echo "\n### $(basename "$f")"; sed -n "1,' + str(max_lines) + 'p" "$f"; done; '
                'echo "\n## recent tasks"; ls -1 "$BASE/tasks" 2>/dev/null | tail -n 10 || true; '
                'for d in $(ls -1 "$BASE/tasks" 2>/dev/null | grep -E "^[0-9]{14}$" | tail -n 3); do '
                '  echo "\n### task:$d"; '
                '  sed -n "1,' + str(max_lines) + 'p" "$BASE/tasks/$d/task.md" 2>/dev/null || true; '
                'done'
            )
            stdout, stderr, code = await run_ssh_command(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                command=read_cmd,
                timeout=60,
            )
            if code != 0:
                return json.dumps({"success": False, "error": stderr or stdout or "读取 .edgeops 上下文失败"}, ensure_ascii=False)
            missing = "__EDGEOPS_NOT_FOUND__" in (stdout or "")
            return json.dumps(
                {
                    "success": True,
                    "base_dir": base_dir,
                    "exists": (not missing),
                    "context": (stdout or "").replace("__EDGEOPS_NOT_FOUND__", "").strip(),
                },
                ensure_ascii=False,
            )

        if name == "edgeops_append_task_log":
            host_id = arguments.get("host_id")
            task_dir_name = (arguments.get("task_dir_name") or "").strip()
            phase = (arguments.get("phase") or "").strip() or "执行"
            action = (arguments.get("action") or "").strip()
            result = (arguments.get("result") or "").strip()
            details = (arguments.get("details") or "").strip()
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            if not task_dir_name:
                return json.dumps({"success": False, "error": "缺少 task_dir_name"}, ensure_ascii=False)
            if ".." in task_dir_name or "/" in task_dir_name or "\\" in task_dir_name:
                return json.dumps({"success": False, "error": "task_dir_name 非法"}, ensure_ascii=False)
            if not action or not result:
                return json.dumps({"success": False, "error": "action 与 result 不能为空"}, ensure_ascii=False)
            host_row, auth, err = await _resolve_host_for_ai_ops(host_id, user)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            policy = _edgeops_storage_policy(host_row)
            if policy.get("constrained"):
                return json.dumps(
                    {
                        "success": True,
                        "skipped": True,
                        "constrained_mode": True,
                        "mode": policy.get("mode"),
                        "message": "该主机为设备型/专用系统，已跳过任务日志落盘。建议把关键步骤写入主机知识库。",
                        "reason": policy.get("reason") or "",
                    },
                    ensure_ascii=False,
                )
            base_dir, err = await _edgeops_home_dir(host_row, auth)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            task_dir = f"{base_dir}/tasks/{task_dir_name}"
            check_stdout, check_stderr, check_code = await run_ssh_command(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                command=f'[ -d "{task_dir}" ] && echo "__OK__" || echo "__NO__"',
                timeout=15,
            )
            if check_code != 0 or "__OK__" not in (check_stdout or ""):
                return json.dumps({"success": False, "error": check_stderr or "任务目录不存在，请先初始化任务目录"}, ensure_ascii=False)
            task_file = f"{task_dir}/task.md"
            read_stdout, _, read_code = await run_ssh_command(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                command=f'cat "{task_file}" 2>/dev/null || true',
                timeout=20,
            )
            content = (read_stdout or "") if read_code == 0 else ""
            if not content.strip():
                content = "# 任务记录\n\n## 过程记录\n\n"
            if "## 过程记录" not in content:
                if not content.endswith("\n"):
                    content += "\n"
                content += "\n## 过程记录\n\n"
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            block = (
                f"### {ts} | {phase}\n"
                f"- 动作: {action}\n"
                f"- 结果: {result}\n"
            )
            if details:
                block += f"- 详情:\n\n{details}\n"
            block += "\n"
            if not content.endswith("\n"):
                content += "\n"
            content += block
            put_err = await sftp_put_content(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                remote_path=task_file,
                content=content.encode("utf-8"),
                timeout=30,
            )
            if put_err:
                return json.dumps({"success": False, "error": "追加任务日志失败: " + put_err}, ensure_ascii=False)
            return json.dumps({"success": True, "task_file": task_file}, ensure_ascii=False)

        if name == "edgeops_write_rule":
            host_id = arguments.get("host_id")
            rule_file = _safe_md_filename(arguments.get("rule_file") or "")
            content = arguments.get("content") or ""
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            if not rule_file:
                return json.dumps({"success": False, "error": "rule_file 非法"}, ensure_ascii=False)
            if not content:
                return json.dumps({"success": False, "error": "content 不能为空"}, ensure_ascii=False)
            host_row, auth, err = await _resolve_host_for_ai_ops(host_id, user)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            policy = _edgeops_storage_policy(host_row)
            if policy.get("constrained"):
                return json.dumps(
                    {
                        "success": True,
                        "skipped": True,
                        "constrained_mode": True,
                        "mode": policy.get("mode"),
                        "message": "该主机为设备型/专用系统，已跳过 rules 落盘。建议将规则摘要写入主机知识库。",
                        "reason": policy.get("reason") or "",
                    },
                    ensure_ascii=False,
                )
            base_dir, err = await _edgeops_home_dir(host_row, auth)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            rule_dir = f"{base_dir}/rules"
            _, mk_err, mk_code = await run_ssh_command(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                command=f'mkdir -p "{rule_dir}"',
                timeout=20,
            )
            if mk_code != 0:
                return json.dumps({"success": False, "error": mk_err or "创建 rules 目录失败"}, ensure_ascii=False)
            rule_path = f"{rule_dir}/{rule_file}"
            put_err = await sftp_put_content(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                remote_path=rule_path,
                content=content.encode("utf-8"),
                timeout=30,
            )
            if put_err:
                return json.dumps({"success": False, "error": "写入规则失败: " + put_err}, ensure_ascii=False)
            return json.dumps({"success": True, "rule_path": rule_path}, ensure_ascii=False)

        if name == "edgeops_write_info":
            host_id = arguments.get("host_id")
            info_file = _safe_md_filename(arguments.get("info_file") or "")
            content = arguments.get("content") or ""
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            if not info_file:
                return json.dumps({"success": False, "error": "info_file 非法"}, ensure_ascii=False)
            if not content:
                return json.dumps({"success": False, "error": "content 不能为空"}, ensure_ascii=False)
            host_row, auth, err = await _resolve_host_for_ai_ops(host_id, user)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            policy = _edgeops_storage_policy(host_row)
            if policy.get("constrained"):
                return json.dumps(
                    {
                        "success": True,
                        "skipped": True,
                        "constrained_mode": True,
                        "mode": policy.get("mode"),
                        "message": "该主机为设备型/专用系统，已跳过 info 落盘。建议将环境信息写入主机知识库。",
                        "reason": policy.get("reason") or "",
                    },
                    ensure_ascii=False,
                )
            base_dir, err = await _edgeops_home_dir(host_row, auth)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            info_dir = f"{base_dir}/info"
            _, mk_err, mk_code = await run_ssh_command(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                command=f'mkdir -p "{info_dir}"',
                timeout=20,
            )
            if mk_code != 0:
                return json.dumps({"success": False, "error": mk_err or "创建 info 目录失败"}, ensure_ascii=False)
            info_path = f"{info_dir}/{info_file}"
            put_err = await sftp_put_content(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                remote_path=info_path,
                content=content.encode("utf-8"),
                timeout=30,
            )
            if put_err:
                return json.dumps({"success": False, "error": "写入信息失败: " + put_err}, ensure_ascii=False)
            return json.dumps({"success": True, "info_path": info_path}, ensure_ascii=False)

        if name == "list_maintenance_history":
            db = await get_db()
            host_id = arguments.get("host_id")
            host_filter = arguments.get("host")
            limit = int(arguments.get("limit") or 50)
            if host_id and not host_filter:
                row = await _get_host_row(host_id)
                host_filter = row.get("host") if row else None
            query = "SELECT id, host, port, category, content, file_path, details, created_at, created_by FROM server_maintenance_history WHERE 1=1"
            params = []
            if not _is_admin(user):
                query += " AND created_by = ?"
                params.append(user["id"])
            if host_filter:
                query += " AND host = ?"
                params.append(host_filter)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(min(limit, 200))
            rows = await db.execute_fetchall(query, params)
            return json.dumps({"success": True, "items": [dict(r) for r in rows]}, ensure_ascii=False)

        if name == "list_host_groups":
            db = await get_db()
            if _is_admin(user):
                rows = await db.execute_fetchall(
                    "SELECT id, name, description, parent_id, created_by, created_at FROM host_groups ORDER BY COALESCE(parent_id, 0), id"
                )
            else:
                rows = await db.execute_fetchall(
                    "SELECT id, name, description, parent_id, created_by, created_at FROM host_groups WHERE created_by = ? ORDER BY COALESCE(parent_id, 0), id",
                    (user["id"],),
                )
            return json.dumps({"success": True, "groups": [dict(r) for r in rows]}, ensure_ascii=False)

        if name == "get_host_groups_tree":
            db = await get_db()
            host_q = (arguments.get("host_q") or "").strip()
            matching_ids = None
            if host_q:
                like = f"%{host_q.lower()}%"
                parts = [
                    "LOWER(name) LIKE ?",
                    "LOWER(host) LIKE ?",
                    "CAST(port AS TEXT) LIKE ?",
                    "LOWER(IFNULL(description,'')) LIKE ?",
                    "LOWER(IFNULL(remark,'')) LIKE ?",
                    "LOWER(IFNULL(aliases,'')) LIKE ?",
                    "LOWER(IFNULL(host_type,'')) LIKE ?",
                ]
                sp: list = [like, like, like, like, like, like, like]
                if host_q.isdigit():
                    parts.append("id = ?")
                    try:
                        sp.append(int(host_q))
                    except ValueError:
                        pass
                sw = "(" + " OR ".join(parts) + ")"
                if _is_admin(user):
                    id_rows = await db.execute_fetchall(f"SELECT id FROM hosts WHERE {sw}", sp)
                else:
                    id_rows = await db.execute_fetchall(
                        f"""SELECT DISTINCT h.id
                            FROM hosts h
                            LEFT JOIN host_shares hs
                              ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                            WHERE (h.created_by = ? OR hs.id IS NOT NULL) AND {sw}""",
                        (user["id"], user["id"], *sp),
                    )
                matching_ids = {r["id"] for r in id_rows}

            if _is_admin(user):
                rows = await db.execute_fetchall("SELECT id, name, description, parent_id, created_by, created_at FROM host_groups ORDER BY id")
                host_rows = await db.execute_fetchall(
                    f"""SELECT {HOST_TREE_SELECT_COLS}
                        FROM hosts h {HOST_LIST_OWNER_JOIN}
                        ORDER BY h.name"""
                )
            else:
                rows = await db.execute_fetchall("SELECT id, name, description, parent_id, created_by, created_at FROM host_groups WHERE created_by = ? ORDER BY id", (user["id"],))
                host_rows = await db.execute_fetchall(
                    f"""SELECT DISTINCT {HOST_TREE_SELECT_COLS}
                        FROM hosts h {HOST_LIST_OWNER_JOIN}
                        LEFT JOIN host_shares hs
                          ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                        WHERE h.created_by = ? OR hs.id IS NOT NULL
                        ORDER BY h.name""",
                    (user["id"], user["id"]),
                )
            groups = [dict(r) for r in rows]
            members = await db.execute_fetchall("SELECT host_id, group_id FROM host_group_members")
            group_to_hosts = {}
            for m in members:
                gid = m["group_id"]
                group_to_hosts.setdefault(gid, []).append(m["host_id"])
            hosts_by_id = {h["id"]: normalize_host_aliases_in_dict(dict(h)) for h in host_rows}
            for gid, hid_list in list(group_to_hosts.items()):
                # 与 /api/host-groups/tree 保持一致：同分组内按 host_id 去重，避免旧数据重复关系导致计数偏大
                unique_ids = []
                seen_ids = set()
                for hid in hid_list:
                    if hid in seen_ids:
                        continue
                    seen_ids.add(hid)
                    unique_ids.append(hid)
                group_to_hosts[gid] = [
                    hosts_by_id[hid]
                    for hid in unique_ids
                    if hid in hosts_by_id and (matching_ids is None or hid in matching_ids)
                ]

            def build_node(g):
                return {**g, "children": [], "hosts": group_to_hosts.get(g["id"], [])}

            by_id = {g["id"]: build_node(g) for g in groups}
            root = []
            for g in groups:
                node = by_id[g["id"]]
                pid = g.get("parent_id")
                if not pid:
                    root.append(node)
                else:
                    parent = by_id.get(pid)
                    if parent:
                        parent["children"].append(node)
                    else:
                        root.append(node)
            out = {"success": True, "tree": root}
            if host_q:
                out["host_q"] = host_q
                out["matched_host_count"] = len(matching_ids) if matching_ids is not None else 0
            return json.dumps(out, ensure_ascii=False)

        if name == "get_group_detail":
            gid = arguments.get("group_id")
            if gid is None:
                return json.dumps({"success": False, "error": "缺少 group_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT * FROM host_groups WHERE id = ?", (gid,))
            if not rows:
                return json.dumps({"success": False, "error": f"分组 ID={gid} 不存在"}, ensure_ascii=False)
            group = dict(rows[0])
            if not _is_admin(user) and group.get("created_by") != user["id"]:
                return json.dumps({"success": False, "error": f"分组 ID={gid} 不存在"}, ensure_ascii=False)
            members = await db.execute_fetchall("SELECT host_id FROM host_group_members WHERE group_id = ?", (gid,))
            unique_host_ids = []
            seen_host_ids = set()
            for m in members:
                hid = m["host_id"]
                if hid in seen_host_ids:
                    continue
                seen_host_ids.add(hid)
                unique_host_ids.append(hid)
            group["host_ids"] = unique_host_ids
            return json.dumps({"success": True, "group": group}, ensure_ascii=False)

        if name == "create_host":
            name_ = (arguments.get("name") or "").strip()
            host_ = normalize_host_address(arguments.get("host") or "")
            if not name_ or not host_:
                return json.dumps({"success": False, "error": "name 和 host 必填"}, ensure_ascii=False)
            port_ = normalize_host_port(arguments.get("port"))
            allow_dup = bool(arguments.get("allow_duplicate"))
            desc_ = (arguments.get("description") or "").strip()
            aliases_json = serialize_host_aliases_for_db(arguments.get("aliases"))
            remark_ = (arguments.get("remark") or "").strip()
            cred_id = arguments.get("credential_id")
            new_cred = arguments.get("new_credential")
            db = await get_db()
            if not allow_dup:
                existing = await find_duplicate_host_for_owner(
                    db, owner_user_id=int(user["id"]), host=host_, port=port_
                )
                if existing:
                    return json.dumps(
                        {
                            "success": False,
                            "duplicate": True,
                            **host_duplicate_error_detail(existing),
                        },
                        ensure_ascii=False,
                    )
            if cred_id:
                r = await db.execute_fetchall("SELECT id, created_by FROM credentials WHERE id = ?", (cred_id,))
                if not r:
                    return json.dumps({"success": False, "error": "所选凭证不存在"}, ensure_ascii=False)
                if not _is_admin(user) and r[0]["created_by"] != user["id"]:
                    return json.dumps({"success": False, "error": "无权使用该凭证"}, ensure_ascii=False)
                await db.execute(
                    "INSERT INTO hosts (name, host, port, credential_id, description, aliases, remark, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (name_, host_, port_, cred_id, desc_, aliases_json, remark_, user["id"]),
                )
            elif new_cred and isinstance(new_cred, dict):
                code = (new_cred.get("code") or "").strip() or _make_inline_credential_code(host_, port_)
                cred_name = (new_cred.get("name") or "").strip() or (name_[:28] + " 登录")
                username = (new_cred.get("username") or "").strip()
                typ = (new_cred.get("type") or "password").strip().lower()
                # 若提供了私钥或公钥却未声明 key_pair，按内容推断为公钥认证，避免存成 password 导致认证失败
                if typ not in ("key_pair", "key") and (new_cred.get("private_key") or new_cred.get("public_key")):
                    typ = "key_pair"
                if typ in ("key_pair", "key"):
                    priv = normalize_private_key_pem(new_cred.get("private_key")) or ""
                    if not priv:
                        return json.dumps({"success": False, "error": "新建密钥凭证需填写 private_key"}, ensure_ascii=False)
                    pub = (new_cred.get("public_key") or "").strip() or None
                    await db.execute(
                        """INSERT INTO credentials (type, code, name, description, username, key_type, key_bits, public_key, private_key_enc, created_by)
                           VALUES ('key_pair', ?, ?, '', ?, 'RSA', 2048, ?, ?, ?)""",
                        (code, cred_name, username, pub, priv, user["id"]),
                    )
                else:
                    pw = new_cred.get("password") or ""
                    if not username:
                        return json.dumps({"success": False, "error": "新建密码凭证需填写 username"}, ensure_ascii=False)
                    if not pw:
                        return json.dumps({"success": False, "error": "新建密码凭证需填写 password"}, ensure_ascii=False)
                    await db.execute(
                        "INSERT INTO credentials (type, code, name, description, username, password_enc, created_by) VALUES ('password', ?, ?, '', ?, ?, ?)",
                        (code, cred_name, username, pw, user["id"]),
                    )
                await db.commit()
                cur = await db.execute("SELECT last_insert_rowid()")
                cred_id = (await cur.fetchone())[0]
                await db.execute(
                    "INSERT INTO hosts (name, host, port, credential_id, description, aliases, remark, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (name_, host_, port_, cred_id, desc_, aliases_json, remark_, user["id"]),
                )
            else:
                return json.dumps({"success": False, "error": "请提供 credential_id 或 new_credential"}, ensure_ascii=False)
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            hid = (await cur.fetchone())[0]
            return json.dumps({"success": True, "id": hid}, ensure_ascii=False)

        if name == "update_host":
            host_id = arguments.get("host_id")
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            db = await get_db()
            r = await db.execute_fetchall("SELECT id, created_by FROM hosts WHERE id = ?", (host_id,))
            if not r:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            host_row = dict(r[0])
            if not (_is_admin(user) or host_row.get("created_by") == user["id"]):
                return json.dumps({"success": False, "error": "仅主机所有者可修改主机信息"}, ensure_ascii=False)
            updates, params = [], []
            for f in ("name", "host", "port", "credential_id", "username", "auth_type", "key_path", "description", "remark"):
                if f in arguments and arguments[f] is not None:
                    updates.append(f"{f} = ?")
                    params.append(arguments[f])
            if "aliases" in arguments and arguments["aliases"] is not None:
                updates.append("aliases = ?")
                params.append(serialize_host_aliases_for_db(arguments["aliases"]))
            if "password" in arguments and arguments["password"] is not None:
                updates.append("password_enc = ?")
                params.append(arguments["password"])
            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                params.append(host_id)
                await db.execute(f"UPDATE hosts SET {', '.join(updates)} WHERE id = ?", params)
                await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "delete_host":
            host_id = arguments.get("host_id")
            task_dir_name = (arguments.get("task_dir_name") or "").strip()
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            db = await get_db()
            r = await db.execute_fetchall("SELECT id, created_by FROM hosts WHERE id = ?", (host_id,))
            if not r:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            host_row = dict(r[0])
            if _is_admin(user) or host_row.get("created_by") == user["id"]:
                auth = await _resolve_host_auth(db, host_row)
                if auth:
                    await _edgeops_auto_append_task_log(
                        host_row,
                        auth,
                        task_dir_name,
                        phase="高风险操作",
                        action=f"删除主机 ID={host_id}",
                        result="已执行真实删除",
                    )
                await db.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
                await db.commit()
                return json.dumps({"success": True, "deleted": True}, ensure_ascii=False)
            if not await _can_access_host_with_shares(host_row, user):
                return json.dumps({"success": False, "error": "无权操作该主机"}, ensure_ascii=False)
            auth = await _resolve_host_auth(db, host_row)
            if auth:
                await _edgeops_auto_append_task_log(
                    host_row,
                    auth,
                    task_dir_name,
                    phase="高风险操作",
                    action=f"解除分享主机 ID={host_id}",
                    result="已解除当前用户分享绑定",
                )
            await db.execute(
                "UPDATE host_shares SET revoked_at = CURRENT_TIMESTAMP WHERE host_id = ? AND shared_with_user_id = ? AND revoked_at IS NULL",
                (host_id, user["id"]),
            )
            await _cleanup_shared_host_group_members(host_id, user["id"])
            await _log_share_audit(
                actor_user_id=user["id"],
                host_id=host_id,
                operation="host_share_detach",
                params={"host_id": host_id, "shared_user_id": user["id"]},
            )
            await db.commit()
            return json.dumps({"success": True, "detached": True, "message": "已解除该主机分享"}, ensure_ascii=False)

        if name == "share_host":
            host_id = arguments.get("host_id")
            user_id = arguments.get("user_id")
            username = (arguments.get("username") or "").strip()
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            db = await get_db()
            host_rows = await db.execute_fetchall("SELECT id, created_by FROM hosts WHERE id = ?", (host_id,))
            if not host_rows:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            host_row = dict(host_rows[0])
            if not (_is_admin(user) or host_row.get("created_by") == user["id"]):
                return json.dumps({"success": False, "error": "仅主机所有者可分享该主机"}, ensure_ascii=False)
            target_id = int(user_id) if user_id is not None else None
            if target_id is None and not username:
                return json.dumps({"success": False, "error": "请提供 user_id 或 username"}, ensure_ascii=False)
            if target_id is not None:
                target_rows = await db.execute_fetchall("SELECT id, username, display_name FROM users WHERE id = ?", (target_id,))
            else:
                target_rows = await db.execute_fetchall("SELECT id, username, display_name FROM users WHERE username = ?", (username,))
            if not target_rows:
                return json.dumps({"success": False, "error": "目标用户不存在"}, ensure_ascii=False)
            target = dict(target_rows[0])
            target_id = int(target["id"])
            owner_id = int(host_row["created_by"])
            if target_id == owner_id:
                return json.dumps({"success": False, "error": "无需分享给自己"}, ensure_ascii=False)
            await db.execute(
                """INSERT INTO host_shares (host_id, owner_user_id, shared_with_user_id, created_by, created_at, revoked_at)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, NULL)
                   ON CONFLICT(host_id, shared_with_user_id) DO UPDATE SET
                     owner_user_id = excluded.owner_user_id,
                     created_by = excluded.created_by,
                     created_at = CURRENT_TIMESTAMP,
                     revoked_at = NULL""",
                (host_id, owner_id, target_id, user["id"]),
            )
            await _log_share_audit(
                actor_user_id=user["id"],
                host_id=host_id,
                operation="host_share_create",
                params={"host_id": host_id, "owner_user_id": owner_id, "shared_with_user_id": target_id},
            )
            await db.commit()
            return json.dumps(
                {
                    "success": True,
                    "host_id": host_id,
                    "shared_with_user_id": target_id,
                    "shared_with_username": target.get("username") or "",
                    "shared_with_display_name": target.get("display_name") or "",
                },
                ensure_ascii=False,
            )

        if name == "revoke_host_share":
            host_id = arguments.get("host_id")
            target_user_id = arguments.get("target_user_id")
            if host_id is None or target_user_id is None:
                return json.dumps({"success": False, "error": "需要 host_id 和 target_user_id"}, ensure_ascii=False)
            db = await get_db()
            host_rows = await db.execute_fetchall("SELECT id, created_by FROM hosts WHERE id = ?", (host_id,))
            if not host_rows:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            host_row = dict(host_rows[0])
            is_owner = _is_admin(user) or host_row.get("created_by") == user["id"]
            is_self = int(target_user_id) == int(user["id"])
            if not is_owner and not is_self:
                return json.dumps({"success": False, "error": "无权撤销该分享"}, ensure_ascii=False)
            rows = await db.execute_fetchall(
                """SELECT id FROM host_shares
                   WHERE host_id = ? AND shared_with_user_id = ? AND revoked_at IS NULL""",
                (host_id, target_user_id),
            )
            if not rows:
                return json.dumps({"success": False, "error": "分享记录不存在"}, ensure_ascii=False)
            await db.execute(
                "UPDATE host_shares SET revoked_at = CURRENT_TIMESTAMP WHERE host_id = ? AND shared_with_user_id = ? AND revoked_at IS NULL",
                (host_id, target_user_id),
            )
            await _cleanup_shared_host_group_members(host_id, target_user_id)
            await _log_share_audit(
                actor_user_id=user["id"],
                host_id=host_id,
                operation="host_share_revoke",
                params={"host_id": host_id, "target_user_id": target_user_id},
            )
            await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "list_host_shares":
            host_id = arguments.get("host_id")
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            db = await get_db()
            host_rows = await db.execute_fetchall("SELECT id, created_by FROM hosts WHERE id = ?", (host_id,))
            if not host_rows:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            host_row = dict(host_rows[0])
            if not (_is_admin(user) or host_row.get("created_by") == user["id"]):
                return json.dumps({"success": False, "error": "仅主机所有者可查看分享清单"}, ensure_ascii=False)
            rows = await db.execute_fetchall(
                """SELECT hs.id, hs.host_id, hs.shared_with_user_id, hs.created_at,
                          u.username AS shared_with_username, u.display_name AS shared_with_display_name
                   FROM host_shares hs
                   JOIN users u ON u.id = hs.shared_with_user_id
                   WHERE hs.host_id = ? AND hs.revoked_at IS NULL
                   ORDER BY hs.created_at DESC""",
                (host_id,),
            )
            return json.dumps({"success": True, "shares": [dict(r) for r in rows]}, ensure_ascii=False)

        if name == "list_received_host_shares":
            db = await get_db()
            rows = await db.execute_fetchall(
                """SELECT hs.id, hs.host_id, hs.created_at,
                          h.name, h.host, h.port, h.aliases, h.remark,
                          u.id AS owner_user_id, u.username AS owner_username, u.display_name AS owner_display_name
                   FROM host_shares hs
                   JOIN hosts h ON h.id = hs.host_id
                   JOIN users u ON u.id = hs.owner_user_id
                   WHERE hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                   ORDER BY hs.created_at DESC""",
                (user["id"],),
            )
            return json.dumps(
                {"success": True, "shares": [normalize_host_aliases_in_dict(dict(r)) for r in rows]},
                ensure_ascii=False,
            )

        if name == "host_stats":
            db = await get_db()
            if _is_admin(user):
                cur = await db.execute("SELECT COUNT(*) FROM hosts")
            else:
                cur = await db.execute(
                    """SELECT COUNT(DISTINCT h.id)
                       FROM hosts h
                       LEFT JOIN host_shares hs
                         ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                       WHERE h.created_by = ? OR hs.id IS NOT NULL""",
                    (user["id"], user["id"]),
                )
            total = (await cur.fetchone())[0]
            return json.dumps({"success": True, "stats": {"total_hosts": total}}, ensure_ascii=False)

        if name == "create_group":
            name_ = (arguments.get("name") or "").strip()
            if not name_:
                return json.dumps({"success": False, "error": "name 必填"}, ensure_ascii=False)
            db = await get_db()
            await db.execute(
                "INSERT INTO host_groups (name, description, parent_id, created_by) VALUES (?, ?, ?, ?)",
                (name_, (arguments.get("description") or "").strip(), arguments.get("parent_id") or None, user["id"]),
            )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            gid = (await cur.fetchone())[0]
            return json.dumps({"success": True, "id": gid}, ensure_ascii=False)

        if name == "update_group":
            group_id = arguments.get("group_id")
            if group_id is None:
                return json.dumps({"success": False, "error": "缺少 group_id"}, ensure_ascii=False)
            db = await get_db()
            r = await db.execute_fetchall("SELECT id, created_by FROM host_groups WHERE id = ?", (group_id,))
            if not r:
                return json.dumps({"success": False, "error": f"分组 ID={group_id} 不存在"}, ensure_ascii=False)
            if not _can_access_group(dict(r[0]), user):
                return json.dumps({"success": False, "error": "无权操作该分组"}, ensure_ascii=False)
            updates, params = [], []
            for f in ("name", "description", "parent_id"):
                if f in arguments and arguments[f] is not None:
                    updates.append(f"{f} = ?")
                    params.append(arguments[f])
            if updates:
                params.append(group_id)
                await db.execute(f"UPDATE host_groups SET {', '.join(updates)} WHERE id = ?", params)
                await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "delete_group":
            group_id = arguments.get("group_id")
            if group_id is None:
                return json.dumps({"success": False, "error": "缺少 group_id"}, ensure_ascii=False)
            db = await get_db()
            r = await db.execute_fetchall("SELECT id, created_by FROM host_groups WHERE id = ?", (group_id,))
            if not r:
                return json.dumps({"success": False, "error": f"分组 ID={group_id} 不存在"}, ensure_ascii=False)
            if not _can_access_group(dict(r[0]), user):
                return json.dumps({"success": False, "error": "无权操作该分组"}, ensure_ascii=False)
            await db.execute("DELETE FROM host_group_members WHERE group_id = ?", (group_id,))
            await db.execute("DELETE FROM host_groups WHERE id = ?", (group_id,))
            await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "get_group_hosts":
            group_id = arguments.get("group_id")
            if group_id is None:
                return json.dumps({"success": False, "error": "缺少 group_id"}, ensure_ascii=False)
            db = await get_db()
            gr = await db.execute_fetchall("SELECT id, created_by FROM host_groups WHERE id = ?", (group_id,))
            if not gr:
                return json.dumps({"success": False, "error": f"分组 ID={group_id} 不存在"}, ensure_ascii=False)
            if not _can_access_group(dict(gr[0]), user):
                return json.dumps({"success": False, "error": "无权访问该分组"}, ensure_ascii=False)
            rows = await db.execute_fetchall("SELECT host_id FROM host_group_members WHERE group_id = ?", (group_id,))
            host_ids = [r["host_id"] for r in rows]
            if not host_ids:
                return json.dumps({"success": True, "hosts": []}, ensure_ascii=False)
            ph = ",".join("?" * len(host_ids))
            if _is_admin(user):
                hosts = await db.execute_fetchall(
                    f"SELECT id, name, host, port, credential_id, aliases, remark FROM hosts WHERE id IN ({ph})",
                    host_ids,
                )
            else:
                hosts = await db.execute_fetchall(
                    f"""SELECT DISTINCT h.id, h.name, h.host, h.port, h.credential_id, h.aliases, h.remark
                        FROM hosts h
                        LEFT JOIN host_shares hs
                          ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
                        WHERE h.id IN ({ph}) AND (h.created_by = ? OR hs.id IS NOT NULL)""",
                    [user["id"], *host_ids, user["id"]],
                )
            return json.dumps(
                {"success": True, "hosts": [normalize_host_aliases_in_dict(dict(h)) for h in hosts]},
                ensure_ascii=False,
            )

        if name == "add_hosts_to_group":
            group_id = arguments.get("group_id")
            host_ids = arguments.get("host_ids") or []
            if group_id is None:
                return json.dumps({"success": False, "error": "缺少 group_id"}, ensure_ascii=False)
            db = await get_db()
            gr = await db.execute_fetchall("SELECT id, created_by FROM host_groups WHERE id = ?", (group_id,))
            if not gr:
                return json.dumps({"success": False, "error": f"分组 ID={group_id} 不存在"}, ensure_ascii=False)
            if not _can_access_group(dict(gr[0]), user):
                return json.dumps({"success": False, "error": "无权操作该分组"}, ensure_ascii=False)
            group_owner = gr[0]["created_by"]
            if host_ids:
                host_rows = await db.execute_fetchall(
                    "SELECT id, created_by FROM hosts WHERE id IN ({})".format(",".join("?" * len(host_ids))),
                    host_ids,
                )
                for r in host_rows:
                    if not await _can_access_host_with_shares(dict(r), user):
                        return json.dumps({"success": False, "error": "无权将该主机加入分组"}, ensure_ascii=False)
                ph = ",".join("?" * len(host_ids))
                await db.execute(
                    f"""DELETE FROM host_group_members
                        WHERE host_id IN ({ph})
                          AND group_id IN (SELECT id FROM host_groups WHERE created_by = ?)""",
                    [*host_ids, group_owner],
                )
            for hid in host_ids:
                try:
                    await db.execute("INSERT OR IGNORE INTO host_group_members (host_id, group_id) VALUES (?, ?)", (hid, group_id))
                except Exception:
                    pass
            await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "remove_host_from_group":
            group_id = arguments.get("group_id")
            host_id = arguments.get("host_id")
            if group_id is None or host_id is None:
                return json.dumps({"success": False, "error": "需要 group_id 和 host_id"}, ensure_ascii=False)
            db = await get_db()
            gr = await db.execute_fetchall("SELECT id, created_by FROM host_groups WHERE id = ?", (group_id,))
            if not gr:
                return json.dumps({"success": False, "error": "分组不存在"}, ensure_ascii=False)
            if not _can_access_group(dict(gr[0]), user):
                return json.dumps({"success": False, "error": "无权操作该分组"}, ensure_ascii=False)
            await db.execute("DELETE FROM host_group_members WHERE group_id = ? AND host_id = ?", (group_id, host_id))
            await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "list_credentials":
            db = await get_db()
            if _is_admin(user):
                rows = await db.execute_fetchall(
                    "SELECT id, type, code, name, description, username, key_type, key_bits, created_at FROM credentials ORDER BY id"
                )
            else:
                rows = await db.execute_fetchall(
                    "SELECT id, type, code, name, description, username, key_type, key_bits, created_at FROM credentials WHERE created_by = ? ORDER BY id",
                    (user["id"],),
                )
            return json.dumps({"success": True, "credentials": [dict(r) for r in rows]}, ensure_ascii=False)

        if name == "get_credential_detail":
            cid = arguments.get("credential_id")
            if cid is None:
                return json.dumps({"success": False, "error": "缺少 credential_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT * FROM credentials WHERE id = ?", (cid,))
            if not rows:
                return json.dumps({"success": False, "error": f"凭证 ID={cid} 不存在"}, ensure_ascii=False)
            d = dict(rows[0])
            if not _is_admin(user) and d.get("created_by") != user["id"]:
                return json.dumps({"success": False, "error": f"凭证 ID={cid} 不存在"}, ensure_ascii=False)
            if d.get("password_enc"):
                d["password_enc"] = "***"
            if d.get("private_key_enc"):
                d["private_key_enc"] = "***"
            return json.dumps({"success": True, "credential": d}, ensure_ascii=False)

        if name == "create_credential":
            cred_type = (arguments.get("type") or "password").strip().lower()
            code = (arguments.get("code") or "").strip()
            name_ = (arguments.get("name") or "").strip()
            if not code or not name_:
                return json.dumps({"success": False, "error": "code 和 name 必填"}, ensure_ascii=False)
            db = await get_db()
            try:
                if cred_type == "password":
                    username = (arguments.get("username") or "").strip()
                    password = arguments.get("password") or ""
                    if not username:
                        return json.dumps({"success": False, "error": "密码型凭证需填写 username"}, ensure_ascii=False)
                    await db.execute(
                        "INSERT INTO credentials (type, code, name, description, username, password_enc, created_by) VALUES ('password', ?, ?, ?, ?, ?, ?)",
                        (code, name_, (arguments.get("description") or "").strip(), username, password, user["id"]),
                    )
                else:
                    username = (arguments.get("username") or "").strip()
                    key_type = (arguments.get("key_type") or "RSA").upper()
                    key_bits = int(arguments.get("key_bits") or 2048)
                    public_key = arguments.get("public_key") or ""
                    private_key = normalize_private_key_pem(arguments.get("private_key")) or ""
                    if not username and not (public_key and private_key):
                        return json.dumps({"success": False, "error": "密钥型需提供 username 及公钥/私钥或使用 generate_key"}, ensure_ascii=False)
                    await db.execute(
                        "INSERT INTO credentials (type, code, name, description, username, key_type, key_bits, public_key, private_key_enc, created_by) VALUES ('key_pair', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (code, name_, (arguments.get("description") or "").strip(), username, key_type, key_bits, public_key or None, private_key or None, user["id"]),
                    )
                await db.commit()
                cur = await db.execute("SELECT last_insert_rowid()")
                cid = (await cur.fetchone())[0]
                return json.dumps({"success": True, "id": cid}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "update_credential":
            cid = arguments.get("credential_id")
            if cid is None:
                return json.dumps({"success": False, "error": "缺少 credential_id"}, ensure_ascii=False)
            db = await get_db()
            r = await db.execute_fetchall("SELECT id, created_by FROM credentials WHERE id = ?", (cid,))
            if not r:
                return json.dumps({"success": False, "error": f"凭证 ID={cid} 不存在"}, ensure_ascii=False)
            if not _is_admin(user) and r[0]["created_by"] != user["id"]:
                return json.dumps({"success": False, "error": f"凭证 ID={cid} 不存在"}, ensure_ascii=False)
            updates, params = [], []
            for f in ("code", "name", "description", "username"):
                if f in arguments and arguments[f] is not None:
                    updates.append(f"{f} = ?")
                    params.append(arguments[f])
            if "password" in arguments and arguments["password"] is not None:
                updates.append("password_enc = ?")
                params.append(arguments["password"])
            if "public_key" in arguments and arguments["public_key"] is not None:
                updates.append("public_key = ?")
                params.append(arguments["public_key"])
            if "private_key" in arguments and arguments["private_key"] is not None:
                updates.append("private_key_enc = ?")
                params.append(normalize_private_key_pem(arguments["private_key"]))
            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                params.append(cid)
                await db.execute(f"UPDATE credentials SET {', '.join(updates)} WHERE id = ?", params)
                await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "delete_credential":
            cid = arguments.get("credential_id")
            if cid is None:
                return json.dumps({"success": False, "error": "缺少 credential_id"}, ensure_ascii=False)
            db = await get_db()
            r = await db.execute_fetchall("SELECT id, created_by FROM credentials WHERE id = ?", (cid,))
            if not r:
                return json.dumps({"success": False, "error": "凭证不存在"}, ensure_ascii=False)
            if not _is_admin(user) and r[0]["created_by"] != user["id"]:
                return json.dumps({"success": False, "error": "凭证不存在"}, ensure_ascii=False)
            cur = await db.execute("SELECT COUNT(*) FROM hosts WHERE credential_id = ?", (cid,))
            if (await cur.fetchone())[0] > 0:
                return json.dumps({"success": False, "error": "该凭证已被主机引用，请先解除关联"}, ensure_ascii=False)
            await db.execute("DELETE FROM credentials WHERE id = ?", (cid,))
            await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "cleanup_orphan_credentials":
            scope = (arguments.get("scope") or "mine").strip().lower()
            dry_run = bool(arguments.get("dry_run", False))
            if scope not in ("mine", "all"):
                return json.dumps({"success": False, "error": "scope 只支持 mine / all"}, ensure_ascii=False)
            if scope == "all" and not _is_admin(user):
                return json.dumps({"success": False, "error": "scope=all 仅管理员可用；请改用 scope=mine 只清理自己的"}, ensure_ascii=False)
            db = await get_db()
            # 孤立凭证 = 没有任何 hosts.credential_id 指向它的凭证
            base_sql = (
                "SELECT c.id, c.code, c.name, c.type, c.created_by, c.created_at "
                "FROM credentials c "
                "WHERE NOT EXISTS (SELECT 1 FROM hosts h WHERE h.credential_id = c.id)"
            )
            params: list = []
            if scope == "mine":
                base_sql += " AND c.created_by = ?"
                params.append(user["id"])
            base_sql += " ORDER BY c.id"
            rows = await db.execute_fetchall(base_sql, tuple(params))
            items = [
                {
                    "id": r["id"],
                    "code": r["code"],
                    "name": r["name"],
                    "type": r["type"],
                    "created_by": r["created_by"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
            if not items:
                return json.dumps(
                    {
                        "success": True,
                        "deleted": 0,
                        "dry_run": dry_run,
                        "scope": scope,
                        "message": "未发现孤立凭证（所有凭证都已被主机引用）",
                        "items": [],
                    },
                    ensure_ascii=False,
                )
            if dry_run:
                return json.dumps(
                    {
                        "success": True,
                        "deleted": 0,
                        "dry_run": True,
                        "scope": scope,
                        "message": f"预览：将删除 {len(items)} 个孤立凭证。请用 dry_run=false 实际执行。",
                        "items": items,
                    },
                    ensure_ascii=False,
                )
            ids = [it["id"] for it in items]
            CHUNK = 500
            for i in range(0, len(ids), CHUNK):
                chunk = ids[i:i + CHUNK]
                placeholders = ",".join("?" * len(chunk))
                await db.execute(
                    f"DELETE FROM credentials WHERE id IN ({placeholders})", chunk
                )
            await db.commit()
            return json.dumps(
                {
                    "success": True,
                    "deleted": len(ids),
                    "dry_run": False,
                    "scope": scope,
                    "message": f"已清理 {len(ids)} 个未被主机引用的孤立凭证。",
                    "items": items,
                },
                ensure_ascii=False,
            )

        if name == "generate_key":
            key_type = (arguments.get("key_type") or "RSA").upper()
            key_bits = int(arguments.get("key_bits") or 2048)
            try:
                if key_type == "RSA":
                    private_pem, public_pem = generate_rsa_key(key_bits)
                elif key_type in ("ECC", "EC"):
                    curve = "secp256r1" if key_bits <= 256 else ("secp384r1" if key_bits <= 384 else "secp521r1")
                    private_pem, public_pem = generate_ecc_key(curve)
                else:
                    return json.dumps({"success": False, "error": "key_type 支持 RSA / ECC"}, ensure_ascii=False)
                return json.dumps({"success": True, "private_key": private_pem, "public_key": public_pem}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "get_maintenance_item":
            item_id = arguments.get("item_id")
            if item_id is None:
                return json.dumps({"success": False, "error": "缺少 item_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT * FROM server_maintenance_history WHERE id = ?", (item_id,))
            if not rows:
                return json.dumps({"success": False, "error": f"记录 ID={item_id} 不存在"}, ensure_ascii=False)
            r = dict(rows[0])
            if not _is_admin(user) and r.get("created_by") != user["id"]:
                return json.dumps({"success": False, "error": "记录不存在或无权访问"}, ensure_ascii=False)
            return json.dumps({"success": True, "item": r}, ensure_ascii=False)

        if name == "create_maintenance":
            host_ = (arguments.get("host") or "").strip()
            category_ = (arguments.get("category") or "").strip()
            if not host_ or not category_:
                return json.dumps({"success": False, "error": "host 和 category 必填"}, ensure_ascii=False)
            db = await get_db()
            await db.execute(
                "INSERT INTO server_maintenance_history (host, port, category, content, file_path, details, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (host_, int(arguments.get("port") or 22), category_, (arguments.get("content") or "").strip(), arguments.get("file_path"), arguments.get("details"), user["id"]),
            )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            iid = (await cur.fetchone())[0]
            return json.dumps({"success": True, "id": iid}, ensure_ascii=False)

        if name == "update_maintenance":
            item_id = arguments.get("item_id")
            if item_id is None:
                return json.dumps({"success": False, "error": "缺少 item_id"}, ensure_ascii=False)
            db = await get_db()
            r = await db.execute_fetchall("SELECT id, created_by FROM server_maintenance_history WHERE id = ?", (item_id,))
            if not r:
                return json.dumps({"success": False, "error": f"记录 ID={item_id} 不存在"}, ensure_ascii=False)
            row = dict(r[0])
            if not _is_admin(user) and row.get("created_by") != user["id"]:
                return json.dumps({"success": False, "error": "仅创建人或管理员可修改"}, ensure_ascii=False)
            updates, params = [], []
            for f in ("category", "content", "file_path", "details"):
                if f in arguments and arguments[f] is not None:
                    updates.append(f"{f} = ?")
                    params.append(arguments[f])
            if updates:
                params.append(item_id)
                await db.execute(f"UPDATE server_maintenance_history SET {', '.join(updates)} WHERE id = ?", params)
                await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "delete_maintenance":
            item_id = arguments.get("item_id")
            if item_id is None:
                return json.dumps({"success": False, "error": "缺少 item_id"}, ensure_ascii=False)
            db = await get_db()
            r = await db.execute_fetchall("SELECT id, created_by FROM server_maintenance_history WHERE id = ?", (item_id,))
            if not r:
                return json.dumps({"success": False, "error": f"记录 ID={item_id} 不存在"}, ensure_ascii=False)
            if not _is_admin(user) and dict(r[0]).get("created_by") != user["id"]:
                return json.dumps({"success": False, "error": "仅创建人或管理员可删除"}, ensure_ascii=False)
            await db.execute("DELETE FROM server_maintenance_history WHERE id = ?", (item_id,))
            await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "list_prompt_skills":
            db = await get_db()
            rows = await db.execute_fetchall("SELECT id, code, name, description, parameters_schema, enabled FROM skills WHERE enabled = 1 ORDER BY id")
            return json.dumps({"success": True, "skills": [dict(r) for r in rows]}, ensure_ascii=False)

        if name == "get_prompt_skill":
            skill_id = arguments.get("skill_id")
            if skill_id is None:
                return json.dumps({"success": False, "error": "缺少 skill_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT * FROM skills WHERE id = ? AND enabled = 1", (skill_id,))
            if not rows:
                return json.dumps({"success": False, "error": f"Skill ID={skill_id} 不存在"}, ensure_ascii=False)
            return json.dumps({"success": True, "skill": dict(rows[0])}, ensure_ascii=False)

        # ----- SSH Channel（会话/任务边界，TTY 与行缓冲） -----
        if name == "ssh_channel_create":
            host_id = arguments.get("host_id")
            if host_id is None:
                return json.dumps({"success": False, "error": "缺少 host_id"}, ensure_ascii=False)
            owner_type, owner_id = resolve_channel_owner(
                owner_type=arguments.get("owner_type"),
                owner_id=arguments.get("owner_id"),
                terminal_scope_id=terminal_scope_id,
                session_id=session_id,
                task_id=task_id,
            )
            integration_ctx = bool(
                session_id is not None and not terminal_scope_id and owner_type == "session"
            )
            db = await get_db()
            try:
                channel = await create_channel_and_open(
                    db,
                    user,
                    host_id=int(host_id),
                    owner_type=owner_type,
                    owner_id=owner_id,
                    input_timeout_sec=arguments.get("input_timeout_sec"),
                    output_timeout_sec=arguments.get("output_timeout_sec"),
                    idle_close_sec=arguments.get("idle_close_sec"),
                    session_id=session_id,
                    terminal_scope_id=terminal_scope_id,
                    integration_context=integration_ctx,
                )
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            return json.dumps({"success": True, **channel}, ensure_ascii=False)

        if name == "ssh_channel_list":
            db = await get_db()
            if arguments.get("all_open"):
                channels = await list_channels_for_user(db, user, all_open=True)
            else:
                otype, oid = resolve_channel_owner(
                    owner_type=arguments.get("owner_type"),
                    owner_id=arguments.get("owner_id"),
                    terminal_scope_id=terminal_scope_id,
                    session_id=session_id,
                    task_id=task_id,
                )
                channels = await list_channels_for_user(
                    db, user, owner_type=otype, owner_id=oid, all_open=False
                )
            return json.dumps({"success": True, "channels": channels, "count": len(channels)}, ensure_ascii=False)

        if name == "ssh_channel_info":
            cid = arguments.get("channel_id")
            if cid is None:
                return json.dumps({"success": False, "error": "缺少 channel_id"}, ensure_ascii=False)
            db = await get_db()
            detail = await get_channel_detail(db, user, int(cid))
            if not detail:
                return json.dumps({"success": False, "error": "通道不存在"}, ensure_ascii=False)
            return json.dumps({"success": True, "channel": detail}, ensure_ascii=False)

        if name == "ssh_channel_get_status":
            cid = arguments.get("channel_id")
            if cid is None:
                return json.dumps({"success": False, "error": "缺少 channel_id"}, ensure_ascii=False)
            include_tail = 0
            if arguments.get("include_tail_lines") is not None:
                try:
                    include_tail = max(0, min(20, int(arguments.get("include_tail_lines"))))
                except (TypeError, ValueError):
                    include_tail = 0
            db = await get_db()
            row, st, err = await _ssh_channel_status_for_id(db, user, int(cid))
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            out = {
                "success": True,
                "channel_id": int(cid),
                "db_status": row.get("status"),
                **channel_session_status_payload(st),
            }
            _attach_channel_false_busy_hint(out, st or {})
            if include_tail > 0 and st and st.get("can_read_buffer"):
                tail_text = SSHChannelManager.get_instance().get_tail_text(int(cid), last_n=include_tail) or ""
                lines = tail_text.splitlines()
                out["buffer_tail_lines"] = lines[-include_tail:] if lines else []
            return json.dumps(out, ensure_ascii=False)

        if name == "ssh_channel_send":
            cid = arguments.get("channel_id")
            content = arguments.get("content") or ""
            if cid is None:
                return json.dumps({"success": False, "error": "缺少 channel_id"}, ensure_ascii=False)
            db = await get_db()
            await reconcile_channel_if_stale(db, user, int(cid))
            rows = await db.execute_fetchall("SELECT id FROM ssh_channels WHERE id = ? AND user_id = ? AND status = 'open'", (cid, user["id"]))
            if not rows:
                return json.dumps({"success": False, "error": "通道不存在或已关闭"}, ensure_ascii=False)
            _, st_before, _ = await _ssh_channel_status_for_id(db, user, int(cid))
            if st_before and not st_before.get("connected"):
                return json.dumps({
                    "success": False,
                    "error": (
                        f"SSH 通道已断开（connected=false，db_status={st_before.get('db_status')}，"
                        f"disconnect_reason={st_before.get('disconnect_reason') or 'unknown'}），"
                        "无法 ssh_channel_send；请 ssh_channel_create 新建通道。"
                    ),
                    **channel_session_status_payload(st_before),
                }, ensure_ascii=False)
            err = SSHChannelManager.get_instance().send(cid, content)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            _, st_after, _ = await _ssh_channel_status_for_id(db, user, int(cid))
            payload = {"success": True, "message": "已发送", **channel_session_status_payload(st_after)}
            _attach_channel_send_advisory(payload, st_after or {})
            return json.dumps(payload, ensure_ascii=False)

        if name == "ssh_channel_read_lines":
            cid = arguments.get("channel_id")
            if cid is None:
                return json.dumps({"success": False, "error": "缺少 channel_id"}, ensure_ascii=False)
            db = await get_db()
            await reconcile_channel_if_stale(db, user, int(cid))
            rows = await db.execute_fetchall("SELECT id FROM ssh_channels WHERE id = ? AND user_id = ?", (cid, user["id"]))
            if not rows:
                return json.dumps({"success": False, "error": "通道不存在"}, ensure_ascii=False)
            mgr = SSHChannelManager.get_instance()
            until_needle = normalize_until_contains(arguments.get("until_contains"))
            until_reason = None
            until_snippet = None
            if until_needle:
                async def _fetch_ch_tail():
                    t = mgr.get_tail_text(int(cid), last_n=200) or ""
                    p = mgr.get_pending_partial(int(cid)) or ""
                    return (t + ("\n" + p if p else "")), {}

                until_reason, until_snippet, _, _ = await poll_until_contains(
                    fetch_raw=_fetch_ch_tail,
                    needle=until_needle,
                    timeout_sec=clamp_until_wait_seconds(
                        arguments.get("wait_seconds"), default=30, max_sec=30
                    ),
                    session_id=session_id,
                    match_mode="full",
                )
            result = mgr.get_lines(
                cid,
                from_line=arguments.get("from_line"),
                to_line=arguments.get("to_line"),
                last_n=arguments.get("last_n"),
                since_line=arguments.get("since_line"),
            )
            if result is None:
                payload = {
                    "success": True,
                    "lines": [],
                    "oldest_line_no": 0,
                    "latest_line_no": 0,
                    "pending_partial": "",
                    "tail_text": "",
                }
            else:
                lines, oldest, latest = result
                last_n = arguments.get("last_n") or 30
                tail_text = mgr.get_tail_text(int(cid), last_n=last_n) or ""
                pending = mgr.get_pending_partial(int(cid)) or ""
                _, st, _ = await _ssh_channel_status_for_id(db, user, int(cid))
                payload = {
                    "success": True,
                    "lines": lines,
                    "oldest_line_no": oldest,
                    "latest_line_no": latest,
                    "pending_partial": pending,
                    "tail_text": tail_text,
                    **channel_session_status_payload(st),
                }
                if st:
                    _attach_channel_false_busy_hint(payload, st)
                if arguments.get("spill", True):
                    text = tail_text or format_lines_as_text(lines)
                    spill_info = maybe_spill_channel_text(user, session_id, int(cid), text, tool_suffix="read_lines")
                    if spill_info.get("spilled"):
                        payload["spill"] = spill_info
                        payload["text_preview"] = spill_info.get("preview", "")
                    else:
                        payload["text"] = spill_info.get("content", text)
            if until_needle:
                payload["until_contains"] = until_needle
                payload["until_wait_reason"] = until_reason or "timeout"
                payload["until_wait_done"] = True
                payload["wait_done_in_tool"] = True
                if until_snippet:
                    payload["until_matched_snippet"] = until_snippet
            else:
                attach_ssh_channel_wait_fields(payload, arguments)
            return json.dumps(payload, ensure_ascii=False)

        if name == "ssh_channel_read_length":
            cid = arguments.get("channel_id")
            max_chars = arguments.get("max_chars")
            if cid is None:
                return json.dumps({"success": False, "error": "缺少 channel_id"}, ensure_ascii=False)
            if max_chars is not None:
                try:
                    max_chars = max(1, min(1024 * 1024, int(max_chars)))
                except (TypeError, ValueError):
                    max_chars = 8192
            else:
                max_chars = 8192
            db = await get_db()
            await reconcile_channel_if_stale(db, user, int(cid))
            rows = await db.execute_fetchall("SELECT id FROM ssh_channels WHERE id = ? AND user_id = ?", (cid, user["id"]))
            if not rows:
                return json.dumps({"success": False, "error": "通道不存在"}, ensure_ascii=False)
            mgr = SSHChannelManager.get_instance()
            until_needle = normalize_until_contains(arguments.get("until_contains"))
            until_reason = None
            until_snippet = None
            if until_needle:
                async def _fetch_ch_len():
                    r = mgr.get_content_length(cid, max_chars)
                    body = (r[0] if r else "") or ""
                    p = mgr.get_pending_partial(int(cid)) or ""
                    return (body + ("\n" + p if p else "")), {}

                until_reason, until_snippet, _, _ = await poll_until_contains(
                    fetch_raw=_fetch_ch_len,
                    needle=until_needle,
                    timeout_sec=clamp_until_wait_seconds(
                        arguments.get("wait_seconds"), default=30, max_sec=30
                    ),
                    session_id=session_id,
                    match_mode="full",
                )
            result = mgr.get_content_length(cid, max_chars)
            if result is None:
                payload = {"success": True, "content": "", "length": 0}
            else:
                content_text, oldest, latest = result
                payload = {
                    "success": True,
                    "length": len(content_text),
                    "oldest_line_no": oldest,
                    "latest_line_no": latest,
                }
                spill_info = maybe_spill_channel_text(user, session_id, int(cid), content_text, tool_suffix="read_length")
                if spill_info.get("spilled"):
                    payload["spill"] = spill_info
                    payload["content_preview"] = spill_info.get("preview", "")
                else:
                    payload["content"] = spill_info.get("content", content_text)
            if until_needle:
                payload["until_contains"] = until_needle
                payload["until_wait_reason"] = until_reason or "timeout"
                payload["until_wait_done"] = True
                payload["wait_done_in_tool"] = True
                if until_snippet:
                    payload["until_matched_snippet"] = until_snippet
            else:
                attach_ssh_channel_wait_fields(payload, arguments)
            return json.dumps(payload, ensure_ascii=False)

        if name == "ssh_channel_has_new":
            cid = arguments.get("channel_id")
            after_line = arguments.get("after_line", 0)
            if cid is None:
                return json.dumps({"success": False, "error": "缺少 channel_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT id FROM ssh_channels WHERE id = ? AND user_id = ?", (cid, user["id"]))
            if not rows:
                return json.dumps({"success": False, "error": "通道不存在"}, ensure_ascii=False)
            mgr = SSHChannelManager.get_instance()
            until_needle = normalize_until_contains(arguments.get("until_contains"))
            until_reason = None
            until_snippet = None
            if until_needle:
                async def _fetch_ch_new():
                    t = mgr.get_tail_text(int(cid), last_n=200) or ""
                    p = mgr.get_pending_partial(int(cid)) or ""
                    return (t + ("\n" + p if p else "")), {}

                until_reason, until_snippet, _, _ = await poll_until_contains(
                    fetch_raw=_fetch_ch_new,
                    needle=until_needle,
                    timeout_sec=clamp_until_wait_seconds(
                        arguments.get("wait_seconds"), default=30, max_sec=30
                    ),
                    session_id=session_id,
                    match_mode="full",
                )
            result = mgr.has_new(cid, after_line)
            if result is None:
                payload = {
                    "success": True,
                    "has_new": False,
                    "latest_line_no": 0,
                    "pending_partial": "",
                }
            else:
                has_new_val, latest, pending = result
                payload = {
                    "success": True,
                    "has_new": has_new_val,
                    "latest_line_no": latest,
                    "pending_partial": pending or "",
                }
            if until_needle:
                payload["until_contains"] = until_needle
                payload["until_wait_reason"] = until_reason or "timeout"
                payload["until_wait_done"] = True
                payload["wait_done_in_tool"] = True
                if until_snippet:
                    payload["until_matched_snippet"] = until_snippet
                # until 命中说明相关输出已出现；勿因 after_line 未推进而误报无新输出
                if until_reason == "matched":
                    payload["has_new"] = True
            else:
                attach_ssh_channel_wait_fields(payload, arguments)
            return json.dumps(payload, ensure_ascii=False)

        if name == "ssh_channel_close":
            cid = arguments.get("channel_id")
            if cid is None:
                return json.dumps({"success": False, "error": "缺少 channel_id"}, ensure_ascii=False)
            db = await get_db()
            ok = await close_channel_full(db, user, int(cid))
            if not ok:
                return json.dumps({"success": False, "error": "通道不存在"}, ensure_ascii=False)
            return json.dumps({"success": True, "message": "通道已关闭"}, ensure_ascii=False)

        if name == "ssh_channel_close_batch":
            db = await get_db()
            otype, oid = resolve_channel_owner(
                owner_type=arguments.get("owner_type"),
                owner_id=arguments.get("owner_id"),
                terminal_scope_id=terminal_scope_id,
                session_id=session_id,
                task_id=task_id,
            )
            result = await close_channels_by_owner(db, user, owner_type=otype, owner_id=oid)
            return json.dumps({"success": True, **result}, ensure_ascii=False)

        if name == "ssh_channel_dump_output":
            cid = arguments.get("channel_id")
            if cid is None:
                return json.dumps({"success": False, "error": "缺少 channel_id"}, ensure_ascii=False)
            db = await get_db()
            try:
                result = await dump_channel_buffer_to_file(
                    db,
                    user,
                    int(cid),
                    session_id=session_id,
                    max_chars=int(arguments.get("max_chars") or 2_000_000),
                )
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            return json.dumps({"success": True, **result}, ensure_ascii=False)

        # ----- 触发任务 / 定时任务（供 AI 查询与触发） -----
        if name == "triggered_task_list":
            db = await get_db()
            rows = await db.execute_fetchall(
                """SELECT id, name, content, intro, trigger_conditions, created_at, updated_at, last_run_at, last_run_status, is_running
                   FROM triggered_tasks WHERE user_id = ? ORDER BY updated_at DESC""",
                (user["id"],),
            )
            tasks = [dict(r) for r in rows]
            return json.dumps({"success": True, "tasks": tasks}, ensure_ascii=False)

        if name == "triggered_task_list_exposed":
            db = await get_db()
            rows = await db.execute_fetchall(
                """SELECT t.id, t.name, t.intro, e.expose_code, e.description
                   FROM triggered_tasks t
                   LEFT JOIN triggered_task_expose e ON e.task_id = t.id
                   WHERE t.user_id = ? ORDER BY t.name""",
                (user["id"],),
            )
            by_task = {}
            for r in rows:
                r = dict(r)
                tid = r["id"]
                if tid not in by_task:
                    by_task[tid] = {"id": r["id"], "name": r["name"], "intro": r.get("intro") or "", "expose": []}
                if r.get("expose_code"):
                    by_task[tid]["expose"].append({"code": r["expose_code"], "description": r.get("description") or ""})
            return json.dumps({"success": True, "tasks": list(by_task.values())}, ensure_ascii=False)

        if name == "triggered_task_status":
            db = await get_db()
            task_id = arguments.get("task_id")
            task_name = (arguments.get("task_name") or "").strip()
            if task_id is not None:
                rows = await db.execute_fetchall(
                    """SELECT id, name, intro, trigger_conditions, last_run_at, last_run_status, is_running
                       FROM triggered_tasks WHERE id = ? AND user_id = ?""",
                    (task_id, user["id"]),
                )
            elif task_name:
                rows = await db.execute_fetchall(
                    """SELECT id, name, intro, trigger_conditions, last_run_at, last_run_status, is_running
                       FROM triggered_tasks WHERE name = ? AND user_id = ?""",
                    (task_name, user["id"]),
                )
            else:
                rows = await db.execute_fetchall(
                    """SELECT id, name, intro, trigger_conditions, last_run_at, last_run_status, is_running
                       FROM triggered_tasks WHERE user_id = ? ORDER BY id""",
                    (user["id"],),
                )
            tasks = [dict(r) for r in rows]
            return json.dumps({"success": True, "tasks": tasks}, ensure_ascii=False)

        if name == "triggered_task_get":
            task_id = arguments.get("task_id")
            if task_id is None:
                return json.dumps({"success": False, "error": "缺少 task_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT * FROM triggered_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
            if not rows:
                return json.dumps({"success": False, "error": "触发任务不存在"}, ensure_ascii=False)
            return json.dumps({"success": True, "task": dict(rows[0])}, ensure_ascii=False)

        if name == "triggered_task_create":
            name_val = (arguments.get("name") or "").strip()
            content_val = (arguments.get("content") or "").strip()
            if not name_val:
                return json.dumps({"success": False, "error": "任务名不能为空"}, ensure_ascii=False)
            if not content_val:
                return json.dumps({"success": False, "error": "任务内容不能为空"}, ensure_ascii=False)
            intro = (arguments.get("intro") or "").strip()
            trigger_conditions = (arguments.get("trigger_conditions") or "").strip()
            db = await get_db()
            await db.execute(
                """INSERT INTO triggered_tasks (user_id, name, content, intro, trigger_conditions)
                   VALUES (?, ?, ?, ?, ?)""",
                (user["id"], name_val, content_val, intro, trigger_conditions),
            )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            tid = (await cur.fetchone())[0]
            return json.dumps({"success": True, "task_id": tid, "message": "已创建"}, ensure_ascii=False)

        if name == "triggered_task_update":
            task_id = arguments.get("task_id")
            if task_id is None:
                return json.dumps({"success": False, "error": "缺少 task_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT id FROM triggered_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
            if not rows:
                return json.dumps({"success": False, "error": "触发任务不存在"}, ensure_ascii=False)
            updates = []
            params = []
            if arguments.get("name") is not None:
                updates.append("name = ?")
                params.append((arguments.get("name") or "").strip())
            if arguments.get("content") is not None:
                updates.append("content = ?")
                params.append((arguments.get("content") or "").strip())
            if arguments.get("intro") is not None:
                updates.append("intro = ?")
                params.append((arguments.get("intro") or "").strip())
            if arguments.get("trigger_conditions") is not None:
                updates.append("trigger_conditions = ?")
                params.append((arguments.get("trigger_conditions") or "").strip())
            if not updates:
                return json.dumps({"success": True, "message": "无字段更新"}, ensure_ascii=False)
            params.extend([task_id, user["id"]])
            await db.execute(
                "UPDATE triggered_tasks SET " + ", ".join(updates) + ", updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                params,
            )
            await db.commit()
            return json.dumps({"success": True, "message": "已更新"}, ensure_ascii=False)

        if name == "triggered_task_delete":
            task_id = arguments.get("task_id")
            task_name = (arguments.get("task_name") or "").strip()
            if task_id is None and not task_name:
                return json.dumps({"success": False, "error": "请提供 task_id 或 task_name"}, ensure_ascii=False)
            db = await get_db()
            if task_id is not None:
                rows = await db.execute_fetchall("SELECT id FROM triggered_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
            else:
                rows = await db.execute_fetchall("SELECT id FROM triggered_tasks WHERE name = ? AND user_id = ?", (task_name, user["id"]))
            if not rows:
                return json.dumps({"success": False, "error": "触发任务不存在"}, ensure_ascii=False)
            tid = rows[0]["id"]
            await db.execute("DELETE FROM triggered_tasks WHERE id = ? AND user_id = ?", (tid, user["id"]))
            await db.commit()
            return json.dumps({"success": True, "message": "已删除"}, ensure_ascii=False)

        if name == "triggered_task_trigger":
            task_id = arguments.get("task_id")
            task_name = arguments.get("task_name")
            instruction = arguments.get("instruction") or ""
            caller_task_id = arguments.get("caller_task_id")
            caller_task_name = arguments.get("caller_task_name")
            caller_status = arguments.get("caller_status")
            if not task_id and not task_name:
                return json.dumps({"success": False, "error": "请提供 task_id 或 task_name"}, ensure_ascii=False)
            db = await get_db()
            if task_id:
                rows = await db.execute_fetchall("SELECT id FROM triggered_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
            else:
                rows = await db.execute_fetchall("SELECT id FROM triggered_tasks WHERE name = ? AND user_id = ?", (task_name, user["id"]))
            if not rows:
                return json.dumps({"success": False, "error": "触发任务不存在"}, ensure_ascii=False)
            tid = rows[0]["id"]
            await db.execute(
                """INSERT INTO triggered_task_runs (task_id, triggered_by_type, triggered_by_id, caller_task_name, status, instruction)
                   VALUES (?, 'api', ?, ?, 'pending', ?)""",
                (tid, caller_task_id or "", caller_task_name or "", instruction),
            )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            run_id = (await cur.fetchone())[0]
            await db.execute(
                "UPDATE triggered_tasks SET last_run_at = CURRENT_TIMESTAMP, last_run_status = 'pending', is_running = 1 WHERE id = ?",
                (tid,),
            )
            await db.commit()
            from services.task_runner import run_triggered_task
            asyncio.create_task(run_triggered_task(run_id))
            return json.dumps({"success": True, "task_id": tid, "run_id": run_id, "message": "已加入执行队列"}, ensure_ascii=False)

        if name == "triggered_task_current_run_history":
            task_id = arguments.get("task_id")
            run_id = arguments.get("run_id")
            if task_id is None:
                return json.dumps({"success": False, "error": "缺少 task_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT id FROM triggered_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
            if not rows:
                return json.dumps({"success": False, "error": "触发任务不存在"}, ensure_ascii=False)
            if run_id is not None:
                run_rows = await db.execute_fetchall(
                    "SELECT id, task_id, triggered_at, status FROM triggered_task_runs WHERE id = ? AND task_id = ?", (run_id, task_id)
                )
            else:
                run_rows = await db.execute_fetchall(
                    "SELECT id, task_id, triggered_at, status FROM triggered_task_runs WHERE task_id = ? ORDER BY triggered_at DESC LIMIT 1", (task_id,)
                )
            if not run_rows:
                return json.dumps({"success": True, "run": None, "messages": [], "message": "尚无执行历史"}, ensure_ascii=False)
            run_row = dict(run_rows[0])
            msg_rows = await db.execute_fetchall(
                """SELECT role, content, created_at FROM triggered_task_run_messages WHERE run_id = ? ORDER BY id ASC LIMIT 500""",
                (run_row["id"],),
            )
            messages = [{"role": r["role"], "content": (r["content"] or "").strip(), "created_at": r["created_at"] or ""} for r in msg_rows]
            return json.dumps({"success": True, "run": run_row, "messages": messages}, ensure_ascii=False)

        if name == "scheduled_task_list":
            db = await get_db()
            rows = await db.execute_fetchall(
                """SELECT id, name, content, cron_expr, next_run_at, created_at, updated_at, last_run_at, last_run_status, is_running, enabled, notify_email_to
                   FROM scheduled_tasks WHERE user_id = ? ORDER BY updated_at DESC""",
                (user["id"],),
            )
            return json.dumps(
                {"success": True, "tasks": [_scheduled_task_dict_for_tool(r) for r in rows]},
                ensure_ascii=False,
            )

        if name == "scheduled_task_status":
            db = await get_db()
            task_id = arguments.get("task_id")
            task_name = (arguments.get("task_name") or "").strip()
            if task_id is not None:
                rows = await db.execute_fetchall(
                    """SELECT id, name, is_running, last_run_at, last_run_status, next_run_at, enabled
                       FROM scheduled_tasks WHERE id = ? AND user_id = ?""",
                    (task_id, user["id"]),
                )
            elif task_name:
                rows = await db.execute_fetchall(
                    """SELECT id, name, is_running, last_run_at, last_run_status, next_run_at, enabled
                       FROM scheduled_tasks WHERE name = ? AND user_id = ?""",
                    (task_name, user["id"]),
                )
            else:
                rows = await db.execute_fetchall(
                    """SELECT id, name, is_running, last_run_at, last_run_status, next_run_at, enabled
                       FROM scheduled_tasks WHERE user_id = ? ORDER BY id""",
                    (user["id"],),
                )
            tasks = [dict(r) for r in rows]
            return json.dumps({"success": True, "tasks": tasks}, ensure_ascii=False)

        if name == "scheduled_task_get":
            task_id = arguments.get("task_id")
            if task_id is None:
                return json.dumps({"success": False, "error": "缺少 task_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT * FROM scheduled_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
            if not rows:
                return json.dumps({"success": False, "error": "定时任务不存在"}, ensure_ascii=False)
            return json.dumps({"success": True, "task": _scheduled_task_dict_for_tool(rows[0])}, ensure_ascii=False)

        if name == "scheduled_task_create":
            name_val = (arguments.get("name") or "").strip()
            content_val = (arguments.get("content") or "").strip()
            if not name_val:
                return json.dumps({"success": False, "error": "任务名不能为空"}, ensure_ascii=False)
            if not content_val:
                return json.dumps({"success": False, "error": "任务内容不能为空"}, ensure_ascii=False)
            cron_expr = (arguments.get("cron_expr") or "").strip()
            db = await get_db()
            from services.scheduler import _next_run_from_cron
            next_run = _next_run_from_cron(cron_expr)
            next_run_str = next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else None
            en = 1 if arguments.get("enabled", True) else 0
            if not en:
                next_run_str = None
            notify_to = (arguments.get("notify_email_to") or "").strip()
            await db.execute(
                """INSERT INTO scheduled_tasks (user_id, name, content, cron_expr, next_run_at, enabled, notify_email_to)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user["id"], name_val, content_val, cron_expr, next_run_str, en, notify_to),
            )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            task_id = (await cur.fetchone())[0]
            return json.dumps({"success": True, "task_id": task_id, "message": "已创建"}, ensure_ascii=False)

        if name == "scheduled_task_update":
            task_id = arguments.get("task_id")
            if task_id is None:
                return json.dumps({"success": False, "error": "缺少 task_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT id FROM scheduled_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
            if not rows:
                return json.dumps({"success": False, "error": "定时任务不存在"}, ensure_ascii=False)
            updates = []
            params = []
            if arguments.get("name") is not None:
                updates.append("name = ?")
                params.append((arguments.get("name") or "").strip())
            if arguments.get("content") is not None:
                updates.append("content = ?")
                params.append((arguments.get("content") or "").strip())
            cron_updated_next = False
            if arguments.get("cron_expr") is not None:
                cron_expr = (arguments.get("cron_expr") or "").strip()
                updates.append("cron_expr = ?")
                params.append(cron_expr)
                from services.scheduler import _next_run_from_cron
                next_run = _next_run_from_cron(cron_expr)
                next_run_str = next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else None
                updates.append("next_run_at = ?")
                params.append(next_run_str)
                cron_updated_next = True
            if arguments.get("enabled") is not None:
                en = bool(arguments.get("enabled"))
                updates.append("enabled = ?")
                params.append(1 if en else 0)
                if en and not cron_updated_next:
                    crow = await db.execute_fetchall(
                        "SELECT cron_expr FROM scheduled_tasks WHERE id = ? AND user_id = ?",
                        (task_id, user["id"]),
                    )
                    if crow:
                        ce = (crow[0]["cron_expr"] or "").strip()
                        from services.scheduler import _next_run_from_cron
                        nr = _next_run_from_cron(ce) if ce else None
                        updates.append("next_run_at = ?")
                        params.append(nr.strftime("%Y-%m-%d %H:%M:%S") if nr else None)
            if arguments.get("notify_email_to") is not None:
                updates.append("notify_email_to = ?")
                params.append((arguments.get("notify_email_to") or "").strip())
            if not updates:
                return json.dumps({"success": True, "message": "无字段更新"}, ensure_ascii=False)
            params.extend([task_id, user["id"]])
            await db.execute(
                "UPDATE scheduled_tasks SET " + ", ".join(updates) + ", updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                params,
            )
            await db.commit()
            return json.dumps({"success": True, "message": "已更新"}, ensure_ascii=False)

        if name == "scheduled_task_delete":
            task_id = arguments.get("task_id")
            task_name = (arguments.get("task_name") or "").strip()
            if task_id is None and not task_name:
                return json.dumps({"success": False, "error": "请提供 task_id 或 task_name"}, ensure_ascii=False)
            db = await get_db()
            if task_id is not None:
                rows = await db.execute_fetchall("SELECT id FROM scheduled_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
            else:
                rows = await db.execute_fetchall("SELECT id FROM scheduled_tasks WHERE name = ? AND user_id = ?", (task_name, user["id"]))
            if not rows:
                return json.dumps({"success": False, "error": "定时任务不存在"}, ensure_ascii=False)
            tid = rows[0]["id"]
            await db.execute(
                "DELETE FROM scheduled_task_run_messages WHERE run_id IN (SELECT id FROM scheduled_task_runs WHERE task_id = ?)",
                (tid,),
            )
            await db.execute("DELETE FROM scheduled_task_runs WHERE task_id = ?", (tid,))
            await db.execute("DELETE FROM scheduled_tasks WHERE id = ? AND user_id = ?", (tid, user["id"]))
            await db.commit()
            return json.dumps({"success": True, "message": "已删除（含执行历史）"}, ensure_ascii=False)

        if name == "scheduled_task_current_run_history":
            task_id = arguments.get("task_id")
            run_id = arguments.get("run_id")
            if task_id is None:
                return json.dumps({"success": False, "error": "缺少 task_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT id FROM scheduled_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
            if not rows:
                return json.dumps({"success": False, "error": "定时任务不存在"}, ensure_ascii=False)
            if run_id is not None:
                run_rows = await db.execute_fetchall(
                    "SELECT id, task_id, run_at, status FROM scheduled_task_runs WHERE id = ? AND task_id = ?", (run_id, task_id)
                )
            else:
                run_rows = await db.execute_fetchall(
                    "SELECT id, task_id, run_at, status FROM scheduled_task_runs WHERE task_id = ? ORDER BY run_at DESC LIMIT 1", (task_id,)
                )
            if not run_rows:
                return json.dumps({"success": True, "run": None, "messages": [], "message": "尚无执行历史"}, ensure_ascii=False)
            run_row = dict(run_rows[0])
            msg_rows = await db.execute_fetchall(
                """SELECT role, content, created_at FROM scheduled_task_run_messages WHERE run_id = ? ORDER BY id ASC LIMIT 500""",
                (run_row["id"],),
            )
            messages = [{"role": r["role"], "content": (r["content"] or "").strip(), "created_at": r["created_at"] or ""} for r in msg_rows]
            return json.dumps({"success": True, "run": run_row, "messages": messages}, ensure_ascii=False)

        if name == "scheduled_task_run_now":
            task_id = arguments.get("task_id")
            task_name = arguments.get("task_name")
            if task_id is None and not (task_name and str(task_name).strip()):
                return json.dumps({"success": False, "error": "请提供 task_id 或 task_name"}, ensure_ascii=False)
            db = await get_db()
            if task_id is not None:
                rows = await db.execute_fetchall("SELECT id, name FROM scheduled_tasks WHERE id = ? AND user_id = ?", (task_id, user["id"]))
            else:
                rows = await db.execute_fetchall("SELECT id, name FROM scheduled_tasks WHERE name = ? AND user_id = ?", (str(task_name).strip(), user["id"]))
            if not rows:
                return json.dumps({"success": False, "error": "定时任务不存在"}, ensure_ascii=False)
            tid = rows[0]["id"]
            await db.execute(
                "INSERT INTO scheduled_task_runs (task_id, run_at, status) VALUES (?, datetime('now'), 'running')",
                (tid,),
            )
            await db.execute("UPDATE scheduled_tasks SET is_running = 1 WHERE id = ?", (tid,))
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            run_id = (await cur.fetchone())[0]
            from services.task_runner import run_scheduled_task
            asyncio.create_task(run_scheduled_task(run_id))
            return json.dumps({"success": True, "task_id": tid, "run_id": run_id, "message": "已加入执行队列"}, ensure_ascii=False)

        if name == "get_terminal_buffer":
            slot = arguments.get("slot")
            full_output = arguments.get("full_output") is True
            tail_only = arguments.get("tail_only")
            if tail_only is None:
                tail_only = True
            else:
                tail_only = bool(tail_only)
            max_lines = 40
            if arguments.get("max_lines") is not None:
                try:
                    max_lines = max(10, min(200, int(arguments.get("max_lines"))))
                except (TypeError, ValueError):
                    max_lines = 40
            next_poll = arguments.get("next_poll_in_seconds")
            if next_poll is not None:
                try:
                    next_poll = max(1, min(3600, int(next_poll)))
                except (TypeError, ValueError):
                    next_poll = None
            until_needle = normalize_until_contains(arguments.get("until_contains"))
            is_local = (scope or "").strip().lower() == "local"

            if is_local:
                from api import local_host

                if slot is not None:
                    try:
                        slot = int(slot)
                        slot = max(0, min(slot, 31))
                    except (TypeError, ValueError):
                        slot = None
                slot, slot_err = resolve_local_slot(slot)
                if slot_err:
                    return json.dumps({"success": False, "error": slot_err, "terminal_scope_id": terminal_scope_id}, ensure_ascii=False)

                async def _fetch_term():
                    b, c = local_host.get_local_terminal_buffer(user["id"], slot, terminal_scope_id)
                    return b or "", {"connected": c}

                buf, connected = local_host.get_local_terminal_buffer(user["id"], slot, terminal_scope_id)
                if not connected:
                    await local_host.wait_for_local_terminal_ready(user["id"], slot, terminal_scope_id)
                    buf, connected = local_host.get_local_terminal_buffer(user["id"], slot, terminal_scope_id)
            else:
                slot, slot_err = resolve_ai_slot(slot, arguments.get("host_id"))
                if slot_err:
                    return json.dumps(attach_terminals_snapshot({"success": False, "error": slot_err}), ensure_ascii=False)

                async def _fetch_term():
                    b, c = get_terminal_buffer_for_user(user["id"], slot, scope_id=terminal_scope_id)
                    return b or "", {"connected": c}

                buf, connected = get_terminal_buffer_for_user(user["id"], slot, scope_id=terminal_scope_id)
                if not connected:
                    await wait_for_terminal_session_ready(user["id"], slot, terminal_scope_id)
                    buf, connected = get_terminal_buffer_for_user(user["id"], slot, scope_id=terminal_scope_id)

            until_reason = None
            until_snippet = None
            if until_needle:
                timeout_sec = clamp_until_wait_seconds(
                    next_poll, default=30, max_sec=3600
                )
                until_reason, until_snippet, buf, meta_u = await poll_until_contains(
                    fetch_raw=_fetch_term,
                    needle=until_needle,
                    timeout_sec=timeout_sec,
                    session_id=session_id,
                )
                connected = bool((meta_u or {}).get("connected", connected))

            abbreviated = False
            total_lines = 0
            abbrev_note = ""
            if buf:
                buf, abbreviated, total_lines, abbrev_note = abbreviate_terminal_buffer(
                    buf,
                    full_output=full_output,
                    tail_only=tail_only,
                    max_lines=max_lines,
                )
            if is_local:
                st = local_host.get_local_terminal_session_state(user["id"], slot, terminal_scope_id)
                out = {
                    "success": True,
                    "buffer": buf,
                    "connected": connected,
                    "slot": slot,
                    "terminal_scope_id": terminal_scope_id,
                    **_terminal_status_payload(st),
                }
            else:
                st = get_terminal_session_state(user["id"], slot, terminal_scope_id)
                out = attach_terminals_snapshot(attach_terminal_host_fields({
                    "success": True,
                    "buffer": buf,
                    "connected": connected,
                    "slot": slot,
                    **_terminal_status_payload(st),
                }, slot))
            _attach_false_busy_hint(out, st)
            if abbreviated:
                out["abbreviated"] = True
                out["total_lines"] = total_lines
                out["tail_only"] = tail_only
                out["max_lines"] = max_lines
                out["abbreviation_note"] = abbrev_note
            if until_needle:
                out["until_contains"] = until_needle
                out["until_wait_reason"] = until_reason or "timeout"
                out["until_wait_done"] = True
                out["wait_done_in_tool"] = True
                if until_snippet:
                    out["until_matched_snippet"] = until_snippet
            elif next_poll is not None:
                out["next_poll_in_seconds"] = next_poll
            return json.dumps(out, ensure_ascii=False)

        if name == "build_scp_transfer_script":
            source_host_id = arguments.get("source_host_id")
            target_host_id = arguments.get("target_host_id")
            source_path = (arguments.get("source_path") or "").strip()
            target_path = (arguments.get("target_path") or "").strip()
            compress = True if arguments.get("compress") is None else bool(arguments.get("compress"))
            if not source_host_id or not target_host_id or not source_path or not target_path:
                return json.dumps({"success": False, "error": "需要 source_host_id/source_path/target_host_id/target_path"}, ensure_ascii=False)
            source_row = await _get_host_row(source_host_id)
            target_row = await _get_host_row(target_host_id)
            if not source_row or not target_row:
                return json.dumps({"success": False, "error": "源或目标主机不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(source_row, user):
                return json.dumps({"success": False, "error": "无权访问源主机"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(target_row, user):
                return json.dumps({"success": False, "error": "无权访问目标主机"}, ensure_ascii=False)
            auth = await _resolve_host_auth(await get_db(), target_row)
            if not auth or not (auth.get("username") or "").strip():
                return json.dumps({"success": False, "error": "目标主机凭证不可用"}, ensure_ascii=False)
            dst_user = (auth.get("username") or "").strip()
            dst_host = (target_row.get("host") or "").strip()
            dst_port = int(target_row.get("port") or 22)
            c_flag = "-C " if compress else ""
            if (auth.get("auth_type") or "").strip() == "password":
                pw = auth.get("password") or ""
                script = (
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    "if ! command -v sshpass >/dev/null 2>&1; then\n"
                    "  echo \"需要先安装 sshpass（例如 apt/yum install sshpass）\" >&2\n"
                    "  exit 1\n"
                    "fi\n"
                    f"export SSHPASS={shlex.quote(pw)}\n"
                    f"scp {c_flag}-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -P {dst_port} -- "
                    f"{shlex.quote(source_path)} {shlex.quote(dst_user)}@{shlex.quote(dst_host)}:{shlex.quote(target_path)}\n"
                    "unset SSHPASS\n"
                )
                return json.dumps(
                    {
                        "success": True,
                        "mode": "direct_scp_password",
                        "script": script,
                        "source_host_id": source_host_id,
                        "target_host_id": target_host_id,
                        "note": "脚本在源主机上执行，使用 sshpass + scp -C 直连推送。",
                    },
                    ensure_ascii=False,
                )
            key_pem = (auth.get("private_key_pem") or "").strip()
            if not key_pem:
                return json.dumps({"success": False, "error": "目标主机为密钥认证但私钥内容为空"}, ensure_ascii=False)
            script = (
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "TMP_KEY=$(mktemp /tmp/edgeops-scp-XXXXXX.key)\n"
                "cleanup(){ rm -f \"$TMP_KEY\"; }\n"
                "trap cleanup EXIT\n"
                "cat > \"$TMP_KEY\" <<'__EDGEOPS_KEY__'\n"
                f"{key_pem}\n"
                "__EDGEOPS_KEY__\n"
                "chmod 600 \"$TMP_KEY\"\n"
                f"scp {c_flag}-i \"$TMP_KEY\" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -P {dst_port} -- "
                f"{shlex.quote(source_path)} {shlex.quote(dst_user)}@{shlex.quote(dst_host)}:{shlex.quote(target_path)}\n"
            )
            return json.dumps(
                {
                    "success": True,
                    "mode": "direct_scp_key",
                    "script": script,
                    "source_host_id": source_host_id,
                    "target_host_id": target_host_id,
                    "note": "脚本在源主机上执行，临时落地私钥并使用 scp -C 推送，结束会自动删除临时私钥。",
                },
                ensure_ascii=False,
            )

        if name == "transfer_file_between_hosts":
            source_host_id = arguments.get("source_host_id")
            target_host_id = arguments.get("target_host_id")
            source_path = (arguments.get("source_path") or "").strip()
            target_path = (arguments.get("target_path") or "").strip()
            methods_raw = arguments.get("methods")
            timeout_seconds = max(60, min(3600, int(arguments.get("transfer_timeout_seconds") or 600)))
            if not source_host_id or not target_host_id or not source_path or not target_path:
                return json.dumps({"success": False, "error": "需要 source_host_id/source_path/target_host_id/target_path"}, ensure_ascii=False)
            source_row = await _get_host_row(source_host_id)
            target_row = await _get_host_row(target_host_id)
            if not source_row or not target_row:
                return json.dumps({"success": False, "error": "源或目标主机不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(source_row, user):
                return json.dumps({"success": False, "error": "无权访问源主机"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(target_row, user):
                return json.dumps({"success": False, "error": "无权访问目标主机"}, ensure_ascii=False)
            db = await get_db()
            source_auth = await _resolve_host_auth(db, source_row)
            target_auth = await _resolve_host_auth(db, target_row)
            if not source_auth or not (source_auth.get("username") or "").strip():
                return json.dumps({"success": False, "error": "源主机凭证不可用"}, ensure_ascii=False)
            if not target_auth or not (target_auth.get("username") or "").strip():
                return json.dumps({"success": False, "error": "目标主机凭证不可用"}, ensure_ascii=False)
            await _log_credential_usage_audit(
                actor_user_id=user["id"],
                host_row=source_row,
                auth=source_auth,
                operation="transfer_file_between_hosts",
                stage="source_prepare",
                extra={"target_host_id": target_host_id},
            )
            await _log_credential_usage_audit(
                actor_user_id=user["id"],
                host_row=target_row,
                auth=target_auth,
                operation="transfer_file_between_hosts",
                stage="target_prepare",
                extra={"source_host_id": source_host_id},
            )

            a_to_b_ok, a2b_out, a2b_err, a2b_code = await _probe_tcp22_from_host(
                source_row, source_auth, (target_row.get("host") or "").strip()
            )
            b_to_a_ok, b2a_out, b2a_err, b2a_code = await _probe_tcp22_from_host(
                target_row, target_auth, (source_row.get("host") or "").strip()
            )

            if a_to_b_ok:
                active_row, active_auth = source_row, source_auth
                passive_row, passive_auth = target_row, target_auth
                mode = "push"
                local_path = source_path
                remote_path = target_path
                connectivity_decision = "A_to_B"
            elif b_to_a_ok:
                active_row, active_auth = target_row, target_auth
                passive_row, passive_auth = source_row, source_auth
                mode = "pull"
                local_path = target_path
                remote_path = source_path
                connectivity_decision = "B_to_A"
            else:
                relay_payload = {
                    "source_host_id": source_host_id,
                    "source_path": source_path,
                    "target_host_id": target_host_id,
                    "target_path": target_path,
                    "edgeops_base_url": (arguments.get("edgeops_base_url") or "").strip(),
                    "ttl_seconds": int(arguments.get("ttl_seconds") or 600),
                    "keep_staging_for_multi_target": bool(arguments.get("keep_staging_for_multi_target") or False),
                    "auto_unpack_on_target": True if arguments.get("auto_unpack_on_target") is None else bool(arguments.get("auto_unpack_on_target")),
                }
                relay_raw = await execute_tool(
                    "relay_file_between_hosts",
                    relay_payload,
                    user,
                    scope=scope,
                    terminal_scope_id=terminal_scope_id,
                    default_terminal_slot=default_terminal_slot,
                    task_id=task_id,
                    ui_locale=ui_locale,
                    stream_callback=stream_callback,
                    transfer_cancel_event=transfer_cancel_event,
                )
                try:
                    relay_obj = json.loads(relay_raw)
                except Exception:
                    relay_obj = {"success": False, "error": relay_raw}
                return json.dumps(
                    {
                        "success": bool(relay_obj.get("success")),
                        "method": "relay",
                        "reason": "A/B 双向 22 端口不可达，已自动回退中转",
                        "connectivity": {
                            "a_to_b_22": a_to_b_ok,
                            "b_to_a_22": b_to_a_ok,
                            "a_to_b_probe_exit_code": a2b_code,
                            "b_to_a_probe_exit_code": b2a_code,
                        },
                        "relay_result": relay_obj,
                    },
                    ensure_ascii=False,
                )

            if isinstance(methods_raw, list) and methods_raw:
                methods = [str(x).strip().lower() for x in methods_raw if str(x).strip().lower() in ("scp", "rsync", "sshfs")]
                if not methods:
                    methods = ["scp", "rsync", "sshfs"]
            else:
                methods = ["scp", "rsync", "sshfs"]

            attempts = []
            for method in methods:
                cmd = _build_direct_transfer_command(
                    method=method,
                    mode=mode,
                    local_path=local_path,
                    remote_path=remote_path,
                    remote_host=(passive_row.get("host") or "").strip(),
                    remote_port=int(passive_row.get("port") or 22),
                    remote_user=(passive_auth.get("username") or "").strip(),
                    remote_auth=passive_auth,
                )
                if not cmd.strip():
                    attempts.append({"method": method, "success": False, "error": "不支持的方法"})
                    continue
                out, err, code = await run_ssh_command(
                    host=active_row["host"],
                    port=int(active_row.get("port") or 22),
                    username=active_auth.get("username") or "",
                    auth_type=active_auth.get("auth_type") or "password",
                    password=active_auth.get("password"),
                    key_path=active_auth.get("key_path"),
                    private_key_pem=active_auth.get("private_key_pem"),
                    command=cmd,
                    timeout=timeout_seconds,
                )
                ok = int(code or 1) == 0
                attempts.append(
                    {
                        "method": method,
                        "success": ok,
                        "exit_code": int(code or 1),
                        "stdout": out,
                        "stderr": err,
                    }
                )
                if ok:
                    return json.dumps(
                        {
                            "success": True,
                            "method": method,
                            "mode": mode,
                            "connectivity_decision": connectivity_decision,
                            "active_host_id": active_row.get("id"),
                            "passive_host_id": passive_row.get("id"),
                            "source_host_id": source_host_id,
                            "target_host_id": target_host_id,
                            "source_path": source_path,
                            "target_path": target_path,
                            "connectivity": {
                                "a_to_b_22": a_to_b_ok,
                                "b_to_a_22": b_to_a_ok,
                                "a_to_b_probe_exit_code": a2b_code,
                                "b_to_a_probe_exit_code": b2a_code,
                            },
                            "attempts": attempts,
                        },
                        ensure_ascii=False,
                    )

            relay_payload = {
                "source_host_id": source_host_id,
                "source_path": source_path,
                "target_host_id": target_host_id,
                "target_path": target_path,
                "edgeops_base_url": (arguments.get("edgeops_base_url") or "").strip(),
                "ttl_seconds": int(arguments.get("ttl_seconds") or 600),
                "keep_staging_for_multi_target": bool(arguments.get("keep_staging_for_multi_target") or False),
                "auto_unpack_on_target": True if arguments.get("auto_unpack_on_target") is None else bool(arguments.get("auto_unpack_on_target")),
            }
            relay_raw = await execute_tool(
                "relay_file_between_hosts",
                relay_payload,
                user,
                scope=scope,
                terminal_scope_id=terminal_scope_id,
                default_terminal_slot=default_terminal_slot,
                task_id=task_id,
                ui_locale=ui_locale,
                stream_callback=stream_callback,
                transfer_cancel_event=transfer_cancel_event,
            )
            try:
                relay_obj = json.loads(relay_raw)
            except Exception:
                relay_obj = {"success": False, "error": relay_raw}
            return json.dumps(
                {
                    "success": bool(relay_obj.get("success")),
                    "method": "relay_after_direct_failed",
                    "mode": mode,
                    "connectivity_decision": connectivity_decision,
                    "connectivity": {
                        "a_to_b_22": a_to_b_ok,
                        "b_to_a_22": b_to_a_ok,
                        "a_to_b_probe_exit_code": a2b_code,
                        "b_to_a_probe_exit_code": b2a_code,
                    },
                    "attempts": attempts,
                    "relay_result": relay_obj,
                },
                ensure_ascii=False,
            )

        if name == "relay_file_between_hosts":
            source_host_id = arguments.get("source_host_id")
            target_host_id = arguments.get("target_host_id")
            source_path = (arguments.get("source_path") or "").strip()
            target_path = (arguments.get("target_path") or "").strip()
            staging_path = (arguments.get("staging_path") or "").strip().replace("\\", "/").lstrip("/")
            keep_staging_for_multi_target = bool(arguments.get("keep_staging_for_multi_target") or False)
            auto_unpack_on_target = True if arguments.get("auto_unpack_on_target") is None else bool(arguments.get("auto_unpack_on_target"))
            cleanup_staging_arg = arguments.get("cleanup_staging")
            if cleanup_staging_arg is None:
                cleanup_staging = not keep_staging_for_multi_target
            else:
                cleanup_staging = bool(cleanup_staging_arg)
            timeout_seconds = _sftp_timeout_from_args(arguments, default=600)
            pull_cap, tree_cap = _scp_pull_byte_caps(arguments)
            if not source_host_id or not target_host_id or not source_path or not target_path:
                return json.dumps({"success": False, "error": "需要 source_host_id/source_path/target_host_id/target_path"}, ensure_ascii=False)
            source_row = await _get_host_row(source_host_id)
            target_row = await _get_host_row(target_host_id)
            if not source_row or not target_row:
                return json.dumps({"success": False, "error": "源或目标主机不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(source_row, user):
                return json.dumps({"success": False, "error": "无权访问源主机"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(target_row, user):
                return json.dumps({"success": False, "error": "无权访问目标主机"}, ensure_ascii=False)
            db = await get_db()
            source_auth = await _resolve_host_auth(db, source_row)
            target_auth = await _resolve_host_auth(db, target_row)
            if not source_auth or not (source_auth.get("username") or "").strip():
                return json.dumps({"success": False, "error": "源主机凭证不可用"}, ensure_ascii=False)
            if not target_auth or not (target_auth.get("username") or "").strip():
                return json.dumps({"success": False, "error": "目标主机凭证不可用"}, ensure_ascii=False)
            await _log_credential_usage_audit(
                actor_user_id=user["id"],
                host_row=source_row,
                auth=source_auth,
                operation="relay_file_between_hosts",
                stage="source_prepare",
                extra={"target_host_id": target_host_id},
            )
            await _log_credential_usage_audit(
                actor_user_id=user["id"],
                host_row=target_row,
                auth=target_auth,
                operation="relay_file_between_hosts",
                stage="target_prepare",
                extra={"source_host_id": source_host_id},
            )
            path_kind, prep_stdout, prep_stderr, prep_code = await _probe_remote_path_kind(
                source_row, source_auth, source_path
            )
            if prep_code != 0 or path_kind not in ("file", "dir"):
                return json.dumps(
                    {
                        "success": False,
                        "step": "prepare_source_payload",
                        "error": "源路径检查失败",
                        "stdout": prep_stdout,
                        "stderr": prep_stderr,
                    },
                    ensure_ascii=False,
                )
            source_is_dir = path_kind == "dir"
            if not staging_path:
                staging_path = _relay_default_staging_path(source_path, is_dir=source_is_dir)
            if ".." in staging_path.split("/"):
                return json.dumps({"success": False, "error": "staging_path 不允许包含 .."}, ensure_ascii=False)

            fs_base = get_user_fs_root(user)
            try:
                local_abs = resolve_fs_path(staging_path, fs_base).resolve()
                local_abs.relative_to(fs_base.resolve())
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            local_abs.parent.mkdir(parents=True, exist_ok=True)

            pull_result = await run_sftp_pull_async(
                host=source_row["host"],
                port=int(source_row.get("port") or 22),
                username=source_auth.get("username") or "",
                auth_type=source_auth.get("auth_type") or "password",
                password=source_auth.get("password"),
                key_path=source_auth.get("key_path"),
                private_key_pem=source_auth.get("private_key_pem"),
                remote_path=source_path,
                local_path=str(local_abs),
                recursive=source_is_dir,
                max_bytes=pull_cap,
                max_tree_bytes=tree_cap,
                timeout=timeout_seconds,
                stream_callback=stream_callback,
                cancel_event=transfer_cancel_event,
            )
            if not pull_result.success:
                out = {
                    "success": False,
                    "step": "pull_to_staging",
                    "error": pull_result.error or "从源主机拉到 web/fs 失败",
                    "staging_path": staging_path,
                }
                if pull_result.interrupted:
                    out["interrupted"] = True
                if pull_result.bytes_transferred:
                    out["bytes_transferred"] = pull_result.bytes_transferred
                return json.dumps(out, ensure_ascii=False)

            source_name = Path(source_path.replace("\\", "/")).name
            if source_is_dir and auto_unpack_on_target:
                push_remote = f"{target_path.rstrip('/')}/{source_name}"
            else:
                push_remote = target_path
            push_recursive = source_is_dir or local_abs.is_dir()

            push_result = await run_sftp_push_async(
                host=target_row["host"],
                port=int(target_row.get("port") or 22),
                username=target_auth.get("username") or "",
                auth_type=target_auth.get("auth_type") or "password",
                password=target_auth.get("password"),
                key_path=target_auth.get("key_path"),
                private_key_pem=target_auth.get("private_key_pem"),
                local_path=str(local_abs),
                remote_path=push_remote,
                recursive=push_recursive,
                timeout=timeout_seconds,
                stream_callback=stream_callback,
                cancel_event=transfer_cancel_event,
            )
            if not push_result.success:
                out = {
                    "success": False,
                    "step": "push_from_staging",
                    "error": push_result.error or "从 web/fs 推到目标主机失败",
                    "staging_path": staging_path,
                    "push_remote_path": push_remote,
                }
                if push_result.interrupted:
                    out["interrupted"] = True
                if push_result.bytes_transferred:
                    out["bytes_transferred"] = push_result.bytes_transferred
                return json.dumps(out, ensure_ascii=False)

            cleanup_result = {"staging_deleted": False}
            if cleanup_staging:
                try:
                    await fs_delete_async(staging_path, fs_base)
                    cleanup_result["staging_deleted"] = True
                except Exception:
                    cleanup_result["staging_deleted"] = False

            return json.dumps(
                {
                    "success": True,
                    "method": "relay_via_edgeops_fs_sftp",
                    "source_host_id": source_host_id,
                    "target_host_id": target_host_id,
                    "source_path": source_path,
                    "source_is_dir": source_is_dir,
                    "auto_unpack_on_target": bool(source_is_dir and auto_unpack_on_target),
                    "target_path": target_path,
                    "push_remote_path": push_remote,
                    "staging_path": staging_path,
                    "keep_staging_for_multi_target": keep_staging_for_multi_target,
                    "pull": {
                        "bytes_transferred": pull_result.bytes_transferred,
                        "files_transferred": pull_result.files_transferred,
                        "duration_sec": pull_result.duration_sec,
                    },
                    "push": {
                        "bytes_transferred": push_result.bytes_transferred,
                        "files_transferred": push_result.files_transferred,
                        "duration_sec": push_result.duration_sec,
                    },
                    "cleanup": cleanup_result,
                },
                ensure_ascii=False,
            )

        if name == "scp_push":
            host_id = arguments.get("host_id")
            remote_path = (arguments.get("remote_path") or "").strip()
            content = arguments.get("content")
            local_path = (arguments.get("local_path") or "").strip()
            recursive = bool(arguments.get("recursive"))
            timeout = _sftp_timeout_from_args(arguments)
            if not host_id or not remote_path:
                return json.dumps({"success": False, "error": "需要 host_id 和 remote_path"}, ensure_ascii=False)
            host_row = await _get_host_row(host_id)
            if not host_row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(host_row, user):
                return json.dumps({"success": False, "error": "无权操作该主机"}, ensure_ascii=False)
            auth = await _resolve_host_auth(await get_db(), host_row)
            if not auth:
                return json.dumps({"success": False, "error": "主机认证信息无效"}, ensure_ascii=False)
            await _log_credential_usage_audit(
                actor_user_id=user["id"],
                host_row=host_row,
                auth=auth,
                operation="scp_push",
                stage="prepare",
                extra={"remote_path": remote_path, "local_path": local_path or None, "recursive": recursive},
            )
            if local_path:
                try:
                    base = get_user_fs_root(user)
                    path_obj = resolve_fs_path(local_path, base).resolve()
                    path_obj.relative_to(base.resolve())
                except ValueError as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                if not path_obj.exists():
                    return json.dumps(
                        {
                            "success": False,
                            "error": f"本地路径不存在: {local_path}",
                            "hint": (
                                "scp_push 不会自动补全路径。请用 fs_list 或上一工具返回的精确相对路径；"
                                "禁止手拼 chats/YYYY/MM/DD/。工作区根下的文件可直接写文件名"
                                "（如 edgeops-v1.8.6-sp2.tgz）。"
                            ),
                        },
                        ensure_ascii=False,
                    )
                if path_obj.is_dir() and not recursive:
                    return json.dumps({"success": False, "error": "本地路径为目录，请设置 recursive=true"}, ensure_ascii=False)
                if not path_obj.is_file() and not path_obj.is_dir():
                    return json.dumps({"success": False, "error": f"本地路径无效: {local_path}"}, ensure_ascii=False)
                result = await run_sftp_push_async(
                    host=host_row["host"],
                    port=int(host_row.get("port") or 22),
                    username=auth.get("username") or "",
                    auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"),
                    key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    local_path=str(path_obj),
                    remote_path=remote_path,
                    recursive=recursive,
                    timeout=timeout,
                    stream_callback=stream_callback,
                    cancel_event=transfer_cancel_event,
                )
                if not result.success:
                    out = {"success": False, "error": result.error or "上传失败"}
                    if result.interrupted:
                        out["interrupted"] = True
                    if result.bytes_transferred:
                        out["bytes_transferred"] = result.bytes_transferred
                    return json.dumps(out, ensure_ascii=False)
                return json.dumps(
                    {
                        "success": True,
                        "message": f"已上传至 {result.resolved_remote_path or remote_path}",
                        "remote_path": result.resolved_remote_path or remote_path,
                        "local_path": local_path,
                        "bytes_transferred": result.bytes_transferred,
                        "files_transferred": result.files_transferred,
                        "duration_sec": result.duration_sec,
                        "recursive": recursive,
                    },
                    ensure_ascii=False,
                )
            if content is None:
                content = ""
            content_b = (content if isinstance(content, str) else str(content)).encode("utf-8", errors="replace")
            err = await sftp_put_content(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                remote_path=remote_path,
                content=content_b,
                timeout=min(timeout, 120),
            )
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            return json.dumps(
                {
                    "success": True,
                    "message": f"已写入 {remote_path}",
                    "bytes_transferred": len(content_b),
                    "files_transferred": 1,
                },
                ensure_ascii=False,
            )

        if name == "scp_pull":
            host_id = arguments.get("host_id")
            remote_path = (arguments.get("remote_path") or "").strip()
            local_path = (arguments.get("local_path") or "").strip()
            recursive = bool(arguments.get("recursive"))
            if not host_id or not remote_path or not local_path:
                return json.dumps(
                    {"success": False, "error": "需要 host_id、remote_path、local_path（相对工作区根的路径）"},
                    ensure_ascii=False,
                )
            raw_local_requested = local_path
            max_bytes, tree_cap = _scp_pull_byte_caps(arguments)
            timeout = _sftp_timeout_from_args(arguments)
            local_path = _normalize_sftp_pull_local_path(
                raw_local_requested,
                remote_path,
                as_directory=recursive,
                session_managed=_effective_session_managed(arguments, raw_local_requested),
                session_id=session_id,
            )
            host_row = await _get_host_row(host_id)
            if not host_row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(host_row, user):
                return json.dumps({"success": False, "error": "无权操作该主机"}, ensure_ascii=False)
            auth = await _resolve_host_auth(await get_db(), host_row)
            if not auth:
                return json.dumps({"success": False, "error": "主机认证信息无效"}, ensure_ascii=False)
            try:
                base = get_user_fs_root(user)
                path_obj = resolve_fs_path(local_path, base)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            path_obj = path_obj.resolve()
            try:
                path_obj.relative_to(base.resolve())
            except ValueError:
                return json.dumps({"success": False, "error": "local_path 越界"}, ensure_ascii=False)
            if not recursive:
                path_obj.parent.mkdir(parents=True, exist_ok=True)
            await _log_credential_usage_audit(
                actor_user_id=user["id"],
                host_row=host_row,
                auth=auth,
                operation="scp_pull",
                stage="prepare",
                extra={
                    "remote_path": remote_path,
                    "local_path": local_path,
                    "requested_local_path": raw_local_requested,
                    "recursive": recursive,
                },
            )
            result = await run_sftp_pull_async(
                host=host_row["host"],
                port=int(host_row.get("port") or 22),
                username=auth.get("username") or "",
                auth_type=auth.get("auth_type") or "password",
                password=auth.get("password"),
                key_path=auth.get("key_path"),
                private_key_pem=auth.get("private_key_pem"),
                remote_path=remote_path,
                local_path=str(path_obj),
                recursive=recursive,
                max_bytes=max_bytes,
                max_tree_bytes=tree_cap,
                timeout=timeout,
                stream_callback=stream_callback,
                cancel_event=transfer_cancel_event,
            )
            if not result.success:
                out = {"success": False, "error": result.error or "下载失败"}
                if result.interrupted:
                    out["interrupted"] = True
                if result.bytes_transferred:
                    out["bytes_transferred"] = result.bytes_transferred
                return json.dumps(out, ensure_ascii=False)
            return json.dumps(
                {
                    "success": True,
                    "message": f"已保存到工作区:{local_path}",
                    "local_path": local_path,
                    "requested_local_path": raw_local_requested,
                    "bytes_transferred": result.bytes_transferred,
                    "files_transferred": result.files_transferred,
                    "duration_sec": result.duration_sec,
                    "remote_path": remote_path,
                    "recursive": recursive,
                },
                ensure_ascii=False,
            )

        if name == "http_request":
            from services.http_transfer import http_request_async

            url = (arguments.get("url") or "").strip()
            if not url:
                return json.dumps({"success": False, "error": "需要 url"}, ensure_ascii=False)
            method = (arguments.get("method") or "GET").strip().upper()
            result = await http_request_async(
                method=method,
                url=url,
                headers=arguments.get("headers"),
                query=arguments.get("query"),
                body=arguments.get("body"),
                body_encoding=(arguments.get("body_encoding") or "text"),
                timeout=_http_timeout_from_args(arguments),
                max_response_bytes=arguments.get("max_response_bytes"),
                follow_redirects=arguments.get("follow_redirects") is not False,
                stream_callback=stream_callback,
                cancel_event=transfer_cancel_event,
            )
            return json.dumps(_http_result_payload(result), ensure_ascii=False)

        if name == "http_download":
            from services.http_transfer import http_download_async

            url = (arguments.get("url") or "").strip()
            raw_local_requested = (arguments.get("local_path") or "").strip()
            if not url or not raw_local_requested:
                return json.dumps(
                    {"success": False, "error": "需要 url 和 local_path（相对工作区根的路径）"},
                    ensure_ascii=False,
                )
            max_bytes = _http_transfer_cap(arguments, "HTTP_TOOL_MAX_DOWNLOAD_BYTES")
            chunk_size = arguments.get("chunk_size")
            if chunk_size is not None:
                try:
                    chunk_size = max(1024 * 1024, int(chunk_size))
                except (TypeError, ValueError):
                    return json.dumps({"success": False, "error": "chunk_size 无效"}, ensure_ascii=False)
            local_path = _normalize_sftp_pull_local_path(
                raw_local_requested,
                url,
                as_directory=False,
                session_managed=_effective_session_managed(arguments, raw_local_requested),
                session_id=session_id,
            )
            try:
                base = get_user_fs_root(user)
                path_obj = resolve_fs_path(local_path, base)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            path_obj = path_obj.resolve()
            try:
                path_obj.relative_to(base.resolve())
            except ValueError:
                return json.dumps({"success": False, "error": "local_path 越界"}, ensure_ascii=False)
            chunk_index = arguments.get("chunk_index")
            if chunk_index is not None:
                try:
                    chunk_index = int(chunk_index)
                except (TypeError, ValueError):
                    return json.dumps({"success": False, "error": "chunk_index 无效"}, ensure_ascii=False)
            result = await http_download_async(
                url=url,
                local_path=path_obj,
                headers=arguments.get("headers"),
                timeout=_http_timeout_from_args(arguments),
                max_bytes=max_bytes,
                follow_redirects=arguments.get("follow_redirects") is not False,
                chunk_size=chunk_size,
                chunked=bool(arguments.get("chunked")),
                chunk_index=chunk_index,
                merge_chunks=arguments.get("merge_chunks") is not False,
                delete_parts=arguments.get("delete_parts") is not False,
                stream_callback=stream_callback,
                cancel_event=transfer_cancel_event,
            )
            payload = _http_result_payload(result)
            if result.success:
                payload["message"] = (
                    f"已合并保存到工作区:{local_path}"
                    if result.merged
                    else f"已保存到工作区:{payload.get('local_path') or local_path}"
                )
                payload["requested_local_path"] = raw_local_requested
            return json.dumps(payload, ensure_ascii=False)

        if name == "http_download_merge":
            from services.http_transfer import http_download_merge_async

            raw_local = (arguments.get("local_path") or "").strip()
            if not raw_local:
                return json.dumps({"success": False, "error": "需要 local_path"}, ensure_ascii=False)
            try:
                base = get_user_fs_root(user)
                path_obj = resolve_fs_path(raw_local, base).resolve()
                path_obj.relative_to(base.resolve())
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            part_paths = None
            raw_parts = arguments.get("part_paths")
            if isinstance(raw_parts, list) and raw_parts:
                part_paths = []
                for rp in raw_parts:
                    p = resolve_fs_path(str(rp).strip(), base).resolve()
                    p.relative_to(base.resolve())
                    part_paths.append(p)
            result = await http_download_merge_async(
                output_path=path_obj,
                part_paths=part_paths,
                delete_parts=arguments.get("delete_parts") is not False,
            )
            payload = _http_result_payload(result)
            if result.success:
                payload["message"] = f"已合并到工作区:{raw_local}"
            return json.dumps(payload, ensure_ascii=False)

        if name == "http_upload":
            from services.http_transfer import http_upload_async

            url = (arguments.get("url") or "").strip()
            local_path = (arguments.get("local_path") or "").strip()
            if not url or not local_path:
                return json.dumps(
                    {"success": False, "error": "需要 url 和 local_path（相对工作区根的文件路径）"},
                    ensure_ascii=False,
                )
            max_bytes = _http_transfer_cap(arguments, "HTTP_TOOL_MAX_UPLOAD_BYTES")
            try:
                base = get_user_fs_root(user)
                path_obj = resolve_fs_path(local_path, base).resolve()
                path_obj.relative_to(base.resolve())
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            if not path_obj.is_file():
                return json.dumps({"success": False, "error": f"本地文件不存在: {local_path}"}, ensure_ascii=False)
            result = await http_upload_async(
                url=url,
                local_path=path_obj,
                method=(arguments.get("method") or "POST"),
                headers=arguments.get("headers"),
                field_name=(arguments.get("field_name") or "file"),
                form_fields=arguments.get("form_fields"),
                content_type=arguments.get("content_type"),
                timeout=_http_timeout_from_args(arguments),
                max_bytes=max_bytes,
                follow_redirects=arguments.get("follow_redirects") is not False,
                multipart=arguments.get("multipart") is not False,
                stream_callback=stream_callback,
                cancel_event=transfer_cancel_event,
            )
            payload = _http_result_payload(result)
            if result.success:
                payload["message"] = f"已上传 {local_path}"
            return json.dumps(payload, ensure_ascii=False)

        if name == "ask_user_choice":
            question = (arguments.get("question") or "").strip()
            raw_options = arguments.get("options") or []
            allow_multiple = bool(arguments.get("allow_multiple"))
            allow_text = arguments.get("allow_text")
            allow_text = True if allow_text is None else bool(allow_text)
            default_id = (arguments.get("default_id") or "").strip() or None
            if not question:
                return json.dumps({"success": False, "error": "question 不能为空"}, ensure_ascii=False)
            if not isinstance(raw_options, list) or len(raw_options) < 2:
                return json.dumps({"success": False, "error": "options 至少 2 项"}, ensure_ascii=False)
            normalized: list[dict] = []
            seen_ids: set[str] = set()
            for idx, opt in enumerate(raw_options[:12]):
                if not isinstance(opt, dict):
                    continue
                label = (opt.get("label") or "").strip()
                if not label:
                    continue
                oid = (opt.get("id") or "").strip()
                if not oid:
                    oid = chr(ord("A") + idx) if idx < 26 else f"O{idx+1}"
                if oid in seen_ids:
                    oid = f"{oid}{idx+1}"
                seen_ids.add(oid)
                value = opt.get("value")
                value = value if isinstance(value, str) and value else label
                style = (opt.get("style") or "default").strip().lower()
                if style not in ("default", "primary", "danger", "success"):
                    style = "default"
                description = (opt.get("description") or "").strip()
                normalized.append({
                    "id": oid,
                    "label": label,
                    "value": value,
                    "style": style,
                    "description": description,
                })
            if len(normalized) < 2:
                return json.dumps({"success": False, "error": "至少需要 2 个有效选项（必须含 label）"}, ensure_ascii=False)
            ui_action = {
                "action": "ask_user_choice",
                "question": question,
                "options": normalized,
                "allow_multiple": allow_multiple,
                "allow_text": allow_text,
                "default_id": default_id,
            }
            if not ui_capable:
                lines = [f"已向用户提出选择题（**当前为无 UI 模式**，按钮无法渲染——请在你接下来的纯文本回复里复述如下问题与选项，并明确告知用户用文字回复对应编号或选项文本即可）：", "", f"问题：{question}", ""]
                for opt in normalized:
                    bits = f"[{opt['id']}] {opt['label']}"
                    if opt.get("description"):
                        bits += f" — {opt['description']}"
                    lines.append(bits)
                if allow_multiple:
                    lines.append("\n（多选：用户可以同时选多项，如 A,C；也可文字补充）")
                if allow_text:
                    lines.append("\n（用户也可以不选，直接文字回复补充说明）")
                return json.dumps({
                    "success": True,
                    "ui_capable": False,
                    "message": "\n".join(lines),
                    "wait_for_user": True,
                }, ensure_ascii=False)
            return json.dumps({
                "success": True,
                "ui_capable": True,
                "message": "已向用户提出选择题，请等待用户点击或文字回复后再继续；本轮请勿继续调用其他工具或代用户作答。",
                "wait_for_user": True,
                "ui_action": ui_action,
            }, ensure_ascii=False)

        if name == "get_server_time":
            db = await get_db()
            tz = await get_effective_site_timezone(db)
            payload = build_server_time_payload(tz)
            return json.dumps(
                {
                    "success": True,
                    **payload,
                    "note": "site_timezone 由管理员在全局设置中配置（默认 Asia/Shanghai），用于全站时间展示与 AI 回答当前时刻。",
                },
                ensure_ascii=False,
            )

        if name == "regex_process":
            def _run_regex_process():
                op = (arguments.get("operation") or "").strip().lower()
                text = str(arguments.get("text") or "")
                pattern = str(arguments.get("pattern") or "")
                max_results = min(1000, max(1, int(arguments.get("max_results") or 100)))
                count_arg = max(0, int(arguments.get("count") or 0))
                rx = re.compile(pattern, _regex_flags(arguments.get("flags")))
                if op == "search":
                    m = rx.search(text)
                    return json.dumps({"success": True, "matched": bool(m), "match": ({"span": list(m.span()), "text": m.group(0), "groups": list(m.groups()), "groupdict": m.groupdict()} if m else None)}, ensure_ascii=False)
                if op in ("findall", "extract"):
                    items = []
                    for m in rx.finditer(text):
                        items.append({"span": list(m.span()), "text": m.group(0), "groups": list(m.groups()), "groupdict": m.groupdict()})
                        if len(items) >= max_results:
                            break
                    return json.dumps({"success": True, "count_returned": len(items), "truncated": len(items) >= max_results, "matches": items}, ensure_ascii=False)
                if op == "split":
                    parts = rx.split(text, maxsplit=count_arg)
                    return json.dumps({"success": True, "parts": parts[:max_results], "count": len(parts), "truncated": len(parts) > max_results}, ensure_ascii=False)
                if op == "replace":
                    replacement = str(arguments.get("replacement") or "")
                    result, n = rx.subn(replacement, text, count=count_arg)
                    return json.dumps({"success": True, "replacements": n, "result": _truncate_value(result)}, ensure_ascii=False)
                if op == "count":
                    n = sum(1 for _ in rx.finditer(text))
                    return json.dumps({"success": True, "count": n}, ensure_ascii=False)
                return json.dumps({"success": False, "error": "operation 不支持"}, ensure_ascii=False)
            return await asyncio.to_thread(_run_regex_process)

        if name == "string_process":
            op = (arguments.get("operation") or "").strip().lower()
            text = str(arguments.get("text") or "")
            if op == "trim":
                result = text.strip()
            elif op == "case":
                case = (arguments.get("case") or "lower").strip().lower()
                result = {"lower": text.lower, "upper": text.upper, "title": text.title, "capitalize": text.capitalize, "swap": text.swapcase}[case]()
            elif op == "replace":
                result = text.replace(str(arguments.get("old") or ""), str(arguments.get("new") or ""))
            elif op == "split":
                sep = arguments.get("sep")
                result = text.split(sep if sep is not None else None)
            elif op == "join":
                sep = str(arguments.get("sep") if arguments.get("sep") is not None else "")
                result = sep.join(str(x) for x in (arguments.get("items") or []))
            elif op == "substring":
                start = arguments.get("start")
                end = arguments.get("end")
                result = text[int(start) if start is not None else None:int(end) if end is not None else None]
            elif op == "contains":
                result = str(arguments.get("old") or "") in text
            elif op == "count":
                result = text.count(str(arguments.get("old") or ""))
            elif op == "line_stats":
                lines = text.splitlines()
                result = {"chars": len(text), "lines": len(lines), "non_empty_lines": sum(1 for l in lines if l.strip()), "words": len(re.findall(r"\S+", text))}
            elif op == "base64_encode":
                result = base64.b64encode(text.encode("utf-8")).decode("ascii")
            elif op == "base64_decode":
                result = base64.b64decode(text.encode("ascii")).decode("utf-8", errors="replace")
            elif op == "url_encode":
                result = quote(text)
            elif op == "url_decode":
                result = unquote(text)
            elif op == "hash":
                algo = (arguments.get("algorithm") or "sha256").strip().lower()
                h = getattr(hashlib, algo)()
                h.update(text.encode("utf-8"))
                result = h.hexdigest()
            else:
                return json.dumps({"success": False, "error": "operation 不支持"}, ensure_ascii=False)
            return json.dumps({"success": True, "result": _truncate_value(result)}, ensure_ascii=False)

        if name == "crypto_toolkit":
            try:
                out = await asyncio.to_thread(_crypto_toolkit_impl, arguments or {})
                return json.dumps(out, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "math_calculate":
            from services.math_compute import run_math_calculate
            try:
                out = await asyncio.to_thread(run_math_calculate, arguments or {})
                return json.dumps(out, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "data_query":
            op = (arguments.get("operation") or "").strip().lower()
            max_results = min(1000, max(1, int(arguments.get("max_results") or 100)))
            data, detected = _parse_structured_data(str(arguments.get("data") or ""), str(arguments.get("format") or "auto"))
            if op == "parse":
                return json.dumps({"success": True, "format": detected, "data": _truncate_value(data)}, ensure_ascii=False)
            if op == "summary":
                return json.dumps({"success": True, "format": detected, "summary": _data_summary(data)}, ensure_ascii=False)
            if op == "get_path":
                value = _get_path_value(data, str(arguments.get("path") or ""))
                return json.dumps({"success": True, "format": detected, "path": arguments.get("path"), "value": _truncate_value(value)}, ensure_ascii=False)
            if op == "search":
                query = str(arguments.get("query") or "")
                results = _search_data(data, query, bool(arguments.get("regex")), max_results)
                return json.dumps({"success": True, "format": detected, "count_returned": len(results), "truncated": len(results) >= max_results, "results": results}, ensure_ascii=False)
            if op == "filter_list":
                arr = _get_path_value(data, str(arguments.get("path") or "")) if arguments.get("path") else data
                if not isinstance(arr, list):
                    return json.dumps({"success": False, "error": "filter_list 目标必须是数组"}, ensure_ascii=False)
                key = str(arguments.get("key") or "")
                cmp_op = (arguments.get("op") or "eq").strip().lower()
                target = arguments.get("value")

                def ok(item):
                    cur = item.get(key) if isinstance(item, dict) else item
                    if cmp_op == "eq": return cur == target
                    if cmp_op == "ne": return cur != target
                    if cmp_op == "contains": return str(target) in str(cur)
                    if cmp_op == "regex": return bool(re.search(str(target), str(cur), re.IGNORECASE))
                    if cmp_op in ("gt", "gte", "lt", "lte"):
                        a, b = float(cur), float(target)
                        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[cmp_op]
                    return False

                filtered = [x for x in arr if ok(x)]
                return json.dumps({"success": True, "format": detected, "count": len(filtered), "items": _truncate_value(filtered[:max_results]), "truncated": len(filtered) > max_results}, ensure_ascii=False)
            return json.dumps({"success": False, "error": "operation 不支持"}, ensure_ascii=False)

        if name == "markup_query":
            op = (arguments.get("operation") or "").strip().lower()
            raw = str(arguments.get("data") or "")
            fmt = _detect_markup_format(raw, str(arguments.get("format") or "auto"))
            max_results = min(1000, max(1, int(arguments.get("max_results") or 100)))
            attrs = [str(a) for a in (arguments.get("attrs") or []) if str(a or "").strip()]
            if op == "summary":
                return json.dumps({"success": True, "summary": _markup_summary(raw, fmt)}, ensure_ascii=False)
            if fmt == "html":
                soup = BeautifulSoup(raw, "html.parser")
                if op == "find_tags":
                    tag = (arguments.get("tag") or True)
                    items = [_html_node_payload(n, attrs) for n in soup.find_all(tag)[:max_results]]
                    return json.dumps({"success": True, "format": fmt, "count_returned": len(items), "items": items}, ensure_ascii=False)
                if op == "select":
                    selector = str(arguments.get("selector") or "")
                    items = [_html_node_payload(n, attrs) for n in soup.select(selector, limit=max_results)]
                    return json.dumps({"success": True, "format": fmt, "selector": selector, "count_returned": len(items), "items": items}, ensure_ascii=False)
                if op == "search_text":
                    items = _markup_search_text(raw, fmt, str(arguments.get("query") or ""), bool(arguments.get("regex")), max_results)
                    return json.dumps({"success": True, "format": fmt, "count_returned": len(items), "items": items}, ensure_ascii=False)
                if op == "get_text":
                    selector = str(arguments.get("selector") or "")
                    nodes = soup.select(selector, limit=max_results) if selector else [soup]
                    texts = [n.get_text(" ", strip=True) for n in nodes]
                    return json.dumps({"success": True, "format": fmt, "texts": _truncate_value(texts)}, ensure_ascii=False)
                if op == "extract_attrs":
                    tag = arguments.get("tag") or True
                    items = [_html_node_payload(n, attrs) for n in soup.find_all(tag)[:max_results]]
                    return json.dumps({"success": True, "format": fmt, "count_returned": len(items), "items": items}, ensure_ascii=False)
                if op == "extract_links":
                    links = []
                    for n in soup.find_all(["a", "link", "script", "img"]):
                        val = n.get("href") or n.get("src")
                        if val:
                            links.append({"tag": n.name, "url": val, "text": n.get_text(" ", strip=True)[:300]})
                        if len(links) >= max_results:
                            break
                    return json.dumps({"success": True, "format": fmt, "count_returned": len(links), "links": links}, ensure_ascii=False)
            else:
                root = ET.fromstring(raw)
                if op == "find_tags":
                    tag = str(arguments.get("tag") or "*")
                    elems = list(root.iter(tag if tag != "*" else None)) if tag != "*" else list(root.iter())
                    items = [_xml_node_payload(n, attrs) for n in elems[:max_results]]
                    return json.dumps({"success": True, "format": fmt, "count_returned": len(items), "items": items}, ensure_ascii=False)
                if op == "select":
                    selector = str(arguments.get("selector") or ".")
                    elems = root.findall(selector)
                    items = [_xml_node_payload(n, attrs) for n in elems[:max_results]]
                    return json.dumps({"success": True, "format": fmt, "selector": selector, "count_returned": len(items), "items": items}, ensure_ascii=False)
                if op == "search_text":
                    items = _markup_search_text(raw, fmt, str(arguments.get("query") or ""), bool(arguments.get("regex")), max_results)
                    return json.dumps({"success": True, "format": fmt, "count_returned": len(items), "items": items}, ensure_ascii=False)
                if op == "get_text":
                    selector = str(arguments.get("selector") or ".")
                    elems = root.findall(selector) if selector != "." else [root]
                    texts = ["".join(n.itertext()).strip() for n in elems[:max_results]]
                    return json.dumps({"success": True, "format": fmt, "texts": _truncate_value(texts)}, ensure_ascii=False)
                if op == "extract_attrs":
                    tag = str(arguments.get("tag") or "*")
                    elems = list(root.iter(tag if tag != "*" else None)) if tag != "*" else list(root.iter())
                    items = [_xml_node_payload(n, attrs) for n in elems[:max_results]]
                    return json.dumps({"success": True, "format": fmt, "count_returned": len(items), "items": items}, ensure_ascii=False)
                if op == "extract_links":
                    links = []
                    for n in root.iter():
                        for key in ("href", "src", "url"):
                            if key in n.attrib:
                                links.append({"tag": n.tag, "attr": key, "url": n.attrib[key], "text": "".join(n.itertext()).strip()[:300]})
                                break
                        if len(links) >= max_results:
                            break
                    return json.dumps({"success": True, "format": fmt, "count_returned": len(links), "links": links}, ensure_ascii=False)
            return json.dumps({"success": False, "error": "operation 不支持"}, ensure_ascii=False)

        if name == "get_settings":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "需要管理员权限"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT * FROM settings ORDER BY key")
            return json.dumps({"success": True, "settings": [dict(r) for r in rows]}, ensure_ascii=False)

        if name == "update_setting":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "需要管理员权限"}, ensure_ascii=False)
            key_ = (arguments.get("key") or "").strip()
            value_ = arguments.get("value")
            if not key_:
                return json.dumps({"success": False, "error": "key 必填"}, ensure_ascii=False)
            if key_ == SETTINGS_KEY_SITE_TZ:
                ok_tz, msg_tz = validate_iana_timezone(str(value_ or ""))
                if not ok_tz:
                    return json.dumps({"success": False, "error": msg_tz}, ensure_ascii=False)
                value_ = msg_tz
            if (key_.lower().count("key") or key_.lower().count("secret") or key_.lower().count("password")) and (value_ is None or str(value_).strip() in ("", "***")):
                return json.dumps({"success": True}, ensure_ascii=False)
            db = await get_db()
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=?, updated_at=CURRENT_TIMESTAMP",
                (key_, str(value_ or ""), str(value_ or "")),
            )
            await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "list_logs":
            db = await get_db()
            limit = int(arguments.get("limit") or 100)
            host_id = arguments.get("host_id")
            user_id = arguments.get("user_id")
            if not _is_admin(user):
                user_id = user["id"]
            query = "SELECT l.*, u.username FROM operation_logs l LEFT JOIN users u ON u.id = l.user_id WHERE 1=1"
            params = []
            if host_id is not None:
                query += " AND l.host_id = ?"
                params.append(host_id)
            if user_id is not None:
                query += " AND l.user_id = ?"
                params.append(user_id)
            query += " ORDER BY l.created_at DESC LIMIT ?"
            params.append(min(limit, 500))
            rows = await db.execute_fetchall(query, params)
            return json.dumps({"success": True, "logs": [dict(r) for r in rows]}, ensure_ascii=False)

        if name == "clear_logs":
            db = await get_db()
            uid = arguments.get("user_id")
            if _is_admin(user) and uid is not None:
                await db.execute("DELETE FROM operation_logs WHERE user_id = ?", (int(uid),))
            elif _is_admin(user):
                await db.execute("DELETE FROM operation_logs")
            else:
                await db.execute("DELETE FROM operation_logs WHERE user_id = ?", (user["id"],))
            await db.commit()
            return json.dumps({"success": True, "message": "已清空操作日志"}, ensure_ascii=False)

        if name == "clear_batches":
            db = await get_db()
            if _is_admin(user):
                await db.execute("DELETE FROM batch_operation_details")
                await db.execute("DELETE FROM batch_operations")
            else:
                await db.execute("DELETE FROM batch_operation_details WHERE batch_id IN (SELECT id FROM batch_operations WHERE created_by = ?)", (user["id"],))
                await db.execute("DELETE FROM batch_operations WHERE created_by = ?", (user["id"],))
            await db.commit()
            return json.dumps({"success": True, "message": "已清空批量任务"}, ensure_ascii=False)

        if name == "batch_create":
            from api.batch import _create_batch_and_start
            op = (arguments.get("operation_type") or "").strip()
            task_host_id = arguments.get("task_host_id")
            task_dir_name = (arguments.get("task_dir_name") or "").strip()
            scope = (arguments.get("scope_type") or "selected").strip()
            tag_match_mode = (arguments.get("tag_match_mode") or "any").strip().lower() or "any"
            scope_value = arguments.get("scope_value")
            if isinstance(scope_value, (list, tuple)):
                scope_value = [int(x) for x in scope_value if x is not None]
            else:
                scope_value = []
            params = arguments.get("params")
            if not isinstance(params, dict):
                params = {}
            if op not in ("run_command", "scp_push", "scp_pull", "run_script", "restart"):
                return json.dumps(
                    {"success": False, "error": "operation_type 须为 run_command/scp_push/scp_pull/run_script/restart"},
                    ensure_ascii=False,
                )
            if scope == "tag" and tag_match_mode not in ("any", "all"):
                return json.dumps({"success": False, "error": "tag_match_mode 须为 any/all"}, ensure_ascii=False)
            try:
                batch_id = await _create_batch_and_start(op, scope, scope_value, params, user["id"], tag_match_mode, _is_admin(user))
                is_risky = (op == "restart")
                if op == "run_command":
                    is_risky = _is_high_risk_command((params.get("command") or ""))
                if is_risky and task_host_id is not None and task_dir_name:
                    host_row = await _get_host_row(int(task_host_id))
                    if host_row and await _can_access_host_with_shares(host_row, user):
                        auth = await _resolve_host_auth(await get_db(), host_row)
                        if auth:
                            detail = (
                                f"batch_id={batch_id}\n"
                                f"operation_type={op}\n"
                                f"scope_type={scope}\n"
                                f"tag_match_mode={tag_match_mode}\n"
                                f"scope_value={scope_value}\n"
                                f"params={json.dumps(params, ensure_ascii=False)}"
                            )
                            await _edgeops_auto_append_task_log(
                                host_row,
                                auth,
                                task_dir_name,
                                phase="高风险操作",
                                action=f"创建批量任务 #{batch_id}",
                                result=f"已提交 {op} 批量任务",
                                details=detail,
                            )
                hint = (
                    f"已创建批量任务 #{batch_id}，正在后台执行。"
                    f"请用 get_batch_detail(batch_id={batch_id}) 查询进度，直至 status 为 completed/cancelled。"
                )
                if op == "scp_pull":
                    hint += f" 拉取文件默认落在工作区 batch_pulls/{batch_id}/<host_id>/ 下。"
                return json.dumps({"success": True, "batch_id": batch_id, "message": hint}, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "list_batch_operations":
            db = await get_db()
            limit = min(int(arguments.get("limit") or 20), 50)
            if _is_admin(user):
                rows = await db.execute_fetchall(
                    "SELECT id, operation_type, scope_type, total_count, pending_count, success_count, fail_count, status, created_at FROM batch_operations ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            else:
                rows = await db.execute_fetchall(
                    "SELECT id, operation_type, scope_type, total_count, pending_count, success_count, fail_count, status, created_at FROM batch_operations WHERE created_by = ? ORDER BY created_at DESC LIMIT ?",
                    (user["id"], limit),
                )
            batches = []
            for r in rows:
                d = dict(r)
                total = int(d.get("total_count") or 0)
                success = int(d.get("success_count") or 0)
                fail = int(d.get("fail_count") or 0)
                pending = int(d.get("pending_count") or 0)
                done = success + fail
                d["progress"] = {
                    "done": done,
                    "total": total,
                    "percent": round(100.0 * done / total, 1) if total > 0 else 0.0,
                    "pending": pending,
                    "success": success,
                    "failed": fail,
                }
                batches.append(d)
            return json.dumps({"success": True, "batches": batches}, ensure_ascii=False)

        if name == "get_batch_detail":
            batch_id = arguments.get("batch_id")
            if batch_id is None:
                return json.dumps({"success": False, "error": "缺少 batch_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT * FROM batch_operations WHERE id = ?", (batch_id,))
            if not rows:
                return json.dumps({"success": False, "error": "批量操作不存在"}, ensure_ascii=False)
            batch = dict(rows[0])
            if not _is_admin(user) and batch.get("created_by") != user["id"]:
                return json.dumps({"success": False, "error": "批量操作不存在"}, ensure_ascii=False)
            if isinstance(batch.get("params"), str):
                try:
                    batch["params"] = json.loads(batch["params"] or "{}")
                except Exception:
                    batch["params"] = {}
            if isinstance(batch.get("scope_value"), str):
                try:
                    batch["scope_value"] = json.loads(batch["scope_value"] or "[]")
                except Exception:
                    batch["scope_value"] = []
            details = await db.execute_fetchall(
                "SELECT bd.*, h.name as host_name, h.host FROM batch_operation_details bd JOIN hosts h ON h.id = bd.host_id WHERE bd.batch_id = ? ORDER BY bd.id",
                (batch_id,),
            )
            detail_list = []
            status_counts = {"pending": 0, "running": 0, "success": 0, "failed": 0, "skipped": 0}
            for r in details:
                item = dict(r)
                st = (item.get("status") or "").strip() or "pending"
                status_counts[st] = status_counts.get(st, 0) + 1
                raw_res = item.get("result")
                if isinstance(raw_res, str) and raw_res.strip():
                    try:
                        item["result"] = json.loads(raw_res)
                    except Exception:
                        pass
                detail_list.append(item)
            total = int(batch.get("total_count") or 0) or len(detail_list)
            success = int(batch.get("success_count") or 0)
            fail = int(batch.get("fail_count") or 0)
            pending = int(batch.get("pending_count") or 0)
            done = success + fail + int(status_counts.get("skipped") or 0)
            batch["details"] = detail_list
            batch["progress"] = {
                "done": done,
                "total": total,
                "percent": round(100.0 * done / total, 1) if total > 0 else 0.0,
                "pending": pending,
                "running": int(status_counts.get("running") or 0),
                "success": success,
                "failed": fail,
                "skipped": int(status_counts.get("skipped") or 0),
                "by_status": status_counts,
                "finished": (batch.get("status") or "") in ("completed", "cancelled"),
            }
            return json.dumps({"success": True, "batch": batch}, ensure_ascii=False)

        if name == "get_ai_config":
            from services.ai_model_profiles import (
                get_active_profile_id,
                get_resolved_user_ai_settings,
                list_profiles,
            )

            db = await get_db()
            uid, err = _profile_tool_target_uid(user, arguments)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            if arguments.get("user_id") is not None and _is_admin(user):
                rows = await db.execute_fetchall("SELECT id FROM users WHERE id = ?", (uid,))
                if not rows:
                    return json.dumps({"success": False, "error": "用户不存在"}, ensure_ascii=False)
            settings = await get_resolved_user_ai_settings(db, uid)

            def _safe_int(s: str | None) -> int:
                try:
                    return int((s or "0").strip() or "0")
                except (TypeError, ValueError):
                    return 0

            ak = (settings.get("ai_api_key") or "").strip()
            result = {
                "api_key": "***" if ak else "",
                "api_key_set": bool(ak),
                "base_url": settings.get("ai_base_url") or "",
                "model": settings.get("ai_model") or "",
                "system_prompt": settings.get("ai_system_prompt") or "",
                "auto_approve": (settings.get("ai_auto_approve") or "false").lower() == "true",
                "assistant_enabled": (settings.get("ai_assistant_enabled") or "false").lower() == "true",
                "context_size": int(settings.get("ai_context_size") or "0"),
                "provider": (settings.get("ai_provider") or "").strip(),
                "agent_max_steps": _safe_int(settings.get("ai_agent_max_steps")),
                "assistant_max_rounds": _safe_int(settings.get("ai_assistant_max_rounds")),
                "vision_enabled": (settings.get("ai_vision_enabled") or "true").lower() != "false",
                "output_locale": (settings.get("ai_output_locale") or "").strip(),
            }
            try:
                from config import AGENT_MAX_STEPS as _AGENT_DEF
                from config import ASSISTANT_MAX_ROUNDS as _ROUNDS_DEF
                from config import AGENT_MAX_STEPS_CAP as _AGENT_CAP
                from config import ASSISTANT_MAX_ROUNDS_CAP as _ROUNDS_CAP
            except Exception:
                _AGENT_DEF, _ROUNDS_DEF, _AGENT_CAP, _ROUNDS_CAP = 100, 100, 1000, 1000
            for _k, _sk in (
                ("agent_max_steps", "ai_agent_max_steps"),
                ("assistant_max_rounds", "ai_assistant_max_rounds"),
            ):
                if int(result.get(_k) or 0) <= 0:
                    rows = await db.execute_fetchall("SELECT value FROM settings WHERE key = ?", (_sk,))
                    try:
                        v = int(((rows[0]["value"] if rows else "") or "0"))
                    except (TypeError, ValueError):
                        v = 0
                    result[_k] = v or (_AGENT_DEF if _k == "agent_max_steps" else _ROUNDS_DEF)
            prof_items, _ = await list_profiles(db, uid)
            active_id = await get_active_profile_id(db, uid)
            return json.dumps({
                "success": True,
                "config": result,
                "active_profile_id": active_id,
                "profiles": prof_items,
                "agent_max_steps_default": _AGENT_DEF,
                "assistant_max_rounds_default": _ROUNDS_DEF,
                "agent_max_steps_cap": _AGENT_CAP,
                "assistant_max_rounds_cap": _ROUNDS_CAP,
            }, ensure_ascii=False)

        if name == "update_ai_config":
            from services.ai_model_profiles import (
                get_active_profile_row,
                sync_legacy_user_ai_config_from_profile,
                upsert_active_profile_from_config,
            )

            db = await get_db()
            uid, err = _profile_tool_target_uid(user, arguments)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            if arguments.get("user_id") is not None and _is_admin(user):
                rows = await db.execute_fetchall("SELECT id FROM users WHERE id = ?", (uid,))
                if not rows:
                    return json.dumps({"success": False, "error": "用户不存在"}, ensure_ascii=False)
            patch = _profile_patch_from_tool_args(arguments)
            if not patch:
                return json.dumps({"success": False, "error": "未提供要更新的字段"}, ensure_ascii=False)
            try:
                await upsert_active_profile_from_config(db, uid, patch)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            prof = await get_active_profile_row(db, uid)
            if prof:
                await sync_legacy_user_ai_config_from_profile(db, uid, prof)
            await db.commit()
            try:
                from config import AGENT_MAX_STEPS_CAP as _AGENT_CAP
                from config import ASSISTANT_MAX_ROUNDS_CAP as _ROUNDS_CAP
            except Exception:
                _AGENT_CAP, _ROUNDS_CAP = 1000, 1000
            return json.dumps({
                "success": True,
                "message": "已更新当前激活的模型配置",
                "active_profile_id": int(prof["id"]) if prof else None,
                "applied": {
                    "agent_max_steps_cap": _AGENT_CAP,
                    "assistant_max_rounds_cap": _ROUNDS_CAP,
                },
            }, ensure_ascii=False)

        if name == "list_ai_model_profiles":
            from services.ai_model_profiles import list_profiles

            db = await get_db()
            uid, err = _profile_tool_target_uid(user, arguments)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            if arguments.get("user_id") is not None and _is_admin(user):
                rows = await db.execute_fetchall("SELECT id FROM users WHERE id = ?", (uid,))
                if not rows:
                    return json.dumps({"success": False, "error": "用户不存在"}, ensure_ascii=False)
            items, active_id = await list_profiles(db, uid)
            return json.dumps({
                "success": True,
                "profiles": items,
                "active_profile_id": active_id,
            }, ensure_ascii=False)

        if name == "create_ai_model_profile":
            from services.ai_model_profiles import (
                activate_profile,
                create_profile,
                get_active_profile_id,
                list_profiles,
                profile_row_to_tool_config,
                sync_legacy_user_ai_config_from_profile,
            )

            db = await get_db()
            uid, err = _profile_tool_target_uid(user, arguments)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            if arguments.get("user_id") is not None and _is_admin(user):
                rows = await db.execute_fetchall("SELECT id FROM users WHERE id = ?", (uid,))
                if not rows:
                    return json.dumps({"success": False, "error": "用户不存在"}, ensure_ascii=False)
            pname = (arguments.get("name") or "").strip()
            if not pname:
                return json.dumps({"success": False, "error": "缺少 name"}, ensure_ascii=False)
            fields = _profile_create_fields_from_tool_args(arguments)
            try:
                row = await create_profile(db, uid, pname, fields)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            set_active = bool(arguments.get("set_active"))
            active_id = await get_active_profile_id(db, uid)
            if set_active:
                await activate_profile(db, uid, int(row["id"]))
                row = dict(row)
                active_id = int(row["id"])
                await sync_legacy_user_ai_config_from_profile(db, uid, row)
                await db.commit()
            items, active_id = await list_profiles(db, uid)
            return json.dumps({
                "success": True,
                "message": "已创建模型配置" + ("并已设为当前模型" if set_active else "（未切换当前模型）"),
                "profile": profile_row_to_tool_config(row),
                "profiles": items,
                "active_profile_id": active_id,
            }, ensure_ascii=False)

        if name == "update_ai_model_profile":
            from services.ai_model_profiles import (
                get_active_profile_id,
                list_profiles,
                profile_row_to_tool_config,
                sync_legacy_user_ai_config_from_profile,
                update_profile,
            )

            db = await get_db()
            uid, err = _profile_tool_target_uid(user, arguments)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            row, resolve_err = await _profile_tool_resolve_row(db, uid, arguments)
            if resolve_err:
                return json.dumps({"success": False, "error": resolve_err}, ensure_ascii=False)
            patch = _profile_patch_from_tool_args(arguments)
            rename = (arguments.get("name") or "").strip() if "name" in arguments else None
            if not patch and rename is None:
                return json.dumps({"success": False, "error": "未提供要更新的字段"}, ensure_ascii=False)
            try:
                updated = await update_profile(db, uid, int(row["id"]), patch, name=rename)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            active_id = await get_active_profile_id(db, uid)
            if active_id is not None and int(active_id) == int(updated["id"]):
                await sync_legacy_user_ai_config_from_profile(db, uid, updated)
            await db.commit()
            items, active_id = await list_profiles(db, uid)
            return json.dumps({
                "success": True,
                "message": "已更新模型配置",
                "profile": profile_row_to_tool_config(updated),
                "profiles": items,
                "active_profile_id": active_id,
            }, ensure_ascii=False)

        if name == "activate_ai_model_profile":
            from services.ai_model_profiles import (
                activate_profile,
                get_profile_row,
                list_profiles,
                profile_row_to_tool_config,
                sync_legacy_user_ai_config_from_profile,
            )

            db = await get_db()
            uid, err = _profile_tool_target_uid(user, arguments)
            if err:
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            row, resolve_err = await _profile_tool_resolve_row(db, uid, arguments)
            if resolve_err:
                return json.dumps({"success": False, "error": resolve_err}, ensure_ascii=False)
            try:
                await activate_profile(db, uid, int(row["id"]))
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            updated = await get_profile_row(db, uid, int(row["id"]))
            assert updated is not None
            await sync_legacy_user_ai_config_from_profile(db, uid, updated)
            await db.commit()
            items, active_id = await list_profiles(db, uid)
            return json.dumps({
                "success": True,
                "message": "已切换当前模型配置",
                "profile": profile_row_to_tool_config(updated),
                "profiles": items,
                "active_profile_id": active_id,
            }, ensure_ascii=False)

        if name == "apply_system_ai_config_to_user":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "需要管理员权限"}, ensure_ascii=False)
            uid = arguments.get("user_id")
            if uid is None:
                return json.dumps({"success": False, "error": "缺少 user_id"}, ensure_ascii=False)
            uid = int(uid)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT id FROM users WHERE id = ?", (uid,))
            if not rows:
                return json.dumps({"success": False, "error": "用户不存在"}, ensure_ascii=False)
            keys = ["ai_api_key", "ai_base_url", "ai_model", "ai_system_prompt", "ai_auto_approve", "ai_assistant_enabled", "ai_context_size", "ai_agent_max_steps", "ai_assistant_max_rounds", "ai_output_locale"]
            raw = {}
            for k in keys:
                r = await db.execute_fetchall("SELECT value FROM settings WHERE key = ?", (k,))
                raw[k] = (r[0]["value"] if r else "") or ""
            await db.execute(
                """INSERT INTO user_ai_config (user_id, api_key, base_url, model, system_prompt, auto_approve, assistant_enabled, context_size, agent_max_steps, assistant_max_rounds, provider, vision_enabled, ai_output_locale, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(user_id) DO UPDATE SET
                   api_key=excluded.api_key, base_url=excluded.base_url, model=excluded.model,
                   system_prompt=excluded.system_prompt, auto_approve=excluded.auto_approve,
                   assistant_enabled=excluded.assistant_enabled, context_size=excluded.context_size,
                   agent_max_steps=excluded.agent_max_steps, assistant_max_rounds=excluded.assistant_max_rounds,
                   provider=excluded.provider, vision_enabled=excluded.vision_enabled, ai_output_locale=excluded.ai_output_locale, updated_at=CURRENT_TIMESTAMP""",
                (
                    uid,
                    (raw.get("ai_api_key") or "").strip(),
                    (raw.get("ai_base_url") or "").strip().rstrip("/"),
                    (raw.get("ai_model") or "").strip(),
                    (raw.get("ai_system_prompt") or "").strip(),
                    (raw.get("ai_auto_approve") or "false").strip().lower(),
                    (raw.get("ai_assistant_enabled") or "false").strip().lower(),
                    (raw.get("ai_context_size") or "0").strip(),
                    (raw.get("ai_agent_max_steps") or "").strip(),
                    (raw.get("ai_assistant_max_rounds") or "").strip(),
                    "",
                    "true",
                    (raw.get("ai_output_locale") or "").strip(),
                ),
            )
            await db.commit()
            return json.dumps({"success": True, "message": "已将系统默认 AI 配置应用到该用户"}, ensure_ascii=False)

        if name == "get_me":
            db = await get_db()
            mc = await load_user_mail_config(db, user["id"])
            mp = public_mail_config_for_api(mc, bool((mc.get("smtp_password") or "").strip()))
            tz = await get_effective_site_timezone(db)
            times = build_server_time_payload(tz)
            return json.dumps({
                "success": True,
                "user": {
                    "id": user.get("id"),
                    "username": user.get("username"),
                    "display_name": user.get("display_name"),
                    "role": user.get("role"),
                    "email": user.get("email") or "",
                    "mail_config": mp,
                    "user_mail_setup_hint": USER_MAIL_SETUP_HINT_ZH,
                },
                **times,
            }, ensure_ascii=False)

        if name == "get_user_mail_settings":
            db = await get_db()
            mc = await load_user_mail_config(db, user["id"])
            mp = public_mail_config_for_api(mc, bool((mc.get("smtp_password") or "").strip()))
            return json.dumps({"success": True, "config": mp, "setup_hint": USER_MAIL_SETUP_HINT_ZH}, ensure_ascii=False)

        if name == "update_user_mail_settings":
            patch = {}
            for k in (
                "mail_enabled", "smtp_host", "smtp_port", "smtp_user", "smtp_password",
                "smtp_from", "smtp_use_tls", "smtp_use_ssl",
            ):
                if k in arguments:
                    patch[k] = arguments.get(k)
            db = await get_db()
            ok, err, pub = await upsert_user_mail_from_patch(db, user["id"], patch)
            if not ok:
                return json.dumps({"success": False, "error": err, "config": pub}, ensure_ascii=False)
            return json.dumps({"success": True, "message": "已保存发信设置。", "config": pub}, ensure_ascii=False)

        if name == "send_email":
            to_raw = (arguments.get("to") or "").strip()
            subject = (arguments.get("subject") or "").strip()
            body = (arguments.get("body") or "").strip()
            body_html = (arguments.get("body_html") or "").strip()
            if not to_raw or not subject:
                return json.dumps({"success": False, "error": "to 与 subject 必填"}, ensure_ascii=False)
            if not body and not body_html:
                return json.dumps({"success": False, "error": "body 与 body_html 至少填一项"}, ensure_ascii=False)
            recipients = [x.strip() for x in to_raw.replace(";", ",").split(",") if x.strip()]
            bad = [x for x in recipients if "@" not in x]
            if bad:
                return json.dumps({"success": False, "error": f"无效收件人: {bad}"}, ensure_ascii=False)
            truncated_note = ""

            def _cap_text(text: str, cap: int, label: str) -> str:
                if cap <= 0 or len(text) <= cap:
                    return text
                note = (
                    f"\n\n---\n（{label}过长已截断：原文共 {len(text)} 字，邮件仅含前 {cap} 字。"
                    "完整内容请用 create_chat_artifact 或 fs_write_file 落盘后附链接。）"
                )
                return text[: max(0, cap - len(note))] + note

            try:
                from config import (
                    USER_SEND_EMAIL_BODY_MAX_CHARS as _mail_cap,
                    USER_SEND_EMAIL_HTML_MAX_CHARS as _html_cap,
                )
            except Exception:
                _mail_cap = 500_000
                _html_cap = 500_000
            _mail_cap = int(_mail_cap or 0)
            _html_cap = int(_html_cap or 0)
            if body and _mail_cap > 0 and len(body) > _mail_cap:
                body = _cap_text(body, _mail_cap, "纯文本正文")
                truncated_note = "（正文已按系统上限截断）"
            if body_html and _html_cap > 0 and len(body_html) > _html_cap:
                body_html = _cap_text(body_html, _html_cap, "HTML 正文")
                truncated_note = "（HTML 已按系统上限截断）"

            attachments, att_err = await resolve_user_mail_attachments(user, arguments.get("attachments"))
            if att_err:
                return json.dumps({"success": False, "error": att_err}, ensure_ascii=False)

            db = await get_db()
            okm, msg = await send_mail_as_user(
                db, user["id"], recipients, subject, body,
                body_html=body_html or None,
                attachments=attachments or None,
            )
            if not okm:
                return json.dumps({
                    "success": False,
                    "error": msg,
                    "setup_hint": USER_MAIL_SETUP_HINT_ZH,
                }, ensure_ascii=False)
            parts = [f"已发送至: {', '.join(recipients)}"]
            if body_html:
                parts.append("含 HTML 正文")
            if attachments:
                parts.append(f"附件 {len(attachments)} 个")
            ok_msg = "；".join(parts)
            if truncated_note:
                ok_msg += " " + truncated_note
            return json.dumps({"success": True, "message": ok_msg}, ensure_ascii=False)

        if name == "send_bind_email_code":
            email = (arguments.get("email") or "").strip().lower()
            if not email or "@" not in email:
                return json.dumps({"success": False, "error": "请输入有效邮箱地址"}, ensure_ascii=False)
            import random
            code = "".join(str(random.randint(0, 9)) for _ in range(6))
            from datetime import timedelta
            from services.email_sender import send_email_to_address
            db = await get_db()
            expires = datetime.now(timezone.utc) + timedelta(minutes=10)
            cursor = await db.execute(
                "INSERT INTO email_verification_codes (user_id, email, code, purpose, expires_at) VALUES (?, ?, ?, ?, ?)",
                (user["id"], email, code, "bind", expires.isoformat()),
            )
            await db.commit()
            code_id = cursor.lastrowid
            body = f"您好，\n\n您正在绑定 毛竹（Moso）账户邮箱，验证码为：{code}\n\n10 分钟内有效，请勿泄露。如非本人操作请忽略。"
            sent = await send_email_to_address(db, email, "毛竹（Moso）邮箱验证码", body)
            if not sent:
                await db.execute("DELETE FROM email_verification_codes WHERE id = ?", (code_id,))
                await db.commit()
                return json.dumps({"success": False, "error": "邮件发送失败，请检查系统 SMTP 配置"}, ensure_ascii=False)
            return json.dumps({"success": True, "message": "验证码已发送到该邮箱，请让用户查收后提供 6 位验证码，再调用 verify_bind_email 完成绑定。"}, ensure_ascii=False)

        if name == "verify_bind_email":
            email = (arguments.get("email") or "").strip().lower()
            code = (arguments.get("code") or "").strip()
            if not email or not code:
                return json.dumps({"success": False, "error": "请输入邮箱和验证码"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall(
                """SELECT id, expires_at FROM email_verification_codes
                   WHERE user_id = ? AND email = ? AND code = ? AND purpose = 'bind' AND used_at IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (user["id"], email, code),
            )
            if not rows:
                return json.dumps({"success": False, "error": "验证码错误或已使用，请重新获取"}, ensure_ascii=False)
            r = dict(rows[0])
            try:
                exp = datetime.fromisoformat(r["expires_at"].replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > exp:
                    return json.dumps({"success": False, "error": "验证码已过期，请重新获取"}, ensure_ascii=False)
            except (TypeError, ValueError):
                return json.dumps({"success": False, "error": "验证码无效"}, ensure_ascii=False)
            now = datetime.now(timezone.utc).isoformat()
            await db.execute("UPDATE email_verification_codes SET used_at = ? WHERE id = ?", (now, r["id"]))
            await db.execute("UPDATE users SET email = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (email, user["id"]))
            await db.commit()
            return json.dumps({"success": True, "message": "邮箱已绑定。"}, ensure_ascii=False)

        if name == "unbind_email":
            db = await get_db()
            await db.execute("UPDATE users SET email = '', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (user["id"],))
            await db.commit()
            return json.dumps({"success": True, "message": "邮箱已解绑。"}, ensure_ascii=False)

        if name == "local_exec":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import run_local_command_impl
            cmd = (arguments.get("command") or "").strip()
            timeout = int(arguments.get("timeout") or 60)
            cwd = (arguments.get("cwd") or "").strip() or None
            out, err, code = await run_local_command_impl(cmd, timeout=timeout, cwd=cwd)
            return json.dumps({
                "success": True, "stdout": out, "stderr": err, "returncode": code,
                "ui_action": {"action": "ensure_local_console", "scope": "local", "created_by": "ai"},
            }, ensure_ascii=False)

        if name == "local_run_script":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import run_local_script_impl
            code_str = (arguments.get("code") or "").strip()
            script_path = (arguments.get("script_path") or "").strip() or None
            timeout = int(arguments.get("timeout") or 120)
            out, err, code_ret = await run_local_script_impl(code=code_str, script_path=script_path or "", timeout=timeout)
            return json.dumps({
                "success": True, "stdout": out, "stderr": err, "returncode": code_ret,
                "ui_action": {"action": "ensure_local_console", "scope": "local", "created_by": "ai"},
            }, ensure_ascii=False)

        if name == "create_local_console":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api import local_host
            slot = local_host.next_local_terminal_slot(user["id"], terminal_scope_id)
            return json.dumps({
                "success": True,
                "message": f"已请求在本机管理页打开本机终端 slot {slot}（AI 创建），请稍候。",
                "slot": slot,
                "terminal_scope_id": terminal_scope_id,
                "ui_action": {"action": "create_local_console", "scope": "local", "created_by": "ai", "slot": slot},
            }, ensure_ascii=False)

        if name == "close_local_console":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            slot = arguments.get("slot")
            if slot is None:
                return json.dumps({"success": False, "error": "需要 slot 参数"}, ensure_ascii=False)
            try:
                slot = int(slot)
            except (TypeError, ValueError):
                return json.dumps({"success": False, "error": "slot 须为整数"}, ensure_ascii=False)
            return json.dumps({
                "success": True,
                "message": "已请求关闭本机控制台，仅会关闭由 AI 创建的控制台。",
                "ui_action": {"action": "close_local_console", "scope": "local", "slot": slot},
            }, ensure_ascii=False)

        if name == "local_fs_list":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import local_fs_list_impl
            path = (arguments.get("path") or "").strip()
            items = await local_fs_list_impl(path)
            return json.dumps({"success": True, "items": items}, ensure_ascii=False)

        if name == "local_fs_read":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import local_fs_read_impl
            path = (arguments.get("path") or "").strip()
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            try:
                content = await local_fs_read_impl(path)
                return json.dumps({"success": True, "content": content}, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        # 兼容旧工具名：历史上下文里偶发会生成 local_fs_write_file，
        # 统一按 local_fs_write 处理，避免因工具名漂移导致任务中断。
        if name in ("local_fs_write", "local_fs_write_file"):
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import local_fs_write_impl
            path = (arguments.get("path") or "").strip()
            content = arguments.get("content")
            if content is None:
                content = ""
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            try:
                # local_* 保持最大自由度：尊重调用方传入路径，不做强制改写。
                await local_fs_write_impl(path, str(content))
                return json.dumps({"success": True, "message": "已写入", "path": path}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "local_chat_write_file":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            path = (arguments.get("path") or "").strip()
            content = arguments.get("content")
            if content is None:
                content = ""
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            try:
                managed_rel = _chat_managed_relative_path(path, local_scope=True, fallback_ext=".txt")
                base = get_user_fs_root(user)
                out = await fs_write_file_async(
                    managed_rel,
                    content if isinstance(content, str) else str(content),
                    base,
                    mode="overwrite",
                )
                return json.dumps(
                    {
                        "success": True,
                        "message": "已写入",
                        "path": out.get("path"),
                        "requested_path": path,
                        "managed_relative_path": managed_rel,
                    },
                    ensure_ascii=False,
                )
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "local_fs_mkdir":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import local_fs_mkdir_impl
            path = (arguments.get("path") or "").strip()
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            try:
                await local_fs_mkdir_impl(path)
                return json.dumps({"success": True, "message": "已创建目录"}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "local_fs_delete":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import local_fs_delete_impl
            path = (arguments.get("path") or "").strip()
            recursive = bool(arguments.get("recursive"))
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            try:
                await local_fs_delete_impl(path, recursive=recursive)
                return json.dumps({"success": True, "message": "已删除"}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "local_fs_rename":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import local_fs_rename_impl
            src = (arguments.get("src") or "").strip()
            dst = (arguments.get("dst") or "").strip()
            if not src or not dst:
                return json.dumps({"success": False, "error": "缺少 src 或 dst"}, ensure_ascii=False)
            try:
                await local_fs_rename_impl(src, dst)
                return json.dumps({"success": True, "message": "已移动/重命名"}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "local_fs_truncate":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import local_fs_truncate_impl
            path = (arguments.get("path") or "").strip()
            size = int(arguments.get("size") or 0)
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            try:
                await local_fs_truncate_impl(path, size)
                return json.dumps({"success": True, "message": "已截断"}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "local_fs_read_binary":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import local_fs_read_binary_impl
            path = (arguments.get("path") or "").strip()
            offset = int(arguments.get("offset") or 0)
            size = arguments.get("size")
            encoding = (arguments.get("encoding") or "base64").strip().lower()
            if size is not None:
                size = int(size)
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            if encoding not in ("base64", "hex"):
                return json.dumps({"success": False, "error": "encoding 仅支持 base64 或 hex"}, ensure_ascii=False)
            try:
                content_b64 = await local_fs_read_binary_impl(path, offset=offset, size=size)
                if encoding == "hex":
                    try:
                        raw = base64.b64decode(content_b64.encode("ascii"), validate=True)
                    except Exception:
                        return json.dumps({"success": False, "error": "读取结果 base64 解码失败"}, ensure_ascii=False)
                    return json.dumps({"success": True, "content": raw.hex(), "encoding": "hex"}, ensure_ascii=False)
                return json.dumps({"success": True, "content": content_b64, "encoding": "base64"}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "local_fs_write_binary":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import local_fs_write_binary_impl
            path = (arguments.get("path") or "").strip()
            content = arguments.get("content") or ""
            offset = arguments.get("offset")
            if offset is not None:
                offset = int(offset)
            truncate = bool(arguments.get("truncate"))
            encoding = (arguments.get("encoding") or "base64").strip().lower()
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            if encoding not in ("base64", "hex"):
                return json.dumps({"success": False, "error": "encoding 仅支持 base64 或 hex"}, ensure_ascii=False)
            try:
                content_b64 = str(content)
                if encoding == "hex":
                    try:
                        raw = bytes.fromhex(content_b64.strip())
                    except Exception:
                        return json.dumps({"success": False, "error": "hex 内容非法"}, ensure_ascii=False)
                    content_b64 = base64.b64encode(raw).decode("ascii")
                await local_fs_write_binary_impl(path, content_b64, offset=offset, truncate=truncate)
                return json.dumps({"success": True, "message": "已写入", "encoding": encoding, "path": path}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "local_chat_write_binary":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            path = (arguments.get("path") or "").strip()
            content = arguments.get("content") or ""
            offset = arguments.get("offset")
            if offset is not None:
                offset = int(offset)
            truncate = bool(arguments.get("truncate"))
            encoding = (arguments.get("encoding") or "base64").strip().lower()
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            if encoding not in ("base64", "hex"):
                return json.dumps({"success": False, "error": "encoding 仅支持 base64 或 hex"}, ensure_ascii=False)
            try:
                content_b64 = str(content)
                if encoding == "hex":
                    try:
                        raw = bytes.fromhex(content_b64.strip())
                    except Exception:
                        return json.dumps({"success": False, "error": "hex 内容非法"}, ensure_ascii=False)
                    content_b64 = base64.b64encode(raw).decode("ascii")
                managed_rel = _chat_managed_relative_path(path, local_scope=True, fallback_ext=".bin")
                base = get_user_fs_root(user)
                out = await fs_write_binary_async(
                    managed_rel,
                    content_b64,
                    base,
                    offset=offset,
                    truncate=truncate,
                    encoding="base64",
                )
                return json.dumps(
                    {
                        "success": True,
                        "message": "已写入",
                        "encoding": encoding,
                        "path": out.get("path"),
                        "requested_path": path,
                        "managed_relative_path": managed_rel,
                    },
                    ensure_ascii=False,
                )
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "local_chat_data_paths":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            scope_val = (scope or "default").strip().lower() or "default"
            if scope_val != "local":
                return json.dumps({"success": False, "error": "仅本机管理会话可用"}, ensure_ascii=False)
            preview = (arguments.get("preview_subdir") or arguments.get("subdir") or "").strip()
            sub = _safe_optional_subdir_only(preview) if preview else ""
            now = datetime.now()
            date_dir = f"{now.year:04d}/{now.month:02d}/{now.day:02d}"
            base_rel = f"local/{date_dir}"
            base = get_user_fs_root(user)
            abs_base = (base / base_rel).resolve()
            abs_with_sub = (abs_base / sub).resolve() if sub else abs_base
            u = uuid4().hex
            ex_name = f"{u}-example.txt"
            ex_rel = f"{base_rel}/{sub}/{ex_name}" if sub else f"{base_rel}/{ex_name}"
            ex_abs = str((base / ex_rel).resolve()).replace("\\", "/")
            return json.dumps(
                {
                    "success": True,
                    "naming_convention": "相对 web/fs/<username>：local/年/月/日/[可自定子目录/]<uuid32hex>-功能名.扩展名。`local_chat_write_file` 的 path 只传「功能性子路径」（如 weather/a.html）；若误传已含 local/年/月/日 或当日纯日期前缀，实现会自动剥除一层以免重复。",
                    "default_policy": "本接口给出**推荐根目录**；在推荐目录下**子目录与文件名**可由 AI 按任务自行规划。未指定时**宜**将脚本**输出/临时数据**指到该目录。若用户**明确要求**从其它位置读取、写到其它位置、或**仅处理**其它数据，则按用户；一般情形可优先本目录。",
                    "for_script_output": "生成脚本时，把 print/文件写出/下载保存等**输出路径**设到本响应的 absolute_dir 或 suggested_cwd_for_shell 之下（可再分子目录）。**不要**在路径里再拼一层 `local/年/月/日`（推荐根已含该段）。",
                    "local_chat_write_path_hint": "调用 local_chat_write_file 时 path 只传例如 `weather/result.html`；勿传 `local/2026/04/28/weather/...`（易与自动前缀重复，虽已做去重仍应避免）。",
                    "date_yyyy_mm_dd": date_dir,
                    "fs_relative_dir": base_rel,
                    "fs_relative_dir_with_subdir": f"{base_rel}/{sub}" if sub else None,
                    "absolute_dir": str(abs_base).replace("\\", "/"),
                    "absolute_dir_with_subdir": str(abs_with_sub).replace("\\", "/") if sub else None,
                    "example_fs_relative_path": ex_rel,
                    "example_absolute_path": ex_abs,
                    "suggested_cwd_for_shell": str(abs_with_sub).replace("\\", "/"),
                },
                ensure_ascii=False,
            )

        if name == "process_start":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import process_start_impl
            command = (arguments.get("command") or "").strip()
            cwd = (arguments.get("cwd") or "").strip() or None
            env = arguments.get("env") if isinstance(arguments.get("env"), dict) else None
            if not command:
                return json.dumps({"success": False, "error": "缺少 command"}, ensure_ascii=False)
            try:
                out = await process_start_impl(command, cwd=cwd, env=env)
                return json.dumps({"success": True, **out}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "process_terminate":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import process_terminate_impl
            pid = arguments.get("pid")
            if pid is None:
                return json.dumps({"success": False, "error": "缺少 pid"}, ensure_ascii=False)
            try:
                await process_terminate_impl(int(pid), force=bool(arguments.get("force")))
                return json.dumps({"success": True, "message": "已终止"}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "process_wait":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import process_wait_impl
            pid = arguments.get("pid")
            if pid is None:
                return json.dumps({"success": False, "error": "缺少 pid"}, ensure_ascii=False)
            timeout = arguments.get("timeout")
            if timeout is not None:
                timeout = float(timeout)
            try:
                out = await process_wait_impl(int(pid), timeout=timeout)
                return json.dumps({"success": True, **out}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "process_stdin_write":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import process_stdin_write_impl
            pid = arguments.get("pid")
            data = arguments.get("data")
            if pid is None:
                return json.dumps({"success": False, "error": "缺少 pid"}, ensure_ascii=False)
            if data is None:
                data = ""
            try:
                await process_stdin_write_impl(int(pid), str(data))
                return json.dumps({"success": True, "message": "已写入 stdin"}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "process_stdin_close":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import process_stdin_close_impl
            pid = arguments.get("pid")
            if pid is None:
                return json.dumps({"success": False, "error": "缺少 pid"}, ensure_ascii=False)
            try:
                await process_stdin_close_impl(int(pid))
                return json.dumps({"success": True, "message": "已关闭 stdin"}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "process_stdout_read":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import process_stdout_read_impl
            pid = arguments.get("pid")
            if pid is None:
                return json.dumps({"success": False, "error": "缺少 pid"}, ensure_ascii=False)
            max_bytes = int(arguments.get("max_bytes") or 65536)
            try:
                content_b64 = await process_stdout_read_impl(int(pid), max_bytes=max_bytes)
                return json.dumps({"success": True, "content": content_b64}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "process_stderr_read":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import process_stderr_read_impl
            pid = arguments.get("pid")
            if pid is None:
                return json.dumps({"success": False, "error": "缺少 pid"}, ensure_ascii=False)
            max_bytes = int(arguments.get("max_bytes") or 65536)
            try:
                content_b64 = await process_stderr_read_impl(int(pid), max_bytes=max_bytes)
                return json.dumps({"success": True, "content": content_b64}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "process_list":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "本机管理仅管理员可用"}, ensure_ascii=False)
            from api.local_host import process_list_impl
            try:
                procs = await process_list_impl()
                return json.dumps({"success": True, "processes": procs}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "list_users":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "需要管理员权限"}, ensure_ascii=False)
            from api.users import _user_public_dict

            db = await get_db()
            rows = await db.execute_fetchall(
                "SELECT id, username, display_name, role, status, locked_until, created_at, last_login FROM users ORDER BY id"
            )
            return json.dumps(
                {"success": True, "users": [_user_public_dict(dict(r)) for r in rows]},
                ensure_ascii=False,
            )

        if name == "get_user":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "需要管理员权限"}, ensure_ascii=False)
            uid = arguments.get("user_id")
            if uid is None:
                return json.dumps({"success": False, "error": "缺少 user_id"}, ensure_ascii=False)
            from api.users import _user_public_dict

            db = await get_db()
            rows = await db.execute_fetchall(
                "SELECT id, username, display_name, role, status, locked_until, created_at, last_login FROM users WHERE id = ?",
                (uid,),
            )
            if not rows:
                return json.dumps({"success": False, "error": f"用户 ID={uid} 不存在"}, ensure_ascii=False)
            return json.dumps({"success": True, "user": _user_public_dict(dict(rows[0]))}, ensure_ascii=False)

        if name == "create_user":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "需要管理员权限"}, ensure_ascii=False)
            username_ = (arguments.get("username") or "").strip()
            password_ = arguments.get("password") or ""
            if not username_ or not password_:
                return json.dumps({"success": False, "error": "username 和 password 必填"}, ensure_ascii=False)
            import bcrypt
            pw_hash = await asyncio.to_thread(
                lambda: bcrypt.hashpw(password_.encode(), bcrypt.gensalt()).decode()
            )
            db = await get_db()
            try:
                await db.execute(
                    "INSERT INTO users (username, display_name, password_hash, role) VALUES (?, ?, ?, ?)",
                    (username_, (arguments.get("display_name") or username_).strip(), pw_hash, (arguments.get("role") or "user").strip()),
                )
                await db.commit()
                cur = await db.execute("SELECT last_insert_rowid()")
                uid = (await cur.fetchone())[0]
                return json.dumps({"success": True, "id": uid}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": "用户名已存在" if "UNIQUE" in str(e) else str(e)}, ensure_ascii=False)

        if name == "update_user":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "需要管理员权限"}, ensure_ascii=False)
            uid = arguments.get("user_id")
            if uid is None:
                return json.dumps({"success": False, "error": "缺少 user_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT id, role FROM users WHERE id = ?", (uid,))
            if not rows:
                return json.dumps({"success": False, "error": f"用户 ID={uid} 不存在"}, ensure_ascii=False)
            target = dict(rows[0])
            if str(target.get("role")).strip().lower() == "admin" and str(user.get("role")).strip().lower() != "admin":
                return json.dumps({"success": False, "error": "无法修改内置管理员"}, ensure_ascii=False)
            updates, params = [], []
            for f in ("display_name", "role"):
                if f in arguments and arguments[f] is not None:
                    updates.append(f"{f} = ?")
                    params.append(arguments[f])
            if "status" in arguments and arguments["status"] is not None:
                raw = str(arguments["status"]).strip().lower()
                if raw == "disabled":
                    raw = "suspended"
                if raw == "locked":
                    return json.dumps(
                        {
                            "success": False,
                            "error": "不可将 status 设为 locked；安全锁定仅系统自动。暂停请用 suspended，解除锁定请管理员在界面「解锁」或使用专门接口。",
                        },
                        ensure_ascii=False,
                    )
                if raw not in ("active", "suspended"):
                    return json.dumps({"success": False, "error": "status 仅可为 active 或 suspended"}, ensure_ascii=False)
                updates.append("status = ?")
                params.append(raw)
                if raw == "suspended":
                    updates.append("locked_until = NULL")
                    updates.append("failed_login_attempts = 0")
                else:
                    updates.append("locked_until = NULL")
                    updates.append("failed_login_attempts = 0")
            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                params.append(uid)
                await db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
                await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "admin_unlock_user":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "需要管理员权限"}, ensure_ascii=False)
            uid = arguments.get("user_id")
            if uid is None:
                return json.dumps({"success": False, "error": "缺少 user_id"}, ensure_ascii=False)
            from api.users import perform_admin_security_unlock

            db = await get_db()
            ok, err = await perform_admin_security_unlock(db, int(uid))
            if not ok:
                return json.dumps({"success": False, "error": err or "解锁失败"}, ensure_ascii=False)
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "delete_user":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "需要管理员权限"}, ensure_ascii=False)
            uid = arguments.get("user_id")
            if uid is None:
                return json.dumps({"success": False, "error": "缺少 user_id"}, ensure_ascii=False)
            from api.users import delete_user_with_cascade
            db = await get_db()
            ok, err = await delete_user_with_cascade(db, int(uid))
            if not ok:
                return json.dumps({"success": False, "error": err or "删除失败"}, ensure_ascii=False)
            await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "reset_user_password":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "需要管理员权限"}, ensure_ascii=False)
            uid = arguments.get("user_id")
            password_ = arguments.get("password")
            if uid is None or not password_:
                return json.dumps({"success": False, "error": "需要 user_id 和 password"}, ensure_ascii=False)
            import bcrypt
            pw_hash = await asyncio.to_thread(
                lambda: bcrypt.hashpw(password_.encode(), bcrypt.gensalt()).decode()
            )
            db = await get_db()
            await db.execute("UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (pw_hash, uid))
            await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "reset_user_system_ai_usage":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "需要管理员权限"}, ensure_ascii=False)
            uid = arguments.get("user_id")
            if uid is None:
                return json.dumps({"success": False, "error": "需要 user_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT id FROM users WHERE id = ?", (uid,))
            if not rows:
                return json.dumps({"success": False, "error": f"用户 ID={uid} 不存在"}, ensure_ascii=False)
            await db.execute("DELETE FROM user_system_ai_usage WHERE user_id = ?", (uid,))
            await db.commit()
            return json.dumps({"success": True, "message": f"已重置用户 ID={uid} 的系统共享 Key 计数，该用户可继续使用共享 Key 至配额上限"}, ensure_ascii=False)

        if name == "change_my_password":
            old_pw = (arguments.get("old_password") or "").strip()
            new_pw = (arguments.get("new_password") or "").strip()
            if not old_pw or not new_pw:
                return json.dumps({"success": False, "error": "需要 old_password 和 new_password"}, ensure_ascii=False)
            if len(new_pw) < 6:
                return json.dumps({"success": False, "error": "新密码至少 6 个字符"}, ensure_ascii=False)
            import bcrypt
            db = await get_db()
            rows = await db.execute_fetchall("SELECT password_hash FROM users WHERE id = ?", (user["id"],))
            if not rows:
                return json.dumps({"success": False, "error": "用户不存在"}, ensure_ascii=False)
            raw = rows[0]["password_hash"]
            current_hash = raw.encode("utf-8") if isinstance(raw, str) else (raw or b"")
            ok = False
            if current_hash:
                ok = await asyncio.to_thread(bcrypt.checkpw, old_pw.encode("utf-8"), current_hash)
            if not ok:
                return json.dumps({"success": False, "error": "当前密码错误"}, ensure_ascii=False)
            new_hash = await asyncio.to_thread(
                lambda: bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
            )
            await db.execute("UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_hash, user["id"]))
            await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "list_ai_sessions":
            db = await get_db()
            rows = await db.execute_fetchall(
                "SELECT id, title, created_at, updated_at FROM ai_chat_sessions WHERE user_id = ? ORDER BY updated_at DESC",
                (user["id"],),
            )
            return json.dumps({"success": True, "sessions": [dict(r) for r in rows]}, ensure_ascii=False)

        if name == "get_ai_session":
            sid = arguments.get("session_id")
            if sid is None:
                return json.dumps({"success": False, "error": "缺少 session_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall(
                "SELECT id, title, created_at, updated_at FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
                (sid, user["id"]),
            )
            if not rows:
                return json.dumps({"success": False, "error": "会话不存在或无权访问"}, ensure_ascii=False)
            sess = dict(rows[0])
            msg_rows = await db.execute_fetchall(
                "SELECT id, role, content, created_at FROM ai_chat_messages WHERE session_id = ? ORDER BY id ASC",
                (sid,),
            )
            try:
                from services.chat_utils import strip_ui_action_sentinels as _strip_uia
            except Exception:
                _strip_uia = None
            sess["messages"] = []
            for m in msg_rows:
                d = dict(m)
                if _strip_uia and (d.get("role") == "assistant"):
                    d["content"] = _strip_uia(d.get("content") or "")
                sess["messages"].append(d)
            return json.dumps({"success": True, "session": sess}, ensure_ascii=False)

        if name == "create_ai_session":
            db = await get_db()
            title = (arguments.get("title") or "").strip()[:200]
            if not title or title in EDGEOPS_SESSION_TITLE_CLIENT_PLACEHOLDERS:
                title = (EDGEOPS_TEMP_SESSION_PREFIX + datetime.now().strftime("%Y%m%d%H%M%S"))[:200]
            await db.execute("INSERT INTO ai_chat_sessions (user_id, title) VALUES (?, ?)", (user["id"], title))
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            sid = (await cur.fetchone())[0]
            return json.dumps({"success": True, "session_id": sid}, ensure_ascii=False)

        if name == "update_ai_session":
            sid = arguments.get("session_id")
            title = arguments.get("title")
            if sid is None or not title:
                return json.dumps({"success": False, "error": "需要 session_id 和 title"}, ensure_ascii=False)
            db = await get_db()
            await db.execute(
                "UPDATE ai_chat_sessions SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                (title[:200], sid, user["id"]),
            )
            await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "delete_ai_session":
            sid = arguments.get("session_id")
            if sid is None:
                return json.dumps({"success": False, "error": "缺少 session_id"}, ensure_ascii=False)
            db = await get_db()
            await db.execute("DELETE FROM ai_chat_sessions WHERE id = ? AND user_id = ?", (sid, user["id"]))
            await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "clear_ai_sessions":
            db = await get_db()
            await db.execute("DELETE FROM ai_chat_sessions WHERE user_id = ?", (user["id"],))
            await db.commit()
            return json.dumps({"success": True}, ensure_ascii=False)

        if name == "update_session_prompt":
            sid = arguments.get("session_id")
            content = (arguments.get("content") or "").strip()
            append = arguments.get("append") is True
            if sid is None:
                return json.dumps({"success": False, "error": "缺少 session_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall(
                "SELECT COALESCE(session_prompt, '') AS session_prompt FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
                (sid, user["id"]),
            )
            if not rows:
                return json.dumps({"success": False, "error": "会话不存在或无权操作"}, ensure_ascii=False)
            existing = (rows[0]["session_prompt"] or "").strip()
            new_prompt = (existing + "\n\n" + content).strip()[:50000] if append and existing else content[:50000]
            await db.execute(
                "UPDATE ai_chat_sessions SET session_prompt = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                (new_prompt, sid, user["id"]),
            )
            await db.commit()
            return json.dumps({"success": True, "message": "已更新会话级提示词"}, ensure_ascii=False)

        if name == "get_session_operations":
            sid = arguments.get("session_id")
            limit = max(1, min(200, int(arguments.get("limit") or 50)))
            if sid is None:
                return json.dumps({"success": False, "error": "缺少 session_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall(
                "SELECT id, user_id FROM ai_chat_sessions WHERE id = ?", (sid,)
            )
            if not rows or rows[0]["user_id"] != user["id"]:
                return json.dumps({"success": False, "error": "会话不存在或无权访问"}, ensure_ascii=False)
            msg_rows = await db.execute_fetchall(
                """SELECT role, content, created_at FROM ai_chat_messages
                   WHERE session_id = ? ORDER BY id ASC LIMIT ?""",
                (sid, limit),
            )
            items = []
            for r in msg_rows:
                role = r["role"]
                content = (r["content"] or "").strip()
                if role == "assistant":
                    content = assistant_content_for_summary(r["content"] or "")
                items.append({"role": role, "content": content, "created_at": r["created_at"] or ""})
            return json.dumps({"success": True, "operations": items}, ensure_ascii=False)

        if name == "get_session_chat_detail":
            sid = arguments.get("session_id")
            include_tool_results = arguments.get("include_tool_results") is True
            limit = max(1, min(200, int(arguments.get("limit") or 50)))
            if sid is None:
                return json.dumps({"success": False, "error": "缺少 session_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall(
                "SELECT id, user_id FROM ai_chat_sessions WHERE id = ?", (sid,)
            )
            if not rows or rows[0]["user_id"] != user["id"]:
                return json.dumps({"success": False, "error": "会话不存在或无权访问"}, ensure_ascii=False)
            msg_rows = await db.execute_fetchall(
                """SELECT role, content, created_at FROM ai_chat_messages
                   WHERE session_id = ? ORDER BY id ASC LIMIT ?""",
                (sid, limit),
            )
            from services.chat_utils import assistant_content_for_chat_detail

            items = []
            for r in msg_rows:
                role = r["role"]
                created_at = r["created_at"] or ""
                if role == "assistant":
                    detail = assistant_content_for_chat_detail(
                        r["content"] or "",
                        include_tool_results=include_tool_results,
                    )
                    item = {
                        "role": role,
                        "content": detail.get("content") or "",
                        "created_at": created_at,
                    }
                    if include_tool_results:
                        item["tool_trace"] = detail.get("tool_trace") or []
                        item["tool_trace_step_count"] = int(detail.get("tool_trace_step_count") or 0)
                    items.append(item)
                else:
                    items.append(
                        {
                            "role": role,
                            "content": (r["content"] or "").strip(),
                            "created_at": created_at,
                        }
                    )
            return json.dumps(
                {
                    "success": True,
                    "messages": items,
                    "include_tool_results": include_tool_results,
                    "note": (
                        "历史注入给模型时默认剥离工具轨迹；本接口 include_tool_results=true 会返回解码后的 tool_trace，"
                        "用于回答「怎么查到的/调了哪些工具」。"
                        if include_tool_results
                        else "当前为指令摘要模式；需要工具轨迹请设 include_tool_results=true。"
                    ),
                },
                ensure_ascii=False,
            )

        if name == "ensure_chat_tools":
            from services.agent_optimize import (
                CAPABILITY_FULL,
                KNOWN_CAPABILITIES,
                allow_set_for_capabilities,
                expand_allow_for_tools,
                resolve_capabilities_for_tools,
            )

            raw_names = arguments.get("tool_names") or []
            if isinstance(raw_names, str):
                raw_names = [x.strip() for x in raw_names.split(",") if x.strip()]
            tool_names = [str(x).strip() for x in raw_names if str(x).strip()]
            raw_caps = arguments.get("capabilities") or []
            if isinstance(raw_caps, str):
                raw_caps = [x.strip() for x in raw_caps.split(",") if x.strip()]
            capabilities = []
            for c in raw_caps:
                c0 = str(c or "").strip().lower()
                if not c0:
                    continue
                if c0 in ("all", "full"):
                    capabilities.append(CAPABILITY_FULL)
                elif c0 in KNOWN_CAPABILITIES or c0 == "ops":
                    capabilities.append(c0)
            if not tool_names and not capabilities:
                return json.dumps(
                    {
                        "success": False,
                        "error": "请提供 tool_names 和/或 capabilities（terminal|fs|http|host_transfer|full）",
                    },
                    ensure_ascii=False,
                )
            caps = set(capabilities) | resolve_capabilities_for_tools(tool_names)
            if CAPABILITY_FULL in caps:
                recovery = {
                    "capabilities": [CAPABILITY_FULL],
                    "tool_names": tool_names,
                    "tier_label": "full",
                    "force_full": True,
                }
                return json.dumps(
                    {
                        "success": True,
                        "message": "已请求装载全量工具；下一轮推理即可使用。请继续执行用户任务，勿再声称缺少工具。",
                        "capabilities": [CAPABILITY_FULL],
                        "requested_tools": tool_names,
                        "tier_label": "full",
                        "edgeops_tools_recovery": recovery,
                    },
                    ensure_ascii=False,
                )
            plan = expand_allow_for_tools(tool_names) if tool_names else {
                "recoverable": True,
                "capabilities": sorted(caps),
                "allow": allow_set_for_capabilities(caps),
                "tier_label": "+".join(["core"] + sorted(c for c in caps if c != "core")) if caps else "core",
                "needed": [],
                "missing_in_catalog": [],
            }
            if tool_names and caps:
                # 合并显式 capabilities
                merged_caps = set(plan.get("capabilities") or []) | caps
                plan = {
                    **plan,
                    "capabilities": sorted(merged_caps),
                    "allow": allow_set_for_capabilities(merged_caps),
                    "tier_label": (
                        "full"
                        if CAPABILITY_FULL in merged_caps
                        else "+".join(["core"] + sorted(c for c in merged_caps if c != "core"))
                    ),
                }
            allow = plan.get("allow")
            force_full = allow is None or (plan.get("tier_label") == "full")
            recovery = {
                "capabilities": list(plan.get("capabilities") or sorted(caps)),
                "tool_names": tool_names,
                "tier_label": plan.get("tier_label") or "core",
                "force_full": bool(force_full),
            }
            loaded_hint = ", ".join(recovery["capabilities"]) or "core"
            return json.dumps(
                {
                    "success": True,
                    "message": (
                        f"已请求装载能力 [{loaded_hint}]"
                        + (f"（含 {', '.join(tool_names)}）" if tool_names else "")
                        + "。下一轮推理即可调用相应工具；请继续执行用户任务，禁止再声称缺少工具。"
                    ),
                    "capabilities": recovery["capabilities"],
                    "requested_tools": tool_names,
                    "tier_label": recovery["tier_label"],
                    "approx_allow_count": None if force_full else len(allow or []),
                    "edgeops_tools_recovery": recovery,
                },
                ensure_ascii=False,
            )

        if name == "get_best_practices":
            db = await get_db()
            category = (arguments.get("category") or "").strip() or None
            keyword = (arguments.get("keyword") or "").strip() or None
            query = "SELECT id, title, category, content, source, created_at, updated_at, created_by FROM best_practices WHERE 1=1"
            params = []
            if not _is_admin(user):
                query += " AND created_by = ?"
                params.append(user["id"])
            if category:
                query += " AND category = ?"
                params.append(category)
            if keyword:
                query += " AND (title LIKE ? OR content LIKE ? OR category LIKE ?)"
                q = "%" + keyword + "%"
                params.extend([q, q, q])
            query += " ORDER BY updated_at DESC LIMIT 100"
            rows = await db.execute_fetchall(query, params)
            return json.dumps({"success": True, "items": [dict(r) for r in rows]}, ensure_ascii=False)

        if name == "add_best_practice":
            title = (arguments.get("title") or "").strip()
            content = (arguments.get("content") or "").strip()
            if not title or not content:
                return json.dumps({"success": False, "error": "标题和内容必填"}, ensure_ascii=False)
            category = (arguments.get("category") or "").strip()[:100]
            source = (arguments.get("source") or "ai_solved").strip()[:50]
            db = await get_db()
            await db.execute(
                """INSERT INTO best_practices (title, category, content, source, created_by, updated_at)
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (title, category, content, source, user["id"]),
            )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            rid = (await cur.fetchone())[0]
            return json.dumps({"success": True, "id": rid, "message": "已添加到最佳实践"}, ensure_ascii=False)

        if name == "update_best_practice":
            item_id = arguments.get("id")
            if item_id is None:
                return json.dumps({"success": False, "error": "缺少 id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT id, created_by FROM best_practices WHERE id = ?", (item_id,))
            if not rows:
                return json.dumps({"success": False, "error": f"记录 ID={item_id} 不存在"}, ensure_ascii=False)
            if not _is_admin(user) and rows[0]["created_by"] != user["id"]:
                return json.dumps({"success": False, "error": "无权修改该记录"}, ensure_ascii=False)
            updates, params = [], []
            if arguments.get("title") is not None:
                updates.append("title = ?")
                params.append((arguments.get("title") or "").strip())
            if arguments.get("category") is not None:
                updates.append("category = ?")
                params.append((arguments.get("category") or "").strip()[:100])
            if arguments.get("content") is not None:
                updates.append("content = ?")
                params.append((arguments.get("content") or "").strip())
            if updates:
                params.append(item_id)
                await db.execute(
                    f"UPDATE best_practices SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    params,
                )
                await db.commit()
            return json.dumps({"success": True, "message": "已更新"}, ensure_ascii=False)

        if name == "delete_best_practice":
            item_id = arguments.get("id")
            if item_id is None:
                return json.dumps({"success": False, "error": "缺少 id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT id, created_by FROM best_practices WHERE id = ?", (item_id,))
            if not rows:
                return json.dumps({"success": False, "error": "记录不存在"}, ensure_ascii=False)
            if not _is_admin(user) and rows[0]["created_by"] != user["id"]:
                return json.dumps({"success": False, "error": "无权删除该记录"}, ensure_ascii=False)
            await db.execute("DELETE FROM best_practices WHERE id = ?", (item_id,))
            await db.commit()
            return json.dumps({"success": True, "message": "已删除"}, ensure_ascii=False)

        # ── AI 帮助文档（web/aihelp）：用户只读，仅管理员可写/更新 index
        async def _aihelp_read_payload(arguments: dict, *, default_path: str) -> dict:
            path_arg = (arguments.get("path") or default_path).strip()
            try:
                text = await read_aihelp_text_async(path_arg)
            except FileNotFoundError:
                if default_path == "index.md":
                    text = "# AI 帮助文档\n\n（暂无目录，请联系管理员维护 web/aihelp/index.md）\n"
                else:
                    raise
            section_path = arguments.get("section_path")
            if section_path is not None and not isinstance(section_path, list):
                section_path = None
            payload = read_markdown_document(
                text,
                sections_only=bool(arguments.get("sections_only")),
                max_level=arguments.get("max_level") if arguments.get("max_level") is not None else 6,
                section_index=arguments.get("section_index"),
                section_path=section_path,
                heading=arguments.get("heading"),
                case_insensitive=bool(arguments.get("case_insensitive")),
                max_chars=arguments.get("max_chars"),
                include_heading=arguments.get("include_heading") is not False,
                include_children=arguments.get("include_children") is not False,
            )
            payload["path"] = path_arg
            return payload

        if name == "get_aihelp_index":
            try:
                payload = await _aihelp_read_payload(arguments, default_path="index.md")
                return json.dumps({"success": True, **payload}, ensure_ascii=False)
            except FileNotFoundError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "list_aihelp_files":
            try:
                files = await asyncio.to_thread(list_aihelp_md_paths_sync)
                return json.dumps({"success": True, "files": files}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "get_aihelp_file":
            path_arg = (arguments.get("path") or "").strip()
            if not path_arg:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            try:
                payload = await _aihelp_read_payload(arguments, default_path=path_arg)
                return json.dumps({"success": True, **payload}, ensure_ascii=False)
            except FileNotFoundError:
                return json.dumps({"success": False, "error": "文件不存在"}, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "write_aihelp_file":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "仅管理员可编辑帮助文档"}, ensure_ascii=False)
            path_arg = (arguments.get("path") or "").strip()
            content = arguments.get("content")
            if not path_arg:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            if content is None:
                content = ""
            try:
                resolved = resolve_aihelp_path(path_arg)
                await asyncio.to_thread(resolved.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(resolved.write_text, content if isinstance(content, str) else str(content), encoding="utf-8")
                return json.dumps({"success": True, "message": f"已写入 {path_arg}，请记得维护 index.md 目录"}, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "update_aihelp_index":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "仅管理员可更新帮助目录 index.md"}, ensure_ascii=False)
            content = arguments.get("content")
            if content is None:
                content = ""
            try:
                base = Path(getattr(config, "AIHELP_DIR", None) or (Path(config.BASE_DIR) / "web" / "aihelp"))
                await asyncio.to_thread(base.mkdir, parents=True, exist_ok=True)
                index_file = base / "index.md"
                await asyncio.to_thread(index_file.write_text, content if isinstance(content, str) else str(content), encoding="utf-8")
                return json.dumps({"success": True, "message": "已更新 index.md"}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        async def _load_markdown_source_text(arguments: dict) -> tuple[str, str, str]:
            file_root = (arguments.get("file_root") or "fs").strip().lower()
            path_arg = (arguments.get("path") or "").strip()
            if not path_arg:
                raise ValueError("缺少 path")
            if file_root == "aihelp":
                text = await read_aihelp_text_async(path_arg)
                return text, file_root, path_arg
            if file_root == "skill":
                slug = (arguments.get("skill_name") or "").strip()
                if not slug:
                    raise ValueError("file_root=skill 时需要 skill_name")
                from services.user_skills_registry import read_skill_resource_file

                text = read_skill_resource_file(user, slug, path_arg)
                return text, file_root, path_arg
            if file_root == "fs":
                guard = _skill_fs_path_guard(path_arg)
                if guard:
                    raise ValueError(guard)
                base = get_user_fs_root(user)
                out = await fs_read_file_async(path_arg, base, offset=0, size=None)
                if not out.get("success"):
                    raise ValueError("读取失败")
                return out.get("content") or "", file_root, path_arg
            raise ValueError("file_root 须为 fs、aihelp 或 skill")

        async def _save_markdown_source_text(arguments: dict, text: str) -> None:
            file_root = (arguments.get("file_root") or "fs").strip().lower()
            path_arg = (arguments.get("path") or "").strip()
            if file_root == "aihelp":
                if not _is_admin(user):
                    raise PermissionError("仅管理员可修改 aihelp")
                resolved = resolve_aihelp_path(path_arg)
                await asyncio.to_thread(resolved.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(resolved.write_text, text, encoding="utf-8")
                return
            if file_root == "skill":
                slug = (arguments.get("skill_name") or "").strip()
                if not slug:
                    raise ValueError("file_root=skill 时需要 skill_name")
                from services.user_skills_registry import write_skill_resource_file

                write_skill_resource_file(user, slug, path_arg, text)
                return
            if file_root == "fs":
                guard = _skill_fs_path_guard(path_arg)
                if guard:
                    raise ValueError(guard)
                base = get_user_fs_root(user)
                await fs_write_file_async(path_arg, text, base, mode="overwrite")
                return
            raise ValueError("file_root 须为 fs、aihelp 或 skill")

        if name == "markdown_search_sections":
            query = (arguments.get("query") or arguments.get("q") or "").strip()
            if not query:
                return json.dumps({"success": False, "error": "缺少 query"}, ensure_ascii=False)
            try:
                file_root = (arguments.get("file_root") or "aihelp").strip().lower()
                path_arg = (arguments.get("path") or "").strip()
                scope = arguments.get("scope") or "all"
                _search_kw = dict(
                    scope=scope,
                    regex=bool(arguments.get("regex")),
                    case_insensitive=arguments.get("case_insensitive") is not False,
                    max_level=arguments.get("max_level") or 6,
                    max_hits=arguments.get("max_hits") or 30,
                    snippet_chars=arguments.get("snippet_chars") or 200,
                )
                if file_root == "aihelp" and not path_arg:
                    rels = await asyncio.to_thread(list_aihelp_md_paths_sync)
                    max_files = int(getattr(config, "MARKDOWN_SECTIONS_SEARCH_MAX_FILES", 100))
                    pairs: list[tuple[str, str]] = []
                    for rel in rels[:max_files]:
                        try:
                            pairs.append((rel, await read_aihelp_text_async(rel)))
                        except (FileNotFoundError, ValueError):
                            continue
                    out = search_markdown_corpus(pairs, query, **_search_kw)
                elif file_root == "fs":
                    from services.user_memory import list_fs_markdown_under

                    base = get_user_fs_root(user)
                    rel_try = coerce_fs_relative_path(path_arg or "", base) if path_arg else ""
                    is_dir_corpus = False
                    if not path_arg:
                        is_dir_corpus = True
                        corpus_root = ""
                    else:
                        try:
                            resolved = resolve_fs_path(rel_try, base)
                            is_dir_corpus = resolved.exists() and resolved.is_dir()
                            corpus_root = rel_try
                        except (ValueError, OSError):
                            is_dir_corpus = False
                            corpus_root = rel_try
                    if is_dir_corpus:
                        pairs = await list_fs_markdown_under(user, corpus_root)
                        out = search_markdown_corpus(pairs, query, **_search_kw)
                        out["path"] = corpus_root or "/"
                        out["corpus"] = True
                    else:
                        text, file_root, path_arg = await _load_markdown_source_text(arguments)
                        out = search_markdown_sections(text, query, **_search_kw)
                        if path_arg:
                            out["path"] = path_arg
                else:
                    text, file_root, path_arg = await _load_markdown_source_text(arguments)
                    out = search_markdown_sections(text, query, **_search_kw)
                    if path_arg:
                        out["path"] = path_arg
                out["success"] = True
                out["file_root"] = file_root
                return json.dumps(out, ensure_ascii=False)
            except (ValueError, PermissionError) as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            except FileNotFoundError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "markdown_list_sections":
            try:
                text, file_root, path_arg = await _load_markdown_source_text(arguments)
                out = list_markdown_sections(
                    text,
                    max_level=arguments.get("max_level") if arguments.get("max_level") is not None else 6,
                    include_preamble=bool(arguments.get("include_preamble")),
                )
                out["success"] = True
                out["file_root"] = file_root
                out["path"] = path_arg
                return json.dumps(out, ensure_ascii=False)
            except (ValueError, PermissionError) as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            except FileNotFoundError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "markdown_read_section":
            try:
                text, file_root, path_arg = await _load_markdown_source_text(arguments)
                section_path = arguments.get("section_path")
                if section_path is not None and not isinstance(section_path, list):
                    section_path = None
                out = get_markdown_section(
                    text,
                    section_index=arguments.get("section_index"),
                    section_path=section_path,
                    heading=arguments.get("heading"),
                    case_insensitive=bool(arguments.get("case_insensitive")),
                    max_chars=arguments.get("max_chars"),
                    include_heading=arguments.get("include_heading") is not False,
                    include_children=arguments.get("include_children") is not False,
                )
                out["success"] = True
                out["file_root"] = file_root
                out["path"] = path_arg
                return json.dumps(out, ensure_ascii=False)
            except (ValueError, PermissionError) as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            except FileNotFoundError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "markdown_replace_section":
            if arguments.get("new_content") is None:
                return json.dumps({"success": False, "error": "缺少 new_content"}, ensure_ascii=False)
            try:
                text, file_root, path_arg = await _load_markdown_source_text(arguments)
                section_path = arguments.get("section_path")
                if section_path is not None and not isinstance(section_path, list):
                    section_path = None
                rep = replace_markdown_section(
                    text,
                    str(arguments.get("new_content") or ""),
                    section_index=arguments.get("section_index"),
                    section_path=section_path,
                    heading=arguments.get("heading"),
                    case_insensitive=bool(arguments.get("case_insensitive")),
                    mode=arguments.get("mode") or "replace_body",
                )
                await _save_markdown_source_text(arguments, rep["content"])
                rep["success"] = True
                rep["file_root"] = file_root
                rep["path"] = path_arg
                rep.pop("content", None)
                rep["message"] = "已写回文件"
                return json.dumps(rep, ensure_ascii=False)
            except (ValueError, PermissionError) as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            except FileNotFoundError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        # ── 文件系统（web/fs）──
        if name == "get_chats_workspace_dir":
            from api.chat_attachments import get_chats_workspace_dir as _gcwd

            info = _gcwd(user, session_id)
            return json.dumps(
                {
                    "success": True,
                    "storage_subdir": info["storage_subdir"],
                    "utc_date_subdir": info["storage_subdir"],  # 兼容旧字段名
                    "chats_workspace_relative_prefix": info["chats_workspace_relative_prefix"],
                    "layout": info["layout"],
                    "session_id": session_id,
                    "filename_format": "{UUID}-{kebab-or-safe-ascii-desc}.{ext}",
                    "notes": (
                        "默认会话区为 chats/sessions/<session_id>/（附件、spill、session_managed 写入）。"
                        "旧 chats/YYYY/MM/DD/ 仍可读取。"
                        "写入 scripts/、exchange/ 等指定路径时传完整相对 path；"
                        "强制归位会话区可显式 session_managed=true。"
                    ),
                },
                ensure_ascii=False,
            )
        if name == "fs_list":
            try:
                base = get_user_fs_root(user)
                path = (arguments.get("path") or "").strip()
                out = await fs_list_dir_async(path, base)
                return json.dumps(out, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        if name == "fs_search":
            try:
                base = get_user_fs_root(user)
                path = (arguments.get("path") or "").strip()
                out = await fs_search_files_async(
                    path,
                    base,
                    name_regex=(arguments.get("name_regex") or "").strip(),
                    path_regex=(arguments.get("path_regex") or "").strip(),
                    extensions=arguments.get("extensions"),
                    min_bytes=arguments.get("min_bytes"),
                    max_bytes=arguments.get("max_bytes"),
                    min_mtime=arguments.get("min_mtime"),
                    max_mtime=arguments.get("max_mtime"),
                    modified_after=(arguments.get("modified_after") or "").strip(),
                    modified_before=(arguments.get("modified_before") or "").strip(),
                    recursive=bool(arguments.get("recursive", True)),
                    files_only=bool(arguments.get("files_only", True)),
                    limit=arguments.get("limit"),
                )
                return json.dumps(out, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        if name == "fs_read_file":
            path = (arguments.get("path") or "").strip()
            offset = arguments.get("offset")
            size = arguments.get("size")
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            try:
                base = get_user_fs_root(user)
                offset_i = int(offset or 0)
                size_i = int(size) if size is not None else None
                out = await fs_read_file_async(path, base, offset=offset_i, size=size_i)
                return json.dumps(out, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        if name == "fs_write_file":
            path = (arguments.get("path") or "").strip()
            content = arguments.get("content")
            mode = (arguments.get("mode") or "overwrite")
            offset = arguments.get("offset")
            replace_length = arguments.get("replace_length")
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            guard = _skill_fs_path_guard(path)
            if guard:
                return json.dumps({"success": False, "error": guard}, ensure_ascii=False)
            if content is None:
                content = ""
            try:
                base = get_user_fs_root(user)
                offset_i = int(offset) if offset is not None else None
                rl_i = int(replace_length or 0)
                is_local_scope = (scope or "").strip().lower() == "local"
                session_managed = _effective_session_managed(arguments, path, base=base)
                managed_rel = _resolve_fs_write_relative_path(
                    path,
                    session_managed=session_managed,
                    local_scope=is_local_scope,
                    fallback_ext=".txt",
                    base=base,
                    session_id=session_id,
                )
                out = await fs_write_file_async(
                    managed_rel,
                    content if isinstance(content, str) else str(content),
                    base,
                    mode=str(mode or "overwrite"),
                    offset=offset_i,
                    replace_length=rl_i,
                )
                out["requested_path"] = path
                out["managed_relative_path"] = managed_rel
                out["session_managed"] = session_managed
                if out.get("success") is not False and session_id:
                    try:
                        from services.session_file_resources import record_session_file_resource

                        record_session_file_resource(
                            username=(user.get("username") or "default"),
                            session_id=session_id,
                            kind="workspace",
                            path=managed_rel or path,
                            title=Path(managed_rel or path).name,
                            note="fs_write_file",
                        )
                        out["session_file_indexed"] = True
                    except Exception:
                        pass
                return json.dumps(out, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        if name == "fs_read_binary":
            path = (arguments.get("path") or "").strip()
            offset = arguments.get("offset")
            size = arguments.get("size")
            encoding = (arguments.get("encoding") or "base64").strip().lower()
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            if encoding not in ("base64", "hex"):
                return json.dumps({"success": False, "error": "encoding 仅支持 base64 或 hex"}, ensure_ascii=False)
            try:
                base = get_user_fs_root(user)
                offset_i = int(offset or 0)
                size_i = int(size) if size is not None else None
                out = await fs_read_binary_async(path, base, offset=offset_i, size=size_i, encoding=encoding)
                return json.dumps(out, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        if name == "fs_write_binary":
            path = (arguments.get("path") or "").strip()
            content = arguments.get("content") or ""
            offset = arguments.get("offset")
            truncate = bool(arguments.get("truncate"))
            encoding = (arguments.get("encoding") or "base64").strip().lower()
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            if encoding not in ("base64", "hex"):
                return json.dumps({"success": False, "error": "encoding 仅支持 base64 或 hex"}, ensure_ascii=False)
            guard = _skill_fs_path_guard(path)
            if guard:
                return json.dumps({"success": False, "error": guard}, ensure_ascii=False)
            try:
                base = get_user_fs_root(user)
                offset_i = int(offset) if offset is not None else None
                is_local_scope = (scope or "").strip().lower() == "local"
                session_managed = _effective_session_managed(arguments, path, base=base)
                managed_rel = _resolve_fs_write_relative_path(
                    path,
                    session_managed=session_managed,
                    local_scope=is_local_scope,
                    fallback_ext=".bin",
                    base=base,
                    session_id=session_id,
                )
                out = await fs_write_binary_async(
                    managed_rel,
                    str(content),
                    base,
                    offset=offset_i,
                    truncate=truncate,
                    encoding=encoding,
                )
                out["requested_path"] = path
                out["managed_relative_path"] = managed_rel
                out["session_managed"] = session_managed
                if out.get("success") is not False and session_id:
                    try:
                        from services.session_file_resources import record_session_file_resource

                        record_session_file_resource(
                            username=(user.get("username") or "default"),
                            session_id=session_id,
                            kind="workspace",
                            path=managed_rel or path,
                            title=Path(managed_rel or path).name,
                            note="fs_write_binary",
                        )
                        out["session_file_indexed"] = True
                    except Exception:
                        pass
                return json.dumps(out, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        if name == "fs_truncate":
            path = (arguments.get("path") or "").strip()
            size = arguments.get("size")
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            try:
                base = get_user_fs_root(user)
                size_i = int(size or 0)
                out = await fs_truncate_async(path, size_i, base)
                return json.dumps(out, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        if name == "fs_mkdir":
            path = (arguments.get("path") or "").strip()
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            guard = _skill_fs_path_guard(path)
            if guard:
                return json.dumps({"success": False, "error": guard}, ensure_ascii=False)
            try:
                base = get_user_fs_root(user)
                out = await fs_mkdir_async(path, base)
                return json.dumps(out, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        if name == "fs_pack_tgz":
            path = (arguments.get("path") or "").strip()
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            try:
                base = get_user_fs_root(user)
                out = await fs_pack_tgz_async(path, base)
                return json.dumps(out, ensure_ascii=False)
            except (ValueError, OSError) as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        if name == "fs_unpack_tgz":
            path = (arguments.get("path") or "").strip()
            dest = (arguments.get("dest") or "").strip()
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            try:
                base = get_user_fs_root(user)
                out = await fs_unpack_tgz_async(path, dest, base)
                return json.dumps(out, ensure_ascii=False)
            except (ValueError, OSError) as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        if name == "fs_delete":
            path = (arguments.get("path") or "").strip()
            if not path:
                return json.dumps({"success": False, "error": "缺少 path"}, ensure_ascii=False)
            guard = _skill_fs_path_guard(path)
            if guard:
                return json.dumps({"success": False, "error": guard}, ensure_ascii=False)
            try:
                base = get_user_fs_root(user)
                out = await fs_delete_async(path, base)
                return json.dumps(out, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        if name == "fs_copy":
            path = (arguments.get("path") or "").strip()
            dest_dir = (arguments.get("dest_dir") or "").strip()
            move = bool(arguments.get("move"))
            if not path or not dest_dir:
                return json.dumps({"success": False, "error": "缺少 path 或 dest_dir"}, ensure_ascii=False)
            try:
                base = get_user_fs_root(user)
                out = await fs_copy_or_move_async(path, dest_dir, move, base)
                return json.dumps(out, ensure_ascii=False)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        # ── AI 聊天附件（chats/<username>/<uuid>.<ext>）──
        if name == "read_chat_attachment":
            from api.chat_attachments import (
                load_attachments_for_user as _load_attachments_for_user,
                resolve_attachment_file as _resolve_attachment_file,
                humanize_size as _humanize_size,
                attachment_relative_path as _attachment_relative_path,
                read_image_pixel_size as _read_image_pixel_size,
            )
            from services.vision_image import inline_vision_dimension_info as _inline_vision_dimension_info
            uuid_s = (arguments.get("uuid") or "").strip()
            if not uuid_s:
                return json.dumps({"success": False, "error": "缺少 uuid"}, ensure_ascii=False)
            try:
                max_chars = int(arguments.get("max_chars") or 40_000)
            except (TypeError, ValueError):
                max_chars = 40_000
            max_chars = max(1_000, min(200_000, max_chars))
            if "as_data_url" in arguments and arguments.get("as_data_url") is not None:
                as_data_url = bool(arguments.get("as_data_url"))
            else:
                as_data_url = True
            # 默认优先返回 AI 已保存的描述（扩展信息）；force_reload=true 时强制回读原图像素
            prefer_description = arguments.get("prefer_description")
            prefer_description = True if prefer_description is None else bool(prefer_description)
            force_reload = bool(arguments.get("force_reload") or False)
            db = await get_db()
            rows = await _load_attachments_for_user(db, user["id"], [uuid_s])
            if not rows:
                return json.dumps({"success": False, "error": "附件不存在或无权访问"}, ensure_ascii=False)
            row = rows[0]
            username = (user.get("username") or "default")
            path = _resolve_attachment_file(row, username)
            if not path.exists() or not path.is_file():
                return json.dumps({"success": False, "error": "附件文件已丢失"}, ensure_ascii=False)
            kind = (row.get("kind") or "binary").lower()
            cached_desc = (row.get("ai_description") or "").strip()
            cached_model = (row.get("ai_description_model") or "").strip()
            cached_updated = row.get("ai_description_updated_at") or None
            meta = {
                "uuid": row.get("uuid"),
                "name": row.get("original_name") or "",
                "mime": row.get("mime_type") or "",
                "size": int(row.get("size_bytes") or 0),
                "size_human": _humanize_size(row.get("size_bytes") or 0),
                "kind": kind,
                "url": f"/api/ai/attachments/{row.get('uuid')}",
                "fs_path": _attachment_relative_path(row),
                "has_ai_description": bool(cached_desc),
                "ai_description_model": cached_model,
                "ai_description_updated_at": cached_updated,
            }
            if kind == "image":
                dims = _read_image_pixel_size(path)
                if dims:
                    meta["width"] = dims[0]
                    meta["height"] = dims[1]
                    meta["original_width"] = dims[0]
                    meta["original_height"] = dims[1]
                try:
                    dim_info = _inline_vision_dimension_info(
                        await asyncio.to_thread(path.read_bytes),
                        mime=(row.get("mime_type") or "image/png"),
                    )
                    meta.update({k: v for k, v in dim_info.items() if v is not None})
                except OSError:
                    pass
            if kind in ("text", "markdown"):
                try:
                    raw = await asyncio.to_thread(path.read_bytes)
                except OSError as exc:
                    return json.dumps({"success": False, "error": f"读取失败: {exc}"}, ensure_ascii=False)
                try:
                    text = raw.decode("utf-8", errors="replace")
                except Exception:
                    text = raw.decode("latin-1", errors="replace")
                truncated = False
                if len(text) > max_chars:
                    text = text[:max_chars]
                    truncated = True
                return json.dumps({
                    "success": True,
                    "attachment": meta,
                    "content": text,
                    "truncated": truncated,
                    "total_chars": int(row.get("size_bytes") or 0),
                }, ensure_ascii=False)
            _doc_name = row.get("original_name") or ""
            _doc_mime = row.get("mime_type") or ""
            _needs_markitdown = kind == "document"
            if not _needs_markitdown and kind == "binary":
                from services.markitdown_convert import is_markitdown_convertible as _is_md_conv
                _needs_markitdown = _is_md_conv(_doc_name, _doc_mime)
            if _needs_markitdown:
                from services.markitdown_convert import convert_file_to_markdown as _convert_attachment_md
                conv = await _convert_attachment_md(path)
                if not conv.get("success"):
                    return json.dumps({
                        "success": False,
                        "attachment": meta,
                        "error": conv.get("error") or "文档转换失败",
                        "hint": "请确认服务器已安装 markitdown（pip install 'markitdown[pdf,docx,pptx,xlsx]'），或请用户导出为 .md/.txt 后重新上传。",
                    }, ensure_ascii=False)
                text = conv.get("markdown") or ""
                truncated = False
                if len(text) > max_chars:
                    text = text[:max_chars]
                    truncated = True
                resp = {
                    "success": True,
                    "attachment": meta,
                    "content": text,
                    "content_format": "markdown",
                    "converted_from_markitdown": True,
                    "conversion_source": conv.get("source") or "markitdown",
                    "truncated": truncated,
                    "total_chars": len(conv.get("markdown") or ""),
                }
                if conv.get("title"):
                    resp["document_title"] = conv["title"]
                if truncated:
                    resp["hint"] = f"Markdown 内容已按 max_chars={max_chars} 截断；可调大 max_chars 或分段分析。"
                return json.dumps(resp, ensure_ascii=False)
            if kind == "image":
                region_arg = arguments.get("region") if isinstance(arguments.get("region"), dict) else None
                tile_grid_arg = arguments.get("tile_grid") if isinstance(arguments.get("tile_grid"), dict) else None
                region_requested = bool(region_arg or tile_grid_arg)
                # 有缓存描述 & 未强制重读：优先只返回描述文本，省掉 base64 开销
                if cached_desc and prefer_description and not force_reload and not region_requested:
                    return json.dumps({
                        "success": True,
                        "attachment": meta,
                        "ai_description": cached_desc,
                        "source": "cached_description",
                        "hint": (
                            "该图片此前已由 AI 识别并保存扩展信息；这里直接返回已缓存的描述以节省上下文。"
                            "若当前问题与描述不匹配、或用户明确要求重新识别，请再次调用本工具并传 `force_reload=true` 获取原图 data_url。"
                        ),
                    }, ensure_ascii=False)
                if as_data_url:
                    from services.vision_image import build_inline_vision_meta
                    from services.vision_region import build_tile_regions, crop_image_region

                    try:
                        raw = await asyncio.to_thread(path.read_bytes)
                    except OSError as exc:
                        return json.dumps({"success": False, "error": f"读取失败: {exc}"}, ensure_ascii=False)
                    raw_for_vision = raw
                    region_meta = None
                    tiles = None
                    mode = "image"
                    src_w = int(meta.get("original_width") or meta.get("width") or 0)
                    src_h = int(meta.get("original_height") or meta.get("height") or 0)
                    if tile_grid_arg and src_w and src_h:
                        try:
                            rows_n = int(tile_grid_arg.get("rows") or arguments.get("tile_rows") or 2)
                        except (TypeError, ValueError):
                            rows_n = 2
                        try:
                            cols_n = int(tile_grid_arg.get("cols") or arguments.get("tile_cols") or 2)
                        except (TypeError, ValueError):
                            cols_n = 2
                        try:
                            overlap = float(tile_grid_arg.get("overlap_ratio", arguments.get("overlap_ratio", 0.08)))
                        except (TypeError, ValueError):
                            overlap = 0.08
                        tiles = build_tile_regions(src_w, src_h, rows_n, cols_n, overlap)
                        try:
                            tile_id = int(arguments.get("tile_id") or tile_grid_arg.get("tile_id") or 1)
                        except (TypeError, ValueError):
                            tile_id = 1
                        selected = next((t for t in tiles if int(t.get("tile_id") or 0) == tile_id), None) or (tiles[0] if tiles else None)
                        if selected:
                            raw_for_vision, region_meta = crop_image_region(raw, selected, coordinate_space="pixel", pad_ratio=0.0)
                            mode = "tile_region"
                    elif region_arg:
                        try:
                            pad = float(arguments.get("pad_ratio", 0.08))
                        except (TypeError, ValueError):
                            pad = 0.08
                        raw_for_vision, region_meta = crop_image_region(
                            raw,
                            region_arg,
                            coordinate_space=(arguments.get("region_coordinate_space") or "auto"),
                            pad_ratio=pad,
                        )
                        mode = "region"
                    data_url, _mime_out, jpeg_len, dim_meta = build_inline_vision_meta(
                        raw_for_vision,
                        mime=(row.get("mime_type") or "image/png"),
                    )
                    if region_meta:
                        region_vision_meta = dict(dim_meta or {})
                        region_vision_meta["source_region"] = region_meta
                    else:
                        region_vision_meta = None
                        meta.update({k: v for k, v in (dim_meta or {}).items() if v is not None})
                    resp = {
                        "success": True,
                        "mode": mode,
                        "attachment": meta,
                        "data_url": data_url,
                        "bytes": int(row.get("size_bytes") or 0) or len(raw),
                        "vision_jpeg_bytes": jpeg_len,
                        "data_url_chars": len(data_url),
                    }
                    if region_meta:
                        resp["source_width"] = region_meta.get("source_width")
                        resp["source_height"] = region_meta.get("source_height")
                        resp["region_meta"] = region_meta
                        resp["region_vision_meta"] = region_vision_meta
                    if tiles:
                        resp["tile_grid"] = {
                            "rows": max((int(t.get("row") or 0) for t in tiles), default=0) + 1,
                            "cols": max((int(t.get("col") or 0) for t in tiles), default=0) + 1,
                            "count": len(tiles),
                        }
                        resp["tiles"] = tiles
                    ow = meta.get("original_width") or meta.get("width")
                    oh = meta.get("original_height") or meta.get("height")
                    vw = meta.get("vision_width")
                    vh = meta.get("vision_height")
                    coord_hint = (
                        f"原图 {ow}×{oh}px；edit 输出用原图坐标。"
                        if ow and oh
                        else "edit 标注请用原图坐标（见 attachment.width/height）。"
                    )
                    mv_w = meta.get("model_view_width")
                    mv_h = meta.get("model_view_height")
                    if mv_w and mv_h and ow and oh and (int(mv_w) != int(ow) or int(mv_h) != int(oh)):
                        coord_hint += (
                            f" 模型视图 {mv_w}×{mv_h}px；若按所见估坐标，edit 传 reference_width={mv_w}, reference_height={mv_h}。"
                        )
                    elif vw and vh and ow and oh and (int(vw) != int(ow) or int(vh) != int(oh)):
                        coord_hint += (
                            f" 识图 {vw}×{vh}px；若按所见估坐标，edit 传 reference_width={vw}, reference_height={vh}。"
                        )
                    if region_meta:
                        _mag = region_meta.get("magnify") or 1.0
                        _mag_note = (
                            f" 本局部图已放大 {_mag:.2f}× 便于你看清细节（用 percent 给坐标不受放大影响）。"
                            if isinstance(_mag, (int, float)) and _mag > 1.01
                            else ""
                        )
                        coord_hint = (
                            f"这是原图局部区域：原图 {ow}×{oh}px，区域 left={region_meta.get('x')} top={region_meta.get('y')} "
                            f"width={region_meta.get('width')} height={region_meta.get('height')}。"
                            "局部图中的位置请作为局部坐标处理；最终标注原图时，将本响应的 region_meta 作为 "
                            "`edit_chat_attachment_image.source_region` 传回，后端会自动回填原图坐标。"
                            + _mag_note
                        )
                    resp["coordinate_hint"] = coord_hint
                    if region_meta:
                        resp["backfill_hint"] = (
                            "精识别后调用 edit_chat_attachment_image：uuid 仍用原图 uuid，传 source_region=本响应 region_meta，"
                            "最好用 coordinate_space=percent（0–100 相对局部图）。若使用 pixel 局部坐标，请同时传 "
                            "source_region_vision_meta=本响应 region_vision_meta，后端会按模型所见尺寸缩放回原图区域。"
                        )
                    # 若同时存在缓存描述，一并带出，便于 AI 对比/更新
                    if cached_desc:
                        resp["ai_description"] = cached_desc
                        resp["hint"] = "已返回原图 data_url；如描述需要更新，请基于最新观察调用 `save_image_description` 覆盖旧值。"
                    elif region_meta:
                        resp["hint"] = "已返回局部高清图 data_url；识别出小目标后请用 region_meta 回填原图坐标再标注。"
                    else:
                        resp["hint"] = "首次识读该图。分析完成后**请立即调用 `save_image_description(uuid=..., description=...)`** 保存扩展信息，后续多轮将自动复用，避免反复消耗图像 token。"
                    return json.dumps(resp, ensure_ascii=False)
                if tile_grid_arg:
                    from services.vision_region import build_tile_regions

                    src_w = int(meta.get("original_width") or meta.get("width") or 0)
                    src_h = int(meta.get("original_height") or meta.get("height") or 0)
                    try:
                        rows_n = int(tile_grid_arg.get("rows") or arguments.get("tile_rows") or 2)
                    except (TypeError, ValueError):
                        rows_n = 2
                    try:
                        cols_n = int(tile_grid_arg.get("cols") or arguments.get("tile_cols") or 2)
                    except (TypeError, ValueError):
                        cols_n = 2
                    try:
                        overlap = float(tile_grid_arg.get("overlap_ratio", arguments.get("overlap_ratio", 0.08)))
                    except (TypeError, ValueError):
                        overlap = 0.08
                    tiles = build_tile_regions(src_w, src_h, rows_n, cols_n, overlap) if src_w and src_h else []
                    return json.dumps({
                        "success": True,
                        "mode": "tile_grid",
                        "attachment": meta,
                        "tile_grid": {"rows": rows_n, "cols": cols_n, "count": len(tiles), "overlap_ratio": overlap},
                        "tiles": tiles,
                        "hint": "已返回分块列表；指定 tile_id 并保持 as_data_url=true 可读取某一块局部图。",
                    }, ensure_ascii=False)
                # 既不要 data_url，又没有缓存描述：仅返回元信息
                return json.dumps({
                    "success": True,
                    "attachment": meta,
                    "hint": "仅元信息；如需图像内容请重新调用本工具并省略 as_data_url（默认会返回缓存描述或 data_url）",
                }, ensure_ascii=False)
            # 其它二进制：只返回元信息
            return json.dumps({
                "success": True,
                "attachment": meta,
                "hint": (
                    "二进制附件，未提供内容读取。"
                    "若为 Office/PDF，请确认扩展名与服务器 MarkItDown 支持；"
                    "或请用户转为 .md/.txt 后重新上传。"
                ),
            }, ensure_ascii=False)

        if name == "read_chat_data":
            from services.chat_tool_spill import read_chat_data_slice_async

            spill_id = (arguments.get("spill_id") or "").strip()
            date_subdir = (arguments.get("date_subdir") or arguments.get("storage_subdir") or "").strip()
            mode = (arguments.get("mode") or "head_tail").strip()
            if not spill_id or not date_subdir:
                return json.dumps(
                    {"success": False, "error": "缺少 spill_id 或 date_subdir（与工具消息哨兵行一致）"},
                    ensure_ascii=False,
                )
            from services.chat_tool_spill import (
                CHAT_TOOL_SPILL_READ_DEFAULT_HEAD_CHARS,
                CHAT_TOOL_SPILL_READ_DEFAULT_RANGE_CHARS,
                CHAT_TOOL_SPILL_READ_DEFAULT_TAIL_CHARS,
            )

            try:
                head_chars = int(arguments.get("head_chars") or CHAT_TOOL_SPILL_READ_DEFAULT_HEAD_CHARS)
            except (TypeError, ValueError):
                head_chars = CHAT_TOOL_SPILL_READ_DEFAULT_HEAD_CHARS
            try:
                tail_chars = int(arguments.get("tail_chars") or CHAT_TOOL_SPILL_READ_DEFAULT_TAIL_CHARS)
            except (TypeError, ValueError):
                tail_chars = CHAT_TOOL_SPILL_READ_DEFAULT_TAIL_CHARS
            try:
                range_start = int(arguments.get("range_start") or 0)
            except (TypeError, ValueError):
                range_start = 0
            try:
                max_chars = int(arguments.get("max_chars") or CHAT_TOOL_SPILL_READ_DEFAULT_RANGE_CHARS)
            except (TypeError, ValueError):
                max_chars = CHAT_TOOL_SPILL_READ_DEFAULT_RANGE_CHARS
            try:
                out = await read_chat_data_slice_async(
                    user,
                    session_id,
                    spill_id,
                    date_subdir,
                    mode,
                    head_chars=head_chars,
                    tail_chars=tail_chars,
                    range_start=range_start,
                    max_chars=max_chars,
                )
            except (ValueError, PermissionError, FileNotFoundError) as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            return json.dumps(out, ensure_ascii=False)

        if name == "save_image_description":
            from api.chat_attachments import (
                load_attachments_for_user as _load_attachments_for_user,
                save_attachment_ai_description as _save_attachment_ai_description,
            )
            uuid_s = (arguments.get("uuid") or "").strip()
            description = (arguments.get("description") or "").strip()
            if not uuid_s:
                return json.dumps({"success": False, "error": "缺少 uuid"}, ensure_ascii=False)
            if not description:
                return json.dumps({"success": False, "error": "description 不能为空（如需清空请传一个明确的占位描述）"}, ensure_ascii=False)
            # 过长描述折中裁剪到 8000 字，避免个别模型塞整本小说进来把 DB 撑大；前端列表展示层会再截到 1500
            if len(description) > 8_000:
                description = description[:8_000].rstrip() + " …（已在保存时截断到 8000 字）"
            db = await get_db()
            rows = await _load_attachments_for_user(db, user["id"], [uuid_s])
            if not rows:
                return json.dumps({"success": False, "error": "附件不存在或无权访问"}, ensure_ascii=False)
            row = rows[0]
            if (row.get("kind") or "").lower() != "image":
                return json.dumps({"success": False, "error": "save_image_description 只作用于图片附件"}, ensure_ascii=False)
            # 用 provider 模型标识作为水印（若可取），便于刷新判断
            model_tag = ""
            try:
                _mrows = await db.execute_fetchall(
                    "SELECT model FROM user_ai_config WHERE user_id = ?",
                    (user["id"],),
                )
                if _mrows:
                    model_tag = (dict(_mrows[0]).get("model") or "").strip()
            except Exception:  # noqa: BLE001
                model_tag = ""
            ok = await _save_attachment_ai_description(
                db,
                user_id=user["id"],
                uuid=uuid_s,
                description=description,
                model=model_tag,
            )
            if not ok:
                return json.dumps({"success": False, "error": "写入失败（附件可能已被删除）"}, ensure_ascii=False)
            return json.dumps({
                "success": True,
                "uuid": uuid_s,
                "chars_saved": len(description),
                "model": model_tag,
                "hint": "描述已保存为该图片的扩展信息；后续轮次将默认以此文本形式出现在 📎 附件清单与 read_chat_attachment 返回里，你不必再重读原图。",
            }, ensure_ascii=False)

        if name == "edit_chat_attachment_image":
            from api.chat_attachments import (
                load_attachments_for_user as _load_attachments_for_user,
                resolve_attachment_file as _resolve_attachment_file,
                save_bytes_as_chat_attachment as _save_bytes_as_chat_attachment,
                attachment_relative_path as _attachment_relative_path,
            )
            from services.image_calibration import (
                build_calibration_plan as _build_calibration_plan,
                build_percent_grid_annotations as _build_percent_grid_annotations,
                build_cell_grid_plan as _build_cell_grid_plan,
                cells_to_bbox as _cells_to_bbox,
                parse_global_transform as _parse_global_transform,
                estimate_auto_global_transform as _estimate_auto_global_transform,
                detect_content_bounds as _detect_content_bounds,
                apply_calibration_transform_to_annotations as _apply_affine_anns,
                clamp_tight_box_sizes as _clamp_tight_box_sizes,
                normalize_box_anchor_to_top_left as _normalize_box_anchor_to_top_left,
                resolve_annotation_transform as _resolve_annotation_transform,
                CALIBRATION_MIN_POINTS as _CAL_MIN,
            )
            from services.image_edit import (
                apply_image_edits as _apply_image_edits,
                read_image_pixel_size_from_bytes as _read_image_pixel_size_from_bytes,
            )
            from services.vision_region import map_local_annotations_to_original as _map_local_annotations_to_original
            from services.vision_image import inline_vision_dimension_info as _inline_vision_dimension_info

            uuid_s = (arguments.get("uuid") or "").strip()
            if not uuid_s:
                return json.dumps({"success": False, "error": "缺少 uuid"}, ensure_ascii=False)
            db = await get_db()
            rows = await _load_attachments_for_user(db, user["id"], [uuid_s])
            if not rows:
                return json.dumps({"success": False, "error": "附件不存在或无权访问"}, ensure_ascii=False)
            row = rows[0]
            if (row.get("kind") or "").lower() != "image":
                return json.dumps({"success": False, "error": "edit_chat_attachment_image 只作用于图片附件"}, ensure_ascii=False)
            username = (user.get("username") or "default")
            src_path = _resolve_attachment_file(row, username)
            if not src_path.exists() or not src_path.is_file():
                return json.dumps({"success": False, "error": "源图片文件已丢失"}, ensure_ascii=False)
            try:
                raw = await asyncio.to_thread(src_path.read_bytes)
            except OSError as exc:
                return json.dumps({"success": False, "error": f"读取失败: {exc}"}, ensure_ascii=False)
            crop = arguments.get("crop")
            if crop is not None and not isinstance(crop, dict):
                crop = None
            anns = arguments.get("annotations")
            if anns is not None and not isinstance(anns, list):
                anns = None
            cal_obs = arguments.get("calibration_observations")
            if cal_obs is not None and not isinstance(cal_obs, list):
                cal_obs = None
            calibration_probe = arguments.get("calibration_probe") in (True, "true", 1, "1")
            grid_overlay = arguments.get("grid_overlay") in (True, "true", 1, "1")
            cell_grid = arguments.get("cell_grid") in (True, "true", 1, "1")
            cell_cols = arguments.get("cell_cols")
            cell_rows = arguments.get("cell_rows")
            coordinate_space = arguments.get("coordinate_space")

            src_size = _read_image_pixel_size_from_bytes(raw)
            src_w, src_h = src_size if src_size else (None, None)
            if (calibration_probe or cal_obs or grid_overlay or cell_grid) and (not src_w or not src_h):
                return json.dumps({"success": False, "error": "无法读取原图尺寸，标注校准失败"}, ensure_ascii=False)

            cal_reference, cal_draw_anns = (
                _build_calibration_plan(src_w, src_h) if src_w and src_h else ([], [])
            )

            if cell_grid:
                try:
                    cell_anns, grid_meta = _build_cell_grid_plan(src_w, src_h, cell_cols, cell_rows)
                    cell_bytes, mime = await asyncio.to_thread(
                        _apply_image_edits,
                        raw,
                        rotate=arguments.get("rotate") or 0,
                        crop=crop,
                        scale=arguments.get("scale"),
                        annotations=cell_anns,
                    )
                except Exception as exc:  # noqa: BLE001
                    return json.dumps({"success": False, "error": f"编号网格预览生成失败: {exc}"}, ensure_ascii=False)
                cell_name = (arguments.get("output_name") or "").strip() or "cell-grid.png"
                if not cell_name.lower().endswith(".png"):
                    cell_name = f"{cell_name.rsplit('.', 1)[0] if '.' in cell_name else cell_name}.png"
                sid = arguments.get("session_id")
                if sid is None:
                    sid = row.get("session_id")
                try:
                    sid = int(sid) if sid is not None else None
                except (TypeError, ValueError):
                    sid = row.get("session_id")
                try:
                    saved = await _save_bytes_as_chat_attachment(
                        user, cell_bytes, original_name=cell_name, mime=mime, session_id=sid,
                    )
                except ValueError as exc:
                    return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
                url = saved.get("url") or f"/api/ai/attachments/{saved.get('uuid')}"
                title = saved.get("name") or cell_name
                try:
                    from services.vision_image import build_inline_vision_meta as _bivm
                    _preview_url, _, _, _ = _bivm(cell_bytes, mime=mime)
                except Exception:  # noqa: BLE001
                    _preview_url = None
                return json.dumps({
                    "success": True,
                    "mode": "cell_grid",
                    "source_uuid": uuid_s,
                    "attachment": saved,
                    "markdown_image": f"![{title}]({url})",
                    "data_url": _preview_url,
                    "source_width": src_w,
                    "source_height": src_h,
                    "grid": {"cols": grid_meta["cols"], "rows": grid_meta["rows"], "count": grid_meta["count"]},
                    "hint": (
                        f"**请查看上面返回的 data_url**：图已被切成 {grid_meta['cols']}×{grid_meta['rows']}={grid_meta['count']} 个编号小格"
                        "（红字编号在每格左上角，1 起从左到右、从上到下）。"
                        "对每个要标注的目标，**只需说出它覆盖了哪些格子编号**，然后调用本工具并在 annotations 里用 "
                        '`{"type":"rect","cells":[编号,...],"outline":"#ff0000"}`（不要给 x/y/坐标）。'
                        "后端会把这些格子并成精确像素框。务必传相同的 cell_cols/cell_rows。"
                        "这是兜底预览，不是最终交付；下一步必须单次带全量 annotations，且不要连续开启预览。"
                    ),
                }, ensure_ascii=False)

            if grid_overlay:
                try:
                    grid_anns = _build_percent_grid_annotations(src_w, src_h)
                    grid_bytes, mime = await asyncio.to_thread(
                        _apply_image_edits,
                        raw,
                        rotate=arguments.get("rotate") or 0,
                        crop=crop,
                        scale=arguments.get("scale"),
                        annotations=grid_anns,
                    )
                except Exception as exc:  # noqa: BLE001
                    return json.dumps({"success": False, "error": f"刻度网格预览生成失败: {exc}"}, ensure_ascii=False)
                grid_name = (arguments.get("output_name") or "").strip() or "grid-overlay.png"
                if not grid_name.lower().endswith(".png"):
                    grid_name = f"{grid_name.rsplit('.', 1)[0] if '.' in grid_name else grid_name}.png"
                sid = arguments.get("session_id")
                if sid is None:
                    sid = row.get("session_id")
                try:
                    sid = int(sid) if sid is not None else None
                except (TypeError, ValueError):
                    sid = row.get("session_id")
                try:
                    saved = await _save_bytes_as_chat_attachment(
                        user, grid_bytes, original_name=grid_name, mime=mime, session_id=sid,
                    )
                except ValueError as exc:
                    return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
                url = saved.get("url") or f"/api/ai/attachments/{saved.get('uuid')}"
                title = saved.get("name") or grid_name
                try:
                    from services.vision_image import build_inline_vision_meta as _bivm
                    _preview_url, _, _, _ = _bivm(grid_bytes, mime=mime)
                except Exception:  # noqa: BLE001
                    _preview_url = None
                return json.dumps({
                    "success": True,
                    "mode": "grid_overlay",
                    "source_uuid": uuid_s,
                    "attachment": saved,
                    "markdown_image": f"![{title}]({url})",
                    "data_url": _preview_url,
                    "source_width": src_w,
                    "source_height": src_h,
                    "hint": (
                        "**请查看上面返回的 data_url（带 0–100 百分比刻度的预览图）**：横轴红字=X%，纵轴红字=Y%。"
                        "对照刻度读出每个目标的左上角(x,y)和宽高(width,height)百分比，"
                        "然后用 coordinate_space=\"percent\" + annotations 再次调用本工具输出标注图。"
                        "关键点优先只给 x/y 并使用 crosshair/target/callout；这是兜底预览，不是最终交付，不要连续开启预览。"
                    ),
                }, ensure_ascii=False)

            if calibration_probe:
                try:
                    probe_bytes, mime = await asyncio.to_thread(
                        _apply_image_edits,
                        raw,
                        rotate=arguments.get("rotate") or 0,
                        crop=crop,
                        scale=arguments.get("scale"),
                        annotations=cal_draw_anns,
                    )
                except Exception as exc:  # noqa: BLE001
                    return json.dumps({"success": False, "error": f"校准探测图生成失败: {exc}"}, ensure_ascii=False)
                probe_name = (arguments.get("output_name") or "").strip() or "calibration-probe.png"
                if not probe_name.lower().endswith(".png"):
                    probe_name = f"{probe_name.rsplit('.', 1)[0] if '.' in probe_name else probe_name}.png"
                sid = arguments.get("session_id")
                if sid is None:
                    sid = row.get("session_id")
                try:
                    sid = int(sid) if sid is not None else None
                except (TypeError, ValueError):
                    sid = row.get("session_id")
                try:
                    saved = await _save_bytes_as_chat_attachment(
                        user,
                        probe_bytes,
                        original_name=probe_name,
                        mime=mime,
                        session_id=sid,
                    )
                except ValueError as exc:
                    return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
                url = saved.get("url") or f"/api/ai/attachments/{saved.get('uuid')}"
                title = saved.get("name") or probe_name
                try:
                    from services.vision_image import build_inline_vision_meta as _bivm
                    _preview_url, _, _, _ = _bivm(probe_bytes, mime=mime)
                except Exception:  # noqa: BLE001
                    _preview_url = None
                return json.dumps({
                    "success": True,
                    "mode": "calibration_probe",
                    "source_uuid": uuid_s,
                    "attachment": saved,
                    "markdown_image": f"![{title}]({url})",
                    "data_url": _preview_url,
                    "source_width": src_w,
                    "source_height": src_h,
                    "calibration_reference": cal_reference,
                    "hint": (
                        "**请查看上面返回的 data_url（带角标 ①②③④ 的预览图）**，读取每个角标左上角在你所见画面中的 x,y，"
                        f"至少 {_CAL_MIN} 条不同 id，填入 calibration_observations 后再调用本工具输出最终标注图。"
                        "这是兜底预览，不是最终交付；下一步必须单次带全量 annotations，且不要连续开启预览。"
                    ),
                }, ensure_ascii=False)

            ref_w = arguments.get("reference_width")
            ref_h = arguments.get("reference_height")
            try:
                ref_w = int(ref_w) if ref_w is not None else None
            except (TypeError, ValueError):
                ref_w = None
            try:
                ref_h = int(ref_h) if ref_h is not None else None
            except (TypeError, ValueError):
                ref_h = None
            try:
                offset_x = float(arguments.get("offset_x") or 0)
            except (TypeError, ValueError):
                offset_x = 0.0
            try:
                offset_y = float(arguments.get("offset_y") or 0)
            except (TypeError, ValueError):
                offset_y = 0.0
            use_original = arguments.get("use_original_coordinates") in (True, "true", 1, "1")

            dim_info = _inline_vision_dimension_info(raw, mime=(row.get("mime_type") or "")) if src_w and src_h else {}
            scaled_anns = anns
            coord_note = ""
            transform_meta = None
            # 单元网格法：带 cells 的标注由后端按编号确定性换算为像素框，绕过坐标变换
            cell_pixel_anns: list[dict] = []
            coord_anns: list[dict] = anns or []
            cells_used = 0
            if anns and src_w and src_h and any(isinstance(a, dict) and a.get("cells") for a in anns):
                _, cell_grid_meta = _build_cell_grid_plan(src_w, src_h, cell_cols, cell_rows)
                coord_anns = []
                for a in anns:
                    if isinstance(a, dict) and a.get("cells"):
                        box = _cells_to_bbox(a.get("cells"), cell_grid_meta)
                        if box:
                            merged = {k: v for k, v in a.items() if k not in ("cells", "x", "y", "width", "height", "w", "h")}
                            merged.setdefault("type", "rect")
                            merged.update(box)
                            cell_pixel_anns.append(merged)
                            cells_used += 1
                    else:
                        coord_anns.append(a)
            anchor_notes: list[str] = []
            if coord_anns:
                coord_anns, anchor_notes = _normalize_box_anchor_to_top_left(coord_anns)
            # 全局微调：整组标注相对布局准、仅整体缩放/平移有偏差时，在坐标系换算前统一校正
            global_transform = _parse_global_transform(arguments.get("global_transform"))
            source_region_arg = arguments.get("source_region") if isinstance(arguments.get("source_region"), dict) else None
            source_region = source_region_arg
            source_region_vision_meta = arguments.get("source_region_vision_meta") if isinstance(arguments.get("source_region_vision_meta"), dict) else None
            if source_region and isinstance(source_region.get("region_meta"), dict):
                source_region_vision_meta = source_region.get("region_vision_meta") if isinstance(source_region.get("region_vision_meta"), dict) else source_region_vision_meta
                source_region = source_region.get("region_meta")
            elif source_region and isinstance(source_region.get("source_region"), dict):
                source_region_vision_meta = source_region
                source_region = source_region.get("source_region")
            content_bounds = None
            auto_gt_flag = arguments.get("auto_global_transform")
            auto_gt_on = auto_gt_flag is None or auto_gt_flag in (True, "true", 1, "1")
            if not coordinate_space and coord_anns:
                coordinate_space = "percent"
            if coordinate_space in ("percent_content", "content", "content_percent", "内容区百分比"):
                content_bounds = _detect_content_bounds(raw)
            global_note = ""
            auto_space_switch = ""
            if not source_region and not global_transform and auto_gt_on and coord_anns and len(coord_anns) >= 3 and src_w and src_h:
                auto_res = _estimate_auto_global_transform(
                    coord_anns, src_w, src_h, space=coordinate_space or "percent", raw=raw,
                )
                if auto_res:
                    gsx, gsy, gox, goy, sp_auto, cb_auto = auto_res
                    global_transform = (gsx, gsy, gox, goy)
                    if sp_auto and sp_auto != (coordinate_space or "percent"):
                        coordinate_space = sp_auto
                        auto_space_switch = f"坐标系→{sp_auto}"
                    if cb_auto:
                        content_bounds = cb_auto
                    global_note = (
                        f"auto_global_transform scale=({gsx:.3f},{gsy:.3f}) offset=({gox:+.2f},{goy:+.2f})"
                        + (f"；{auto_space_switch}" if auto_space_switch else "")
                    )
            if global_transform and coord_anns:
                gsx, gsy, gox, goy = global_transform
                coord_anns = _apply_affine_anns(coord_anns, gsx, gsy, gox, goy) or coord_anns
                global_note = f"全局微调 scale=({gsx:.3f},{gsy:.3f}) offset=({gox:+.2f},{goy:+.2f})"
            clamp_notes: list[str] = []
            tight_flag = arguments.get("tight_boxes")
            tight_on = tight_flag is None or tight_flag in (True, "true", 1, "1")
            try:
                max_box_h = float(arguments.get("max_box_height_percent") or 5.5)
            except (TypeError, ValueError):
                max_box_h = 5.5
            try:
                max_box_w = float(arguments.get("max_box_width_percent") or 18.0)
            except (TypeError, ValueError):
                max_box_w = 18.0
            if coord_anns and tight_on:
                coord_anns, clamp_notes = _clamp_tight_box_sizes(
                    coord_anns,
                    space=coordinate_space or "percent",
                    max_width=max_box_w,
                    max_height=max_box_h,
                    enabled=True,
                )
            if anchor_notes:
                clamp_notes = anchor_notes + clamp_notes
            if source_region and coord_anns and src_w and src_h:
                ref_w = None
                ref_h = None
                if isinstance(source_region_vision_meta, dict):
                    ref_w = (
                        source_region_vision_meta.get("model_view_width")
                        or source_region_vision_meta.get("vision_width")
                        or source_region_vision_meta.get("region_width")
                    )
                    ref_h = (
                        source_region_vision_meta.get("model_view_height")
                        or source_region_vision_meta.get("vision_height")
                        or source_region_vision_meta.get("region_height")
                    )
                scaled_anns = _map_local_annotations_to_original(
                    coord_anns,
                    source_region,
                    coordinate_space=coordinate_space or "percent",
                    reference_width=ref_w,
                    reference_height=ref_h,
                ) or []
                coord_note = (
                    f"局部区域回填 → 原图：x={source_region.get('x', source_region.get('left'))} "
                    f"y={source_region.get('y', source_region.get('top'))} "
                    f"w={source_region.get('width', source_region.get('region_width'))} "
                    f"h={source_region.get('height', source_region.get('region_height'))}"
                )
                transform_meta = {
                    "method": "source_region",
                    "source_region": source_region,
                    "source_region_vision_meta": source_region_vision_meta,
                    "space": coordinate_space or "percent",
                }
                if clamp_notes:
                    coord_note = "；".join(clamp_notes) + "；" + coord_note
                    transform_meta["clamp_notes"] = clamp_notes
            elif coord_anns and src_w and src_h:
                scaled_anns, coord_note, transform_meta = _resolve_annotation_transform(
                    coord_anns,
                    src_w,
                    src_h,
                    dim_info,
                    cal_reference,
                    cal_obs,
                    coordinate_space=coordinate_space,
                    content_bounds=content_bounds,
                    reference_width=ref_w,
                    reference_height=ref_h,
                    offset_x=offset_x,
                    offset_y=offset_y,
                    use_original=use_original,
                )
                if global_note or clamp_notes:
                    extra = "；".join([p for p in (global_note, "；".join(clamp_notes) if clamp_notes else "") if p])
                    coord_note = (extra + "；" + coord_note) if coord_note else extra
                    if isinstance(transform_meta, dict):
                        if clamp_notes:
                            transform_meta["clamp_notes"] = clamp_notes
                            transform_meta["tight_boxes"] = {
                                "enabled": tight_on,
                                "max_height_percent": max_box_h,
                                "max_width_percent": max_box_w,
                            }
                        if global_transform:
                            transform_meta["global_transform"] = {
                                "scale_x": global_transform[0], "scale_y": global_transform[1],
                                "offset_x": global_transform[2], "offset_y": global_transform[3],
                                "auto": bool(auto_gt_on and not arguments.get("global_transform")),
                            }
                        if content_bounds:
                            transform_meta["content_bounds"] = content_bounds
                if scaled_anns is None:
                    return json.dumps({
                        "success": False,
                        "error": f"校准失败：至少需要 {_CAL_MIN} 条有效 calibration_observations",
                        "calibration_reference": cal_reference,
                        "hint": "可先 calibration_probe=true 获取角标参考图，或省略观测由后端 auto 推断（精度较低）。",
                    }, ensure_ascii=False)
            else:
                scaled_anns = []
            # 合并单元网格法生成的精确像素框
            if cell_pixel_anns:
                scaled_anns = list(scaled_anns or []) + cell_pixel_anns
                if cells_used:
                    _note = f"单元网格 {cell_cols or 12}×{cell_rows or 8}：{cells_used} 个目标按编号确定性出框"
                    coord_note = (coord_note + "；" + _note) if coord_note else _note
                    if not isinstance(transform_meta, dict):
                        transform_meta = {}
                    transform_meta["cell_grid_used"] = cells_used

            try:
                edited, mime = await asyncio.to_thread(
                    _apply_image_edits,
                    raw,
                    rotate=arguments.get("rotate") or 0,
                    crop=crop,
                    scale=arguments.get("scale"),
                    annotations=scaled_anns,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("edit_chat_attachment_image failed uuid=%s err=%s", uuid_s, exc)
                return json.dumps({"success": False, "error": f"图片编辑失败: {exc}"}, ensure_ascii=False)
            out_size = _read_image_pixel_size_from_bytes(edited)
            orig_name = row.get("original_name") or "image.png"
            out_name = (arguments.get("output_name") or "").strip() or f"edited-{orig_name}"
            if not out_name.lower().endswith(".png"):
                out_name = f"{out_name.rsplit('.', 1)[0] if '.' in out_name else out_name}.png"
            sid = arguments.get("session_id")
            if sid is None:
                sid = row.get("session_id")
            try:
                sid = int(sid) if sid is not None else None
            except (TypeError, ValueError):
                sid = row.get("session_id")
            try:
                saved = await _save_bytes_as_chat_attachment(
                    user,
                    edited,
                    original_name=out_name,
                    mime=mime,
                    session_id=sid,
                )
            except ValueError as exc:
                return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
            url = saved.get("url") or f"/api/ai/attachments/{saved.get('uuid')}"
            title = saved.get("name") or out_name
            markdown_image = f"![{title}]({url})"
            try:
                from services.vision_image import build_inline_vision_meta as _bivm
                _result_url, _, _, _ = _bivm(edited, mime=mime)
            except Exception:  # noqa: BLE001
                _result_url = None
            point_types = {"crosshair", "hair", "target", "ring", "callout", "pin", "marker", "point"}
            rect_like_types = {"rect", "rectangle", "box", "overlay", "mask", "highlight", "ellipse", "circle"}
            annotation_count = len(anns or [])
            point_annotation_count = sum(
                1
                for a in (anns or [])
                if isinstance(a, dict) and (str(a.get("type") or "").strip().lower() in point_types)
            )
            rect_like_count = sum(
                1
                for a in (anns or [])
                if isinstance(a, dict) and (str(a.get("type") or "rect").strip().lower() in rect_like_types)
            )
            likely_transform_error = bool(
                isinstance(transform_meta, dict)
                and (transform_meta.get("likely_offset_error") or transform_meta.get("recommend_calibration_probe"))
            )
            # 单个目标、文字/UI 小目标、矩形/遮罩类标注最容易出现“位置看似合理但语义目标错/整体偏高”。
            # 这类结果需要模型看 data_url 做一次确认，不能直接 deliver_now。
            visual_review_required = bool(annotation_count <= 1 or rect_like_count > 0)
            should_retry = likely_transform_error
            deliver_now = not should_retry and not visual_review_required
            if should_retry:
                retry_mode = "global_transform_only"
            elif visual_review_required:
                retry_mode = "visual_check_then_deliver_or_one_retry"
            else:
                retry_mode = None
            suggested_global_transform = (
                {"scale_x": global_transform[0], "scale_y": global_transform[1],
                 "offset_x": global_transform[2], "offset_y": global_transform[3]}
                if global_transform else None
            )
            if deliver_now:
                hint = (
                    "后端坐标换算与自动校正已完成，`deliver_now=true`。"
                    "请立即把 `markdown_image` 插入回复交付；不要再次调用本工具，不要逐个目标微调。"
                )
            elif should_retry:
                hint = (
                    "检测到标注可能存在整组偏移，`should_retry=true`。"
                    "只允许最后再调用一次：原样复用本次 annotations，仅调整 `global_transform`（或 percent/percent_content 坐标系），"
                    "禁止逐个目标修改 x/y，禁止再开 grid_overlay/cell_grid/calibration_probe。"
                )
            else:
                hint = (
                    "这是单目标或矩形/遮罩类高风险标注，`deliver_now=false` 且需要先查看 `data_url`。"
                    "若红框/标记已经准确覆盖用户要求的语义目标，再交付 `markdown_image`；"
                    "若整体偏高/偏低，最多再调用一次并只调 `global_transform`；"
                    "若标到了错误对象（如把主机名标成地址栏/搜索框），最多再调用一次，改为 crosshair/target/callout 标真实目标中心，避免实心矩形。"
                )
            return json.dumps({
                "success": True,
                "source_uuid": uuid_s,
                "attachment": saved,
                "fs_path": saved.get("fs_path") or _attachment_relative_path(saved),
                "markdown_image": markdown_image,
                "data_url": _result_url,
                "source_width": src_w,
                "source_height": src_h,
                "output_width": out_size[0] if out_size else None,
                "output_height": out_size[1] if out_size else None,
                "coordinate_transform": coord_note or None,
                "transform_meta": transform_meta,
                "applied_global_transform": (
                    {"scale_x": global_transform[0], "scale_y": global_transform[1],
                     "offset_x": global_transform[2], "offset_y": global_transform[3]}
                    if global_transform else None
                ),
                "suggested_global_transform": suggested_global_transform,
                "deliver_now": deliver_now,
                "should_retry": should_retry,
                "retry_mode": retry_mode,
                "visual_review_required": visual_review_required,
                "annotation_count": annotation_count,
                "point_annotation_count": point_annotation_count,
                "rect_like_count": rect_like_count,
                "hint": hint,
            }, ensure_ascii=False)

        if name == "list_chat_attachments":
            from api.chat_attachments import attachment_relative_path as _attachment_relative_path

            db = await get_db()
            session_id = arguments.get("session_id")
            try:
                limit = int(arguments.get("limit") or 50)
            except (TypeError, ValueError):
                limit = 50
            limit = max(1, min(200, limit))
            if session_id is not None:
                try:
                    session_id = int(session_id)
                except (TypeError, ValueError):
                    return json.dumps({"success": False, "error": "session_id 不合法"}, ensure_ascii=False)
                rows = await db.execute_fetchall(
                    """SELECT uuid, original_name, mime_type, size_bytes, kind, storage_subdir, created_at
                         FROM chat_attachments
                        WHERE user_id = ? AND session_id = ?
                        ORDER BY id DESC LIMIT ?""",
                    (user["id"], session_id, limit),
                )
            else:
                rows = await db.execute_fetchall(
                    """SELECT uuid, original_name, mime_type, size_bytes, kind, storage_subdir, created_at
                         FROM chat_attachments
                        WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
                    (user["id"], limit),
                )
            items = []
            for r in rows:
                d = dict(r)
                items.append({
                    "uuid": d.get("uuid"),
                    "name": d.get("original_name") or "",
                    "mime": d.get("mime_type") or "",
                    "size": int(d.get("size_bytes") or 0),
                    "kind": d.get("kind") or "binary",
                    "storage_subdir": d.get("storage_subdir") or "",
                    "created_at": d.get("created_at"),
                    "url": f"/api/ai/attachments/{d.get('uuid')}",
                    "fs_path": _attachment_relative_path(d),
                })
            return json.dumps({"success": True, "attachments": items}, ensure_ascii=False)

        # ── AI 成果物（artifacts）──
        if name == "create_chat_artifact":
            from api.ai_artifacts import create_artifact as _create_artifact
            title = (arguments.get("title") or "").strip()
            if not title:
                return json.dumps({"success": False, "error": "title 不能为空"}, ensure_ascii=False)
            files_arg = _normalize_create_chat_artifact_files(arguments.get("files"))
            if not isinstance(files_arg, list) or not files_arg:
                return json.dumps({
                    "success": False,
                    "error": (
                        "files 必须是非空数组，每项为 {path, content}。"
                        '正确示例：files=[{"path":"index.html","content":"<!doctype html>..."}]；'
                        "不要把 HTML/CSV 字符串直接赋给 files，也不要传单个对象而缺外层 []。"
                    ),
                }, ensure_ascii=False)
            normalized_files = []
            for idx, item in enumerate(files_arg):
                if not isinstance(item, dict):
                    return json.dumps({
                        "success": False,
                        "error": f"files[{idx}] 必须是对象 {{path, content}}，当前类型不正确",
                    }, ensure_ascii=False)
                path = (item.get("path") or "").strip()
                if not path or "content" not in item:
                    return json.dumps({
                        "success": False,
                        "error": f"files[{idx}] 缺少 path 或 content",
                    }, ensure_ascii=False)
                normalized_files.append(item)
            files_arg = normalized_files
            description = arguments.get("description") or ""
            entry_file = arguments.get("entry_file") or None
            # HTML 自包含依赖：把 manifest 中存在的包名复制到 artifact 的 libs/ 下
            libs_arg = arguments.get("libs")
            if libs_arg is not None and not isinstance(libs_arg, list):
                return json.dumps({"success": False, "error": "libs 必须是字符串数组"}, ensure_ascii=False)
            libs_subdir_arg = arguments.get("libs_subdir")
            db = await get_db()
            try:
                artifact = await _create_artifact(
                    db,
                    user,
                    title=title,
                    description=description,
                    files=files_arg,
                    entry_file=entry_file,
                    session_id=session_id,
                    libs=libs_arg,
                    libs_subdir=libs_subdir_arg,
                )
            except ValueError as exc:
                return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": f"创建失败: {exc}"}, ensure_ascii=False)
            # 附一个 markdown_link 方便 AI 直接贴到最终答复里；前端见到 `artifact:UUID` 会渲染为下载按钮。
            artifact["markdown_link"] = f"[{title}](artifact:{artifact.get('uuid')})"
            try:
                from services.session_file_resources import record_session_file_resource

                record_session_file_resource(
                    username=(user.get("username") or "default"),
                    session_id=session_id or artifact.get("session_id"),
                    kind="artifact",
                    path=(artifact.get("fs_path") or ""),
                    uuid=(artifact.get("uuid") or ""),
                    title=title,
                    entry_file=(artifact.get("entry_file") or ""),
                    note="create_chat_artifact",
                )
            except Exception:
                pass
            artifact["session_file_indexed"] = True
            return json.dumps({"success": True, "artifact": artifact}, ensure_ascii=False)

        if name == "update_chat_artifact":
            from api.ai_artifacts import update_artifact as _update_artifact
            uuid_s = (arguments.get("uuid") or "").strip()
            if not uuid_s:
                return json.dumps({"success": False, "error": "uuid 不能为空"}, ensure_ascii=False)
            files_raw = arguments.get("files")
            libs_arg = arguments.get("libs")
            if libs_arg is not None and not isinstance(libs_arg, list):
                return json.dumps({"success": False, "error": "libs 必须是字符串数组"}, ensure_ascii=False)
            normalized_files = None
            if files_raw is not None:
                files_arg = _normalize_create_chat_artifact_files(files_raw)
                if not isinstance(files_arg, list):
                    return json.dumps({
                        "success": False,
                        "error": "files 必须是数组，每项为 {path, content}",
                    }, ensure_ascii=False)
                normalized_files = []
                for idx, item in enumerate(files_arg):
                    if not isinstance(item, dict):
                        return json.dumps({
                            "success": False,
                            "error": f"files[{idx}] 必须是对象 {{path, content}}",
                        }, ensure_ascii=False)
                    path = (item.get("path") or "").strip()
                    if not path or "content" not in item:
                        return json.dumps({
                            "success": False,
                            "error": f"files[{idx}] 缺少 path 或 content",
                        }, ensure_ascii=False)
                    normalized_files.append(item)
            if not normalized_files and not libs_arg:
                return json.dumps({
                    "success": False,
                    "error": "files 与 libs 至少提供其一",
                }, ensure_ascii=False)
            db = await get_db()
            try:
                artifact = await _update_artifact(
                    db,
                    user,
                    uuid=uuid_s,
                    files=normalized_files,
                    title=(arguments.get("title") or None),
                    description=arguments.get("description"),
                    entry_file=arguments.get("entry_file") or None,
                    libs=libs_arg,
                    libs_subdir=arguments.get("libs_subdir"),
                )
            except ValueError as exc:
                return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": f"更新失败: {exc}"}, ensure_ascii=False)
            title_out = artifact.get("title") or "成果物"
            artifact["markdown_link"] = f"[{title_out}](artifact:{artifact.get('uuid')})"
            try:
                from services.session_file_resources import record_session_file_resource

                record_session_file_resource(
                    username=(user.get("username") or "default"),
                    session_id=session_id or artifact.get("session_id"),
                    kind="artifact",
                    path=(artifact.get("fs_path") or ""),
                    uuid=(artifact.get("uuid") or ""),
                    title=title_out,
                    entry_file=(artifact.get("entry_file") or ""),
                    note="update_chat_artifact",
                )
            except Exception:
                pass
            artifact["session_file_indexed"] = True
            return json.dumps({"success": True, "artifact": artifact, "updated": True}, ensure_ascii=False)

        if name == "list_chat_artifacts":
            db = await get_db()
            sid_arg = arguments.get("session_id")
            try:
                limit = int(arguments.get("limit") or 50)
            except (TypeError, ValueError):
                limit = 50
            limit = max(1, min(200, limit))
            # 省略 session_id：Agent 内默认当前会话；显式 0 = 全部
            if sid_arg is None and session_id is not None:
                sid_arg = int(session_id)
            list_all = False
            if sid_arg is not None:
                try:
                    sid_arg = int(sid_arg)
                except (TypeError, ValueError):
                    return json.dumps({"success": False, "error": "session_id 不合法"}, ensure_ascii=False)
                if sid_arg == 0:
                    list_all = True
            if sid_arg is not None and not list_all:
                rows = await db.execute_fetchall(
                    """SELECT uuid, title, description, kind, storage_subdir, entry_file,
                              file_count, total_bytes, created_at
                         FROM ai_artifacts WHERE user_id = ? AND session_id = ?
                         ORDER BY id DESC LIMIT ?""",
                    (user["id"], sid_arg, limit),
                )
            else:
                rows = await db.execute_fetchall(
                    """SELECT uuid, title, description, kind, storage_subdir, entry_file,
                              file_count, total_bytes, created_at
                         FROM ai_artifacts WHERE user_id = ?
                         ORDER BY id DESC LIMIT ?""",
                    (user["id"], limit),
                )
            from api.ai_artifacts import _workspace_relpath_for_artifact as _art_fs_path

            items = []
            for r in rows:
                d = dict(r)
                sub = d.get("storage_subdir") or ""
                entry = d.get("entry_file") or ""
                items.append(
                    {
                        "uuid": d["uuid"],
                        "title": d.get("title") or "",
                        "description": d.get("description") or "",
                        "kind": d.get("kind") or "bundle",
                        "storage_subdir": sub,
                        "entry_file": entry,
                        "fs_path": _art_fs_path(sub, entry),
                        "file_count": int(d.get("file_count") or 0),
                        "total_bytes": int(d.get("total_bytes") or 0),
                        "created_at": d.get("created_at"),
                        "download_url": f"/api/ai/artifacts/{d['uuid']}/download",
                        "markdown_link": f"[{d.get('title') or '成果物'}](artifact:{d['uuid']})",
                    }
                )
            return json.dumps({"success": True, "artifacts": items}, ensure_ascii=False)

        if name == "read_chat_artifact_file":
            from api.ai_artifacts import load_artifacts_for_user as _load_artifacts, _artifact_dir_for, _validate_relative_path
            uuid_s = (arguments.get("uuid") or "").strip()
            rel = (arguments.get("path") or "").strip()
            if not uuid_s or not rel:
                return json.dumps({"success": False, "error": "缺少 uuid 或 path"}, ensure_ascii=False)
            try:
                max_chars = int(arguments.get("max_chars") or 20_000)
            except (TypeError, ValueError):
                max_chars = 20_000
            max_chars = max(500, min(200_000, max_chars))
            db = await get_db()
            rows = await _load_artifacts(db, user["id"], [uuid_s])
            if not rows:
                return json.dumps({"success": False, "error": "artifact 不存在或无权访问"}, ensure_ascii=False)
            row = rows[0]
            try:
                rel_norm = _validate_relative_path(rel)
            except ValueError as exc:
                return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
            try:
                dir_path = _artifact_dir_for(row, user.get("username") or "default")
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"success": False, "error": f"定位失败: {exc}"}, ensure_ascii=False)
            fpath = (dir_path / rel_norm).resolve()
            try:
                fpath.relative_to(dir_path.resolve())
            except ValueError:
                return json.dumps({"success": False, "error": "路径越界"}, ensure_ascii=False)
            if not fpath.exists() or not fpath.is_file():
                return json.dumps({"success": False, "error": "文件不存在"}, ensure_ascii=False)
            try:
                raw = await asyncio.to_thread(fpath.read_bytes)
            except OSError as exc:
                return json.dumps({"success": False, "error": f"读取失败: {exc}"}, ensure_ascii=False)
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                text = raw.decode("latin-1", errors="replace")
            truncated = False
            if len(text) > max_chars:
                text = text[:max_chars]
                truncated = True
            try:
                from api.ai_artifacts import _workspace_relpath_for_artifact as _art_path
                from services.session_file_resources import record_session_file_resource

                record_session_file_resource(
                    username=(user.get("username") or "default"),
                    session_id=session_id or row.get("session_id"),
                    kind="artifact",
                    path=_art_path(row.get("storage_subdir") or "", row.get("entry_file") or ""),
                    uuid=uuid_s,
                    title=(row.get("title") or ""),
                    entry_file=rel_norm,
                    note="read_chat_artifact_file",
                )
            except Exception:
                pass
            return json.dumps({
                "success": True,
                "uuid": uuid_s,
                "path": rel_norm,
                "content": text,
                "truncated": truncated,
                "size_bytes": len(raw),
            }, ensure_ascii=False)

        if name == "batch_cancel":
            batch_id = arguments.get("batch_id")
            if batch_id is None:
                return json.dumps({"success": False, "error": "缺少 batch_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT id, created_by FROM batch_operations WHERE id = ?", (batch_id,))
            if not rows or (not _is_admin(user) and rows[0]["created_by"] != user["id"]):
                return json.dumps({"success": False, "error": "批量操作不存在"}, ensure_ascii=False)
            try:
                from api.batch import _cancel_batch
                await _cancel_batch(int(batch_id))
                return json.dumps({"success": True, "message": f"已取消批量任务 #{batch_id}"}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
        if name == "batch_retry":
            batch_id = arguments.get("batch_id")
            if batch_id is None:
                return json.dumps({"success": False, "error": "缺少 batch_id"}, ensure_ascii=False)
            db = await get_db()
            rows = await db.execute_fetchall("SELECT id, created_by FROM batch_operations WHERE id = ?", (batch_id,))
            if not rows or (not _is_admin(user) and rows[0]["created_by"] != user["id"]):
                return json.dumps({"success": False, "error": "批量操作不存在"}, ensure_ascii=False)
            try:
                from api.batch import _retry_batch
                await _retry_batch(int(batch_id))
                return json.dumps({"success": True, "message": f"已重试批量任务 #{batch_id}"}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

        if name == "submit_feedback":
            from services.feedback import create_feedback
            from services.feedback_notify import schedule_notify_admins_on_new_feedback
            content = (arguments.get("content") or "").strip()
            if not content:
                return json.dumps({"success": False, "error": "缺少 content"}, ensure_ascii=False)
            db = await get_db()
            try:
                fb = await create_feedback(
                    db, user["id"],
                    title=arguments.get("title") or "",
                    content=content,
                    category=(arguments.get("category") or "general"),
                    is_ai_submitted=True,
                )
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            schedule_notify_admins_on_new_feedback(
                db, feedback=fb, submitter_username=(user.get("username") or ""),
            )
            return json.dumps({"success": True, "feedback": fb, "message": f"已提交反馈 #{fb.get('id')}，管理员稍后处理"}, ensure_ascii=False)

        if name == "list_my_feedback":
            from services.feedback import list_feedback_for_user
            db = await get_db()
            limit = arguments.get("limit") or 50
            items = await list_feedback_for_user(db, user["id"], limit=limit)
            return json.dumps({"success": True, "items": items, "count": len(items)}, ensure_ascii=False)

        if name == "list_user_feedback_admin":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "仅管理员可调用"}, ensure_ascii=False)
            from services.feedback import list_feedback_for_admin
            db = await get_db()
            res = await list_feedback_for_admin(
                db,
                filter_kind=(arguments.get("filter") or "unread"),
                limit=arguments.get("limit") or 50,
                offset=arguments.get("offset") or 0,
            )
            return json.dumps({"success": True, **res}, ensure_ascii=False)

        if name == "get_user_feedback_detail":
            from services.feedback import get_feedback_detail, mark_feedback_read
            fb_id = arguments.get("feedback_id")
            if not fb_id:
                return json.dumps({"success": False, "error": "缺少 feedback_id"}, ensure_ascii=False)
            db = await get_db()
            is_admin = _is_admin(user)
            item = await get_feedback_detail(db, int(fb_id), requester_user_id=user["id"], is_admin=is_admin)
            if not item:
                return json.dumps({"success": False, "error": "反馈不存在或无权访问"}, ensure_ascii=False)
            if is_admin:
                await mark_feedback_read(db, int(fb_id))
            return json.dumps({"success": True, "item": item}, ensure_ascii=False)

        if name == "reply_user_feedback_admin":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "仅管理员可调用"}, ensure_ascii=False)
            from services.feedback import reply_feedback_admin
            fb_id = arguments.get("feedback_id")
            content = (arguments.get("content") or "").strip()
            if not fb_id or not content:
                return json.dumps({"success": False, "error": "需要 feedback_id 和 content"}, ensure_ascii=False)
            db = await get_db()
            try:
                reply = await reply_feedback_admin(db, int(fb_id), user["id"], content, is_ai_drafted=True)
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            return json.dumps({"success": True, "reply": reply, "message": f"已回复反馈 #{fb_id}"}, ensure_ascii=False)

        if name == "ignore_user_feedback_admin":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "仅管理员可调用"}, ensure_ascii=False)
            from services.feedback import ignore_feedback_admin
            fb_id = arguments.get("feedback_id")
            if not fb_id:
                return json.dumps({"success": False, "error": "缺少 feedback_id"}, ensure_ascii=False)
            db = await get_db()
            item = await ignore_feedback_admin(db, int(fb_id))
            if not item:
                return json.dumps({"success": False, "error": "反馈不存在"}, ensure_ascii=False)
            return json.dumps({"success": True, "item": item, "message": f"已忽略反馈 #{fb_id}"}, ensure_ascii=False)

        if name == "mark_all_user_feedback_read":
            if not _is_admin(user):
                return json.dumps({"success": False, "error": "仅管理员可调用"}, ensure_ascii=False)
            from services.feedback import mark_all_feedback_read
            db = await get_db()
            n = await mark_all_feedback_read(db)
            return json.dumps({"success": True, "marked": n, "message": f"已把 {n} 条未读反馈标为已读"}, ensure_ascii=False)

        if name == "list_search_providers":
            from services.search_config import list_user_search_configs
            db = await get_db()
            configs = await list_user_search_configs(db, user["id"])
            return json.dumps({"success": True, "providers": configs}, ensure_ascii=False)

        if name == "configure_search_provider":
            from services.search_config import upsert_user_search_config
            provider_name = (arguments.get("provider") or "").strip().lower()
            if not provider_name:
                return json.dumps({"success": False, "error": "缺少 provider"}, ensure_ascii=False)
            api_key_val = arguments.get("api_key")
            enabled_val = arguments.get("enabled")
            extra_val = arguments.get("extra") if isinstance(arguments.get("extra"), dict) else None
            db = await get_db()
            try:
                pub = await upsert_user_search_config(
                    db, user["id"], provider_name,
                    api_key=api_key_val, enabled=enabled_val, extra=extra_val,
                )
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            return json.dumps({"success": True, "config": pub, "message": f"已更新 {pub.get('display_name', provider_name)} 配置"}, ensure_ascii=False)

        if name == "search_github":
            from services.search_config import call_search
            query = (arguments.get("query") or "").strip()
            if not query:
                return json.dumps({"success": False, "error": "缺少 query"}, ensure_ascii=False)
            opts: dict = {"type": arguments.get("type") or "repositories"}
            if arguments.get("limit") is not None:
                opts["limit"] = arguments.get("limit")
            if arguments.get("sort"):
                opts["sort"] = arguments.get("sort")
            if arguments.get("order"):
                opts["order"] = arguments.get("order")
            db = await get_db()
            res = await call_search(db, user["id"], "github", query, options=opts)
            return json.dumps(res, ensure_ascii=False)

        if name == "search_web":
            from services.search_config import call_search
            query = (arguments.get("query") or "").strip()
            if not query:
                return json.dumps({"success": False, "error": "缺少 query"}, ensure_ascii=False)
            opts = {}
            for k_in, k_out in [
                ("engine_type", "engine_type"),
                ("time_range", "time_range"),
                ("limit", "limit"),
                ("with_main_text", "with_main_text"),
                ("with_markdown", "with_markdown"),
                ("with_summary", "with_summary"),
            ]:
                if arguments.get(k_in) is not None:
                    opts[k_out] = arguments.get(k_in)
            db = await get_db()
            res = await call_search(db, user["id"], "iqs", query, options=opts)
            return json.dumps(res, ensure_ascii=False)

        if name == "list_user_mcp_servers":
            from services.user_mcp_registry import list_user_mcp_servers
            db = await get_db()
            items = await list_user_mcp_servers(db, user["id"])
            return json.dumps({"success": True, "servers": items}, ensure_ascii=False)

        if name == "configure_user_mcp_server":
            from services.user_mcp_client import invalidate_user_mcp_cache
            from services.user_mcp_registry import (
                create_user_mcp_server,
                get_user_mcp_server_raw_by_name,
                update_user_mcp_server,
            )
            slug = (arguments.get("name") or "").strip()
            if not slug:
                return json.dumps({"success": False, "error": "缺少 name"}, ensure_ascii=False)
            db = await get_db()
            config_patch: dict = {}
            for key in ("command", "args", "url", "env", "headers"):
                if arguments.get(key) is not None:
                    config_patch[key] = arguments.get(key)
            transport = arguments.get("transport")
            try:
                existing = await get_user_mcp_server_raw_by_name(db, user["id"], slug)
                if existing:
                    row = await update_user_mcp_server(
                        db,
                        user["id"],
                        int(existing["id"]),
                        display_name=arguments.get("display_name"),
                        transport=transport,
                        config_patch=config_patch or None,
                        enabled=arguments.get("enabled"),
                        chat_enabled=arguments.get("chat_enabled"),
                        chat_scope_web=arguments.get("chat_scope_web"),
                        chat_scope_host=arguments.get("chat_scope_host"),
                        chat_scope_integration=arguments.get("chat_scope_integration"),
                    )
                    created = False
                else:
                    if not transport and not config_patch.get("command") and not config_patch.get("url"):
                        return json.dumps(
                            {"success": False, "error": "新建 MCP 须指定 transport 与 command 或 url"},
                            ensure_ascii=False,
                        )
                    row = await create_user_mcp_server(
                        db,
                        user["id"],
                        name=slug,
                        display_name=(arguments.get("display_name") or slug),
                        transport=(transport or "stdio"),
                        config=config_patch,
                        enabled=bool(arguments.get("enabled", True)),
                        chat_enabled=bool(arguments.get("chat_enabled", True)),
                        chat_scope_web=bool(arguments.get("chat_scope_web", True)),
                        chat_scope_host=bool(arguments.get("chat_scope_host", True)),
                        chat_scope_integration=bool(arguments.get("chat_scope_integration", True)),
                    )
                    created = True
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            invalidate_user_mcp_cache(user_id=user["id"], server_id=row.get("id"))
            return json.dumps(
                {"success": True, "created": created, "server": row, "message": "已保存 MCP 配置"},
                ensure_ascii=False,
            )

        if name == "delete_user_mcp_server":
            from services.user_mcp_client import invalidate_user_mcp_cache
            from services.user_mcp_registry import delete_user_mcp_server, get_user_mcp_server_raw_by_name
            db = await get_db()
            sid = arguments.get("server_id")
            slug = (arguments.get("name") or "").strip()
            if sid is None and slug:
                raw = await get_user_mcp_server_raw_by_name(db, user["id"], slug)
                sid = int(raw["id"]) if raw else None
            if sid is None:
                return json.dumps({"success": False, "error": "需要 name 或 server_id"}, ensure_ascii=False)
            ok = await delete_user_mcp_server(db, user["id"], int(sid))
            if not ok:
                return json.dumps({"success": False, "error": "MCP 服务器不存在"}, ensure_ascii=False)
            invalidate_user_mcp_cache(user_id=user["id"], server_id=int(sid))
            return json.dumps({"success": True, "message": "已删除 MCP 服务器"}, ensure_ascii=False)

        if name == "test_user_mcp_server":
            from services.user_mcp_client import test_user_mcp_server as _test_mcp
            from services.user_mcp_registry import get_user_mcp_server_raw, get_user_mcp_server_raw_by_name
            db = await get_db()
            sid = arguments.get("server_id")
            slug = (arguments.get("name") or "").strip()
            raw = None
            if sid is not None:
                raw = await get_user_mcp_server_raw(db, user["id"], int(sid))
            elif slug:
                raw = await get_user_mcp_server_raw_by_name(db, user["id"], slug)
            if not raw:
                return json.dumps({"success": False, "error": "MCP 服务器不存在"}, ensure_ascii=False)
            result = await _test_mcp(db, user["id"], raw)
            return json.dumps(result, ensure_ascii=False)

        if name == "import_user_mcp_config":
            from services.user_mcp_client import invalidate_user_mcp_cache
            from services.user_mcp_import import import_user_mcp_servers
            cfg = arguments.get("config")
            if cfg is None or cfg == "":
                return json.dumps({"success": False, "error": "缺少 config"}, ensure_ascii=False)
            db = await get_db()
            try:
                result = await import_user_mcp_servers(
                    db,
                    user["id"],
                    cfg,
                    overwrite=bool(arguments.get("overwrite")),
                    chat_enabled=bool(arguments.get("chat_enabled", True)),
                    chat_scope_web=bool(arguments.get("chat_scope_web", True)),
                    chat_scope_host=bool(arguments.get("chat_scope_host", True)),
                    chat_scope_integration=bool(arguments.get("chat_scope_integration", True)),
                )
            except ValueError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
            invalidate_user_mcp_cache(user_id=user["id"])
            return json.dumps(result, ensure_ascii=False)

        if name == "refresh_user_mcp_tools":
            from services.user_mcp_client import refresh_user_mcp_server_tools
            db = await get_db()
            result = await refresh_user_mcp_server_tools(
                db,
                user["id"],
                server_id=arguments.get("server_id"),
                name=(arguments.get("name") or "").strip() or None,
            )
            return json.dumps(result, ensure_ascii=False)

        if name == "export_user_mcp_config":
            from services.user_mcp_export import export_user_mcp_servers, export_user_mcp_servers_json

            db = await get_db()
            data = await export_user_mcp_servers(
                db,
                user["id"],
                include_disabled=bool(arguments.get("include_disabled", True)),
                include_edgeops_meta=bool(arguments.get("include_edgeops_meta", True)),
            )
            return json.dumps(
                {
                    "success": True,
                    "config": data,
                    "json": export_user_mcp_servers_json(data),
                    "count": len(data.get("mcpServers") or {}),
                },
                ensure_ascii=False,
            )

        if name in USER_SKILLS_AI_TOOLS:
            from services.user_skills_registry import (
                bulk_assign_skills_to_group,
                create_user_skill,
                create_user_skill_group,
                delete_user_skill,
                delete_user_skill_group,
                collect_description_warnings,
                delete_skill_resource_file,
                get_user_skill,
                get_user_skill_raw_by_name,
                list_skill_files_detail,
                list_user_skill_groups_summary,
                list_user_skills,
                read_skill_content,
                read_skill_resource_file,
                require_user_skills_access,
                resolve_skill_group_ref,
                resolve_skill_content_for_save,
                scan_user_skills_from_disk,
                update_user_skill,
                update_user_skill_group,
                write_skill_resource_file,
            )

            db = await get_db()
            try:
                await require_user_skills_access(db, user)
            except PermissionError as e:
                return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

            if name == "list_user_skills":
                items = await list_user_skills(db, user["id"], user)
                return json.dumps({"success": True, "skills": items}, ensure_ascii=False)

            if name == "list_user_skill_groups":
                groups = await list_user_skill_groups_summary(db, user["id"])
                return json.dumps({"success": True, "groups": groups}, ensure_ascii=False)

            if name == "create_user_skill_group":
                gname = (arguments.get("name") or "").strip()
                if not gname:
                    return json.dumps({"success": False, "error": "需要 name"}, ensure_ascii=False)
                try:
                    group = await create_user_skill_group(
                        db,
                        user["id"],
                        name=gname,
                        sort_order=int(arguments.get("sort_order") or 0),
                    )
                except ValueError as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                return json.dumps({"success": True, "group": group}, ensure_ascii=False)

            if name == "update_user_skill_group":
                new_name = (arguments.get("name") or "").strip()
                if not new_name:
                    return json.dumps({"success": False, "error": "需要 name（新分组名）"}, ensure_ascii=False)
                try:
                    gid = await resolve_skill_group_ref(
                        db,
                        user["id"],
                        group_id=arguments.get("group_id"),
                        group_name=(arguments.get("group_name") or "").strip() or None,
                    )
                except ValueError as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                if gid is None:
                    return json.dumps({"success": False, "error": "需要 group_id 或 group_name"}, ensure_ascii=False)
                try:
                    group = await update_user_skill_group(db, user["id"], gid, name=new_name)
                except (ValueError, LookupError) as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                return json.dumps({"success": True, "group": group}, ensure_ascii=False)

            if name == "delete_user_skill_group":
                try:
                    gid = await resolve_skill_group_ref(
                        db,
                        user["id"],
                        group_id=arguments.get("group_id"),
                        group_name=(arguments.get("group_name") or "").strip() or None,
                    )
                except ValueError as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                if gid is None:
                    return json.dumps({"success": False, "error": "需要 group_id 或 group_name"}, ensure_ascii=False)
                ok = await delete_user_skill_group(db, user["id"], gid)
                if not ok:
                    return json.dumps({"success": False, "error": "分组不存在"}, ensure_ascii=False)
                return json.dumps({"success": True}, ensure_ascii=False)

            if name == "assign_user_skills_to_group":
                all_ungrouped = bool(arguments.get("all_ungrouped"))
                skill_ids: list[int] = []
                if not all_ungrouped:
                    raw_ids = arguments.get("skill_ids") or []
                    skill_ids = [int(x) for x in raw_ids if x is not None]
                    for sname in arguments.get("skill_names") or []:
                        slug = (sname or "").strip()
                        if not slug:
                            continue
                        raw = await get_user_skill_raw_by_name(db, user["id"], slug)
                        if not raw:
                            return json.dumps(
                                {"success": False, "error": f"Skill「{slug}」不存在"},
                                ensure_ascii=False,
                            )
                        skill_ids.append(int(raw["id"]))
                    skill_ids = list(dict.fromkeys(skill_ids))
                try:
                    gid = await resolve_skill_group_ref(
                        db,
                        user["id"],
                        group_id=arguments.get("group_id"),
                        group_name=(arguments.get("group_name") or "").strip() or None,
                    )
                except ValueError as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                if all_ungrouped and gid is None:
                    return json.dumps(
                        {"success": False, "error": "all_ungrouped 需指定目标分组"},
                        ensure_ascii=False,
                    )
                try:
                    result = await bulk_assign_skills_to_group(
                        db,
                        user["id"],
                        group_id=gid,
                        skill_ids=skill_ids or None,
                        all_ungrouped=all_ungrouped,
                    )
                except (ValueError, LookupError) as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                return json.dumps({"success": True, **result}, ensure_ascii=False)

            if name == "get_user_skill":
                sid = arguments.get("skill_id")
                slug = (arguments.get("name") or "").strip()
                row = None
                if sid:
                    row = await get_user_skill(db, user["id"], int(sid), user)
                elif slug:
                    raw = await get_user_skill_raw_by_name(db, user["id"], slug)
                    if raw:
                        row = await get_user_skill(db, user["id"], int(raw["id"]), user)
                if not row:
                    return json.dumps({"success": False, "error": "Skill 不存在"}, ensure_ascii=False)
                return json.dumps({"success": True, "skill": row}, ensure_ascii=False)

            if name == "save_user_skill":
                slug = (arguments.get("name") or "").strip()
                if not slug:
                    return json.dumps({"success": False, "error": "需要 name"}, ensure_ascii=False)
                existing = await get_user_skill_raw_by_name(db, user["id"], slug)
                existing_md = ""
                if existing:
                    try:
                        existing_md = await read_skill_content(user, slug)
                    except Exception:
                        existing_md = ""
                resolved_content = resolve_skill_content_for_save(
                    slug,
                    content=arguments.get("content"),
                    description=(arguments.get("description") or ""),
                    display_name=(arguments.get("display_name") or ""),
                    body=(arguments.get("body") or ""),
                    existing_content=existing_md,
                )
                hooks_json_arg = arguments.get("hooks_json")
                if isinstance(hooks_json_arg, dict):
                    hooks_json_arg = json.dumps(hooks_json_arg, ensure_ascii=False, indent=2)
                elif hooks_json_arg is not None:
                    hooks_json_arg = str(hooks_json_arg)
                matcher_arg = arguments.get("pre_tool_use_matcher")
                decision_arg = arguments.get("pre_tool_use_decision")
                hooks_enabled_arg = arguments.get("hooks_enabled")
                # 写了 hooks_json / matcher 且未显式关 Hook 时默认开启
                if hooks_enabled_arg is None and (
                    (isinstance(hooks_json_arg, str) and hooks_json_arg.strip())
                    or (isinstance(matcher_arg, str) and matcher_arg.strip())
                ):
                    hooks_enabled_arg = True
                try:
                    group_kw: dict = {}
                    if "group_id" in arguments or "group_name" in arguments:
                        gid_val = await resolve_skill_group_ref(
                            db,
                            user["id"],
                            group_id=arguments.get("group_id"),
                            group_name=(arguments.get("group_name") or "").strip() or None,
                        )
                        group_kw["group_id"] = gid_val
                    if existing:
                        upd_kw: dict = {
                            "display_name": arguments.get("display_name"),
                            "description": arguments.get("description"),
                            "content": resolved_content,
                            "enabled": arguments.get("enabled"),
                            "chat_enabled": arguments.get("chat_enabled"),
                            "chat_scope_web": arguments.get("chat_scope_web"),
                            "chat_scope_host": arguments.get("chat_scope_host"),
                            "chat_scope_integration": arguments.get("chat_scope_integration"),
                        }
                        if "slash_name" in arguments:
                            upd_kw["slash_name"] = arguments.get("slash_name")
                        if hooks_enabled_arg is not None:
                            upd_kw["hooks_enabled"] = bool(hooks_enabled_arg)
                        if "pre_tool_use_matcher" in arguments:
                            upd_kw["pre_tool_use_matcher"] = matcher_arg
                        if "pre_tool_use_decision" in arguments:
                            upd_kw["pre_tool_use_decision"] = decision_arg
                        if "allowed_tools" in arguments:
                            upd_kw["allowed_tools"] = arguments.get("allowed_tools")
                        if "hooks_json" in arguments:
                            upd_kw["hooks_json"] = hooks_json_arg
                        if "group_id" in group_kw:
                            upd_kw["group_id"] = group_kw["group_id"]
                        row = await update_user_skill(
                            db,
                            user["id"],
                            user,
                            int(existing["id"]),
                            **upd_kw,
                        )
                    else:
                        row = await create_user_skill(
                            db,
                            user["id"],
                            user,
                            name=slug,
                            display_name=(arguments.get("display_name") or ""),
                            description=(arguments.get("description") or ""),
                            content=resolved_content,
                            enabled=bool(arguments.get("enabled", True)),
                            chat_enabled=bool(arguments.get("chat_enabled", True)),
                            chat_scope_web=bool(arguments.get("chat_scope_web", True)),
                            chat_scope_host=bool(arguments.get("chat_scope_host", True)),
                            chat_scope_integration=bool(arguments.get("chat_scope_integration", False)),
                            group_id=group_kw.get("group_id"),
                            slash_name=(arguments.get("slash_name") or ""),
                            hooks_enabled=bool(hooks_enabled_arg) if hooks_enabled_arg is not None else False,
                            pre_tool_use_matcher=(matcher_arg or "") if matcher_arg is not None else "",
                            pre_tool_use_decision=(decision_arg or "ask") if decision_arg is not None else "ask",
                            allowed_tools=(arguments.get("allowed_tools") or "")
                            if "allowed_tools" in arguments
                            else "",
                            hooks_json=hooks_json_arg if "hooks_json" in arguments else None,
                        )
                except (ValueError, LookupError) as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                warnings = collect_description_warnings(row.get("name") or slug, row.get("description") or "")
                return json.dumps({"success": True, "skill": row, "warnings": warnings}, ensure_ascii=False)

            if name == "delete_user_skill":
                sid = arguments.get("skill_id")
                slug = (arguments.get("name") or "").strip()
                if not sid and slug:
                    raw = await get_user_skill_raw_by_name(db, user["id"], slug)
                    sid = raw["id"] if raw else None
                if not sid:
                    return json.dumps({"success": False, "error": "需要 name 或 skill_id"}, ensure_ascii=False)
                ok = await delete_user_skill(
                    db,
                    user["id"],
                    user,
                    int(sid),
                    remove_files=bool(arguments.get("remove_files", False)),
                )
                if not ok:
                    return json.dumps({"success": False, "error": "Skill 不存在"}, ensure_ascii=False)
                return json.dumps({"success": True}, ensure_ascii=False)

            if name == "scan_user_skills":
                result = await scan_user_skills_from_disk(db, user["id"], user)
                items = await list_user_skills(db, user["id"], user)
                return json.dumps({"success": True, **result, "skills": items}, ensure_ascii=False)

            if name == "read_user_skill_file":
                slug = (arguments.get("name") or "").strip()
                rel = (arguments.get("path") or "").strip()
                if not slug or not rel:
                    return json.dumps({"success": False, "error": "需要 name 与 path"}, ensure_ascii=False)
                try:
                    text = read_skill_resource_file(user, slug, rel)
                    section_path = arguments.get("section_path")
                    if section_path is not None and not isinstance(section_path, list):
                        section_path = None
                    if rel.lower().endswith(".md") or bool(arguments.get("sections_only")) or arguments.get("section_index") is not None or section_path or arguments.get("heading"):
                        payload = read_markdown_document(
                            text,
                            sections_only=bool(arguments.get("sections_only")),
                            max_level=arguments.get("max_level") if arguments.get("max_level") is not None else 6,
                            section_index=arguments.get("section_index"),
                            section_path=section_path,
                            heading=arguments.get("heading"),
                            max_chars=arguments.get("max_chars"),
                            include_heading=arguments.get("include_heading") is not False,
                            include_children=arguments.get("include_children") is not False,
                        )
                        return json.dumps(
                            {"success": True, "name": slug, "path": rel.replace("\\", "/"), **payload},
                            ensure_ascii=False,
                        )
                except FileNotFoundError as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                except ValueError as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                return json.dumps(
                    {
                        "success": True,
                        "name": slug,
                        "path": rel.replace("\\", "/"),
                        "mode": "full",
                        "content": text,
                    },
                    ensure_ascii=False,
                )

            if name == "list_user_skill_files":
                slug = (arguments.get("name") or "").strip()
                if not slug:
                    return json.dumps({"success": False, "error": "需要 name"}, ensure_ascii=False)
                try:
                    files = list_skill_files_detail(user, slug)
                except ValueError as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                return json.dumps({"success": True, "name": slug, "files": files}, ensure_ascii=False)

            if name == "write_user_skill_file":
                slug = (arguments.get("name") or "").strip()
                rel = (arguments.get("path") or "").strip()
                content = arguments.get("content")
                if not slug or not rel:
                    return json.dumps({"success": False, "error": "需要 name 与 path"}, ensure_ascii=False)
                if content is None:
                    content = ""
                try:
                    out = write_skill_resource_file(
                        user,
                        slug,
                        rel,
                        content if isinstance(content, str) else str(content),
                        append=bool(arguments.get("append", False)),
                    )
                except ValueError as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                # 写入 hooks.json 时自动开启 hooks_enabled，便于 AI 一步落地 Hook
                if rel.replace("\\", "/").strip("/").lower() == "hooks.json":
                    try:
                        raw = await get_user_skill_raw_by_name(db, user["id"], slug)
                        if raw and not bool(raw.get("hooks_enabled")):
                            await update_user_skill(
                                db, user["id"], user, int(raw["id"]), hooks_enabled=True
                            )
                            out = {**out, "hooks_enabled": True, "hooks_enabled_auto": True}
                    except Exception:
                        pass
                return json.dumps(out, ensure_ascii=False)

            if name == "delete_user_skill_file":
                slug = (arguments.get("name") or "").strip()
                rel = (arguments.get("path") or "").strip()
                if not slug or not rel:
                    return json.dumps({"success": False, "error": "需要 name 与 path"}, ensure_ascii=False)
                try:
                    out = delete_skill_resource_file(user, slug, rel)
                except FileNotFoundError as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                except ValueError as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                return json.dumps(out, ensure_ascii=False)

            if name == "export_user_skills_config":
                from services.user_skills_export import export_user_skills_bundle, export_user_skills_json

                data = await export_user_skills_bundle(
                    db,
                    user["id"],
                    user,
                    include_disabled=bool(arguments.get("include_disabled", True)),
                )
                return json.dumps(
                    {
                        "success": True,
                        "config": data,
                        "json": export_user_skills_json(data),
                        "count": len(data.get("skills") or {}),
                    },
                    ensure_ascii=False,
                )

            if name == "import_user_skills_config":
                from services.user_skills_export import import_user_skills_bundle

                raw = arguments.get("data")
                if raw is None:
                    return json.dumps({"success": False, "error": "需要 data"}, ensure_ascii=False)
                try:
                    result = await import_user_skills_bundle(
                        db,
                        user["id"],
                        user,
                        raw,
                        overwrite=bool(arguments.get("overwrite", False)),
                    )
                except ValueError as e:
                    return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                items = await list_user_skills(db, user["id"], user)
                return json.dumps({"success": True, **result, "skills": items}, ensure_ascii=False)

        if name == "git_clone_on_host":
            host_id = arguments.get("host_id")
            repo_url = (arguments.get("repo_url") or "").strip()
            if not host_id or not repo_url:
                return json.dumps({"success": False, "error": "需要 host_id 和 repo_url"}, ensure_ascii=False)
            host_row = await _get_host_row(host_id)
            if not host_row:
                return json.dumps({"success": False, "error": f"主机 ID={host_id} 不存在"}, ensure_ascii=False)
            if not await _can_access_host_with_shares(host_row, user):
                return json.dumps({"success": False, "error": "无权操作该主机"}, ensure_ascii=False)
            auth = await _resolve_host_auth(await get_db(), host_row)
            if not auth:
                return json.dumps({"success": False, "error": "主机认证信息无效（凭证不存在或未配置）"}, ensure_ascii=False)
            # 拼装 git clone 命令；使用 shlex.quote 防止 URL/路径里的特殊字符注入
            import shlex as _shlex
            parts = ["git", "clone"]
            depth = arguments.get("depth")
            if depth:
                try:
                    parts += ["--depth", str(int(depth))]
                except (TypeError, ValueError):
                    pass
            branch = (arguments.get("branch") or "").strip()
            if branch:
                parts += ["--branch", _shlex.quote(branch)]
            parts.append(_shlex.quote(repo_url))
            target_dir = (arguments.get("target_dir") or "").strip()
            if target_dir:
                parts.append(_shlex.quote(target_dir))
            # 先探测 git 是否存在；不在就给出友好提示
            check_cmd = "command -v git >/dev/null 2>&1 && git --version || echo __NO_GIT__"
            try:
                stdout_chk, _, _ = await run_ssh_command(
                    host=host_row["host"], port=int(host_row.get("port") or 22),
                    username=auth.get("username") or "", auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"), key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    command=check_cmd, timeout=15,
                )
            except Exception as e:
                return json.dumps({"success": False, "error": f"探测 git 失败：{e}"}, ensure_ascii=False)
            if "__NO_GIT__" in (stdout_chk or ""):
                return json.dumps({
                    "success": False,
                    "error": "目标主机未安装 git，请先安装（如 Debian/Ubuntu: sudo apt install git；CentOS/Alibaba Linux: sudo yum install git）",
                }, ensure_ascii=False)
            clone_cmd = " ".join(parts)
            try:
                stdout, stderr, code = await run_ssh_command(
                    host=host_row["host"], port=int(host_row.get("port") or 22),
                    username=auth.get("username") or "", auth_type=auth.get("auth_type") or "password",
                    password=auth.get("password"), key_path=auth.get("key_path"),
                    private_key_pem=auth.get("private_key_pem"),
                    command=clone_cmd, timeout=300,
                )
            except Exception as e:
                return json.dumps({"success": False, "error": f"git clone 执行失败：{e}"}, ensure_ascii=False)
            return json.dumps({
                "success": code == 0,
                "command": clone_cmd,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": code,
                "git_version": (stdout_chk or "").strip(),
            }, ensure_ascii=False)

        return json.dumps({"success": False, "error": f"未知工具: {name}"}, ensure_ascii=False)
    except Exception as e:
        logger.exception("execute_tool %s failed", name)
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


def get_skills_summary():
    """返回工具名称与描述摘要，供前端或文档使用。"""
    return [{"name": t["function"]["name"], "description": t["function"].get("description", "")} for t in TOOLS]
