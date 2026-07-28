# 服务凭证库

除 **SSH 登录凭证**（「凭证管理」页的 `credentials` 表，用于毛竹 SSH 连接已管理主机）外，运维中还需要保存 **sudo 密码**、**数据库密码**、**SSH 到某 IP 的密码** 等。这些密码不应写进 AI 回复，也不应反复向用户索要。

**服务凭证库**（`host_service_credentials`）按 **用户** 保存「访问某服务所需的账号密码」，供 AI 在终端/SSH 通道出现密码提示时通过 **`send_service_password`** 自动注入 stdin。**凭证不绑定**从哪台主机发起操作。

> **功能开关**：默认关闭。管理员在 **系统设置** 页勾选「**服务凭证库**」，或将 **`credentials_vault_enabled`** 设为 `true` 后生效。

---

## 1. 与 SSH 登录凭证的区别

| 类型 | 存储位置 | 含义 | AI 能否读出密码 |
|------|----------|------|------------------|
| SSH 登录 | `credentials` + `hosts.credential_id` | **用毛竹 SSH 连上**已管理主机 | 否 |
| 服务凭证 | `host_service_credentials` | **连上之后**（或任意交互）还要登录的服务：sudo / mysql / 跳板 ssh 等 | **否（仅可注入）** |
| 主机知识库 | `ai_host_knowledge` | 非密码说明；**不推荐**再存明文密码 | 是（内部用，禁止展示） |

---

## 2. 匹配键（保存与查找）

每条凭证由当前用户私有，用以下字段唯一描述「要登录什么」：

| 字段 | 说明 |
|------|------|
| `last_accessed_at` | **最近使用**时间（`send_service_password` 成功注入时更新） |
| `service` | 服务/协议：`sudo`、`ssh`、`mysql`、`postgres`、`redis`、`ftp`、`other` 等 |
| `address` | 目标 IP/域名；**sudo 留空**表示本机 sudo |
| `port` | 端口；省略或 NULL 时使用默认（ssh=22、mysql=3306、postgres=5432、ftp=21…） |
| `service_username` | 服务账户名；**同一 service+address 下不同用户**用此字段区分 |
| `label` / `notes` | 标签与备注（可查询） |
| `has_password` | **是否有可用密码**（库中已存 `password_enc`，或设置了 `linked_host_id` / `linked_credential_id`）；列表 API 计算该字段，**不返回明文** |
| `linked_host_id` | 可选：目标 SSH 主机已在平台管理时，复用其 **登录密码** |
| `linked_credential_id` | 可选：引用「凭证管理」中 `credentials.id` 的登录密码 |
| 密码 | 写入后 **不可查询**；设 `linked_*` 时可不存明文 |

**不再使用 `host_id` 参与匹配。** 旧数据中的 `host_id` 仅作兼容保留，新建凭证无需填写。

`send_service_password` 的 **`host_id` 仅表示 Web 控制台注入目标**（`target=terminal` 时必填），与查凭证无关。

---

## 3. 保存示例

### sudo（本机）

```text
add_service_credential(
  service="sudo",
  password="...",
  service_username="deploy",
  label="默认 sudo"
)
```

### SSH 到远端 IP

```text
add_service_credential(
  service="ssh",
  address="172.31.0.1",
  port=22,
  service_username="pi",
  password="..."
)
```

若 `172.31.0.1` 已是平台主机，可设 `linked_host_id=<hosts.id>` 而无需重复存密码。

### MySQL

```text
add_service_credential(
  service="mysql",
  address="127.0.0.1",
  port=3306,
  service_username="root",
  password="..."
)
```

### 跨机登录选凭证（SSH / SCP / MySQL）

从 **A 机** 连 **B 机**（或 scp 到 B）时：

1. **先查凭证**：`list_service_credentials(service="ssh", address="B的IP", command_hint="ssh …" 或 "scp …")`  
   - **scp / sftp / rsync 一律按 `service=ssh` 查凭证**（走 SSH 认证）
2. 看返回 **`resolution`**：
   - `use_credential` → 用 `suggested_credential_id` 里的 `service_username` 构造命令
   - `user_choice` → **ask_user_choice** 让用户选 credential_id
   - `ask_user_identity` → 无凭证：**ask_user_choice**（① 指定用户名 ② 使用当前控制台 whoami）
3. **禁止**默认用「当前 SSH 控制台登录用户」当作目标机用户
4. 同 `service+address+port+username` 多条重复 → 工具**自动保留最新**
5. 出现 password 提示 → `send_service_password(credential_id, …)`

---

## 4. AI 工具

| 工具 | 作用 |
|------|------|
| `list_service_credentials` | 搜索/列出凭证元数据（`command_hint`、keyword、过滤、排序；含 `last_accessed_at`） |
| `add_service_credential` | 新增 |
| `update_service_credential` | 更新元数据或密码 |
| `delete_service_credential` | 删除 |
| `send_service_password` | 终端/通道出现密码提示时注入 |

### 注入流程（AI 选凭证，工具写密码）

密码**不进 AI 上下文**。模型只处理元数据与决策；注入由 `send_service_password` 在服务端完成。

**本机 sudo / su（当前控制台主机）**

1. send 仅发 `sudo …`（或 su）——**不要假设一定要密码**（常见 NOPASSWD）  
2. **必须 read** 尾部（可用 `until_contains="password"`；超时无命中也要看是否已成功）  
3. **仅当**出现 `[sudo] password for` / `Password:` / `口令：` 等提示时才注入；无提示则结束，勿调用凭证函数  
4. 有提示时：`list_service_credentials(service=sudo, host_id=当前)`；有本机绑定 id 用它，否则 `send_service_password(use_host_login=true, host_id=当前, target=…)`  
5. 服务端默认校验密码提示，无提示会拒绝注入  
6. **禁止** sudo 后默认发密码；**禁止**用其它主机 / 未绑定凭证  
7. **无凭证时的备选**：用户本轮已提供密码，或密码在主机知识/会话记忆中 → read 确认提示后，可用 `send_to_terminal` / `ssh_channel_send` 发「密码+<Enter>」（勿与 sudo 同次 send）

**跨机 SSH / MySQL 等**

1. send 命令 → read 确认提示  
2. `list_service_credentials(service=…, address=目标IP, command_hint=…)`  
3. **有密码提示后**再 `send_service_password(credential_id=…, target=…)`，或无凭证时按上条直接 send  

**禁止**：未 read 就注入；用空回车探测 password；在回复中展示密码。

---

## 5. REST API

前缀 **`/api/service-credentials`**（需登录；未开启凭证库时 403）：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/enabled` | 是否已开启 |
| GET | `/` | 搜索/列出（query：`keyword`、`command_hint`、`service`、`sort_by`、`sort_order` 等） |
| GET | `/{id}` | 单条详情（无密码） |
| POST | `/` | 新增（body 含 `service`；`password` 或 `linked_*`） |
| PUT | `/{id}` | 更新 |
| DELETE | `/{id}` | 删除 |
| POST | `/inject` | 按 **credential_id** 注入密码（脚本/集成用；与 `send_service_password` 同源） |

### POST `/inject` 请求体

| 字段 | 必填 | 说明 |
|------|------|------|
| `credential_id` | 是 | 凭证 id（先 GET `/` 列出元数据） |
| `target` | 否 | `terminal`（默认）/ `ssh_channel` / `local_terminal` |
| `host_id` | terminal 时必填 | Web 控制台所在主机 |
| `channel_id` | ssh_channel 时必填 | SSH 通道 id |
| `slot` | 否 | 控制台槽位 |
| `scope_id` | 否 | 终端 scope |
| `require_password_prompt` | 否 | 默认 `true`：须检测到 password 提示才注入（防 sudo 免密误注入）；显式 `false` 可跳过（不推荐用于 sudo） |

响应不含明文密码；失败时 HTTP 400，`detail` 为 `{ success: false, error: "..." }`。

---

## 6. 相关文档

- [credentials.md](credentials.md) — SSH **登录**凭证  
- [terminal.md](terminal.md) — 控制台与 sudo 交互  
- [ai-assistant.md](ai-assistant.md) — AI 助手总览  
