"""毛竹（Moso）MCP 工具注册 — 与 claw-ops 同名；Token 仅走 HTTP 头 / 环境变量。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Awaitable

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from services.edgeops_mcp.client import create_client
from services.edgeops_mcp.context import (
    bind_call_context,
    resolve_access_token,
    resolve_integration_session_id,
)
from services.output_wait import (
    clamp_until_wait_seconds,
    normalize_until_contains,
    poll_until_contains,
)
from services.terminal_poll import attach_ssh_channel_wait_fields, clamp_ssh_channel_wait_seconds

mcp = FastMCP(
    "edgeops",
    instructions=(
        "毛竹（Moso）MCP：edgeops_* 运维工具（含 MCP 专用编排 ops_orchestrate_*）。"
        "鉴权：MCP 客户端配置 Authorization: Bearer（HTTP）或 EDGEOPS_ACCESS_TOKEN（stdio）。"
        "HTTP 调用会自动带 X-EdgeOps-Client: mcp。"
        "多会话：session_id 参数，或 HTTP 头 X-EdgeOps-Session-Id，或 edgeops_context_bind。"
        "无 Web UI：勿依赖 connect_terminal / ask_user_choice；长任务用 ops_orchestrate_chat + ops_task_*。"
        "若 ops-chat 触发 Web 终端 batch 末等待，可用 POST /api/ai/sessions/{session_id}/runtime-control action=wake 跳过。"
        "ssh_channel 读工具：wait_seconds=1～30（无 until 时直调静默 sleep；0=立即）；"
        "亦可 until_contains（字面子串，超时内轮询至命中或超时，默认超时 30s）。"
        "SSH 通道内嵌套登录：先 edgeops_list_service_credentials，再 edgeops_send_service_password（勿 ssh_channel_send 发明文）。"
    ),
    # 挂载到主 Web 的 /mcp 时，子应用内路由为 `/`；独立 --http 进程由 mount 层再包一层 /mcp。
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


async def _run(coro, *, ctx: Context | None = None) -> str:
    try:
        client = create_client(ctx=ctx)
        return _json(await coro(client))
    except Exception as exc:
        return _json({"success": False, "error": str(exc)})


def _channel_read_haystack(data: Any) -> str:
    """从读通道响应拼出可匹配文本。"""
    if not isinstance(data, dict):
        return ""
    parts: list[str] = []
    for key in ("tail_text", "text", "content", "content_preview", "pending_partial", "last_line"):
        val = data.get(key)
        if isinstance(val, str) and val:
            parts.append(val)
    lines = data.get("lines")
    if isinstance(lines, list):
        for item in lines[-80:]:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("text") or item.get("line") or item.get("content")
                if isinstance(t, str) and t:
                    parts.append(t)
    return "\n".join(parts)


async def _run_ssh_channel_read(
    coro_factory: Callable[[Any], Awaitable[Any]],
    *,
    wait_seconds: int | None,
    until_contains: str | None = None,
    until_haystack_factory: Callable[[Any], Awaitable[str]] | None = None,
    session_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """直调读通道：until_contains 时工具内轮询；否则成功后按 wait_seconds 静默 sleep。"""
    needle = normalize_until_contains(until_contains)
    wait = clamp_ssh_channel_wait_seconds(wait_seconds)
    try:
        client = create_client(ctx=ctx)
        if needle:
            timeout = clamp_until_wait_seconds(wait_seconds, default=30, max_sec=30)
            last_data: dict[str, Any] = {}

            async def _fetch_raw() -> tuple[str, dict]:
                nonlocal last_data
                data = await coro_factory(client)
                if not isinstance(data, dict):
                    last_data = {"success": False, "error": "invalid response"}
                    return "", last_data
                last_data = data
                if data.get("error") and data.get("success") is False:
                    return "", data
                haystack = _channel_read_haystack(data)
                if until_haystack_factory is not None:
                    try:
                        extra = await until_haystack_factory(client)
                        if extra:
                            haystack = (haystack + "\n" + extra).strip()
                    except Exception:
                        pass
                return haystack, {"_payload": data}

            reason, snippet, _, _ = await poll_until_contains(
                fetch_raw=_fetch_raw,
                needle=needle,
                timeout_sec=timeout,
                session_id=session_id,
                match_mode="full",
            )
            data = last_data if isinstance(last_data, dict) else {}
            if data.get("error") and data.get("success") is False:
                return _json(data)
            data["until_contains"] = needle
            data["until_wait_reason"] = reason or "timeout"
            data["until_wait_done"] = True
            if snippet:
                data["until_matched_snippet"] = snippet
            if reason == "matched" and "has_new" in data:
                data["has_new"] = True
            return _json(data)
        data = await coro_factory(client)
        if isinstance(data, dict) and not data.get("error"):
            attach_ssh_channel_wait_fields(data, {"wait_seconds": wait})
            if wait > 0:
                await asyncio.sleep(wait)
        return _json(data)
    except Exception as exc:
        return _json({"success": False, "error": str(exc)})


def _session(explicit: int | None, ctx: Context | None) -> int | None:
    return resolve_integration_session_id(explicit, ctx)


@mcp.tool()
async def edgeops_context_bind(
    integration_session_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """绑定默认 integration session_id（Token 仍来自 MCP 客户端 headers/env）。"""
    try:
        token = resolve_access_token(ctx)
        bind_call_context(token, integration_session_id)
        return _json(
            {
                "success": True,
                "bound": True,
                "integration_session_id": integration_session_id,
                "note": "后续 session 相关工具可省略 session_id",
            }
        )
    except Exception as exc:
        return _json({"success": False, "error": str(exc)})


@mcp.tool()
async def edgeops_gateway_ping(ctx: Context | None = None) -> str:
    """检查毛竹（Moso）服务是否可达（GET /api/version）。"""
    return await _run(lambda c: c.get_version(), ctx=ctx)


@mcp.tool()
async def edgeops_list_hosts(
    page: int = 1,
    page_size: int = 100,
    ctx: Context | None = None,
) -> str:
    """分页列出毛竹（Moso）主机（GET /api/hosts）。"""
    return await _run(
        lambda c: c.list_hosts(page=page, page_size=page_size),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_search_hosts(
    query: str,
    group_id: int | None = None,
    tag_ids: list[int] | None = None,
    limit: int = 50,
    ctx: Context | None = None,
) -> str:
    """按名称/IP/别名/标签/remark 检索主机。"""
    return await _run(
        lambda c: c.search_hosts(query, group_id=group_id, tag_ids=tag_ids, limit=limit),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_search_hosts_by_prompt(
    query: str,
    group_id: int | None = None,
    tag_ids: list[int] | None = None,
    limit: int = 30,
    ctx: Context | None = None,
) -> str:
    """在主机级 AI 提示词中搜索。"""
    return await _run(
        lambda c: c.search_hosts_by_prompt(
            query, group_id=group_id, tag_ids=tag_ids, limit=limit
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_get_host(host_id: int, ctx: Context | None = None) -> str:
    """GET /api/hosts/{host_id}。"""
    return await _run(lambda c: c.get_host(host_id), ctx=ctx)


@mcp.tool()
async def edgeops_get_host_prompt(host_id: int, ctx: Context | None = None) -> str:
    """GET /api/ai/hosts/{host_id}/prompt。"""
    return await _run(lambda c: c.get_host_prompt(host_id), ctx=ctx)


@mcp.tool()
async def edgeops_list_host_tags(ctx: Context | None = None) -> str:
    """GET /api/host-tags。"""
    return await _run(lambda c: c.list_host_tags(), ctx=ctx)


@mcp.tool()
async def edgeops_host_alive(host_id: int, ctx: Context | None = None) -> str:
    """GET /api/hosts/{host_id}/alive。"""
    return await _run(lambda c: c.host_alive(host_id), ctx=ctx)


@mcp.tool()
async def edgeops_host_stats(ctx: Context | None = None) -> str:
    """GET /api/hosts/stats。"""
    return await _run(lambda c: c.host_stats(), ctx=ctx)


@mcp.tool()
async def edgeops_search_best_practices(
    keyword: str | None = None,
    category: str | None = None,
    page: int = 1,
    page_size: int = 20,
    ctx: Context | None = None,
) -> str:
    """GET /api/best-practices。"""
    return await _run(
        lambda c: c.search_best_practices(
            keyword=keyword, category=category, page=page, page_size=page_size
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_ops_chat(
    message: str,
    session_id: int | None = None,
    host_id: int | None = None,
    skip_secondary_assistant: bool = True,
    ui_locale: str | None = None,
    ctx: Context | None = None,
) -> str:
    """POST /api/integration/ops-chat/complete — 集成运维 Agent。"""
    sid = _session(session_id, ctx)

    async def _call(c):
        return await c.ops_chat_complete(
            message,
            session_id=sid,
            host_id=host_id,
            skip_secondary_assistant=skip_secondary_assistant,
            ui_locale=ui_locale,
        )

    result = await _run(_call, ctx=ctx)
    try:
        parsed = json.loads(result)
        if parsed.get("success") and parsed.get("session_id") and sid is None:
            token = resolve_access_token(ctx)
            bind_call_context(token, int(parsed["session_id"]))
    except Exception:
        pass
    return result


@mcp.tool()
async def edgeops_ssh_channel_create(
    host_id: int,
    session_id: int | None = None,
    idle_close_sec: int | None = None,
    ctx: Context | None = None,
) -> str:
    """创建 SSH 交互通道。"""
    sid = _session(session_id, ctx)
    return await _run(
        lambda c: c.ssh_channel_create(
            host_id, session_id=sid, idle_close_sec=idle_close_sec
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_ssh_channel_list(
    all_open: bool = False,
    owner_type: str | None = None,
    owner_id: str | None = None,
    session_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """列出 SSH 通道；未传 owner 且提供 session_id 时按 integration 会话筛选。"""
    sid = _session(session_id, ctx)
    otype = owner_type
    oid = owner_id
    if not oid and sid is not None and not otype:
        otype, oid = "session", str(sid)
    return await _run(
        lambda c: c.ssh_channel_list(all_open=all_open, owner_type=otype, owner_id=oid),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_ssh_channel_info(
    channel_id: int,
    check_alive: bool = False,
    ctx: Context | None = None,
) -> str:
    """SSH 通道详情。"""
    return await _run(
        lambda c: c.ssh_channel_info(channel_id, check_alive=check_alive),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_ssh_channel_send(
    channel_id: int,
    content: str,
    ctx: Context | None = None,
) -> str:
    """向 SSH 通道 stdin 发送内容。"""
    return await _run(
        lambda c: c.ssh_channel_send(channel_id, content),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_ssh_channel_read_lines(
    channel_id: int,
    since_line: int | None = None,
    last_n: int | None = None,
    session_id: int | None = None,
    wait_seconds: int | None = None,
    until_contains: str | None = None,
    ctx: Context | None = None,
) -> str:
    """按行读通道。无 until_contains：wait_seconds=1～30 读完后静默等；0/省略=立即。
    有 until_contains：超时内轮询至字面子串出现（wait_seconds 为超时，默认 30）。"""
    sid = _session(session_id, ctx)
    return await _run_ssh_channel_read(
        lambda c: c.ssh_channel_read_lines(
            channel_id, since_line=since_line, last_n=last_n, session_id=sid
        ),
        wait_seconds=wait_seconds,
        until_contains=until_contains,
        session_id=sid,
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_ssh_channel_read(
    channel_id: int,
    max_chars: int | None = None,
    session_id: int | None = None,
    wait_seconds: int | None = None,
    until_contains: str | None = None,
    ctx: Context | None = None,
) -> str:
    """按字符读通道最近输出。可选 wait_seconds / until_contains（同 read_lines）。"""
    sid = _session(session_id, ctx)
    return await _run_ssh_channel_read(
        lambda c: c.ssh_channel_read(channel_id, max_chars=max_chars, session_id=sid),
        wait_seconds=wait_seconds,
        until_contains=until_contains,
        session_id=sid,
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_ssh_channel_has_new(
    channel_id: int,
    after_line: int | None = None,
    session_id: int | None = None,
    wait_seconds: int | None = None,
    until_contains: str | None = None,
    ctx: Context | None = None,
) -> str:
    """轮询是否有新输出。可选 wait_seconds / until_contains。
    until 匹配会额外拉取 recent lines/tail（因 has-new 本身无完整文本）。"""
    sid = _session(session_id, ctx)

    async def _haystack(c: Any) -> str:
        extra = await c.ssh_channel_read_lines(
            channel_id, last_n=80, session_id=sid
        )
        return _channel_read_haystack(extra)

    return await _run_ssh_channel_read(
        lambda c: c.ssh_channel_has_new(channel_id, after_line=after_line),
        wait_seconds=wait_seconds,
        until_contains=until_contains,
        until_haystack_factory=_haystack if normalize_until_contains(until_contains) else None,
        session_id=sid,
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_ssh_channel_close(channel_id: int, ctx: Context | None = None) -> str:
    """关闭 SSH 通道。"""
    return await _run(lambda c: c.ssh_channel_close(channel_id), ctx=ctx)


@mcp.tool()
async def edgeops_ssh_channel_dump(
    channel_id: int,
    session_id: int | None = None,
    max_chars: int | None = None,
    ctx: Context | None = None,
) -> str:
    """导出 SSH 通道缓冲到 spill。"""
    sid = _session(session_id, ctx)
    return await _run(
        lambda c: c.ssh_channel_dump(channel_id, session_id=sid, max_chars=max_chars),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_ssh_channel_close_batch(
    session_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """按 session_id 批量关闭 SSH 通道。"""
    sid = _session(session_id, ctx)
    return await _run(
        lambda c: c.ssh_channel_close_batch(session_id=sid),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_list_service_credentials(
    command_hint: str | None = None,
    service: str | None = None,
    address: str | None = None,
    port: int | None = None,
    service_username: str | None = None,
    keyword: str | None = None,
    sort_by: str = "last_accessed_at",
    sort_order: str = "desc",
    limit: int = 50,
    ctx: Context | None = None,
) -> str:
    """搜索/列出服务凭证元数据（不含密码）。需系统开启 credentials_vault_enabled。

    搜索方式（可组合）：
    - command_hint：从待执行命令推断 service+address（如 `ssh 172.31.0.1`、`scp user@10.0.0.2:/path`；scp/sftp/rsync 按 ssh 凭证）
    - service + address：精确按服务类型与目标 IP/域名（跨机 SSH 填 service=ssh）
    - keyword：模糊匹配 id、address、service_username、label、notes、service
    - port / service_username：进一步过滤

    返回 credentials 列表及 resolution（use_credential / user_choice / ask_user_identity）与 suggested_credential_id。
    跨机 SSH 前先调用本工具选 credential_id，再用 edgeops_send_service_password 注入。"""
    return await _run(
        lambda c: c.list_service_credentials(
            command_hint=command_hint,
            service=service,
            address=address,
            port=port,
            service_username=service_username,
            keyword=keyword,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_send_service_password(
    target: str,
    credential_id: int | None = None,
    channel_id: int | None = None,
    host_id: int | None = None,
    slot: int | None = None,
    require_password_prompt: bool = True,
    use_host_login: bool = False,
    ctx: Context | None = None,
) -> str:
    """向 PTY 注入密码（结果不含明文）。需开启 credentials_vault_enabled。

    默认校验密码提示：sudo/su 须先 read，仅有提示时再调；无提示勿调（可能免密）。
    本机 sudo：确认提示后 use_host_login=true + host_id。跨机：credential_id。
    禁止用 edgeops_ssh_channel_send 发送明文密码。"""
    return await _run(
        lambda c: c.send_service_password(
            credential_id=credential_id,
            target=target,
            host_id=host_id,
            channel_id=channel_id,
            slot=slot,
            require_password_prompt=require_password_prompt,
            use_host_login=use_host_login,
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_read_chat_data(
    spill_id: str,
    date_subdir: str,
    mode: str = "head_tail",
    session_id: int | None = None,
    range_start: int | None = None,
    max_chars: int | None = None,
    ctx: Context | None = None,
) -> str:
    """分段读取 spill 落盘。"""
    sid = _session(session_id, ctx)
    return await _run(
        lambda c: c.read_spill(
            spill_id,
            date_subdir,
            mode=mode,
            session_id=sid,
            range_start=range_start,
            max_chars=max_chars,
        ),
        ctx=ctx,
    )


# ── P1：SSH / 分组 / 能力画像 / 提示词 / 审计 ──


@mcp.tool()
async def edgeops_ssh_execute(
    host_id: int,
    command: str,
    timeout: int | None = None,
    detach: bool = False,
    poll_log: bool = False,
    log_path: str | None = None,
    tail_lines: int | None = None,
    session_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """MCP 非交互 SSH（支持 detach + poll_log；无 Web UI）。"""
    sid = _session(session_id, ctx)

    async def _call(c):
        return await c.ssh_execute(
            host_id,
            command,
            timeout=timeout,
            detach=detach,
            poll_log=poll_log,
            log_path=log_path,
            tail_lines=tail_lines,
            session_id=sid,
        )

    return await _run(_call, ctx=ctx)


@mcp.tool()
async def edgeops_list_host_groups(ctx: Context | None = None) -> str:
    """列出主机分组（GET /api/host-groups）。"""
    return await _run(lambda c: c.list_host_groups(), ctx=ctx)


@mcp.tool()
async def edgeops_get_host_groups_tree(ctx: Context | None = None) -> str:
    """获取主机分组树（GET /api/host-groups/tree）。"""
    return await _run(lambda c: c.get_host_groups_tree(), ctx=ctx)


@mcp.tool()
async def edgeops_get_group_hosts(group_id: int, ctx: Context | None = None) -> str:
    """列出分组内主机。"""
    return await _run(lambda c: c.get_group_hosts(group_id), ctx=ctx)


@mcp.tool()
async def edgeops_probe_host_capabilities(
    host_id: int,
    refresh: bool = False,
    max_age_hours: int = 24,
    timeout: int = 40,
    ctx: Context | None = None,
) -> str:
    """SSH 探测主机能力画像并写入主机提示词哨兵块。"""
    return await _run(
        lambda c: c.probe_host_capabilities(
            host_id, refresh=refresh, max_age_hours=max_age_hours, timeout=timeout
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_get_host_capabilities(host_id: int, ctx: Context | None = None) -> str:
    """读取已缓存的主机能力画像（结构化）。"""
    return await _run(lambda c: c.get_host_capabilities(host_id), ctx=ctx)


@mcp.tool()
async def edgeops_update_host_prompt(
    host_id: int,
    content: str,
    ctx: Context | None = None,
) -> str:
    """覆盖写入主机级 AI 提示词。"""
    return await _run(lambda c: c.update_host_prompt(host_id, content), ctx=ctx)


@mcp.tool()
async def edgeops_append_host_prompt(
    host_id: int,
    text: str,
    ctx: Context | None = None,
) -> str:
    """追加主机级 AI 提示词。"""
    return await _run(lambda c: c.append_host_prompt(host_id, text), ctx=ctx)


@mcp.tool()
async def edgeops_list_maintenance_history(
    host: str | None = None,
    category: str | None = None,
    page: int = 1,
    page_size: int = 20,
    ctx: Context | None = None,
) -> str:
    """只读：维护历史列表。"""
    return await _run(
        lambda c: c.list_maintenance_history(
            host=host, category=category, page=page, page_size=page_size
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_list_operation_logs(
    page: int = 1,
    page_size: int = 20,
    host_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """只读：操作审计日志。"""
    return await _run(
        lambda c: c.list_operation_logs(page=page, page_size=page_size, host_id=host_id),
        ctx=ctx,
    )


# ── P2：MCP 专用编排式 ops（不进入 claw-ops）──


@mcp.tool()
async def edgeops_ops_orchestrate_chat(
    message: str,
    session_id: int | None = None,
    host_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """MCP 编排 ops：主编排快响 + 后台子任务（仅 MCP，非阻塞 complete）。"""
    sid = _session(session_id, ctx)

    async def _call(c):
        return await c.ops_orchestrate_chat(message, session_id=sid, host_id=host_id)

    result = await _run(_call, ctx=ctx)
    try:
        parsed = json.loads(result)
        if parsed.get("success") and parsed.get("session_id") and sid is None:
            token = resolve_access_token(ctx)
            bind_call_context(token, int(parsed["session_id"]))
    except Exception:
        pass
    return result


@mcp.tool()
async def edgeops_ops_task_list(
    session_id: int | None = None,
    status: str | None = None,
    limit: int = 30,
    ctx: Context | None = None,
) -> str:
    """列出 MCP 后台子任务。"""
    sid = _session(session_id, ctx)
    return await _run(
        lambda c: c.ops_task_list(session_id=sid, status=status, limit=limit),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_ops_task_output(task_id: int, ctx: Context | None = None) -> str:
    """读取 MCP 后台子任务进度与结果。"""
    return await _run(lambda c: c.ops_task_output(task_id), ctx=ctx)


@mcp.tool()
async def edgeops_ops_task_control(
    task_id: int,
    action: str,
    message: str = "",
    ctx: Context | None = None,
) -> str:
    """控制 MCP 后台子任务：stop | supplement。"""
    return await _run(
        lambda c: c.ops_task_control(task_id, action, message=message),
        ctx=ctx,
    )


# ── P2：远程文件 / 批量与定时任务只读 / 会话历史 ──


@mcp.tool()
async def edgeops_remote_fs_list(
    host_id: int,
    path: str = "/",
    ctx: Context | None = None,
) -> str:
    """SFTP 列出远程目录（无 Web UI）。"""
    return await _run(lambda c: c.remote_fs_list(host_id, path), ctx=ctx)


@mcp.tool()
async def edgeops_remote_fs_read(
    host_id: int,
    path: str,
    ctx: Context | None = None,
) -> str:
    """SFTP 读取远程文本文件。"""
    return await _run(lambda c: c.remote_fs_read(host_id, path), ctx=ctx)


@mcp.tool()
async def edgeops_remote_fs_write(
    host_id: int,
    path: str,
    content: str,
    ctx: Context | None = None,
) -> str:
    """SFTP 写入远程文本文件（≤2MB）。大文件/目录请用 edgeops_scp_push。"""
    return await _run(
        lambda c: c.remote_fs_write(host_id, path, content),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_scp_push(
    host_id: int,
    remote_path: str,
    local_path: str | None = None,
    content: str | None = None,
    recursive: bool = False,
    timeout: int | None = None,
    session_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """SFTP 推送到主机（与 Web AI scp_push 同一实现）。大文件/目录用 local_path（相对 web/fs）；小文本可用 content。"""
    sid = _session(session_id, ctx)
    return await _run(
        lambda c: c.scp_push(
            host_id=host_id,
            remote_path=remote_path,
            local_path=local_path,
            content=content,
            recursive=recursive,
            timeout=timeout,
            session_id=sid,
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_scp_pull(
    host_id: int,
    remote_path: str,
    local_path: str,
    recursive: bool = False,
    session_managed: bool | None = None,
    max_bytes: int | None = None,
    timeout: int | None = None,
    session_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """SFTP 从主机拉取到 web/fs（与 Web AI scp_pull 同一实现；默认不限制体积；目录需 recursive=true）。"""
    sid = _session(session_id, ctx)
    return await _run(
        lambda c: c.scp_pull(
            host_id=host_id,
            remote_path=remote_path,
            local_path=local_path,
            recursive=recursive,
            session_managed=session_managed,
            max_bytes=max_bytes,
            timeout=timeout,
            session_id=sid,
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_list_batch_jobs(
    page: int = 1,
    page_size: int = 20,
    operation_type: str | None = None,
    status: str | None = None,
    ctx: Context | None = None,
) -> str:
    """批量任务列表（可按 operation_type/status 筛选；含 scp_push/scp_pull）。"""
    return await _run(
        lambda c: c.list_batch_jobs(
            page=page,
            page_size=page_size,
            operation_type=operation_type,
            status=status,
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_get_batch_job(batch_id: int, ctx: Context | None = None) -> str:
    """批量任务详情（每机 status/result；用于轮询进度）。"""
    return await _run(lambda c: c.get_batch_job(batch_id), ctx=ctx)


@mcp.tool()
async def edgeops_create_batch_job(
    operation_type: str,
    scope_type: str,
    scope_value: list[int] | None = None,
    params: dict | None = None,
    tag_match_mode: str = "any",
    ctx: Context | None = None,
) -> str:
    """创建批量任务（run_command/scp_push/scp_pull/run_script/restart）。创建后用 get_batch_job 轮询至 completed。"""
    return await _run(
        lambda c: c.create_batch_job(
            operation_type=operation_type,
            scope_type=scope_type,
            scope_value=scope_value,
            params=params,
            tag_match_mode=tag_match_mode,
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_cancel_batch_job(batch_id: int, ctx: Context | None = None) -> str:
    """取消运行中的批量任务。"""
    return await _run(lambda c: c.cancel_batch_job(batch_id), ctx=ctx)


@mcp.tool()
async def edgeops_retry_batch_job(batch_id: int, ctx: Context | None = None) -> str:
    """重试批量任务中的失败项。"""
    return await _run(lambda c: c.retry_batch_job(batch_id), ctx=ctx)


@mcp.tool()
async def edgeops_list_scheduled_tasks(ctx: Context | None = None) -> str:
    """只读：定时任务列表。"""
    return await _run(lambda c: c.list_scheduled_tasks(), ctx=ctx)


@mcp.tool()
async def edgeops_get_scheduled_task(task_id: int, ctx: Context | None = None) -> str:
    """只读：定时任务详情。"""
    return await _run(lambda c: c.get_scheduled_task(task_id), ctx=ctx)


@mcp.tool()
async def edgeops_list_triggered_tasks(ctx: Context | None = None) -> str:
    """只读：触发式任务列表。"""
    return await _run(lambda c: c.list_triggered_tasks(), ctx=ctx)


@mcp.tool()
async def edgeops_get_triggered_task(task_id: int, ctx: Context | None = None) -> str:
    """只读：触发式任务详情。"""
    return await _run(lambda c: c.get_triggered_task(task_id), ctx=ctx)


@mcp.tool()
async def edgeops_list_session_messages(
    session_id: int,
    limit: int = 50,
    ctx: Context | None = None,
) -> str:
    """只读：integration / MCP 编排会话消息历史。"""
    sid = _session(session_id, ctx) or session_id
    return await _run(lambda c: c.list_session_messages(sid, limit=limit), ctx=ctx)


@mcp.tool()
async def edgeops_http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    body: str | None = None,
    body_encoding: str = "text",
    timeout: int | None = None,
    max_response_bytes: int | None = None,
    follow_redirects: bool = True,
    session_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """HTTP/HTTPS 出站请求（GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS）。响应体有字节上限；超大请用 http_download。"""
    sid = _session(session_id, ctx)
    return await _run(
        lambda c: c.http_request(
            url=url,
            method=method,
            headers=headers,
            query=query,
            body=body,
            body_encoding=body_encoding,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
            follow_redirects=follow_redirects,
            session_id=sid,
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_http_download(
    url: str,
    local_path: str,
    headers: dict[str, str] | None = None,
    session_managed: bool | None = None,
    max_bytes: int | None = None,
    chunked: bool = False,
    chunk_size: int | None = None,
    chunk_index: int | None = None,
    merge_chunks: bool = True,
    delete_parts: bool = True,
    timeout: int | None = None,
    follow_redirects: bool = True,
    session_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """从 HTTP/HTTPS URL 下载到用户 web/fs（无体积上限；可选 Range 分块并自动合并）。"""
    sid = _session(session_id, ctx)
    return await _run(
        lambda c: c.http_download(
            url=url,
            local_path=local_path,
            headers=headers,
            session_managed=session_managed,
            max_bytes=max_bytes,
            chunked=chunked,
            chunk_size=chunk_size,
            chunk_index=chunk_index,
            merge_chunks=merge_chunks,
            delete_parts=delete_parts,
            timeout=timeout,
            follow_redirects=follow_redirects,
            session_id=sid,
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_http_download_merge(
    local_path: str,
    part_paths: list[str] | None = None,
    delete_parts: bool = True,
    session_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """合并 HTTP 分块下载产物（local_path.part000000 …）为最终文件。"""
    sid = _session(session_id, ctx)
    return await _run(
        lambda c: c.http_download_merge(
            local_path=local_path,
            part_paths=part_paths,
            delete_parts=delete_parts,
            session_id=sid,
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_http_upload(
    url: str,
    local_path: str,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    field_name: str = "file",
    form_fields: dict[str, str] | None = None,
    content_type: str | None = None,
    multipart: bool = True,
    max_bytes: int | None = None,
    timeout: int | None = None,
    follow_redirects: bool = True,
    session_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """从用户 web/fs 工作区上传文件到 HTTP/HTTPS URL（流式上传，无体积上限，显示进度）。"""
    sid = _session(session_id, ctx)
    return await _run(
        lambda c: c.http_upload(
            url=url,
            local_path=local_path,
            method=method,
            headers=headers,
            field_name=field_name,
            form_fields=form_fields,
            content_type=content_type,
            multipart=multipart,
            max_bytes=max_bytes,
            timeout=timeout,
            follow_redirects=follow_redirects,
            session_id=sid,
        ),
        ctx=ctx,
    )
