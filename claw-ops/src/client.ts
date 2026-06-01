/** Moso HTTP API client for the OpenClaw plugin. */

/** OpenClaw 未配置 `baseUrl` 时使用的默认 Moso 根地址；可通过插件配置覆盖。 */
export const DEFAULT_EDGEOPS_BASE_URL = "https://ops.pinglan.cc";

export type ClawOpsPluginConfig = {
  baseUrl: string;
  accessToken: string;
};

function normalizeBaseUrl(url: string): string {
  return url.replace(/\/+$/, "");
}

/** 与 Moso httpx read=300s 对齐，略留余量。 */
const DEFAULT_FETCH_TIMEOUT_MS = 330_000;
const INTEGRATION_CLIENT = "openclaw";

export type OpsChatCompleteResponse = {
  success?: boolean;
  reply?: string;
  session_id?: number;
  error?: string;
  note?: string;
};

export type ListHostsResponse = {
  success?: boolean;
  hosts?: unknown[];
  total?: number;
  page?: number;
  page_size?: number;
};

export type SearchHostsResponse = {
  success?: boolean;
  query?: string;
  count?: number;
  hosts?: unknown[];
};

export type SearchHostsByPromptResponse = SearchHostsResponse & {
  regex?: string;
  group_id?: number | null;
  tag_ids?: number[];
};

function appendIntList(qs: URLSearchParams, key: string, ids?: number[]) {
  if (!ids?.length) return;
  for (const id of ids) {
    qs.append(key, String(id));
  }
}

export function createMosoClient(config: ClawOpsPluginConfig) {
  const base = normalizeBaseUrl(config.baseUrl);
  const token = config.accessToken;

  async function requestJson<T>(
    path: string,
    init?: RequestInit,
    options?: { timeoutMs?: number },
  ): Promise<T> {
    const url = `${base}${path.startsWith("/") ? path : `/${path}`}`;
    const timeoutMs = options?.timeoutMs ?? DEFAULT_FETCH_TIMEOUT_MS;
    const ownSignal =
      typeof AbortSignal !== "undefined" && "timeout" in AbortSignal
        ? AbortSignal.timeout(timeoutMs)
        : undefined;
    const signal =
      init?.signal !== undefined ? init.signal : ownSignal;
    const res = await fetch(url, {
      ...init,
      ...(signal !== undefined ? { signal } : {}),
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-EdgeOps-Client": INTEGRATION_CLIENT,
        ...(init?.headers as Record<string, string> | undefined),
      },
    });
    const text = await res.text();
    if (!res.ok) {
      throw new Error(
        `Moso HTTP ${res.status}: ${text.slice(0, 1_000) || res.statusText}`,
      );
    }
    if (!text) return {} as T;
    try {
      return JSON.parse(text) as T;
    } catch {
      throw new Error(`Moso returned non-JSON (${text.slice(0, 280)}…)`);
    }
  }

  return {
    async getVersion(): Promise<unknown> {
      return requestJson("/api/version", { method: "GET" }, { timeoutMs: 15_000 });
    },

    /**
     * 列出当前令牌可见的主机（GET /api/hosts），支持分页。
     */
    async listHosts(params?: {
      page?: number;
      page_size?: number;
    }): Promise<ListHostsResponse> {
      const page = params?.page ?? 1;
      const page_size = Math.min(100, Math.max(1, params?.page_size ?? 100));
      const q = new URLSearchParams({
        page: String(page),
        page_size: String(page_size),
      });
      return requestJson<ListHostsResponse>(
        `/api/hosts?${q}`,
        { method: "GET" },
        { timeoutMs: 60_000 },
      );
    },

    /**
     * 按名称/IP/别名/标签名/remark 等检索主机（GET /api/hosts/search）。
     */
    async searchHosts(params: {
      query: string;
      group_id?: number;
      tag_ids?: number[];
      regex?: string;
      case_sensitive?: boolean;
      limit?: number;
    }): Promise<SearchHostsResponse> {
      const qs = new URLSearchParams({ query: params.query.trim() });
      if (params.group_id != null) qs.set("group_id", String(params.group_id));
      appendIntList(qs, "tag_ids", params.tag_ids);
      if (params.regex) qs.set("regex", params.regex);
      if (params.case_sensitive) qs.set("case_sensitive", "true");
      qs.set("limit", String(Math.min(200, Math.max(1, params.limit ?? 50))));
      return requestJson<SearchHostsResponse>(
        `/api/hosts/search?${qs}`,
        { method: "GET" },
        { timeoutMs: 60_000 },
      );
    },

    /** 按主机级 AI 提示词内容搜索（GET /api/integration/hosts/search-by-prompt）。 */
    async searchHostsByPrompt(params: {
      query: string;
      group_id?: number;
      tag_ids?: number[];
      regex?: string;
      case_sensitive?: boolean;
      limit?: number;
      snippet_chars?: number;
    }): Promise<SearchHostsByPromptResponse> {
      const qs = new URLSearchParams({ query: params.query.trim() });
      if (params.group_id != null) qs.set("group_id", String(params.group_id));
      appendIntList(qs, "tag_ids", params.tag_ids);
      if (params.regex) qs.set("regex", params.regex);
      if (params.case_sensitive) qs.set("case_sensitive", "true");
      qs.set("limit", String(Math.min(100, Math.max(1, params.limit ?? 30))));
      if (params.snippet_chars != null) {
        qs.set("snippet_chars", String(params.snippet_chars));
      }
      return requestJson<SearchHostsByPromptResponse>(
        `/api/integration/hosts/search-by-prompt?${qs}`,
        { method: "GET" },
        { timeoutMs: 60_000 },
      );
    },

    async getHost(hostId: number): Promise<unknown> {
      return requestJson(
        `/api/hosts/${hostId}`,
        { method: "GET" },
        { timeoutMs: 30_000 },
      );
    },

    async getHostPrompt(hostId: number): Promise<unknown> {
      return requestJson(
        `/api/ai/hosts/${hostId}/prompt`,
        { method: "GET" },
        { timeoutMs: 30_000 },
      );
    },

    async listHostTags(): Promise<unknown> {
      return requestJson(
        "/api/host-tags",
        { method: "GET" },
        { timeoutMs: 30_000 },
      );
    },

    async hostAlive(hostId: number): Promise<unknown> {
      return requestJson(
        `/api/hosts/${hostId}/alive`,
        { method: "GET" },
        { timeoutMs: 20_000 },
      );
    },

    async hostStats(): Promise<unknown> {
      return requestJson(
        "/api/hosts/stats",
        { method: "GET" },
        { timeoutMs: 15_000 },
      );
    },

    async searchBestPractices(params?: {
      keyword?: string;
      category?: string;
      page?: number;
      page_size?: number;
    }): Promise<unknown> {
      const qs = new URLSearchParams();
      if (params?.keyword?.trim()) qs.set("keyword", params.keyword.trim());
      if (params?.category?.trim()) qs.set("category", params.category.trim());
      qs.set("page", String(Math.max(1, params?.page ?? 1)));
      qs.set("page_size", String(Math.min(100, Math.max(1, params?.page_size ?? 20))));
      const suffix = qs.toString() ? `?${qs}` : "";
      return requestJson(
        `/api/best-practices${suffix}`,
        { method: "GET" },
        { timeoutMs: 30_000 },
      );
    },

    /** 纯后台运维 AI（POST /api/integration/ops-chat/complete），与网页 /api/ai/chat 分离。 */
    async opsChatComplete(body: {
      message: string;
      session_id?: number | null;
      host_id?: number | null;
      skip_secondary_assistant?: boolean;
      attachment_uuids?: string[];
      ui_locale?: string | null;
    }): Promise<OpsChatCompleteResponse> {
      return requestJson<OpsChatCompleteResponse>(
        "/api/integration/ops-chat/complete",
        {
          method: "POST",
          body: JSON.stringify({
            message: body.message,
            session_id: body.session_id ?? undefined,
            host_id: body.host_id ?? undefined,
            skip_secondary_assistant:
              body.skip_secondary_assistant !== false,
            attachment_uuids: body.attachment_uuids ?? [],
            ui_locale: body.ui_locale ?? undefined,
          }),
        },
        { timeoutMs: DEFAULT_FETCH_TIMEOUT_MS },
      );
    },

    /** SSH 交互通道 — 创建 TTY 连接（POST /api/ssh-channel） */
    async sshChannelCreate(body: {
      host_id: number;
      session_id?: number | null;
      owner_type?: string;
      owner_id?: string;
      idle_close_sec?: number;
      input_timeout_sec?: number;
      output_timeout_sec?: number;
    }): Promise<unknown> {
      return requestJson(
        "/api/ssh-channel",
        { method: "POST", body: JSON.stringify(body) },
        { timeoutMs: 120_000 },
      );
    },

    async sshChannelList(params?: {
      owner_type?: string;
      owner_id?: string;
      all_open?: boolean;
    }): Promise<unknown> {
      const qs = new URLSearchParams();
      if (params?.owner_type) qs.set("owner_type", params.owner_type);
      if (params?.owner_id) qs.set("owner_id", params.owner_id);
      if (params?.all_open) qs.set("all_open", "true");
      const suffix = qs.toString() ? `?${qs}` : "";
      return requestJson(`/api/ssh-channel${suffix}`, { method: "GET" }, { timeoutMs: 60_000 });
    },

    async sshChannelInfo(channelId: number, checkAlive?: boolean): Promise<unknown> {
      const qs = checkAlive ? "?check_alive=1" : "";
      return requestJson(
        `/api/ssh-channel/${channelId}${qs}`,
        { method: "GET" },
        { timeoutMs: 30_000 },
      );
    },

    async sshChannelSend(channelId: number, content: string): Promise<unknown> {
      return requestJson(
        `/api/ssh-channel/${channelId}/send`,
        { method: "POST", body: JSON.stringify({ content }) },
        { timeoutMs: 60_000 },
      );
    },

    async sshChannelReadLines(
      channelId: number,
      params?: {
        from_line?: number;
        to_line?: number;
        last_n?: number;
        since_line?: number;
        spill?: boolean;
        session_id?: number;
      },
    ): Promise<unknown> {
      const qs = new URLSearchParams();
      if (params?.from_line != null) qs.set("from_line", String(params.from_line));
      if (params?.to_line != null) qs.set("to_line", String(params.to_line));
      if (params?.last_n != null) qs.set("last_n", String(params.last_n));
      if (params?.since_line != null) qs.set("since_line", String(params.since_line));
      if (params?.spill === false) qs.set("spill", "false");
      if (params?.session_id != null) qs.set("session_id", String(params.session_id));
      const suffix = qs.toString() ? `?${qs}` : "";
      return requestJson(
        `/api/ssh-channel/${channelId}/lines${suffix}`,
        { method: "GET" },
        { timeoutMs: 120_000 },
      );
    },

    async sshChannelRead(
      channelId: number,
      params?: { max_chars?: number; spill?: boolean; session_id?: number },
    ): Promise<unknown> {
      const qs = new URLSearchParams();
      if (params?.max_chars != null) qs.set("max_chars", String(params.max_chars));
      if (params?.spill === false) qs.set("spill", "false");
      if (params?.session_id != null) qs.set("session_id", String(params.session_id));
      const suffix = qs.toString() ? `?${qs}` : "";
      return requestJson(
        `/api/ssh-channel/${channelId}/read${suffix}`,
        { method: "GET" },
        { timeoutMs: 120_000 },
      );
    },

    async sshChannelHasNew(channelId: number, afterLine?: number): Promise<unknown> {
      const qs = new URLSearchParams();
      if (afterLine != null) qs.set("after_line", String(afterLine));
      const suffix = qs.toString() ? `?${qs}` : "";
      return requestJson(
        `/api/ssh-channel/${channelId}/has-new${suffix}`,
        { method: "GET" },
        { timeoutMs: 30_000 },
      );
    },

    async sshChannelClose(channelId: number): Promise<unknown> {
      return requestJson(
        `/api/ssh-channel/${channelId}`,
        { method: "DELETE" },
        { timeoutMs: 30_000 },
      );
    },

    async sshChannelDump(
      channelId: number,
      body?: { session_id?: number; max_chars?: number },
    ): Promise<unknown> {
      return requestJson(
        `/api/ssh-channel/${channelId}/dump`,
        { method: "POST", body: JSON.stringify(body ?? {}) },
        { timeoutMs: 120_000 },
      );
    },

    async sshChannelCloseBatch(body: {
      session_id?: number;
      owner_type?: string;
      owner_id?: string;
    }): Promise<unknown> {
      return requestJson(
        "/api/ssh-channel/close-batch",
        { method: "POST", body: JSON.stringify(body) },
        { timeoutMs: 60_000 },
      );
    },

    /** 分段读取 spill 落盘（GET /api/integration/spill/read） */
    async readSpill(params: {
      spill_id: string;
      date_subdir: string;
      mode?: string;
      session_id?: number;
      head_chars?: number;
      tail_chars?: number;
      range_start?: number;
      max_chars?: number;
    }): Promise<unknown> {
      const qs = new URLSearchParams({
        spill_id: params.spill_id.trim(),
        date_subdir: params.date_subdir.trim(),
        mode: params.mode ?? "head_tail",
      });
      if (params.session_id != null) qs.set("session_id", String(params.session_id));
      if (params.head_chars != null) qs.set("head_chars", String(params.head_chars));
      if (params.tail_chars != null) qs.set("tail_chars", String(params.tail_chars));
      if (params.range_start != null) qs.set("range_start", String(params.range_start));
      if (params.max_chars != null) qs.set("max_chars", String(params.max_chars));
      return requestJson(
        `/api/integration/spill/read?${qs}`,
        { method: "GET" },
        { timeoutMs: 120_000 },
      );
    },

    /** ClawOps manifest（GET /api/integration/claw-ops/manifest） */
    async getClawOpsManifest(params?: {
      base_url?: string;
      plugin_version?: string;
      capabilities_version?: string;
    }): Promise<import("./manifest-fallback.js").ClawOpsManifest> {
      const qs = new URLSearchParams();
      if (params?.base_url?.trim()) qs.set("base_url", params.base_url.trim());
      if (params?.plugin_version?.trim()) {
        qs.set("plugin_version", params.plugin_version.trim());
      }
      if (params?.capabilities_version?.trim()) {
        qs.set("capabilities_version", params.capabilities_version.trim());
      }
      const suffix = qs.toString() ? `?${qs}` : "";
      return requestJson(
        `/api/integration/claw-ops/manifest${suffix}`,
        { method: "GET" },
        { timeoutMs: 30_000 },
      );
    },

    /** 检查 claw-ops 插件是否需升级 */
    async checkClawOpsUpdate(pluginVersion: string): Promise<unknown> {
      const qs = new URLSearchParams({ plugin_version: pluginVersion });
      return requestJson(
        `/api/integration/claw-ops/check-update?${qs}`,
        { method: "GET" },
        { timeoutMs: 15_000 },
      );
    },

    /** 统一 invoke（POST /api/integration/claw-ops/invoke） */
    async invokeTool(
      tool: string,
      args: Record<string, unknown> = {},
      options?: { timeoutMs?: number },
    ): Promise<unknown> {
      return requestJson(
        "/api/integration/claw-ops/invoke",
        {
          method: "POST",
          body: JSON.stringify({ tool, arguments: args }),
        },
        { timeoutMs: options?.timeoutMs ?? DEFAULT_FETCH_TIMEOUT_MS },
      );
    },
  };
}
