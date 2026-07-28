import { DEFAULT_EDGEOPS_BASE_URL } from "./client.js";



/**

 * 注入到 Agent 系统提示（prependSystemContext），强调 ClawOps / Moso 运维能力走 edgeops_* 远程工具，

 * 不要求、也不应在 OpenClaw 本机 exec/curl 调 Moso。

 */

export function buildClawOpsPrependSystemContext(baseUrl: string): string {

  const root = (baseUrl || DEFAULT_EDGEOPS_BASE_URL).trim();

  return [

    "## ClawOps（claw-ops）· Moso 运维插件",

    `- **OpenClaw 配置（前提）**：须在 \`openclaw.json\` 中启用本插件并填写 **必填** 的 \`plugins.entries.claw-ops.config.accessToken\`（Moso JWT 或 \`eop_\`），**可选/可改** 的 \`config.baseUrl\`（默认 \`${root}\`）；\`plugins.allow\` 须含 \`claw-ops\`；使用 \`tools.profile: coding\` 时 **还须** \`tools.alsoAllow\` 含 \`claw-ops\`。缺任一项则 \`edgeops_*\` 不可用或不出现在工具列表。详见插件包内 \`openclaw.claw-ops.example.json\`。`,

    `- **执行流（无 Moso Web 页逻辑）**：ClawOps 只在 **OpenClaw Gateway / 插件进程** 内运行；工具通过 **HTTP 直接访问 Moso REST API**。**不依赖**浏览器 SSH 终端 UI。`,

    `- **关键词**：运维 / 主机 / 排障 / 变更 / 配置 / SSH → 优先 \`edgeops_*\`，禁止本机 exec/curl 打 Moso。`,

    `- **资产解析**：\`edgeops_search_hosts\` / \`edgeops_search_hosts_by_prompt\` / \`edgeops_get_host_prompt\` → 解析 host_id。`,

    `- **短命令**：优先 \`edgeops_ssh_execute\`（长任务 detach + poll_log）；勿为单条命令走 ops_chat。`,

    `- **非交互远程命令**：复杂多步仍可用 \`edgeops_ops_chat\`；简单命令用 \`edgeops_ssh_execute\`。`,

    `- **交互式 SSH（无界面）**：sudo 密码、vi、多步向导、Ctrl+C → **必须用** \`edgeops_ssh_channel_*\` 管道：create → send → read_lines/has_new → close；**禁止**用本机 shell 模拟 SSH。`,

    `- **edgeops_ssh_channel_create**：建 TTY 通道；传 \`session_id\`（与 ops_chat 一致）绑定会话；默认 **600s** 无读写自动关。`,

    `- **edgeops_ssh_channel_list(all_open=true)**：列出全部 open 通道（含 IP/别名/用途/提示词摘要）。`,

    `- **edgeops_ssh_channel_send / read_lines / read / has_new / dump / close / close_batch**：读写管道；content 支持 <Ctrl+X>、<Esc>、方向键、<F1> 等占位符；大输出 spill 后用 **edgeops_read_chat_data** 分段读全量。`,

    `- **服务凭证（需 credentials_vault_enabled）**：password 提示 → **优先 edgeops_send_service_password**；无凭证且用户/知识库有密码时 read 确认后可 channel_send 密码+<Enter>。`,

    `- **edgeops_ops_chat**：复杂编排仍可用集成 Agent；简单交互优先直连 ssh_channel 工具链。`,

    `- Bearer 仅用插件配置；禁止在命令或回复里拼接 \`eop_\`。`,

  ].join("\n");

}


