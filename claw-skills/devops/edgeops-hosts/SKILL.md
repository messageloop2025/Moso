---
name: edgeops-hosts
description: Moso 主机资产检索、探活、标签与最佳实践 — REST 只读查询
version: 0.9.1
author: Moso
license: MIT-0
platforms: [macos, linux, windows]
metadata:
  hermes:
    tags: [devops, edgeops, inventory, hosts]
    category: devops
    related_skills: [edgeops, edgeops-ops-chat]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: EDGEOPS_ACCESS_TOKEN
    prompt: "Moso Bearer Token"
    help: "eop_… 或 JWT；或 ~/.config/edgeops/config.json"
    required_for: "hosts API"
  - name: EDGEOPS_BASE_URL
    prompt: "Moso 根地址（可选）"
    help: "默认 https://ops.pinglan.cc"
    required_for: "自建环境"
---

# Moso · 主机与最佳实践

## When to Use

- 解析用户口语中的「249」「网关」「生产 Redis」→ `host_id`
- 盘点资产、探活、查标签
- 执行变更前查最佳实践条目

## Quick Reference

| 目的 | 方法 | 路径 |
|------|------|------|
| 探活 Moso | GET | `/api/version` |
| 分页列表 | GET | `/api/hosts?page=1&page_size=100` |
| 关键词搜索 | GET | `/api/hosts/search?query=249&limit=50&tag_ids=1&group_id=2` |
| 提示词内搜索 | GET | `/api/integration/hosts/search-by-prompt?query=阿里云&limit=30` |
| 主机详情 | GET | `/api/hosts/{host_id}` |
| 主机 AI 提示词 | GET | `/api/ai/hosts/{host_id}/prompt` |
| 标签列表 | GET | `/api/host-tags` |
| SSH 端口探活 | GET | `/api/hosts/{host_id}/alive` |
| 资产统计 | GET | `/api/hosts/stats` |
| 最佳实践 | GET | `/api/best-practices?keyword=nginx` |

Header：`Authorization: Bearer $EDGEOPS_ACCESS_TOKEN`

## Procedure

0. 配置 Token：`EDGEOPS_ACCESS_TOKEN` 或加载 `load-edgeops-env.sh` / `.ps1`
1. 用户名词 → **优先** `/api/hosts/search?query=…`（等同 claw-ops `edgeops_search_hosts`）
2. 服务/能力写在提示词里 → `search-by-prompt`（等同 `edgeops_search_hosts_by_prompt`）
3. 需要 tag 过滤 → 先 `GET /api/host-tags` 取 `tag_ids`，再带入 search
4. 确认资产 → `/api/hosts/{id}` 或 `/api/ai/hosts/{id}/prompt`
5. 排障前 → `/api/hosts/{id}/alive`
6. 得到 `host_id` 后 → `edgeops-ops-chat`（复杂）或 `edgeops-ssh-channel`（交互 TTY）

示例：

```bash
BASE="${EDGEOPS_BASE_URL:-https://ops.pinglan.cc}"
AUTH="Authorization: Bearer $EDGEOPS_ACCESS_TOKEN"
curl -sS -H "$AUTH" "$BASE/api/hosts/search?query=249&limit=10"
```

## Pitfalls

- `search` 的 `query` 必填；多词可空格
- `tag_ids` 重复 query 参数：`tag_ids=1&tag_ids=2`
- 分享主机权限随 Token 用户变化

## Verification

`search` 返回 `"hosts": [{ "id": N, "name": "...", "aliases": [...] }]`
