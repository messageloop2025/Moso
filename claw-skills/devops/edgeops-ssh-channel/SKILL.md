---
name: edgeops-ssh-channel
description: Moso 无界面交互式 SSH TTY 管道 — sudo/vi/多步向导；REST /api/ssh-channel
version: 0.9.1
author: Moso
license: MIT-0
platforms: [macos, linux, windows]
metadata:
  hermes:
    tags: [devops, edgeops, ssh, terminal, tty]
    category: devops
    related_skills: [edgeops, edgeops-hosts, edgeops-mcp]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: EDGEOPS_ACCESS_TOKEN
    prompt: "Moso Bearer Token"
    help: "eop_… 或 JWT；或 ~/.config/edgeops/config.json"
    required_for: "ssh-channel API"
  - name: EDGEOPS_BASE_URL
    prompt: "Moso 根地址（可选）"
    help: "默认 https://ops.pinglan.cc"
    required_for: "自建环境"
---

# Moso · SSH 交互通道

通过 REST 维护持久 SSH TTY，适合 **sudo 密码、vi、交互式安装向导**；不等同于单次 `ssh_execute`。

## When to Use

- ops-chat 无法满足的**逐步交互**（密码提示、菜单选择）
- 需要轮询终端输出、发送 Ctrl+C
- OpenClaw/claw-ops 的 `edgeops_ssh_channel_*` 不可用，只有 HTTP

## 生命周期

```
create → send → read_lines/has_new（循环）→ close
         ↘ spill 大输出 → read spill API
```

集成会话传 `session_id`（来自 ops-chat）可绑定 owner，默认 **idle 600s** 关断。

## Quick Reference

| 动作 | 方法 | 路径 |
|------|------|------|
| 创建 | POST | `/api/ssh-channel` — `host_id`, `session_id?`, `idle_close_sec?`（集成默认 600s） |
| 列表 | GET | `/api/ssh-channel?all_open=true&owner_type=&owner_id=` |
| 详情 | GET | `/api/ssh-channel/{id}?check_alive=true` |
| 发送 | POST | `/api/ssh-channel/{id}/send` — `content`（含 Ctrl+C 等控制序列） |
| 按行读 | GET | `…/lines?since_line=&last_n=&from_line=&to_line=&session_id=` |
| 按字符读 | GET | `…/read?max_chars=8192&session_id=` |
| 有新输出? | GET | `…/has-new?after_line=100` |
| 导出 spill | POST | `…/dump` — `session_id?`, `max_chars?` |
| 关闭 | DELETE | `/api/ssh-channel/{id}` |
| 批量关 | POST | `/api/ssh-channel/close-batch` — `session_id?`, `owner_type?`, `owner_id?` |
| 读 spill | GET | `/api/integration/spill/read?spill_id=&date_subdir=2026/05/22&mode=head_tail&max_chars=` |

## Procedure

0. 配置 Token：`EDGEOPS_ACCESS_TOKEN` 或 `source …/load-edgeops-env.sh`
1. 解析 `host_id`（见 `edgeops-hosts`）
2. 创建通道：

```bash
BASE="${EDGEOPS_BASE_URL:-https://ops.pinglan.cc}"
curl -sS -X POST "$BASE/api/ssh-channel" \
  -H "Authorization: Bearer $EDGEOPS_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"host_id": 123, "session_id": 456}'
```

3. 记 `channel_id`；发送命令：`POST …/send` `{"content":"sudo apt update\n"}`
4. 轮询：`GET …/lines?since_line=LAST` 或 `has-new?after_line=LAST`
5. 响应含 `spill_id` + `storage_subdir` 时 → `GET /api/integration/spill/read`
6. 结束：`DELETE …/ssh-channel/{id}` 或 `close-batch`

## Pitfalls

- 单行 send 末尾加 `\n` 执行命令
- 通道空闲超时自动关闭；长任务间歇 read 续命
- 大日志勿整段进上下文；用 spill + 分段 read

## Verification

create 返回 `channel_id`；read_lines 返回 `lines` 数组与 `last_line`
