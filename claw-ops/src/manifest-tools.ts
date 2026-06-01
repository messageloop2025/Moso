import { Type } from "@sinclair/typebox";

import type { createMosoClient } from "./client.js";
import {
  CLAW_OPS_PLUGIN_VERSION,
  FALLBACK_EXTENDED_TOOLS,
  type ClawOpsManifest,
  type ManifestToolDef,
} from "./manifest-fallback.js";

type ClientFactory = ReturnType<typeof createMosoClient>;
type OkResult = (data: unknown) => {
  content: { type: "text"; text: string }[];
  details: unknown;
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type PluginApi = { registerTool: (tool: any) => void };

let pluginApi: PluginApi | null = null;
let getClientRef: (() => ClientFactory) | null = null;
let okResultRef: OkResult | null = null;
const registeredExtendedToolNames = new Set<string>();

let cachedSystemPrompt: string | null = null;
let cachedCapabilitiesVersion: string | null = null;
let updateNotice: string | null = null;

export function getCachedSystemPrompt(): string | null {
  return cachedSystemPrompt;
}

export function getUpdateNotice(): string | null {
  return updateNotice;
}

export function getCachedCapabilitiesVersion(): string | null {
  return cachedCapabilitiesVersion;
}

export function getRegisteredExtendedToolNames(): ReadonlySet<string> {
  return registeredExtendedToolNames;
}

/** 保存 register 上下文，供 manifest 动态 registerTool。 */
export function initExtendedToolRegistry(
  api: PluginApi,
  getClient: () => ClientFactory,
  okResult: OkResult,
): void {
  pluginApi = api;
  getClientRef = getClient;
  okResultRef = okResult;
}

function registerOneTool(
  api: PluginApi,
  getClient: () => ClientFactory,
  okResult: OkResult,
  tool: ManifestToolDef,
) {
  api.registerTool({
    name: tool.name,
    label: tool.label,
    description: tool.description,
    parameters: Type.Unsafe(tool.parameters_schema),
    async execute(_id: string, params: Record<string, unknown>) {
      const data = await getClient().invokeTool(tool.name, params ?? {});
      return okResult(data);
    },
  });
}

/** 按 manifest 增量注册尚未出现的扩展工具；返回本次新增数量。 */
export function syncExtendedToolsFromManifest(tools: ManifestToolDef[]): number {
  if (!pluginApi || !getClientRef || !okResultRef) return 0;
  let added = 0;
  for (const tool of tools) {
    const name = tool?.name?.trim();
    if (!name || registeredExtendedToolNames.has(name)) continue;
    registerOneTool(pluginApi, getClientRef, okResultRef, tool);
    registeredExtendedToolNames.add(name);
    added += 1;
  }
  return added;
}

export function registerInvokeTool(
  api: PluginApi,
  getClient: () => ClientFactory,
  okResult: OkResult,
) {
  api.registerTool({
    name: "edgeops_invoke",
    label: "Moso · 通用调用",
    description:
      "**ClawOps 扩展**：按名称调用 Moso 集成工具（POST /api/integration/claw-ops/invoke）。" +
      "manifest 尚未同步的新工具可先用本入口；Gateway 启动或 ping 后会自动注册具名工具。",
    parameters: Type.Object({
      tool: Type.String({ description: "edgeops_* 工具名" }),
      arguments: Type.Optional(
        Type.Record(Type.String(), Type.Unknown(), {
          description: "工具参数字典",
        }),
      ),
    }),
    async execute(_id: string, params: { tool: string; arguments?: Record<string, unknown> }) {
      const data = await getClient().invokeTool(
        params.tool,
        params.arguments ?? {},
      );
      return okResult(data);
    },
  });
}

function applyManifestMetadata(manifest: ClawOpsManifest): void {
  if (manifest.system_prompt?.prepend_markdown) {
    cachedSystemPrompt = manifest.system_prompt.prepend_markdown;
  }
  if (manifest.capabilities_version) {
    cachedCapabilitiesVersion = manifest.capabilities_version;
  }
  const uc = manifest.update_check;
  if (uc?.needs_update || uc?.incompatible) {
    updateNotice =
      `> **ClawOps 更新提示**：${uc.message ?? "建议升级 claw-ops 插件"} ` +
      `(当前 ${CLAW_OPS_PLUGIN_VERSION}，推荐 ${uc.recommended_version ?? "?"})`;
  } else {
    updateNotice = null;
  }
}

export async function refreshClawOpsManifest(
  getClient: () => ClientFactory,
  baseUrl: string,
): Promise<ClawOpsManifest | null> {
  try {
    const client = getClient();
    const manifest = await client.getClawOpsManifest({
      base_url: baseUrl,
      plugin_version: CLAW_OPS_PLUGIN_VERSION,
      capabilities_version: cachedCapabilitiesVersion ?? undefined,
    });
    applyManifestMetadata(manifest);
    if (manifest.extended_tools?.length) {
      syncExtendedToolsFromManifest(manifest.extended_tools);
    }
    return manifest;
  } catch {
    return null;
  }
}

/** Gateway 启动：拉 manifest 注册扩展工具；失败时用本地 fallback。 */
export async function bootstrapExtendedTools(
  getClient: () => ClientFactory,
  baseUrl: string,
): Promise<void> {
  const manifest = await refreshClawOpsManifest(getClient, baseUrl);
  if (manifest === null || !manifest.extended_tools?.length) {
    syncExtendedToolsFromManifest(FALLBACK_EXTENDED_TOOLS);
  }
}

export function scheduleManifestRefresh(
  getClient: () => ClientFactory,
  baseUrl: string,
) {
  void refreshClawOpsManifest(getClient, baseUrl);
}
