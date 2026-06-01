---
name: edgeops-ops-chat
description: 通过 Moso 集成运维 Agent 一句话完成排障/变更/SSH — 最简单 REST 入口，无需 MCP
version: 0.9.1
author: Moso
license: MIT-0
platforms: [macos, linux, windows]
metadata:
  hermes:
    tags: [devops, edgeops, ops, automation]
    category: devops
    related_skills: [edgeops, edgeops-hosts]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: EDGEOPS_ACCESS_TOKEN
    prompt: "Moso Bearer Token"
    help: "eop_… 或 JWT；或 ~/.config/edgeops/config.json（claw-skills/edgeops.config.example.json）"
    required_for: "ops-chat API"
  - name: EDGEOPS_BASE_URL
    prompt: "Moso 根地址（可选）"
    help: "默认 https://ops.pinglan.cc；与 claw-ops config.baseUrl 相同"
    required_for: "自建环境"
---

# Moso · 集成运维对话（ops-chat）

将用户的运维需求交给 Moso **服务端 AI**，由 Moso 内部执行 SSH、最佳实践、编排等（等同网页 AI，但 `session_scope=integration`）。

## When to Use

- 用户说「帮我看下服务」「查阿里云余额」「重启 nginx」「排障 XXX」
- 任务可能涉及多台主机或 Moso 内置工具链
- **没有** MCP / claw-ops，只有 terminal 或 HTTP 能力

> **耗时任务**：若 Agent 支持 MCP，优先 [edgeops-mcp](../edgeops-mcp/SKILL.md) 的 `edgeops_ops_orchestrate_chat`（快返 + 后台子任务）。本技能 ops-chat 为**阻塞式**单次 HTTP（≤330s）。

## Quick Reference

| 字段 | 说明 |
|------|------|
| `message` | 用户运维意图原文 |
| `session_id` | 多轮对话时沿用上次响应中的值 |
| `host_id` | 新会话且已明确目标主机时传入（可先 `/api/hosts/search` 解析） |
| `skip_secondary_assistant` | 默认 `true`（外部 agent / OpenClaw 推荐，与 claw-ops 一致） |
| `attachment_uuids` | 可选；Moso `POST /api/ai/attachments` 返回的 UUID 数组 |
| `ui_locale` | 可选 BCP-47，如 `zh-CN`、`en` |

## Procedure

0. 配置 Token：`EDGEOPS_ACCESS_TOKEN` 或 `source claw-skills/scripts/load-edgeops-env.sh`
1. 若用户提到主机别名/IP，可先 `GET /api/hosts/search?query=…` 得到 `host_id`（见 `edgeops-hosts` 技能）
2. 调用 ops-chat：

```bash
BASE="${EDGEOPS_BASE_URL:-https://ops.pinglan.cc}"
curl -sS -X POST "$BASE/api/integration/ops-chat/complete" \
  -H "Authorization: Bearer $EDGEOPS_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "用户原话或整理后的运维目标",
    "host_id": null,
    "session_id": null,
    "skip_secondary_assistant": true
  }'
```

3. 解析 JSON：`reply` 为 Markdown 结果；保存 `session_id` 供追问
4. 多轮：同一 `session_id` 再次 POST，只改 `message`

PowerShell 示例：

```powershell
$base = if ($env:EDGEOPS_BASE_URL) { $env:EDGEOPS_BASE_URL } else { "https://ops.pinglan.cc" }
$body = @{ message = "查 249 上阿里云余额"; skip_secondary_assistant = $true } | ConvertTo-Json
Invoke-RestMethod -Uri "$base/api/integration/ops-chat/complete" -Method Post `
  -Headers @{ Authorization = "Bearer $env:EDGEOPS_ACCESS_TOKEN" } `
  -ContentType "application/json" -Body $body
```

## Pitfalls

- 单次请求可能 **>60s**；curl 加 `--max-time 330`
- 401 → Token 无效或过期
- 若回复提示找不到主机 → 先用 hosts 搜索或 message 里写清别名

## Verification

响应含 `"success": true` 与非空 `"reply"`；`session_id` 为整数。
