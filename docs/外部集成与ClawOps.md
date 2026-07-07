# 毛竹 外部集成与 ClawOps

本文档说明如何让 **OpenClaw**、**Hermes**、**Cursor MCP** 等外部智能体调用 毛竹，而无需打开网页 AI 聊天区。

---

## 1. 三种集成路径

| 路径 | 适用 | 文档 |
|------|------|------|
| **claw-ops**（OpenClaw Node 插件） | OpenClaw Gateway：**22 核心 + manifest 动态扩展 + invoke**（v1.1+；baseline 43） | [claw-ops/README.md](../claw-ops/README.md) |
| **claw-skills**（Hermes 技能包） | 无 OpenClaw；REST / curl 或 MCP 客户端 | [claw-skills/README.md](../claw-skills/README.md) |
| **毛竹 MCP**（内置，同进程 `/mcp`） | Cursor 等 MCP 客户端；与 claw-ops 同名工具 | [services/edgeops_mcp/README.md](../services/edgeops_mcp/README.md) |

**路由建议（与 claw-ops 一致）**：

1. 名词 → `edgeops_search_hosts` / `search_hosts_by_prompt`
2. 复杂编排 → `edgeops_ops_chat`（`POST /api/integration/ops-chat/complete`）
3. 交互 TTY（sudo/vi）→ `edgeops_ssh_channel_*`（`POST/GET /api/ssh-channel`）
4. 大输出 → `edgeops_read_chat_data`（`GET /api/integration/spill/read`）

---

## 2. 鉴权（Token）

### 2.1 获取 Token

1. 登录 毛竹 → **系统设置 → 个人 API Token**
2. 创建令牌，保存 **`eop_…`** 原文（仅创建时显示一次）
3. 也可使用登录 JWT，但长期集成推荐 **`eop_` API Token**

### 2.2 使用方式

所有 REST / MCP 请求头：

```http
Authorization: Bearer eop_你的Token
```

**勿**在工具参数、聊天消息或 curl 的 `-d` 里传 Token。

| 集成方式 | Token 配置位置 |
|----------|----------------|
| claw-ops | `openclaw.json` → `plugins.entries.claw-ops.config.accessToken` |
| claw-skills（Hermes） | 环境变量 `EDGEOPS_ACCESS_TOKEN` 或 `~/.config/edgeops/config.json` |
| MCP HTTP | MCP 客户端 `headers.Authorization` 或 `X-EdgeOps-Access-Token` |
| MCP stdio | 子进程 `env.EDGEOPS_ACCESS_TOKEN` |

claw-skills 配置文件示例：[claw-skills/edgeops.config.example.json](../claw-skills/edgeops.config.example.json)  
加载脚本：`claw-skills/scripts/load-edgeops-env.ps1` / `load-edgeops-env.sh`

### 2.3 Base URL

| 场景 | 默认值 |
|------|--------|
| SaaS | `https://ops.pinglan.cc` |
| 自建 | `http://你的域名:8010`（无尾斜杠） |

claw-ops：`config.baseUrl`  
claw-skills：`EDGEOPS_BASE_URL`  
MCP 本地：`EDGEOPS_API_BASE_URL=http://127.0.0.1:8010`

---

## 3. claw-ops（OpenClaw）

- **插件 ID**：`claw-ops`
- **npm**：`@edgeops/claw-ops`
- **独立仓库**：<https://github.com/messageloop2025/edgeops-claw-ops>
- **配置示例**：[claw-ops/openclaw.claw-ops.example.json](../claw-ops/openclaw.claw-ops.example.json)
- **安装详解**：[claw-ops/OPENCLAW_INSTALL.md](../claw-ops/OPENCLAW_INSTALL.md)
- **v1.1.0+**：Gateway 启动/`edgeops_gateway_ping` 拉 `GET /integration/claw-ops/manifest`，按 `extended_tools` **动态 registerTool**；毛竹 改 `claw_ops_registry.py` 后 **重启 Gateway** 即可加载新具名工具（执行仍走 `POST …/invoke`）

集成会话使用 `session_scope=integration`，不出现在网页 AI 助手会话列表。

---

## 4. claw-skills（Hermes 等）

### 技能名（`name`）

| 技能名 | 说明 |
|--------|------|
| `edgeops` | 总览与路由 |
| `edgeops-ops-chat` | 一条 REST 完成运维 |
| `edgeops-hosts` | 主机检索 REST |
| `edgeops-ssh-channel` | 交互式 SSH REST |
| `edgeops-mcp` | MCP 用法说明 |

安装：`cp -r claw-skills/devops/* ~/.hermes/skills/devops/`

---

## 5. 毛竹 MCP（内置）

- **服务注册名**：`edgeops`（FastMCP / `mcpServers.edgeops`）
- **HTTP 端点**：`http://127.0.0.1:8010/mcp/`（默认随主 Web 同端口；独立 `--http` 可选 `:8011/mcp`）
- **默认启用**：主进程启动即挂载 `/mcp`；设 `EDGEOPS_MCP_ENABLED=false` 可关闭
- **工具数量**：**52** 个 `edgeops_*`（含 claw-ops 同名 22 个 + MCP 扩展 30 个）
- **编排式 ops**：**仅 MCP**（`edgeops_ops_orchestrate_chat` + `ops_task_*`），不进 claw-ops / 不改 `ops-chat/complete`

环境变量见 [技术栈说明.md](技术栈说明.md) §5。工具清单见 [services/edgeops_mcp/README.md](../services/edgeops_mcp/README.md)。

### MCP 与 claw-ops 差异

| 能力 | claw-ops | 毛竹 MCP |
|------|----------|-------------|
| 工具数 | **22 核心 + manifest 动态扩展 + invoke**（baseline **46**） | **52**（超集） |
| 扩展工具来源 | 毛竹 manifest 动态注册（v1.1+） | MCP `list_tools` |
| 编排快响 + 后台子任务 | 否 | **是**（MCP 专用） |
| ssh_execute / 分组 / 画像 / 审计 / remote_fs / batch 只读 | **是**（manifest 扩展） | **是** |
| 后台新增扩展工具 | 改 registry + **重启 Gateway** | 改代码 + 重启毛竹/MCP |

---

## 6. 主要 REST 接口（集成）

详见 [API文档.md](API文档.md) §16–§18。

| 接口 | 说明 |
|------|------|
| `GET /version` | 探活（可无 Token） |
| `POST /integration/ops-chat/complete` | 集成运维对话（阻塞，claw-ops 同款） |
| `GET /integration/hosts/search-by-prompt` | 按主机提示词搜主机 |
| `GET /integration/spill/read` | 读 spill 大输出 |
| **`/integration/claw-ops/*`** | **ClawOps 专用**：manifest、invoke、提示词、更新检查 |
| **`/integration/mcp/*`** | **MCP 专用**（需 `X-EdgeOps-Client: mcp`）：ssh_execute、编排、remote_fs、**http-request/download/upload** 等 |
| `GET/POST /ssh-channel` … | 无界面 SSH TTY 管道 |

---

## 7. 与网页 AI 的差异

| 项 | 网页 AI | 集成（claw-ops / ops-chat） | MCP |
|----|---------|----------------------------|-----|
| 会话 scope | default / local | **integration** | **integration** / **mcp_orchestrate** / **mcp_runtime** |
| UI 动作 | `ask_user_choice` 按钮 | 纯文本回退 | 纯文本回退 |
| 终端轮询等待 | CoT 步骤 **唤醒/停止** | 无 UI；`POST /ai/sessions/{id}/runtime-control` **`wake`** | 同左（MCP 未封装 wake 工具） |
| 编排后台子任务 | 否 | 否 | **是**（orchestrate） |
| 依赖浏览器 | 是 | **否** | **否** |

> **wake 说明**：仅 **Web 控制台**路径（`get_terminal_buffer` / `send_to_terminal` / `ssh_execute` detach）在 tool 批次结束后可能有 batch 末 sleep；`wake` 跳过倒计时继续推理，`stop` 中断整轮。**ssh_channel_* 无此等待**。

---

## 8. 反向集成：毛竹 AI 使用你的 MCP 与 Skills

外部 Agent **调用** 毛竹 见上文；本节为 毛竹 **自身 AI 聊天**接入用户扩展能力。

| 能力 | 存储 | 作用 | 网页 | 默认 |
|------|------|------|------|------|
| **个人 MCP** | `user_mcp_servers` | 第三方 MCP **工具**并入对话 | `/mcp-servers` | 所有登录用户 |
| **Agent Skills** | `web/fs/<user>/skills/<name>/SKILL.md` + `user_skills` | **指令**注入 system prompt | `/skills` | **关**（管理员 `skills_enabled`） |

**场景开关**（两者相同）：`chat_scope_web`（AI 助手、本机管理 AI）、`chat_scope_host`（主机 AI）、`chat_scope_integration`（OpenClaw/集成 API）。须同时 `enabled` + `chat_enabled`。**触发/定时任务**不加载。

详见 [web/aihelp/external-integration.md](../web/aihelp/external-integration.md)、[API文档.md](API文档.md) §20–§21。

---

## 9. 相关文档

- [API文档.md](API文档.md) — REST 明细
- [SSH通道与后台任务设计.md](SSH通道与后台任务设计.md) — TTY 通道设计
- [web/aihelp/external-integration.md](../web/aihelp/external-integration.md) — 用户向操作帮助
