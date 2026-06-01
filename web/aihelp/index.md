# 毛竹 操作帮助目录

本文档汇集 毛竹 各帮助主题，便于通过 AI 或直接查阅。**用户只读**；仅管理员可编辑帮助文档与本目录。

---

## 建议阅读顺序

如果你是第一次使用，建议按下面顺序阅读：

1. 先看 [operations-manual.md](operations-manual.md)，建立系统整体认知
2. 再看 [overview.md](overview.md)，快速了解功能边界与典型流程
3. 然后根据任务需要进入具体步骤手册

如果你已经在使用系统，可直接从下方目录跳到对应主题。

---

## 一、入门与总览

| 文档 | 说明 |
|------|------|
| [operations-manual.md](operations-manual.md) | 综合操作手册与实战方法，适合先建立整体认知 |
| [overview.md](overview.md) | 系统概述、核心能力、典型使用路径 |
| [aihelp.md](aihelp.md) | 帮助文档说明（查阅方式、权限、维护方式） |

---

## 二、日常操作步骤手册

| 文档 | 说明 |
|------|------|
| [credentials.md](credentials.md) | 凭证管理步骤手册（密码型、密钥型、复用与变更） |
| [hosts.md](hosts.md) | 主机管理步骤手册（新增、验证、编辑、删除、交给 AI） |
| [host-groups.md](host-groups.md) | 服务器树与主机分组步骤手册 |
| [terminal.md](terminal.md) | SSH 控制台与终端使用规则、自动切换、AI 边界 |
| [filesystem.md](filesystem.md) | 文件系统 `web/fs` 步骤手册（脚本、模板、上传材料、`chats/` 附件与成果物） |
| [batch.md](batch.md) | 批量操作步骤手册（巡检、上传、脚本、重启） |
| [ai-assistant.md](ai-assistant.md) | AI 助手使用与配置（低交互、运行中补充、聊天附件含 Office/PDF、流式分块 Markdown、会话级提示词、终端边界、Markdown/公式/图形化输出） |
| [external-integration.md](external-integration.md) | **外部智能体集成**：API Token、OpenClaw、Hermes、Cursor MCP；**反向接入**——个人 MCP（`/mcp-servers`）与个人 Agent Skills（`/skills`） |
| [maintenance.md](maintenance.md) | 维护历史步骤手册（记录、分类、复盘） |
| [feedback.md](feedback.md) | 反馈与登录留言板步骤手册（用户提交、管理员审核与回复、邮件通知） |
| [local.md](local.md) | 本机管理步骤手册（仅管理员） |

---

## 三、专题参考

| 文档 | 说明 |
|------|------|
| [esxi.md](esxi.md) | ESXi 主机接入：如何开启 ESXi SSH |
| [windows.md](windows.md) | Windows 主机接入：如何安装 OpenSSH 服务器 |
| [nginx-and-wireguard.md](nginx-and-wireguard.md) | Nginx 配置与建站参考、WireGuard 配置与用途 |
| [firewall-and-logs.md](firewall-and-logs.md) | Linux 防火墙、端口检查、系统日志检查 |

---

## 四、按场景快速查阅

| 你要做什么 | 建议先看 |
|------|------|
| 想快速知道这个系统怎么用 | [operations-manual.md](operations-manual.md) |
| 想新增主机并连接 SSH | [credentials.md](credentials.md)、[hosts.md](hosts.md) |
| 想整理服务器树或做分组批量 | [host-groups.md](host-groups.md)、[batch.md](batch.md) |
| 想让 AI 帮你运维，或在 AI 执行中补充/暂停/停止 | [ai-assistant.md](ai-assistant.md)、[terminal.md](terminal.md) |
| 想用 OpenClaw / Cursor / 外部 Agent 调 毛竹 | [external-integration.md](external-integration.md) |
| 想给 毛竹 的 AI 接入自己的 MCP 服务器（filesystem/Notion 等） | [external-integration.md](external-integration.md)（反向集成 · MCP）、导航「MCP 配置」 |
| 想给 毛竹 的 AI 配置个人 Agent Skills（SKILL.md） | [external-integration.md](external-integration.md)（反向集成 · Skills）；管理员先在用户管理开启 Skills |
| 想给 AI 上传 PDF / Word / Excel / PPT 等文档分析 | [ai-assistant.md](ai-assistant.md)（聊天附件）、[filesystem.md](filesystem.md)（`chats/` 存储） |
| 想准备脚本、模板、上传材料 | [filesystem.md](filesystem.md) |
| 想做本机运维 | [local.md](local.md) |
| 想做维护记录或复盘 | [maintenance.md](maintenance.md) |
| 想给管理员留反馈，或在登录页留言 | [feedback.md](feedback.md) |
| 管理员想审核留言、回复用户反馈 | [feedback.md](feedback.md) |

---

## 使用方式

- **推荐起点**：先阅读 [operations-manual.md](operations-manual.md)，快速了解系统资源关系、推荐流程、AI/终端边界与排障顺序。
- **通过 AI**：在对话中询问「如何添加主机」「批量操作怎么用」「有哪些功能」等，AI 会读取本目录及对应文档后回答。
- **直接查阅**：在 `web/aihelp/` 下按上表路径打开对应 `.md` 文件。
