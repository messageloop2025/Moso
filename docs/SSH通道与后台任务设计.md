# SSH 通道与后台任务设计

## 1. 概述

- **SSH Channel**：带 TTY 的、供 AI 全权控制的交互式 SSH 执行通道，支持密码输入、Ctrl+C、按行缓冲与按行引用；以**用户会话**或**后台任务**为边界，由 SSH Channel 管理器统一管理。
- **触发任务**：由定时任务完成/失败或定时任务主动调用触发的自动化 AI 任务；有任务 ID、任务名、介绍信息、触发条件；执行历史可查。
- **定时任务**：按 crontab 思想配置周期/时间，到点启动后台 AI Agent，可借助 SSH Channel 与本地脚本执行；执行历史类似会话保存。
- **后台任务**：与 Web 界面任务互不干扰，以**用户**为边界隔离；以**任务 ID** 为边界，不同任务之间不可见对方的 SSH Channel。定时任务与触发任务均产生后台任务。

---

## 2. SSH Channel（TTY 执行通道）

### 2.1 能力要求

- 完全由 AI 控制、支持交互（如 sudo 密码、vi）、支持 Ctrl+C 等控制字符。
- **sudo 密码**：出现 `[sudo] password for` 等提示时，**优先** **`send_service_password`**（target=`ssh_channel`）；无凭证且用户/知识库有密码时 read 确认后可直接 send。见 `web/aihelp/service-credentials.md`。
- 具有“控制台效果”的 TTY 通道；与现有 **Web SSH 控制台**不同，本通道**以后台 PTY 为主**，AI 经工具链直接读写；可选 **WebSocket 挂接**（断线后可重连同一 Channel）。Web **「SSH通道管理」** Tab 对该通道**只读监视**，不向 stdin 发送（见 §7.4）。
- 以**用户会话**或**后台任务**为边界：
  - **Web 会话**：本机管理 / AI 助手 / 主机详情 的 AI 各自可创建仅属于当前会话的 Channel。
  - **后台任务**：无会话 ID，有 **task_id**；同一 task_id 可创建多个 Channel 连多台主机，不同 task_id 之间 Channel 不可见。
- 每个 Channel 可指定：
  - **输入超时**、**输出超时**、**默认空闲关断时间**（如 5 分钟）；输入或输出在指定时间内无数据可自动关闭，避免 AI 中断后通道长期占用。
- AI 创建 Channel 时可指定：输入超时、输出超时、默认超时（关断时间）。

### 2.2 行缓冲与行号

- 输出按**行**缓存；**硬换行**与**软换行**均占行号（软换行：单行内容超过标准行宽时自动折行，每段仍为一逻辑行，便于 AI 按行引用）。
- **标准行长度**：可配置（如 120 字符），单行内容不超过该长度；超出部分做软换行，AI 能识别软换行（例如通过行元数据或前缀标记）。
- 缓冲以**行**为单位，容量可配置（默认 1000 行），超过时 FIFO 丢弃最老行。
- **行号**：隐含、连续；提供**当前最新行号**、**最老行号**；支持：
  - 按行号读：第 N 行到第 M 行；
  - 读倒数 N 行；
  - 自上次读取以来的全部新内容；
  - 查询是否有新输出（是否有新增行）。
- **未完成行（pending_partial）**：尚未以 `\n`/`\r` 结束的当前片段（如 `password:` 提示）单独暴露；**tail_text** 为最近 N 行与 pending 的合成视图。`has_new` 在 pending 非空时也为 true。REST `/lines`、`/has-new` 与 AI `ssh_channel_read_lines` / `ssh_channel_has_new` 均返回上述字段。

### 2.3 Channel 基本信息（AI 可查）

- 主机 IP、主机名、主机类型、版本信息、BASH 信息、主机是否存在（可探测）。
- Channel 列表：当前会话/任务下自己创建的所有 Channel 及上述基本信息。

### 2.4 操作

- 创建 Channel（指定 host_id、可选超时参数）。
- 向 Channel 发送内容（含控制字符，如 Ctrl+C）。
- 读取内容：按行号范围、倒数 N 行、自上次以来的全部。
- 读取特定长度（按字符数）。
- 关闭 Channel。
- WebSocket：后端暴露 WebSocket 端点，前端或其它服务可挂到某 Channel 上；断线后可凭 channel_id 重连同一 Channel。

### 2.5 数据模型（概要）

- **ssh_channels**：id, owner_type('session'|'task'), owner_id(session_id 或 task_id), user_id, host_id, 超时参数, 创建/更新时间, 状态。
- **ssh_channel_lines**：channel_id, line_index(逻辑行号), content, is_soft_wrap, created_at；或按 channel 分表/分区。
- 行缓冲在内存，可配置持久化到 DB 或仅内存（设计阶段建议先内存 + 行数上限）。

---

## 3. 触发任务

### 3.1 表结构（概要）

- **triggered_tasks**：id, user_id, name, content(提示词/任务内容), intro(介绍信息，供其它 AI 决策是否调用), created_at, updated_at, last_run_at, last_run_status, is_running, trigger_conditions(JSON 或单独表)。
- **triggered_task_runs**：id, task_id, triggered_at, triggered_by(scheduled_task_id 或来源), status, instruction(定时任务 AI 传入的指令), log/output 摘要, created_at。
- **triggered_task_expose**：任务对外暴露的接口列表（供定时任务发现）：task_id, expose_name/code, 描述等。

### 3.2 触发方式（设计）

1. **定时任务完成时触发**：定时任务跑完后调用触发接口，传入状态=成功、任务名/ID、指令。
2. **定时任务失败时触发**：同上，状态=失败。
3. **定时任务主动呼叫触发**：定时任务 AI 决策后调用“触发任务”接口，传入目标任务名/ID、指令。

触发接口统一：**用户名**（必须同用户）、**定时任务名/ID**、**状态**、**定时任务 AI 提供的 instruction**。

### 3.3 界面与 API

- 菜单：**触发任务**；列表：任务 ID、任务名、任务内容摘要、介绍信息、创建时间、最后运行时间、最后运行状态、是否正在运行、触发条件。
- 历史：按任务 ID、任务名、执行时间、状态查看执行记录。
- API：CRUD 触发任务、执行触发、查询可被定时任务发现的触发任务列表（用于 AI 决策）。

---

## 4. 定时任务

### 4.1 表结构（概要）

- **scheduled_tasks**：id, user_id, name, content(提示词), created_at, updated_at, last_run_at, last_run_status, is_running, cron_expr(或 next_run_at + interval)。
- **scheduled_task_runs**：id, task_id, run_at, status, session_id(类似 AI 会话的执行历史 ID), 摘要/日志, created_at。
- **scheduled_task_history**：与 run 关联的“会话式”执行历史（消息/步骤），供 AI 查询“当前次执行的会话历史”。

### 4.2 执行模型

- 到点启动**后台 AI Agent**，装载任务 content 作为目标提示词；执行方式：**SSH Channel** + 本地 python/bash 等辅助。
- 执行过程产生类似“会话”的历史，存入 scheduled_task_history（或复用 ai_chat_messages 形态，用 run_id 区分）。
- AI 可查询：同一用户、同一任务、**当前次执行**的会话历史。

### 4.3 定时条件

- 参考 crontab：分、时、日、月、周；或简单周期（每 N 分钟/小时）。

### 4.4 界面与 API

- 菜单：**定时任务**；列表：任务 ID、任务内容摘要、创建时间、最后运行时间、最后运行状态、是否正在运行、定时条件。
- 历史：按任务 ID、任务名、执行时间、状态查看。
- API：CRUD 定时任务、查询本用户可用的**触发任务**列表（供定时任务 AI 决策是否调用）。

---

## 5. 后台任务与日志

- 定时任务、触发任务执行时均写**日志**（operation_logs 或专用 task_logs），便于排查。
- 后台任务与 Web 界面任务**互不干扰**；以 **user_id** 隔离；以 **task_id** 隔离不同任务的 SSH Channel。

---

## 6. AI Skills（工具）概要

### 6.1 SSH Channel 相关

完整参数以《[Skills文档](Skills文档.md)》§16 为准。

- **ssh_channel_create**：创建 TTY SSH 通道；参数 host_id, input_timeout?, output_timeout?, idle_close_seconds?；返回 channel_id 及基本信息（主机 IP、主机名、类型、版本、BASH、是否存在）。
- **ssh_channel_list**：列出当前会话/任务下自己创建的通道及基本信息。
- **ssh_channel_info**：获取通道详情（主机信息、当前行号范围、缓冲行数等）；参数 channel_id。
- **ssh_channel_get_status**：轻量查询通/断（connected）与闲/忙（buffer_idle）；**仅 connected=false 时禁止 ssh_channel_send**。
- **ssh_channel_send**：向通道发送内容（支持控制字符）；参数 channel_id, content。
- **ssh_channel_read_lines**：按行读；参数 channel_id, from_line?, to_line?, last_n?, since_line?；可选 wait_seconds / until_contains；返回行列表及 tail_text / pending_partial。
- **ssh_channel_read_length**：读指定字符数；参数 channel_id, max_chars?。
- **ssh_channel_has_new**：查询是否有新输出；参数 channel_id；返回 has_new, latest_line_no。
- **ssh_channel_close** / **ssh_channel_close_batch**：关闭单通道或按 owner/session 批量关闭。
- **ssh_channel_dump_output**：导出通道缓冲到 spill。

### 6.2 触发任务相关（供定时任务 AI 等调用）

- **triggered_task_list_exposed**：列出本用户可被定时任务发现的触发任务（名称、介绍、接口 code）。
- **triggered_task_trigger**：触发指定触发任务；参数 task_id/name, instruction, caller_task_id?, caller_status?。

### 6.3 定时任务相关（供定时任务 AI 内部使用）

- **scheduled_task_list**：列出本用户定时任务（当前任务上下文可用）。
- **scheduled_task_current_run_history**：查询当前次执行的会话历史；参数 task_id, run_id?。

---

## 7. 前后端 API 概要

### 7.1 SSH Channel（后端 REST + WS）

- `POST /api/ssh-channel`：创建（body: host_id, owner_type, owner_id, session_id?, 超时参数, idle_close_sec；Web 默认 idle 1800s，集成 session 默认 3600s）。
- `GET /api/ssh-channel`：列表（query: owner_type, owner_id, **all_open**）。
- `GET /api/ssh-channel/{id}`：详情与行号范围；`check_alive?`。
- `POST /api/ssh-channel/{id}/send`：发送内容。
- `GET /api/ssh-channel/{id}/lines`：读行；响应含 **pending_partial**、**tail_text**。
- `GET /api/ssh-channel/{id}/read`：按字符读。
- `GET /api/ssh-channel/{id}/has-new`：是否有新输出（含 pending）。
- `POST /api/ssh-channel/{id}/dump`：导出 spill。
- `DELETE /api/ssh-channel/{id}`：关闭；`POST /api/ssh-channel/close-batch`：批量关。
- `WS /api/ssh-channel/{id}/ws?token=`：推送 `ready` / `lines` / `partial` / `closed` / `error`；可重连。详见《API 文档》§17。

### 7.4 Web UI「SSH通道管理」（只读）

- **位置**：AI 助手页、主机详情 AI 页 → 左侧终端区 Tab（控制台 / 文件系统 / **SSH通道管理** / Log）。
- **列表**：`GET /api/ssh-channel?all_open=true`；主机页按 host 过滤。
- **输出**：xterm 只读；「刷新输出」≈ `GET /lines?last_n=80`；「实时监视」≈ WS 订阅（UI **不 send**）。
- **同步**：AI 聊天 `ssh_channel_create` / `close` / `close_batch` 完成后前端自动刷新；用户再次打开 Tab 或整页刷新后打开 Tab 时自动拉列表。
- **帮助**：`web/aihelp/ssh-channel.md`。

### 7.2 触发任务

- `GET/POST/PATCH/DELETE /api/triggered-tasks`：CRUD。
- `GET /api/triggered-tasks/exposed`：可被发现的列表。
- `POST /api/triggered-tasks/trigger`：执行触发（body: task_id, instruction, caller_task_id, caller_status）。
- `GET /api/triggered-tasks/{id}/runs`：执行历史。

### 7.3 定时任务

- `GET/POST/PATCH/DELETE /api/scheduled-tasks`：CRUD。
- `GET /api/scheduled-tasks/{id}/runs`：执行历史。
- `GET /api/scheduled-tasks/triggered-list`：本用户可用的触发任务列表（供 AI）。

---

## 8. 实现阶段建议

1. **Phase 1**：数据库表（ssh_channels、channel 行缓冲策略）、触发任务表、定时任务表、执行历史表；API 骨架与权限（user_id/task_id 隔离）。
2. **Phase 2**：SSH Channel 管理器（TTY、Paramiko Channel、行缓冲、超时与空闲关闭）；AI Skills 实现 Channel 创建/发送/读行/关闭。
3. **Phase 3**：触发任务执行引擎（调用 AI + SSH Channel）；触发接口与“定时任务完成/失败/主动呼叫”的对接。
4. **Phase 4**：定时任务调度器（crontab 解析、到点启动 Agent）；执行历史与会话式存储。
5. **Phase 5**：触发/定时任务 Web 列表与历史、Channel WebSocket 重连、**AI 页「SSH通道管理」只读 Tab**（xterm + 实时监视 + 与聊天工具事件同步）。侧栏独立「SSH 通道」菜单项仍为可选扩展。

本文档仅描述设计与接口；具体字段以迁移脚本与 API 实现为准。
