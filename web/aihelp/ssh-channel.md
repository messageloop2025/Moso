# SSH 通道（ssh_channel）与「SSH通道管理」

**SSH 通道**是带 PTY 的后台交互式 SSH 会话，供 AI 通过工具链 **`ssh_channel_*`** 创建与控制。与 **Web SSH 控制台**（用户可见、可输入）不同：通道由 AI 通过工具链控制，用户不必打开「控制台」tab 也能跑安装、编译、sudo 等多步交互。

> **用户怎么说，AI 怎么做**  
> - 「**打开终端**」「**打开某主机**」「**连上 XX**」（未提「通道」）→ **Web 控制台**：`connect_terminal` / `create_console`  
> - 「**打开通道**」「**打开 SSH 通道**」「**建通道**」→ **`ssh_channel_create`** 等，**不是** Web 控制台  

界面侧在 **AI 助手页**、**主机详情 AI 页** 左侧终端区提供 **「SSH通道管理」** Tab，用于**只读监视** AI 已创建的 open 通道输出，**不能**在此 Tab 里向通道发送命令（不影响 AI 后台 API）。

---

## 与 Web 控制台的区别

| 维度 | Web SSH 控制台 | SSH 通道（ssh_channel） |
|------|----------------|-------------------------|
| 创建方式 | 用户「连接」或 AI `create_console` | AI `ssh_channel_create`（或 REST POST） |
| 用户输入 | 控制台可打字 | 「SSH通道管理」Tab **只读**；输入仅 AI 工具 `ssh_channel_send` |
| 典型场景 | 用户要**边看边操作** | 多步安装/编译/sudo/嵌套 SSH，用户不必盯着界面 |
| AI 读输出 | `get_terminal_buffer`（slot 0 等 AI 控制台） | `ssh_channel_read_lines` / `has_new` / `read_length` |
| 空闲关断 | 控制台会话策略 | Web 会话默认 **1800s** 无读写自动关；集成 `session_id` 默认 **3600s** |

二者可并存：例如 AI 用 channel 跑编译，控制台给用户看另一条 tail 日志。

---

## AI 工具链（概要）

标准流程：`ssh_channel_create(host_id)` → 循环 `ssh_channel_send` + `ssh_channel_read_lines` / `ssh_channel_has_new` → 结束 `ssh_channel_close`。

- **PTY**：外层 channel 已分配真实 TTY；channel 内再 SSH 其它主机时，交互登录建议 **`ssh -tt user@host`**。
- **无换行输出**：`password:`、`Password:` 等提示**常无 `\n`**，会出现在 **`tail_text` / `pending_partial`**，不要因 `lines` 为空就认为无输出。
- **禁止**用空回车探测密码；检测到密码提示后用 **`send_service_password`**（`target=ssh_channel`，`channel_id=…`），勿 `ssh_channel_send` 发明文。详见 [service-credentials.md](service-credentials.md)。
- **列表与关闭**：`ssh_channel_list(all_open=true)`；`ssh_channel_get_status` 轻量查 **通/断/闲/忙**；`ssh_channel_close` / `ssh_channel_close_batch`。

### 通/断与闲/忙（与 Web 控制台一致）

| 字段 | 含义 |
|------|------|
| `connected` | **通/断**：DB 为 open 且内存中仍有 TTY 时为 true；false 时**禁止** `ssh_channel_send` |
| `memory_connected` | 本 worker 进程内存中是否仍持有连接 |
| `db_status` | 库中状态：open / closed / failed |
| `buffer_idle` | **闲/忙**：true 表示输出末尾像 shell 提示符，可发**新**命令 |
| `session_state` | idle / busy / waiting_password / waiting_input / disconnected 等 |
| `can_send_command` | connected 且 idle 且无密码/交互等待时为 true（**仅供参考**，busy 不拦截 send） |
| `last_line` | 缓冲末尾一行（判 busy 时优先看 tail_text / pending_partial） |

发命令前可先 `ssh_channel_list` 或 `ssh_channel_get_status`；`read_lines` 也会附带上述字段。

集成模式无 Web Tab 时更应优先 channel，而非假装能用界面终端。

### 与 Web 控制台「轮询等待 / 唤醒」的区别

- **Web 控制台**在长任务时，`get_terminal_buffer(next_poll_in_seconds=N)` 可能在工具批次结束后由服务端 **sleep N 秒**；浏览器 CoT 对应步骤可 **唤醒 / 停止**（见 [terminal.md](terminal.md)）。
- **ssh_channel** 是 AI 连续 `send` + `read_lines/has_new`，**没有**上述 batch 末倒计时，CoT 上**不会出现**针对 channel 的唤醒条。觉得 AI 读 channel 太慢时，应 **停止整轮任务** 或 **补充**说明，而不是找「唤醒 get_terminal_buffer」。

---

## 「SSH通道管理」Tab（Web 界面）

**位置**：AI 助手页 / 主机详情 AI 页 → 左侧终端区 → **控制台 · 文件系统 · SSH通道管理 · Log**。

### 列表与 Tab

- 顶部为每个 **open** 通道一个子 Tab（`#通道ID · 主机名`）。
- 右侧 **「刷新」**：`GET /api/ssh-channel?all_open=true` 重新拉列表。
- **主机 AI 页**：仅显示当前主机的 open 通道；**全局 AI 页**：显示当前用户全部 open 通道。

### 输出展示

- 使用 **xterm.js** 只读终端（`disableStdin: true`），支持 ANSI 颜色。
- **「刷新输出」**：REST 读取最近约 **80 行**（含 `tail_text` / `pending_partial`），非实时。
- **「实时监视」**：WebSocket `WS /api/ssh-channel/{id}/ws?token=…`，仅**接收**服务端推送的新行/未完成行；按钮变为 **「关闭监视」**；WS 断开或通道关闭后恢复为「实时监视」。
- 实时监视与手工刷新**互斥**：监视中手工刷新会提示先关闭监视。

### 与 AI 聊天自动同步

- AI 在聊天中 **`ssh_channel_create` 成功** → 列表自动刷新，并选中新建通道、刷新输出。
- AI **`ssh_channel_close` / `ssh_channel_close_batch`** → 列表自动更新，已关通道 Tab 消失。
- **整页刷新后**再打开「SSH通道管理」Tab → 自动拉取最新 open 列表并刷新当前通道输出，与服务端状态对齐。

以上 Web 行为**仅只读 API**，不改变 AI 工具链与 `ssh_channel_send` 等后台逻辑。

---

## 常见问题

- **列表为空**：当前无 open 通道；让 AI 创建或点「刷新」。AI 关通道后 Tab 会消失属正常。
- **看不到 password 提示**：读 `read_lines` 的 **`tail_text`**，或开「实时监视」；不要只看 `lines` 数组。
- **监视按钮仍显示「实时监视」**：需 WS 已连接或已点「实时监视」；断线后会恢复原文案，可再点一次重连。
- **能否在 Tab 里帮 AI 输入**：不能；请用 AI 对话或 Web **控制台**（若任务适合用户可见终端）。

设计细节与 REST/WS 字段见项目 `docs/SSH通道与后台任务设计.md`、`docs/API文档.md` §17。
