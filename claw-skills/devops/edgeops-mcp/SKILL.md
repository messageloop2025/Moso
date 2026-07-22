---
name: edgeops-mcp
description: Moso 内置 Python MCP — 49 个 edgeops_* 工具（含编排 ops、直连 SSH、服务凭证注入）；Token 在 MCP headers/env
version: 1.0.0
author: Moso
license: MIT-0
platforms: [macos, linux, windows]
metadata:
  hermes:
    tags: [devops, edgeops, mcp, tools, orchestrate]
    category: devops
    related_skills: [edgeops, edgeops-ops-chat, edgeops-hosts, edgeops-ssh-channel]
required_environment_variables:
  - name: EDGEOPS_ACCESS_TOKEN
    prompt: "Moso Bearer Token（stdio MCP 必填）"
    help: "HTTP MCP 可改在客户端 headers.Authorization；与 claw-ops accessToken 相同"
    required_for: "stdio MCP 或 fallback 鉴权"
  - name: EDGEOPS_BASE_URL
    prompt: "Moso 根地址（可选）"
    help: "默认 https://ops.pinglan.cc；stdio 本地可用 EDGEOPS_API_BASE_URL=http://127.0.0.1:8010"
    required_for: "非默认 / 本机 Moso"
---

# Moso · MCP（多用户 / 多会话 / 无 Web UI）

## When to Use

- Moso 主服务已启动（**默认** HTTP MCP：`http://127.0.0.1:8010/mcp/`）
- 需要 **比 claw-ops 更全** 的工具：直连 `ssh_execute`、分组/画像/审计、**编排式 ops**、远程文件
- **Cursor / Hermes MCP 客户端**；OpenClaw 用户仍推荐 [claw-ops](../../../claw-ops/README.md)（manifest 动态扩展，v1.1+）

## 鉴权与会话

| 传输 | Token |
|------|-------|
| HTTP MCP | `headers.Authorization: Bearer eop_…` |
| stdio MCP | `env.EDGEOPS_ACCESS_TOKEN` |
| MCP 专用 REST 回连 | 自动 `X-EdgeOps-Client: mcp` |

可选 `X-EdgeOps-Session-Id` 或 `edgeops_context_bind` 绑定会话。

## 推荐路由（无 Web UI）

1. **探活** → `edgeops_gateway_ping`
2. **名词 → host_id** → `edgeops_search_hosts` / `edgeops_search_hosts_by_prompt`
3. **短命令** → `edgeops_ssh_execute`（长任务 `detach=true`，再 `poll_log=true`）
4. **sudo/vi/多步 TTY** → `edgeops_ssh_channel_*`；出现 password 提示 → `edgeops_list_service_credentials` + `edgeops_send_service_password`
5. **复杂 / 耗时编排** → `edgeops_ops_orchestrate_chat` + `edgeops_ops_task_*`（**仅 MCP**）
6. **简单一句话**（可接受阻塞 ≤330s）→ `edgeops_ops_chat`
7. **远程文件** → `edgeops_remote_fs_*`（无 web/fs 依赖）
8. **大输出 spill** → `edgeops_read_chat_data`

## 编排式 ops（MCP 专用）

```text
edgeops_ops_orchestrate_chat(message, session_id?, host_id?)
  → mode: reply_direct | background_task
  → task_ids / task_completions

edgeops_ops_task_list(session_id?, status?)
edgeops_ops_task_output(task_id)
edgeops_ops_task_control(task_id, action=stop|supplement, message?)
```

- 主编排通常 **≤120s** 返回；SSH/部署在后台 `mcp_agent_tasks` 表执行
- **不会**主动推送到客户端；需轮询 `task_output` 或下轮 `orchestrate_chat` 看 `task_completions`
- 会话 scope = `mcp_orchestrate`，不出现在网页 AI 列表

## 工具分组（49）

**与 claw-ops 同名（22）**：ping、hosts、prompt、tags、alive、stats、best-practices、ops_chat、ssh_channel×10、read_chat_data、context_bind

**MCP 服务凭证（2）**：list_service_credentials、send_service_password（需 `credentials_vault_enabled`）

**P1**：ssh_execute、host_groups×3、probe/get_capabilities、update/append_host_prompt、maintenance_history、operation_logs

**P2 编排**：ops_orchestrate_chat、ops_task_list/output/control

**P2 其它**：remote_fs×3、batch×2、scheduled×2、triggered×2、list_session_messages

完整 REST 映射见 [services/edgeops_mcp/README.md](../../../services/edgeops_mcp/README.md) 与 [docs/API文档.md](../../../docs/API文档.md) §18。

## Procedure

1. `edgeops_gateway_ping`
2. 用户指某台机 → `edgeops_search_hosts` 或 `search_hosts_by_prompt`
3. 需画像 → `edgeops_probe_host_capabilities(host_id)`
4. 耗时任务 → `edgeops_ops_orchestrate_chat`；保存 `session_id` 与 `task_id`
5. 轮询 → `edgeops_ops_task_output(task_id)` 直至 status 为 completed/failed/cancelled
6. 交互 SSH → `edgeops_ssh_channel_*`（同一 `session_id` 绑定 owner）

## Pitfalls

- **不要**多 Agent 共用同一 `session_id` 并发写
- `edgeops_ops_chat` 可能 **>60s**；长任务请用 **orchestrate**
- OpenClaw + MCP 并行时用 **不同 session_id**
- MCP 专用写接口（ssh_execute、编排等）需 `X-EdgeOps-Client: mcp`（内置 client 已自动携带）
- 无 Web UI：勿期待按钮/终端 SSE；确认用纯文本 `[A]/[B]` 选项
- **`ops-chat` 轮询等待**：`get_terminal_buffer(next_poll…)` / ssh_channel `wait_seconds`；或 **`until_contains`**（命中子串或超时返回）。可用 `runtime-control: wake` 跳过（MCP 无 wake 工具）。MCP 直调读通道：无 until 时 wait_seconds 静默 sleep；有 until 时工具内轮询

## Verification

- `edgeops_gateway_ping` 返回 version
- `edgeops_ops_orchestrate_chat` 返回 `"success": true` 与 `session_id`
- `edgeops_ssh_execute` 返回 `success` / `exit_code` / 可选 `session_id`（poll_log 状态）
