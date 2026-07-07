"""ClawOps 插件注册表：工具 manifest、系统提示词、统一 invoke（服务端单一事实来源）。

新增 OpenClaw 工具时优先在此扩展 manifest + invoke，claw-ops 插件通过
GET /api/integration/claw-ops/manifest 与 POST …/invoke 自动获得能力，无需发版。
"""

from __future__ import annotations

import json
import re
from typing import Any

from database import get_db

# manifest 版本：变更 extended_tools / system_prompt 时递增
CAPABILITIES_VERSION = "2026070721"
CLAW_OPS_PLUGIN_MIN_VERSION = "1.0.0"
CLAW_OPS_PLUGIN_RECOMMENDED_VERSION = "1.1.0"

_OPENCLAW_MODE_RULES = """
## 运行模式（OpenClaw / ClawOps — 无毛竹（Moso）Web UI）
- 当前通道为 OpenClaw Gateway：无浏览器 SSH 控制台、无 SSE、无按钮 UI。
- 非交互短命令优先 `edgeops_ssh_execute`（长任务 detach + poll_log）；交互式用 `edgeops_ssh_channel_*`。
- ssh_channel 内嵌套 SSH/sudo 出现 password 提示：`edgeops_list_service_credentials` → `edgeops_send_service_password`（勿 ssh_channel_send 发明文；需 credentials_vault_enabled）。
- 复杂多步仍可用 `edgeops_ops_chat`（可能阻塞 ≤330s）；**编排式后台子任务仅 MCP**，ClawOps 不提供 orchestrate。
- 禁止依赖 connect_terminal / ask_user_choice；需要用户确认时用纯文本 [A]/[B] 选项。
- 大输出 spill 后用 `edgeops_read_chat_data` 分段读取。
""".strip()


def _tool_schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


def get_extended_tools_manifest() -> list[dict[str, Any]]:
    """ClawOps 动态注册工具（P1/P2，不含 MCP 专用 orchestrate）。"""
    int_opt = {"type": "integer"}
    str_opt = {"type": "string"}
    bool_t = {"type": "boolean"}
    return [
        {
            "name": "edgeops_ssh_execute",
            "label": "毛竹（Moso） · 非交互 SSH",
            "description": "在主机上执行 SSH 命令（detach/poll_log）；无 Web UI。",
            "timeout_ms": 330_000,
            "parameters_schema": _tool_schema(
                {
                    "host_id": {**int_opt, "description": "主机 ID"},
                    "command": {**str_opt, "description": "Shell 命令"},
                    "timeout": {**int_opt, "description": "秒，5–300"},
                    "detach": bool_t,
                    "poll_log": bool_t,
                    "log_path": str_opt,
                    "tail_lines": int_opt,
                    "session_id": {**int_opt, "description": "poll_log 运行态会话"},
                },
                ["host_id", "command"],
            ),
        },
        {
            "name": "edgeops_list_host_groups",
            "label": "毛竹（Moso） · 主机分组列表",
            "description": "GET /api/host-groups",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema({}),
        },
        {
            "name": "edgeops_get_host_groups_tree",
            "label": "毛竹（Moso） · 分组树",
            "description": "GET /api/host-groups/tree",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema({}),
        },
        {
            "name": "edgeops_get_group_hosts",
            "label": "毛竹（Moso） · 分组内主机",
            "description": "GET /api/host-groups/{group_id}/hosts",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema(
                {"group_id": {**int_opt, "description": "分组 ID"}},
                ["group_id"],
            ),
        },
        {
            "name": "edgeops_probe_host_capabilities",
            "label": "毛竹（Moso） · 探测能力画像",
            "description": "SSH 探测主机 CLI/OS 画像并写入提示词哨兵块",
            "timeout_ms": 120_000,
            "parameters_schema": _tool_schema(
                {
                    "host_id": int_opt,
                    "refresh": bool_t,
                    "max_age_hours": int_opt,
                    "timeout": int_opt,
                },
                ["host_id"],
            ),
        },
        {
            "name": "edgeops_get_host_capabilities",
            "label": "毛竹（Moso） · 读取能力画像",
            "description": "读取已缓存的结构化能力画像",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema({"host_id": int_opt}, ["host_id"]),
        },
        {
            "name": "edgeops_update_host_prompt",
            "label": "毛竹（Moso） · 覆盖主机提示词",
            "description": "PUT 主机级 AI 提示词",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema(
                {"host_id": int_opt, "content": str_opt},
                ["host_id", "content"],
            ),
        },
        {
            "name": "edgeops_append_host_prompt",
            "label": "毛竹（Moso） · 追加主机提示词",
            "description": "追加主机级 AI 提示词",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema(
                {"host_id": int_opt, "text": str_opt},
                ["host_id", "text"],
            ),
        },
        {
            "name": "edgeops_list_maintenance_history",
            "label": "毛竹（Moso） · 维护历史",
            "description": "只读维护历史列表",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema(
                {
                    "host": str_opt,
                    "category": str_opt,
                    "page": int_opt,
                    "page_size": int_opt,
                }
            ),
        },
        {
            "name": "edgeops_list_operation_logs",
            "label": "毛竹（Moso） · 操作审计",
            "description": "只读操作日志",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema(
                {"page": int_opt, "page_size": int_opt, "host_id": int_opt}
            ),
        },
        {
            "name": "edgeops_list_service_credentials",
            "label": "毛竹（Moso） · 搜索服务凭证",
            "description": (
                "搜索/列出服务凭证元数据（不含密码）。跨机 SSH/SCP/sudo 前先调用。"
                "支持 command_hint、service+address、keyword 等；返回 resolution 与 suggested_credential_id。"
                "需 credentials_vault_enabled。"
            ),
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema(
                {
                    "command_hint": {**str_opt, "description": "待执行命令，推断 service+address"},
                    "service": {**str_opt, "description": "ssh、mysql、sudo 等"},
                    "address": {**str_opt, "description": "目标 IP/域名"},
                    "port": int_opt,
                    "service_username": str_opt,
                    "keyword": {**str_opt, "description": "模糊搜索 id/address/label/notes 等"},
                    "sort_by": str_opt,
                    "sort_order": str_opt,
                    "limit": int_opt,
                }
            ),
        },
        {
            "name": "edgeops_send_service_password",
            "label": "毛竹（Moso） · 注入服务密码",
            "description": (
                "按 credential_id 向 ssh_channel/terminal 注入密码（结果不含明文）。"
                "MCP/OpenClaw 直连 ssh_channel 嵌套 SSH 时用 target=ssh_channel + channel_id。"
                "需 credentials_vault_enabled。"
            ),
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema(
                {
                    "credential_id": {**int_opt, "description": "来自 list_service_credentials"},
                    "target": {
                        **str_opt,
                        "enum": ["terminal", "ssh_channel", "local_terminal"],
                        "description": "注入目标",
                    },
                    "channel_id": {**int_opt, "description": "target=ssh_channel 时必填"},
                    "host_id": {**int_opt, "description": "target=terminal 时必填"},
                    "slot": int_opt,
                    "require_password_prompt": bool_t,
                },
                ["credential_id", "target"],
            ),
        },
        {
            "name": "edgeops_remote_fs_list",
            "label": "毛竹（Moso） · 远程目录",
            "description": "SFTP 列出远程目录",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema(
                {"host_id": int_opt, "path": {**str_opt, "default": "/"}},
                ["host_id"],
            ),
        },
        {
            "name": "edgeops_remote_fs_read",
            "label": "毛竹（Moso） · 远程读文件",
            "description": "SFTP 读取远程文本文件",
            "timeout_ms": 120_000,
            "parameters_schema": _tool_schema(
                {"host_id": int_opt, "path": str_opt},
                ["host_id", "path"],
            ),
        },
        {
            "name": "edgeops_remote_fs_write",
            "label": "毛竹（Moso） · 远程写文件",
            "description": "SFTP 写入远程文本（≤2MB）",
            "timeout_ms": 120_000,
            "parameters_schema": _tool_schema(
                {"host_id": int_opt, "path": str_opt, "content": str_opt},
                ["host_id", "path"],
            ),
        },
        {
            "name": "edgeops_list_batch_jobs",
            "label": "毛竹（Moso） · 批量任务列表",
            "description": "只读批量任务",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema(
                {
                    "page": int_opt,
                    "page_size": int_opt,
                    "operation_type": str_opt,
                    "status": str_opt,
                }
            ),
        },
        {
            "name": "edgeops_get_batch_job",
            "label": "毛竹（Moso） · 批量任务详情",
            "description": "只读批量任务详情",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema({"batch_id": int_opt}, ["batch_id"]),
        },
        {
            "name": "edgeops_list_scheduled_tasks",
            "label": "毛竹（Moso） · 定时任务列表",
            "description": "只读定时任务",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema({}),
        },
        {
            "name": "edgeops_get_scheduled_task",
            "label": "毛竹（Moso） · 定时任务详情",
            "description": "只读定时任务详情",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema({"task_id": int_opt}, ["task_id"]),
        },
        {
            "name": "edgeops_list_triggered_tasks",
            "label": "毛竹（Moso） · 触发任务列表",
            "description": "只读触发式任务",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema({}),
        },
        {
            "name": "edgeops_get_triggered_task",
            "label": "毛竹（Moso） · 触发任务详情",
            "description": "只读触发式任务详情",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema({"task_id": int_opt}, ["task_id"]),
        },
        {
            "name": "edgeops_list_session_messages",
            "label": "毛竹（Moso） · 集成会话消息",
            "description": "只读 integration 会话历史",
            "timeout_ms": 60_000,
            "parameters_schema": _tool_schema(
                {"session_id": int_opt, "limit": int_opt},
                ["session_id"],
            ),
        },
        {
            "name": "edgeops_http_request",
            "label": "毛竹（Moso） · HTTP 请求",
            "description": "HTTP/HTTPS 出站请求（GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS）",
            "timeout_ms": 600_000,
            "parameters_schema": _tool_schema(
                {
                    "url": {**str_opt, "description": "完整 URL"},
                    "method": {**str_opt, "description": "HTTP 方法，默认 GET"},
                    "headers": {"type": "object"},
                    "query": {"type": "object"},
                    "body": str_opt,
                    "body_encoding": str_opt,
                    "timeout": int_opt,
                    "max_response_bytes": int_opt,
                    "follow_redirects": bool_t,
                    "session_id": int_opt,
                },
                ["url"],
            ),
        },
        {
            "name": "edgeops_http_download",
            "label": "毛竹（Moso） · HTTP 下载",
            "description": "从 HTTP/HTTPS URL 下载到用户 web/fs（无体积上限；可选 Range 分块并合并）",
            "timeout_ms": 3_600_000,
            "parameters_schema": _tool_schema(
                {
                    "url": str_opt,
                    "local_path": {**str_opt, "description": "相对工作区路径"},
                    "headers": {"type": "object"},
                    "session_managed": bool_t,
                    "max_bytes": int_opt,
                    "chunked": bool_t,
                    "chunk_size": int_opt,
                    "chunk_index": int_opt,
                    "merge_chunks": bool_t,
                    "delete_parts": bool_t,
                    "timeout": int_opt,
                    "follow_redirects": bool_t,
                    "session_id": int_opt,
                },
                ["url", "local_path"],
            ),
        },
        {
            "name": "edgeops_http_download_merge",
            "label": "毛竹（Moso） · HTTP 分块合并",
            "description": "合并 local_path.part* 分块为最终文件",
            "timeout_ms": 600_000,
            "parameters_schema": _tool_schema(
                {
                    "local_path": {**str_opt, "description": "合并输出路径"},
                    "part_paths": {"type": "array", "items": {"type": "string"}},
                    "delete_parts": bool_t,
                    "session_id": int_opt,
                },
                ["local_path"],
            ),
        },
        {
            "name": "edgeops_http_upload",
            "label": "毛竹（Moso） · HTTP 上传",
            "description": "从用户 web/fs 上传文件到 HTTP/HTTPS URL（无体积上限，流式，显示进度）",
            "timeout_ms": 3_600_000,
            "parameters_schema": _tool_schema(
                {
                    "url": str_opt,
                    "local_path": {**str_opt, "description": "相对工作区文件路径"},
                    "method": str_opt,
                    "headers": {"type": "object"},
                    "field_name": str_opt,
                    "form_fields": {"type": "object"},
                    "content_type": str_opt,
                    "multipart": bool_t,
                    "max_bytes": int_opt,
                    "timeout": int_opt,
                    "follow_redirects": bool_t,
                    "session_id": int_opt,
                },
                ["url", "local_path"],
            ),
        },
    ]


def build_system_prompt_sections(base_url: str = "https://ops.pinglan.cc") -> list[str]:
    root = (base_url or "https://ops.pinglan.cc").strip().rstrip("/")
    return [
        "## ClawOps（claw-ops）· 毛竹（Moso）运维插件",
        f"- **毛竹（Moso）根地址**：`{root}`（插件 config.baseUrl 可改）",
        "- **执行流**：ClawOps 在 OpenClaw Gateway 内通过 HTTP 调毛竹（Moso）REST；**不依赖**浏览器终端 UI。",
        "- **关键词**：运维/主机/排障/SSH → 优先 `edgeops_*`；**禁止**本机 exec/curl 打毛竹（Moso）。",
        "- **资产解析**：`edgeops_search_hosts` / `edgeops_search_hosts_by_prompt` / `edgeops_get_host_prompt`。",
        "- **短命令**：`edgeops_ssh_execute`（安装/编译等长任务用 detach + poll_log）。",
        "- **交互 SSH**：sudo/vi/向导 → `edgeops_ssh_channel_*`（create → send → read → close）；password 提示 → `edgeops_list_service_credentials` + `edgeops_send_service_password`。",
        "- **复杂编排**：`edgeops_ops_chat`（可能较慢）；后台编排子任务请用 **毛竹（Moso）MCP**（OpenClaw 不提供 orchestrate）。",
        "- **Bearer** 仅插件 config；禁止在回复中泄露 `eop_`。",
        _OPENCLAW_MODE_RULES,
    ]


def get_claw_ops_manifest(*, base_url: str = "") -> dict[str, Any]:
    tools = get_extended_tools_manifest()
    sections = build_system_prompt_sections(base_url)
    prompt_text = "\n".join(sections)
    return {
        "success": True,
        "capabilities_version": CAPABILITIES_VERSION,
        "plugin": {
            "npm_package": "@edgeops/claw-ops",
            "min_version": CLAW_OPS_PLUGIN_MIN_VERSION,
            "recommended_version": CLAW_OPS_PLUGIN_RECOMMENDED_VERSION,
        },
        "system_prompt": {
            "prepend_markdown": prompt_text,
            "sections": sections,
            "etag": CAPABILITIES_VERSION,
        },
        "extended_tools": tools,
        "core_tools": [
            "edgeops_gateway_ping",
            "edgeops_list_hosts",
            "edgeops_search_hosts",
            "edgeops_search_hosts_by_prompt",
            "edgeops_get_host",
            "edgeops_get_host_prompt",
            "edgeops_list_host_tags",
            "edgeops_host_alive",
            "edgeops_host_stats",
            "edgeops_search_best_practices",
            "edgeops_ops_chat",
            "edgeops_ssh_channel_create",
            "edgeops_ssh_channel_list",
            "edgeops_ssh_channel_info",
            "edgeops_ssh_channel_send",
            "edgeops_ssh_channel_read_lines",
            "edgeops_ssh_channel_read",
            "edgeops_ssh_channel_has_new",
            "edgeops_ssh_channel_close",
            "edgeops_ssh_channel_dump",
            "edgeops_ssh_channel_close_batch",
            "edgeops_read_chat_data",
            "edgeops_invoke",
        ],
        "routing_hints": [
            "名词→host_id: search_hosts / search_hosts_by_prompt",
            "短命令: edgeops_ssh_execute",
            "TTY: ssh_channel_*; password: list_service_credentials + send_service_password",
            "复杂: edgeops_ops_chat",
            "未知新工具: edgeops_invoke(tool, arguments)",
        ],
    }


def _parse_semver(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", (v or "0").strip())
    return tuple(int(p) for p in parts[:3]) if parts else (0,)


def check_plugin_update(plugin_version: str) -> dict[str, Any]:
    cur = _parse_semver(plugin_version)
    rec = _parse_semver(CLAW_OPS_PLUGIN_RECOMMENDED_VERSION)
    min_v = _parse_semver(CLAW_OPS_PLUGIN_MIN_VERSION)
    needs_update = cur < rec
    incompatible = cur < min_v
    return {
        "success": True,
        "plugin_version": plugin_version,
        "min_version": CLAW_OPS_PLUGIN_MIN_VERSION,
        "recommended_version": CLAW_OPS_PLUGIN_RECOMMENDED_VERSION,
        "capabilities_version": CAPABILITIES_VERSION,
        "needs_update": needs_update,
        "incompatible": incompatible,
        "message": (
            f"建议升级 claw-ops 至 {CLAW_OPS_PLUGIN_RECOMMENDED_VERSION}"
            if needs_update and not incompatible
            else (
                f"当前 claw-ops {plugin_version} 低于最低要求 {CLAW_OPS_PLUGIN_MIN_VERSION}"
                if incompatible
                else "claw-ops 版本满足要求；扩展工具由服务端 manifest 下发"
            )
        ),
    }


async def _ensure_integration_runtime_session(db, user: dict, session_id: int | None) -> int:
    from api.integration_mcp import _ensure_mcp_runtime_session

    return await _ensure_mcp_runtime_session(db, user, session_id)


async def invoke_claw_ops_tool(
    db,
    user: dict,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统一工具 invoke；返回 dict（非 JSON 字符串）。"""
    from services.ai_skills import execute_tool

    name = (tool_name or "").strip()
    args = dict(arguments or {})
    allowed = {t["name"] for t in get_extended_tools_manifest()}
    if name not in allowed:
        return {"success": False, "error": f"未知或未授权工具: {name}"}

    if name == "edgeops_ssh_execute":
        sid = await _ensure_integration_runtime_session(db, user, args.pop("session_id", None))
        internal = {
            "host_id": args.get("host_id"),
            "command": args.get("command"),
            "detach": bool(args.get("detach", False)),
            "poll_log": bool(args.get("poll_log", False)),
            "timeout": args.get("timeout"),
            "log_path": args.get("log_path"),
            "tail_lines": args.get("tail_lines"),
        }
        raw = await execute_tool(
            "ssh_execute",
            {k: v for k, v in internal.items() if v is not None},
            user,
            scope="default",
            ui_capable=False,
            session_id=sid,
        )
        out = json.loads(raw)
        out["session_id"] = sid
        return out

    if name in ("edgeops_probe_host_capabilities", "edgeops_get_host_capabilities"):
        internal_name = name.replace("edgeops_", "")
        raw = await execute_tool(
            internal_name,
            args,
            user,
            scope="default",
            ui_capable=False,
        )
        return json.loads(raw)

    if name in ("edgeops_update_host_prompt", "edgeops_append_host_prompt"):
        internal_name = name.replace("edgeops_", "")
        raw = await execute_tool(internal_name, args, user, scope="default", ui_capable=False)
        return json.loads(raw)

    if name == "edgeops_list_host_groups":
        from api.host_groups import list_groups

        return await list_groups(user=user)

    if name == "edgeops_get_host_groups_tree":
        from api.host_groups import tree_groups

        return await tree_groups(user=user)

    if name == "edgeops_get_group_hosts":
        from api.host_groups import get_group_hosts

        gid = args.get("group_id")
        if gid is None:
            return {"success": False, "error": "缺少 group_id"}
        return await get_group_hosts(int(gid), user=user)

    if name == "edgeops_list_maintenance_history":
        from api.maintenance_history import list_history

        return await list_history(
            host=args.get("host"),
            category=args.get("category"),
            page=int(args.get("page") or 1),
            page_size=int(args.get("page_size") or 20),
            user=user,
        )

    if name == "edgeops_list_operation_logs":
        from api.settings import list_logs

        return await list_logs(
            page=int(args.get("page") or 1),
            page_size=int(args.get("page_size") or 20),
            host_id=args.get("host_id"),
            user=user,
        )

    if name == "edgeops_list_service_credentials":
        raw = await execute_tool(
            "list_service_credentials",
            {k: v for k, v in args.items() if v is not None},
            user,
            scope="default",
            ui_capable=False,
        )
        return json.loads(raw)

    if name == "edgeops_send_service_password":
        raw = await execute_tool(
            "send_service_password",
            {k: v for k, v in args.items() if v is not None},
            user,
            scope="default",
            ui_capable=False,
        )
        return json.loads(raw)

    if name == "edgeops_remote_fs_list":
        from api.remote_fs import remote_list

        return await remote_list(
            host_id=int(args["host_id"]),
            path=str(args.get("path") or "/"),
            user=user,
        )

    if name == "edgeops_remote_fs_read":
        from api.remote_fs import remote_read

        return await remote_read(
            host_id=int(args["host_id"]),
            path=str(args["path"]),
            user=user,
        )

    if name == "edgeops_remote_fs_write":
        from api.remote_fs import WriteBody, remote_write

        return await remote_write(
            host_id=int(args["host_id"]),
            path=str(args["path"]),
            body=WriteBody(content=str(args.get("content") or "")),
            user=user,
        )

    if name == "edgeops_list_batch_jobs":
        from api.batch import list_batches

        return await list_batches(
            page=int(args.get("page") or 1),
            page_size=int(args.get("page_size") or 20),
            operation_type=args.get("operation_type"),
            status=args.get("status"),
            user=user,
        )

    if name == "edgeops_get_batch_job":
        from api.batch import get_batch

        return await get_batch(int(args["batch_id"]), user=user)

    if name == "edgeops_list_scheduled_tasks":
        from api.scheduled_tasks import list_scheduled_tasks

        return await list_scheduled_tasks(user=user)

    if name == "edgeops_get_scheduled_task":
        from api.scheduled_tasks import get_scheduled_task

        return await get_scheduled_task(int(args["task_id"]), user=user)

    if name == "edgeops_list_triggered_tasks":
        from api.triggered_tasks import list_triggered_tasks

        return await list_triggered_tasks(user=user)

    if name == "edgeops_get_triggered_task":
        from api.triggered_tasks import get_triggered_task

        return await get_triggered_task(int(args["task_id"]), user=user)

    if name == "edgeops_list_session_messages":
        from services.integration_session_helpers import list_integration_scope_messages

        return await list_integration_scope_messages(
            db,
            user,
            int(args["session_id"]),
            limit=int(args.get("limit") or 50),
        )

    if name in ("edgeops_http_request", "edgeops_http_download", "edgeops_http_upload", "edgeops_http_download_merge"):
        internal_name = name.replace("edgeops_", "")
        sid = await _ensure_integration_runtime_session(db, user, args.pop("session_id", None))
        raw = await execute_tool(
            internal_name,
            {k: v for k, v in args.items() if v is not None},
            user,
            scope="default",
            ui_capable=False,
            session_id=sid,
        )
        out = json.loads(raw)
        out["session_id"] = sid
        return out

    return {"success": False, "error": f"工具 {name} 尚未实现 invoke"}
