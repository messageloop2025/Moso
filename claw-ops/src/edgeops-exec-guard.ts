/**
 * 拦截「本机 exec 类工具 + 内容像 Moso HTTP 客户端」的调用，迫使模型走 edgeops_* 插件工具。
 */

const EDGEOPS_PATH_MARKERS = [
  "/api/hosts",
  "\\api\\hosts",
  "/api/integration",
  "ops-chat/complete",
  "api/integration/ops-chat",
  "api/integration/claw-ops",
  "claw-ops/manifest",
  "claw-ops/invoke",
];

/** 若同时出现 version 请求与明显本机 Moso 根，也拦截（减少误杀：仅在与 host 线索共存时）。 */
const VERSION_PATH = "/api/version";

function tryParseBaseUrlHostPort(baseUrl: string): { hostPort: string[]; host: string[] } {
  const hostPort: string[] = [];
  const host: string[] = [];
  const u = baseUrl.trim();
  if (!u) return { hostPort, host };
  try {
    const parsed = new URL(u);
    const h = parsed.hostname.toLowerCase();
    host.push(h);
    const p = parsed.port || (parsed.protocol === "https:" ? "443" : "80");
    hostPort.push(`${h}:${p}`);
    if (parsed.port) {
      hostPort.push(`${h}:${parsed.port}`);
    }
    host.push(`${parsed.host}`.toLowerCase());
  } catch {
    /* ignore */
  }
  return { hostPort, host };
}

function isExecLikeTool(toolName: string): boolean {
  const n = toolName.toLowerCase();
  return (
    n === "exec" ||
    n === "run_terminal_cmd" ||
    n.includes("terminal") ||
    n.includes("shell")
  );
}

function payloadBlob(params: Record<string, unknown>): string {
  try {
    return `${JSON.stringify(params)}`.toLowerCase();
  } catch {
    return "";
  }
}

export function shouldBlockLocalMosoShell(args: {
  toolName: string;
  params: Record<string, unknown>;
  baseUrl: string;
}): boolean {
  if (!isExecLikeTool(args.toolName)) {
    return false;
  }
  const blob = payloadBlob(args.params);
  if (!blob) {
    return false;
  }

  for (const m of EDGEOPS_PATH_MARKERS) {
    if (blob.includes(m.toLowerCase())) {
      return true;
    }
  }

  const rest = /invoke-restmethod|curl\s|wget\s|http\.request|\bfetch\(/.test(blob);
  if (!rest) {
    /* 仍可能用纯 URI 字符串 */
  }

  const { hostPort, host } = tryParseBaseUrlHostPort(args.baseUrl);
  const hasHostHint = [...hostPort, ...host].some((h) => h && blob.includes(h));

  if (blob.includes(VERSION_PATH) && hasHostHint) {
    return true;
  }

  if (rest && hasHostHint && (blob.includes("/api/") || blob.includes("\\api\\"))) {
    return true;
  }

  if (blob.includes("eop_") && (blob.includes("127.0.0.1") || blob.includes("localhost") || hasHostHint)) {
    return true;
  }

  return false;
}
