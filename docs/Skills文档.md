# 毛竹 Skills 文档

本文档列出 AI 运维助手可调用的全部工具（Skills），与 `services/ai_skills.py` 中的 TOOLS 定义一致。Agent 通过 **Function Calling** 调用这些工具；接口与多端模型配置见《API 文档》与《软件设计文档》。

> **名词区分**：① 本文 **TOOLS**（内置 Function Calling）；② 数据库 **`skills` 表**（`list_prompt_skills`，全局模板）；③ **个人 Agent Skills**（`user_skills` + `SKILL.md`，注入 system prompt，见 §15，非 TOOLS）。

---

## 1. 主机与终端控制

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| list_hosts | 列出 SSH 主机；按分组筛选；**q/search** 按名称、IP、端口、描述、**用途备注 remark**、**别名 aliases**、系统类型或数字 id 搜索（limit 可限条数） | group_id?、q?、search?、limit? |
| search_hosts | 独立的主机关键词搜索：先用 SQL 在 名称/IP/端口/描述/用途/别名/标签 上预筛，再可选用正则二次精筛；可按分组/标签过滤 | query, group_id?, tag_ids?, regex?, case_sensitive?, limit? |
| get_host_detail | 获取单台主机详细信息（含 **aliases**、**remark**、host_type/host_version） | host_id |
| detect_host_os | 检测主机 OS 类型与版本并写回主机信息 | host_id |
| probe_host_capabilities | **主机能力画像**：一次 SSH 自检 OS/硬件/已装 CLI（云/AI/安全/运行时/DB 客户端）并把结构化结果格式化为 Markdown 写入主机级提示词的哨兵块内（`<!-- EDGEOPS:HOST_PROFILE v1 -->`），用户手写内容不被覆盖。默认 24 小时内有缓存则复用，传 `refresh=true` 强制重探。返回 `{cached, probed_at, os, hardware, tools_by_group, profile_markdown}` | host_id, refresh?, max_age_hours?, timeout? |
| get_host_capabilities | 读取已缓存的能力画像（结构化）。未画像返回 `profile_exists=false`，提示先调用 `probe_host_capabilities`。比 `get_host_prompt` 更适合程序消费（返回解析后的 tools 字典） | host_id |
| delegate_to_cli_agent | **子 AI 委派**：把任务交给远端主机上的另一个 AI 代理 CLI（cursor-agent / opencode / aider / claude / codex / goose / cline / llm，或 `auto` 按画像挑）一次性非交互执行；自动抓 git HEAD 前后 diff 概要（文件数/增删行/文件名/unified diff 预览）。写类 agent 默认要求 `confirmed=true`（由主 AI 先 `ask_user_choice` 让用户确认）；task scope 下视为已授权。返回 `{exit_code, duration_sec, stdout_preview, stdout_truncated, git_diff}` | host_id, agent, task, workdir?, model?, extra_args?, command_template?, env?, output_format?, timeout?, max_output_chars?, confirmed? |
| delegate_chain | **多步编排（含跨主机 + 流式进度）**：一次声明 1–10 步组成的执行链，每步是 `delegate`（子 AI）/`ssh`（命令）/`sleep`，通过 `when=always\|on_success\|on_failure` 做分支，用 `{prev_stdout}`/`{prev_stderr}`/`{prev_exit_code}`/`{prev_files_changed}` 等模板把上一步结果喂给下一步。**每步可选 `host_id` 覆盖默认主机**，实现"A 机改 → B 机测 → C 机部署"式跨主机流水线；所有涉及主机会各自做画像校验 / 访问控制 / 凭证解析 / 审计。链含写类 delegate 时要求 `confirmed=true`；task scope 视为已授权。**运行时通过 SSE 向前端实时推 `chain_step_start` / `chain_step_line` / `chain_step_end` / `chain_step_skip` 事件，不用等整条跑完就能看到进度。** 返回 `{total_steps, executed, failed_at, distinct_host_ids, steps[{..., host_id, host_label}], summary}` | host_id（默认主机）, steps[{kind, host_id?, when?, agent?, task?, command?, workdir?, ...}], stop_on_failure?, confirmed?, task_dir_name? |
| save_workflow_template | 把一条 `delegate_chain` 的 payload 保存为可复用模板。payload 里字符串字段（task/command/workdir/env.value 等）可写 `${var}` 占位符，和 chain 运行时的 `{prev_*}` 互不冲突。按 (owner_user_id, name) 唯一，重名默认拒绝，可 `overwrite=true`。返回 `{id, name, declared_variables[]}` | name, payload, description?, tags?, visibility(`private`\|`org`)?, overwrite?, kind? |
| list_workflow_templates | 列出可用编排模板（own + visibility=org 的公共模板），按 updated_at 倒序。不返回 payload 详情。返回 `{count, templates[{id, name, description, kind, tags, visibility, owner_is_me, last_run_at, run_count, updated_at}]}` | query?, include_org?, limit? |
| run_workflow_template | 跑一条已保存的模板：取 payload → 用 `variable_overrides` 做 `${var}` 替换 → 内联执行 `delegate_chain`（复用全部安全门禁与流式进度）。`dry_run=true` 只返回 `{resolved_payload, declared_variables, missing_variables, steps_preview}` 供用户预览。真跑前 `confirmed=true`；成功后 `run_count+1, last_run_at=now`。返回沿用 delegate_chain 形态并附带 `_template_id/_template_name` | template_id, variable_overrides?, host_id_override?, dry_run?, confirmed?, stop_on_failure? |
| delegate_to_edgeops_ai | **内部 AI 递归（单任务）**：起一个 毛竹 子 AI 对话（独立 system_prompt + `allowed_tools` 白名单 + 短生命周期），跑完返回最终 Markdown。不走 SSH，只消耗主 AI 的 LLM 账号。执行期间 SSE 推送 `sub_ai_step` / `sub_ai_tool` / `sub_ai_done`。硬限制递归深度=2。典型用途：写报告 / 代码 reviewer。返回 `{final_text, steps_used, duration_sec, depth, tool_calls_summary, truncated}` | task, system_prompt, allowed_tools?, max_steps?, max_depth?, timeout_sec?, context_hint? |
| delegate_sub_tasks_batch | **内部子 AI 批量并发**：一次 1–8 个子任务，`max_parallel` 默认 3（上限 5）。适合 Map-Reduce 大数据分析。SSE 推送 `sub_ai_batch_start` / `sub_ai_step` / `sub_ai_batch_end`。返回 `{total, succeeded, failed, results[{name, final_text, ...}]}` | tasks[], shared_system_prompt?, default_allowed_tools?, max_parallel?, max_steps?, timeout_sec? |
| ssh_execute | 在指定主机上执行 SSH 命令；长任务可用 detach 后台写日志，再用 poll_log 读尾部 | host_id, command?, timeout?, detach?, poll_log?, log_path?, tail_lines? |
| send_to_terminal | 向当前用户 SSH 终端注入输入（命令、控制键等）；**勿用于发送密码明文**——凭证库开启时用 send_service_password | text, slot? |
| connect_terminal | 请求前端自动连接指定主机终端 | host_id |
| list_terminals | 查询当前 AI 助手页所有控制台列表（slot、host_id、created_by、connected） | — |
| create_console | 在 AI 助手页动态创建新控制台并连接指定主机（多机协同） | host_id |
| close_console | 关闭由 AI 创建的控制台 | slot |
| create_host | 新建 SSH 主机（需管理员） | name, host, port?, credential_id? / new_credential?, description?, aliases?, remark? |
| update_host | 更新主机信息（**aliases** 传入为整表替换，**[]** 清空别名） | host_id, name? / host? / port? / credential_id? / description? / aliases? / remark? 等 |
| delete_host | 删除主机：所有者/管理员真实删除；接收分享用户调用时解除分享 | host_id |
| share_host | 分享主机给指定用户（所有者/管理员） | host_id, user_id? / username? |
| revoke_host_share | 撤销主机分享（所有者可撤销任意接收方；接收方可撤销自己） | host_id, target_user_id |
| list_host_shares | 查看某主机分享清单（分享给了谁） | host_id |
| list_received_host_shares | 查看当前用户收到的主机分享列表 | — |
| host_stats | 获取主机统计（如总数量） | — |

---

## 2. 主机知识（敏感信息）

> 保存「账户/密码/Token/私钥路径」等**敏感**信息。按 **用户 × 主机** 独立存储（主机分享给其他用户时不共用）。仅在 AI 需要时定向读取，不参与跨主机搜索。

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| get_host_knowledge | 获取指定主机的 AI 知识（账户、密码、路径等） | host_id |
| update_host_knowledge | 设置或覆盖指定主机的 AI 知识 | host_id, content |
| append_host_knowledge | 向指定主机 AI 知识末尾追加内容 | host_id, text |

---

## 2.0 服务凭证库（需 `credentials_vault_enabled`）

按 **用户** 保存 sudo/SSH/MySQL 等密码；匹配 `service + address + port + service_username`；**不绑定操作主机**。`send_service_password` 的 `host_id` 仅用于 Web 控制台注入目标。

> 存储 **sudo / 数据库 / 跳板 SSH** 等密码；与 SSH **登录**凭证（`credentials` 表）分离。密码 **不可查询**，仅 **`send_service_password`** 在 terminal / ssh_channel / local_terminal **已出现密码提示** 时注入 stdin。功能关闭时上述工具不会出现在 AI 工具列表中。

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| list_service_credentials | 列出当前用户服务凭证元数据（无 password） | service?, address?, port?, service_username? |
| add_service_credential | 新增服务凭证 | service, password?, address?, port?, service_username?, label?, linked_host_id?, linked_credential_id? |
| update_service_credential | 更新元数据或密码 | credential_id, … |
| delete_service_credential | 删除 | credential_id |
| send_service_password | 按 service/address/port/username 匹配并注入 | target, service?, address?, port?, service_username?, credential_id?, host_id?（terminal 注入目标）, slot?, channel_id? |

**sudo 流程**：`send_to_terminal` 发 sudo → `get_terminal_buffer` 确认提示 → `send_service_password`。`ssh_execute` 非交互，不能注入。

---

## 2.1 主机级 AI 提示词（规则 / 能力 / 工具链描述）

> 与主机知识分离，用于保存主机独有的**规则、工具链、目录约定、禁忌等**（非密文）。按 **用户 × 主机** 独立，主机分享给其他用户时不共用；进入主机会话时 AI 会自动读取并参考。

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| get_host_prompt | 获取指定主机的「主机级提示词」（当前用户维度） | host_id |
| update_host_prompt | 设置或覆盖指定主机的主机级提示词（Markdown） | host_id, content |
| append_host_prompt | 向指定主机的主机级提示词末尾追加内容 | host_id, text |
| search_hosts_by_prompt | 在当前用户可访问且存在主机级提示词的主机中按内容搜索；支持 group/tag 过滤、可选正则二次精筛；返回命中主机与片段，用于"找一台具备某某能力的主机" | query, group_id?, tag_ids?, regex?, case_sensitive?, limit?, snippet_chars? |

---

## 2.2 主机标签（用户私有）

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| list_host_tags | 列出当前用户的标签（与其上关联的主机） | — |
| create_host_tag | 新建一个用户私有标签 | name, color? |
| update_host_tag | 更新标签（名称 / 颜色） | tag_id, name? / color? |
| delete_host_tag | 删除标签（自动解除主机关联） | tag_id |
| set_host_tags | 将一台主机的标签整表替换为给定标签集合 | host_id, tag_ids |

---

## 2.3 主机上 `~/.edgeops` 工作区（可选辅助记忆）

> 在被管理主机当前用户家目录下维护一个 `.edgeops/` 目录，用于脚本、任务日志、规则、主机信息四类文件。**不是保存主机级规则/能力的主渠道**（首选「主机级 AI 提示词」），主要用于**脚本与任务日志**。按 `EDGEOPS_PERSIST_HOST_TYPE_WHITELIST` 白名单（默认 linux,macos,windows）开启。

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| edgeops_init_workspace | 在主机上初始化 `~/.edgeops/` 基础结构 | host_id |
| edgeops_save_script | 将脚本保存到 `~/.edgeops/scripts/` 并授予执行权限 | host_id, name, content, language? |
| edgeops_read_workspace_context | 读取主机上的 `.edgeops` 上下文概览（规则/信息/最近任务日志） | host_id |
| edgeops_append_task_log | 向本次任务的 `.edgeops/tasks/<task_dir>/log.md` 追加一段 Markdown 日志 | host_id, task_dir, note |
| edgeops_write_rule | 写入 `.edgeops/rules/` 下的规则文件（建议主机级提示词优先） | host_id, name, content |
| edgeops_write_info | 写入 `.edgeops/info/` 下的通用信息文件 | host_id, name, content |

---

## 3. 分组

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| list_host_groups | 列出所有主机分组（扁平） | — |
| get_host_groups_tree | 获取分组树（含 children、hosts）；**host_q** 过滤各组下主机（匹配名称、IP、描述、remark、aliases 等，与界面服务器树搜索语义一致） | host_q? |
| get_group_detail | 获取单个分组详情 | group_id |
| create_group | 新建分组（需管理员） | name, description?, parent_id? |
| update_group | 更新分组（需管理员） | group_id, name? / description? / parent_id? |
| delete_group | 删除分组（需管理员） | group_id |
| get_group_hosts | 获取某分组下主机列表 | group_id |
| add_hosts_to_group | 将多台主机加入分组；支持把收到分享的主机加入接收方自己的分组 | group_id, host_ids |
| remove_host_from_group | 将一台主机从分组移除（需管理员） | group_id, host_id |

---

## 4. 凭证

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| list_credentials | 列出所有凭证（脱敏） | — |
| get_credential_detail | 获取单条凭证详情（脱敏） | credential_id |
| create_credential | 新建凭证（需管理员） | type(password/key_pair), code, name, username? / password? / key_type? 等 |
| update_credential | 更新凭证（需管理员） | credential_id, code? / name? / password? 等 |
| delete_credential | 删除凭证（需管理员） | credential_id |
| cleanup_orphan_credentials | **批量清理孤立凭证**：删除所有没有被任何主机引用（`hosts.credential_id` 指向它的行数为 0）的凭证。默认 `scope="mine"` 只清自己创建的；`scope="all"` 仅管理员可用。建议先 `dry_run=true` 预览。返回 `{deleted, items[], dry_run, scope, message}` | scope?, dry_run? |
| generate_key | 生成 RSA/ECC 密钥对（需管理员） | key_type?, key_bits? |

---

## 5. 维护历史

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| list_maintenance_history | 列出维护历史，可按主机/条数筛选 | host_id?, host?, limit? |
| get_maintenance_item | 获取单条维护记录详情 | item_id |
| create_maintenance | 新建维护记录 | host, category, content? / port? / file_path? / details? |
| update_maintenance | 更新维护记录 | item_id, category? / content? / file_path? / details? |
| delete_maintenance | 删除维护记录 | item_id |

---

## 6. 最佳实践

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| get_best_practices | 查询最佳实践列表（分类/关键词） | category?, keyword? |
| add_best_practice | 添加最佳实践（用户指定或 AI 归纳） | title, content, category?, source? |
| update_best_practice | 更新最佳实践 | id, title? / category? / content? |
| delete_best_practice | 删除最佳实践 | id |

---

## 7. 终端与文件

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| get_terminal_buffer | 获取指定控制台最近输出；默认 **tail_only=true** 仅返回末尾 **max_lines**（默认 40）行；`full_output=true` 返回全文；可选 `next_poll_in_seconds` 轮询长任务（batch 末服务端 sleep；Web CoT 可唤醒/停止；集成 API 可用 `runtime-control: wake`） | slot?, full_output?, tail_only?, max_lines?, next_poll_in_seconds? |
| scp_push | 通过 SFTP 推送到主机：**content**（文本）或 **local_path**（web/fs 相对路径，支持二进制 .tgz/.zip 等）二选一 | host_id, remote_path, content? 或 local_path? |
| build_scp_transfer_script | 生成在源主机 A 上执行的 `scp -C` 推送脚本（A→B），仅生成不执行；用于跨主机直连方案 | source_host_id, source_path, target_host_id, target_path, compress? |
| transfer_file_between_hosts | 自动跨主机传输：先探测 A↔B 22 端口可达性，按 [scp, rsync, sshfs] 顺序尝试直连；全部失败时自动回退到 毛竹 `web/fs` 中转 | source_host_id, source_path, target_host_id, target_path, methods?, edgeops_base_url?, ttl_seconds?, keep_staging_for_multi_target?, auto_unpack_on_target?, transfer_timeout_seconds? |
| relay_file_between_hosts | 经 毛竹 `web/fs` 中转：A 用 curl 上传 → B 用 curl 下载；自动签发短时效 API key，完成后默认撤销 key 并删除中转文件；目录会先在 A 上打 .tgz 再传 | source_host_id, source_path, target_host_id, target_path, edgeops_base_url?, staging_path?, ttl_seconds?, keep_staging_for_multi_target?, auto_unpack_on_target?, cleanup_staging?, revoke_token_on_finish? |

### 7.0 HTTP 出站（毛竹服务器 → 外网）

从毛竹进程向外发 HTTP/HTTPS（非 SSH 主机上的 curl）。**`http_request` 支持 GET/POST 及自定义 `headers`**，一般 API 调用够用；大文件用下载/上传工具。

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| http_request | HTTP/HTTPS 请求：GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS；可设 **headers**、query、body（text/json/base64） | url, method?, **headers?**, query?, body?, body_encoding?, timeout?, max_response_bytes? |
| http_download | 从 URL 流式下载到用户 web/fs；进度条；Web 可停止取消 | url, local_path, **headers?**, session_managed?, max_bytes?, timeout? |
| http_upload | 从 web/fs 流式上传到 URL（multipart 或 raw）；进度条；可取消 | url, local_path, method?, **headers?**, field_name?, form_fields?, multipart?, max_bytes?, timeout? |

MCP 同名：`edgeops_http_request` / `edgeops_http_download` / `edgeops_http_upload`。默认禁止内网 SSRF；明文 HTTP 需 `EDGEOPS_HTTP_TOOL_ALLOW_INSECURE=true`。

| fs_list | 列出 **web/fs/当前用户名** 下某目录的文件与子目录（作用范围 per-user） | path? |
| fs_read_file | 读取 **web/fs/当前用户名** 下文本文件内容 | path |
| fs_write_file | 向 **web/fs/当前用户名** 写入文本文件 | path, content |
| fs_mkdir | 在 **web/fs/当前用户名** 中创建目录 | path |
| fs_pack_tgz | 将 **web/fs/当前用户名** 下某目录打包为 .tgz | path |
| fs_unpack_tgz | 解压 **web/fs/当前用户名** 下的 .tgz 文件 | path, dest? |
| fs_delete | 删除 **web/fs/当前用户名** 下文件或空目录 | path |
| fs_copy | 在 **web/fs/当前用户名** 下复制或移动（path → dest_dir；move=true 为移动） | path, dest_dir, move? |

### 7.1 聊天附件、溢出与成果物

> 附件落盘 `web/fs/<用户名>/chats/YYYY/MM/DD/`；仅可访问**当前用户**自己的 uuid。Office/PDF（kind=`document`）由 **MarkItDown** 转为 Markdown（`services/markitdown_convert.py`），缓存 `原文件名.extracted.md`。

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| read_chat_attachment | 读取聊天附件：**text/markdown** 返回 `content`；**image** 默认返回已缓存 `ai_description` 或 `data_url`（`force_reload` 强制原图）；**document**（及可识别的 Office/PDF）返回 MarkItDown 转换后的 Markdown（`converted_from_markitdown: true`） | uuid, max_chars?, as_data_url?, prefer_description?, force_reload? |
| save_image_description | 将图片识别结果写回附件行，后续轮次优先复用描述 | uuid, description |
| list_chat_attachments | 列出当前用户已上传附件（可按 session_id 过滤） | session_id? |
| read_chat_data | 读取工具大结果溢出文件（`[[EDGEOPS_CHAT_DATA ref=… subdir=…]]` 哨兵对应 `chats/<subdir>/spill/<ref>.data`） | spill_id, date_subdir, mode?, head_chars?, tail_chars?, range_start?, max_chars? |
| create_chat_artifact | 写入 AI 成果物（单文件或 bundle）到 `chats/日期/<id>/`；返回 `markdown_link` 供回复中插入 | title, files[{path, content, encoding?}], entry_file?, description? |
| list_chat_artifacts | 列出当前用户/会话的成果物 | session_id? |
| read_chat_artifact_file | 读回成果物内某文本文件 | uuid, path |

---

## 8. 批量操作

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| batch_create | 创建批量任务并下发：run_command / scp_push / run_script / restart。普通用户仅能对自己创建的主机/分组操作；管理员可对全部 | operation_type, scope_type, scope_value?, params |
| list_batch_operations | 列出最近批量操作记录（普通用户仅自己的） | limit? |
| get_batch_detail | 获取单次批量操作详情（含每台主机状态） | batch_id |
| batch_cancel | 取消正在运行的批量任务 | batch_id |
| batch_retry | 将批量任务中失败项重置并重试 | batch_id |
| clear_batches | 清空批量任务记录（普通用户清空自己的，管理员可清空全部） | user_id?（仅管理员可选） |

---

## 9. 本机管理（仅管理员）

在 毛竹 运行的本机上执行命令、Python 脚本、读写本机文件与目录、管理子进程；可用于 curl/wget 发网络请求、脚本自动化等。

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| local_exec | 在本机执行一条 shell 命令（如 curl、wget、dir、ls、python -c "..."） | command, timeout?, cwd? |
| local_run_script | 在本机执行 Python 代码或脚本文件；可做 HTTP 请求、读写本机文件（需本机已安装 requests 等） | code? 或 script_path?, timeout? |
| create_local_console | 在本机管理页打开一个本机终端（由 AI 创建）；仅管理员 | — |
| close_local_console | 关闭本机管理页指定槽位的本机终端（仅可关闭 AI 创建的）；仅管理员 | slot |
| local_fs_list | 列出本机目录。path 为空或 `/` 时：Windows 返回所有可用驱动器（C:、D: 等），Linux 返回根目录 `/`；全系统模式下可传绝对路径 | path? |
| local_fs_read | 读取本机文本文件 | path |
| local_fs_write | 向本机文件写入文本（覆盖） | path, content |
| local_fs_mkdir | 在本机创建目录 | path |
| local_fs_delete | 删除本机文件或空目录 | path |
| local_fs_rename | 重命名本机文件或目录 | path, new_name |
| local_fs_truncate | 将本机文件截断为指定字节长度 | path, length |
| local_fs_read_binary | 读取本机二进制文件（返回 base64） | path |
| local_fs_write_binary | 向本机写入二进制内容（base64） | path, content_base64 |
| process_start | 启动子进程，返回 pid | command, cwd?, env? |
| process_terminate | 终止托管进程 | pid |
| process_wait | 等待进程结束 | pid, timeout? |
| process_stdin_write | 向进程 stdin 写入 | pid, text |
| process_stdin_close | 关闭进程 stdin | pid |
| process_stdout_read | 读进程已缓冲的 stdout（base64） | pid |
| process_stderr_read | 读进程已缓冲的 stderr（base64） | pid |
| process_list | 列出当前托管的进程 | — |

---

## 10. 系统与 AI 配置

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| get_settings | 获取系统设置键值列表（需管理员） | — |
| update_setting | 更新系统设置项（需管理员） | key, value |
| list_logs | 查询操作日志（**普通用户仅能查自己的日志**；管理员可传 user_id 筛选） | limit?, host_id?, user_id? |
| clear_logs | 清空操作日志（普通用户清空自己的，管理员可清空全部或指定 user_id） | user_id?（仅管理员可选） |
| get_ai_config | 获取当前激活 AI 配置 + 全部模型配置组列表 profiles / active_profile_id | user_id?（仅管理员） |
| update_ai_config | 更新**当前激活**模型配置（不新建配置组） | user_id? / api_key? / base_url? / model? 等 |
| list_ai_model_profiles | 列出全部模型配置组（名称、model、是否当前等） | user_id? |
| create_ai_model_profile | **新建**模型配置组；`set_active=false` 时不切换当前模型 | name（必填）/ set_active? / provider? / base_url? / model? 等 |
| update_ai_model_profile | 按 profile_id 或 profile_name 更新指定配置组 | profile_id? / profile_name? / 各配置字段 |
| activate_ai_model_profile | **切换当前模型** | profile_id? / profile_name? |
| apply_system_ai_config_to_user | **仅管理员**：将系统默认 AI 配置（全局设置）直接写入指定用户的 AI 配置 | user_id（必填） |

---

## 10.5 通用数据处理

这些工具不修改文件或系统状态，只对输入文本/数据返回处理结果。适合在 AI 对话中快速处理正则、字符串、数学、JSON/YAML、XML/HTML 等问题；遇到超大文件或大批量数据时，仍应优先使用脚本化方式处理文件，再把摘要交给 AI。

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| regex_process | 通用正则处理：search / findall / extract / split / replace / count。支持 ignorecase、multiline、dotall、ascii flags；replace 只返回替换后的文本，不写文件 | operation, text, pattern, replacement?, flags?, max_results?, count? |
| string_process | 通用字符串处理：trim、大小写转换、replace、split、join、substring、contains、count、line_stats、base64/url 编解码、hash | operation, text?, items?, old?, new?, sep?, start?, end?, case?, algorithm? |
| math_calculate | 安全数学/科学计算：表达式 eval（仅 math 常用函数与常量）、stats 数组统计、unit_convert 常见长度/质量/力/时间/温度换算 | operation, expression?, numbers?, value?, from_unit?, to_unit? |
| data_query | JSON/YAML 数据搜索与分析：parse、summary、get_path（如 `items[0].name`）、search（key/value 递归搜索）、filter_list（简单列表过滤） | operation, data, format?, path?, query?, regex?, key?, op?, value?, max_results? |
| markup_query | XML/HTML 搜索与提取：summary、find_tags、select（HTML CSS 选择器 / XML 简单路径）、search_text、get_text、extract_attrs、extract_links | operation, data, format?, tag?, selector?, query?, regex?, attrs?, max_results? |

---

## 11. 用户与 AI 会话

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| get_me | 获取当前登录用户信息（含 site_timezone、server_time_local、server_time_utc、个人发信配置摘要 mail_config） | — |
| get_server_time | 获取站点配置时区下的当前时刻（用于"现在几点""今天日期"等时间问题） | — |
| ask_user_choice | **向用户发起可点击的选择题**（ABCD 多选、是/否同意、确认/取消、风险动作二次确认）。浏览器 UI 场景下工具返回 `ui_action`，前端渲染可点击按钮；调用方若为 OpenClaw/API 集成等**无 UI** 场景，工具改返回 `ui_capable=false` 的纯文本回退，需 AI 在随后的回复里以 `[A] …` 形式列出让用户文字回复。**定时/触发任务（task scope）下本工具会被从工具清单中移除**，避免后台任务阻塞等待。调用后 AI 必须结束本轮，等待用户下一条消息 | question, options[ {id?, label, value?, style?(default/primary/success/danger), description?} ] (≥2), allow_multiple?, allow_text?, default_id? |
| change_my_password | 当前用户自助修改自己的密码 | old_password, new_password |
| send_bind_email_code | 发送绑定邮箱验证码 | email |
| verify_bind_email | 验证并绑定邮箱 | email, code |
| unbind_email | 解绑当前用户邮箱 | — |
| list_users | 列出所有用户（需管理员） | — |
| get_user | 获取单个用户详情（需管理员） | user_id |
| create_user | 新建用户（需管理员） | username, password, display_name?, role? |
| update_user | 更新用户（需管理员） | user_id, display_name? / role? / status? |
| delete_user | 删除用户（需管理员） | user_id |
| reset_user_password | 重置用户密码（需管理员） | user_id, password |
| reset_user_system_ai_usage | 清零指定用户系统共享 Key 调用计数（需管理员） | user_id |
| admin_unlock_user | 解除指定用户的登录锁定（清空失败计数与 locked_until），需管理员 | user_id |
| list_ai_sessions | 列出当前用户 AI 聊天会话（返回会话级 `low_interaction_mode`，用于前端「低交互」/`Auto` 开关） | — |
| get_ai_session | 获取某条会话详情及消息 | session_id |
| create_ai_session | 新建 AI 聊天会话 | title? |
| update_ai_session | 更新会话标题，或更新会话级低交互开关 `low_interaction_mode` | session_id, title?, low_interaction_mode? |
| delete_ai_session | 删除会话及其消息 | session_id |
| clear_ai_sessions | 清空当前用户所有 AI 会话 | — |
| update_session_prompt | 更新或追加当前会话的会话级提示词（供「把上述要求记到会话里」等场景）。content 建议使用 Markdown 格式（## 标题、- 列表、\`代码\`）便于查看 | session_id, content, append? |
| get_session_operations | 获取当前会话的「操作序列」：仅用户要求与助手指令，不含程序输出。用于生成会话提示词或归纳最佳实践/经验时参考 | session_id, limit? |
| get_session_chat_detail | 获取当前会话聊天详情。include_tool_results=false 时同 get_session_operations；true 时含完整消息（含执行结果/日志），供分析报错或引用输出时使用 | session_id, include_tool_results?, limit? |

---

## 12. 操作帮助文档（web/aihelp）

用户只读、仅管理员可编辑。用于「如何操作」「帮助」「怎么用」等场景。

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| get_aihelp_index | 获取帮助目录 index.md 内容 | — |
| list_aihelp_files | 列出当前所有帮助文档路径 | — |
| get_aihelp_file | 读取指定帮助文档内容 | path |
| write_aihelp_file | 创建或覆盖帮助页（仅管理员） | path, content |
| update_aihelp_index | 更新 index.md 目录（仅管理员） | content |

---

## 13. 数据库 Skills 表（能力模板）

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| list_prompt_skills | 列出系统中配置的 Skills（数据库 skills 表） | — |
| get_prompt_skill | 获取单条 Skill 详情（id、code、name、description、parameters_schema） | skill_id |

---

## 14. 个人 MCP 配置（每用户）

配置存于 `user_mcp_servers`；stdio 命令在 **毛竹 服务端**启动。网页 **MCP 配置**（`/mcp-servers`）或下列工具管理。

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| list_user_mcp_servers | 列出当前用户 MCP（脱敏 env/headers） | — |
| configure_user_mcp_server | 按 name upsert 服务器 | name, transport, command/args/env 或 url/headers, enabled?, chat_enabled?, chat_scope_*? |
| import_user_mcp_config | 批量导入 mcp.json | config, overwrite?, chat_scope_*? |
| export_user_mcp_config | 导出 Cursor 风格 JSON | include_disabled?, include_edgeops_meta? |
| test_user_mcp_server | 测试连接 | name 或 server_id |
| refresh_user_mcp_tools | 刷新工具 schema 缓存 | name 或 server_id（可选） |
| delete_user_mcp_server | 删除 | name 或 server_id |

**场景**：须 `enabled` + `chat_enabled` + 对应 `chat_scope_*`。详见《API 文档》§20 场景表；**触发/定时任务**不加载。

---

## 15. 个人 Agent Skills（每用户）

须管理员开启 `skills_enabled`。文件：`web/fs/<用户名>/skills/<name>/SKILL.md`（**Cursor Agent Skills** 格式：YAML frontmatter + Markdown）。**渐进式披露**（默认）：system 仅注入各 Skill 的 `name` + `description` 目录；AI 匹配后须 `get_user_skill` 加载正文；附属文件用 `read_user_skill_file`。`disable-model-invocation: false` 或 `always-apply: true` 时内联正文。

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| list_user_skills | 列出 Skill 元数据（含 group_id/group_name） | — |
| list_user_skill_groups | 列出 Skills 分组摘要 | — |
| create_user_skill_group | 新建分组 | name |
| update_user_skill_group | 重命名分组 | group_id 或 group_name, name |
| delete_user_skill_group | 删除分组（组内 Skill 移入未分组） | group_id 或 group_name |
| assign_user_skills_to_group | 批量移入分组 | group_id/group_name, skill_names?/skill_ids?, all_ungrouped? |
| get_user_skill | 按需加载 SKILL.md 正文（含 resources 列表） | name 或 skill_id |
| read_user_skill_file | 读取 reference.md 等附属文件 | name, path |
| save_user_skill | 按 name upsert（Cursor 格式）；可设 group_name/group_id | name, content?, body?, description?, chat_scope_*?, group_name? |
| delete_user_skill | 删除 | name 或 skill_id, remove_files? |
| scan_user_skills | 扫描磁盘与库**双向同步**：导入/更新 SKILL.md 元数据；磁盘已删除或改名的 Skill 从库移除（改名=删旧行+新增行，分组/启停不继承） | — |
| export_user_skills_config | 导出 JSON 包 | include_disabled? |
| import_user_skills_config | 导入 JSON 包 | data, overwrite? |

**场景**：同 §14；新建 Skill 时 `chat_scope_integration` 默认 **false**。

---

## 16. SSH Channel 与后台任务（TTY 通道 / 触发任务 / 定时任务）

设计详见《SSH通道与后台任务设计.md》。后台任务以用户与任务 ID 隔离；SSH 通道按会话或任务边界创建，支持按行缓冲、**pending_partial / tail_text**（无换行提示）与超时关闭。Web **「SSH通道管理」** Tab 只读监视，见 `web/aihelp/ssh-channel.md`。

### SSH Channel（TTY 通道）

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| ssh_channel_create | 创建属于当前会话或指定任务的 SSH TTY 通道 | host_id, owner_type?, owner_id?, input_timeout_sec?, output_timeout_sec?, idle_close_sec? |
| ssh_channel_list | 列出 SSH 通道；`all_open=true` 列全部 open | owner_type?, owner_id?, all_open? |
| ssh_channel_info | 获取指定通道详情（主机信息、行号范围） | channel_id |
| ssh_channel_send | 向通道发送内容（含控制字符） | channel_id, content |
| ssh_channel_read_lines | 按行读；返回 **tail_text**、**pending_partial**（password 等无换行提示） | channel_id, from_line?/to_line?/last_n?/since_line? |
| ssh_channel_read_length | 按字符数读取通道输出（最近 max_chars 字符） | channel_id, max_chars? |
| ssh_channel_has_new | 是否有新输出（含 pending_partial） | channel_id, after_line? |
| ssh_channel_close | 关闭指定通道 | channel_id |
| ssh_channel_close_batch | 按 owner 或 session 批量关闭 | owner_type?, owner_id?, session_id? |
| ssh_channel_dump_output | 导出通道缓冲到 spill | channel_id, max_chars? |

### 触发任务（供定时任务发现与调用）

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| triggered_task_list | 列出当前用户所有触发任务（名称、介绍、触发条件、最后运行状态等） | — |
| triggered_task_list_exposed | 列出本用户已配置暴露接口的触发任务（仅可被定时任务发现的那部分） | — |
| triggered_task_status | 查看触发任务运行状态（是否运行中、最后运行时间与结果） | task_id?/task_name?，不传则返回全部 |
| triggered_task_get | 获取单条触发任务详情 | task_id |
| triggered_task_create | 创建触发任务 | name, content, intro?, trigger_conditions? |
| triggered_task_update | 更新触发任务 | task_id, name?/content?/intro?/trigger_conditions? |
| triggered_task_delete | 删除触发任务 | task_id?/task_name? |
| triggered_task_trigger | 触发执行一个触发任务 | task_id?/task_name?, instruction?, caller_task_id?, caller_status? |
| triggered_task_current_run_history | 查询同一触发任务当前次执行的会话式历史 | task_id, run_id? |

### 定时任务

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| scheduled_task_list | 列出当前用户的定时任务 | — |
| scheduled_task_status | 查看定时任务状态（是否运行中、最后运行时间与结果、下次运行时间） | task_id?/task_name?，不传则返回全部 |
| scheduled_task_get | 获取单条定时任务详情 | task_id |
| scheduled_task_create | 创建定时任务 | name, content, cron_expr?, enabled?（默认 true；false 仅建任务不排 cron）、**notify_email_to?**（执行结束后向这些地址发摘要，须已启用个人SMTP） |
| scheduled_task_update | 更新定时任务 | task_id, name?/content?/cron_expr?/enabled?/**notify_email_to?** |
| scheduled_task_delete | 删除定时任务及**全部执行历史与会话** | task_id?/task_name? |
| scheduled_task_current_run_history | 查询同一定时任务当前次执行的会话式历史 | task_id, run_id? |
| scheduled_task_run_now | 立即执行一次定时任务（不等到 cron 时间） | task_id?/task_name? |

### 个人发信（用户 SMTP）

与管理员全局 **settings** 中 `smtp_*` **无关**。`send_bind_email_code` / 忘记密码等仍走**系统**邮件。默认关；须 `smtp_host`、`smtp_user`、`smtp_password`、`smtp_from`、合法端口等到齐且 `mail_enabled=true` 后 `may_send_mail` 才为 true。

| 名称 | 说明 | 主要参数 |
|------|------|----------|
| get_user_mail_settings | 查看当前用户个人 SMTP 摘要（密码不返回；含 may_send_mail 等） | — |
| update_user_mail_settings | 更新个人 SMTP 与启用开关；不完整配置时不可开启发信 | mail_enabled?, smtp_host?, smtp_port?, smtp_user?, smtp_password?, smtp_from?, smtp_use_tls?, smtp_use_ssl? |
| send_email | 使用**已启用的个人 SMTP**发送纯文本邮件 | to（逗号分隔）、subject、body |

---

## Skills 与 Web API 覆盖说明

| Web 模块 | 主要接口 | Skills 覆盖情况 |
|----------|----------|-----------------|
| 认证 | /auth/me、/auth/change-password | get_me、change_my_password |
| 用户 | /users CRUD、reset-password | list_users、get_user、create_user、update_user、delete_user、reset_user_password、reset_user_system_ai_usage |
| 主机 | /hosts CRUD、execute、check-type | list_hosts、get_host_detail、create_host、update_host、delete_host、ssh_execute、detect_host_os、host_stats |
| 主机分享 | /hosts/shares/received、/hosts/shares/sent、/hosts/{host_id}/shares、/hosts/{host_id}/shares/{target_user_id} | share_host、revoke_host_share、list_host_shares、list_received_host_shares |
| 主机分组 | /host-groups CRUD、tree、hosts | list_host_groups、get_host_groups_tree、get_group_detail、create_group、update_group、delete_group、get_group_hosts、add_hosts_to_group、remove_host_from_group |
| 凭证 | /credentials CRUD、generate-key | list_credentials、get_credential_detail、create_credential、update_credential、delete_credential、cleanup_orphan_credentials、generate_key |
| 维护历史 | /maintenance-history CRUD | list_maintenance_history、get_maintenance_item、create_maintenance、update_maintenance、delete_maintenance |
| 最佳实践 | /best-practices CRUD、categories | get_best_practices、add_best_practice、update_best_practice、delete_best_practice |
| 本地文件系统 | /fs list/read/write/mkdir/upload/download/pack-tgz/unpack-tgz/delete/copy | fs_list、fs_read_file、fs_write_file、fs_mkdir、fs_pack_tgz、fs_unpack_tgz、**fs_delete**、**fs_copy**（上传为二进制可经 fs_write_file 写文本或由用户界面操作） |
| 远程文件系统 | /remote-fs list/read/download/mkdir/upload/write/delete/rename/copy | **scp_push** 对应写/上传内容；列目录、读文件、删除/重命名/复制等可由 **ssh_execute** 执行相应命令实现，无独立 remote_fs_* 工具 |
| 终端 | /terminal buffer/list/send、控制台创建/关闭 | get_terminal_buffer、send_to_terminal、list_terminals、create_console、close_console、connect_terminal |
| 批量操作 | /batch POST/GET/cancel/retry/clear/export | batch_create、list_batch_operations、get_batch_detail、batch_cancel、batch_retry、**clear_batches** |
| 本机管理 | /local fs/list\|read\|write\|mkdir、execute、run-script、sessions、ws、buffer | **local_exec**、**local_run_script**、**create_local_console**、**close_local_console**、**local_fs_list/read/write/mkdir/delete/rename/truncate/read_binary/write_binary**、**process_start/terminate/wait/stdin_write/stdin_close/stdout_read/stderr_read/process_list**（仅管理员） |
| AI 助手 | /ai config/sessions/chat、prompt、summarize-title、profiles | get_ai_config、update_ai_config、**list/create/update/activate_ai_model_profile**、list_ai_sessions、get_ai_session、create_ai_session、update_ai_session、delete_ai_session、clear_ai_sessions、**update_session_prompt**、**get_session_operations**、**get_session_chat_detail** |
| 聊天附件 | /ai/attachments POST/GET/DELETE、bind | read_chat_attachment、save_image_description、list_chat_attachments |
| AI 成果物 | /ai/artifacts GET/download/file、bind、DELETE | create_chat_artifact、list_chat_artifacts、read_chat_artifact_file |
| 系统设置与日志 | /settings、/logs、/logs/export、/logs/clear | get_settings、update_setting、list_logs、**clear_logs** |
| 操作帮助文档 | web/aihelp（index、各 .md） | get_aihelp_index、list_aihelp_files、get_aihelp_file、write_aihelp_file、update_aihelp_index |
| Skills 表 | /skills GET | list_prompt_skills、get_prompt_skill |
| 个人 MCP | /api/user-mcp-servers CRUD、import、export、test、refresh-tools | list/configure/import/export/test/refresh/delete_user_mcp_* |
| 个人 Agent Skills | /api/user-skills（须 skills_enabled） | list/get/save/delete/scan_user_skills |
| SSH Channel | /api/ssh-channel REST + WS；Web「SSH通道管理」只读 Tab | ssh_channel_create/list/info/send/read_lines/read_length/has_new/close/close_batch/dump_output |
| 触发任务 | /api/triggered-tasks CRUD、exposed、trigger、runs、runs/{run_id}/messages | triggered_task_list、triggered_task_list_exposed、triggered_task_status、triggered_task_get、triggered_task_create、triggered_task_update、triggered_task_delete、triggered_task_trigger、triggered_task_current_run_history |
| 定时任务 | /api/scheduled-tasks CRUD、runs、run-now、triggered-list、runs/{run_id}/messages | scheduled_task_list、scheduled_task_status、scheduled_task_get、scheduled_task_create、scheduled_task_update、scheduled_task_delete、scheduled_task_current_run_history、scheduled_task_run_now |
| 用户发信 | /api/user-mail-config GET/PUT | get_user_mail_settings、update_user_mail_settings、send_email（另：send_bind_email_code / verify_bind_email / unbind_email 为账户邮箱绑定，走**系统**SMTP） |

**提示词**：运维助手 system prompt（`api/ai_agent.py` 中 `_build_system_prompt`）已列举主机与终端控制（含 list_terminals、create_console、close_console）、主机知识、本地文件系统（含 fs_delete、fs_copy）、批量任务（含 batch_cancel、batch_retry、clear_batches）、操作日志（list_logs、clear_logs）、**本机管理**（local_exec、local_run_script、local_fs_*、process_*，仅管理员）、最佳实践、**操作帮助文档**（get_aihelp_index、list_aihelp_files、get_aihelp_file、write_aihelp_file、update_aihelp_index）、**会话提示词**（update_session_prompt；生成或归纳时建议先调用 get_session_operations 获取仅用户与助手指令，需要详细输出时再调用 get_session_chat_detail(include_tool_results=true)）等能力，并强调所有执行类操作必须经 tool_call 完成。对于大量文本/日志/CSV/JSON 或大批数据，提示词要求优先用 shell / Python / PowerShell 脚本处理；涉及批量修改、覆盖或删除时，需先评估备份、dry-run 与用户确认。

---

## API 与配置说明

- **GET /api/ai/skills**：返回上述工具的名称与描述列表，与 Agent 实际使用的 TOOLS 一致。
- **GET /api/skills**、**GET /api/skills/{skill_id}**：读写数据库 `skills` 表，用于扩展/提示词类技能，与 TOOLS 可并存。
- **GET/POST /api/user-skills**：每用户 Agent Skills（须 `skills_enabled`）；与上表全局 `skills` 表、内置 TOOLS 三者并存。
- AI 模型与接口：支持在「模型配置」中选择**模型类型**（阿里云 DashScope / Ollama / OpenAI）或自定义地址与模型；可配置**上下文长度**（字符数上限，0=不限制，支持到 8MB，前端可手工输入）。通过 `config.py`（MODEL_TYPES、CONTEXT_SIZE_OPTIONS、CONTEXT_SIZE_MAX）与 `services/llm_adapter.py` 统一适配；详见《软件设计文档》第 6 节与侧栏「模型配置」页。

---

## 外部集成（OpenClaw / Hermes / MCP）

网页 AI 使用的 TOOLS（上文 §1–§12）在 **毛竹 服务端** 执行。外部智能体另有专用通道：

| 通道 | 说明 | 文档 |
|------|------|------|
| **claw-ops** | OpenClaw 插件，**22 核心 + manifest 动态扩展 + invoke**（v1.1+；baseline 43） | [claw-ops/README.md](../claw-ops/README.md) |
| **claw-skills** | Hermes REST 技能 + Token 配置 | [claw-skills/README.md](../claw-skills/README.md) |
| **edgeops MCP** | 内置 FastMCP，**47** 工具；默认 `http://<host>:<port>/mcp`；含编排 ops | [services/edgeops_mcp/README.md](../services/edgeops_mcp/README.md) |
| **集成 REST** | `POST /integration/ops-chat/complete`、`/integration/mcp/*` 等 | [API文档.md](API文档.md) §16–§18 |

总览：[外部集成与ClawOps.md](外部集成与ClawOps.md) · 用户帮助：[web/aihelp/external-integration.md](../web/aihelp/external-integration.md)

鉴权：系统设置签发的 **`eop_…` API Token** 或 JWT，`Authorization: Bearer`。
