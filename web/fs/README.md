# 文件系统（每用户工作区）

本目录为 毛竹 **每用户文件系统根**（`web/fs/<username>/`），用于脚本、文档、聊天附件、Agent Skills 等。

## 常见子目录

| 路径 | 用途 |
|------|------|
| **scripts/** | 批量操作引用的脚本（如 `scripts/restart_nginx.sh`） |
| **docs/** | 文档或其它资源 |
| **chats/** | AI 聊天附件、成果物、工具 spill（按日期分目录） |
| **skills/** | 个人 **Agent Skills**（Cursor 格式 `skills/<name>/SKILL.md` + 可选 `reference.md`）；须管理员开启；渐进式披露；`/skills` 页支持导入导出 |

路径均相对用户根目录，禁止使用 `..` 逃逸。通过「文件系统」页或 API `/api/fs/*` 管理；AI 可通过 `fs_*`、`batch_create` 等工具访问。
