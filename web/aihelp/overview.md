# 毛竹 系统概览与导航

毛竹 是一套**远程 AI 运维系统**。它将 SSH 主机管理、文件准备、批量任务、AI 协同、维护记录和帮助文档整合在一起，目标是让用户既能手工运维，也能和 AI 高效协同。

这页的作用不是展开讲每个细节，而是告诉你：

- 这个系统大致能做什么
- 你应该先从哪里开始
- 遇到某类任务时该看哪份文档

---

## 建议先读什么

如果你是第一次接触 毛竹，建议按这个顺序阅读：

1. [operations-manual.md](operations-manual.md)
2. [overview.md](overview.md)
3. 进入对应的步骤手册，如 [hosts.md](hosts.md)、[ai-assistant.md](ai-assistant.md)、[batch.md](batch.md)

### 为什么这样读

- `operations-manual.md` 负责建立整体认知
- 本页负责快速导航
- 各步骤手册负责“具体怎么做”

---

## 这个系统可以做什么

### 远程主机运维

- 管理 Linux、ESXi、已安装 OpenSSH 的 Windows 主机
- 通过 SSH 执行命令
- 打开控制台进行持续交互
- 上传脚本、配置和压缩包到主机

### 多机协同操作

- 用服务器树组织主机
- 按分组做批量执行、批量上传、批量脚本、批量重启
- 让 AI 在多个 AI 控制台中并行处理任务
- **多主机生命周期联动**：一条 AI 消息跨 N 台主机串起「开发 → 发布 → 运维 → 反馈」闭环——开发机 agent 改代码 → 构建机出包 → 生产机热更新 → 监控机巡检 → 子 AI 写报告发 oncall。详见 [ai-assistant.md](ai-assistant.md) 的「多主机生命周期联动」段落，以及 `docs/AI-Delegation-Cookbook.md` 的旗舰用例。

### AI 协同运维

- 用自然语言驱动日常运维
- 给会话设置专用提示词
- 按会话开启「低交互」，让 AI 在可继续时少问确认；也可在运行中补充上下文、暂停或停止
- 结合主机知识、终端状态、最佳实践做判断
- 输出文本说明，也可输出图形化内容、代码高亮、Callout 和 LaTeX 公式；流式回复时对已闭合的代码块、表格等**分块即时排版**
- 在会话中上传**聊天附件**（文本、图片、PDF / Office 等），由 MarkItDown 等机制转为可分析的 Markdown（详见 [ai-assistant.md](ai-assistant.md)）

### 平台与审计能力

- 管理 `web/fs` 工作区
- 记录维护历史
- 维护最佳实践
- 通过 `web/aihelp` 提供系统内帮助文档
- **反馈与留言板**：登录页右下角的匿名留言板 + 系统内「反馈」菜单，让访客与已登录用户都能向管理员发声；管理员可审核 / 公开 / 回复 / 忽略，可选邮件通知。详见 [feedback.md](feedback.md)。
- **外部智能体集成**：OpenClaw（claw-ops）、Hermes（claw-skills）、Cursor MCP 等可通过 **个人 API Token** 无浏览器调用 毛竹。详见 [external-integration.md](external-integration.md)。
- **反向扩展 AI**：每用户可配置 **个人 MCP**（`/mcp-servers`，导入/导出）与 **Agent Skills**（`/skills`，管理员开关），扩展网页/主机/集成通道内的 AI 能力。详见 [external-integration.md](external-integration.md)。

---

## 系统的核心模块

可以把 毛竹 理解成 8 个主要模块：

1. **凭证管理**：维护 SSH 登录认证信息
2. **主机管理**：维护主机列表、连接、检测类型
3. **服务器树 / 分组**：组织主机范围
4. **SSH 控制台**：做持续交互式操作
5. **文件系统 `web/fs`**：准备脚本、模板和上传材料
6. **批量任务**：做统一下发和批量执行
7. **AI 助手**：自然语言协同运维
8. **维护历史 / 最佳实践 / 帮助文档**：沉淀经验和操作方法

---

## 推荐使用路径

### 路径一：新增一台主机并开始运维

1. 看 [credentials.md](credentials.md)
2. 看 [hosts.md](hosts.md)
3. 如需控制台操作，再看 [terminal.md](terminal.md)
4. 如需让 AI 接手，再看 [ai-assistant.md](ai-assistant.md)

### 路径二：准备脚本并下发到主机

1. 看 [filesystem.md](filesystem.md)
2. 看 [hosts.md](hosts.md)
3. 如需批量下发，再看 [batch.md](batch.md)

### 路径三：整理服务器树并做批量任务

1. 看 [host-groups.md](host-groups.md)
2. 看 [batch.md](batch.md)

### 路径四：让 AI 协助你排障或部署

1. 看 [ai-assistant.md](ai-assistant.md)
2. 看 [terminal.md](terminal.md)
3. 如需记录结果，再看 [maintenance.md](maintenance.md)

### 路径五：管理员维护 毛竹 本机

1. 看 [local.md](local.md)
2. 如需排查日志或端口，再看 [firewall-and-logs.md](firewall-and-logs.md)

---

## 你应该知道的关键规则

### AI 与终端的边界

- AI 只能操作 **AI 创建** 的 SSH 控制台
- AI 不会抢占用户自己创建的终端
- 若用户正在手工操作自己的终端，AI 不会强行切换过去

### 文件范围的边界

- `web/fs` 是用户自己的工作区
- 本机管理操作的是 毛竹 所在机器
- 远程主机文件则属于 SSH / SCP / 远程文件系统能力

### 权限边界

- 普通用户通常只能操作自己有权限的资源
- 管理员可看到和管理更广范围的数据
- 帮助文档默认用户只读，管理员可维护

---

## 典型使用场景

### 场景一：日常巡检

- 用主机管理确认主机状态
- 用批量任务执行巡检命令
- 有异常时再进入 AI 助手或主机详情 AI 分析

### 场景二：部署与发布

- 在 `web/fs` 准备脚本和包
- 先单机验证
- 再小范围批量执行
- 必要时记录维护历史

### 场景三：故障排查

- 先看终端和日志
- 需要持续交互时用控制台
- 需要保留过程时写维护历史
- 需要复用经验时沉淀最佳实践

---

## 按任务查文档

| 任务 | 建议先看 |
|------|------|
| 新增主机 | [credentials.md](credentials.md)、[hosts.md](hosts.md) |
| 整理服务器树 | [host-groups.md](host-groups.md) |
| 用控制台做连续操作 | [terminal.md](terminal.md) |
| 让 AI 接管任务 | [ai-assistant.md](ai-assistant.md) |
| AI 正在执行时想补充信息、暂停或停止 | [ai-assistant.md](ai-assistant.md)、[terminal.md](terminal.md) |
| 准备上传脚本和配置 | [filesystem.md](filesystem.md) |
| 做批量任务 | [batch.md](batch.md) |
| 管理本机 | [local.md](local.md) |
| 记录和复盘维护 | [maintenance.md](maintenance.md) |
| 看专题参考 | [windows.md](windows.md)、[esxi.md](esxi.md)、[nginx-and-wireguard.md](nginx-and-wireguard.md)、[firewall-and-logs.md](firewall-and-logs.md) |
| 给管理员留反馈 / 看登录页留言板 | [feedback.md](feedback.md) |

---

## 后续怎么用这套帮助文档

- 想快速建立整体认知：先看 [operations-manual.md](operations-manual.md)
- 想知道入口和路径：看本页
- 想知道具体怎么操作：看对应步骤手册
- 想让 AI 回答“怎么做”：直接在对话里提问，AI 会读取这些帮助文档后再答复
