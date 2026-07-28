# SSH 控制台与终端

本系统中「控制台」与「终端」同义，均指对**单台主机**建立的 **Web SSH 连接**及其交互界面。

> **与 SSH 通道（ssh_channel）不同**：AI 还可创建**后台 PTY 通道**（用户说「**打开通道 / SSH 通道**」时用 `ssh_channel_*`）。用户说「**打开终端 / 打开某主机**」且未提「通道」时，指的是本页 **Web 控制台**（`connect_terminal` / `create_console`）。详见 [ssh-channel.md](ssh-channel.md)。

---

## 打开与关闭控制台

- **打开**：在主机管理或 AI 助手页，选择主机后「连接」；或由 AI 调用 **connect_terminal(host_id)**（首连/复用已有）或 **create_console(host_id)**（强制再开一个并行 tab）建立 SSH 连接并显示终端。用户也可点击「+ 新建控制台」再选主机连接。
- **connect_terminal vs create_console**：
  - **connect_terminal**：该 host **还没有** AI 控制台时用来**首次连接**（会预分配 slot 并等待就绪）；已有控制台时**切到空闲 slot**，**不会**再开一个。
  - **create_console**：同一 host **再开一个**并行 tab（例如现有终端在跑长任务、或用户说「再开一个终端」）。
  - connect_terminal 后若读 buffer 暂不可用，应 **list_terminals + get_terminal_buffer(next_poll_in_seconds=2～5)** 重试，**不要**立刻 create_console。
- **关闭**：用户可点击终端标签旁的 **×** 关闭该控制台；AI 可调用 **close_console(slot)** 关闭由 AI 创建的控制台（仅可关闭 created_by 为 ai 的）。

---

## AI 与控制台配合

- **send_to_terminal**：向某个 **AI 创建** 的控制台槽位发送文本（命令、TUI 控制键等）。支持完整占位符语法：`<Ctrl+A>`…`<Ctrl+Z>`、`<Alt+M>`、`<Shift+Tab>`、`<Enter>`、方向键、`<F1>`…`<F12>` 等（详见下方「控制键占位符」）。
- **get_terminal_status**：轻量查询控制台 **通/断**（`connected`）与 **闲/忙**（`buffer_idle` / `session_state`）。`connected=false` 时**禁止** `send_to_terminal`；`session_state=busy` 时勿发新 shell 命令，应 `get_terminal_buffer` 轮询。
- **get_terminal_buffer**：获取 **AI 创建** 控制台的最近输出（**末尾**即最新状态），并附带与 `get_terminal_status` 相同的状态字段。默认 `tail_only=true`：超长时仅返回最后 40 行；`tail_only=false` 为前 2+后 33 行；`full_output=true` 为全量。**仅供 AI 内部使用**。
- **条件返回（until_contains）**：可传字面子串 `until_contains`；工具在超时内轮询，**命中或超时即返回**（`until_wait_reason`）。超时取自 `next_poll_in_seconds`（默认 30、最长 3600）。适合脚本 `echo` 标记串、等 `password:`；带此参数时**不再**额外 batch 末 sleep。**无** CoT 橙色倒计时条，但仍可被 runtime **唤醒/停止**打断。
- **长任务轮询等待（next_poll_in_seconds）**：未使用 `until_contains` 时，若传入 `next_poll_in_seconds`（1～3600），或刚 `send_to_terminal` 发了 apt/make/编译等长命令、buffer 末尾仍有进度，服务端会在**该轮工具全部执行完后**倒计时 N 秒，再进入下一轮 AI 推理。**Web 控制台路径**：`get_terminal_buffer` / `send_to_terminal` / `ssh_execute` detach。**ssh_channel** 另有 `wait_seconds` / `until_contains`（见 [ssh-channel.md](ssh-channel.md)）。
- **CoT 上的唤醒与停止**：倒计时期间，聊天右侧「思考与计划」里**对应工具步骤**（多为 `get_terminal_buffer`）会显示剩余秒数，并提供：
  - **唤醒**：跳过剩余等待，AI 立刻进入下一轮（例如马上再读 buffer）；**不中断**整个任务。
  - **停止**：终止当前 AI 执行轮次（与输入区「停止」相同）。
  底部运行中控制条的「暂停 / 补充 / 停止」仍可用于整轮任务；唤醒仅针对该次终端轮询等待。
- **sudo 与密码**：sudo **不总是**要密码（常见 NOPASSWD）。流程：先 send 仅 sudo → **必须** read 尾部 → **仅当**出现 `[sudo] password for` / `Password:` 等提示时才 `send_service_password`（本机可用 `use_host_login=true`）。无提示且命令已继续 → 成功，**勿**注入。服务端默认也会拒绝「无提示就注入」。**禁止** sudo 后立刻跟发密码。详见 [service-credentials.md](service-credentials.md)。

### 终端状态（通/断 · 闲/忙）

| 字段 | 含义 |
|------|------|
| `connected` | **通/断**：PTY/SSH 通道是否仍存活。`false` 表示已断开，一般**不可恢复**，勿再 `send_to_terminal`。 |
| `session_state` | `idle`（已回提示符）、`busy`（命令仍在跑）、`waiting_password`、`waiting_input`、`pending`（连接中）、`disconnected`、`missing` |
| `buffer_idle` | **闲/忙**：`true` 表示 buffer 末尾像 shell 提示符，可发**新**命令 |
| `can_send_command` | `connected` 且 `buffer_idle` 且无密码/交互等待时为 `true` |
| `can_read_buffer` | 会话记录仍在内存时可读最后输出（断开瞬间可能仍有缓冲） |

PTY 没有标准「就绪」协议，服务端通过输出末尾启发式判断（提示符、`sudo` 密码、进度条、`--More--` 等）。操作前先 `list_terminals` 或 `get_terminal_status`。

### 操作规则

- AI **只能操作 AI 创建的 SSH 控制台**。
- AI **不能读取、切换、关闭、写入**用户创建的控制台。
- 用户创建的控制台主要供用户手工操作，避免被 AI 打断。
- 若当前聊天区域没有可用的 AI 控制台，AI 需要先创建控制台后才能做连续终端操作。

### 花屏 / 大片空白 / 旧内容看不见

多为 **浏览器端 xterm 画面状态损坏**（WebSocket 丢包或 ANSI 转义序列被截断），**不是 SSH 会话真的丢了**：新命令往往仍能执行并出字。

- 控制台底部点 **「修复显示」**：从服务端缓冲重建画面，**不断开**连接。
- 仍异常再断开重连或新建控制台。
- 控制台若出现大量 `/api/terminal/...` 的 **502**，多半是网关/后端瞬时过载，与花屏常同时出现；待服务恢复后用「修复显示」即可。

---

## 多控制台

- 用户可同时打开多个主机控制台（多槽位）。
- AI 可同时创建多个 **AI 控制台**，用于多任务并行，如一边查看日志、一边执行部署、一边做巡检。
- 用户创建的控制台与 AI 创建的控制台可共存，但职责建议分开：用户终端用于人工接管，AI 终端用于自动化执行。
- 关闭 AI 控制台可通过 **close_console(slot)** 或界面操作；用户终端通常由用户自己关闭。

---

## 自动切换控制台

- AI 向某个 **AI 控制台** 发送输入时，前端可**自动切换到该控制台**，方便用户看到 AI 操作。
- 若用户手动切到 **用户创建** 的控制台，自动切换会暂停，避免影响用户手工操作。
- 当用户再次切回 **AI 创建** 的控制台后，自动切换恢复。
- 界面提供「**不自动切换**」选项，勾选后不再随 AI 输入自动切台。

- **sudo 与密码**：sudo **不总是**要密码（常见 NOPASSWD）。流程：先 send 仅 sudo → **必须** read 尾部 → **仅当**出现 password 提示才注入。**优先** `send_service_password`（凭证库或 `use_host_login`）；无凭证且用户已提供密码、或密码在主机知识/记忆中时，read 确认提示后可用 send 发「密码+<Enter>」。详见 [service-credentials.md](service-credentials.md)。

---

## 控制键占位符（AI send 语法）

`send_to_terminal` / `ssh_channel_send` 的 text/content 支持 `<…>` 占位符（由 `services/terminal_input.py` 展开）：

| 类别 | 示例 |
|------|------|
| Ctrl 全字母 | `<Ctrl+C>` 中断、`<Ctrl+X>` nano 退出、`<Ctrl+O>` nano 保存 |
| Alt / 组合 | `<Alt+M>`、`<Ctrl+Alt+X>` |
| 命名键 | `<Enter>` `<Tab>` `<Esc>` `<Up>` `<Down>` `<Home>` `<End>` `<Delete>` |
| 功能键 | `<F1>`…`<F12>`（可叠加 Ctrl/Alt/Shift） |
| TUI 组合 | vi 退出 `<Esc>:wq<Enter>`；nano 保存退出 `<Ctrl+O><Enter><Ctrl+X>` |

纯控制键/方向键不会自动补换行；普通命令末尾无换行时会自动补 `\n`。

---

## 推荐用法

- 连续执行多步命令时，优先让 AI 创建独立 AI 控制台。
- 用户想手工接管时，优先新建用户控制台，不要直接和 AI 共用同一个 AI 终端。
- 长任务可拆成多个 AI 控制台并行执行，以减少等待时间。
- 高风险操作前，先让 AI 说明将在哪个控制台做什么，再决定是否放行。

---

## SSH 通道管理（只读监视）

AI 助手页与主机 AI 页左侧均有 **「SSH通道管理」** Tab（在「文件系统」与「Log」之间）：

- 列出 AI 创建的 **open** SSH 通道，每个通道一个子 Tab。
- **刷新** / **刷新输出** / **实时监视**（监视中显示 **关闭监视**）仅用于查看输出，**不向通道发送命令**。
- AI 在聊天中创建或关闭通道时，列表会自动更新；页面刷新后再次打开该 Tab 会自动与服务端同步。

完整说明见 [ssh-channel.md](ssh-channel.md)。
