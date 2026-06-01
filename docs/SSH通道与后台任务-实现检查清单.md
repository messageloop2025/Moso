# SSH 通道与后台任务 — 实现检查清单

依据需求逐项对照当前代码与设计文档的实现情况。

---

## 一、SSH Channel（带 TTY 的命令执行通道）

| 需求项 | 状态 | 说明 |
|--------|------|------|
| 带 TTY、完全由 AI 控制，支持交互（密码、Ctrl+C 等）、控制台效果 | ✅ 已实现 | services/ssh_channel_manager.py：Paramiko PTY + 行缓冲 |
| 以用户会话为边界；本机管理/AI 助手/主机详情的 AI 可创建仅属当前会话的通道 | ✅ 已实现 | owner_type=session + owner_id=session_id；AI Skills 用 terminal_scope_id 绑定会话 |
| 以任务为边界；不同 task_id 不能看到别的任务的 SSH Channel | ✅ 已实现 | owner_type=task + owner_id=task_id；列表按 owner 过滤 |
| 可指定输入超时、输出超时、空闲关断时间（默认 5 分钟） | ✅ 已实现 | 创建 API/Skill 支持 input_timeout_sec、output_timeout_sec、idle_close_sec（默认 300） |
| 输入/输出超时或无内容时自动关闭通道 | ✅ 已实现 | Channel 管理器看门狗线程检测空闲/输入/输出超时并关闭 |
| AI 获得通道清单及基本信息（主机 IP、主机名、类型、版本、BASH、主机是否存在） | ✅ 已实现 | 列表/详情同上；GET /{id}/host-alive 与 GET 详情 ?check_alive=1 提供主机存活探测；GET /api/hosts/{id}/alive 独立主机探测 |
| 向通道发送内容（含控制字符） | ✅ 已实现 | manager.send + expand_control_keys；支持 Ctrl+C 等 |
| 读取通道内容：按行读 N～M 行、倒数 N 行、自上次以来全部 | ✅ 已实现 | GET /lines、ssh_channel_read_lines 从行缓冲返回 |
| 读取特定内容长度（按字符数） | ✅ 已实现 | GET /api/ssh-channel/{id}/read?max_chars=、Skill ssh_channel_read_length |
| 行缓存：默认 1000 行，FIFO 淘汰最老行 | ✅ 已实现 | DEFAULT_MAX_LINES=1000，超出 popleft |
| 每行长度不超过标准长度，超长软换行；AI 能识别软换行 | ✅ 已实现 | DEFAULT_LINE_WIDTH=120，返回行含 is_soft_wrap |
| 行号、最新/最老行号、has_new 查询 | ✅ 已实现 | oldest_line_no/latest_line_no、has_new 由管理器提供 |
| 纯后端、直接与 AI 通信；WebSocket 断线后可重连同一 Channel | ✅ 已实现 | WS /api/ssh-channel/{id}/ws，query 传 token，服务端推送 lines/closed/error，客户端可发输入 |
| 前后端 API 与 AI Skills | ✅ 已实现 | 创建/列表/详情/发送/按行读/按字符读/has_new/关闭 均已接管理器 |

---

## 二、触发任务

| 需求项 | 状态 | 说明 |
|--------|------|------|
| 任务表：任务 ID、任务名、任务内容、介绍信息、创建时间、最后运行时间、最后运行状态、是否在运行、触发条件 | ✅ 已实现 | triggered_tasks 表及 CRUD API 均具备 |
| 界面：任务菜单含上述字段 | ✅ 已实现 | 列表含 ID、任务名、任务内容摘要、介绍、触发条件、创建时间、最后运行、状态、运行中；支持新建/编辑 |
| 触发方式 1：定时任务完成时触发 | ✅ 已实现 | trigger_conditions 为 JSON 且含 on_scheduled_complete: [任务ID] 时，run_scheduled_task 结束后自动触发 |
| 触发方式 2：定时任务失败时触发 | ✅ 已实现 | trigger_conditions 含 on_scheduled_fail: [任务ID] 时，定时任务失败后自动触发 |
| 触发方式 3：定时任务主动呼叫触发 | ✅ 接口已有 | POST /api/triggered-tasks/trigger；AI Skill triggered_task_trigger |
| 触发接口参数：同用户、定时任务名/ID、状态、instruction | ✅ 已实现 | instruction、caller_task_id、caller_task_name、caller_status；同用户由鉴权保证 |
| 触发任务历史：按任务 ID、任务名、执行时间、任务状态查看 | ✅ 已实现 | 单任务 GET /{task_id}/runs；全局 GET /all-runs 支持 task_id、task_name、status、from_time、to_time；前端「全部执行历史」弹窗带筛选 |
| 触发任务需指定一组“被定时任务访问的接口”（暴露接口） | ✅ 已实现 | POST /api/triggered-tasks/{id}/expose、DELETE /api/triggered-tasks/{id}/expose?code=；界面“暴露接口”弹窗可增删 |
| 触发任务执行会话历史（类似定时任务） | ✅ 已实现 | triggered_task_run_messages 表（迁移 003）；run_triggered_task 写入 user/assistant；Skill triggered_task_current_run_history；GET /{task_id}/runs/{run_id}/messages |
| 系统菜单“触发任务”菜单项 | ✅ 已实现 | 侧栏有“触发任务”，路由 /triggered-tasks |

---

## 三、定时任务

| 需求项 | 状态 | 说明 |
|--------|------|------|
| 定时任务表：任务 ID、任务内容、创建时间、最后运行时间、最后运行状态、是否在运行、定时条件 | ✅ 已实现 | scheduled_tasks 含 cron_expr、next_run_at、**enabled**（004）、**notify_email_to**（007，结果邮件用个人 SMTP）；支持启用/停用、删除时清理 runs/messages |
| 定时条件参考 crontab（指定时间、周期） | ✅ 已实现 | croniter 解析 cron_expr，创建/更新时计算 next_run_at |
| 到点启动后台 AI Agent，以任务内容为提示词；借助 SSH Channel + 本地 python/bash | ✅ 已实现 | services/scheduler.py 约每 **30** 秒检查；事务内抢占 `is_running` 避免并发 tick 重复调度；task_runner.run_scheduled_task 跑 Agent |
| 执行历史类似“会话”保存；AI 可查同一用户、同一任务、当前次执行的会话历史 | ✅ 已实现 | scheduled_task_runs + scheduled_task_run_messages；Skill scheduled_task_current_run_history；GET /{task_id}/runs/{run_id}/messages |
| 界面：按任务 ID、任务名、执行时间、任务状态查看历史 | ✅ 已实现 | 单任务 GET /{task_id}/runs；全局 GET /all-runs 支持 task_id、task_name、status、from_time、to_time；前端「全部执行历史」弹窗带筛选 |
| 定时任务 AI 可查询本用户可用的触发任务（供决策是否调用） | ✅ 已实现 | GET /api/scheduled-tasks/triggered-list；Skill triggered_task_list_exposed |
| 系统菜单“定时任务”菜单项 | ✅ 已实现 | 侧栏有“定时任务”，路由 /scheduled-tasks |

---

## 四、后台任务与隔离

| 需求项 | 状态 | 说明 |
|--------|------|------|
| 后台任务与 Web 界面任务互不干扰 | ✅ 设计满足 | 以 owner_type/owner_id 与 user_id 隔离 |
| 以用户为边界隔离 | ✅ 已实现 | 所有 API 按 user_id 过滤 |
| 以任务 ID 为边界，不同任务不可见对方 SSH Channel | ✅ 已实现 | 列表按 owner_id（task_id）过滤 |
| 一个任务可创建多个 SSH Channel 连多台主机 | ✅ 已实现 | 创建时仅校验 host_id 与 owner，无数量限制 |
| 后台任务/定时任务有日志 | ✅ 已实现 | run 结束时写入 log_summary；执行引擎写入 runs 与 messages |

---

## 五、API 与 AI Skills 汇总

| 类别 | 项目 | 状态 |
|------|------|------|
| SSH Channel | POST/GET/DELETE /api/ssh-channel、GET/POST /api/ssh-channel/{id}/... | ✅ 骨架齐全 |
| SSH Channel | 按字符数读 read_length（API + Skill） | ✅ 已实现 |
| SSH Channel | WebSocket 端点 WS /{id}/ws | ✅ 已实现 |
| 触发任务 | CRUD、exposed、trigger、runs、all-runs、GET /{task_id}/runs/{run_id}/messages | ✅ 已实现 |
| 触发任务 | 暴露接口的增删改 API | ✅ 已实现（POST/DELETE expose） |
| 定时任务 | CRUD、runs、all-runs、run-now、triggered-list、GET /{task_id}/runs/{run_id}/messages | ✅ 已实现 |
| AI Skills | ssh_channel_*、triggered_task_list_exposed、triggered_task_trigger、triggered_task_current_run_history、scheduled_task_list、scheduled_task_current_run_history、scheduled_task_run_now | ✅ 已实现 |

---

## 六、触发条件 JSON 说明（定时任务完成/失败时触发）

触发任务的 **触发条件** 字段可存 JSON，用于“定时任务完成时触发”“定时任务失败时触发”：

- **on_scheduled_complete**：数组为定时任务 ID 列表，当这些定时任务**成功完成**时自动触发本触发任务。例：`{"on_scheduled_complete": [1, 2]}`
- **on_scheduled_fail**：数组为定时任务 ID 列表，当这些定时任务**失败**时自动触发。例：`{"on_scheduled_fail": [3]}`

可在编辑触发任务时在“触发条件”中填写上述 JSON（与其它说明文字并存时需为合法 JSON）。

## 七、建议补全项（可选）

1. ~~**可选**：Channel 的 WebSocket 端点。~~ **已实现**：`WS /api/ssh-channel/{id}/ws`，query 传 `token`，服务端推送 `ready`/`lines`/`closed`/`error`，客户端发文本即写入通道。
2. ~~**可选**：主机是否存在（存活探测）。~~ **已实现**：`GET /api/ssh-channel/{id}/host-alive`、`GET /api/hosts/{id}/alive`，以及通道详情 `?check_alive=1`。
3. ~~**可选**：触发/定时任务历史按任务名、状态全局筛选。~~ **已实现**：GET /api/triggered-tasks/all-runs、GET /api/scheduled-tasks/all-runs 及前端「全部执行历史」筛选。

当前实现阶段：**Phase 1～5** 与上述可选项均已完成。
