# 文件系统步骤手册（web/fs）

毛竹 为每个用户提供独立的工作目录 `web/fs/{用户名}`，可用来存放脚本、配置、压缩包、导出文件和临时资料。它既可以手工使用，也可以让 AI 帮你读写与整理。

如果把主机理解为“执行现场”，那么 `web/fs` 就是“准备材料和脚本的工作区”。

---

## 1. 什么时候该用 `web/fs`

适合放在这里的内容：

- Shell 脚本
- Python 脚本
- 配置模板
- 压缩包
- 导出的 Markdown
- 临时排障文件
- 准备上传到主机的文件

不适合放在这里的内容：

- 需要直接在远程主机上生成的运行时文件
- 应长期放在主机本地的数据目录
- 不希望保留在系统中的临时敏感文件

---

## 2. 推荐的目录结构

建议按用途分目录：

```text
scripts/
configs/
packages/
backups/
exports/
temp/
```

### 推荐说明

- `scripts/`：部署脚本、巡检脚本、修复脚本
- `configs/`：Nginx、systemd、应用配置模板
- `packages/`：安装包、压缩包、发布包
- `backups/`：备份文件、导出结果
- `exports/`：会话导出、报表、图表
- `temp/`：临时文件目录

这样后续无论你自己找文件，还是让 AI 帮你操作，都会更清晰。

---

## 3. 路径规则

这里的路径都是相对于你自己的 `web/fs` 根目录。

### 正确示例

- `scripts/deploy.sh`
- `configs/nginx/site.conf`
- `packages/app-release.tgz`

### 规则说明

- 不能使用 `..` 向上逃逸
- 路径引用时通常都用相对路径
- AI 或 API 调用时，也应尽量传相对路径

---

## 3.5 列表内打开文件：编辑与预览

在「文件系统」中点击文件后，右侧会出现编辑器，顶部有 **「编辑」** 与 **「预览」**：

- **Markdown（`.md`）**：预览为渲染后的 Markdown。
- **HTML（`.html` / `.htm`）**：预览在沙箱 **iframe** 中按 **网页效果** 展示（含页面内样式）；默认打开此类文件时会优先落在预览页，便于查看报告类页面。
- **常见脚本扩展名**：预览为带语法高亮的源码。
- **其它文本**：预览为纯文本。

---

## 3.6 聊天附件与 AI 成果物（`chats/`）

AI 会话相关的文件落在你的 `web/fs` 根目录下：附件与临时会话文件在 **`chats/sessions/<会话ID>/`**（无会话时回退日期目录）；报告成果物在 **`reports/年/月/日/<示意名>/`**。与手工整理的 `scripts/`、`configs/` 等并列：

| 内容 | 典型路径 | 说明 |
|------|----------|------|
| **聊天附件** | `chats/sessions/<session_id>/<uuid>.<ext>` | 在 AI 输入框上传的文本、图片、**PDF / Office**。元数据在表 `chat_attachments`；AI 通过 `read_chat_attachment` 读取。旧日期路径只读兼容。 |
| **MarkItDown 缓存** | 同目录 `<原文件名>.extracted.md` | 对 kind=`document` 的 Office/PDF，服务端用 [MarkItDown](https://github.com/microsoft/markitdown) 转为 Markdown 并旁路缓存；删除原附件时一并清理。 |
| **AI 成果物** | `reports/YYYY/MM/DD/<示意名>/<uuid>.<ext>` | AI 用 `create_chat_artifact` 写入；**修订用 `update_chat_artifact`**；同目录可有 `libs/` 等。旧数据可能在 `chats/sessions/<id>/…`。 |
| **工具溢出** | `chats/…/spill/` 等 | 单条工具返回过大时落盘 spill；全量用 `read_chat_data`。 |
| **长期 Memory** | `memory/`（hosts/topics/journal 等） | 跨会话笔记；AI 用 `memory_*` 读写；**勿存密码**。 |
| **Agent Skills** | `skills/<name>/SKILL.md` | 个人 Agent Skills（须管理员开启）。 |

### AI 常用文件工具

- 文本：`fs_list` / `fs_search` / `fs_read_file` / `fs_write_file` / `fs_mkdir` / `fs_delete` / `fs_copy` / pack·unpack
- 二进制与截断：`fs_read_binary` / `fs_write_binary` / `fs_truncate`
- 当前会话工作区前缀：`get_chats_workspace_dir`（`chats/sessions/<session_id>/`）
- Markdown 按章节：`markdown_list_sections` / `markdown_read_section` / `markdown_replace_section` / `markdown_search_sections`

### 使用注意

- 单文件默认上限 **20 MB**，单会话附件累计约 **500 MB**（可由 `EDGEOPS_CHAT_ATTACHMENT_*` 配置）。
- 富文档转 Markdown 可由 `EDGEOPS_MARKITDOWN_ENABLED` 关闭；输出长度受 `EDGEOPS_MARKITDOWN_MAX_OUTPUT_CHARS` 限制，超大文档可能只返回截断摘要。
- 在「文件系统」页可浏览 `chats/` 与 `reports/`，但**不建议**手工改附件内容——优先在 AI 会话里重新上传或让 AI 用 `create_chat_artifact` / `update_chat_artifact` 生成与修订报告。
- 详细上传说明与限额见 [ai-assistant.md](ai-assistant.md) 中的「聊天附件」相关段落。

---

## 4. 新建目录和文件

### 适用场景

- 开始一个新项目的运维工作区
- 为某个业务建立专属脚本目录
- 为某次发布准备配置和安装包

### 推荐步骤

1. 先确定用途
2. 创建对应目录
3. 再创建脚本或配置文件
4. 写入后先检查内容
5. 再上传到主机或交给批量任务使用

### 示例目录规划

```text
scripts/pay/
configs/pay/
packages/pay/
```

---

## 5. 写脚本到 `web/fs`

这是最常见的使用方式之一。

### 推荐步骤

1. 在 `scripts/` 下创建脚本文件
2. 写入脚本内容
3. 让 AI 或自己检查脚本逻辑
4. 如有需要，先在单机上测试
5. 再用于上传或批量执行

### 适合的脚本

- 巡检脚本
- 一键部署脚本
- 修复脚本
- 数据收集脚本

### 建议

- 文件名尽量体现用途，例如 `scripts/check-nginx.sh`
- 同类脚本尽量按业务或环境分目录
- 高风险脚本先人工审查

---

## 6. 写配置模板到 `web/fs`

把配置模板放在 `web/fs` 中，便于版本化和复用。

### 适合放的配置

- Nginx 配置
- systemd 服务文件
- `.env` 模板
- 应用配置模板
- WireGuard 配置模板

### 推荐步骤

1. 先在 `configs/` 下建目录
2. 写入模板内容
3. 检查变量和路径
4. 上传到目标主机验证
5. 成熟后再用于正式环境

### 建议

- 模板文件命名尽量清晰
- 若有环境差异，可按 `prod/`、`test/` 分开
- 可以让 AI 先根据需求生成模板，再由你确认

---

## 7. 与远程主机互传（scp_push / scp_pull）

这是 `web/fs` 最重要的用途之一：毛竹工作区是多机转运的中转站。

### 典型场景

- 上传部署脚本 / 压缩包 / 配置
- 从主机拉回日志、包、目录到工作区再分析
- **多系统转运**：A 机 → 工作区 → B 机（`scp_pull` 再 `scp_push`）

### 上传（工作区 → 主机）

1. 先把文件放到 `web/fs`
2. 确认相对路径正确
3. 确认目标主机可连接、远程路径可写
4. 用 **`scp_push`**（AI 对话调用卡有进度条；目录设 `recursive=true`）
5. 上传后在主机上校验（`ls -l`、大小、权限）

### 下载（主机 → 工作区）

1. 确认远程路径存在
2. 用 **`scp_pull`**（进度展示与 `scp_push` 相同；默认不限制体积；目录需 `recursive=true`）
3. 默认落到 `chats/sessions/<session_id>/`；精确路径可设 `session_managed=false`；报告用 `create_chat_artifact`→`reports/…`
4. 在侧栏「文件系统」或 `fs_list` / `data_query` 继续处理

---

## 8. 打包与解包

打包和解包适合管理一组文件，而不是单个小文件。

### 适用场景

- 上传整个目录
- 归档脚本和配置
- 备份某次发布材料
- 把多个文件一次性推到主机

### 推荐步骤

1. 把相关文件整理到同一目录
2. 打包为 `.tgz`
3. 上传到主机
4. 在主机解压
5. 检查解压目录和权限

### 建议

- 发布包、脚本包、配置包尽量分开打包
- 打包前清理无关文件，减少体积

---

## 9. 复制、移动、删除文件

这些操作适合整理工作区，但删除动作需要谨慎。

### 复制适合

- 生成模板副本
- 拷贝旧版本做新版本修改
- 备份某个脚本再修改

### 移动适合

- 重新整理目录结构
- 把临时文件移入归档目录

### 删除前建议先确认

- 文件是否仍被批量任务或 AI 会话引用
- 是否还需要上传到其它主机
- 是否要先备份到 `backups/`

### 推荐做法

- 高价值文件不要直接删，先移动到 `backups/` 或 `temp/`
- 大改动前先复制一份

---

## 10. 典型场景一：准备一次部署材料

### 推荐步骤

1. 在 `packages/` 中放发布包
2. 在 `scripts/` 中放部署脚本
3. 在 `configs/` 中放配置模板
4. 先让 AI 检查路径与内容
5. 再上传到目标主机
6. 成功后把本次材料归档

---

## 11. 典型场景二：准备一次批量巡检

### 推荐步骤

1. 在 `scripts/` 中创建巡检脚本
2. 让 AI 帮你检查脚本逻辑
3. 在单机试跑
4. 再用于批量任务
5. 输出结果可放回 `exports/` 或 `backups/`

---

## 12. 典型场景三：让 AI 代写文件

AI 很适合帮你生成和整理以下内容：

- Shell 脚本
- Python 工具脚本
- 配置模板
- Markdown 文档
- 巡检说明

### 推荐用法

- 先说明用途
- 再说明路径
- 再说明约束

示例：

- “在 `scripts/` 下写一个检查 Nginx 状态的脚本。”
- “在 `configs/nginx/` 下生成一个反向代理模板，不要覆盖旧文件。”
- “把当前会话总结成 Markdown，保存到 `exports/`。”

---

## 13. 权限说明

- 每个用户只能访问自己的 `web/fs`
- 管理员在特定场景下可协助查看或处理
- 普通用户不能越权访问别人的工作区

---

## 14. 常见问题

### 14.1 找不到文件

优先检查：

- 路径是否写成了相对路径
- 目录是否拼写错误
- 文件是否还没保存

### 14.2 上传失败

优先检查：

- `web/fs` 路径是否正确
- 目标主机是否可连接
- 远程路径是否可写
- 目标目录是否存在

### 14.3 文件太乱

建议立刻做三件事：

1. 按用途分目录
2. 删除或归档无用临时文件
3. 给脚本和模板统一命名

---

## 15. 推荐的使用习惯

- 把 `web/fs` 当作“运维工作区”，不是杂物箱
- 常用脚本固定存放，不要每次临时写
- 高风险脚本先备份、先审查、先单机测试
- 上传到远程主机前，先在 `web/fs` 中整理清楚
- 让 AI 写文件时，明确说明目录、文件名和是否允许覆盖
