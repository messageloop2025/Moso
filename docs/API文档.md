# 毛竹 API 文档

除登录/注册外，请求头需带：`Authorization: Bearer <access_token>`。  
`<access_token>` 可为登录 JWT，或系统设置中签发的 **`eop_…` API Token**（推荐外部集成长期使用）。  
基础路径：`/api`（下文路径均省略此前缀）。

集成（OpenClaw / Hermes / MCP）REST 明细见 **§16–§19**；总览见 [外部集成与ClawOps.md](外部集成与ClawOps.md)。

---

## 1. 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /auth/captcha | 获取登录/注册用验证码（无需认证） |
| POST | /auth/login | 登录（需携带验证码；连续 5 次密码错误将锁定账户） |
| POST | /auth/register | 注册（需携带验证码 token 与答案） |
| GET | /auth/me | 当前用户信息（需登录） |
| POST | /auth/change-password | 当前用户修改自己的密码（需登录） |
| POST | /auth/forgot-password | 忘记密码：按用户名发送重置邮件（需用户已绑定邮箱且系统配置 SMTP、site_url） |
| POST | /auth/reset-password | 通过邮件链接 token 重置密码（无需登录） |
| POST | /auth/request-unlock | 账户锁定后请求解锁邮件（需用户已绑定邮箱且配置 SMTP） |
| POST | /auth/unlock-by-token | 通过邮件中的 token 解锁账户（无需登录） |

### GET /auth/captcha

**响应**
```json
{ "success": true, "captcha_token": "string", "question": "string" }
```
- **captcha_token**：提交登录/注册时与用户输入的答案一并提交，用于服务端校验。  
- **question**：简单数学题（如 "3 + 5 = ?"），用户将计算结果填入，提交时作为 **captcha_answer**。

### POST /auth/login

**请求体**
```json
{ "username": "string", "password": "string", "captcha_token": "string", "captcha_answer": "string" }
```
**captcha_token**、**captcha_answer** 为必填；验证码错误或过期时返回 400。

**响应**
```json
{ "access_token": "string", "token_type": "bearer", "user": { "id", "username", "display_name", "role" } }
```
- 若账户已锁定（连续 5 次密码错误）：返回 **403**，detail 提示「账户已锁定，可通过邮件解锁或联系管理员」。

### POST /auth/register

**请求体**
```json
{ "username": "string", "password": "string", "display_name": "string?", "captcha_token": "string", "captcha_answer": "string" }
```
**captcha_token**、**captcha_answer** 为必填；验证码错误或过期时返回 400。

**响应**：同 login。

### GET /auth/me

**响应**
```json
{ "success": true, "user": { "id", "username", "display_name", "role", "status" } }
```

### POST /auth/change-password

**请求体**
```json
{ "old_password": "string", "new_password": "string" }
```
新密码至少 6 个字符；旧密码错误返回 400。

**响应**：`{ "success": true }`

### POST /auth/forgot-password（忘记密码）

**请求体**：`{ "username": "string" }`  
若用户存在且已绑定邮箱，则生成重置 token 并发送邮件；响应统一为成功提示（不暴露用户是否存在）。需在系统设置中配置 **smtp_*** 与 **site_url**。

### POST /auth/reset-password（邮件重置密码）

**请求体**：`{ "token": "string", "new_password": "string" }`  
token 来自邮件链接；新密码至少 6 位。成功后清除该用户的锁定状态。

### POST /auth/request-unlock（请求解锁邮件）

**请求体**：`{ "username": "string" }`  
账户被锁定时可请求发送解锁链接到用户邮箱。

### POST /auth/unlock-by-token（邮件解锁）

**请求体**：`{ "token": "string" }`  
token 来自解锁邮件链接。

---

## 1.1 个人 API 访问令牌

用户可在系统设置中创建 **`eop_…`** 令牌，与 JWT 一样通过 `Authorization: Bearer` 调用 API。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /user-api-tokens | 列出当前用户的令牌（仅 prefix，不含明文） |
| POST | /user-api-tokens | 创建。body：`name?`；响应含 **`token`** 明文（仅一次） |
| DELETE | /user-api-tokens/{token_id} | 吊销/删除 |

每人最多 **50** 个活跃令牌。

---

## 2. 用户管理（管理员）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /users | 用户列表（含 email、locked_until） |
| POST | /users | 新建用户 |
| PUT | /users/{user_id} | 更新用户（含 email、**skills_enabled**） |
| DELETE | /users/{user_id} | 删除用户（**级联清理该用户一切数据**：其主机、这些主机在他人账号上的每用户数据（含聊天记录）、该用户的所有 AI 会话/凭证/自动化任务/令牌/发信配置等；`best_practices` 仅把 `created_by` 置 NULL） |
| POST | /users/{user_id}/reset-password | 重置密码（同时解锁） |
| POST | /users/{user_id}/unlock | 解锁被锁定的用户 |
| POST | /users/me/delete | **用户自助注销账户**（任一登录用户，**管理员禁用**）：体 `{password: string, confirm: "DELETE"}`；校验当前密码 + 文字二次确认后，按与 `DELETE /users/{user_id}` 完全一致的级联逻辑清理所有数据 |

**POST /users** 体：`username`, `password`, `display_name?`, `role?`  
**PUT /users/{id}** 体：`display_name?`, `role?`, `status?`, `email?`, **`skills_enabled?`**（管理员为指定用户开启/关闭个人 **Agent Skills** 功能，默认关）

列表与详情响应含 **`skills_enabled`**（bool）。登录 / refresh / `/auth/me` 的 `user` 对象亦含该字段，供前端决定是否显示「Skills」菜单。  
**POST reset-password** 体：`{ "password": "string" }`  
**POST /users/me/delete** 体：`{ "password": "当前密码", "confirm": "DELETE" }`；成功返回 `{success, message}`，前端需自行清除登录态并跳回 `/login`。管理员账户被拒绝（避免误删最后一位管理员）。

---

## 3. 主机

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /hosts | 主机列表（含 credential_code/name；普通用户返回“自有主机 + 收到分享的主机”） |
| GET | /hosts/stats | 主机统计 |
| GET | /hosts/check-duplicate | 检测当前用户下是否已有相同地址+端口的主机（含所有者信息） |
| GET | /hosts/{host_id} | 主机详情 |
| POST | /hosts | 新建主机（可选 `allow_duplicate` 强制添加） |
| PUT | /hosts/{host_id} | 更新主机（仅主机所有者/管理员） |
| DELETE | /hosts/{host_id} | 删除主机（所有者/管理员=真实删除；接收方=解除分享） |
| GET | /hosts/shares/received | 当前用户收到的主机分享列表 |
| GET | /hosts/shares/sent | 当前用户发出的主机分享列表 |
| POST | /hosts/{host_id}/shares | 将主机分享给某用户（仅所有者/管理员） |
| GET | /hosts/{host_id}/shares | 查看某主机分享清单（仅所有者/管理员） |
| DELETE | /hosts/{host_id}/shares/{target_user_id} | 撤销某用户的主机分享（所有者可撤销任意接收方；接收方可撤销自己） |
| POST | /hosts/{host_id}/execute | 在主机上执行 SSH 命令 |
| POST | /hosts/{host_id}/check-type | 检测主机操作系统类型、版本、Shell、包管理器并写回主机信息（可重复执行，有变化则更新） |

主机列表与详情均返回 **aliases**（字符串数组，别名）、**remark**（用途说明）、**host_type**、**host_version**（默认「未知」），以及 **host_shell**、**host_package_manager**（检测得到或为空）。PUT 更新主机时也可传 **host_type**、**host_version**、**host_shell**、**host_package_manager** 手动设置；传 **aliases** 时为**整表替换**（传 `[]` 可清空别名），**remark** 为用途说明文本。

集成运维 AI 工具 **list_hosts** 的搜索条件会匹配 **description**、**remark** 与 **aliases**（JSON 子串），便于用昵称定位主机。

### AI 帮助文档（Markdown 章节）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/aihelp/index` | index.md；支持 `sections_only`、`max_level`、`section_path` / `heading` / `max_chars` 等 |
| GET | `/api/aihelp/files` | 列出全部 `.md` |
| GET | `/api/aihelp/file?path=hosts.md` | 读单文件；同上章节参数；默认全文（可 `max_chars` 截断） |
| GET | `/api/aihelp/search?q=关键字` | 章节搜索；`scope=titles\|content\|all`；可选 `path` 限定单文件 |
| PUT | `/api/aihelp/file?path=...` | 管理员覆盖写入（body: `{content}`） |

用户 Skills Markdown：`GET /api/user-skills/by-name/{skill_name}/markdown?path=SKILL.md`（参数与 aihelp 章节读/搜一致）。

### GET /hosts/check-duplicate

查询参数：`host`（必填）、`port`（可选，未传按 **22** 参与比较）。

响应：`{ "success": true, "duplicate": bool, "host", "port", "existing_host": null | { id, name, host, port, created_by, created_by_username, created_by_display_name } }`

重复规则：仅当**同一所有者**（当前登录用户）下，`lower(trim(host))` 与 **port** 均与已有记录一致时 `duplicate=true`。不同用户可拥有相同 `host:port`。

### POST /hosts（新建）

必须二选一：**credential_id**（已有凭证）或 **new_credential**（新建凭证并关联）。

未传 `port` 时默认 **22**。与已有记录重复时默认返回 **409**，`detail` 含 `code: host_duplicate` 与 `existing_host`（含所有者字段）；请求体设 **`allow_duplicate": true`** 可仍创建一条新记录。

**请求体**
```json
{
  "name": "string",
  "host": "string",
  "port": 22,
  "credential_id": null,
  "new_credential": {
    "code": "string?",
    "name": "string",
    "username": "string",
    "type": "password|key_pair",
    "description": "",
    "password": "string?",
    "public_key": "string?",
    "private_key": "string?"
  },
  "description": "",
  "aliases": ["别名一", "别名二"],
  "remark": "用途说明，如：生产环境 Web 入口",
  "allow_duplicate": false
}
```

其中 **aliases**、**remark** 均为可选；**aliases** 为字符串数组，省略表示默认 `[]`，**remark** 省略表示空字符串。

**响应**：`{ "success": true, "id": number }`

### PUT /hosts/{host_id}（更新）

可部分更新。常用字段与 POST 一致；**aliases** 若传入则**整体替换**（`[]` 清空）；**remark** 传入则更新用途说明。  
权限：仅主机所有者或管理员可更新。

### DELETE /hosts/{host_id}（删除 / 解除分享）

- 主机所有者或管理员调用：**真实删除主机**；
- 分享接收方调用：仅**解除自己收到的分享**（不会删除真实主机），并自动清理该主机在接收方分组中的残留关联。

### 主机分享接口

#### POST /hosts/{host_id}/shares

请求体（二选一）：
```json
{ "user_id": 12 }
```
或
```json
{ "username": "alice" }
```
说明：仅主机所有者/管理员可分享；不能分享给自己。

#### GET /hosts/{host_id}/shares

返回该主机当前有效分享清单（分享给了谁、何时分享）。

#### DELETE /hosts/{host_id}/shares/{target_user_id}

撤销分享。撤销后会自动清理该接收方在其分组中的残留主机关联。

#### GET /hosts/shares/received

当前用户收到的分享主机清单（含来源用户信息）。

#### GET /hosts/shares/sent

当前用户发出的分享主机清单（含接收用户信息）。

### POST /hosts/{host_id}/execute

**请求体**：`{ "command": "string", "timeout": 30 }`  
**响应**：`{ "success": true, "stdout", "stderr", "exit_code" }`

### POST /hosts/{host_id}/check-type

无请求体。通过 SSH 检测主机操作系统类型、版本、Shell（bash/zsh/sh 等）、包管理器（apt/yum/apk 等）并写回主机信息，供 AI 优化命令与脚本策略。  
**响应**：`{ "success": true, "host_type": "Linux", "host_version": "Ubuntu 22.04.3 LTS", "host_shell": "bash", "host_package_manager": "apt" }`  
可重复调用；若检测结果与当前保存值不同会更新。

---

## 4. 主机分组

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /host-groups | 分组扁平列表 |
| GET | /host-groups/tree | 树形结构 + ungrouped_hosts |
| GET | /host-groups/{group_id} | 分组详情（含 host_ids） |
| POST | /host-groups | 新建分组 |
| PUT | /host-groups/{group_id} | 更新分组 |
| DELETE | /host-groups/{group_id} | 删除分组 |
| GET | /host-groups/{group_id}/hosts | 分组下主机列表 |
| POST | /host-groups/{group_id}/hosts | 将主机加入分组 |
| DELETE | /host-groups/{group_id}/hosts/{host_id} | 从分组移除主机 |

**POST /host-groups** 体：`{ "name", "description?", "parent_id?" }`  
**POST .../hosts** 体：`{ "host_ids": [number] }`

**GET /host-groups/tree** 响应：`{ "success", "tree": [...], "ungrouped_hosts": [], "by_user?": [...] }`（结构见实现；树中每项主机含 **aliases**、**remark**）。前端「服务器树」页支持**按主机名、IP、别名、用途说明**等搜索（仅前端过滤，不改变本接口）。

补充说明（与主机分享联动）：
- 普通用户的树数据包含“自有主机 + 收到分享的主机”；
- 分享主机可加入接收方自己的分组；
- 分组归属按“用户视角”维护，不影响主机所有者的原分组；
- 分享撤销或接收方解除分享时，会自动清理该用户分组中的残留关联。

---

## 5. 凭证

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /credentials | 凭证列表（脱敏） |
| GET | /credentials/{credential_id} | 凭证详情（脱敏） |
| POST | /credentials | 新建凭证（管理员） |
| PUT | /credentials/{credential_id} | 更新凭证（管理员） |
| DELETE | /credentials/{credential_id} | 删除凭证（管理员） |
| POST | /credentials/generate-key | 生成 RSA/ECC 密钥对（管理员） |

**POST /credentials** 体：`type`, `code`, `name`, `description?`, `username?`, `password?`（password 型）, `key_type?`, `key_bits?`, `public_key?`, `private_key?`（key_pair 型）  
**POST /credentials/generate-key** 体：`{ "key_type": "RSA|ECC", "key_bits": 2048 }`，响应含 `public_key`, `private_key`。

---

## 6. 维护历史

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /maintenance-history | 列表（query: host?, category?, limit=200） |
| GET | /maintenance-history/{item_id} | 单条详情 |
| POST | /maintenance-history | 新建 |
| PUT | /maintenance-history/{item_id} | 更新 |
| DELETE | /maintenance-history/{item_id} | 删除 |

**POST** 体：`host`, `port?`, `category`, `content?`, `file_path?`, `details?`

---

## 7. 最佳实践

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /best-practices | 列表（query: category?, keyword?） |
| GET | /best-practices/categories | 分类列表 |
| GET | /best-practices/{item_id} | 单条详情 |
| POST | /best-practices | 新建 |
| PUT | /best-practices/{item_id} | 更新 |
| DELETE | /best-practices/{item_id} | 删除 |

**POST** 体：`title`, `category?`, `content`, `source?`

---

## 8. SSH 终端

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /terminal/buffer | 当前用户**第一个控制台**（slot 0）最近输出（供 AI 上下文） |
| POST | /terminal/send | 向当前用户**第一个控制台**（slot 0）注入输入（文本未以换行结尾时自动补回车） |

**POST /terminal/send** 体：`{ "text": "string" }`

**WebSocket /api/terminal/ws**：建立 SSH 终端通道。首帧须为 JSON：`{ "type": "init", "host_id": number, "slot": number? }`。  
- **slot** 可选，默认 0；0 表示「第一个控制台」，AI 使用的 get_terminal_buffer / send_to_terminal 仅针对 slot 0；可传 1、2 等支持多控制台。  
- 之后客户端发送的为终端输入（xterm 原始字符或 `{ "type": "input", "data": "string" }`）。  
- 服务端回复 `{ "type": "ready", "slot": number }` 后开始转发 SSH 输出为文本帧。

---

## 9. 本地文件系统（web/fs，按用户隔离）

根目录为 **web/fs/当前用户名**；管理员可传 query **username=** 访问指定用户目录。路径均相对该根，禁止 `..` 逃逸。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /fs/list | 列出目录（query: path?, **username?** 仅管理员） |
| GET | /fs/read | 读取文本文件（query: path, username?） |
| PUT | /fs/write | 写入文本（body: path, content；query: username?） |
| POST | /fs/mkdir | 创建目录（query: path, username?） |
| POST | /fs/upload | 上传文件（Form: path?, file；query: username?） |
| GET | /fs/download | 下载文件（query: path, username?） |
| POST | /fs/pack-tgz | 将目录打包为 .tgz（query: path, username?） |
| POST | /fs/unpack-tgz | 解压 .tgz（query: path, dest?, username?） |
| DELETE | /fs/delete | 删除文件或空目录（query: path, username?） |
| POST | /fs/copy | 复制或移动（body: path, dest_dir, move；query: username?） |

**GET /fs/list**：path 为空或 `/` 表示用户根目录。  
**响应**：`{ "success": true, "path": "...", "items": [{ "name", "path", "dir", "size" }, ...] }`

---

## 10. 远程文件系统（SSH 主机）

通过 SSH/SFTP 列出与读取主机上的文件，路径禁止 `..` 逃逸。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /remote-fs/list | 列出主机某目录下的项（目录在上、文件在下） |
| GET | /remote-fs/read | 读取主机上的文本文件内容（约 2MB 内，供预览） |
| GET | /remote-fs/download | 下载主机上的文件（约 50MB 内，二进制流） |

**GET /remote-fs/list**：query `host_id`（必填）、`path`（默认 `/`）。  
**响应**：`{ "success": true, "path": "/...", "items": [{ "name", "path", "dir", "size" }, ...] }`

**GET /remote-fs/read**：query `host_id`、`path`（必填，文件路径）。  
**响应**：`{ "success": true, "path": "/...", "content": "string" }`（仅支持文本解码，否则 400）

**GET /remote-fs/download**：query `host_id`、`path`。  
**响应**：`Content-Type: application/octet-stream`，`Content-Disposition: attachment; filename*=UTF-8''...`

---

## 11. AI 助手

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /ai/config | AI 配置。当前用户获取自己的；**管理员可传 query user_id 获取指定用户的配置**。返回 config、model_types、context_size_options 等 |
| GET | /ai/config/system | **仅管理员**：获取系统默认 AI 配置（来自全局设置），用于「将系统配置应用到用户」等 |
| POST | /ai/config/apply-system | **仅管理员**：将系统默认 AI 配置写入指定用户。query: **user_id** |
| GET | /ai/config/trial | 查询系统共享 Key 配额状态（路径名仍为 `trial`）。无 query 查自己；**管理员可传 user_id 查任意用户**。返回 `{user_id, has_own_key, used, limit, remaining, exhausted, system_key_available}` |
| POST | /ai/config/trial/reset | **仅管理员**：清零指定用户的系统共享 Key 调用计数。query: **user_id** |
| POST | /ai/config/trial/unlock | **仅管理员**：将系统默认 AI 配置写入指定用户并清零共享 Key 计数；之后该用户按自有配置调用，不再受共享 Key 次数限制。query: **user_id** |
| POST | /ai/config | 更新 AI 配置。当前用户更新自己的；**管理员可在请求体中传 user_id 更新指定用户的配置**。请求体可含 context_size（0=不限制）等 |
| GET | /ai/sessions | 会话列表（query: **host_id?**；有 host_id 仅返回该主机会话，无则仅返回全局会话；每项含 `low_interaction_mode`） |
| POST | /ai/sessions | 新建会话（query: title?；body 可选 **host_id**，用于主机详情页 AI 运维） |
| GET | /ai/sessions/{session_id} | 会话详情（含 `low_interaction_mode`） |
| PATCH | /ai/sessions/{session_id} | 更新会话（如标题、`low_interaction_mode`；低交互默认 false，前端显示为「低交互」/`Auto`） |
| POST | /ai/sessions/{session_id}/runtime-control | 运行中注入控制（`supplement`/`pause`/`resume`/`stop`），用于 AI 流式执行或工具调用期间补充上下文、暂停/恢复或停止 |
| PATCH | /ai/sessions/{session_id}/messages/{message_id} | 更新单条会话消息内容（当前用于将 Mermaid 自动修复后的助手消息写回持久化） |
| DELETE | /ai/sessions/{session_id} | 删除会话 |
| POST | /ai/sessions/clear | 清空当前用户会话（body 可选 **host_id**：仅清空该主机会话；无则清空全局会话） |
| GET | /ai/sessions/{session_id}/prompt | 获取当前会话的会话级提示词（返回 prompt 文本，建议 Markdown 格式） |
| PUT | /ai/sessions/{session_id}/prompt | 更新会话级提示词（body: `{ "prompt": "string" }`） |
| POST | /ai/sessions/{session_id}/prompt/summarize | 由 AI 根据最近对话归纳会话级提示词并替换或追加（query: **action**=replace\|append；返回 prompt、skipped?） |
| GET | /ai/skills | 启用中的 Skills 列表（供前端/外部展示） |
| POST | /ai/chat | 对话（SSE 流式） |

### POST /ai/chat（流式）

**请求体**：`{ "message": "string", "session_id": number|null, "host_id": number|null, "attachment_uuids": string[]? }`  
- 若未传 session_id，后端会新建会话并在首条 SSE 中返回 `session_id`。  
- **host_id**：可选；在主机详情页「AI 运维」下发时传入，新建的会话会绑定该主机（仅该主机会话列表可见；system 中会注入主机范围说明）。
- **attachment_uuids**：可选；本轮用户通过 📎 上传的附件 UUID 列表。后端会把附件元信息追加到 user 消息末尾的「📎 附件」清单，供 AI 调用 `read_chat_attachment`；图片在开启「支持图像识别」时还可能内联多模态。
- Mermaid 图形块在前端渲染前会先做预检查和自动修复；若修复成功，前端可调用 `PATCH /ai/sessions/{session_id}/messages/{message_id}` 将修复后的助手消息内容回写到历史记录，避免下次打开再次重复修复。

**流式回复前端渲染**：助手正文在 SSE 过程中采用**分块增量 Markdown**（`edgeopsRenderStreamIncremental`）：已闭合的代码块、表格（表后空行或非表行）、段落（`\n\n`）立即排版并冻结 DOM；尾部未完成块以纯文本或实时表格 tbody 更新，避免逐字全量 `innerHTML` 闪动；流结束后仍做一次完整 `formatMarkdown` 收尾。

**响应**：`Content-Type: text/event-stream`。每行 `data: <JSON>`，事件类型包括：

| 字段 | 说明 |
|------|------|
| session_id | 会话 ID |
| content | 助手回复文本片段（流式拼接） |
| action | executing / completed / failed（工具执行状态） |
| tool, args, result_preview | 工具名、参数、结果摘要 |
| ui_action | 前端动作：`connect_terminal`（host_id）、`switch_console`（slot, scope）、`ensure_local_console` / `create_local_console`（scope: local, created_by: ai）、`close_local_console`（scope: local, slot）、`ask_user_choice`（question, options[id,label,value,style,description], allow_multiple, allow_text, default_id — 由 AI 工具 `ask_user_choice` 触发，前端在对应 assistant 气泡下渲染可点击的选择卡片；OpenClaw/API 集成通道不会收到此 ui_action，改由工具返回 `ui_capable=false` 的纯文本回退） |
| assistant_continue | 辅助 AI 的继续执行引导语 |
| requires_user_confirm | 与 `assistant_continue` 配套；`true` 表示需用户确认是否继续旧任务 |
| runtime_control | 运行时控制回执（例如 `{action, accepted, during_tool?}`） |
| _edgeops_ping | SSE 保活帧。前端忽略该字段；用于慢工具（如网页搜索）期间保持 chunked 流活跃 |
| error | 错误信息 |

结束标记：`data: [DONE]`。

**运行中控制**：  
`POST /ai/sessions/{session_id}/runtime-control` 请求体：

```json
{ "action": "supplement", "message": "补充信息或控制说明" }
```

- `action` 支持 `supplement`、`pause`、`resume`、`stop`；未知值返回 400。
- `supplement`：把新上下文插入当前会话的 runtime control 队列，AI 在当前轮可用时优先整合。
- `pause` / `resume`：用于让用户临时提问、补充条件后再继续。
- `stop`：请求停止当前 agent 循环；若正在等待工具，后端会尽量在等待间隙中断并返回工具被运行时控制打断的结果。
- 后端在新一轮 `/ai/chat` 开始时会清空该会话残留的 runtime control 队列，避免上一轮控制指令影响下一条普通消息。

**SSE 稳定性约定**：  
`POST /ai/chat` 响应头包含 `Cache-Control: no-cache, no-transform`、`Connection: keep-alive`、`X-Accel-Buffering: no`，并周期性发送 JSON keepalive。慢工具等待使用 `asyncio.shield` 避免轮询超时取消底层任务；若工具异常，流内返回失败事件而不是让浏览器收到不完整 chunk。

**非流式错误**（请求未进入流式前即返回）：  
- **400**：未配置服务地址或 API Key（Ollama 可留空 Key）；未配置时若系统已配置 AI_API_KEY/AI_BASE_URL，则自动使用系统共享 Key 并计入该用户配额。**用户配置了自己的 KEY 则不受此计数限制。**  
- **403**：仅 `/api/ai/summarize-host-prompt*`、`/api/ai/sessions/{id}/summarize-title` 等**一次性操作**在共享 Key 配额用尽时返回。提示文案与线上一致，大意：配额已达上限（默认 2000 次），请在「我的 AI 配置」填写自有 Key 以解除次数限制，或联系管理员重置计数 / 写入系统默认配置。
- **使用共享 Key 时的额外 SSE 事件**：`POST /api/ai/chat` 若走系统共享 Key，会在流开头多推 `{"trial_info": {"exhausted": false, "used": N, "limit": 2000, "remaining": R}}`（字段名仍为 `trial_info`），且 AI 本轮首条 assistant 回复前会自动追加 Markdown 横幅展示已用/剩余。
- **配额用尽时**：`POST /api/ai/chat` **不再返回 403**，而是直接用固定 Markdown 文案流式回复（持久化到 `ai_chat_messages`），SSE 里 `trial_info.exhausted = true`；正文引导用户配置自有模型或联系管理员。
- **管理员操作方式**：
  - UI：「用户管理」→ 用户行 **「AI 配额」**，可查看 `used / limit / remaining` 与是否自有 KEY：
    - **重置共享 Key 计数**（`POST /ai/config/trial/reset?user_id=<id>`，等效 `reset_user_system_ai_usage`）；
    - **写入系统配置并解除次数限制**（`POST /ai/config/trial/unlock?user_id=<id>`，等效 `apply_system_ai_config_to_user` + 清零计数）。
  - AI：可调用上述两技能完成等价操作。
- **普通用户自查**：`GET /ai/config/trial` 返回配额状态；「我的 AI 配置」页顶部横幅同步展示额度或「已使用自有 Key」类提示。

---

## 11.1 聊天附件（/api/ai/attachments）

用户在 AI 输入框上传或粘贴的参考材料；落盘 `web/fs/<username>/chats/YYYY/MM/DD/<uuid>.<ext>`，元数据表 `chat_attachments`。与「文件系统」菜单共用用户 fs 根，但路径隔离在 `chats/` 下。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /ai/attachments | 上传单个文件（`multipart/form-data`：`file`；可选 `session_id`） |
| GET | /ai/attachments | 列出当前用户附件（query: **session_id** 可选） |
| GET | /ai/attachments/{uuid} | 下载/预览原文件（`Authorization: Bearer` 或 query **token**=JWT，供 `<img src>`） |
| GET | /ai/attachments/{uuid}/meta | 元信息（kind、mime、size 等） |
| POST | /ai/attachments/bind | 将已上传附件绑定到会话（body: `{ uuids[], session_id, message_id? }`） |
| DELETE | /ai/attachments/{uuid} | 删除附件（含 MarkItDown 旁路缓存 `*.extracted.md`） |

**kind 取值**：`image`、`text`、`markdown`、`document`（Office/PDF 等，供 MarkItDown 转换）、`binary`。

**限制**（`config.py` / 环境变量）：

| 配置 | 默认 | 说明 |
|------|------|------|
| `EDGEOPS_CHAT_ATTACHMENT_MAX_BYTES` | 20 MB | 单文件上限 |
| `EDGEOPS_CHAT_ATTACHMENT_SESSION_QUOTA_BYTES` | 500 MB | 单会话附件累计上限 |
| `EDGEOPS_MARKITDOWN_ENABLED` | true | 是否对 document 类附件做 Office/PDF→Markdown |
| `EDGEOPS_MARKITDOWN_MAX_OUTPUT_CHARS` | 500000 | 单次转换写入/返回的最大字符数 |

**AI 读取**：Agent 通过技能 `read_chat_attachment(uuid, max_chars?, …)` 获取文本；`document` 类型由 `services/markitdown_convert.py`（Microsoft MarkItDown）转为 Markdown 后返回 `content`，响应含 `converted_from_markitdown: true`。转换结果缓存为同目录 `原文件名.extracted.md`。

---

## 11.2 AI 成果物（/api/ai/artifacts）

AI 通过 `create_chat_artifact` 技能写入的报告/数据包/HTML 等；落盘 `web/fs/<username>/chats/YYYY/MM/DD/<artifact-uuid>/`（与附件共用 `chats/` 根，附件为单文件、成果物为子目录）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /ai/artifacts | 列出成果物（query: **session_id** 可选） |
| GET | /ai/artifacts/{uuid}/meta | 元信息（title、file_count、entry_file 等） |
| GET | /ai/artifacts/{uuid}/download | 下载（单文件直传；bundle 打包为 `.tgz`） |
| GET | /ai/artifacts/{uuid}/file | 读取入口或单文件内容/流（预览用，支持 `?download=1`） |
| GET | /ai/artifacts/{uuid}/files/{path} | 读取 bundle 内指定相对路径 |
| POST | /ai/artifacts/{uuid}/bind | 绑定到会话 |
| DELETE | /ai/artifacts/{uuid} | 删除成果物目录 |

创建仅通过 AI 技能 `create_chat_artifact`，无独立 POST 上传接口。

---

## 12. Skills（仅读）

**AI 工具列表（Function Calling）**：Agent 对话时使用的工具由 `services/ai_skills.py` 的 TOOLS 定义；以下接口供前端或外部展示用。

当前工具集中包含通用只读数据处理能力：`regex_process`（正则处理）、`string_process`（字符串处理）、`math_calculate`（数学/科学计算与单位换算）、`data_query`（JSON/YAML 解析、路径读取、搜索与过滤）、`markup_query`（XML/HTML 摘要、标签/选择器/文本/属性/链接搜索与提取）。这些工具通过 `/ai/chat` 的 function calling 使用，`GET /ai/skills` 会返回其摘要。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /ai/skills | AI 可调用工具摘要（name、description），与 Agent 实际使用的 TOOLS 一致 |

**数据库 Skills 表**：扩展用技能定义（如提示词类能力），与上述 TOOLS 可并存。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /skills | 启用中的 skills 表记录（id、code、name、description、parameters_schema） |
| GET | /skills/{skill_id} | 单条技能详情（含 parameters_schema） |

---

## 12.1 用户发信配置（个人 SMTP）

**登录**用户读写**本人**发信参数，与管理员「全局设置」中的 **smtp_*** 无关。用于：AI 工具 `send_email`、定时任务 `notify_email_to` 结果通知。绑定邮箱验证码、忘记密码等仍使用全局 SMTP（见下文 §14）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /user-mail-config | 返回 `config`（**不返回** `smtp_password` 明文；含 `smtp_password_set`、`smtp_config_complete`、`may_send_mail`）、`setup_hint`（配置指引文案） |
| PUT | /user-mail-config | body 可选字段：`mail_enabled`、`smtp_host`、`smtp_port`、`smtp_user`、`smtp_password`、`smtp_from`、`smtp_use_tls`、`smtp_use_ssl`。默认 `mail_enabled=false`；若将 `mail_enabled` 置为 true，须已具备完整 SMTP（否则 **400**）。`smtp_password` 仅在不省略且非空时更新密码；省略表示保留库中已有密码 |

---

## 12.5 定时任务

进程内调度器（`services/scheduler.py`）按 cron 与 `next_run_at` 到点触发后台 AI 执行；**单 Uvicorn 进程**下约每 30 秒扫描一次。详见 [并发与扩展.md](并发与扩展.md)。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /scheduled-tasks | 当前用户任务列表（含 `enabled`、`next_run_at`、`is_running`、`notify_email_to` 等） |
| POST | /scheduled-tasks | 新建。body：`name`、`content`、`cron_expr?`（五段：分 时 日 月 周）、`enabled?`（默认 true）、`notify_email_to?`（可选；收件人逗号/分号分隔，执行结束后用**该用户**个人 SMTP 发摘要） |
| GET | /scheduled-tasks/triggered-list | 本用户触发任务列表（供定时任务内 AI 决策调用） |
| GET | /scheduled-tasks/all-runs | 全部执行历史（query：`task_id?`、`task_name?`、`status?`、`from_time?`、`to_time?`、`limit?`） |
| GET | /scheduled-tasks/{task_id} | 单条任务详情 |
| PATCH | /scheduled-tasks/{task_id} | 更新；可含 `enabled`（停用后不跑 cron，仍可「立即执行」）、`cron_expr`（会重算 `next_run_at`）、`name` / `content` / **`notify_email_to`** |
| DELETE | /scheduled-tasks/{task_id} | 删除任务并**级联删除**该任务全部 `scheduled_task_runs` 与 `scheduled_task_run_messages` |
| POST | /scheduled-tasks/{task_id}/run-now | 立即执行一次（不等 cron） |
| GET | /scheduled-tasks/{task_id}/runs | 该任务执行历史 |
| GET | /scheduled-tasks/{task_id}/runs/{run_id}/messages | 单次执行的会话式消息 |

**DELETE**：若任务不存在返回 404。

---

## 13. 批量操作（参考 IOTHub）

向多台主机下发命令/上传文件/执行脚本/重启。脚本与资源可放在文件系统（web/fs，如 scripts/、docs/）。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /batch | 创建批量任务并异步执行（**登录**；普通用户仅能选自己创建的主机/分组） |
| GET | /batch | 最近批量任务列表（**登录**；普通用户仅看自己的） |
| GET | /batch/{batch_id} | 任务详情（含每台主机执行状态与结果） |
| POST | /batch/{batch_id}/cancel | 取消执行中的任务 |
| POST | /batch/{batch_id}/retry | 重试失败主机 |
| DELETE | /batch/clear | 清空批量任务记录（普通用户清空自己的，管理员可清空全部） |
| GET | /batch/export | 导出批量任务 CSV（同上范围） |

**POST /batch** 请求体：`operation_type`（run_command / scp_push / run_script / restart）、`scope_type`（all / group / selected）、`scope_value`（分组或主机 ID 数组）、`params`（依类型：run_command 含 command、timeout；scp_push 含 remote_path、content 或 local_path；run_script 含 script_path、remote_path；restart 可选 command，默认 sudo reboot）。

---

## 13.5 本机管理（仅管理员）

**以下接口均需管理员权限**（Bearer Token 且用户角色为管理员）。本机终端、本机文件系统、本机命令执行、Python 脚本执行、会话历史；全系统模式下支持绝对路径（如 C:/、/etc），否则相对项目根，禁止 `..` 逃逸。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /local/fs/list | 列出本机目录（query: path）。**根目录**：path 为空或 `/` 时，**Windows** 返回所有可用驱动器（C:、D: 等），**Linux** 返回根目录 `/` |
| POST | /local/fs/read | 读取本机文本文件（body: path, encoding?） |
| POST | /local/fs/write | 写入本机文件（body: path, content, encoding?） |
| POST | /local/fs/mkdir | 创建本机目录（body: path） |
| DELETE | /local/fs/delete | 删除本机文件或目录（query: path），含非空目录 |
| POST | /local/fs/rename | 本机重命名/移动（body: path, new_path） |
| POST | /local/fs/upload | 上传文件到本机目录（Form: path?, file） |
| GET | /local/fs/download | 下载本机文件（query: path） |
| POST | /local/execute | 在本机执行 shell 命令（body: command, timeout?, session_id?, cwd?） |
| POST | /local/run-script | 在本机执行 Python 代码或脚本（body: code?, script_path?, timeout?, session_id?） |
| GET | /local/sessions | 本机会话列表 |
| POST | /local/sessions | 创建本机会话（query: title?） |
| GET | /local/sessions/{id}/logs | 某会话的 command/script/stdout/stderr 日志 |
| GET | /local/buffer | 某控制台输出缓冲（query: slot?） |
| WebSocket | /local/ws | 本机终端（query: token）。首帧 `{ type: "init", slot? }`，服务端回复 `{ type: "ready", slot, cwd, platform, pty }`；之后客户端发 `{ type: "input", data }` / `{ type: "resize", cols, rows }`，服务端推送终端输出文本。**Windows** 下优先使用 ConPTY（pywinpty），无则 PIPE；**Linux** 使用 PTY |

---

## 14. 系统设置与日志

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /settings | 所有全局设置项（**仅管理员**；敏感键脱敏） |
| POST | /settings | 更新单条全局设置（body: key, value）（**仅管理员**） |
| GET | /logs | 操作日志（**普通用户仅能查看自己的日志**；管理员可查全部，query: page?, page_size?, host_id?, **user_id?**） |

**邮件与账户安全（系统设置键）**：管理员在「系统设置」中可配置以下键，用于忘记密码、邮件解锁、**绑定邮箱验证码**等**系统代发**场景：  
- **site_url**：站点根 URL（如 `https://edgeops.example.com`），用于生成重置/解锁链接。  
- **smtp_host**、**smtp_port**、**smtp_user**、**smtp_password**、**smtp_from**、**smtp_use_tls**（及部署中若使用的 SMTP SSL 相关约定）：全局 SMTP。  
用户需在用户管理中绑定 **email** 后，方可收到找回密码与解锁邮件。

**个人发信**：用户通过 **GET/PUT /user-mail-config**（或前端「系统设置 → 我的发信设置」）配置**自己的** SMTP；AI `send_email` 与定时任务通知只用个人配置，**不**读取上述全局 `smtp_*`。

**反馈邮件通知（管理员）**：`settings.notify_admin_on_user_feedback`（默认 `false`）。开启后任意用户提交新反馈时，由 `services/feedback_notify.py` 走**全局 SMTP** 给所有"绑定邮箱的管理员"发简短通知；带 30 秒去抖避免短时间内多条反馈刷屏；未配置 SMTP 或未绑邮箱则不发并仅记日志。

---

## 14.5 反馈与登录留言板（迁移 015）

两套通道共用 `services/feedback.py` 业务层，分别挂在 `api/login_board.py`（登录页匿名留言板，含公开/管理员两套路由）与 `api/feedback.py`（系统内反馈，含用户/管理员两套路由）。所有内容字段均为 **Markdown 字符串**。

### 14.5.1 登录页留言板 — 公开（无需认证）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /login-board/captcha | 留言用数学验证码（与 `/auth/captcha` 同实现，返回 `captcha_token` + `question`） |
| GET | /login-board | 公开留言列表：`status='approved'` 且 `show_on_login=1` 的留言及其下"显示在登录页"的管理员回复（树形或扁平 + `parent_id`，按 `created_at` 倒序）；query：`limit?` |
| POST | /login-board | 提交匿名留言。body：`nickname?`、`content`（Markdown）、`captcha_token`、`captcha_answer`。成功后写入 `anonymous_messages`，初始 `status='pending'`、`show_on_login=0`，记录 `ip_address` / `user_agent`。**限频**：同一 IP **60 秒内最多 3 条 + 24 小时内最多 20 条**，超限返回 **429** |

### 14.5.2 登录页留言板 — 管理员

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /admin/login-board | 全部留言列表（query：`status?` ∈ `pending`/`approved`/`hidden`/`all`、`limit?`、`offset?`） |
| POST | /admin/login-board/{id}/reply | 对某条留言写管理员回复。body：`content`、`show_on_login?`（默认 0）。回复也存在 `anonymous_messages` 表（`parent_id=id`、`author_type='admin'`、`author_user_id=管理员id`、`status='approved'`） |
| PATCH | /admin/login-board/{id} | 审核 / 修改留言或回复。body 可选：`status` ∈ `pending`/`approved`/`hidden`、`show_on_login` ∈ 0/1、`content?` |
| DELETE | /admin/login-board/{id} | 删除留言或回复（`parent_id ON DELETE CASCADE`：删主留言会一并清子回复） |

### 14.5.3 系统内反馈 — 用户（需登录）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /feedback | 当前用户自己的反馈列表（按 `created_at` 倒序，含状态、是否被管理员阅读 `admin_read_at`、回复条数等）。query：`status?`、`limit?`、`offset?` |
| POST | /feedback | 提交新反馈。body：`title?`、`content`（Markdown）、`category?`（默认 `general`）。初始 `status='open'`、`admin_read_at=NULL`。成功后异步触发管理员邮件通知（如 `settings.notify_admin_on_user_feedback=true`） |
| GET | /feedback/{id} | 查看自己某条反馈详情（含管理员历次回复） |
| PATCH | /feedback/{id} | 编辑自己 `status='open'` 且**尚无任何管理员回复**的反馈。body：`title?`、`content?`、`category?`。否则返回 **403** |
| DELETE | /feedback/{id} | 撤回自己 `status='open'` 且**无任何回复**的反馈。**实际为物理删除**（不保留 `withdrawn` 状态行）；`replied` / 已有回复时返回 **403** |

### 14.5.4 系统内反馈 — 管理员

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /admin/feedback | 全员反馈列表。query：`status?` ∈ `open`/`replied`/`ignored`/`all`、`unread?` ∈ `true`/`false`（仅 `admin_read_at IS NULL`）、`limit?`、`offset?`；返回每条反馈的提交人、状态、`admin_read_at`、回复条数 |
| GET | /admin/feedback/{id} | 取单条详情（含全部管理员回复），**自动把 `admin_read_at` 写入 `CURRENT_TIMESTAMP`** |
| POST | /admin/feedback/{id}/reply | 写一条管理员回复。body：`content`（Markdown）。状态从 `open` 转为 `replied`，可发多条；同时把 `admin_read_at` 设为 `COALESCE(admin_read_at, CURRENT_TIMESTAMP)` |
| POST | /admin/feedback/{id}/ignore | 标为 `ignored` |
| POST | /admin/feedback/{id}/reopen | 从 `ignored`/`replied` 转回 `open` |
| POST | /admin/feedback/{id}/mark-read | 单条标已读 |
| POST | /admin/feedback/mark-all-read | 全部标已读 |
| PATCH | /admin/feedback/replies/{reply_id} | 修改某条管理员回复内容。body：`content` |
| DELETE | /admin/feedback/replies/{reply_id} | 撤回某条管理员回复；若该条是该反馈的最后一条管理员回复，反馈状态会**回退到 `open`**（用户因此恢复编辑/撤回权限） |

### 14.5.5 状态机（user_feedback.status）

```
[创建] -> open
open --(用户编辑)--> open
open + 无回复 --(用户撤回)--> 物理删除
open --(管理员回复)--> replied
replied --(管理员撤回最后一条回复)--> open
open / replied --(管理员忽略)--> ignored
ignored / replied --(管理员重新打开)--> open
任意 --(管理员删除整条反馈)--> 物理删除（级联清回复）
```

> 注意：业务上**没有** `withdrawn` 状态的行保留——用户撤回是物理删除；只有 `open / replied / ignored` 三种存活状态。

### 14.5.6 限频与去抖

- 匿名留言：以 `ip_address` 为键，**60 秒内最多 3 条 + 24 小时内最多 20 条**；超限返回 **429**，body：`{ "detail": "提交过于频繁，请稍后再试（60 秒内最多 3 条）" }` 或 `{ "detail": "今日匿名留言已达上限（20 条），请明天再来" }`。常量在 `services/feedback.py`：`ANON_RATE_LIMIT_WINDOW_SEC=60`、`ANON_RATE_LIMIT_MAX=3`、`ANON_RATE_LIMIT_DAY_MAX=20`。
- 邮件通知：`feedback_notify.maybe_notify_admins_on_new_feedback` 内部维护进程内字典，**30 秒**内同一类事件最多发一封；多实例部署时去抖按进程独立。

---

## 15. 版本

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /version | 无认证；返回 `{ "success": true, "version": "string" }` |

---

## 16. 外部集成（OpenClaw / Hermes / MCP）

前缀 **`/integration`**。会话为 **`session_scope=integration`**，不出现在网页 AI 助手列表。  
OpenClaw 工具映射见 [外部集成与ClawOps.md](外部集成与ClawOps.md) 与 `claw-ops/openclaw.plugin.json`。

### POST /integration/ops-chat/complete

非流式集成运维对话（等同 claw-ops `edgeops_ops_chat`）。建议 HTTP 超时 ≥ 330s。

**请求体**

| 字段 | 类型 | 说明 |
|------|------|------|
| message | string | 运维意图（必填） |
| session_id | int? | 多轮沿用上次响应 |
| host_id | int? | 新会话绑定主机 |
| skip_secondary_assistant | bool | 默认 `true` |
| attachment_uuids | string[]? | 附件 UUID（`POST /ai/attachments`） |
| ui_locale | string? | BCP-47，如 `zh-CN` |

**响应**：`{ "success": true, "reply": "Markdown", "session_id": N, … }`

### GET /integration/hosts/search-by-prompt

在主机级 AI 提示词中搜索（等同 `edgeops_search_hosts_by_prompt`）。  
query：`query`（必填）、`group_id?`、`tag_ids?`、`regex?`、`case_sensitive?`、`limit?`（默认 30）、`snippet_chars?`

### GET /integration/spill/read

分段读取 spill 落盘（等同 `edgeops_read_chat_data`）。  
query：`spill_id`、`date_subdir`（如 `2026/05/22`）、`mode?`（head/tail/head_tail/range）、`session_id?`、`head_chars?`、`tail_chars?`、`range_start?`、`max_chars?`

---

## 17. SSH 交互通道（/ssh-channel）

无界面持久 SSH TTY，供集成 Agent、OpenClaw、后台任务使用。设计见 [SSH通道与后台任务设计.md](SSH通道与后台任务设计.md)。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /ssh-channel | 列表。`all_open=true` 列出全部 open 通道 |
| POST | /ssh-channel | 创建。body：`host_id`、`session_id?`（集成绑定，idle 默认 600s）、`idle_close_sec?` |
| GET | /ssh-channel/{id} | 详情。`check_alive?` |
| POST | /ssh-channel/{id}/send | 写入 stdin。body：`content` |
| GET | /ssh-channel/{id}/lines | 按行读。`since_line?`、`last_n?`、`from_line?`、`to_line?`、`session_id?` |
| GET | /ssh-channel/{id}/read | 按字符读。`max_chars?`、`session_id?` |
| GET | /ssh-channel/{id}/has-new | 轮询新输出。`after_line?` |
| POST | /ssh-channel/{id}/dump | 导出缓冲到 spill |
| DELETE | /ssh-channel/{id} | 关闭 |
| POST | /ssh-channel/close-batch | 批量关。body：`session_id?` 或 `owner_type` + `owner_id` |

大输出可能返回 `spill_id` + `storage_subdir`，用 **§16** `/integration/spill/read` 分段读取。

---

## 18. MCP 专用集成（/integration/mcp）

前缀 **`/integration/mcp`**。HTTP 头 **`X-EdgeOps-Client: mcp`** 必填（毛竹 内置 MCP client 自动携带）。  
无 Web UI 假设；编排接口**不进** claw-ops / OpenClaw。

### GET /integration/mcp/orchestrate/capabilities

返回 `{ orchestrate_v1, mcp_only, modes, task_control }`。

### POST /integration/mcp/orchestrate/chat

MCP 编排主编排（快响 + 后台子任务）。建议超时 ≤ 120s。

**请求体**：`message`（必填）、`session_id?`、`host_id?`  
**响应**：`mode` = `reply_direct` | `background_task`；`task_ids?`；`task_completions?`；`tasks_running?`

会话 scope = **`mcp_orchestrate`**。

### GET /integration/mcp/orchestrate/tasks

query：`session_id?`、`status?`、`limit?`（默认 30）

### GET /integration/mcp/orchestrate/tasks/{task_id}

子任务进度与结果（含 `progress` 数组）。

### POST /integration/mcp/orchestrate/tasks/{task_id}/control

body：`action` = `stop` | `supplement`，`message?`

### POST /integration/mcp/ssh-execute

等同 MCP 工具 `edgeops_ssh_execute`。body：`host_id`、`command`、`detach?`、`poll_log?`、`log_path?`、`tail_lines?`、`timeout?`、`session_id?`  
返回工具 JSON（含 `session_id` 供 poll_log 运行态）。

### POST /integration/mcp/hosts/{host_id}/capabilities/probe

body：`refresh?`、`max_age_hours?`、`timeout?`

### GET /integration/mcp/hosts/{host_id}/capabilities

### PUT /integration/mcp/hosts/{host_id}/prompt

body：`content`

### POST /integration/mcp/hosts/{host_id}/prompt/append

body：`text`

### GET /integration/mcp/sessions/{session_id}/messages

只读；仅 `integration` / `mcp_orchestrate` / `mcp_runtime` 会话。

### GET/POST /integration/mcp/remote-fs/list|read|write

代理 `/api/remote-fs`（无 web/fs 本地路径依赖）。

**其它 MCP 工具**（分组、审计、batch/定时任务等）直接调用既有 REST（`/api/host-groups`、`/api/logs` 等），见 [services/edgeops_mcp/README.md](../services/edgeops_mcp/README.md)。

---

## 19. ClawOps 集成（/integration/claw-ops）

OpenClaw 插件 **claw-ops** 专用。HTTP 头建议 **`X-EdgeOps-Client: openclaw`**（插件已自动携带）。

### GET /integration/claw-ops/manifest

返回 `capabilities_version`、`extended_tools`（JSON Schema）、`system_prompt.prepend_markdown`、`plugin.min/recommended_version`。  
**v1.1.0+ claw-ops** 在 Gateway 启动/`edgeops_gateway_ping` 时按 `extended_tools` 动态 `registerTool`（增量；执行仍走 invoke）。  
query：`base_url?`、`plugin_version?`、`capabilities_version?`（未变时 `unchanged: true`，仍返回完整 manifest）

### GET /integration/claw-ops/check-update

query：`plugin_version`（必填）

### GET /integration/claw-ops/system-prompt

仅返回提示词块（同 manifest 内 `system_prompt`）。

### POST /integration/claw-ops/invoke

body：`tool`（edgeops_* 名）、`arguments`（对象）  
执行**扩展工具**（P1/P2）。核心工具（如 `edgeops_list_hosts`）由插件直连对应 REST，不经此入口；传核心工具名会返回 `{success:false}`。业务失败（工具非法 / 参数错误 / 执行异常）以 HTTP 200 + `{success:false, error}` 返回，仅鉴权与客户端头校验走 4xx。

---

## 20. 用户自定义 MCP 服务器（/user-mcp-servers）

每用户隔离，把第三方 MCP（stdio / SSE / Streamable HTTP）接入 毛竹 自身 AI 助手。需登录态（JWT 或 `eop_` Token）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/user-mcp-servers` | 列出当前用户的 MCP 配置（脱敏，不回显 env/headers 原值） |
| POST | `/user-mcp-servers` | 新增；body：`name`、`display_name?`、`transport`、`command/args/env` 或 `url/headers`、`enabled?`、`chat_enabled?`、`chat_scope_web/host/integration?` |
| GET | `/user-mcp-servers/{id}` | 单条详情 |
| PUT | `/user-mcp-servers/{id}` | 更新（未传字段保持原值；env/headers 传 `""` 或 `***` 表示不改原值） |
| DELETE | `/user-mcp-servers/{id}` | 删除 |
| POST | `/user-mcp-servers/{id}/test` | 测试连接并统计工具数 |
| POST | `/user-mcp-servers/import` | 导入 Cursor 风格 `{config, overwrite?, chat_scope_*?}` |
| GET | `/user-mcp-servers/export` | 导出为 `{mcpServers, _edgeops?}`；query：`include_disabled?`、`include_edgeops_meta?`、`download=true` 时附件下载 |
| POST | `/user-mcp-servers/{id}/refresh-tools` | 清缓存并重新拉取工具 schema |

`chat_scope_web/host/integration` 控制该 MCP 在哪些 **AI 聊天场景**加载（见下表）。须同时 **`enabled`** 且 **`chat_enabled`**。启用后工具以 `user_mcp_{id}__{原名}` 注入 LLM。AI 聊天内亦可通过 `list_user_mcp_servers` / `configure_user_mcp_server` / `import_user_mcp_config` / **`export_user_mcp_config`** / `test_user_mcp_server` / `refresh_user_mcp_tools` / `delete_user_mcp_server` 自助管理。

**个人 MCP 与 Agent Skills 生效场景**（两者过滤逻辑一致；MCP 注入工具，Skills 注入 system 正文）：

| 聊天入口 | 判定条件 | 对应场景开关 |
|----------|----------|--------------|
| **AI 助手**（`/ai`）、**本机管理 AI**（管理员） | `session_scope` 为 `default`/`local`，且会话 **无** `host_id` | `chat_scope_web` |
| **主机详情 · AI 运维** | 会话绑定了 `host_id` | `chat_scope_host` |
| **OpenClaw / 集成 API**（`integration`）、**毛竹 内置 MCP 编排**（`mcp_orchestrate` / `mcp_runtime`） | 集成专用 scope | `chat_scope_integration` |
| **触发任务 / 定时任务** 后台 AI | `scope=task` | **均不加载** |

> 本机管理 AI 虽为 `local` scope，但无 `host_id` 时仍走 **网页全局** 开关，而非主机开关。

---

## 21. 用户 Agent Skills（/user-skills）

每用户在 `web/fs/<username>/skills/<name>/SKILL.md` 维护 **Cursor Agent Skills** 风格能力扩展（YAML frontmatter + Markdown）。**默认关闭**：须管理员在 **用户管理** 为对应用户开启 `skills_enabled` 后，该用户方可使用本 API 与网页「Skills」菜单（**含管理员账号本身**）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/user-skills/template` | 默认 SKILL.md 模板（query：`name?`、`description?`） |
| GET | `/user-skills/export` | 导出 JSON 包（含 SKILL.md 与附属文件、场景开关） |
| POST | `/user-skills/import` | 导入 JSON 包；body：`data`、`overwrite?` |
| GET | `/user-skills/status` | 当前用户是否已开启 Skills（`skills_enabled`、`can_use`） |
| GET | `/user-skills` | 列出 Skill 元数据（不含正文时可另 GET 详情） |
| POST | `/user-skills/scan` | 扫描磁盘 `skills/` 目录，同步 `SKILL.md` 到库 |
| POST | `/user-skills` | 新建；body：`name`、`display_name?`、`description?`、`content?`、场景开关等 |
| GET | `/user-skills/{id}` | 详情（含 `content` 正文） |
| PUT | `/user-skills/{id}` | 更新元数据或正文 |
| DELETE | `/user-skills/{id}` | 删除；query `remove_files=true` 时同时删磁盘目录 |

场景开关含义同 §20（`chat_enabled` + `chat_scope_web/host/integration`）。**渐进式披露**（默认，`EDGEOPS_USER_SKILLS_PROGRESSIVE_DISCLOSURE=true`）：system 仅注入各 Skill 的 `name`+`description` 目录；AI 按需 `get_user_skill` / `read_user_skill_file`；`always-apply: true` 或 `disable-model-invocation: false` 时内联正文。

AI 工具：`list_user_skills`、`get_user_skill`、`read_user_skill_file`、`save_user_skill`、`delete_user_skill`、`scan_user_skills`、`export_user_skills_config`、`import_user_skills_config`（须 `skills_enabled`）。

相关环境变量：`EDGEOPS_USER_SKILLS_BODY_MAX_CHARS`、`EDGEOPS_USER_SKILLS_TOTAL_MAX_CHARS`、`EDGEOPS_USER_SKILLS_DESC_MAX_CHARS`、`EDGEOPS_USER_SKILLS_RESOURCE_MAX_CHARS`、`EDGEOPS_USER_SKILLS_PROGRESSIVE_DISCLOSURE`。

与 **§12 GET /skills**（全局 `skills` **数据库表**，只读模板）及 **§12 GET /ai/skills**（内置 TOOLS 摘要）不同：本节为**每用户独立**的 Agent Skills 文件 + `user_skills` 表。

---

## 错误与状态码

- **401**：未登录或 Token 无效/过期；body 中 `detail` 为说明。  
- **403**：无权限（如非管理员访问管理接口）。  
- **404**：资源不存在。  
- **500**：服务器错误；`detail` 为错误信息。  

错误响应体示例：`{ "detail": "字符串或数组" }`。
