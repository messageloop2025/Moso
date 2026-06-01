# AI 委派 / 编排 用例集（AI Delegation Cookbook）

> ⭐ **毛竹 的核心优势是"多主机原生"**——同一个 AI 会话可以同时驾驭 N 台机器，把
> 「开发 → 发布 → 运维 → 反馈」的完整生命周期串成一条可审计的执行链。本 Cookbook 的
> 第一节 [★ 旗舰示例](#-旗舰示例开发--发布--运维--反馈-生命周期) 就是这种多机联动的范本，
> 务必先读。

本 Cookbook 把 毛竹 在 AI 运维能力上的四块拼图（**主机能力画像**、**子 AI 委派**、**多步编排**、**工作流模板**、**AI 内部递归**、**流式进度**）串起来，给 15 个可以直接用的端到端用例。每个用例都写出：

- 触发场景（用户会怎么说）
- AI 内部应该怎么调（先画像、后确认、再编排 / 复用模板）
- 前端看到的事件时间线（SSE 进度）
- 审计 / 任务日志

> 相关文档：
> - 功能清单：`docs/功能清单.md` F5.12–F5.17
> - 技能手册：`docs/Skills文档.md`
> - 数据库：`docs/数据库结构.md` §2.13.1（主机级提示词 / 能力画像）、§2.13.2（编排模板）
> - 用户手册：`web/aihelp/ai-assistant.md`

---

## 0. 前置：建立主机能力画像

**任何用例开始前**，主 AI 遇到"要用某台主机做事"时，先读画像（`get_host_capabilities`），若无或陈旧就一次 `probe_host_capabilities`。

```jsonc
// tool_call
{"name": "probe_host_capabilities", "arguments": {"host_id": 12}}
// 返回
{
  "cached": false, "probed_at": "2026-04-18T10:12:03",
  "os": {"pretty": "Ubuntu 24.04"}, "hardware": {"cpu": 8, "mem_gb": 32},
  "tools_by_group": {
    "ai_agents": {"cursor-agent": "0.42.0", "aider": "0.64.2", "llm": "0.17"},
    "devops":    {"git": "2.43", "docker": "27.1.0"},
    "security":  {"nmap": "7.94"}
  }
}
```

后续所有示例都**假设已完成画像**。

---

## ★ 旗舰示例：开发 / 发布 / 运维 / 反馈 生命周期

毛竹 面向"多机协作"设计——**同一个 AI 会话可以同时驾驭多台主机**。这个旗舰用例展示一条跨 4 台主机、覆盖完整软件生命周期的 `delegate_chain`，建议把它作为"怎么用好 毛竹"的第一样板。

### 角色分工

| 阶段 | 主机 | 职责 | 已装工具（画像确认） |
|---|---|---|---|
| ① Dev 开发 | `dev-01` (host_id=21) | agent 改代码 + 自测 | git / cursor-agent / aider / pytest |
| ② Release 发布 | `build-02` (host_id=22) | 出镜像、推生产 | docker / rsync / ssh |
| ② Release 发布 | `prod-03` (host_id=23) | 热更新、健康就绪 | docker / systemctl |
| ③ Ops 运维 | `monitor-04` (host_id=24) | 拉 P99、错误率、报警 | curl / jq / prometheus-cli |
| ④ Feedback 反馈 | （毛竹 本机） | 子 AI 写周报 + 邮件送 oncall | `delegate_to_edgeops_ai` + `send_email` |

### 用户一句话

> 「让 dev-01 的 cursor-agent 按 `/srv/app/SPEC.md` 实现 jwt-login；
>   过了就去 build-02 打 docker tag v2.3.0，把镜像 push 到 prod-03 热更新；
>   等 5 秒让 monitor-04 拉一下 P99 和错误率，
>   最后让 毛竹 子 AI 写一份中文周报，邮件送 `oncall@company.com`。」

### AI 内部动作

1. `probe_host_capabilities` / `get_host_capabilities` 依次画像 21 / 22 / 23 / 24（未画像的一次性做掉）；
2. 组装一条 `delegate_chain`（见下方 payload），**整条链一次性**用 `ask_user_choice` 展开给用户确认；
3. 确认后下发执行——4 台主机并行流式推进度到前端；
4. 跑完询问「要不要把这条存成 `release-v2` 工作流模板？」

### `delegate_chain` Payload

```jsonc
{
  "name": "delegate_chain",
  "arguments": {
    "host_id": 21,
    "confirmed": true,
    "stop_on_failure": true,
    "steps": [
      // ① Dev：开发机改代码
      {
        "kind": "delegate", "name": "code-edit", "host_id": 21,
        "agent": "cursor-agent", "workdir": "/srv/app",
        "task": "Implement jwt-login per /srv/app/SPEC.md. Keep public signatures stable. Run black before finishing."
      },
      // ① Dev：开发机自测
      {
        "kind": "ssh", "name": "self-test", "host_id": 21,
        "command": "pytest -q --cov=src", "workdir": "/srv/app", "timeout": 300
      },
      // ② Release：构建机出镜像（只有自测通过才构建）
      {
        "kind": "ssh", "name": "build-image", "host_id": 22, "when": "on_success",
        "command": "cd /srv/app && git pull && docker build -t app:v2.3.0 .",
        "timeout": 900
      },
      // ② Release：跨机中继镜像到生产
      {
        "kind": "ssh", "name": "ship-to-prod", "host_id": 22, "when": "on_success",
        "command": "docker save app:v2.3.0 | ssh prod-03 'docker load'",
        "timeout": 600
      },
      // ② Release：生产机热更新
      {
        "kind": "ssh", "name": "restart-prod", "host_id": 23, "when": "on_success",
        "command": "systemctl restart app && systemctl is-active app", "timeout": 120
      },
      // 让服务起稳
      {"kind": "sleep", "name": "settle", "seconds": 5},
      // ③ Ops：监控机巡检
      {
        "kind": "ssh", "name": "healthcheck", "host_id": 24,
        "command": "curl -s 'http://prom/api/v1/query?query=p99{app=\"myapp\"}' | jq .data.result",
        "timeout": 60
      },
      // ③ Ops：失败自愈（只在 healthcheck 失败时触发）
      {
        "kind": "delegate", "name": "heal-on-fail", "host_id": 23,
        "when": "on_failure", "agent": "opencode", "workdir": "/srv/app",
        "task": "Production P99 spiked. Inspect /var/log/app/* and /etc/app/*.conf, propose a minimal rollback PR.\n\n=== last stderr ===\n{prev_stderr}"
      }
    ]
  }
}
```

> 链结束后，主 AI **再**发起一次 `delegate_to_edgeops_ai` 写周报 + 一次 `send_email`——
> 这两步不适合塞进 `delegate_chain`，因为它们不走 SSH。

### ④ Feedback：子 AI 写周报 + 邮件

```jsonc
{
  "name": "delegate_to_edgeops_ai",
  "arguments": {
    "system_prompt": "你是 毛竹 的运维周报撰写员。输出纯中文 Markdown，分 ## 本次发布 / ## 健康度 / ## 风险与建议 三节；禁止客套话。",
    "task": "按下面上下文写周报，重点突出 P99 变化与任何失败步骤。",
    "context_hint": "<把前面 chain 每步的 summary / stdout_preview 拼在这里>",
    "allowed_tools": [],
    "max_steps": 4,
    "timeout_sec": 120
  }
}
// → 收到 Markdown 报告字符串 report_md
{
  "name": "send_email",
  "arguments": {
    "to": "oncall@company.com",
    "subject": "[毛竹] release v2.3.0 生命周期周报",
    "body": "<report_md 原文>"
  }
}
```

### 前端时间线（SSE 实际长相）

```
▶ [1] code-edit    delegate  @dev-01
  [stdout] Editing src/auth.py ...
  [stdout] +38 -12 lines, ran black OK
✓ [1] code-edit    18.2s
▶ [2] self-test    ssh       @dev-01
  [stdout] 42 passed in 3.1s
✓ [2] self-test     3.4s  exit=0
▶ [3] build-image  ssh       @build-02
  [stdout] Step 12/20 : COPY . .
  [stdout] Successfully tagged app:v2.3.0
✓ [3] build-image  76.8s exit=0
▶ [4] ship-to-prod ssh       @build-02
  [stdout] Loaded image: app:v2.3.0
✓ [4] ship-to-prod 11.2s exit=0
▶ [5] restart-prod ssh       @prod-03
  [stdout] active
✓ [5] restart-prod  1.6s exit=0
▶ [6] settle       sleep     5s
▶ [7] healthcheck  ssh       @monitor-04
  [stdout] p99=187ms  error_rate=0.03%
✓ [7] healthcheck   0.8s exit=0
⏭ [8] heal-on-fail skipped (only runs on_failure)
完成 8 步，跨 4 台主机，总耗时 126s
```

### 审计 & 任务日志

- `audit_log` 会给 host 21/22/23/24 **各留一条** `kind=cli_agent_delegate` 或 `kind=ssh_execute` 的记录；
- 含写类步骤（code-edit / build-image / ship-to-prod / restart-prod）会在各自主机的 `.edgeops/task/<yyyy-mm>/<chain_id>.md` 里追加任务日志；
- `delegate_to_edgeops_ai` 与 `send_email` 各自单独入 `operation_logs`。

### 存成模板，以后一句话复用

```jsonc
{"name": "save_workflow_template", "arguments": {
  "name": "release-v2",
  "description": "dev→build→prod→monitor 的标准发布链（4 主机）",
  "tags": "cicd,cross-host,release",
  "visibility": "org",
  "payload": { "...上面整段 arguments..." }
}}
```

把链里的 `v2.3.0` / `jwt-login` / `SPEC.md` 路径改写成 `${build_tag}` / `${feature}` / `${spec_path}`。以后：

> 「跑一次 release-v2，feature=payment-v3，build_tag=v2.4.0」

AI 先 `dry_run=true` 给你 `resolved_payload` 预览，确认后真跑；所有安全门禁（画像 / 写类确认 / 审计 / 流式进度）照旧一分不少。

### 关键设计点

| 设计 | 为什么重要（多机场景） |
|---|---|
| **每步独立 `host_id`** | 链里同时存在 4 个主机，AI 不用分 4 次调用 |
| **一次确认整条链** | 用户看到完整"哪步在哪台机"地图，不必逐步批 |
| **每台主机独立鉴权** | 用户对其中一台没权限，整条链直接拒绝 |
| **`when=on_success/on_failure`** | 自测失败不发版、健康度挂了自愈，纯声明式 |
| **`{prev_stdout}` / `{prev_stderr}`** | 上一步日志直接喂给下一步的 agent 任务描述 |
| **流式进度 per-host** | 4 台机并行的日志在前端同一卡片滚动，不黑盒 |
| **`task scope` 自动放行** | 同一条链丢给定时任务，凌晨无人值守也能跑 |

---

## 1. 让 cursor-agent 修一个 bug（单步委派）

**触发**：「让 12 号机的 cursor-agent 把 `/srv/myapp/auth.py` 的登录改成 JWT」

**AI 动作**：
1. `get_host_capabilities(host_id=12)` 确认有 `cursor-agent`；
2. `ask_user_choice` 展开"我准备用 cursor-agent 在 /srv/myapp 改 auth.py，预计消耗 CURSOR_API_KEY，OK 吗？"；
3. 用户点确认 → `delegate_to_cli_agent(confirmed=true, ...)`；
4. 读返回的 `git_diff.files_changed / diff_preview`，用自然语言向用户复述并提示 `git reset` 回滚方式。

```jsonc
{
  "name": "delegate_to_cli_agent",
  "arguments": {
    "host_id": 12,
    "agent": "cursor-agent",
    "task": "Refactor /srv/myapp/auth.py to use JWT; keep the public signature of login() unchanged.",
    "workdir": "/srv/myapp",
    "env": {"CURSOR_API_KEY": "sk-cur-..."},
    "timeout": 600,
    "confirmed": true
  }
}
```

**前端时间线（SSE）**：
```
executing delegate_to_cli_agent
  [stdout] Analyzing auth.py...
  [stdout] Generating edit plan...
  [stdout] Applying edits to auth.py
  [stdout] Running tests...
completed delegate_to_cli_agent   exit=0  12.6s
```

**审计**：`audit_log` 一条 `kind=cli_agent_delegate`，只记 `env_keys=["CURSOR_API_KEY"]`，不记值。

---

## 2. 改→测→失败就自愈（单机 chain）

**触发**：「让 12 号机的 aider 把单元测试补齐到 85% 覆盖率；跑 pytest；失败就按报错让它自己修一遍再跑一次」

```jsonc
{
  "name": "delegate_chain",
  "arguments": {
    "host_id": 12, "confirmed": true,
    "steps": [
      {
        "kind": "delegate", "name": "write-tests", "agent": "aider",
        "task": "Raise pytest coverage of src/ to ≥85%. Add missing tests only; do not modify existing code.",
        "workdir": "/srv/myapp"
      },
      {
        "kind": "ssh", "name": "run-pytest",
        "command": "pytest --cov=src --cov-report=term-missing",
        "workdir": "/srv/myapp",
        "timeout": 300
      },
      {
        "kind": "delegate", "name": "auto-heal", "agent": "aider",
        "when": "on_failure",
        "task": "pytest just failed. Analyze the stderr below and fix the failing tests or source.\n\n=== pytest stderr ===\n{prev_stderr}",
        "workdir": "/srv/myapp"
      },
      {
        "kind": "ssh", "name": "rerun-pytest",
        "when": "on_failure",
        "command": "pytest --cov=src",
        "workdir": "/srv/myapp",
        "timeout": 300
      }
    ]
  }
}
```

**时间线**：
```
▶ [1] write-tests  delegate  @host-12
  [stdout] Adding test_login.py ...
✓ [1] write-tests  18.2s
▶ [2] run-pytest   ssh  @host-12
  [stdout] ==== FAILED tests/test_login.py::test_expired_token ====
✗ [2] run-pytest   3.4s  exit=1
▶ [3] auto-heal    delegate  @host-12   (当 [2] failed)
  [stdout] Patching src/auth.py::_decode_jwt ...
✓ [3] auto-heal    11.9s
▶ [4] rerun-pytest ssh  @host-12   (当 [2] failed)
✓ [4] rerun-pytest 2.8s  exit=0
```

---

## 3. 跨机流水线：A 改代码 → B 跑测试 → C 部署

**触发**：「A 机改完就 rsync 到 B 机跑测试，测试过了在 C 机部署」（host_id 分别是 21/22/23）

```jsonc
{
  "name": "delegate_chain",
  "arguments": {
    "host_id": 21, "confirmed": true,
    "steps": [
      {"kind": "delegate", "name": "edit", "host_id": 21, "agent": "cursor-agent",
       "task": "Implement feature XYZ per /srv/app/SPEC.md", "workdir": "/srv/app"},
      {"kind": "ssh", "name": "sync-to-test", "host_id": 21,
       "command": "rsync -az --delete /srv/app/ testuser@test-box:/srv/app/"},
      {"kind": "ssh", "name": "pytest-on-B", "host_id": 22,
       "command": "pytest -q", "workdir": "/srv/app", "timeout": 600},
      {"kind": "ssh", "name": "deploy-on-C", "host_id": 23, "when": "on_success",
       "command": "bash /opt/deploy/release.sh", "timeout": 600}
    ]
  }
}
```

- 每步在各自主机独立鉴权；`audit_log` 会给 21/22/23 各留一条；
- 前端时间线会交替出现 `@build-a` / `@test-b` / `@prod-c` 标签。

---

## 4. nmap 扫描 → llm 总结漏洞

**触发**：「用 99 号 kali 机的 nmap 扫 10.0.0.0/24，扫完让 llm 把高危漏洞列成清单」

```jsonc
{
  "name": "delegate_chain",
  "arguments": {
    "host_id": 99, "confirmed": true,
    "steps": [
      {"kind": "ssh", "name": "nmap-scan",
       "command": "nmap -sV -Pn -oX - 10.0.0.0/24 | head -c 200000",
       "timeout": 600, "max_output_chars": 200000},
      {"kind": "delegate", "name": "summarize", "agent": "llm",
       "task": "Below is an nmap XML report. Produce a Markdown list of HIGH/CRITICAL risks only, 1 line each with host:port → reason.\n\n{prev_stdout}"}
    ]
  }
}
```

---

## 5. 装依赖 → 等 2 秒 → 验证

`sleep` 步的典型用途：容器 / systemd 重启后轮询。

```jsonc
{"steps": [
  {"kind": "ssh", "name": "restart", "command": "systemctl restart myapp"},
  {"kind": "sleep", "name": "settle", "seconds": 3},
  {"kind": "ssh", "name": "healthcheck", "command": "curl -fsS http://127.0.0.1:8080/health"}
]}
```

---

## 6. 保存成模板：`save_workflow_template`

用户对第 3 例（跨机 CI/CD）说："**这条以后我还要跑，叫 `daily-release`**"。AI：

```jsonc
{
  "name": "save_workflow_template",
  "arguments": {
    "name": "daily-release",
    "description": "build@host-21 → test@host-22 → deploy@host-23",
    "tags": "cicd,cross-host",
    "visibility": "private",
    "payload": {
      "host_id": 21,
      "steps": [
        {"kind": "delegate", "name": "edit", "host_id": 21, "agent": "cursor-agent",
         "task": "Implement feature ${feature} per /srv/app/SPEC.md", "workdir": "/srv/app"},
        {"kind": "ssh", "name": "sync-to-test", "host_id": 21,
         "command": "rsync -az --delete /srv/app/ testuser@test-box:/srv/app/"},
        {"kind": "ssh", "name": "pytest-on-B", "host_id": 22,
         "command": "pytest -q --branch=${branch}", "workdir": "/srv/app", "timeout": 600},
        {"kind": "ssh", "name": "deploy-on-C", "host_id": 23, "when": "on_success",
         "command": "bash /opt/deploy/release.sh --tag ${build_tag}", "timeout": 600}
      ]
    }
  }
}
```

返回 `{id: 7, declared_variables: ["feature", "branch", "build_tag"]}`。

---

## 7. 复用模板：`run_workflow_template`

**触发**：「跑一次 daily-release，feature=jwt-login，branch=release/v2，build_tag=v2.3.0」

```jsonc
// 先 dry_run 给用户预览
{"name": "run_workflow_template", "arguments": {
  "template_id": 7, "dry_run": true,
  "variable_overrides": {"feature": "jwt-login", "branch": "release/v2", "build_tag": "v2.3.0"}
}}
```

返回 `resolved_payload` + `missing_variables: []`。主 AI `ask_user_choice` 展示 "我准备按 daily-release 跑整条链，3 台机，确认？" → 用户确认 → 真跑：

```jsonc
{"name": "run_workflow_template", "arguments": {
  "template_id": 7, "confirmed": true,
  "variable_overrides": {"feature": "jwt-login", "branch": "release/v2", "build_tag": "v2.3.0"}
}}
```

跑完 `run_count += 1, last_run_at = now`。

---

## 8. 组织内共享模板

管理员或资深用户把常用链存成 `visibility="org"`：

```jsonc
{"name": "save_workflow_template", "arguments": {
  "name": "prod-backup-standard",
  "visibility": "org",
  "description": "统一的生产环境备份：db dump → rsync off-site → checksum verify",
  "payload": { "...": "..." }
}}
```

同实例其它用户 `list_workflow_templates()` 能看到它，`run_workflow_template` 照常跑（用他们自己的凭证鉴权每一台机）。

---

## 9. 让子 AI 写报告（`delegate_to_edgeops_ai`）

**触发**：「把刚才这堆 nmap + 日志分析结果整理成一份中文运维周报」

```jsonc
{
  "name": "delegate_to_edgeops_ai",
  "arguments": {
    "system_prompt": "你是 毛竹 的运维报告撰写员。输出纯中文 Markdown，包含：## 摘要 / ## 风险矩阵 / ## 建议动作 三个小节。禁止闲聊，禁止使用英文。",
    "task": "请根据以下上下文，产出本周的运维周报。",
    "context_hint": "<这里贴前面几步工具的 result_preview 摘要>",
    "allowed_tools": [],
    "max_steps": 4,
    "timeout_sec": 120
  }
}
```

- 不走 SSH、不改任何主机；
- `allowed_tools: []` 表示纯推理；
- 递归深度硬上限 2（主→子），孙被拒。

---

## 10. Code reviewer 子 AI

**触发**：「让另一个 AI 帮我检查你刚写的那段 bash 部署脚本有没有坑」

```jsonc
{
  "name": "delegate_to_edgeops_ai",
  "arguments": {
    "system_prompt": "你是资深 SRE / bash 审查员。给出 5 条具体问题并标注严重级（阻塞/建议/可选）。不要客套话。",
    "task": "审查下面这段 bash：\n\n```bash\n<刚才主 AI 写的 release.sh>\n```",
    "allowed_tools": [],
    "max_steps": 2
  }
}
```

---

## 11. 用子 AI 聚合多个分析结果

主 AI 连续跑 3 个 `delegate_chain`（登录日志 / 磁盘告警 / CPU 毛刺），然后：

```jsonc
{
  "name": "delegate_to_edgeops_ai",
  "arguments": {
    "system_prompt": "你是总结员。把三段材料合成一份 200 字以内结论，按严重性倒序；不要重复材料文字。",
    "task": "请合成结论。",
    "context_hint": "材料1：...\n材料2：...\n材料3：...",
    "allowed_tools": []
  }
}
```

---

## 12. 流式进度的实际用户体验

当 `delegate_chain` 跑一条 ~60s 的链时，前端工具卡底下会实时出现这样的滚动日志（最多保留 40 行，超出自动滚动丢弃早期）：

```
▶ [1] build        delegate  @build-a
  [stdout] Downloading deps ... 45%
  [stdout] Downloading deps ... 100%
  [stdout] Compiling ...
✓ [1] build        12.6s
▶ [2] ship         ssh       @build-a
  [stdout] sending incremental file list
  [stdout] app/bin/myapp
  [stdout] app/conf/prod.yml
✓ [2] ship         3.1s  exit=0
▶ [3] integration  ssh       @test-b
  [stdout] collected 182 tests
  [stderr] (pytest running...)
  [stdout] 182 passed in 42.7s
✓ [3] integration  43.1s exit=0
▶ [4] deploy       ssh       @prod-c
  [stdout] Systemd restart myapp
✓ [4] deploy       1.8s  exit=0
```

用户可以**随时中断**（顶部"停止"按钮 → SSE 断流 → 任务引擎收到取消 → 当前步执行完毕后链退出，已跑步骤留审计），这比之前"等整条跑完才出结果"要直观得多。

---

## 13. 定时任务：后台跨机自愈

在「定时任务」里直接写一条 `delegate_chain` 调用作为指令内容，task scope 下自动视为已授权（不用 `confirmed`）：

```
# 每天 02:00 跑一次生产健康巡检
调用 delegate_chain：
  host_id=31（监控机）
  steps:
    1. ssh: prometheus query 拉 P99 延迟
    2. delegate@32: 如果 P99 > 500ms 让 llm 按日志分析 Top-5 慢接口
    3. ssh@33: 把结果邮件发给 oncall
```

失败不重试（`stop_on_failure=true`）；每台机审计 / 任务日志都有一条。定时任务 UI 会显示每步的 host_label / exit_code / duration，不需要到每台机上去看。

---

## 14. 安全边界一览

| 动作 | `delegate_to_cli_agent` | `delegate_chain` | `run_workflow_template` | `delegate_to_edgeops_ai` |
|---|---|---|---|---|
| 需要主机能力画像 | ✅ 校验 agent 已装 | ✅ 每台主机各自校验 | ✅ 同 delegate_chain | ❌（不走 SSH） |
| 需要用户确认（写类） | ✅ `confirmed=true` | ✅ 对整条链一次 | ✅ 同 delegate_chain | ❌ |
| task scope 自动放行 | ✅ | ✅ | ✅ | ✅ |
| 审计日志（每主机一条） | ✅ | ✅ 每台主机独立 | ✅ 同 delegate_chain | ❌（无 SSH 动作） |
| 任务日志（.edgeops/task） | ✅ 高风险时 | ✅ 含写类时按主机拆分 | ✅ 同 delegate_chain | ❌ |
| 递归 / 嵌套限制 | — | — | 内部转发到 delegate_chain | ✅ depth≤2，不准自调 |
| 流式进度 | ✅ `sub_agent_line` | ✅ 4 种事件 | ✅ 同 delegate_chain | ⚠️ 尽力而为（只推 step/tool 摘要） |

---

## 15. 小抄（Cheat Sheet）

- "让 X CLI 干 Y"（改文件类）：`delegate_to_cli_agent`
- "一串步骤，可能跨机"：`delegate_chain`
- "以后还要跑的那串"：`save_workflow_template` + `run_workflow_template`
- "需要独立身份 / 独立上下文"（写报告 / review / 聚合）：`delegate_to_edgeops_ai`
- 永远先 `probe_host_capabilities` / `get_host_capabilities`，再做以上任何一步
