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

- **send_to_terminal**：向某个 **AI 创建** 的控制台槽位发送文本（命令、控制键等）。支持 `<Ctrl+C>`、`<Ctrl+Z>`、`<Ctrl+D>`、`<Ctrl+L>` 等占位符。
- **get_terminal_status**：轻量查询控制台 **通/断**（`connected`）与 **闲/忙**（`buffer_idle` / `session_state`）。`connected=false` 时**禁止** `send_to_terminal`；`session_state=busy` 时勿发新 shell 命令，应 `get_terminal_buffer` 轮询。
- **get_terminal_buffer**：获取 **AI 创建** 控制台的最近输出（**末尾**即最新状态），并附带与 `get_terminal_status` 相同的状态字段。默认 `tail_only=true`：超长时仅返回最后 40 行；`tail_only=false` 为前 2+后 33 行；`full_output=true` 为全量。**仅供 AI 内部使用**。
- **sudo 与密码**：不少环境为免密 sudo（NOPASSWD）。AI 应先 `send_to_terminal` 执行 sudo 命令，再 **必须** `get_terminal_buffer` 查看输出；**仅当**末尾出现 `[sudo] password for`、`Password:` 等提示时才注入密码。**凭证库已启用**（`credentials_vault_enabled=true`）时调用 **`send_service_password`**（target=terminal）；未启用时可从主机知识取密码，但仍须 **另一次** 发送且 **禁止** 在 sudo 同次调用里带密码。**禁止**在 sudo 命令后立即跟发密码，**禁止**未看到提示就默认需要密码。详见 [service-credentials.md](service-credentials.md)。

### 终端状态（通/断 · 闲/忙）

| 字段 | 含义 |
|------|------|
| `connected` | **通/断**：PTY/SSH 通道是否仍存活。`false` 表示已断开，一般**不可恢复**，勿再 `send_to_terminal`。 |
| `session_state` | `idle`（已回提示符）、`busy`（命令仍在跑）、`waiting_password`、`waiting_input`、`pending`（连接中）、`disconnected`、`missing` |
| `buffer_idle` | **闲/忙**：`true` 表示 buffer 末尾像 shell 提示符，可发**新**命令 |
| `can_send_command` | `connected` 且 `buffer_idle` 且无密码/交互等待时为 `true` |
| `can_read_buffer` | 会话记录仍在内存时可读最后输出（断开瞬间可能仍有缓冲） |

PTY 没有标准「就绪」协议，服务端通过输出末尾启发式判断（提示符、`sudo` 密码、进度条、`--More--` 等）。操作前先 `list_terminals` 或 `get_terminal_status`。

### 关键规则

- AI **只能操作 AI 创建的 SSH 控制台**。
- AI **不能读取、切换、关闭、写入**用户创建的控制台。
- 用户创建的控制台主要供用户手工操作，避免被 AI 打断。
- 若当前聊天区域没有可用的 AI 控制台，AI 需要先创建控制台后才能做连续终端操作。

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
