# 毛竹 MCP（内置，同进程 /mcp）

毛竹 内置 MCP，服务注册名 **`edgeops`**。**默认**随主 Web 同端口挂载于 **`/mcp`**（无需另开端口或子进程）。

- **技术总览**：[docs/外部集成与ClawOps.md](../../docs/外部集成与ClawOps.md)
- **Hermes 技能**：[claw-skills/devops/edgeops-mcp](../../claw-skills/devops/edgeops-mcp/SKILL.md)
- **OpenClaw 插件**：[claw-ops](../../claw-ops/README.md)（**22 核心 + manifest 动态扩展**；MCP 为超集，含编排）

## 鉴权（与 claw-ops 相同，不进工具参数）

| 传输 | 配置方式 |
|------|----------|
| **HTTP MCP** | 客户端 `headers.Authorization: Bearer eop_…` 或 `X-EdgeOps-Access-Token`；URL 默认 `http://127.0.0.1:8010/mcp` |
| **stdio MCP** | 客户端 `env.EDGEOPS_ACCESS_TOKEN=eop_…` |
| **MCP 专用 REST** | HTTP 回连时自动带 `X-EdgeOps-Client: mcp`（`/api/integration/mcp/*`） |
| **fallback** | `EDGEOPS_MCP_ACCESS_TOKEN` / `config.MCP_ACCESS_TOKEN` |

多会话：`headers.X-EdgeOps-Session-Id` 或工具参数 `session_id`，或 `edgeops_context_bind(integration_session_id=…)`。

## Cursor HTTP 示例

```json
{
  "mcpServers": {
    "edgeops": {
      "url": "http://127.0.0.1:8010/mcp/",
      "headers": {
        "Authorization": "Bearer eop_你的token",
        "X-EdgeOps-Session-Id": "1001"
      }
    }
  }
}
```

## 运行

```bash
# 默认：随 python app.py / uvicorn 启动，HTTP 即 http://127.0.0.1:8010/mcp
# 关闭：EDGEOPS_MCP_ENABLED=false
python -m services.edgeops_mcp --http   # 可选：独立进程（默认 :8011/mcp，调试用）
python -m services.edgeops_mcp          # stdio（Cursor 本地子进程模式）
```

环境变量见 [docs/技术栈说明.md](../../docs/技术栈说明.md) §5。

**HTTPS 反代**：推荐客户端 URL 带尾斜杠 `https://host/mcp/`；nginx 示例见 [docker/nginx-edgeops.example.conf](../../docker/nginx-edgeops.example.conf)。

## 工具一览（54 个）

### 基础（22 个）

`edgeops_gateway_ping`、`edgeops_list_hosts`、`edgeops_search_hosts`、`edgeops_search_hosts_by_prompt`、`edgeops_get_host`、`edgeops_get_host_prompt`、`edgeops_list_host_tags`、`edgeops_host_alive`、`edgeops_host_stats`、`edgeops_search_best_practices`、`edgeops_ops_chat`、`edgeops_ssh_channel_*`（10 个）、`edgeops_read_chat_data`、`edgeops_context_bind`

> 其中前 21 个与 claw-ops 核心工具同名同义；MCP 的 `edgeops_context_bind` 在 claw-ops 侧对应统一调用入口 `edgeops_invoke`（语义不同，名称不互通）。

### 服务凭证（2 个，需 `credentials_vault_enabled`）

| 工具 | 说明 |
|------|------|
| `edgeops_list_service_credentials` | 搜索服务凭证元数据（command_hint / service+address / keyword；返回 resolution） |
| `edgeops_send_service_password` | 向 ssh_channel / terminal 注入密码（不含明文回显） |

### P1 扩展（MCP 独有或增强）

| 工具 | 说明 |
|------|------|
| `edgeops_ssh_execute` | 非交互 SSH，支持 `detach` / `poll_log` |
| `edgeops_list_host_groups` | 主机分组列表 |
| `edgeops_get_host_groups_tree` | 分组树 |
| `edgeops_get_group_hosts` | 分组内主机 |
| `edgeops_probe_host_capabilities` | SSH 探测能力画像 |
| `edgeops_get_host_capabilities` | 读取画像 |
| `edgeops_update_host_prompt` / `edgeops_append_host_prompt` | 主机提示词写 |
| `edgeops_list_maintenance_history` | 维护历史（只读） |
| `edgeops_list_operation_logs` | 操作审计（只读） |

### P2 编排（**仅 MCP**，不进 claw-ops）

| 工具 | 说明 |
|------|------|
| `edgeops_ops_orchestrate_chat` | 主编排快响 + 后台子任务 |
| `edgeops_ops_task_list` | 子任务列表 |
| `edgeops_ops_task_output` | 进度 / 结果 |
| `edgeops_ops_task_control` | `stop` / `supplement` |

耗时运维**优先** `edgeops_ops_orchestrate_chat`；阻塞式 `edgeops_ops_chat`（complete，≤330s）作回退。

### P2 其它

`edgeops_remote_fs_list/read/write`、`edgeops_list_batch_jobs` / `edgeops_get_batch_job`、`edgeops_list_scheduled_tasks` / `edgeops_get_scheduled_task`、`edgeops_list_triggered_tasks` / `edgeops_get_triggered_task`、`edgeops_list_session_messages`

### HTTP 出站（3 个）

| 工具 | 说明 |
|------|------|
| `edgeops_http_request` | GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS；响应体有字节上限 |
| `edgeops_http_download` | 从 URL 流式下载（无上限；可选 Range 分块+自动合并） |
| `edgeops_http_download_merge` | 合并 `.part000000` 等分块为最终文件 |
| `edgeops_http_upload` | 从 web/fs 流式上传（无上限，进度条，可取消） |

默认 SSRF 防护（禁止内网/本机）；明文 HTTP 需 `EDGEOPS_HTTP_TOOL_ALLOW_INSECURE=true`。

## 无 Web UI 约束

- 勿依赖 `connect_terminal`、`send_to_terminal`、`ask_user_choice`
- 非交互命令 → `edgeops_ssh_execute`；TTY → `edgeops_ssh_channel_*`
- 通道内嵌套 SSH/sudo 密码 → `edgeops_list_service_credentials` + `edgeops_send_service_password`（勿 `ssh_channel_send` 发明文）
- 大输出 → spill + `edgeops_read_chat_data`
- **终端/通道轮询等待**：`get_terminal_buffer(next_poll…)` 或 ssh_channel 读工具 `wait_seconds=1～30` 可触发等待；ops-chat 可用 `runtime-control: wake` 跳过。MCP 直调读通道时 `wait_seconds` 在工具内静默 sleep
- 编排会话 `session_scope=mcp_orchestrate`，不出现在网页 AI 列表

REST 映射见 [docs/API文档.md](../../docs/API文档.md) §16–§19（§19 为 ClawOps manifest / invoke / check-update）。
