# 外部智能体集成（OpenClaw / Hermes / MCP）

毛竹 支持在**不打开网页 AI 聊天**的情况下，让外部智能体帮你做运维。适合 OpenClaw、Hermes Agent、Cursor 等工具。

---

## 你需要准备什么

1. **毛竹 账号**（与网页登录相同）
2. **个人 API Token**（推荐 `eop_…` 开头，长期有效）
3. （可选）自建 毛竹 的访问地址；SaaS 默认 `https://ops.pinglan.cc`

---

## 第一步：创建 API Token

1. 登录 毛竹
2. 打开 **系统设置 → 个人 API Token**
3. 点击创建，输入名称（如「OpenClaw 家用」）
4. **立即复制**页面显示的 `eop_…` 完整字符串并妥善保存  
   - 关闭后无法再次查看  
   - 不要发给他人，不要贴进聊天或代码仓库

Token 与登录 JWT 一样，通过 HTTP 头使用：

```http
Authorization: Bearer eop_你的Token
```

---

## 第二步：选择集成方式

### 方式 A · OpenClaw（claw-ops 插件）

适合已安装 [OpenClaw](https://docs.openclaw.ai/) 的用户。

1. 安装插件（离线 `.tgz` 或 npm `@edgeops/claw-ops`）
2. 在 `~/.openclaw/openclaw.json` 配置：
   - `plugins.entries.claw-ops.config.accessToken` = 你的 `eop_…`
   - `plugins.entries.claw-ops.config.baseUrl` = 毛竹 地址（可省略用默认）
3. 重启 Gateway 后，模型可使用 **22 核心 + manifest 动态扩展 + invoke**（**v1.1+** 启动/ping 时按 `extended_tools` 自动 registerTool；baseline **43** 工具，见 [claw-ops/README.md](../../claw-ops/README.md)）

详细步骤见仓库内 `claw-ops/OPENCLAW_INSTALL.md` 或离线包内同名文档。

### 方式 B · Hermes / 脚本（claw-skills）

适合没有 OpenClaw、但有 terminal 或 HTTP 能力的 Agent。

1. 复制仓库 `claw-skills/devops/` 到 Hermes 技能目录
2. 配置 Token（任选）：
   - Hermes 环境变量 `EDGEOPS_ACCESS_TOKEN`
   - 或配置文件 `~/.config/edgeops/config.json`（复制 `edgeops.config.example.json`）
3. 按任务选用技能：
   - **一句话运维** → `edgeops-ops-chat`
   - **查主机** → `edgeops-hosts`
   - **交互 SSH** → `edgeops-ssh-channel`

Windows 加载配置：

```powershell
. D:\path\to\毛竹\claw-skills\scripts\load-edgeops-env.ps1
```

### 方式 C · Cursor MCP（edgeops）— 功能最全

适合 Cursor 等支持 MCP 的 IDE。**58 个工具**，含直连 SSH、编排式 ops、远程文件等。

1. 确保 毛竹 已启动（**默认** MCP：`http://127.0.0.1:8010/mcp/`）
2. 在 Cursor MCP 配置中添加：

```json
{
  "mcpServers": {
    "edgeops": {
      "url": "http://127.0.0.1:8010/mcp/",
      "headers": {
        "Authorization": "Bearer eop_你的Token"
      }
    }
  }
}
```

3. 推荐工具路由：
   - 短命令 → `edgeops_ssh_execute`
   - 耗时编排 → `edgeops_ops_orchestrate_chat` + `edgeops_ops_task_*`
   - 交互 TTY → `edgeops_ssh_channel_*`
   - 简单一句话（可等）→ `edgeops_ops_chat`

说明见 [claw-skills/devops/edgeops-mcp/SKILL.md](../../claw-skills/devops/edgeops-mcp/SKILL.md)。

---

## 三种方式怎么选

| 你的环境 | 推荐 | 工具规模 |
|----------|------|----------|
| OpenClaw Gateway | **claw-ops**（v1.1+ manifest 动态注册） | baseline **43**（可随 毛竹 扩展） |
| Hermes / curl 脚本 | **claw-skills** ops-chat | 1 条 REST |
| Cursor / MCP 客户端 | **毛竹 MCP** | **58 工具 + 编排** |

---

## 反向集成：给 毛竹 的 AI 接入你自己的 MCP 服务器

前面三种方式是让**外部智能体调用 毛竹**。反过来，你也可以把**第三方 MCP 服务器**（如 filesystem、Notion、GitHub 等）接进 **毛竹 自己的 AI 助手**，让网页对话 / 主机 AI / 集成通道直接使用这些工具。

### 在哪配置

- **网页**：左侧导航 **「MCP 配置」**（`/mcp-servers`）
  - 「添加 MCP 服务器」：填 stdio 命令或远程 URL
  - 「从 mcp.json 导入」 / **「导出下载」**：Cursor / Claude Desktop 兼容 JSON（含完整 env/headers，请妥善保管）
  - 每个服务器可单独勾选三个**聊天场景**，控制工具在哪类对话里加载：
    - **网页全局 AI 聊天**（含 **AI 助手**、**本机管理 AI**——二者均无 `host_id`）
    - **主机维度 AI 聊天**（主机详情 · AI 运维）
    - **OpenClaw / MCP 集成通道**
  - 「测试连接」验证可用；「刷新工具」在改了远端 schema 后强制重新拉取
- **直接对 AI 说**：网页对话里可让助手代你管理（无需打开配置页），它会调用下列工具：
  - `list_user_mcp_servers` 查看已配置
  - `configure_user_mcp_server` 新增 / 修改（按标识名 upsert）
  - `import_user_mcp_config` 批量导入 mcp.json
  - **`export_user_mcp_config`** 导出 JSON
  - `test_user_mcp_server` / `refresh_user_mcp_tools` / `delete_user_mcp_server`

  例如直接说：「帮我加一个 filesystem MCP：`npx -y @modelcontextprotocol/server-filesystem /data`，只在主机对话里用」。

### stdio 传输示例（本地命令）

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data/projects"]
    }
  }
}
```

### 远程传输示例（SSE / Streamable HTTP）

```json
{
  "mcpServers": {
    "my-remote": {
      "url": "https://mcp.example.com/sse",
      "headers": { "Authorization": "Bearer xxxxx" }
    }
  }
}
```

> **注意**：
> - 配置按**每个用户**隔离，互不可见。
> - stdio 类 MCP 的命令在 **毛竹 服务端进程所在机器**上启动（不是你的浏览器本地）。
> - 仅勾选了对应场景且「参与 AI 聊天」开启的服务器才会把工具并入对话。
> - 工具在 LLM 中以 `user_mcp_{id}__{原工具名}` 形式出现，避免与内置工具冲突。
> - **触发任务 / 定时任务** 后台 AI **不会**加载个人 MCP 工具。

### 在哪些聊天里生效（MCP 与 Skills 相同规则）

| 入口 | 须勾选的场景开关 |
|------|------------------|
| AI 助手、本机管理 AI | 网页全局 AI 聊天 |
| 主机详情 · AI 运维 | 主机维度 AI 聊天 |
| OpenClaw / 集成 API | OpenClaw / MCP 集成通道 |
| 触发 / 定时任务 | **均不生效** |

---

## 反向集成：给 毛竹 的 AI 配置个人 Agent Skills

与 MCP（注入**工具**）不同，**Agent Skills** 把 `SKILL.md` **指令正文**注入 AI 的 system prompt，用于扩展助手行为（Cursor Agent Skills 格式）。

### 权限与入口

- **默认关闭**：管理员须在 **用户管理** 为对应用户开启 **Skills**（`skills_enabled`），**含管理员本人**。
- **网页**：开启后侧栏 **「Skills」**（`/skills`）— 新建 / 编辑 / 扫描磁盘 / 场景开关。
- **磁盘路径**：`web/fs/<你的用户名>/skills/<name>/SKILL.md`

### 场景开关

与 MCP 相同三档（`chat_enabled` + `chat_scope_web/host/integration`）；新建 Skill 时**集成通道默认不勾选**。

### AI 工具

`list_user_skills`、`get_user_skill`、`read_user_skill_file`、`save_user_skill`、`delete_user_skill`、`scan_user_skills`（须已开启 Skills 功能）。

**渐进式披露（默认）**：聊天 system 仅注入各 Skill 的 **name + description** 目录；任务匹配时 AI 先 `get_user_skill` 加载 `SKILL.md`，详细参考用 `read_user_skill_file` 读 `reference.md` 等。若 frontmatter 设 `always-apply: true` 或 `disable-model-invocation: false`，则正文直接内联。

**斜杠 Command / Hook**：网页聊天以 `/skill-name [args]` 开头可强制加载 Skill 全文（`{{arg}}` 占位；`commands/*.md` / 组织 Skill）。填参浮层的参数建议来自 frontmatter `slash-args` 或正文「斜杠参数 / Commands」列表（详见《Skills文档》约定）。AI 可用 `save_user_skill` / `write_user_skill_file` 创建带 Hook/Command 的 Skill（`hooks.json`、matcher、`pre_tool_use_decision`、`allowed_tools`、`slash-args`）。`allowed_tools` 仅斜杠唤起当轮强制。集成通道无确认弹窗时，Hook/`strict` 要求确认的操作会被拒绝。

**对话中创建**：在 AI 助手或主机对话里直接说「帮我创建一个 Skill…」，助手会按 Cursor 格式调用 `save_user_skill`（默认 `disable-model-invocation: true`）。

**导入 / 导出**：网页「Skills」页可导出/导入 JSON 包（含 SKILL.md 与附属文件）；AI 工具 `export_user_skills_config` / `import_user_skills_config`。

---

## 常见问题

**Q：Token 和登录密码一样吗？**  
A：不一样。API Token 专用于 HTTP/MCP 调用，可在系统设置里单独创建和删除。

**Q：集成还会占用网页里的 AI 会话吗？**  
A：不会。集成走 `integration` 或 MCP 专用 scope（`mcp_orchestrate`），不出现在网页 AI 助手列表。

**Q：ops-chat 很慢正常吗？**  
A：正常。复杂任务可能数分钟。若用 **MCP**，耗时任务请改用 `edgeops_ops_orchestrate_chat`（通常 ≤120s 返回，后台继续跑）。

**Q：编排任务完成后会自动通知吗？**  
A：不会主动推送。需轮询 `edgeops_ops_task_output` 或再次调用 `edgeops_ops_orchestrate_chat` 查看 `task_completions`。

**Q：集成会话里 AI 在等终端编译，能提前继续吗？**  
A：若 Agent/`ops-chat` 处于 **batch 末 sleep**（`next_poll_in_seconds` / `wait_seconds`）或工具内 **`until_contains` 轮询**，可在阻塞期间调用：

```http
POST /api/ai/sessions/{session_id}/runtime-control
Authorization: Bearer eop_…
Content-Type: application/json

{"action": "wake"}
```

`wake` 跳过当前等待并进入下一轮推理，不中断整任务；`stop` 则中断整轮 Agent。MCP 暂未封装 wake 工具，需直接调 REST；MCP **直调**读通道的 until 轮询需绑定 integration `session_id` 才响应 wake。

**Q：如何用 until_contains 等高效率等待？**  
A：脚本结束时 `echo DONE_<随机串>`，然后 `ssh_channel_read_lines(until_contains="DONE_…", wait_seconds=30)` 或 `get_terminal_buffer(until_contains=…, next_poll_in_seconds=…)`；等密码可用 `until_contains="password"`。超时仍会返回，避免无限卡死。

**Q：401 怎么办？**  
A：检查 Token 是否完整、`Authorization: Bearer` 拼写、Base URL 是否指向正确实例。

---

## 技术文档（开发者）

- 仓库 [docs/外部集成与ClawOps.md](../../docs/外部集成与ClawOps.md)
- [docs/API文档.md](../../docs/API文档.md) §16–§21（§20 个人 MCP、§21 Agent Skills）
- 用户自定义 MCP：REST `GET/POST/PUT/DELETE /api/user-mcp-servers`、`GET .../export`、`POST .../import`、`POST .../{id}/test`、`POST .../{id}/refresh-tools`
- 用户 Agent Skills：`GET/POST/PUT/DELETE /api/user-skills`、`POST .../scan`；管理员 `PUT /users/{id}` 设 `skills_enabled`
- [services/edgeops_mcp/README.md](../../services/edgeops_mcp/README.md)
- [claw-skills/README.md](../../claw-skills/README.md)
