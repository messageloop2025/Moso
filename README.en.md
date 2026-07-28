<div align="center">

# Moso (毛竹)

**SSH-native remote AI operations for small teams and solo developers**

> New images are no longer published to [Docker Hub](https://hub.docker.com/r/messageloop/moso); the open-source edition will pause feature updates for a while.

*Host tree · Web terminal · AI assistant (Function Calling) · Batch & scheduled tasks · Per-user MCP & Agent Skills · OpenClaw / Hermes integration*

<br>

[![version](https://img.shields.io/badge/version-1.8.8-blue)](config.py)
[![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](#quick-start)
[![stack](https://img.shields.io/badge/built%20with-FastAPI-009688)](#tech-stack)
[![db](https://img.shields.io/badge/database-SQLite%20WAL-003B57)](#tech-stack)
[![python](https://img.shields.io/badge/python-3.11+-3776AB)](requirements.txt)
[![port](https://img.shields.io/badge/default%20port-8010-546E7A)](#quick-start)
[![docker](https://img.shields.io/docker/v/messageloop/moso?label=Docker%20Hub&logo=docker)](https://hub.docker.com/r/messageloop/moso)

<br>

**English** · [中文](README.md) · [Docker Hub `messageloop/moso`](https://hub.docker.com/r/messageloop/moso) · [Product intro `/intro/`](http://localhost:8010/intro/) · [Feature list](docs/功能清单.md) · [Docker deploy](docs/Docker部署.md) · [API docs](docs/API文档.md)

<br>

> **AI assists people—it does not replace them.**  
> Hand repetitive ops to the tool; keep your focus on the product. Version: `config.VERSION` or `GET /api/version`.

</div>

---

## Why Moso

AI moves fast, and keeping up can feel exhausting. I built this project with **Vibe Coding**—learning as I go. It started as a draft, but I want it to become genuinely useful software. Open-sourcing it is my way of giving it a place in the AI wave instead of being washed away by the next buzzword every month.

It was originally **EdgeOps** (edge operations): manage VMs, bare metal, and cloud hosts over SSH; analyze logs; deploy and debug production services.

Meanwhile, the shape of AI changes almost every month—chat, tool calls, agents, MCP, Skills, OpenClaw, Hermes… Many builders are a team of one, or only have evenings for a side project. In a world of giants, does something like this still matter? I still ask myself that.

The frontier talks about long-running agents, but I believe **AI assists people; it does not replace them**. When I decided to open-source it, I chose the name **Moso** (毛竹)—bamboo grows across climates: ordinary, yet resilient. Rhizomes spread underground; where the shoots run, a grove forms—like servers woven into networks.

## Screenshots

### AI assistant and Web terminal

Host AI ops: xterm.js terminal on the left, AI chat on the right. Streaming Markdown, Mermaid diagrams, tool calls, and artifact downloads.

![AI assistant and Web terminal](images/微信图片_20260628004559_191_74.png)

### Server tree

Nested groups, tags, and host sharing; in-tree search and drag-and-drop to find targets quickly.

![Server tree](images/微信图片_20260628005353_197_74.png)

### Model config

Multiple AI profiles per user (DashScope, OpenAI-compatible, Ollama, etc.); switch active model; import/export JSON.

![Model config](images/微信图片_20260628005554_201_74.png)

### Filesystem

Private workspace `web/fs/<username>`: directory tree, upload/pack/extract, remote SFTP.

![Filesystem](images/微信图片_20260628005513_200_74.png)

### Maintenance history

Filter by host IP and type; AI session titles can auto-write maintenance records for audit and traceability.

![Maintenance history](images/微信图片_20260628005443_198_74.png)

### AI-generated topology (example)

Ask the assistant to draw node relationships from full-link test results; export as HTML or image bundle.

![Modbus secure communication topology](images/微信图片_20260628004305_190_74.png)

### SCADA / RTU / PLC connection dashboard (example)

The assistant synthesizes session context into an interactive HTML dashboard: network graph, host nodes, edge devices (ESP32-S3), and connection details.

![SCADA RTU PLC device connection topology](images/微信图片_20260628011218_202_74.png)

## Features

### Hosts, credentials, and terminal

- **Users & auth:** admin / regular users, registration, JWT (sliding refresh), math captcha, account lock and email unlock; **password change** and admin reset; **self-delete / admin delete** cascades related data (`api/users.py`).
- **Credentials:** separate from hosts; password or key-based; one-click RSA / ECC key generation.
- **Host tree:** nested groups, **host tags**, **host sharing** (prompts and knowledge isolated per user); search and drag-and-drop; host detail SSH commands and **WebSocket terminal** (xterm.js).
- **Maintenance history / best practices:** per host IP or global knowledge base; AI session titles can auto-write maintenance records.

### AI assistant (core)

- **Global AI assistant** and **host-detail AI ops:** multi-session, streaming Markdown (Mermaid / Markmap / ECharts), **clickable choice cards** (confirm / multi-select / dangerous actions).
- **Per-user AI config:** DashScope / Ollama / OpenAI-compatible APIs; context length, system prompt, auto-approve, **vision** toggle.
- **Host- and session-level prompts**, **host knowledge base**, **`search_hosts_by_prompt`** across hosts.
- **Chat attachments** (images, text, Office/PDF via MarkItDown) and **AI artifacts** (single file or bundle; in-app preview/download).
- **Shared system API key quota** (default 2000 calls; admins can change `system_ai_usage_limit` in **System Settings**; new users follow the current limit); users with their own key are not counted against the shared quota.
- **Orchestration:** `delegate_chain`, `delegate_to_edgeops_ai`, **workflow templates** (save/reuse/dry_run); see [AI-Delegation-Cookbook](docs/AI-Delegation-Cookbook.md).

### Automation and batch

- **Batch ops:** run_command, run_script, scp_push, restart; by group / tag / selected hosts.
- **Triggered / scheduled tasks:** cron, chained triggers, enable switch, run history, optional SMTP summary; in-process scheduler (~30s tick).
- **SSH TTY channel:** long-lived interactive sessions for AI and background tasks.

### Integration and extension

- **Built-in MCP** (same port `/mcp` by default, `edgeops_*` tools including orchestration ops).
- **Per-user MCP** (`/mcp-servers`): stdio / SSE / Streamable HTTP; import/export Cursor-style `mcp.json`; per chat-scene toggles.
- **Per-user Agent Skills** (`/skills`): `web/fs/<user>/skills/` plus metadata table; scan, import/export, progressive disclosure (admin can disable).
- **External agents:** OpenClaw (`claw-ops/`), Hermes (`claw-skills/`), REST + `eop_` API tokens; integration chat channel avoids long-session compression.

### Collaboration, login page, and i18n

- **Login message board** (anonymous; admin moderation, optional public display) and **in-app user feedback** (Markdown, admin replies, unread badge, optional email notify).
- **Login page widgets:** message board, public messages, etc. (`settings` + `/api/public/login-widgets`); **no offline version request** (removed; see migration `030`).
- **UI i18n:** `web/locales/zh-CN` and `en`, 16 JSON modules; run `python scripts/check_locale_parity.py` before release to verify key parity.

### System and other

- **Filesystem:** `web/fs/<username>` private space; remote SFTP browse and edit.
- **Local host management** (admin): host shell, processes, files.
- **API tokens**, **personal SMTP**, **web/code search** config, **dashboard**, **audit logs** (filtered by role).
- **Settings:** global options (display language, site SMTP placeholders, search services, etc.); **Model config** (`/model-config`, multiple AI profiles per user); admin manages users, mail templates, **shared Key trial quota**, and more.

## Tech stack

| Layer | Stack |
|-------|--------|
| Backend | FastAPI, aiosqlite, Paramiko (SSH/SFTP), OpenAI-compatible LLM client |
| Frontend | Vanilla JS SPA (no bundler), dark theme CSS, xterm.js, ECharts / Mermaid / Markmap |
| Data | SQLite (WAL), versioned `database/migrations/` (currently **schema v43**, scripts **000–042**) |
| Default port | **8010** (distinct from common 8000) |

## Quick start

**Option 1: local**

```bash
cd Moso   # or your clone directory name
pip install -r requirements.txt
python app.py
```

On Windows, double-click **start.bat**; on Linux / macOS, run **start.sh**.

**Option 2: Docker Hub image (recommended for deployment)**

Official image: [**messageloop/moso**](https://hub.docker.com/r/messageloop/moso) (e.g. `messageloop/moso:latest` or a version tag).

After cloning the repo, use the root [`docker-compose.yml`](docker-compose.yml):

```bash
git clone https://github.com/messageloop2025/Moso.git
cd Moso
docker compose pull
docker compose up -d
```

Pull only (no clone):

```bash
docker pull messageloop/moso:latest
```

Volumes `./data/data`, `./data/fs`, and `./data/logs` are mounted automatically. See [docs/Docker部署.md](docs/Docker部署.md) (Chinese) for more options.

**Option 3: build Docker image locally**

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

To publish to Hub, use `publish-docker-hub.bat` at the repo root.

---

Open `http://localhost:8010`. Default admin: **admin** / **admin123** (change the password right after first login).

After login, open **Model config** (`/model-config`) in the sidebar and set Base URL, API Key, and model. You can maintain multiple profiles and pick which one is active. Without a working model, the AI assistant and task features are unavailable.

## Database: fresh install and upgrade

| Scenario | Command |
|----------|---------|
| **Fresh empty DB** (default admin, schema v43) | `python scripts/bootstrap_fresh_db.py` (use `--force` if a DB file already exists) |
| **Upgrade existing DB** | `python scripts/migrate_db.py` or **start the app** (migrations run on startup) |
| **Startup behavior** | No DB / empty → full init; recognized Moso DB → pending migrations only; SQLite with unknown tables → **refuse to start** |
| **Regenerate fresh_install.sql** | After editing `database/migrations/`: `python scripts/regenerate_fresh_install_sql.py` |

Offline import: `sqlite3 edgeops.db < database/bundles/fresh_install.sql` (follow your ops policy for empty vs existing files).

## Environment variables (common)

Full list: [docs/技术栈说明.md](docs/技术栈说明.md) (Chinese).

| Variable | Description |
|----------|-------------|
| `EDGEOPS_DB` | SQLite path, default `edgeops.db` |
| `EDGEOPS_SECRET` | JWT secret (**change in production**) |
| `EDGEOPS_HOST` / `EDGEOPS_PORT` | Listen address, default `0.0.0.0:8010` |
| `EDGEOPS_VERSION` | Display version |
| `EDGEOPS_WORKERS` | Default `0` single process (recommended); multiple workers may duplicate scheduled jobs |
| `EDGEOPS_SQLITE_BUSY_TIMEOUT_MS` / `EDGEOPS_SQLITE_WAL` | Concurrency and WAL |
| `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` | Global AI defaults |
| `EDGEOPS_SYSTEM_AI_USAGE_LIMIT` | Shared key per-user cap (**default on first install**); override at runtime via System Settings `system_ai_usage_limit`, default **2000** |
| `EDGEOPS_AGENT_MAX_STEPS` / `EDGEOPS_ASSISTANT_MAX_ROUNDS` | Agent step limits |
| `EDGEOPS_AI_CONTEXT_SIZE` | Chat context char limit; `0` = unlimited |
| `EDGEOPS_MCP_ENABLED` / `EDGEOPS_MCP_HTTP_PATH` | Built-in MCP, default on, path `/mcp` |
| `EDGEOPS_USER_SKILLS_*` | Personal Skills progressive disclosure and size limits |
| `EDGEOPS_TRUST_PROXY_HEADERS` / `EDGEOPS_TRUSTED_PROXY_HOSTS` | Reverse-proxy HTTPS (common with Docker/nginx) |

## Pre-release checks

```bash
# zh-CN / en locale key parity
python scripts/check_locale_parity.py

# Migrate DB only (no web server)
python scripts/migrate_db.py
```

## Possible extensions

- Encrypted credential storage, more LLM providers, finer-grained Skills allowlists by role.
- Separate tables for chat tool calls and results for audit and search.
- Scheduled-task deduplication across instances, PostgreSQL instead of SQLite, etc. (see [docs/并发与扩展.md](docs/并发与扩展.md)).

More ideas: [docs/开发与完善建议.md](docs/开发与完善建议.md).
