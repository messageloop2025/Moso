---
name: edgeops
description: Moso 运维平台集成总览 — 主机资产、集成运维 Agent、无界面 SSH 通道；与 claw-ops 共用 Bearer 鉴权
version: 0.9.1
author: Moso
license: MIT-0
platforms: [macos, linux, windows]
metadata:
  hermes:
    tags: [devops, edgeops, ssh, infrastructure, sre]
    category: devops
    related_skills: [edgeops-ops-chat, edgeops-hosts, edgeops-ssh-channel, edgeops-mcp]
required_environment_variables:
  - name: EDGEOPS_ACCESS_TOKEN
    prompt: "Moso Bearer Token（JWT 或 eop_ API Token）"
    help: "在 Moso 系统设置 → 个人 API Token 创建 eop_…；与 OpenClaw claw-ops 的 accessToken 相同。也可用 ~/.config/edgeops/config.json（见 claw-skills/edgeops.config.example.json）"
    required_for: "所有 Moso HTTP / MCP 调用"
  - name: EDGEOPS_BASE_URL
    prompt: "Moso 根地址（可选）"
    help: "自建实例填 https://your-edgeops.example.com；省略则默认 https://ops.pinglan.cc。与 claw-ops config.baseUrl 相同"
    required_for: "非默认 SaaS 环境"
---

# Moso 集成总览

Moso 是 SSH 主机运维与 AI 编排平台。外部智能体通过 **Bearer Token** 调用 REST，无需打开 Moso 网页。

## When to Use

- 用户要求查服务器、排障、改配置、看余额、部署、SSH 交互
- 用户提到主机别名、IP、标签、或 Moso 里登记的服务名
- 需要无浏览器、无 Web 终端的远程运维

## 路由（与 claw-ops 一致，按优先级）

1. **已配置 Moso Python MCP** → 技能 `edgeops-mcp`，**58** 个 `edgeops_*` 工具（MCP 超集）
2. **复杂编排 / 非交互远程命令** → `edgeops-ops-chat` 或 MCP `edgeops_ops_chat`（服务端 ssh_execute 等）
3. **名词 → host_id** → `edgeops-hosts` 或 MCP `edgeops_search_hosts` / `edgeops_search_hosts_by_prompt`
4. **sudo / vi / 多步 TTY / Ctrl+C** → `edgeops-ssh-channel` 或 MCP `edgeops_ssh_channel_*`（**简单交互优先 ssh_channel**，勿本机 shell 模拟 SSH）
5. **大输出 spill** → `edgeops_read_chat_data` 或 REST `/api/integration/spill/read`

OpenClaw 用户用 [claw-ops](../../../claw-ops/README.md) 插件即可，无需本技能包。

## 配置 Token（三选一）

| 方式 | 适用 | 说明 |
|------|------|------|
| **Hermes 环境变量** | 所有 REST 技能 | 本 frontmatter 的 `EDGEOPS_ACCESS_TOKEN` / `EDGEOPS_BASE_URL` |
| **配置文件** | terminal / curl | 复制 [edgeops.config.example.json](../../../edgeops.config.example.json) → `~/.config/edgeops/config.json`，运行 `source claw-skills/scripts/load-edgeops-env.sh` |
| **MCP HTTP 头** | edgeops-mcp | `Authorization: Bearer …` 或 `X-EdgeOps-Access-Token` |

字段与 claw-ops 一致：`accessToken`、`baseUrl`。REST 请求头：

## 鉴权 Quick Reference

```http
Authorization: Bearer ${EDGEOPS_ACCESS_TOKEN}
Accept: application/json
Content-Type: application/json
```

Base URL：`${EDGEOPS_BASE_URL:-https://ops.pinglan.cc}`

## Procedure

1. **先配置鉴权**：Hermes 填 `EDGEOPS_ACCESS_TOKEN`，或 `source …/load-edgeops-env.sh` 加载 `config.json`
2. 若无 Token，提示用户在 Moso 创建 `eop_…` API Token
3. 可选：`GET {BASE}/api/version` 探活
3. 用户口语指某台机 → 先 `edgeops_search_hosts` 或 REST `/api/hosts/search` 解析 `host_id`
4. 复杂任务 → `edgeops_ops_chat` 或 ops-chat REST；简单只读 → hosts 技能
5. 交互式 SSH → ssh-channel 技能；会话结束 `close` 或 `close-batch`

## Pitfalls

- **勿**在聊天里粘贴 Token；用环境变量
- ops-chat 可能耗时数分钟（SSH/编排）；HTTP 超时建议 ≥ 330s
- SSH 通道默认集成会话 **600s** 空闲关断；记得 `session_id` 绑定 ops-chat 返回的会话
- spill 大输出需 `edgeops_read_chat_data` / `/api/integration/spill/read` 分段读

## Verification

- `GET /api/version` 返回版本 JSON
- ops-chat 返回 `{ "success": true, "reply": "...", "session_id": N }`
- search_hosts 返回 `hosts[].id` 可用于后续 host_id
