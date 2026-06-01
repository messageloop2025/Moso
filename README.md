# 毛竹（Moso）

以 **SSH** 为操作方式的**远程 AI 运维系统**：主机树、Web 终端、Function Calling 驱动的 AI 助手、批量与定时/触发任务、个人 MCP 与 Agent Skills，以及 OpenClaw / MCP 外部集成。当前版本 **v1.3.6**（见 `config.VERSION` / `GET /api/version`）。

- **产品介绍（无需登录）**：[`/intro/`](http://localhost:8010/intro/) 静态站，中英文切换；登录页表单底部保留小字链接触达。
- **详细设计**：见下方 [文档索引](#文档索引) 与 [docs/功能清单.md](docs/功能清单.md)。

## 文档索引

| 文档 | 说明 |
|------|------|
| [docs/设计.md](docs/设计.md) | 产品设计要点 |
| [docs/功能清单.md](docs/功能清单.md) | 功能清单与优先级（验收参考） |
| [docs/软件设计文档.md](docs/软件设计文档.md) | 架构、数据模型、前后端结构 |
| [docs/数据库结构.md](docs/数据库结构.md) | 表结构、列与索引、迁移说明 |
| [docs/API文档.md](docs/API文档.md) | REST / WebSocket 接口 |
| [docs/Skills文档.md](docs/Skills文档.md) | AI 可调用 Skills（Function Calling） |
| [docs/AI-Delegation-Cookbook.md](docs/AI-Delegation-Cookbook.md) | 多机编排、`delegate_chain`、工作流模板等示例 |
| [docs/外部集成与ClawOps.md](docs/外部集成与ClawOps.md) | OpenClaw、Hermes、内置 MCP、API Token |
| [docs/SSH通道与后台任务设计.md](docs/SSH通道与后台任务设计.md) | SSH TTY 通道与任务执行模型 |
| [docs/技术栈说明.md](docs/技术栈说明.md) | 依赖、环境变量 |
| [docs/权限边界说明.md](docs/权限边界说明.md) | 角色、数据隔离、Skills 边界 |
| [docs/Docker部署.md](docs/Docker部署.md) | Docker 部署与卷映射 |
| [docs/并发与扩展.md](docs/并发与扩展.md) | SQLite WAL、多用户并发、worker 建议 |
| [docs/开发与完善建议.md](docs/开发与完善建议.md) | 测试、CI、规范等完善方向 |
| [web/README.md](web/README.md) | 前端本地化与中英文 locale 键对齐校验 |

## 功能概览

### 主机、凭证与终端

- **用户与认证**：管理员 / 普通用户、注册、JWT（可滑动续期）、数学验证码、账户锁定与邮件解锁；**修改密码**与管理员重置；**自助注销 / 管理员删除用户**时级联清理关联数据（见 `api/users.py`）。
- **凭证管理**：与主机分离；密码型 / 密钥型；RSA、ECC 一键生成。
- **主机与服务器树**：多级分组、**主机标签**、**主机分享**（提示词与知识按用户隔离）；树内搜索、拖放整理；主机详情 SSH 命令与 **WebSocket 终端**（xterm.js）。
- **维护历史 / 最佳实践**：按主机 IP 或全局知识库；AI 会话取名可自动写入维护记录。

### AI 助手（核心入口）

- **全局 AI 助手**与**主机详情 AI 运维**：多会话、流式 Markdown（Mermaid / Markmap / ECharts）、**可点击选择卡**（确认/多选/危险操作）。
- **每用户 AI 配置**：DashScope / Ollama / OpenAI 兼容接口；上下文长度、系统提示词、自动审批、**图像识别**开关。
- **主机级 / 会话级提示词**、**主机知识库**、**跨主机 `search_hosts_by_prompt`**。
- **聊天附件**（图片、文本、Office/PDF → MarkItDown）与 **AI 成果物**（单文件或 bundle，站内预览/下载）。
- **系统共享 Key 配额**（默认 2000 次，可配置）；用户配置自有 Key 后不计入共享配额。
- **编排**：`delegate_chain`、`delegate_to_edgeops_ai`、**工作流模板**（保存/复用/dry_run）；详见 [AI-Delegation-Cookbook](docs/AI-Delegation-Cookbook.md)。

### 自动化与批量

- **批量操作**：run_command、run_script、scp_push、restart；按分组 / 标签 / 选定主机。
- **触发任务 / 定时任务**：Cron、链式触发、启用开关、运行历史、可选 SMTP 摘要；进程内调度器（约 30 秒 tick）。
- **SSH TTY 通道**：长连接交互式会话，供 AI 与任务使用。

### 集成与扩展

- **内置 MCP**（默认同端口 `/mcp`，`edgeops_*` 工具，含编排类 ops）。
- **每用户 MCP**（`/mcp-servers`）：stdio / SSE / Streamable HTTP；导入导出 Cursor 风格 `mcp.json`；按聊天场景开关。
- **每用户 Agent Skills**（`/skills`）：`web/fs/<user>/skills/` 目录 + 元数据表；扫描、导入导出、渐进式披露（管理员可关）。
- **外部智能体**：OpenClaw（`claw-ops/`）、Hermes（`claw-skills/`）、REST + `eop_` API Token；集成专用聊天通道防长会话压缩。

### 协作、登录页与国际化

- **登录页留言板**（匿名，管理员审核后可选公开展示）与 **系统内用户反馈**（Markdown、管理员回复、未读角标、可选邮件通知）。
- **登录页公开开关**：留言板、公开留言区等（`settings` + `/api/public/login-widgets`）；**无「离线版本申请」**（已移除，升级见迁移 `030`）。
- **界面 i18n**：`web/locales/zh-CN` 与 `en`，16 个模块 JSON；发布前可运行 `python scripts/check_locale_parity.py` 校验键对齐。

### 系统与其它

- **文件系统**：`web/fs/<用户名>` 私有空间；远程 SFTP 浏览与编辑。
- **本机管理**（管理员）：宿主机 shell、进程、文件。
- **API Token**、**个人 SMTP**、**网页/代码搜索**配置、**仪表盘**、**操作日志**（按角色过滤）。
- **系统设置**：全局项 +「我的 AI 配置」；管理员管理用户、邮件模板、共享 Key 配额等。

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | FastAPI、aiosqlite、Paramiko（SSH/SFTP）、OpenAI 兼容 LLM 客户端 |
| 前端 | 原生 JS SPA（无打包器）、深色主题 CSS、xterm.js、ECharts / Mermaid / Markmap |
| 数据 | SQLite（WAL）、`database/migrations/` 版本化升级（当前 **schema v31**，脚本 **000–030**） |
| 默认端口 | **8010**（与常见 8000 区分） |

## 快速开始

**方式一：本地运行**

```bash
cd 毛竹
pip install -r requirements.txt
python app.py
```

Windows 可直接双击 **start.bat**。

**方式二：Docker（推荐部署）**

```bash
docker compose -f docker/docker-compose.yml up -d
```

详见 [docs/Docker部署.md](docs/Docker部署.md)。

---

浏览器访问 `http://localhost:8010`，默认管理员：**admin** / **admin123**（首次建库后请尽快修改密码）。

首次使用请在登录后配置 **「系统设置 → 我的 AI 配置」**（Base URL、API Key、模型）；无有效模型时 AI 助手与任务类能力不可用。

## 目录结构

与仓库当前布局一致（`api/` 约 30 个路由模块，不含已下线的离线申请接口）。

```
<仓库根目录>/          # Git 克隆目录名可能仍为 EdgeOps
├── app.py                      # FastAPI 入口、API 注册、/intro 挂载、SPA 回退
├── config.py                   # VERSION、数据库、JWT、AI、MCP/Skills、SQLite 并发
├── requirements.txt
├── start.bat                   # Windows 一键启动
├── edgeops.db                  # 默认 SQLite（可用 EDGEOPS_DB 改路径；Docker 常挂卷到 data/）
│
├── docker/                     # Dockerfile、docker-compose.yml
├── data/                       # Docker 运行时数据卷目录（可选）
│
├── database/
│   ├── __init__.py             # init_db、get_db、迁移入口
│   ├── models.py               # SCHEMA_SQL、建表与安全网
│   ├── schema_version.py
│   ├── migrations/             # 000_initial … 030_drop_offline_requests（共 31 档 → schema v31）
│   │   └── README.md           # 迁移编写约定
│   └── bundles/
│       └── fresh_install.sql   # 新库快照（改迁移后运行 regenerate_fresh_install_sql.py）
│
├── api/                        # FastAPI Router（REST + WebSocket）
│   ├── auth.py, users.py, api_tokens.py
│   ├── credentials.py, hosts.py, host_groups.py, host_tags.py
│   ├── maintenance_history.py, best_practices.py, skills.py
│   ├── terminal.py, ssh_channel.py, remote_fs.py, filesystem.py
│   ├── ai_agent.py, chat_attachments.py, ai_artifacts.py
│   ├── batch.py, triggered_tasks.py, scheduled_tasks.py
│   ├── dashboard.py, settings.py, local_host.py
│   ├── user_mail.py, search_config.py
│   ├── user_mcp_servers.py, user_skills.py
│   ├── login_board.py, feedback.py          # 登录留言板 + 用户反馈
│   └── integration_ops.py, integration_mcp.py, integration_claw_ops.py
│
├── services/                   # 业务与 AI 执行层
│   ├── ai_skills.py            # Function Calling 工具定义与 execute_tool
│   ├── llm_adapter.py, chat_utils.py, sub_ai.py, workflow_templates.py
│   ├── ssh_client.py, ssh_shell.py, ssh_channel_*.py, ssh_background.py
│   ├── scheduler.py, task_runner.py, batch_executor.py
│   ├── edgeops_mcp/            # 内置 MCP Server（挂载 /mcp）
│   ├── user_mcp_*.py           # 个人 MCP 注册、连接、导入导出
│   ├── user_skills_*.py        # 个人 Skills 扫描、运行时、导入导出
│   ├── feedback.py, feedback_notify.py, user_mail.py, email_sender.py
│   ├── markitdown_convert.py   # Office/PDF 附件转 Markdown
│   └── host_*.py, search_providers/, …
│
├── scripts/
│   ├── bootstrap_fresh_db.py   # 空库一键建库
│   ├── migrate_db.py           # 仅跑迁移
│   ├── regenerate_fresh_install_sql.py
│   └── check_locale_parity.py  # 中英文 locale 键对齐
│
├── tests/                      # 单元测试（user_mcp、user_skills、mcp_result_fetch 等）
├── docs/                       # 设计、API、数据库、Skills、Docker、并发…
├── claw-ops/                   # OpenClaw 插件
├── claw-skills/                # Hermes / 外部 Agent 技能说明
│
└── web/                        # 静态前端（挂载为 /static，见 web/README.md）
    ├── index.html              # SPA 壳
    ├── favicon.png
    ├── css/                    # style.css、xterm.min.css
    ├── js/
    │   ├── app.js, api.js, router.js, utils.js, i18n.js
    │   ├── intro-page.js       # /intro/ 产品介绍页组字
    │   └── xterm*.js, mermaid/echarts/markmap/d3（本地化，无 CDN）
    ├── locales/
    │   ├── zh-CN/              # 16 个模块 *.json
    │   └── en/
    ├── intro/                  # 产品介绍静态站 → 浏览器 /intro/
    ├── aihelp/                 # AI 可读帮助 Markdown
    ├── res/                    # 图片等静态资源
    └── fs/                     # 运行时用户空间 web/fs/<用户名>/（附件、Skills、chats…）
```

打包与镜像相关：`build/`、`pack.bat`、`export-image.bat`、`build-and-export.bat`（见 Docker 文档）。

## 数据库：全新安装与升级

| 场景 | 命令 |
|------|------|
| **全新空库**（含默认 admin，schema v31） | `python scripts/bootstrap_fresh_db.py`（已有库加 `--force`） |
| **已有库升级** | `python scripts/migrate_db.py` 或**直接启动应用**（启动时自动迁移） |
| **应用启动策略** | 无库 / 空库 → 完整初始化；已识别 毛竹 库 → 仅跑未执行的迁移；指向含陌生表的 SQLite → **拒绝启动** |
| **重新生成 fresh_install.sql** | 修改 `database/migrations/` 后：`python scripts/regenerate_fresh_install_sql.py` |

离线导入：`sqlite3 edgeops.db < database/bundles/fresh_install.sql`（按运维规范处理目标文件是否为空）。

## 环境变量（常用）

完整列表见 [docs/技术栈说明.md](docs/技术栈说明.md)。

| 变量 | 说明 |
|------|------|
| `EDGEOPS_DB` | SQLite 路径，默认 `edgeops.db` |
| `EDGEOPS_SECRET` | JWT 密钥（生产必改） |
| `EDGEOPS_HOST` / `EDGEOPS_PORT` | 监听，默认 `0.0.0.0:8010` |
| `EDGEOPS_VERSION` | 显示版本号 |
| `EDGEOPS_WORKERS` | 默认 `0` 单进程（推荐）；多 worker 可能重复跑定时任务 |
| `EDGEOPS_SQLITE_BUSY_TIMEOUT_MS` / `EDGEOPS_SQLITE_WAL` | 并发与 WAL |
| `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` | 全局 AI 默认 |
| `EDGEOPS_SYSTEM_AI_USAGE_LIMIT` | 共享 Key 每用户次数上限，默认 **2000** |
| `EDGEOPS_AGENT_MAX_STEPS` / `EDGEOPS_ASSISTANT_MAX_ROUNDS` | Agent 步数上限 |
| `EDGEOPS_AI_CONTEXT_SIZE` | 聊天上下文字符上限，0=不限制 |
| `EDGEOPS_MCP_ENABLED` / `EDGEOPS_MCP_HTTP_PATH` | 内置 MCP，默认开启、路径 `/mcp` |
| `EDGEOPS_USER_SKILLS_*` | 个人 Skills 渐进披露与体积上限 |
| `EDGEOPS_TRUST_PROXY_HEADERS` / `EDGEOPS_TRUSTED_PROXY_HOSTS` | 反代 HTTPS（Docker/nginx 常用） |

## 开发与发布前检查

```bash
# 中英文 locale 键一致
python scripts/check_locale_parity.py

# 仅升级数据库（不启动 Web）
python scripts/migrate_db.py
```

## 后续可扩展

- 密钥加密存储、更多 LLM 提供商适配、按角色更细的 Skills 白名单。
- 聊天 tool 调用与结果分表存储，便于审计与检索。
- 多实例部署下的定时任务去重与数据库换 PostgreSQL 等（见 [并发与扩展](docs/并发与扩展.md)）。

更多建议见 [docs/开发与完善建议.md](docs/开发与完善建议.md)。
