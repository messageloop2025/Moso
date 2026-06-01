import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { Type } from "@sinclair/typebox";

import { createMosoClient, DEFAULT_EDGEOPS_BASE_URL } from "./client.js";
import { shouldBlockLocalMosoShell } from "./edgeops-exec-guard.js";
import { OPENCLAW_DOWNSTREAM_FORMAT_BLOCK } from "./openclaw-output-policy.js";
import { buildClawOpsPrependSystemContext } from "./openclaw-system-prompt.js";
import { registerSshChannelTools } from "./ssh-channel-tools.js";
import {
  bootstrapExtendedTools,
  getCachedSystemPrompt,
  getUpdateNotice,
  initExtendedToolRegistry,
  refreshClawOpsManifest,
  registerInvokeTool,
  syncExtendedToolsFromManifest,
} from "./manifest-tools.js";
import { FALLBACK_EXTENDED_TOOLS } from "./manifest-fallback.js";

/** 安装/启动时可无 token；调用工具前再校验。 */
function resolvePluginConfig(raw: Record<string, unknown> | undefined): {
  baseUrl: string;
  accessToken?: string;
  /** 在 ops-chat 消息末尾附加 OpenClaw 展示能力说明（默认 true） */
  appendOpenClawUiHints: boolean;
  /** 拦截本机 exec/curl 等对 Moso 的 HTTP（默认 true） */
  blockLocalMosoExec: boolean;
} {
  const rawBase = raw?.baseUrl;
  const baseUrl =
    typeof rawBase === "string" && rawBase.trim()
      ? rawBase.trim()
      : DEFAULT_EDGEOPS_BASE_URL;
  const accessToken = raw?.accessToken;
  const token =
    typeof accessToken === "string" && accessToken.trim()
      ? accessToken.trim()
      : undefined;
  const appendRaw = raw?.appendOpenClawUiHints;
  const appendOpenClawUiHints =
    appendRaw === undefined || appendRaw === null
      ? true
      : Boolean(appendRaw);
  const blockRaw = raw?.blockLocalMosoExec;
  const blockLocalMosoExec =
    blockRaw === undefined || blockRaw === null ? true : Boolean(blockRaw);
  return {
    baseUrl,
    accessToken: token,
    appendOpenClawUiHints,
    blockLocalMosoExec,
  };
}

function clientOrThrow(cfg: ReturnType<typeof resolvePluginConfig>) {
  if (!cfg.accessToken) {
    throw new Error(
      "claw-ops: 请在 OpenClaw 配置的 plugins.entries.claw-ops.config 中设置 accessToken（Moso JWT 或 eop_...）",
    );
  }
  return createMosoClient({
    baseUrl: cfg.baseUrl,
    accessToken: cfg.accessToken,
  });
}

function jsonText(data: unknown): string {
  return JSON.stringify(data, null, 2);
}

function okResult(data: unknown) {
  return {
    content: [{ type: "text" as const, text: jsonText(data) }],
    details: data,
  };
}

export default definePluginEntry({
  id: "claw-ops",
  name: "Moso",
  description:
    "**ClawOps (claw-ops) — Moso 运维 / ops for OpenClaw; no Moso Web UI required.** Tools run inside the Gateway and call Moso HTTP APIs only (no browser terminal / page logic). Host inventory, health, ops chat → **edgeops_***. **Never** exec/curl Moso; Bearer in plugin config. **edgeops_ops_chat** = remote Moso integration ops agent.",
  register(api) {
    const cfg = resolvePluginConfig(
      api.pluginConfig as Record<string, unknown> | undefined,
    );

    api.on("before_prompt_build", () => {
      const remote = getCachedSystemPrompt();
      const notice = getUpdateNotice();
      const base =
        remote ?? buildClawOpsPrependSystemContext(cfg.baseUrl);
      const prepend = notice ? `${notice}\n\n${base}` : base;
      return { prependSystemContext: prepend };
    });

    api.on("before_tool_call", (event) => {
      if (!cfg.blockLocalMosoExec) {
        return;
      }
      if (
        shouldBlockLocalMosoShell({
          toolName: event.toolName,
          params: event.params,
          baseUrl: cfg.baseUrl,
        })
      ) {
        return {
          block: true,
          blockReason:
            "ClawOps（claw-ops）/ Moso 运维请走插件工具：edgeops_search_hosts、edgeops_search_hosts_by_prompt、edgeops_list_host_tags、edgeops_get_host_prompt、edgeops_ops_chat 等；勿在本机 exec/curl 请求 Moso。",
        };
      }
    });

    api.registerTool({
      name: "edgeops_gateway_ping",
      label: "Moso 运维 · 探活",
      description:
        "**ClawOps / Moso ops**：检查 Moso 服务是否可达（GET /api/version）。运维排障前先 ping。**禁止**用 exec / curl / Invoke-RestMethod 打 /api/version — 仅用本工具。",
      parameters: Type.Object({}),
      async execute(_id) {
        const data = await clientOrThrow(cfg).getVersion();
        if (cfg.accessToken) {
          void refreshClawOpsManifest(() => clientOrThrow(cfg), cfg.baseUrl);
        }
        return okResult(data);
      },
    });

    api.registerTool({
      name: "edgeops_list_hosts",
      label: "Moso 运维 · 主机列表",
      description:
        "**ClawOps — Moso 资产运维 / 主机运维**：分页列出 Moso 管理的主机（GET /api/hosts）。适合全量盘点。 " +
        "若用户提到具体名称/别名/标签/服务名词，**优先** `edgeops_search_hosts` 或 `edgeops_search_hosts_by_prompt` 解析 host_id。 " +
        "**Forbidden**: exec / curl / fetch …/api/hosts。",
      parameters: Type.Object({
        page: Type.Optional(
          Type.Integer({
            minimum: 1,
            description: "Page number (default 1).",
          }),
        ),
        page_size: Type.Optional(
          Type.Integer({
            minimum: 1,
            maximum: 100,
            description: "Page size (default 100, max 100).",
          }),
        ),
      }),
      async execute(_id, params) {
        const data = await clientOrThrow(cfg).listHosts({
          page: params.page,
          page_size: params.page_size,
        });
        return okResult(data);
      },
    });

    api.registerTool({
      name: "edgeops_search_hosts",
      label: "Moso 运维 · 主机检索",
      description:
        "**ClawOps — 名词 → 主机**：按名称、IP、**别名 aliases**、**标签 tag_names**、用途 remark 等检索（GET /api/hosts/search）。 " +
        "用户口语称呼某台机、或提到环境/项目名时，**先**用本工具解析 host_id，再 `edgeops_ops_chat(host_id=…)`。可配合 `edgeops_list_host_tags` 拿 tag_ids。",
      parameters: Type.Object({
        query: Type.String({
          description: "Search keyword: host name, IP, alias, tag name, remark, etc.",
        }),
        tag_ids: Type.Optional(
          Type.Array(Type.Integer(), {
            description: "Optional tag IDs (match any). From edgeops_list_host_tags.",
          }),
        ),
        group_id: Type.Optional(Type.Integer({ description: "Optional host group id." })),
        limit: Type.Optional(
          Type.Integer({ minimum: 1, maximum: 200, description: "Max results (default 50)." }),
        ),
      }),
      async execute(_id, params) {
        const data = await clientOrThrow(cfg).searchHosts({
          query: params.query,
          tag_ids: params.tag_ids,
          group_id: params.group_id,
          limit: params.limit,
        });
        return okResult(data);
      },
    });

    api.registerTool({
      name: "edgeops_search_hosts_by_prompt",
      label: "Moso 运维 · 提示词检索主机",
      description:
        "**ClawOps — 名词 → 主机（按主机级提示词）**：在 Moso 各自主机 AI 提示词中搜索关键字（GET …/integration/hosts/search-by-prompt）。 " +
        "用于「网关」「Redis 主库」「装了 docker」等写在提示词里的能力/服务映射。命中后可用 `edgeops_get_host_prompt` 读全文，再 `edgeops_ops_chat(host_id=…)`。",
      parameters: Type.Object({
        query: Type.String({
          description: "Keyword to search inside per-host AI prompts.",
        }),
        tag_ids: Type.Optional(
          Type.Array(Type.Integer(), { description: "Optional tag IDs to narrow scope." }),
        ),
        group_id: Type.Optional(Type.Integer({ description: "Optional host group id." })),
        limit: Type.Optional(
          Type.Integer({ minimum: 1, maximum: 100, description: "Max results (default 30)." }),
        ),
      }),
      async execute(_id, params) {
        const data = await clientOrThrow(cfg).searchHostsByPrompt({
          query: params.query,
          tag_ids: params.tag_ids,
          group_id: params.group_id,
          limit: params.limit,
        });
        return okResult(data);
      },
    });

    api.registerTool({
      name: "edgeops_get_host",
      label: "Moso 运维 · 主机详情",
      description:
        "GET /api/hosts/{host_id} — 单台主机详情（name、IP、aliases、tags、remark 等）。解析 host_id 后确认资产信息。",
      parameters: Type.Object({
        host_id: Type.Integer({ description: "Moso host id." }),
      }),
      async execute(_id, params) {
        const data = await clientOrThrow(cfg).getHost(params.host_id);
        return okResult(data);
      },
    });

    api.registerTool({
      name: "edgeops_get_host_prompt",
      label: "Moso 运维 · 主机提示词",
      description:
        "GET /api/ai/hosts/{host_id}/prompt — 读取当前用户在指定主机下的**主机级 AI 提示词**（规则/能力/服务映射）。 " +
        "调 `edgeops_ops_chat` 前若需确认约定，先用本工具或 search_hosts_by_prompt。",
      parameters: Type.Object({
        host_id: Type.Integer({ description: "Moso host id." }),
      }),
      async execute(_id, params) {
        const data = await clientOrThrow(cfg).getHostPrompt(params.host_id);
        return okResult(data);
      },
    });

    api.registerTool({
      name: "edgeops_list_host_tags",
      label: "Moso 运维 · 标签列表",
      description:
        "GET /api/host-tags — 当前用户的私有主机标签（id、name、host_count）。用于 search_hosts 的 tag_ids 参数。",
      parameters: Type.Object({}),
      async execute(_id) {
        const data = await clientOrThrow(cfg).listHostTags();
        return okResult(data);
      },
    });

    api.registerTool({
      name: "edgeops_host_alive",
      label: "Moso 运维 · 主机探活",
      description:
        "GET /api/hosts/{host_id}/alive — TCP 探测 SSH 端口是否可达（约 3s 超时）。排障前可先探活。",
      parameters: Type.Object({
        host_id: Type.Integer({ description: "Moso host id." }),
      }),
      async execute(_id, params) {
        const data = await clientOrThrow(cfg).hostAlive(params.host_id);
        return okResult(data);
      },
    });

    api.registerTool({
      name: "edgeops_host_stats",
      label: "Moso 运维 · 资产统计",
      description: "GET /api/hosts/stats — 当前用户可见的主机总数概览。",
      parameters: Type.Object({}),
      async execute(_id) {
        const data = await clientOrThrow(cfg).hostStats();
        return okResult(data);
      },
    });

    api.registerTool({
      name: "edgeops_search_best_practices",
      label: "Moso 运维 · 最佳实践",
      description:
        "GET /api/best-practices — 按 keyword/category 检索最佳实践。执行安装/配置/排障前可先查，再把结论写入 edgeops_ops_chat 的 message。",
      parameters: Type.Object({
        keyword: Type.Optional(Type.String({ description: "Search in title/content/category." })),
        category: Type.Optional(Type.String({ description: "Exact category filter." })),
        page: Type.Optional(Type.Integer({ minimum: 1, description: "Page (default 1)." })),
        page_size: Type.Optional(
          Type.Integer({ minimum: 1, maximum: 100, description: "Page size (default 20)." }),
        ),
      }),
      async execute(_id, params) {
        const data = await clientOrThrow(cfg).searchBestPractices({
          keyword: params.keyword,
          category: params.category,
          page: params.page,
          page_size: params.page_size,
        });
        return okResult(data);
      },
    });

    api.registerTool({
      name: "edgeops_ops_chat",
      label: "Moso 运维 · ops 对话",
      description:
        "**ClawOps — Moso 运维 / ops**：用自然语言把 **运维、排障、变更、配置、上线协助** 交给 Moso **集成运维 Agent**（POST …/ops-chat/complete）。服务端会按别名/标签/主机提示词/会话约定解析用户名词并映射到 host_id，再执行工具链；返回面向 OpenClaw 的 Markdown。 " +
        "用户说「帮我看下服务」「改配置」「SSH/主机问题」「运维怎么做」等 — **优先**本工具而非本机 shell。 " +
        "**Forbidden**: exec/curl POST integration。**Reuse** session_id 多轮；新开会话若已明确目标主机请传 host_id（可先 edgeops_list_hosts）。勿泄露 token；appendOpenClawUiHints 可附加展示约束。",
      parameters: Type.Object({
        message: Type.String({
          description:
            "运维 / ops 需求原文：交给 Moso 集成 Agent 的具体目标（排障、变更、配置、主机范围等）。插件可自动附加 OpenClaw 展示约束。",
        }),
        session_id: Type.Optional(
          Type.Integer({
            description:
              "Previous session_id from this tool's response to continue the same integration conversation.",
          }),
        ),
        host_id: Type.Optional(
          Type.Integer({
            description:
              "When starting a new session, bind Moso integration context to this host id so host-level prompts/knowledge auto-inject. Resolve via edgeops_list_hosts (name/IP/aliases/tags/remark) when the user names a machine or service.",
          }),
        ),
        skip_secondary_assistant: Type.Optional(
          Type.Boolean({
            description:
              "Default true: single agent round style (recommended for OpenClaw). Set false to allow Moso secondary assistant rounds if configured.",
          }),
        ),
        attachment_uuids: Type.Optional(
          Type.Array(Type.String(), {
            description:
              "Optional attachment UUIDs from Moso POST /api/ai/attachments for this user message round.",
          }),
        ),
        ui_locale: Type.Optional(
          Type.String({
            description: "Optional BCP-47 UI locale (e.g. zh-CN, en) for reply language fallback.",
          }),
        ),
      }),
      async execute(_id, params) {
        let message = (params.message ?? "").trim();
        if (cfg.appendOpenClawUiHints && message) {
          message = `${message}\n\n${OPENCLAW_DOWNSTREAM_FORMAT_BLOCK}`;
        }
        const data = await clientOrThrow(cfg).opsChatComplete({
          message,
          session_id: params.session_id,
          host_id: params.host_id,
          skip_secondary_assistant: params.skip_secondary_assistant,
          attachment_uuids: params.attachment_uuids,
          ui_locale: params.ui_locale,
        });
        return okResult(data);
      },
    });

    registerSshChannelTools(api, () => clientOrThrow(cfg), okResult);
    initExtendedToolRegistry(api, () => clientOrThrow(cfg), okResult);
    registerInvokeTool(api, () => clientOrThrow(cfg), okResult);
    if (cfg.accessToken) {
      void bootstrapExtendedTools(() => clientOrThrow(cfg), cfg.baseUrl);
    } else {
      syncExtendedToolsFromManifest(FALLBACK_EXTENDED_TOOLS);
    }
  },
});
