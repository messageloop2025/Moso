"""毛竹（Moso）MCP 工具注册 — 与 claw-ops 同名；Token 仅走 HTTP 头 / 环境变量。"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from services.edgeops_mcp.client import create_client
from services.edgeops_mcp.context import (
    bind_call_context,
    resolve_access_token,
    resolve_integration_session_id,
)

mcp = FastMCP(
    "edgeops",
    instructions=(
        "毛竹（Moso）MCP：edgeops_* 运维工具（含 MCP 专用编排 ops_orchestrate_*）。"
        "鉴权：MCP 客户端配置 Authorization: Bearer（HTTP）或 EDGEOPS_ACCESS_TOKEN（stdio）。"
        "HTTP 调用会自动带 X-EdgeOps-Client: mcp。"
        "多会话：session_id 参数，或 HTTP 头 X-EdgeOps-Session-Id，或 edgeops_context_bind。"
        "无 Web UI：勿依赖 connect_terminal / ask_user_choice；长任务用 ops_orchestrate_chat + ops_task_*。"
        "若 ops-chat 触发 Web 终端 batch 末等待，可用 POST /api/ai/sessions/{session_id}/runtime-control action=wake 跳过（ssh_channel 无此等待）。"
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
    ctx: Context | None = None,
) -> str:
    """按行读取 SSH 通道输出。"""
    sid = _session(session_id, ctx)
    return await _run(
        lambda c: c.ssh_channel_read_lines(
            channel_id, since_line=since_line, last_n=last_n, session_id=sid
        ),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_ssh_channel_read(
    channel_id: int,
    max_chars: int | None = None,
    session_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """按字符读取 SSH 通道最近输出。"""
    sid = _session(session_id, ctx)
    return await _run(
        lambda c: c.ssh_channel_read(channel_id, max_chars=max_chars, session_id=sid),
        ctx=ctx,
    )


@mcp.tool()
async def edgeops_ssh_channel_has_new(
    channel_id: int,
    after_line: int | None = None,
    ctx: Context | None = None,
) -> str:
    """轮询 SSH 通道是否有新输出。"""
    return await _run(
        lambda c: c.ssh_channel_has_new(channel_id, after_line=after_line),
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
    credential_id: int,
    target: str,
    channel_id: int | None = None,
    host_id: int | None = None,
    slot: int | None = None,
    require_password_prompt: bool = False,
    ctx: Context | None = None,
) -> str:
    """按 credential_id 向 PTY 注入密码（结果不含明文）。需系统开启 credentials_vault_enabled。

    target：terminal（Web 控制台，需 host_id）| ssh_channel（需 channel_id）| local_terminal。
    MCP 直连 ssh_channel 嵌套 SSH 时：先 read_lines 确认 password 提示，再 target=ssh_channel + channel_id。
    禁止用 edgeops_ssh_channel_send 发送明文密码。"""
    return await _run(
        lambda c: c.send_service_password(
            credential_id=credential_id,
            target=target,
            host_id=host_id,
            channel_id=channel_id,
            slot=slot,
            require_password_prompt=require_password_prompt,
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
    """SFTP 写入远程文本文件（≤2MB）。"""
    return await _run(
        lambda c: c.remote_fs_write(host_id, path, content),
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
    """只读：批量任务列表。"""
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
    """只读：批量任务详情。"""
    return await _run(lambda c: c.get_batch_job(batch_id), ctx=ctx)


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
    timeout: int | None = None,
    follow_redirects: bool = True,
    session_id: int | None = None,
    ctx: Context | None = None,
) -> str:
    """从 HTTP/HTTPS URL 下载文件到用户 web/fs 工作区（流式落盘，显示进度）。"""
    sid = _session(session_id, ctx)
    return await _run(
        lambda c: c.http_download(
            url=url,
            local_path=local_path,
            headers=headers,
            session_managed=session_managed,
            max_bytes=max_bytes,
            timeout=timeout,
            follow_redirects=follow_redirects,
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
    """从用户 web/fs 工作区上传文件到 HTTP/HTTPS URL（流式上传，显示进度）。"""
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
