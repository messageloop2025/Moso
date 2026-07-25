"""LLM 适配层：统一阿里云 DashScope / Ollama / OpenAI 兼容接口

- 根据 base_url 自动识别提供商，规范化模型名与请求头
- 重点兼容 qwen3.5-plus（阿里云 compatible-mode 官方模型名）
- 统一解析 chat completions 响应（流式 / 非流式），兼容各端返回格式
- Kimi K3：顶层 reasoning_effort + 多轮/tool 完整回传 reasoning_content
"""

import json
import os
import re
from typing import Any
from urllib.parse import urlparse

# Kimi K3 官方允许的 reasoning_effort；未指定时 API 默认 max（思考很久）
KIMI_REASONING_EFFORTS = frozenset({"low", "high", "max"})
# K3 文档标明固定、应省略的采样参数（乱传无益）
_KIMI_K3_OMIT_SAMPLING_KEYS = (
    "temperature",
    "top_p",
    "n",
    "presence_penalty",
    "frequency_penalty",
)

# 提供商类型
PROVIDER_ALIYUN = "aliyun"
PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENAI = "openai"


def detect_provider(base_url: str) -> str:
    """根据 base_url 识别提供商。"""
    if not base_url:
        return PROVIDER_OPENAI
    url = (base_url or "").strip().lower()
    if "dashscope.aliyuncs.com" in url or "dashscope-us.aliyuncs.com" in url or "dashscope-intl.aliyuncs.com" in url:
        return PROVIDER_ALIYUN
    compact = url.replace(" ", "")
    if re.search(r"localhost:\s*11434|127\.0\.0\.1:\s*11434", compact):
        return PROVIDER_OLLAMA
    # 任意主机上的 Ollama 默认端口（含局域网 IP）
    if re.search(r":11434(?:/|$|\?)", compact):
        return PROVIDER_OLLAMA
    if "11434" in compact and ("ollama" in compact or "localhost" in compact or "127.0.0.1" in compact):
        return PROVIDER_OLLAMA
    return PROVIDER_OPENAI


def normalize_model(provider: str, model: str) -> str:
    """按提供商规范化模型名，保证 qwen3.5-plus 等在各端正确传递。"""
    if not (model or "").strip():
        if provider == PROVIDER_ALIYUN:
            return "qwen3.5-plus"
        if provider == PROVIDER_OLLAMA:
            return "llama3.2"
        return "gpt-4o-mini"
    name = (model or "").strip()
    # 阿里云：qwen3.5-plus 为官方兼容接口模型名，直接使用
    if provider == PROVIDER_ALIYUN:
        # 可选：将旧版 qwen-plus 映射到 qwen3.5-plus（用户可自行改回）
        if name in ("qwen-plus", "qwen-plus-latest") and "qwen3.5" not in name:
            return "qwen3.5-plus"
        return name
    # Ollama：保持用户配置，如 qwen3.5:latest、llama3.2 等
    if provider == PROVIDER_OLLAMA:
        return name
    return name


def prepare_headers(provider: str, api_key: str, base_url: str | None = None) -> dict[str, str]:
    """生成请求头。有 Key 时始终带 Authorization；无 Key 且本地/自建 endpoint 时可不带。"""
    headers = {"Content-Type": "application/json"}
    key = (api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
        return headers
    if not require_api_key(provider, "", own_base_url=base_url, resolved_base_url=base_url):
        return headers
    return headers


def ensure_chat_completions_url(base_url: str) -> str:
    """确保 base_url 以 /chat/completions 结尾。"""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/chat/completions"):
        return url
    return f"{url}/chat/completions"


def parse_chat_response(data: dict) -> tuple[dict, list]:
    """从 chat completions 响应中解析出 message 与 tool_calls。

    兼容 OpenAI 格式及阿里云 compatible-mode 返回格式。
    返回 (message_dict, tool_calls_list)，tool_calls 可能为空列表。
    """
    message: dict = {}
    tool_calls: list = []

    # OpenAI / 阿里 compatible-mode: choices[0].message
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    if choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message") or first.get("delta") or {}
        if not message and isinstance(first, dict):
            message = first
    else:
        # 部分实现可能用 output / result
        message = data.get("output") or data.get("result") or data.get("message") or {}

    if not isinstance(message, dict):
        message = {"content": str(message) if message else ""}

    raw_tools = message.get("tool_calls")
    if isinstance(raw_tools, list):
        for tc in raw_tools:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            if isinstance(fn, dict) and fn.get("name"):
                tool_calls.append({
                    "id": (tc.get("id") or "").strip() or f"call_{len(tool_calls)}",
                    "function": {
                        "name": str(fn.get("name", "")).strip(),
                        "arguments": fn.get("arguments") if isinstance(fn.get("arguments"), str) else "{}",
                    },
                })
    # 阿里云部分接口可能把 function_call 放在顶层，兼容一层
    fc = message.get("function_call")
    if not tool_calls and isinstance(fc, dict) and fc.get("name"):
        tool_calls.append({
            "id": "call_0",
            "function": {
                "name": str(fc.get("name", "")).strip(),
                "arguments": fc.get("arguments") if isinstance(fc.get("arguments"), str) else "{}",
            },
        })

    return message, tool_calls


def extract_message_content(message: dict) -> str:
    """从 message 中安全取出 content 字符串。"""
    if not isinstance(message, dict):
        return ""
    c = message.get("content")
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        # 多模态 content 列表，取 text 部分
        for part in c:
            if isinstance(part, dict) and part.get("type") == "text":
                return (part.get("text") or "") or ""
        return ""
    return str(c)


def extract_message_reasoning(message: dict | None) -> str:
    """取出思考链文本（Kimi / 阿里等：reasoning_content / reasoning / thinking）。"""
    if not isinstance(message, dict):
        return ""
    for key in ("reasoning_content", "reasoning", "thinking"):
        raw = message.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw
    return ""


def is_kimi_k3_model(model: str | None) -> bool:
    """识别 Kimi K3 系列（Preserved Thinking 始终开启，用 reasoning_effort 控长度）。"""
    name = (model or "").strip().lower()
    if not name:
        return False
    # 兼容 kimi-k3、moonshot/kimi-k3、kimi-k3-xxx
    base = name.rsplit("/", 1)[-1]
    return base == "kimi-k3" or base.startswith("kimi-k3-") or base.startswith("kimi-k3.")


def resolve_kimi_reasoning_effort(explicit: str | None = None) -> str:
    """解析 K3 的 reasoning_effort：显式值 > EDGEOPS_/MOSS_ 环境变量 > 默认 low。

    官方默认多为 max；Agent 场景默认 low 以缩短空等，需要更强推理时再调高。
    """
    candidates = (
        (explicit or "").strip().lower(),
        (os.getenv("EDGEOPS_KIMI_REASONING_EFFORT") or "").strip().lower(),
        (os.getenv("MOSS_KIMI_REASONING_EFFORT") or "").strip().lower(),
    )
    for val in candidates:
        if val in KIMI_REASONING_EFFORTS:
            return val
    return "low"


def build_assistant_history_message(
    message: dict | None,
    *,
    tool_calls: list | None = None,
) -> dict[str, Any]:
    """构造多轮 / tool 循环回灌的完整 assistant 消息。

    Kimi K3 官方要求：回传 API 返回的完整 assistant（含 reasoning_content 与
    tool_calls），不要只保留 content，否则后续轮次思维链被掐断。
    """
    msg = message if isinstance(message, dict) else {}
    out: dict[str, Any] = {
        "role": "assistant",
        "content": extract_message_content(msg) or "",
    }
    reasoning = extract_message_reasoning(msg)
    if reasoning:
        out["reasoning_content"] = reasoning
    tcs = tool_calls
    if tcs is None:
        raw_tc = msg.get("tool_calls")
        tcs = raw_tc if isinstance(raw_tc, list) and raw_tc else None
    if tcs:
        out["tool_calls"] = tcs
    return out


def apply_provider_request_extensions(
    payload: dict | None,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """按模型注入/清理请求扩展字段（原地友好：返回新 dict）。

    Kimi K3：
    - 注入顶层 reasoning_effort（默认 low，可用环境变量覆盖）
    - 移除 enable_thinking / thinking（那是 Qwen / K2.x 旋钮，对 K3 无效）
    - 移除官方建议省略的固定采样参数
    """
    out: dict[str, Any] = dict(payload or {})
    model_name = (model or out.get("model") or "").strip()
    if not is_kimi_k3_model(model_name):
        return out

    out.pop("enable_thinking", None)
    out.pop("thinking", None)
    for key in _KIMI_K3_OMIT_SAMPLING_KEYS:
        out.pop(key, None)

    existing = out.get("reasoning_effort")
    if isinstance(existing, str) and existing.strip().lower() in KIMI_REASONING_EFFORTS:
        out["reasoning_effort"] = existing.strip().lower()
    else:
        out["reasoning_effort"] = resolve_kimi_reasoning_effort(
            existing if isinstance(existing, str) else None
        )
    return out


def is_ollama(provider: str) -> bool:
    return provider == PROVIDER_OLLAMA


def is_local_or_self_hosted_base_url(base_url: str | None) -> bool:
    """本机 / 内网推理地址（Ollama、LM Studio、LocalAI、vLLM 等）通常可不填 API Key。"""
    raw = (base_url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if not host:
        return False
    if re.search(r":11434(?:/|$|\?)", raw.lower().replace(" ", "")):
        return True
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".local"):
        return True
    if re.match(r"^10\.", host) or re.match(r"^192\.168\.", host) or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host):
        return True
    if port in (11434, 11435, 1234, 8080, 8000, 5000, 3000, 8888, 7860):
        return True
    return False


def require_api_key(
    provider: str,
    api_key: str,
    *,
    own_base_url: str | None = None,
    resolved_base_url: str | None = None,
) -> bool:
    """当前配置下是否必须提供 API Key。

    Profile / user_ai_config 已填写 Base URL 时 Key 可选（本地 Ollama、LM Studio、LocalAI、
    vLLM 等 OpenAI 兼容服务均适用）；仅在使用全局默认云 endpoint 且无 Key 时才强制要求。
    """
    if (api_key or "").strip():
        return False
    if provider == PROVIDER_OLLAMA:
        return False
    if (own_base_url or "").strip():
        return False
    if is_local_or_self_hosted_base_url(resolved_base_url):
        return False
    return True


def should_use_system_shared_api_key(
    *,
    provider: str,
    api_key: str,
    profile_row: dict | None = None,
    user_own_base_url: str | None = None,
) -> bool:
    """未配置用户 Key 时，是否应注入系统共享 Key 并计入配额。

    用户已在 Profile / user_ai_config 中填写自有 Base URL（如 Ollama、LM Studio）时，
    即使 Key 为空也不走系统共享 Key；仅在使用全局默认云 endpoint 且无自有 Key 时才共享。
    """
    if (api_key or "").strip():
        return False
    own_base = ""
    if profile_row:
        if (profile_row.get("api_key") or "").strip():
            return False
        own_base = (profile_row.get("base_url") or "").strip()
        if (profile_row.get("provider") or "").strip() == PROVIDER_OLLAMA:
            return False
    elif user_own_base_url:
        own_base = (user_own_base_url or "").strip()
    if own_base:
        return False
    if not require_api_key(provider, api_key, own_base_url=own_base):
        return False
    return True


# ── 流式（SSE）解析 ──────────────────────────────────────────────────────────
# OpenAI 兼容协议在 chat/completions 流式响应中每行格式为 `data: {...json...}`，
# 末尾以 `data: [DONE]` 终止；阿里云 compatible-mode 与 Ollama OpenAI-style
# 端点都遵循此约定，差异主要在 `delta` 内是否额外携带 `reasoning_content` 等
# 字段。下面三个函数把"按行收到的字节"翻译成上层主循环可直接消费的事件，
# 并负责把 tool_calls 的 chunk 增量按 `index` 合并成完整的工具调用列表。


def parse_stream_line(line: str) -> dict | None:
    """从一行 SSE 文本中解析出原始 chunk dict。

    返回 None 表示该行应当被忽略（注释、空行、`[DONE]` 终止符、非 JSON）。
    上层应在收到 None 时继续读下一行；流真正结束由调用方根据连接关闭或
    `[DONE]` 自行标记，这里只做字面解析、不维护状态。
    """
    if line is None:
        return None
    s = line.strip()
    if not s or s.startswith(":"):
        return None
    if s.startswith("data:"):
        s = s[5:].strip()
    if not s or s == "[DONE]":
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def extract_stream_delta(chunk: dict) -> tuple[str, str, list, str | None]:
    """从一个 chunk 中抽出 (content_delta, reasoning_delta, tool_call_deltas, finish_reason)。

    - content_delta：本 chunk 新增的可见正文片段（无则空串）。
    - reasoning_delta：阿里云 / 部分 OpenAI 兼容端会在 `delta.reasoning_content`
      或 `delta.reasoning` 中带"思考链"片段；上层应单独以 cot 事件透出，
      不要拼进 assistant 正文。
    - tool_call_deltas：本 chunk 中 `delta.tool_calls` 的原始增量列表
      （未合并），交给 `merge_tool_call_deltas` 累加。
    - finish_reason：本 chunk 的 `choices[0].finish_reason`，None 表示未结束。
    """
    if not isinstance(chunk, dict):
        return "", "", [], None
    choices = chunk.get("choices") if isinstance(chunk.get("choices"), list) else []
    if not choices:
        return "", "", [], None
    first = choices[0] if isinstance(choices[0], dict) else {}
    delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
    # 兼容部分实现把首帧或末帧用 message 而不是 delta 装载完整内容
    if not delta and isinstance(first.get("message"), dict):
        delta = first.get("message") or {}

    content = delta.get("content")
    if isinstance(content, list):
        # 多模态 content：取 text 部分拼接
        buf = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text") or ""
                if t:
                    buf.append(t)
        content_text = "".join(buf)
    elif isinstance(content, str):
        content_text = content
    else:
        content_text = ""

    reasoning_text = ""
    for key in ("reasoning_content", "reasoning", "thinking"):
        raw = delta.get(key)
        if isinstance(raw, str) and raw:
            reasoning_text = raw
            break
        if isinstance(raw, dict):
            for sub in ("text", "content", "value"):
                v = raw.get(sub)
                if isinstance(v, str) and v:
                    reasoning_text = v
                    break
            if reasoning_text:
                break

    raw_tools = delta.get("tool_calls")
    tool_deltas = raw_tools if isinstance(raw_tools, list) else []

    finish_reason = first.get("finish_reason")
    if not isinstance(finish_reason, str):
        finish_reason = None

    return content_text, reasoning_text, tool_deltas, finish_reason


def merge_tool_call_deltas(accumulator: list[dict], deltas: list[dict]) -> None:
    """把 stream delta 中的 tool_calls 增量合并到 accumulator（原地修改）。

    OpenAI 兼容协议中，每个 tool_call delta 形如：
        { "index": 0, "id": "...", "type": "function",
          "function": {"name": "fn", "arguments": "{\\"a\\":"} }
    其中：
    - 同一个 `index` 多次出现，按顺序拼接 `function.arguments` 字符串；
    - `id` / `function.name` / `type` 通常只在该 index 的第一帧出现，后续
      帧给空；为了对部分 provider 分块下发 name/id 的极端情况鲁棒，这里
      用"非空才覆盖"的策略，避免把已经收到的字段又被空串清掉。
    最终 accumulator 内每一项都可直接放进 messages[*].tool_calls，并配合
    `parse_chat_response` 的同名格式给下游执行器使用。
    """
    if not deltas:
        return
    for d in deltas:
        if not isinstance(d, dict):
            continue
        idx = d.get("index")
        if not isinstance(idx, int) or idx < 0:
            continue
        while len(accumulator) <= idx:
            accumulator.append({
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
        target = accumulator[idx]
        new_id = d.get("id")
        if isinstance(new_id, str) and new_id:
            target["id"] = new_id
        new_type = d.get("type")
        if isinstance(new_type, str) and new_type:
            target["type"] = new_type
        fn_d = d.get("function")
        if isinstance(fn_d, dict):
            target_fn = target.setdefault("function", {"name": "", "arguments": ""})
            new_name = fn_d.get("name")
            if isinstance(new_name, str) and new_name:
                # 绝大多数 provider 一次性给完整 name；极端情况下若分块下发
                # 则按追加保留全部片段（避免覆盖丢前缀）。
                if not target_fn.get("name"):
                    target_fn["name"] = new_name
                elif not target_fn["name"].endswith(new_name) and not new_name.startswith(target_fn["name"]):
                    target_fn["name"] = (target_fn.get("name") or "") + new_name
            new_args = fn_d.get("arguments")
            if isinstance(new_args, str) and new_args:
                target_fn["arguments"] = (target_fn.get("arguments") or "") + new_args


def finalize_tool_calls(accumulator: list[dict]) -> list[dict]:
    """把累加好的 tool_calls 规范成 `parse_chat_response` 一致的输出格式。

    - 丢弃 `function.name` 为空的项（视为无效 chunk）；
    - 给缺失 `id` 的项补一个稳定占位（与非流式分支同名 `call_<i>`）；
    - 保证 `function.arguments` 为字符串（空则填 `"{}"`，方便下游 json.loads）。
    """
    out: list[dict] = []
    for i, tc in enumerate(accumulator):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = (fn.get("name") or "").strip()
        if not name:
            continue
        args = fn.get("arguments")
        if not isinstance(args, str) or not args.strip():
            args = "{}"
        out.append({
            "id": (tc.get("id") or "").strip() or f"call_{i}",
            "type": tc.get("type") or "function",
            "function": {"name": name, "arguments": args},
        })
    return out
