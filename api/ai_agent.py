"""毛竹（Moso）AI 助手 API — Function Calling + Agent 循环（参考 IOTHub，装载控制台内容；控制台与终端同义）"""
import asyncio
import json
import logging
import re
import threading
from datetime import datetime
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config as _config
from config import (
    AGENT_MAX_STEPS,
    ASSISTANT_MAX_ROUNDS,
    AGENT_MAX_STEPS_CAP,
    ASSISTANT_MAX_ROUNDS_CAP,
    AGENT_POLL_WAIT_CHUNK_SEC,
    EDGEOPS_SESSION_TITLE_CLIENT_PLACEHOLDERS,
    EDGEOPS_TEMP_SESSION_PREFIX,
)  # 默认值与硬上限：默认 100，硬上限 1000
SYSTEM_AI_USAGE_LIMIT = getattr(_config, "SYSTEM_AI_USAGE_LIMIT", 200)
from database import get_db
from api.auth import get_current_user, require_admin, _is_admin_role
from api.hosts import normalize_host_aliases_in_dict
from api.terminal import get_terminal_buffer_for_user, get_current_host_id_for_user, normalize_terminal_scope_id
from services.ai_skills import (
    TOOLS,
    _get_host_row,
    execute_tool,
    get_skills_summary,
    get_tools_for_scope,
)
from services.chat_utils import assistant_content_for_summary
from services.text_abbrev import abbreviate_text_head_focus, abbreviate_text_tail_focus
from services.chat_tool_spill import (
    shrink_tool_message_for_history_budget,
    spill_and_wrap_tool_message,
)
from services.terminal_poll import TerminalPollBatchState, apply_terminal_poll_tool_result
from services.session_runtime import (
    get_runtime_context_for_session,
    list_active_items,
    load_session_runtime,
    prune_runtime_document,
)
from services.ai_output_language import build_output_language_system_section, resolve_output_language
from services.user_mcp_client import resolve_chat_tools

_USER_MCP_SYSTEM_HINT = (
    "\n\n**个人 MCP 服务器**：用户可在「MCP 配置」页或对话中请你代为配置第三方 MCP。"
    "可用工具：list_user_mcp_servers、configure_user_mcp_server、import_user_mcp_config、export_user_mcp_config、"
    "test_user_mcp_server、refresh_user_mcp_tools、delete_user_mcp_server。"
    "配置并启用且勾选场景开关后，MCP 工具会自动并入对应 AI 聊天。"
    "生图等 MCP 若返回 OSS/临时签名 URL，系统会自动拉取为 `/api/ai/attachments/<uuid>`；"
    "向用户展示图片须用 `fetched_assets[].local_url` 或 `markdown_image`（形如 `![描述](/api/ai/attachments/<uuid>)`）；前端会自动加鉴权并内联显示，勿直接贴 OSS source_url。"
)

from services.llm_adapter import (
    detect_provider,
    normalize_model,
    prepare_headers,
    ensure_chat_completions_url,
    parse_chat_response,
    extract_message_content,
    require_api_key,
    parse_stream_line,
    extract_stream_delta,
    merge_tool_call_deltas,
    finalize_tool_calls,
)

logger = logging.getLogger("edgeops.ai_agent")

router = APIRouter(prefix="/api/ai", tags=["AI 助手"])


def _host_env_prompt_snippet(hp: dict) -> str:
    """把主机已识别的系统 / Shell / 包管理器写入会话提示，减少 AI 猜测。"""
    t = (hp.get("host_type") or "").strip()
    v = (hp.get("host_version") or "").strip()
    sh = (hp.get("host_shell") or "").strip()
    pk = (hp.get("host_package_manager") or "").strip()
    bits = []
    if t and t != "未知":
        bits.append("系统: " + t + (f"（{v}）" if v and v != "未知" else ""))
    if sh and sh != "未知":
        bits.append(f"登录/默认 Shell: {sh}")
    if pk and pk != "未知":
        bits.append(f"默认包管理: {pk}")
    if not bits:
        return ""
    return (
        "\n**主机系统环境（请优先按此选择命令解释器与软件安装方式；与下文不符再以 ssh_execute 探测）**："
        + "；".join(bits)
        + "。\n"
    )


def _host_dim_remote_data_processing_rules(session_host_id: int) -> str:
    """主机详情 / AI 运维会话：为大数据/日志生成的脚本须在绑定的远程机上跑，大输出优先落远端 /tmp。"""
    hid = int(session_host_id)
    return (
        "\n### 复杂数据处理 · 须在当前远程主机执行且输出优先落 /tmp（必读）\n"
        "为处理日志、大批量数据、复杂解析/聚合而生成的 **.py、.sh** 等脚本，**必须在当前会话这台远程主机上执行**，"
        f"**禁止**在运行{_config.PRODUCT_DISPLAY}的应用服务器本机执行（不要使用 `local_exec`、`local_run_script`、`local_fs_*` 等本机专用工具处理本场景；"
        "即使模型在历史上下文中见过这些名称，在当前会话也不要调用）。\n"
        "推荐流程：`fs_write_file` 先写在用户 **web/fs** → `scp_push` 上传到当前主机（路径宜在 **`/tmp/`** 下，如 `/tmp/edgeops-<简述>.sh`）→ "
        f"**`ssh_execute(host_id={hid}, …)`** 或 **已打开的 SSH 控制台**（`send_to_terminal`）在远端执行。\n"
        "脚本运行时的**大段输出、中间 CSV/JSON、临时下载、解压目录**等，**优先写到远端 `/tmp/`**"
        "（建议使用独占子目录，如 `/tmp/edgeops-work-<YYYYMMDD>-<任务简述>/`，避免污染业务数据目录；任务结束后可提醒用户按需清理）。\n"
        f"若需拉回{_config.PRODUCT_DISPLAY}侧用 `data_query`/本地工具继续分析，用 **`scp_pull`**（大文件/目录均支持，调用卡显示传输进度）拉到用户 **`chats/<UTC>/`**。\n"
    )


def _effective_provider(settings: dict, base_url: str) -> str:
    """AI 源类型：配置中指定则用配置，否则按 base_url 自动探测。"""
    p = (settings.get("ai_provider") or "").strip()
    if p in ("aliyun", "ollama", "openai"):
        return p
    return detect_provider(base_url)


async def _can_access_host_with_shares(db, host_row: dict | None, user: dict) -> bool:
    """主机访问校验：管理员、所有者或被分享用户。"""
    if not host_row:
        return False
    if _is_admin_role(user.get("role")):
        return True
    if host_row.get("created_by") == user["id"]:
        return True
    hid = host_row.get("id")
    if hid is None:
        return False
    rows = await db.execute_fetchall(
        """SELECT 1 FROM host_shares
           WHERE host_id = ? AND shared_with_user_id = ? AND revoked_at IS NULL
           LIMIT 1""",
        (hid, user["id"]),
    )
    return bool(rows)

# 单条会话消息保存长度上限（字符），避免超长回复被截断导致前端展示不全
AI_MESSAGE_SAVE_MAX = 200_000

# UI Action 哨兵：把 ask_user_choice 等交互卡随 assistant 文本一起持久化，
# 让 loadSession 重新渲染时也能还原选择卡，避免「一闪而过」。
# 形如：<!-- EDGEOPS:UI_ACTION:v1 BASE64_JSON -->
UI_ACTION_SENTINEL_PREFIX = "<!-- EDGEOPS:UI_ACTION:v1 "
UI_ACTION_SENTINEL_SUFFIX = " -->"

# 工具调用 / 推理步骤轨迹：随 assistant 落库，前端历史消息可折叠还原「调用流程」。
TOOL_TRACE_SENTINEL_PREFIX = "<!-- EDGEOPS:TOOL_TRACE:v1 "
TOOL_TRACE_SENTINEL_SUFFIX = " -->"
# 嵌入前控制体积，避免单条消息过大
TOOL_TRACE_MAX_STEPS = 80
TOOL_TRACE_MAX_JSON_CHARS = 120_000

# 运行中会话控制：支持在 tool_call 执行期间插入 stop/pause/supplement/choice 指令。
_SESSION_RUNTIME_CONTROL_QUEUES: dict[int, asyncio.Queue] = {}
_SESSION_RUNTIME_CONTROL_LOCK = asyncio.Lock()
_RUNTIME_ACTIONS = {"supplement", "pause", "resume", "stop", "choice"}


def _strip_ui_action_sentinels(content: str) -> str:
    """从 assistant content 中移除所有 ui_action 哨兵注释，避免回灌 LLM 时污染上下文。"""
    if not content or UI_ACTION_SENTINEL_PREFIX not in content:
        return content or ""
    import re as _re
    pat = _re.compile(
        _re.escape(UI_ACTION_SENTINEL_PREFIX) + r"[A-Za-z0-9+/=]+" + _re.escape(UI_ACTION_SENTINEL_SUFFIX)
    )
    cleaned = pat.sub("", content)
    return cleaned.rstrip() if cleaned else cleaned


def _strip_tool_trace_sentinels(content: str) -> str:
    """从 assistant content 中移除工具轨迹哨兵，避免回灌 LLM 与重复渲染。"""
    if not content or TOOL_TRACE_SENTINEL_PREFIX not in content:
        return content or ""
    import re as _re

    pat = _re.compile(
        _re.escape(TOOL_TRACE_SENTINEL_PREFIX) + r"[A-Za-z0-9+/=]+" + _re.escape(TOOL_TRACE_SENTINEL_SUFFIX)
    )
    cleaned = pat.sub("", content)
    return cleaned.rstrip() if cleaned else cleaned


def _strip_assistant_embedded_sentinels(content: str) -> str:
    """剥离落库时嵌入的 UI_ACTION + TOOL_TRACE 哨兵（供历史上下文与展示清洗）。"""
    return _strip_tool_trace_sentinels(_strip_ui_action_sentinels(content or ""))


def _looks_like_user_wants_continue(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    kws = (
        "继续",
        "继续执行",
        "继续之前任务",
        "接着做",
        "resume",
        "continue",
    )
    return any(k in t for k in kws)


def _looks_like_reason_or_newinfo(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    kws = (
        "为什么",
        "原因",
        "怎么回事",
        "为何",
        "先回答",
        "先解释",
        "补充",
        "我补充",
        "另外",
        "还有",
        "why",
        "reason",
        "what happened",
        "i have more info",
        "additional info",
    )
    if any(k in t for k in kws):
        return True
    return ("?" in t) or ("？" in t)


def _looks_like_truncated_assistant_reply(text: str) -> bool:
    """工具调用后模型偶发只返回一个字/半句话时，要求它重试完整总结。"""
    s = _strip_assistant_embedded_sentinels(text or "").strip()
    if not s:
        return True
    if len(s) <= 2:
        return True
    if len(s) <= 8 and not any(ch in s for ch in "。！？.!?\n"):
        return True
    return False


_ACTIONABLE_USER_KWS = (
    "上传",
    "下载",
    "解压",
    "压缩",
    "安装",
    "部署",
    "执行",
    "运行",
    "重启",
    "启动",
    "停止",
    "复制",
    "迁移",
    "同步",
    "备份",
    "恢复",
    "配置",
    "修改",
    "删除",
    "创建",
    "写入",
    "拉取",
    "推送",
    "挂载",
    "格式化",
    "升级",
    "降级",
    "附件",
    "scp",
    "rsync",
    "sftp",
    "unzip",
    "tar ",
    "systemctl",
    "docker ",
    "kubectl",
    "upload",
    "download",
    "extract",
    "decompress",
    "deploy",
    "install",
    "execute",
    "run ",
    "restart",
)


def _looks_like_actionable_user_request(text: str) -> bool:
    """用户消息是否像在要求执行具体操作（而非纯问答/闲聊）。"""
    t = (text or "").strip().lower()
    if not t or len(t) < 2:
        return False
    if _looks_like_reason_or_newinfo(text) and not any(k in t for k in _ACTIONABLE_USER_KWS):
        return False
    return any(k.lower() in t for k in _ACTIONABLE_USER_KWS)


_REPLY_DONE_MARKERS = (
    "已完成",
    "已经完成",
    "已成功",
    "执行成功",
    "操作完成",
    "任务完成",
    "解压完成",
    "上传完成",
    "部署完成",
    "安装完成",
    "successfully completed",
    "completed successfully",
    "already done",
    "finished successfully",
)


_REPLY_PENDING_MARKERS = (
    "我将",
    "我会",
    "我去",
    "我来",
    "马上",
    "接下来",
    "现在去",
    "准备去",
    "正在为您",
    "正在为你",
    "即将",
    "将要",
    "先去",
    "先去",
    "i will ",
    "i'll ",
    "going to ",
    "about to ",
)


def _reply_suggests_pending_work(content: str, user_text: str) -> bool:
    """助手正文像在承诺将要执行，但本轮可能尚未真正调工具。"""
    s = _strip_assistant_embedded_sentinels(content or "").strip().lower()
    if not s:
        return False
    if any(m.lower() in s for m in _REPLY_DONE_MARKERS):
        return False
    if any(m.lower() in s for m in _REPLY_PENDING_MARKERS):
        return True
    if re.search(r"将.{0,12}(上传|解压|执行|部署|安装|下载|同步|复制|重启)", s):
        return True
    if re.search(r"(upload|extract|deploy|install|execute|run).{0,20}(now|next|will)", s):
        return True
    if _looks_like_actionable_user_request(user_text) and len(s) >= 60:
        toolish = ("步骤", "首先", "然后", "接下来", "第一步", "step 1", "next step")
        if any(k in s for k in toolish) and not any(m.lower() in s for m in _REPLY_DONE_MARKERS):
            return True
    return False


def _format_force_tool_notice(output_locale: str | None = None) -> str:
    loc = (output_locale or "").strip().lower()
    en = loc == "en" or loc.startswith("en-")
    if en:
        return "\n\n---\n*Continuing: invoking tools to complete the requested operation (not text-only).*"
    return "\n\n---\n*正在自动续跑：将调用工具完成操作，而非仅文字说明。*"


def _force_tool_nudge_user_message(user_text: str, output_locale: str | None = None) -> str:
    loc = (output_locale or "").strip().lower()
    en = loc == "en" or loc.startswith("en-")
    snippet = (user_text or "").strip()[:200]
    if en:
        return (
            "[System] Your previous reply only described planned actions without any tool_call. "
            "The user request requires real execution. Call the appropriate tools now "
            "(e.g. fs_*, scp_*, ssh_execute) to fulfill the goal. Do not reply with promises only.\n\n"
            f"Original user request (excerpt): {snippet}"
        )
    return (
        "【系统】你上一轮只在文字中描述了将要执行的操作，但没有任何 tool_call。"
        "用户请求需要真实执行：请立即调用合适的工具（如 fs_*、scp_*、ssh_execute 等）完成目标，"
        "不要再次只用文字承诺。\n\n"
        f"用户原请求（摘要）：{snippet}"
    )


_MAX_FORCE_TOOL_RETRIES = 2


def _infer_low_interaction_preference(conversation: list[dict], current_user_text: str) -> bool:
    """从最近用户消息里推断是否偏好“减少交互、自动完成”。

    仅做轻量关键词判断，不覆盖安全门禁（缺必要条件/执行失败时仍会停下）。
    """
    positive = (
        "减少交互",
        "少打断",
        "少一点交互",
        "减少确认",
        "自动完成",
        "自动执行",
        "自动继续",
        "不要频繁确认",
        "别每步确认",
        "无需确认",
        "一次做完",
        "autopilot",
        "less interaction",
        "minimize interaction",
        "auto complete",
    )
    negative = (
        "每一步确认",
        "多确认",
        "先问我",
        "需要确认",
        "谨慎",
        "先暂停",
        "一步一步来",
        "ask me first",
        "confirm each step",
    )
    texts: list[str] = []
    for m in conversation[-24:]:
        if (m or {}).get("role") != "user":
            continue
        c = (m or {}).get("content")
        if isinstance(c, str) and c.strip():
            texts.append(c.strip().lower())
    if (current_user_text or "").strip():
        texts.append((current_user_text or "").strip().lower())
    if not texts:
        return False
    score = 0
    for t in texts[-8:]:
        if any(k in t for k in positive):
            score += 1
        if any(k in t for k in negative):
            score -= 1
    return score > 0


def _build_continue_confirmation_message(hint: str, output_locale: str | None = None) -> str:
    loc = (output_locale or "").strip().lower()
    en = loc == "en" or loc.startswith("en-")
    h = (hint or "").strip()
    if en:
        base = "I can continue the task that was not yet finished."
        tail = (
            'Reply with "continue" to proceed, or "pause"/"stop", or add your question or constraints.'
        )
        if h:
            return f"{base}\n\nSuggested next step: {h}\n\n{tail}"
        return f"{base}\n\n{tail}"
    base = "我可以继续执行之前未完成的任务。"
    tail = "请回复「继续执行」以继续，或回复「暂停」「停止」，也可以直接补充你关心的问题/约束。"
    if h:
        return f"{base}\n\n建议下一步：{h}\n\n{tail}"
    return f"{base}\n\n{tail}"


async def _get_runtime_control_queue(session_id: int) -> asyncio.Queue:
    async with _SESSION_RUNTIME_CONTROL_LOCK:
        q = _SESSION_RUNTIME_CONTROL_QUEUES.get(session_id)
        if q is None:
            q = asyncio.Queue()
            _SESSION_RUNTIME_CONTROL_QUEUES[session_id] = q
        return q


async def _push_runtime_control(session_id: int, action: str, message: str) -> None:
    q = await _get_runtime_control_queue(session_id)
    await q.put({"action": action, "message": message, "at": datetime.now().isoformat()})


async def _pull_runtime_control_nowait(session_id: int) -> dict | None:
    q = await _get_runtime_control_queue(session_id)
    try:
        return q.get_nowait()
    except asyncio.QueueEmpty:
        return None


async def _pull_runtime_control_matching(session_id: int, action: str) -> dict | None:
    """从控制队列中取出指定 action，其它指令放回队列，便于长 LLM 请求期间优先响应 stop。"""
    q = await _get_runtime_control_queue(session_id)
    skipped: list[dict] = []
    matched = None
    while True:
        try:
            item = q.get_nowait()
        except asyncio.QueueEmpty:
            break
        if isinstance(item, dict) and (item.get("action") or "").strip().lower() == action:
            matched = item
            break
        skipped.append(item)
    for item in skipped:
        await q.put(item)
    return matched


async def _clear_runtime_control_queue(session_id: int) -> None:
    q = await _get_runtime_control_queue(session_id)
    while True:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break


# 用户从 ask_user_choice 选择卡作答时，前端拼接的内容形如 `[A] 是，执行升级`、
# `[B] 否，暂不升级`、或多选 `[A] x ; [B] y`，开头必为 `[ID] `。用于辅助 AI 拦截：
# 当用户上一条来自选择卡作答、且本轮主 AI 没有发起任何 tool_call（即纯文字 ack
# 终态），就直接结束，避免辅助 AI 误判为「未真正执行」而追问第二遍。
_USER_CHOICE_REPLY_RE = re.compile(r"^\s*\[[A-Za-z0-9_\-]{1,32}\]\s+\S")


def _is_user_choice_reply(text: str) -> bool:
    """判断这条 user 消息是否由 ask_user_choice 选择卡按钮回填生成。"""
    if not text:
        return False
    return bool(_USER_CHOICE_REPLY_RE.match(text))


def _default_choice_reply_from_ui_action(ui_action: dict | None) -> str:
    """低交互自动决策时，把 ask_user_choice 的默认/首选项转换成用户回复文本。"""
    if not isinstance(ui_action, dict):
        return ""
    options = ui_action.get("options") or []
    if not isinstance(options, list) or not options:
        return ""
    default_id = str(ui_action.get("default_id") or "").strip()
    chosen = None
    if default_id:
        for opt in options:
            if isinstance(opt, dict) and str(opt.get("id") or "").strip() == default_id:
                chosen = opt
                break
    if chosen is None:
        chosen = next((opt for opt in options if isinstance(opt, dict)), None)
    if not isinstance(chosen, dict):
        return ""
    oid = str(chosen.get("id") or "A").strip() or "A"
    label = str(chosen.get("label") or "").strip()
    value = chosen.get("value")
    text = value if isinstance(value, str) and value.strip() else label
    text = (text or label or oid).strip()
    return f"[{oid}] {text}" if text else ""


def _embed_ui_actions_into_content(content: str, ui_actions: list[dict]) -> str:
    """把若干 ui_action 以哨兵注释追加到 assistant 文本末尾，前端可解析回显。

    使用 base64 包装 JSON 防止换行 / HTML 字符破坏 Markdown 渲染。
    """
    if not ui_actions:
        return content
    import base64 as _b64
    parts: list[str] = []
    for ua in ui_actions:
        try:
            raw = json.dumps(ua, ensure_ascii=False, separators=(",", ":"))
            b64 = _b64.b64encode(raw.encode("utf-8")).decode("ascii")
            parts.append(UI_ACTION_SENTINEL_PREFIX + b64 + UI_ACTION_SENTINEL_SUFFIX)
        except Exception:
            continue
    if not parts:
        return content
    sep = "\n\n" if (content and not content.endswith("\n")) else ""
    return content + sep + "\n".join(parts)


def _embed_tool_trace_into_content(content: str, trace_steps: list[dict] | None) -> str:
    """把本轮工具调用 / 推理步骤序列嵌入 assistant content（单条 BASE64 JSON）。"""
    if not trace_steps:
        return content
    steps = trace_steps[-TOOL_TRACE_MAX_STEPS:]
    import base64 as _b64

    payload = {"v": 1, "steps": steps}
    try:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        while len(raw) > TOOL_TRACE_MAX_JSON_CHARS and len(steps) > 2:
            # 优先丢弃最前的步骤，保留近期调用
            steps = steps[-(len(steps) // 2) :]
            payload = {"v": 1, "steps": steps}
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(raw) > TOOL_TRACE_MAX_JSON_CHARS:
            return content
        b64 = _b64.b64encode(raw.encode("utf-8")).decode("ascii")
    except Exception:
        return content
    sep = "\n\n" if (content and not content.endswith("\n")) else ""
    return content + sep + TOOL_TRACE_SENTINEL_PREFIX + b64 + TOOL_TRACE_SENTINEL_SUFFIX


def _fingerprint_ask_user_choice(ui_action: dict) -> str:
    """Stable fingerprint for ask_user_choice UI actions — used to drop duplicate embeddings."""
    if not isinstance(ui_action, dict) or ui_action.get("action") != "ask_user_choice":
        return ""
    q = " ".join(str(ui_action.get("question") or "").split()).strip()
    opts_raw = ui_action.get("options") or []
    parts: list[str] = []
    if isinstance(opts_raw, list):
        for i, opt in enumerate(opts_raw):
            if not isinstance(opt, dict):
                continue
            oid = str(opt.get("id") or chr(65 + i)).strip()
            lab = " ".join(str(opt.get("label") or "").split()).strip()
            parts.append(f"{oid}:{lab}")
    sig = "|".join(parts)
    flags = ("M" if ui_action.get("allow_multiple") else "") + (
        "0t" if ui_action.get("allow_text") is False else "t"
    )
    return f"{q}\n{sig}\n{flags}"


def _dedupe_ui_actions_ask_user_choice_keep_last(actions: list[dict] | None) -> list[dict]:
    """If the model emits the same choice card multiple times in one round, keep only the last copy."""
    if not actions:
        return []
    last_by_fp: dict[str, int] = {}
    for i, ua in enumerate(actions):
        if not isinstance(ua, dict) or ua.get("action") != "ask_user_choice":
            continue
        fp = _fingerprint_ask_user_choice(ua)
        if fp:
            last_by_fp[fp] = i
    drop: set[int] = set()
    for i, ua in enumerate(actions):
        if not isinstance(ua, dict) or ua.get("action") != "ask_user_choice":
            continue
        fp = _fingerprint_ask_user_choice(ua)
        if fp and last_by_fp.get(fp) != i:
            drop.add(i)
    return [a for j, a in enumerate(actions) if j not in drop]


def _fingerprint_ask_user_choice_tool_call(tc: dict) -> str:
    """Compute the same fingerprint as `_fingerprint_ask_user_choice` but from a raw tool_call entry
    (function.arguments is a JSON string). Returns empty string when not applicable."""
    if not isinstance(tc, dict):
        return ""
    fn = tc.get("function") or {}
    if (fn.get("name") or "").strip() != "ask_user_choice":
        return ""
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except Exception:
        return ""
    if not isinstance(args, dict):
        return ""
    ua_like = {
        "action": "ask_user_choice",
        "question": args.get("question"),
        "options": args.get("options"),
        "allow_multiple": args.get("allow_multiple"),
        "allow_text": args.get("allow_text"),
    }
    return _fingerprint_ask_user_choice(ua_like)


def _recent_assistant_ask_user_choice_fps(messages: list) -> set[str]:
    """走回历史，找到「上一条 user 之前」最近的一次 assistant 工具调用，
    收集其中所有 ask_user_choice 的指纹。

    用途：用户已对选择卡作答（无论点按钮还是自由文字补充）后，
    若主 AI 又在新一轮里发起**相同**的 ask_user_choice，本次直接跳过、
    返回合成 tool_result 提示模型不要再弹同一张选择卡。
    """
    fps: set[str] = set()
    if not messages:
        return fps
    user_seen_after_assistant = False
    for m in reversed(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "user":
            user_seen_after_assistant = True
            continue
        if role == "tool":
            continue
        if role != "assistant":
            continue
        if not user_seen_after_assistant:
            # 此条 assistant 比最近一条 user 更新（不应在历史里出现，但兜底跳过）
            continue
        tcs = m.get("tool_calls") or []
        if isinstance(tcs, list):
            for tc in tcs:
                fp = _fingerprint_ask_user_choice_tool_call(tc)
                if fp:
                    fps.add(fp)
        return fps  # 只看「最近一次有 tool_calls 的 assistant 轮」
    return fps


# ─────────────────────────── 图片多模态内联（视觉模型） ───────────────────────────
# 视觉模型要「看到」图，必须把图以 OpenAI 兼容的 image_url 段挂在 user 消息 content 数组里。
# tool 返回里的 data URL 只是 JSON 字符串，主模型不会当图解析。
#
# 体积/张数默认"不限"（0 表示不限）：按用户需求，AI 加载图片时不做大小截断。
# 上游若有网关级 body 上限（例如某些 provider 的 16 MB）会在 httpx 层报错，再酌情说明。
# 历史轮次追溯窗口（最近 N 条带附件的 user 消息）仍然保留，避免把极早期消息重新压进去。
_VISION_CURRENT_PER_BYTES = 0       # 0 = 不限单图大小
_VISION_CURRENT_TOTAL_BYTES = 0     # 0 = 不限累计大小
_VISION_CURRENT_COUNT = 0           # 0 = 不限张数

_VISION_HISTORY_PER_BYTES = 0
_VISION_HISTORY_TOTAL_BYTES = 0
_VISION_HISTORY_COUNT = 0
_VISION_HISTORY_LOOKBACK_USER_MSGS = 2

# ─────────────────────────── 图片 token 预算（用于扣减文本上下文预算） ───────────────────────────
# 视觉模型把图片作为多模态输入时，单张图会额外吃掉若干 vision token（不同 provider 档位不同）：
#   - OpenAI gpt-4o:low ≈ 85 tokens；high 按 512×512 tile 累加，一般 500~3000；
#   - Qwen-VL / Hunyuan-VL 多按图像分辨率切片，经验值 1.5K~9K / 张。
# 为了把"图片会吃 token"这件事也纳入预算管理，避免多图把历史/工具/主机上下文挤爆，这里按
# 单图 2K~8K token 区间估算开销，再换算成字符数从 context_size（文本预算）里扣减。
# 估算值保守偏高，防止实际吃掉的 token 超出预计导致总 token 溢出。
VISION_IMAGE_TOKEN_MIN = 2_000        # 小图下限（~512KB 以下一律 2K）
VISION_IMAGE_TOKEN_MAX = 8_000        # 大图上限（5MB 以上一律 8K）
VISION_IMAGE_TOKEN_SIZE_LO = 512 * 1024
VISION_IMAGE_TOKEN_SIZE_HI = 5 * 1024 * 1024
# token → char 粗略换算（英文 4 / 中文 2；保守按 4 算，给文本留更多预算）
VISION_TOKEN_TO_CHARS = 4
# 扣减后文本预算的最小地板，避免极端情况下整段被吃光
VISION_RESERVE_FLOOR_CHARS = 8_000


def _estimate_image_tokens(size_bytes: int) -> int:
    """单张图片按字节数线性映射到 [VISION_IMAGE_TOKEN_MIN, VISION_IMAGE_TOKEN_MAX]。

    仅用于 context 预算的"预留扣减"估算，不反映真实 token 计费，也不影响内联字节大小。
    """
    if size_bytes <= 0 or size_bytes <= VISION_IMAGE_TOKEN_SIZE_LO:
        return VISION_IMAGE_TOKEN_MIN
    if size_bytes >= VISION_IMAGE_TOKEN_SIZE_HI:
        return VISION_IMAGE_TOKEN_MAX
    ratio = (size_bytes - VISION_IMAGE_TOKEN_SIZE_LO) / (
        VISION_IMAGE_TOKEN_SIZE_HI - VISION_IMAGE_TOKEN_SIZE_LO
    )
    return int(
        VISION_IMAGE_TOKEN_MIN
        + ratio * (VISION_IMAGE_TOKEN_MAX - VISION_IMAGE_TOKEN_MIN)
    )


def _estimate_vision_token_reserve(
    chat_image_attach_rows: list[dict] | None,
    conversation: list | None,
    *,
    vision_enabled: bool = True,
) -> tuple[int, int, int]:
    """估算本轮会内联的 image_url 总 token 占用。

    覆盖两部分：
    1. 本轮新上传图（按各自 size 精确落点）；
    2. 最近 `_VISION_HISTORY_LOOKBACK_USER_MSGS` 条带 📎 附件清单的 user 历史消息
       （size 未知，按 `VISION_IMAGE_TOKEN_MIN` 保守下限估算 uuid 数）。

    返回 (tokens, chars_equiv, image_count)。vision_enabled=False 时一律返回 0。
    """
    if not vision_enabled:
        return 0, 0, 0
    total_tokens = 0
    image_count = 0
    for r in (chat_image_attach_rows or []):
        if (r or {}).get("kind", "").lower() != "image":
            continue
        total_tokens += _estimate_image_tokens(int((r or {}).get("size_bytes") or 0))
        image_count += 1
    scanned = 0
    for m in reversed(conversation or []):
        if scanned >= _VISION_HISTORY_LOOKBACK_USER_MSGS:
            break
        if (m or {}).get("role") != "user":
            continue
        c = (m or {}).get("content") or ""
        if not isinstance(c, str) or "uuid:" not in c:
            continue
        uuids = _extract_attachment_uuids_from_text(c)
        if uuids:
            # 历史图 size 未知，保守按下限算；可能略高估（含少量非图 uuid），但宁可多留不要溢出
            total_tokens += len(uuids) * VISION_IMAGE_TOKEN_MIN
            image_count += len(uuids)
        scanned += 1
    return total_tokens, total_tokens * VISION_TOKEN_TO_CHARS, image_count


def _apply_vision_token_reserve(context_size: int, reserve_chars: int) -> int:
    """从文本 context 预算中扣除图片 token 折算的字符预留，保留下限避免被吃光。"""
    if reserve_chars <= 0 or context_size <= 0:
        return context_size
    effective = context_size - reserve_chars
    floor = max(VISION_RESERVE_FLOOR_CHARS, min(context_size, AUTO_CONTEXT_MIN // 2))
    return max(floor, effective)

# 匹配 📎 清单行里的 `uuid: XXXX` / `uuid: \`XXXX\``（32 位 hex）
_ATTACHMENT_UUID_IN_TEXT_RE = re.compile(r"uuid:\s*`?([0-9a-fA-F]{32})`?")


def _extract_attachment_uuids_from_text(text: str) -> list[str]:
    """从 user 消息文本（含 📎 附件清单）里抽出所有 uuid，保序去重。"""
    if not text or "uuid:" not in text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _ATTACHMENT_UUID_IN_TEXT_RE.finditer(text):
        u = m.group(1).lower()
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _image_row_to_image_url_part(row: dict, username: str, per_byte_limit: int) -> tuple[dict, dict] | None:
    """把一条 image 附件行读成 image_url 多模态段；失败或单图超限返回 None。"""
    try:
        from api.chat_attachments import resolve_attachment_file as _resolve_attachment_file
    except Exception:
        return None
    import base64 as _b64
    try:
        path = _resolve_attachment_file(row, username)
    except Exception as exc:
        logger.warning("vision: 解析图片路径失败 uuid=%s err=%s", row.get("uuid"), exc)
        return None
    try:
        if not path.exists() or not path.is_file():
            return None
        size = int(row.get("size_bytes") or 0) or path.stat().st_size
        if size <= 0:
            # 兜底：拿不到体积，且底下没法读；保守跳过。
            return None
        if per_byte_limit > 0 and size > per_byte_limit:
            logger.info(
                "vision: 跳过超大图片 uuid=%s size=%s limit=%s",
                row.get("uuid"), size, per_byte_limit,
            )
            return None
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("vision: 读取图片失败 uuid=%s err=%s", row.get("uuid"), exc)
        return None
    except Exception as exc:  # pragma: no cover - 防御性
        logger.warning("vision: 处理图片异常 uuid=%s err=%s", row.get("uuid"), exc)
        return None
    from services.vision_image import build_inline_vision_meta, vision_inline_max_b64_chars

    mime = (row.get("mime_type") or "image/png").strip() or "image/png"
    url, _mime_out, _jpeg_len, dim_meta = build_inline_vision_meta(raw, mime=mime)
    cap = vision_inline_max_b64_chars()
    if len(url) > cap:
        logger.warning(
            "vision: 压缩后仍超限，跳过内联 uuid=%s url_chars=%d cap=%d",
            row.get("uuid"),
            len(url),
            cap,
        )
        return None
    detail = (dim_meta.get("vision_detail") or "high").strip()
    part = {"type": "image_url", "image_url": {"url": url, "detail": detail}}
    return part, dim_meta


def _format_inline_image_dimension_hint(row: dict, dim_meta: dict | None) -> str | None:
    """内联 image_url 前附带的像素尺寸说明，避免 AI 按压缩图估坐标导致标注偏移。"""
    meta = dim_meta or {}
    uuid_s = (row.get("uuid") or "").strip()
    ow = meta.get("original_width") or row.get("original_width") or row.get("image_width") or row.get("width")
    oh = meta.get("original_height") or row.get("original_height") or row.get("image_height") or row.get("height")
    if not ow or not oh:
        return None
    vw = meta.get("vision_width") or row.get("vision_width")
    vh = meta.get("vision_height") or row.get("vision_height")
    mw = meta.get("model_view_width") or row.get("model_view_width")
    mh = meta.get("model_view_height") or row.get("model_view_height")
    parts = [f"[图片 uuid=`{uuid_s}` {int(ow)}×{int(oh)}"]
    if mw and mh and (int(mw) != int(ow) or int(mh) != int(oh)):
        parts.append(f"视图 {int(mw)}×{int(mh)}")
    elif vw and vh and (int(vw) != int(ow) or int(vh) != int(oh)):
        parts.append(f"识图 {int(vw)}×{int(vh)}")
    parts.append("edit 标注坐标请用「视图」尺寸或传 reference_width/height")
    return " · ".join(parts) + "]"


# 用户明确要求"再次回读原图"的关键词；命中时即便附件已有 ai_description，仍强制内联原图像素。
_VISION_REANALYZE_KEYWORDS: tuple[str, ...] = (
    "重新识别", "重新识图", "重新分析图", "重新分析图片", "重新看图", "重新解读", "重新读图",
    "再看一遍", "再看看图", "再看看这张图", "再分析一次", "再识别一次",
    "看原图", "看一下原图", "看看原图", "需要原图", "回读原图", "读原图",
    "reanalyze", "re-analyze", "refresh image", "force reload", "原图",
)


def _message_requests_reanalyze(text: str) -> bool:
    """粗略判断用户本轮消息是否明确要求"重新识别图片"。

    命中任一关键词就返回 True；由调用方决定是否绕过 `ai_description` 缓存直接内联原图。
    """
    if not text:
        return False
    t = text.lower()
    for kw in _VISION_REANALYZE_KEYWORDS:
        if kw.lower() in t:
            return True
    return False


def _build_user_message_content_with_images(
    text: str,
    attach_rows: list[dict] | None,
    user: dict,
    *,
    vision_enabled: bool = True,
):
    """把本轮新上传的 image 附件内联为 OpenAI 兼容视觉多模态 content。

    - 无 image 附件、或用户关闭了「支持图像识别」开关时，原样返回字符串 text（不内联）；
    - **若附件已存在 `ai_description`（此前轮次已缓存的扩展信息）且用户未使用"重新识别/看原图"等关键词，
      则跳过这张图的内联**（描述会通过 `build_attachment_message_suffix` 以文本形式挂在消息末尾），
      实现"默认只解读最后一次新图、已解读过的图走文本缓存"的策略；
    - 有需要内联的图时返回 list[{text}, {image_url}, ...]；text 若空给占位，避免某些网关拒 400；
    - 单图 / 合计 / 张数分别受 `_VISION_CURRENT_*` 约束；超额图跳过，文字清单里仍保留 uuid。
    """
    text = text or ""
    if not attach_rows:
        return text
    if not vision_enabled:
        # 用户明确关闭了识图：不内联 image_url，改让 AI 通过 read_chat_attachment 拿 data_url 兜底
        logger.info(
            "vision: 用户关闭识图开关，跳过 image_url 内联 user=%s images=%d",
            (user.get("username") or "?"),
            sum(1 for r in attach_rows if (r.get("kind") or "").lower() == "image"),
        )
        return text
    force_reanalyze = _message_requests_reanalyze(text)
    images: list[dict] = []
    total = 0
    skipped_cached = 0
    username = (user.get("username") or "default")
    for row in attach_rows:
        if (row.get("kind") or "").lower() != "image":
            continue
        # 已有扩展信息 & 非"重新识别"请求：跳过内联，描述走消息末尾 📎 清单以文本形式提供
        cached_desc = (row.get("ai_description") or "").strip()
        if cached_desc and not force_reanalyze:
            skipped_cached += 1
            continue
        if _VISION_CURRENT_COUNT > 0 and len(images) >= _VISION_CURRENT_COUNT:
            logger.info("vision: 本轮图片数量达上限 %d，后续跳过", _VISION_CURRENT_COUNT)
            break
        size = int(row.get("size_bytes") or 0)
        if (
            _VISION_CURRENT_TOTAL_BYTES > 0
            and size > 0
            and total + size > _VISION_CURRENT_TOTAL_BYTES
        ):
            logger.info("vision: 本轮累计图片超上限，剩余图片跳过 uuid=%s", row.get("uuid"))
            break
        part_result = _image_row_to_image_url_part(row, username, _VISION_CURRENT_PER_BYTES)
        if part_result is None:
            continue
        part, dim_meta = part_result
        hint = _format_inline_image_dimension_hint(row, dim_meta)
        if hint:
            images.append({"type": "text", "text": hint})
        images.append(part)
        total += size if size > 0 else 0
    if skipped_cached:
        logger.info(
            "vision: 跳过已缓存 ai_description 的图片 count=%d user=%s force_reanalyze=%s",
            skipped_cached, (user.get("username") or "?"), force_reanalyze,
        )
    if not images:
        # 即使所有图都走了文本缓存，也保持 content 为字符串，由附件清单里"AI 已识别内容"承载
        return text
    text_part_content = text if text.strip() else "（用户上传了图片，请基于图像内容作答）"
    logger.info(
        "vision: 本轮内联 image_url 段 count=%d total_bytes=%d skipped_cached=%d reanalyze=%s user=%s",
        len(images), total, skipped_cached, force_reanalyze, (user.get("username") or "?"),
    )
    return [{"type": "text", "text": text_part_content}, *images]


async def _inject_history_image_memory(
    messages: list[dict],
    db,
    user: dict,
    *,
    vision_enabled: bool = True,
    force: bool = False,
) -> None:
    """为最近若干条 user 历史消息内联其 📎 附件清单中的 image，跨轮维持视觉记忆。

    默认行为（**force=False**）：**不再跨轮内联历史图**。这是产品策略变更——
    AI 首次解读一张图后会把提取的内容通过 `save_image_description` 写回附件行
    作为扩展信息，后续轮次 📎 附件清单里已经挂着这段"已识别内容"文本，
    模型无需再吃一遍 base64 像素；只有用户显式要求重新分析原图时才会绕过缓存。

    force=True：保留老行为，把最近 `_VISION_HISTORY_LOOKBACK_USER_MSGS` 条
    带图 user 消息 content 改为 list 多模态，适用于"确实需要同时对比多张图"
    的极少数场景。调用方目前默认不会传 True；该参数保留作后门。

    当用户关闭识图开关时也直接跳过（不动 messages）。
    """
    if not messages:
        return
    if not vision_enabled:
        return
    try:
        from api.chat_attachments import load_attachments_for_user as _load_user_attachments
    except Exception as exc:
        logger.warning("vision: 导入附件加载器失败 err=%s", exc)
        return
    if not force:
        # 默认模式：不跨轮内联 base64；但把历史 user 消息里出现过的图片 uuid 对应的
        # ai_description 以文本形式"补齐"到消息末尾（老会话 DB 里保存的 content 不含"AI 已识别内容"段）。
        # 这样 AI 看历史就能复用此前解读，无需再调工具；而且完全不吃图像 token。
        last_user_idx_ = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                last_user_idx_ = i
                break
        if last_user_idx_ <= 0:
            return
        touched = 0
        for i in range(last_user_idx_):
            m = messages[i]
            if m.get("role") != "user":
                continue
            c = m.get("content")
            if not isinstance(c, str) or "uuid:" not in c:
                continue
            if "AI 已识别内容" in c:
                continue  # 已是新格式，跳过
            uuids = _extract_attachment_uuids_from_text(c)
            if not uuids:
                continue
            try:
                rows = await _load_user_attachments(db, user["id"], uuids)
            except Exception as exc_hist:
                logger.debug("vision: 历史描述补齐读附件失败 idx=%s err=%s", i, exc_hist)
                continue
            blocks: list[str] = []
            for row in rows:
                if (row.get("kind") or "").lower() != "image":
                    continue
                desc = (row.get("ai_description") or "").strip()
                if not desc:
                    continue
                short = desc if len(desc) <= 1500 else (desc[:1500] + " …（已截断）")
                uuid_s = row.get("uuid") or ""
                blocks.append(
                    f"> 📎 `{uuid_s}` 这张图此前已由 AI 识别，扩展信息：\n> "
                    + short.replace("\n", "\n> ")
                )
            if blocks:
                messages[i] = {"role": "user", "content": c + "\n\n" + "\n\n".join(blocks)}
                touched += 1
        if touched:
            logger.info("vision: 历史图片改用 ai_description 文本补齐 touched=%d", touched)
        return
    # force=True 分支：保留原"跨轮内联 base64"行为（调用方默认不会触发；预留给极少数多图对比场景）
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break
    target_indices: list[int] = []
    for i in range(last_user_idx - 1, -1, -1):
        if len(target_indices) >= _VISION_HISTORY_LOOKBACK_USER_MSGS:
            break
        m = messages[i]
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if not isinstance(c, str) or "uuid:" not in c:
            continue
        target_indices.append(i)
    if not target_indices:
        return
    total_bytes = 0
    total_imgs = 0
    username = (user.get("username") or "default")
    target_indices.sort()
    for idx in target_indices:
        if _VISION_HISTORY_COUNT > 0 and total_imgs >= _VISION_HISTORY_COUNT:
            break
        msg = messages[idx]
        text = msg.get("content") or ""
        uuids = _extract_attachment_uuids_from_text(text)
        if not uuids:
            continue
        try:
            rows = await _load_user_attachments(db, user["id"], uuids)
        except Exception as exc:
            logger.warning("vision: 加载历史附件失败 idx=%s err=%s", idx, exc)
            continue
        parts: list[dict] = []
        for row in rows:
            if (row.get("kind") or "").lower() != "image":
                continue
            if _VISION_HISTORY_COUNT > 0 and total_imgs >= _VISION_HISTORY_COUNT:
                break
            size = int(row.get("size_bytes") or 0)
            if (
                _VISION_HISTORY_TOTAL_BYTES > 0
                and size > 0
                and total_bytes + size > _VISION_HISTORY_TOTAL_BYTES
            ):
                break
            part_result = _image_row_to_image_url_part(row, username, _VISION_HISTORY_PER_BYTES)
            if part_result is None:
                continue
            part, dim_meta = part_result
            hint = _format_inline_image_dimension_hint(row, dim_meta)
            if hint:
                parts.append({"type": "text", "text": hint})
            parts.append(part)
            total_imgs += 1
            total_bytes += size if size > 0 else 0
        if parts:
            messages[idx] = {
                "role": "user",
                "content": [{"type": "text", "text": text}, *parts],
            }
            logger.info(
                "vision: 跨轮注入历史图片 idx=%d count=%d total_bytes=%d",
                idx, len(parts), total_bytes,
            )


# ─────────────────────────── 视觉错误降级：压缩/剥离重试 ───────────────────────────
# provider/网关常见的三类错误：
#   1. 输入 token 总长超限（如 "Range of input length should be [1, 29804]"）
#   2. 单张图片过大 / 编码不支持
#   3. 模型根本不支持图像（厂商配置错位、选错模型）
# 对 1/2，我们按阶梯压缩所有已内联的 image_url 段重试；多次仍失败就"剥离全部图片 +
# 提示 AI 改走 read_chat_attachment"兜底。对 3，直接剥离一次就停止，避免死循环。
_VISION_ERR_INPUT_TOO_LONG = "input_too_long"
_VISION_ERR_IMAGE_TOO_LARGE = "image_too_large"
_VISION_ERR_UNSUPPORTED = "vision_unsupported"
_VISION_ERR_OTHER = "other"

# 压缩阶梯：(最长边像素, JPEG 质量, 标签)。由宽到窄逐级降级。
_VISION_COMPRESS_STAGES: tuple[tuple[int, int, str], ...] = (
    (1536, 85, "1536/q85"),
    (1024, 80, "1024/q80"),
    (768, 70, "768/q70"),
    (512, 60, "512/q60"),
)


def _classify_vision_error(status_code: int, err_text: str) -> str:
    """按 provider 错误文本判别是"输入太长/图太大"还是"不支持图像"。

    命中 unsupported 时上层不再压缩重试，直接剥离图片做最后一次尝试；
    命中 input_too_long / image_too_large 时走压缩阶梯；其它返回 OTHER 由调用方原样冒泡。
    """
    t = (err_text or "").lower()
    unsupported_markers = (
        "does not support image", "no multimodal", "vision is not supported",
        "not a vision model", "image input is not supported",
        "unsupported image", "image_url is not supported",
        "does not accept image", "no image support",
        "不支持图像", "不支持多模态", "不支持图片", "不是视觉模型",
    )
    if any(m in t for m in unsupported_markers):
        return _VISION_ERR_UNSUPPORTED
    length_markers = (
        "range of input length", "input length", "context length",
        "maximum context length", "maximum token", "token limit",
        "input is too large", "too many tokens", "reduce the length",
        "invalidparameter", "algo.invalidparameter",
        "超长", "超过长度", "超出长度", "输入长度", "上下文长度",
    )
    if any(m in t for m in length_markers):
        return _VISION_ERR_INPUT_TOO_LONG
    image_size_markers = (
        "image too large", "image_too_large", "invalid image size",
        "payload too large", "request entity too large",
        "图片过大", "图像过大",
    )
    if any(m in t for m in image_size_markers):
        return _VISION_ERR_IMAGE_TOO_LARGE
    # 部分网关 413 未必带上面关键字，但状态码本身就是"过大"
    if status_code == 413:
        return _VISION_ERR_IMAGE_TOO_LARGE
    return _VISION_ERR_OTHER


def _messages_have_image_url(messages: list[dict]) -> bool:
    for m in messages or []:
        c = (m or {}).get("content")
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    return True
    return False


def _shrink_messages_vision_inline(messages: list[dict]) -> int:
    """发送前再次收紧 messages 中所有 image_url data URL。返回重编码段数。"""
    from services.vision_image import reencode_data_url_for_vision, vision_inline_max_b64_chars

    cap = vision_inline_max_b64_chars()
    count = 0
    for m in messages or []:
        c = (m or {}).get("content")
        if not isinstance(c, list):
            continue
        for p in c:
            if not isinstance(p, dict) or p.get("type") != "image_url":
                continue
            old_url = ((p.get("image_url") or {}).get("url") or "")
            if not old_url.startswith("data:") or _is_vision_placeholder_data_url(old_url):
                continue
            new_url = reencode_data_url_for_vision(old_url, max_b64_chars=cap)
            if not new_url or new_url == old_url:
                continue
            detail = (p.get("image_url") or {}).get("detail") or "auto"
            if len(new_url) > cap // 2:
                detail = "low"
            p["image_url"] = {"url": new_url, "detail": detail}
            count += 1
    if count:
        logger.info("vision: 发送前收紧 image_url count=%d cap=%d", count, cap)
    return count


def _compress_messages_image_urls(
    messages: list[dict],
    *,
    max_side: int,
    jpeg_quality: int,
) -> int:
    """视觉降级：按指定档位重编码（兼容旧阶梯参数）。返回实际被重编码的段数。"""
    del max_side, jpeg_quality  # 统一走 vision_image 多档阶梯
    return _shrink_messages_vision_inline(messages)


# 历史 base64 占位：让 provider 不再把同一张图的 base64 反复当 token 计入。
# 必须用**可成功 base64 解码**的占位图：若写任意 ASCII（如旧版 "<omitted:history>"），
# 阿里云等网关会整包校验并对 data URL 解码，直接报 `base64 decode fail`。
_VISION_PLACEHOLDER_PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_VISION_PLACEHOLDER_IMAGE_URL = f"data:image/png;base64,{_VISION_PLACEHOLDER_PIXEL_PNG_B64}"
# 旧版错误占位标记（仍可能残留在历史消息里）；见到则升级成合法占位 URL。
_VISION_BASE64_OMITTED_TAG = "<omitted:history>"


def _is_vision_placeholder_data_url(url: str) -> bool:
    """是否为「历史去重」用的占位 data URL（或旧版非法占位）。"""
    u = url or ""
    if not u.startswith("data:") or ";base64," not in u:
        return False
    if u == _VISION_PLACEHOLDER_IMAGE_URL:
        return True
    if _VISION_BASE64_OMITTED_TAG in u:
        return True
    return False


def _sanitize_legacy_invalid_vision_data_urls(messages: list[dict]) -> int:
    """把旧版 `;base64,<omitted:history>` 等非合法载荷升级为合法 1x1 占位，避免上游解码失败。

    返回修复的段数。
    """
    fixed = 0
    for m in messages or []:
        c = (m or {}).get("content")
        if isinstance(c, list):
            for p in c:
                if not isinstance(p, dict) or p.get("type") != "image_url":
                    continue
                url = ((p.get("image_url") or {}).get("url") or "")
                if _VISION_BASE64_OMITTED_TAG in url and url != _VISION_PLACEHOLDER_IMAGE_URL:
                    detail = (p.get("image_url") or {}).get("detail") or "auto"
                    p["image_url"] = {"url": _VISION_PLACEHOLDER_IMAGE_URL, "detail": detail}
                    fixed += 1
        elif isinstance(c, str) and (m or {}).get("role") == "tool":
            if _VISION_BASE64_OMITTED_TAG not in c or '"data_url"' not in c:
                continue
            try:
                obj = json.loads(c)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            du = obj.get("data_url")
            if isinstance(du, str) and _VISION_BASE64_OMITTED_TAG in du:
                obj["data_url"] = _VISION_PLACEHOLDER_IMAGE_URL
                obj["_legacy_placeholder_fixed"] = True
                m["content"] = json.dumps(obj, ensure_ascii=False)
                fixed += 1
    if fixed:
        logger.info("vision: 升级旧版非法 base64 占位 fixed=%d", fixed)
    return fixed


def _messages_indicate_image_attachment_or_degradation(messages: list[dict]) -> bool:
    """当前上下文是否明显包含图片附件/视觉降级任务。"""
    for m in messages or []:
        c = (m or {}).get("content")
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    return True
        if not isinstance(c, str):
            continue
        if "【系统提示 · 视觉降级】" in c or "image_url 段已被移除" in c:
            return True
        if "📎 附件" in c and ("kind=image" in c or "image ·" in c or "image)" in c):
            return True
        if "read_chat_attachment" in c and ("data_url" in c or "图片" in c):
            return True
    return False


def _looks_like_image_blind_reply(text: str) -> bool:
    """模型没有用工具而直接回答看不到图时，拦截并继续执行。"""
    t = (text or "").strip().lower()
    if not t:
        return False
    markers = (
        "看不到图片",
        "看不到图",
        "无法看到图片",
        "无法查看图片",
        "不能查看图片",
        "不能直接查看图片",
        "无法直接查看图片",
        "无法直接访问图片",
        "我无法查看",
        "我不能查看",
        "我看不到",
        "图片太大",
        "图片过大",
        "图像过大",
        "只有一个缩略图",
        "仅有缩略图",
        "没有图片内容",
        "无法获取图片内容",
        "请重新上传",
    )
    return any(m in t for m in markers)


def _deduplicate_vision_base64_in_messages(messages: list[dict]) -> dict:
    """把 messages 中"非最后一次"出现的图像 base64 替换为极短占位。

    覆盖两种 base64 来源：
      1) user/assistant 消息里的多模态 `image_url.url` 段（data:image/...;base64,...）；
      2) `role=tool` 消息里 `read_chat_attachment` 返回 JSON 的 `data_url` 字段。

    策略：遍历找出**最后一个**含 image_url base64 的消息索引，以及**最后一个**含
    tool data_url base64 的消息索引；两者以外的所有 base64 都压缩成短占位。
    这样每轮请求只把"当下真正需要给视觉模型看的那张图"完整送到 provider，
    多轮历史里的 base64 不再重复计入 token 预算——既省 token，又避开上游
    `Range of input length` 这类因历史 base64 累积而爆窗的错误。

    返回统计：
      {"placeholders": N, "bytes_saved": X, "last_img_idx": i, "last_tool_idx": j}
    """
    if not messages:
        return {"placeholders": 0, "bytes_saved": 0, "last_img_idx": -1, "last_tool_idx": -1}

    _sanitize_legacy_invalid_vision_data_urls(messages)

    last_img_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        c = (messages[i] or {}).get("content")
        if not isinstance(c, list):
            continue
        for p in c:
            if not isinstance(p, dict):
                continue
            if p.get("type") != "image_url":
                continue
            url = ((p.get("image_url") or {}).get("url") or "")
            if (
                url.startswith("data:")
                and ";base64," in url
                and not _is_vision_placeholder_data_url(url)
            ):
                last_img_idx = i
                break
        if last_img_idx == i:
            break

    last_tool_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i] or {}
        if m.get("role") != "tool":
            continue
        c = m.get("content")
        if not isinstance(c, str) or '"data_url"' not in c or ";base64," not in c:
            continue
        try:
            obj = json.loads(c)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        du = obj.get("data_url")
        if not (isinstance(du, str) and du.startswith("data:") and ";base64," in du):
            continue
        if _is_vision_placeholder_data_url(du):
            continue
        last_tool_idx = i
        break

    placeholders = 0
    bytes_saved = 0

    for i, m in enumerate(messages):
        c = (m or {}).get("content")
        # 情况 1：多模态 image_url 段
        # last_img_idx < 0 表示本轮请求里没有任何「非占位」的 image_url，切勿误替换。
        if isinstance(c, list) and last_img_idx >= 0 and i != last_img_idx:
            for p in c:
                if not isinstance(p, dict) or p.get("type") != "image_url":
                    continue
                url = ((p.get("image_url") or {}).get("url") or "")
                if not (url.startswith("data:") and ";base64," in url):
                    continue
                if _is_vision_placeholder_data_url(url):
                    continue  # 已占位，跳过
                old_len = len(url)
                detail = (p.get("image_url") or {}).get("detail") or "auto"
                p["image_url"] = {
                    "url": _VISION_PLACEHOLDER_IMAGE_URL,
                    "detail": detail,
                }
                placeholders += 1
                bytes_saved += old_len - len(p["image_url"]["url"])
        # 情况 2：tool 返回 JSON 内的 data_url
        elif (
            isinstance(c, str)
            and (m or {}).get("role") == "tool"
            and last_tool_idx >= 0
            and i != last_tool_idx
        ):
            if '"data_url"' not in c or ";base64," not in c:
                continue
            try:
                obj = json.loads(c)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            du = obj.get("data_url")
            if not (isinstance(du, str) and du.startswith("data:") and ";base64," in du):
                continue
            if _is_vision_placeholder_data_url(du):
                continue
            orig_bytes = obj.get("bytes")
            obj["data_url"] = _VISION_PLACEHOLDER_IMAGE_URL
            obj["_omitted"] = True
            obj["_hint"] = (
                "历史 data_url 已占位化以避免 base64 被反复当作 token 预算；"
                "如仍需此图的像素内容，请在最新轮重新调用 read_chat_attachment(uuid=...)"
            )
            if orig_bytes is not None:
                obj["_original_bytes"] = orig_bytes
            new_c = json.dumps(obj, ensure_ascii=False)
            bytes_saved += len(c) - len(new_c)
            m["content"] = new_c
            placeholders += 1

    return {
        "placeholders": placeholders,
        "bytes_saved": bytes_saved,
        "last_img_idx": last_img_idx,
        "last_tool_idx": last_tool_idx,
    }


def _strip_messages_image_urls(messages: list[dict]) -> int:
    """剥离 messages 里所有 image_url 段。返回被剥离的段数。

    对被剥过图的 user 消息，把 content 还原成字符串并在末尾追加「视觉降级」提示，
    引导 AI 改用 `read_chat_attachment(uuid=...)` 重新拿图片 data_url。
    """
    stripped = 0
    for m in messages or []:
        c = (m or {}).get("content")
        if not isinstance(c, list):
            continue
        text_segs: list[str] = []
        had_image = False
        for p in c:
            if isinstance(p, dict) and p.get("type") == "image_url":
                had_image = True
                stripped += 1
                continue
            if isinstance(p, dict) and p.get("type") == "text":
                text_segs.append(p.get("text") or "")
        if not had_image:
            continue
        base_text = "\n\n".join(s for s in text_segs if s is not None).strip()
        if (m.get("role") == "user") and base_text:
            base_text += (
                "\n\n【系统提示 · 视觉降级】："
                "上游网关拒绝了多模态图像输入（可能因为 input token 超限 / 图片过大 / 模型不支持图像）。"
                "本消息里的 image_url 段已被移除。"
                "如需基于图像作答，请调用 `read_chat_attachment(uuid=...)` 获取图片的 `data_url` 或文字 OCR 描述；"
                "严禁仅凭元信息（mime/size）回答「看不清」。"
            )
        elif not base_text:
            base_text = "（图片已被移除）"
        m["content"] = base_text
    return stripped


def _promote_recent_tool_image_to_user_message(messages: list[dict]) -> bool:
    """把最近一条带真实 data_url 的工具结果，提升为视觉模型真正可见的 image_url user 段。

    OpenAI 兼容 API 中，`role=tool` 消息文本里的 base64 data_url **不会**被当作图像解析，
    模型其实看不到工具产出的图（标注结果图、grid/probe 预览图）。这里把最新一条工具图
    转成标准的 `image_url` 段挂到一条合成 user 消息上，并把 tool 文本里的副本占位化以免重复计费。
    幂等：占位化后下次扫描不到真实 data_url，不会重复注入。
    """
    if not messages:
        return False
    idx = -1
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i] or {}
        if m.get("role") != "tool":
            continue
        c = m.get("content")
        if not isinstance(c, str) or '"data_url"' not in c or ";base64," not in c:
            continue
        try:
            obj = json.loads(c)
        except Exception:
            continue
        du = obj.get("data_url") if isinstance(obj, dict) else None
        if (
            isinstance(du, str)
            and du.startswith("data:")
            and ";base64," in du
            and not _is_vision_placeholder_data_url(du)
        ):
            idx = i
            break
    if idx < 0:
        return False
    # 仅当该 tool 消息位于消息尾部（本轮刚产生、之后没有别的角色）时提升，保证序列合法
    for j in range(idx + 1, len(messages)):
        if (messages[j] or {}).get("role") not in ("tool",):
            return False
    try:
        obj = json.loads(messages[idx]["content"])
    except Exception:
        return False
    du = obj.get("data_url")
    obj["data_url"] = _VISION_PLACEHOLDER_IMAGE_URL
    obj["_promoted_to_image_url"] = True
    messages[idx]["content"] = json.dumps(obj, ensure_ascii=False)
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": "（系统）上一步工具生成/返回的图片如下，请据此核对标注是否对准目标并继续："},
            {"type": "image_url", "image_url": {"url": du, "detail": "high"}},
        ],
    })
    return True


async def _post_chat_with_vision_fallback(
    client,
    *,
    api_url: str,
    headers: dict,
    payload: dict,
    messages: list[dict],
    status_sink=None,
):
    """单次聊天请求的通用封装：遇到 vision 相关错误时自适应压缩/剥离后重试。

    - `payload` 应已包含 model/tools/tool_choice/stream 等；messages 由本函数在内置
      `payload["messages"] = messages`，保证压缩就地改动后下一次请求看到新内容。
    - `status_sink` 可选，形如 `async def sink(info: dict) -> None`，用于给 SSE 流
      推送"正在压缩重试"等用户可见的进度事件；不传则只打日志。
    - 返回最后一次 `resp`；调用方按常规 `resp.status_code != 200` 分支继续处理。
    """

    async def _notify(info: dict) -> None:
        if status_sink is None:
            return
        try:
            await status_sink(info)
        except Exception:
            pass

    payload = dict(payload)  # 避免污染调用方
    # 工具产出的图（标注结果 / grid / probe 预览）默认只在 tool 文本里，视觉模型看不到；
    # 这里先把最新一条提升为标准 image_url user 段，确保模型能真正看到、据此自检修正。
    if _promote_recent_tool_image_to_user_message(messages):
        logger.info("vision: 已把最近工具产出图提升为可见 image_url 段")
    # 发送前去重：把历史轮次里的 image_url/data_url base64 替换为占位，
    # 只保留最近一次出现的完整像素；避免多轮把同一张图的 base64 反复塞进请求，
    # 让 provider 按字符长度算 token 直接打爆窗口（如 `Range of input length`）。
    _dedup = _deduplicate_vision_base64_in_messages(messages)
    if _dedup.get("placeholders"):
        logger.info(
            "vision-dedupe: 历史 base64 占位化 count=%d bytes_saved=%d (last_img=%d last_tool=%d)",
            _dedup["placeholders"], _dedup["bytes_saved"],
            _dedup["last_img_idx"], _dedup["last_tool_idx"],
        )
    _shrink_messages_vision_inline(messages)
    payload["messages"] = messages
    resp = await client.post(api_url, headers=headers, json=payload)
    if resp.status_code == 200:
        return resp

    if not _messages_have_image_url(messages):
        return resp

    try:
        err_text = resp.text or ""
    except Exception:
        err_text = ""
    kind = _classify_vision_error(resp.status_code, err_text)
    if kind == _VISION_ERR_OTHER:
        return resp

    logger.info(
        "vision-fallback: 触发降级 status=%s kind=%s err=%s",
        resp.status_code, kind, (err_text or "")[:240],
    )

    last_resp = resp
    if kind in (_VISION_ERR_INPUT_TOO_LONG, _VISION_ERR_IMAGE_TOO_LARGE):
        for (max_side, quality, label) in _VISION_COMPRESS_STAGES:
            n = _compress_messages_image_urls(messages, max_side=max_side, jpeg_quality=quality)
            if n <= 0:
                # PIL 缺失或全部失败：直接转兜底
                break
            await _notify({
                "action": "vision_retry",
                "stage": label,
                "compressed": n,
                "reason": kind,
            })
            logger.info("vision-fallback: 压缩后重试 stage=%s compressed=%d", label, n)
            payload["messages"] = messages
            resp2 = await client.post(api_url, headers=headers, json=payload)
            last_resp = resp2
            if resp2.status_code == 200:
                return resp2
            try:
                err2 = resp2.text or ""
            except Exception:
                err2 = ""
            k2 = _classify_vision_error(resp2.status_code, err2)
            if k2 == _VISION_ERR_UNSUPPORTED:
                kind = _VISION_ERR_UNSUPPORTED
                break
            if k2 == _VISION_ERR_OTHER:
                # 新错误与视觉无关，没必要继续压缩
                return resp2
            # 仍是长度/图太大：继续下一档
            kind = k2

    # 最终兜底：剥离所有 image_url 段
    n_strip = _strip_messages_image_urls(messages)
    if n_strip <= 0:
        return last_resp
    await _notify({
        "action": "vision_retry",
        "stage": "strip",
        "stripped": n_strip,
        "reason": kind,
    })
    logger.info("vision-fallback: 剥离全部图片重试 stripped=%d kind=%s", n_strip, kind)
    payload["messages"] = messages
    resp3 = await client.post(api_url, headers=headers, json=payload)
    if resp3.status_code == 200 or resp3.status_code != last_resp.status_code:
        return resp3
    return resp3


async def _stream_chat_with_vision_fallback(
    client,
    *,
    api_url: str,
    headers: dict,
    payload: dict,
    messages: list[dict],
):
    """流式版的 `_post_chat_with_vision_fallback`：异步生成器，按 chunk 实时透出事件。

    每收到一个上游 SSE chunk 就立刻 yield 一条事件 dict，调用方据此把 token
    边到边推给浏览器；这样从模型吐出第一个字开始用户就能看到，不再有"步骤
    之间长时间空白"的体感。事件类型：

    - {"kind": "content", "text": str}：assistant 正文增量；
    - {"kind": "reasoning", "text": str}：思考链增量（部分模型独立通道）；
    - {"kind": "vision_retry", "stage": str, "n": int, "reason": str}：触发
      视觉降级（压缩 / 剥离图片）后将进行下一次尝试；
    - {"kind": "http_error", "status_code": int, "body": str}：上游非 200
      且无法继续降级，调用方应中止本轮；
    - {"kind": "done", "content": str, "reasoning": str, "tool_calls": list,
       "finish_reason": str|None}：本轮 stream 正常结束；调用方应根据
      `tool_calls` 是否为空决定走工具分支还是把 `content` 作为最终回复。

    与非流式版本相同的视觉降级策略（`_VISION_COMPRESS_STAGES` 阶梯压缩 + 最终
    剥离图片），只在首帧出现 4xx/5xx 时触发；一旦已经开始接收 200 流，就直接
    顺流读完，不再回退（流中错误由 httpx 抛出，由外层 catch）。
    """
    payload = dict(payload)
    payload["stream"] = True
    # 工具产出的图（标注结果 / grid / probe 预览）默认只在 tool 文本里，视觉模型看不到；
    # 提升为标准 image_url user 段，模型才能看到并据此自检修正标注。
    if _promote_recent_tool_image_to_user_message(messages):
        logger.info("vision(stream): 已把最近工具产出图提升为可见 image_url 段")
    # 与非流式版保持一致：发送前把历史轮次里重复出现的 base64 占位化，避免
    # 同一张图反复塞进上下文导致 provider 算 token 时打爆窗口
    _dedup = _deduplicate_vision_base64_in_messages(messages)
    if _dedup.get("placeholders"):
        logger.info(
            "vision-dedupe(stream): 历史 base64 占位化 count=%d bytes_saved=%d",
            _dedup["placeholders"], _dedup["bytes_saved"],
        )
    _shrink_messages_vision_inline(messages)

    # 视觉降级状态机：先尝试原始一次；若首帧报错且消息里有图，则按压缩阶梯逐档
    # 重试，最后再尝试整体剥离图片。没图的请求则只跑 attempts[0]
    attempts: list[str] = ["original"]
    if _messages_have_image_url(messages):
        for (_max_side, _q, _label) in _VISION_COMPRESS_STAGES:
            attempts.append(f"compress:{_label}")
        attempts.append("strip")

    last_status = 0
    last_body = ""

    for attempt in attempts:
        if attempt.startswith("compress:"):
            label = attempt.split(":", 1)[1]
            stage = next(
                ((ms, q, lb) for (ms, q, lb) in _VISION_COMPRESS_STAGES if lb == label),
                None,
            )
            if not stage:
                continue
            n = _compress_messages_image_urls(messages, max_side=stage[0], jpeg_quality=stage[1])
            if n <= 0:
                # PIL 缺失或全部失败：跳过这一档，让后续 strip 来兜底
                continue
            logger.info("vision-fallback(stream): 压缩重试 stage=%s compressed=%d", label, n)
            yield {
                "kind": "vision_retry",
                "stage": label,
                "n": n,
                "reason": "input_too_long_or_image_too_large",
            }
        elif attempt == "strip":
            n = _strip_messages_image_urls(messages)
            if n <= 0:
                continue
            logger.info("vision-fallback(stream): 剥离图片重试 stripped=%d", n)
            yield {
                "kind": "vision_retry",
                "stage": "strip",
                "n": n,
                "reason": "vision_final_fallback",
            }

        payload["messages"] = messages
        accumulated_content: list[str] = []
        accumulated_reasoning: list[str] = []
        accumulated_tool_calls: list[dict] = []
        finish_reason: str | None = None

        async with client.stream("POST", api_url, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                try:
                    body_bytes = await resp.aread()
                    last_body = body_bytes.decode("utf-8", errors="replace")
                except Exception:
                    last_body = ""
                last_status = resp.status_code
                # 没图就没法降级，直接报错给上层
                if not _messages_have_image_url(messages):
                    yield {"kind": "http_error", "status_code": last_status, "body": last_body}
                    return
                kind = _classify_vision_error(last_status, last_body)
                if kind == _VISION_ERR_OTHER:
                    yield {"kind": "http_error", "status_code": last_status, "body": last_body}
                    return
                # 这次失败，进入下一次 attempt 继续降级（外层 for 自然推进）
                continue

            async for line in resp.aiter_lines():
                if not line:
                    continue
                chunk = parse_stream_line(line)
                if chunk is None:
                    continue
                content_d, reasoning_d, tool_deltas, fr = extract_stream_delta(chunk)
                if content_d:
                    accumulated_content.append(content_d)
                    yield {"kind": "content", "text": content_d}
                if reasoning_d:
                    accumulated_reasoning.append(reasoning_d)
                    yield {"kind": "reasoning", "text": reasoning_d}
                if tool_deltas:
                    merge_tool_call_deltas(accumulated_tool_calls, tool_deltas)
                if fr:
                    finish_reason = fr

        yield {
            "kind": "done",
            "content": "".join(accumulated_content),
            "reasoning": "".join(accumulated_reasoning),
            "tool_calls": finalize_tool_calls(accumulated_tool_calls),
            "finish_reason": finish_reason,
        }
        return

    # 所有降级 attempt 都没能成功
    yield {
        "kind": "http_error",
        "status_code": last_status or 502,
        "body": last_body or "vision fallback exhausted",
    }


# 上下文分段占比（在总预算内：主机列表、分组、主机知识、控制台输出、历史消息）
CONTEXT_RATIO_HOSTS = 0.15
CONTEXT_RATIO_GROUPS = 0.10
CONTEXT_RATIO_KNOWLEDGE = 0.15
CONTEXT_RATIO_TERMINAL = 0.30
CONTEXT_RATIO_HISTORY = 0.30
CONTEXT_HISTORY_MAX_MESSAGES = 20
CONTEXT_HISTORY_RECENT_FULL = 6
# 用户未配置上下文大小时使用的默认总字符预算，避免请求体无限大导致慢/失败
CONTEXT_SIZE_DEFAULT = 262_144
CONTEXT_MAX_HOST_ITEMS = 120
CONTEXT_MAX_GROUP_ITEMS = 120

# 写入 messages 的 tool 结果单条最大长度，防止 get_terminal_buffer 等大输出撑爆请求
TOOL_RESULT_IN_MESSAGE_MAX = 6_000
TOOL_RESULT_PREVIEW_MAX = 12_000
ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
AUTO_CONTEXT_MIN = 16_000
AUTO_CONTEXT_MAX = 262_144
TOOL_RESULT_CACHE_MAX_CHARS = 1_000_000


def _assistant_content_for_summary(raw: str) -> str:
    """仅保留助手的「指令/决策」部分，去掉程序输出日志，供生成会话提示词摘要使用。"""
    return assistant_content_for_summary(raw)


def _truncate_segment(text: str, max_chars: int, suffix: str = "…（已截断）", tail: bool = False) -> str:
    """将文本截断到 max_chars。tail=True 时保留末尾（用于控制台输出）。"""
    if not text or max_chars <= 0 or len(text) <= max_chars:
        return text
    if tail:
        # 保留最后一段，前面加截断提示
        keep = max_chars - len(suffix)
        if keep <= 0:
            return text[-max_chars:]
        return suffix + text[-keep:]
    return text[: max_chars - len(suffix)] + suffix


def _tool_result_preview(text: str, max_chars: int = TOOL_RESULT_PREVIEW_MAX) -> str:
    """给前端 Log 面板使用的工具结果预览。过长时偏保留末尾（与终端/命令输出一致）。"""
    if not text or len(text) <= max_chars:
        return text
    if max_chars <= 64:
        return text[:max_chars]
    return abbreviate_text_tail_focus(text, max_chars, head_ratio=0.15)


def _tool_args_preview(args: dict, max_chars: int = 2000) -> dict:
    """SSE/前端日志用的参数预览：仅对密钥类字段整段隐藏；code/content 等与排障、审计相关的大字段用截断保留头尾可读性，不再整段「已隐藏」（历史里另存完整 tool_calls）。"""
    if not isinstance(args, dict):
        return {}
    redact_fully = {
        "private_key",
        "password",
        "token",
        "api_key",
        "secret",
    }
    large_field_keys = {"content", "content_b64", "code"}
    large_limit = 262_144
    out: dict = {}
    for key, value in args.items():
        key_s = str(key)
        kl = key_s.lower()
        if kl in redact_fully:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            out[key_s] = f"（已隐藏，长度 {len(text)} 字符）"
            continue
        limit = large_limit if kl in large_field_keys else max_chars
        if isinstance(value, str):
            if len(value) > limit:
                out[key_s] = value[:limit] + f"…（已截断，长度 {len(value)} 字符）"
            else:
                out[key_s] = value
            continue
        text = json.dumps(value, ensure_ascii=False)
        if len(text) > limit:
            out[key_s] = text[:limit] + f"…（已截断，长度 {len(text)} 字符）"
        else:
            out[key_s] = value
    return out


# 同一轮 tool_calls 里，若 `ask_user_choice` 排在下列工具**之后**，会出现「不可逆操作已完成却仍弹确认」。
# 流式 Agent 会在执行前重排：把滞后的选择题挪到「首个」此类工具之前（用户点选后再跑后续工具）。
# 集成/API 会话不按此重排：那边在 ask_user_choice 处会直接 return，重排会导致剩余工具本轮无法执行。
_AGENT_IRREVERSIBLE_TOOL_NAMES = frozenset({
    "delete_host",
    "delete_host_tag",
    "delete_group",
    "delete_credential",
    "delete_maintenance",
    "delete_user",
    "delete_ai_session",
    "delete_best_practice",
    "fs_delete",
    "revoke_host_share",
})


def _tool_call_names_from_full_calls(full_tool_calls: list) -> list[str]:
    out: list[str] = []
    for tc in full_tool_calls or []:
        fn = dict((tc or {}).get("function") or {})
        out.append((fn.get("name") or "").strip())
    return out


def _tool_call_reorder_indices_confirm_before_irreversible(full_tool_calls: list) -> list[int]:
    """把出现在首个不可逆工具之后的 ask_user_choice 整段前移，保持其余相对顺序。"""
    n = len(full_tool_calls or [])
    if n <= 1:
        return list(range(n))
    names = _tool_call_names_from_full_calls(full_tool_calls)
    first_irrev: int | None = None
    for i, name in enumerate(names):
        if name in _AGENT_IRREVERSIBLE_TOOL_NAMES:
            first_irrev = i
            break
    if first_irrev is None:
        return list(range(n))
    late_ask = [j for j in range(n) if names[j] == "ask_user_choice" and j > first_irrev]
    if not late_ask:
        return list(range(n))
    late_set = set(late_ask)
    before = [i for i in range(first_irrev) if i not in late_set]
    after = [i for i in range(first_irrev, n) if i not in late_set]
    return before + late_ask + after


def _conversation_recent_irreversible_success(messages: list, *, lookback: int = 36) -> bool:
    """最近上下文中是否已有成功的不可逆删除类工具结果（用于拦截「删完后再问是否删」）。"""
    if not messages:
        return False
    slice_msgs = messages[-lookback:] if len(messages) > lookback else messages
    for m in reversed(slice_msgs):
        if (m or {}).get("role") != "tool":
            continue
        raw = (m or {}).get("content") or ""
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if not obj.get("success"):
            continue
        if obj.get("deleted") is True:
            return True
        if obj.get("detached") is True:
            return True
    return False


def _choice_args_smells_like_delete_confirm(fn_args: dict) -> bool:
    if not isinstance(fn_args, dict):
        return False
    blob = json.dumps(fn_args, ensure_ascii=False)
    lower = blob.lower()
    for n in ("confirm delete", "delete host", "remove host", "irreversible"):
        if n in lower:
            return True
    for n in ("删除", "移除", "不可恢复", "不可逆", "取消", "不删除"):
        if n in blob:
            return True
    return False


def _should_suppress_stale_delete_confirm_asks(messages: list, prepared_tool_calls: list) -> bool:
    """模型在删除已完成后的下一轮仍只发起「是否删除」类选择题时，抑制无效 UI。"""
    if not prepared_tool_calls:
        return False
    for tc, fn_args, _prev in prepared_tool_calls:
        fn = dict((tc or {}).get("function") or {})
        if (fn.get("name") or "").strip() != "ask_user_choice":
            return False
        if not _choice_args_smells_like_delete_confirm(fn_args):
            return False
    return _conversation_recent_irreversible_success(messages)


async def _prepare_tool_calls_for_execution(
    tool_calls: list,
    *,
    reorder_confirm_before_irreversible: bool = False,
) -> tuple[list, list]:
    """解析工具参数；返回写入会话的完整 tool_calls（供后续轮次检索），以及 prepared 列表供执行与 SSE 预览。"""
    full_tool_calls: list = []
    prepared: list[tuple[dict, dict, dict]] = []
    for tc in tool_calls or []:
        fn = dict((tc or {}).get("function") or {})
        raw_args = fn.get("arguments") or "{}"
        try:
            fn_args = await asyncio.to_thread(json.loads, raw_args)
        except json.JSONDecodeError:
            fn_args = {}
        fn_args_preview = _tool_args_preview(fn_args)
        full_fn = dict(fn)
        full_fn["arguments"] = json.dumps(fn_args, ensure_ascii=False)
        full_tc = dict(tc or {})
        full_tc["function"] = full_fn
        full_tool_calls.append(full_tc)
        prepared.append((tc, fn_args, fn_args_preview))
    if reorder_confirm_before_irreversible and len(full_tool_calls) > 1:
        perm = _tool_call_reorder_indices_confirm_before_irreversible(full_tool_calls)
        if perm != list(range(len(perm))):
            full_tool_calls = [full_tool_calls[i] for i in perm]
            prepared = [prepared[i] for i in perm]
    return full_tool_calls, prepared


def _extract_session_id_from_log_params(params_text: str) -> int | None:
    try:
        data = json.loads(params_text or "{}")
    except Exception:
        return None
    if isinstance(data, dict):
        sid = data.get("session_id")
        try:
            return int(sid) if sid is not None else None
        except Exception:
            return None
    return None


def _clip_tool_result_cache_content(text: str) -> tuple[str, bool]:
    raw = text or ""
    if len(raw) <= TOOL_RESULT_CACHE_MAX_CHARS:
        return raw, False
    head = TOOL_RESULT_CACHE_MAX_CHARS // 2
    tail = TOOL_RESULT_CACHE_MAX_CHARS - head - 48
    clipped = raw[:head] + f"\n…（缓存内容过长，已截断，总长 {len(raw)} 字符）…\n" + raw[-tail:]
    return clipped, True


async def _store_tool_result_cache(
    db,
    *,
    user_id: int,
    session_id: int,
    tool_name: str,
    tool_args: dict,
    tool_result: str,
    is_success: bool,
    source: str,
    tool_call_id: str | None = None,
) -> int | None:
    # read_chat_attachment 返回的 data_url 一旦被 1MB 上限切一刀，前端历史回放的图就打不开；
    # 为保证“AI 加载图片不截断”的一致体验，这里对该工具结果整段原样缓存。
    if (tool_name or "").strip() == "read_chat_attachment":
        cached_content = tool_result or ""
        truncated = False
    else:
        cached_content, truncated = _clip_tool_result_cache_content(tool_result or "")
    params = {
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_args": tool_args or {},
        "tool_call_id": tool_call_id or "",
        "cache_truncated": truncated,
        "cache_raw_chars": len(tool_result or ""),
    }
    await db.execute(
        """INSERT INTO operation_logs (user_id, operation, params, result, details, source)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            f"ai_tool:{tool_name}",
            json.dumps(params, ensure_ascii=False),
            "success" if is_success else "failed",
            cached_content,
            source,
        ),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _compact_terminal_context(text: str, max_lines: int = 400, max_line_len: int = 280) -> str:
    """压缩终端上下文：去 ANSI、规范换行、限制行数与单行长度。"""
    if not text:
        return text
    clean = ANSI_ESCAPE_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = clean.split("\n")
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    out_lines = []
    blank_count = 0
    for ln in lines:
        ln = ln.rstrip()
        if not ln:
            blank_count += 1
            if blank_count > 1:
                continue
            out_lines.append("")
            continue
        blank_count = 0
        if len(ln) > max_line_len:
            head = max_line_len // 2 - 8
            tail = max_line_len - head - 1
            ln = ln[:head] + "…" + ln[-tail:]
        out_lines.append(ln)
    return "\n".join(out_lines).strip()


def _cap_items_by_json_size(items: list[dict], max_chars: int, min_items: int = 1) -> list[dict]:
    """按 JSON 估算长度裁剪列表项，尽量保持结构完整。"""
    if not items:
        return []
    if max_chars <= 0:
        return items[:max(0, min_items)]
    out: list[dict] = []
    used = 2  # []
    for it in items:
        seg = len(json.dumps(it, ensure_ascii=False)) + (1 if out else 0)
        if out and (used + seg > max_chars):
            break
        out.append(it)
        used += seg
    if len(out) < min_items:
        return items[:min(min_items, len(items))]
    return out


def _infer_model_window_tokens(provider: str, model: str) -> int:
    """粗略估算模型上下文窗口（token），用于 auto budget。"""
    p = (provider or "").lower()
    m = (model or "").lower()
    s = f"{p}/{m}"
    if any(k in s for k in ("gpt-4.1", "o3", "o4", "claude-3.7", "claude-4", "qwen-max")):
        return 128_000
    if any(k in s for k in ("gpt-4o", "qwen-plus", "deepseek-chat", "deepseek-reasoner", "llama-3.1", "llama3.1")):
        return 64_000
    if any(k in s for k in ("gpt-4", "qwen-turbo", "llama-3", "llama3", "mistral")):
        return 32_000
    if any(k in s for k in ("gpt-3.5", "qwen2", "qwen-7b", "glm", "yi-")):
        return 16_000
    # 未识别模型时按 256k 处理，避免因低估窗口导致上下文过度裁剪。
    return 256_000


def _resolve_request_max_tokens(settings: dict | None = None) -> int:
    """请求输出上限：用户/系统未明确时默认 16k。"""
    settings = settings or {}
    val = settings.get("ai_max_output_tokens")
    try:
        n = int(val) if val is not None and str(val).strip() != "" else 0
    except Exception:
        n = 0
    if n <= 0:
        try:
            n = int(getattr(config, "AI_DEFAULT_MAX_OUTPUT_TOKENS", 16384) or 16384)
        except Exception:
            n = 16384
    return max(256, min(65536, n))


def _resolve_context_budget_chars(context_size: int, settings: dict) -> int:
    """context_size=0 时根据 provider/model 自动估算预算（字符）。"""
    if context_size > 0:
        return context_size
    provider = (settings.get("ai_provider") or "").strip()
    model = (settings.get("ai_model") or "").strip()
    window_tokens = _infer_model_window_tokens(provider, model)
    if window_tokens >= 256_000:
        budget = 262_144
    elif window_tokens >= 128_000:
        budget = 131_072
    elif window_tokens >= 64_000:
        budget = 65_536
    elif window_tokens >= 32_000:
        budget = 36_000
    elif window_tokens >= 16_000:
        budget = 24_000
    else:
        budget = CONTEXT_SIZE_DEFAULT
    return max(AUTO_CONTEXT_MIN, min(AUTO_CONTEXT_MAX, budget))


def _tool_result_message_limit(context_size: int) -> int:
    """根据总预算动态限制单条 tool 写回长度，减少多轮堆积。"""
    if context_size <= 0:
        return TOOL_RESULT_IN_MESSAGE_MAX
    return max(2000, min(TOOL_RESULT_IN_MESSAGE_MAX, context_size // 3))


def _compact_system_prompt_for_request(system_prompt: str, user_message: str) -> str:
    """按请求意图裁剪系统提示中的低相关长段，降低每轮固定开销。"""
    text = system_prompt or ""
    q = (user_message or "").lower()
    need_graph = any(k in q for k in ("流程图", "时序图", "网络图", "拓扑", "mermaid", "markmap", "echarts", "图表", "思维导图"))
    need_mail = any(k in q for k in ("邮件", "smtp", "发信", "邮箱"))
    need_time = any(k in q for k in ("时间", "时区", "timezone", "几点"))

    def strip_block(src: str, title: str) -> str:
        marker = f"\n{title}\n"
        start = src.find(marker)
        if start < 0:
            return src
        # 下一个二级标题（形如 "\nxxx：\n"）
        nxt = src.find("\n", start + len(marker))
        pos = nxt if nxt >= 0 else len(src)
        while True:
            cand = src.find("\n", pos + 1)
            if cand < 0:
                end = len(src)
                break
            line_end = src.find("\n", cand + 1)
            if line_end < 0:
                line_end = len(src)
            line = src[cand + 1:line_end]
            if line.endswith("：") and len(line) < 80:
                end = cand + 1
                break
            pos = cand
        return src[:start] + src[end:]

    if not need_graph:
        text = strip_block(text, "图形输出规范：当用户要求“流程图”“时序图”“网络关系图”“拓扑图”“依赖关系图”等图形化关系展示时，优先直接输出 ` ```mermaid ` 代码块；当用户要求“思维导图”“脑图”时，优先直接输出 ` ```markmap ` 代码块；当用户要求“图表”“柱状图”“折线图”“饼图”“趋势图”时，优先直接输出 ` ```echarts-option ` 代码块。不要把这些图形源码放进普通代码块，也不要只返回普通列表文本。")
    if not need_mail:
        text = strip_block(text, "个人发信（用户 SMTP，与管理员全局 SMTP 独立）：")
    if not need_time:
        text = strip_block(text, "系统时间与显示时区：")
    return text


def _normalize_ratio_map(r: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, float(v)) for v in r.values())
    if total <= 0:
        return {
            "hosts": CONTEXT_RATIO_HOSTS,
            "groups": CONTEXT_RATIO_GROUPS,
            "knowledge": CONTEXT_RATIO_KNOWLEDGE,
            "terminal": CONTEXT_RATIO_TERMINAL,
            "history": CONTEXT_RATIO_HISTORY,
        }
    return {k: max(0.0, float(v)) / total for k, v in r.items()}


def _infer_context_profile(
    *,
    user_message: str,
    session_scope: str,
    session_host_id: int | None,
    has_terminal: bool,
    context_size: int,
) -> dict:
    text = (user_message or "").lower()
    ratios = {
        "hosts": CONTEXT_RATIO_HOSTS,
        "groups": CONTEXT_RATIO_GROUPS,
        "knowledge": CONTEXT_RATIO_KNOWLEDGE,
        "terminal": CONTEXT_RATIO_TERMINAL,
        "history": CONTEXT_RATIO_HISTORY,
    }
    history_max = CONTEXT_HISTORY_MAX_MESSAGES
    recent_full = CONTEXT_HISTORY_RECENT_FULL

    if session_scope == "local":
        ratios.update({"hosts": 0.0, "groups": 0.0, "knowledge": 0.10, "terminal": 0.50, "history": 0.40})
        history_max = 16
        recent_full = 5
    elif session_host_id is not None:
        ratios.update({"hosts": 0.06, "groups": 0.0, "knowledge": 0.24, "terminal": 0.38, "history": 0.32})
        history_max = 18
        recent_full = 6

    if any(k in text for k in ("日志", "报错", "错误", "异常", "失败", "输出", "控制台", "terminal")):
        ratios["terminal"] += 0.12
        ratios["history"] += 0.08
        ratios["hosts"] -= 0.08
        ratios["groups"] -= 0.05
        history_max += 4
    if any(k in text for k in ("主机", "分组", "列表", "搜索", "查找", "筛选")):
        ratios["hosts"] += 0.10
        ratios["groups"] += 0.08
        ratios["terminal"] -= 0.08
    if any(k in text for k in ("脚本", ".edgeops", "rules", "info", "tasks", "复用")):
        ratios["knowledge"] += 0.10
        ratios["history"] += 0.04
        ratios["terminal"] -= 0.06
    if any(k in text for k in ("继续", "接着", "下一步", "按刚才", "同样", "沿用", "继续执行")):
        ratios["terminal"] += 0.14
        ratios["history"] += 0.10
        ratios["hosts"] -= 0.10
        ratios["groups"] -= 0.06
    if not has_terminal:
        ratios["history"] += 0.08
        ratios["terminal"] = min(0.10, ratios["terminal"])
    if context_size <= 20_000:
        history_max = min(history_max, 12)
        recent_full = min(recent_full, 4)
    elif context_size >= 48_000:
        history_max = min(30, history_max + 4)
        recent_full = min(10, recent_full + 2)

    ratios = _normalize_ratio_map(ratios)
    return {
        "ratios": ratios,
        "history_max_messages": max(6, min(40, history_max)),
        "history_recent_full": max(2, min(12, recent_full)),
    }


def _compact_tool_result_for_messages(
    tool_name: str,
    tool_result: str,
    max_chars: int,
    preserve_profile: str = "standard",
) -> str:
    """压缩写回 messages 的 tool 内容，减少多轮上下文膨胀。"""
    text = tool_result or ""
    # read_chat_attachment 是 AI 读取图片/文件内容的唯一通道，返回里的 data_url 一旦被按普通
    # 字符串字段截断就是一段残缺的 base64，视觉模型会直接解不出图。这里整体豁免压缩与截断，
    # 保证 AI 加载图片时"所见即所得"。
    if (tool_name or "").strip() == "read_chat_attachment":
        return text
    is_integration = (preserve_profile or "").strip().lower() == "integration"
    max_depth = 7 if is_integration else 5
    list_limit = 320 if is_integration else 180

    def _compact_json_value(v, key_name: str = "", depth: int = 0):
        if depth > max_depth:
            return "…（层级过深已截断）"
        if isinstance(v, dict):
            out = {}
            # 保结构：不过滤 key，只压 value
            for k, subv in v.items():
                out[k] = _compact_json_value(subv, str(k), depth + 1)
            return out
        if isinstance(v, list):
            # 保结构：列表裁长度时保留“头+尾”，降低只看前几项导致漏项风险
            if len(v) <= list_limit:
                return [_compact_json_value(x, key_name, depth + 1) for x in v]
            head_keep = max(24, int(list_limit * 0.7))
            tail_keep = max(12, list_limit - head_keep)
            head_part = [_compact_json_value(x, key_name, depth + 1) for x in v[:head_keep]]
            tail_part = [_compact_json_value(x, key_name, depth + 1) for x in v[-tail_keep:]]
            omitted = max(0, len(v) - head_keep - tail_keep)
            return head_part + [f"…（中间省略 {omitted} 项）…"] + tail_part
        if isinstance(v, str):
            lname = (key_name or "").lower()
            if lname in ("stdout", "stderr", "buffer"):
                cap = 3200 if is_integration else 1800
                return v if len(v) <= cap else abbreviate_text_tail_focus(v, cap)
            elif lname in ("content", "context", "result", "result_preview"):
                cap = 2400 if is_integration else 1400
                return v if len(v) <= cap else abbreviate_text_head_focus(v, cap)
            else:
                cap = 1000 if is_integration else 600
            return v if len(v) <= cap else (v[:cap] + "…（已截断）")
        return v

    try:
        obj = json.loads(text)
    except Exception:
        obj = None
    if isinstance(obj, (dict, list)):
        text = json.dumps(_compact_json_value(obj), ensure_ascii=False)
    if ("已省略" in text or "已截断" in text) and "【系统提示】上方工具结果为裁剪版" not in text:
        text += "\n【系统提示】上方工具结果为裁剪版：不得据此直接下“已覆盖全部项”的结论。若任务要求全量（如漏洞清单/资产清单），请继续分页或分批调用工具直到无省略/截断，再汇总。"
    if len(text) <= max_chars:
        return text
    # 终端/日志/命令输出：以末尾为主
    if tool_name in (
        "get_terminal_buffer",
        "ssh_execute",
        "ssh_channel_read_lines",
        "ssh_channel_read_length",
        "list_logs",
    ):
        return abbreviate_text_tail_focus(text, max_chars)
    # 读文件类：以开头为主
    if tool_name in ("fs_read_file", "local_fs_read_file", "read_chat_attachment"):
        return abbreviate_text_head_focus(text, max_chars)
    # 其他：默认保留开头（多数为元数据/短回复）
    return text[:max_chars] + "\n…（已截断）"


async def _tool_content_for_llm_with_spill(
    user: dict,
    session_id: int,
    tool_name: str,
    tool_call_id: str | None,
    tool_result: str,
    tool_result_limit: int,
    preserve_profile: str,
) -> str:
    """压缩后的 tool 写入 messages；大号结果额外落盘并由哨兵行引用。"""
    compact = _compact_tool_result_for_messages(
        tool_name, tool_result, tool_result_limit, preserve_profile
    )
    return await spill_and_wrap_tool_message(
        user, session_id, tool_name, tool_call_id, tool_result, compact
    )


def _compact_host_rows_for_context(
    host_rows: list,
    *,
    session_scope: str,
    session_host_id: int | None,
    max_chars_budget: int = 0,
) -> list[dict]:
    """上下文主机信息最小化：只保留检索与命令策略必要字段。"""
    if session_scope == "local":
        return []
    compact = []
    for r in host_rows:
        d = normalize_host_aliases_in_dict(dict(r))
        compact.append({
            "id": d.get("id"),
            "name": d.get("name"),
            "host": d.get("host"),
            "port": d.get("port"),
            "aliases": d.get("aliases") or [],
            "remark": d.get("remark") or "",
            "host_type": d.get("host_type") or "",
            "host_version": d.get("host_version") or "",
            "host_shell": d.get("host_shell") or "",
            "host_package_manager": d.get("host_package_manager") or "",
        })
    if session_host_id is not None:
        scoped = [h for h in compact if int(h.get("id") or 0) == int(session_host_id)]
        return scoped[:1]
    compact = compact[:CONTEXT_MAX_HOST_ITEMS]
    if max_chars_budget > 0:
        compact = _cap_items_by_json_size(compact, max_chars_budget, min_items=6)
    return compact


def _compact_group_rows_for_context(
    group_rows: list,
    *,
    session_scope: str,
    session_host_id: int | None,
    max_chars_budget: int = 0,
) -> list[dict]:
    """分组上下文最小化：主机维度会话与本机会话不注入分组。"""
    if session_scope == "local" or session_host_id is not None:
        return []
    out = []
    for r in group_rows[:CONTEXT_MAX_GROUP_ITEMS]:
        d = dict(r)
        out.append({
            "id": d.get("id"),
            "name": d.get("name"),
            "parent_id": d.get("parent_id"),
        })
    if max_chars_budget > 0:
        out = _cap_items_by_json_size(out, max_chars_budget, min_items=4)
    return out


def _normalize_history_timestamp(created_at: str | None) -> str:
    """规范化历史消息时间，统一为 YYYY-MM-DD HH:MM:SS。"""
    raw = (created_at or "").strip()
    if not raw:
        return "未知时间"
    ts = raw.replace("T", " ")
    if ts.endswith("Z"):
        ts = ts[:-1]
    if "." in ts:
        ts = ts.split(".", 1)[0]
    return ts[:19] if len(ts) >= 19 else ts


def _with_history_timestamp(content: str, created_at: str | None) -> str:
    """给历史消息内容附加可被 AI 识别的时间戳。"""
    ts = _normalize_history_timestamp(created_at)
    return f"[历史时间: {ts}]\n{content or ''}"


def _apply_context_limits(
    context_size: int,
    hosts_ctx: str,
    groups_ctx: str,
    host_knowledge_ctx: str,
    terminal_ctx: str,
    conversation: list,
    profile: dict | None = None,
    summarize_old_assistant: bool = True,
) -> tuple:
    """
    按配置的上下文总大小，动态调整各段资源的长度（分段访问），防止溢出。
    context_size 为 0 时使用 CONTEXT_SIZE_DEFAULT，避免请求体过大导致超时或 500。
    返回 (hosts_ctx, groups_ctx, host_knowledge_ctx, terminal_ctx, conversation).
    """
    ratios = _normalize_ratio_map((profile or {}).get("ratios") or {
        "hosts": CONTEXT_RATIO_HOSTS,
        "groups": CONTEXT_RATIO_GROUPS,
        "knowledge": CONTEXT_RATIO_KNOWLEDGE,
        "terminal": CONTEXT_RATIO_TERMINAL,
        "history": CONTEXT_RATIO_HISTORY,
    })
    max_hosts = max(200, int(context_size * ratios["hosts"]))
    max_groups = max(100, int(context_size * ratios["groups"]))
    max_knowledge = max(200, int(context_size * ratios["knowledge"]))
    max_terminal = max(500, int(context_size * ratios["terminal"]))
    history_budget = max(500, int(context_size * ratios["history"]))

    hosts_ctx = _truncate_segment(hosts_ctx, max_hosts)
    groups_ctx = _truncate_segment(groups_ctx, max_groups)
    host_knowledge_ctx = _truncate_segment(host_knowledge_ctx, max_knowledge)
    terminal_ctx = _truncate_segment(terminal_ctx, max_terminal, tail=True)

    # 历史消息：保留最近若干条；较旧助手消息优先压缩为“决策摘要”
    out_conv = []
    history_max_messages = int((profile or {}).get("history_max_messages") or CONTEXT_HISTORY_MAX_MESSAGES)
    history_recent_full = int((profile or {}).get("history_recent_full") or CONTEXT_HISTORY_RECENT_FULL)
    n_cap = max(6, min(history_max_messages, history_budget // 220))
    n = min(len(conversation), n_cap)
    if n == 0:
        return hosts_ctx, groups_ctx, host_knowledge_ctx, terminal_ctx, []
    recent_full_n = min(history_recent_full, n)
    for idx, m in enumerate(conversation[-n:]):
        role = m.get("role")
        raw_content = m.get("content") or ""
        # 非最近消息里，助手内容优先摘要，显著减少上下文冗余
        if summarize_old_assistant and idx < (n - recent_full_n) and role == "assistant":
            raw_content = _assistant_content_for_summary(raw_content) or raw_content
            per_msg = max(80, history_budget // (n * 2))
        else:
            per_msg = max(120, history_budget // n)
        _shrunk = shrink_tool_message_for_history_budget(raw_content, per_msg, role=role or "")
        if _shrunk is not None:
            raw_content = _shrunk
        content = raw_content[:per_msg]
        if len(raw_content) > per_msg:
            content = content + "…（已截断）"
        out_conv.append({"role": role, "content": content})
    return hosts_ctx, groups_ctx, host_knowledge_ctx, terminal_ctx, out_conv


async def _fetch_setting_value(db, key: str) -> str:
    rows = await db.execute_fetchall("SELECT value FROM settings WHERE key = ?", (key,))
    return ((rows[0]["value"] if rows else "") or "").strip()


async def _get_user_ai_settings(db, user_id: int) -> dict:
    """获取指定用户的 AI 配置：优先 user_ai_config，缺项用全局 settings 补全。返回键为 ai_* 的字典。"""
    keys = [
        "ai_api_key", "ai_base_url", "ai_model", "ai_system_prompt",
        "ai_auto_approve", "ai_assistant_enabled", "ai_context_size",
        "ai_agent_max_steps", "ai_assistant_max_rounds", "ai_provider",
        "ai_vision_enabled",
    ]
    out = {}
    row = await db.execute_fetchall("SELECT * FROM user_ai_config WHERE user_id = ?", (user_id,))
    if row:
        r = dict(row[0])
        out["ai_api_key"] = (r.get("api_key") or "").strip()
        out["ai_base_url"] = (r.get("base_url") or "").strip()
        out["ai_model"] = (r.get("model") or "").strip()
        out["ai_system_prompt"] = (r.get("system_prompt") or "").strip()
        out["ai_auto_approve"] = (r.get("auto_approve") or "false").strip().lower()
        out["ai_assistant_enabled"] = (r.get("assistant_enabled") or "false").strip().lower()
        out["ai_context_size"] = (r.get("context_size") or "0").strip()
        out["ai_agent_max_steps"] = (r.get("agent_max_steps") or "").strip()
        out["ai_assistant_max_rounds"] = (r.get("assistant_max_rounds") or "").strip()
        out["ai_provider"] = (r.get("provider") or "").strip()
        # 视觉开关：旧库未迁移 / 老行未设置时兜底为 'true'（默认启用），与 022 迁移保持一致
        _vision_raw = (r.get("vision_enabled") if "vision_enabled" in r else None)
        out["ai_vision_enabled"] = ((_vision_raw or "true").strip().lower() or "true")
        out["ai_output_locale"] = (r.get("ai_output_locale") or "").strip()
    for k in keys:
        if k not in out or out[k] == "":
            if k == "ai_provider":
                out[k] = ""  # 无默认，空表示自动探测
                continue
            if k == "ai_vision_enabled":
                out[k] = "true"  # 默认启用视觉
                continue
            rows = await db.execute_fetchall("SELECT value FROM settings WHERE key = ?", (k,))
            val = (rows[0]["value"] if rows else "") or ("true" if k == "ai_auto_approve" else ("0" if k == "ai_context_size" else ""))
            # 用户默认系统配置与管理员相同，但 KEY 为空，需用户自行配置
            if k == "ai_api_key":
                val = ""
            out[k] = val
    return out


class AIConfigRequest(BaseModel):
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    system_prompt: str = ""
    auto_approve: bool = False
    assistant_enabled: bool = False
    context_size: int = 0  # 聊天上下文总字符数上限，0 表示不限制
    provider: str = ""  # AI 源类型：aliyun/ollama/openai，空表示按 base_url 自动探测
    # AI 多轮执行控制：0 表示沿用全局默认（config.AGENT_MAX_STEPS / ASSISTANT_MAX_ROUNDS）
    # 上限均为 AGENT_MAX_STEPS_CAP / ASSISTANT_MAX_ROUNDS_CAP（默认 1000）；超出会被截断。
    agent_max_steps: int = 0
    assistant_max_rounds: int = 0
    # 模型是否支持图像识别（多模态视觉输入）。默认 True：后端会把用户本轮上传的图片
    # 作为 OpenAI `image_url` 段内联到 user 消息的 content 数组；若所用模型/网关不支持
    # 多模态，请关闭——后端改为只挂 📎 附件清单，让 AI 按需调 read_chat_attachment 拿 data_url。
    vision_enabled: bool = True
    # 无法从单条用户消息判断语言时，作为默认回复语言（en / zh-CN）；空=走站点与界面语言链路
    output_locale: str = ""
    user_id: int | None = None  # 仅管理员：指定要更新的用户 ID，不传则更新当前用户


class ChatRequest(BaseModel):
    message: str
    session_id: int | None = None
    host_id: int | None = None  # 新建会话时若传入，则创建该主机的 AI 运维会话
    scope: str = "default"  # default=全局 AI 助手；local=本机管理 AI 会话
    terminal_scope_id: str | None = None
    preferred_terminal_slot: int | None = None
    # 全局 AI 助手页：界面当前关注的主机（如远程文件树所选、活动 SSH 标签页），
    # 仅用于注入该机的主机级提示词，不改变会话的 host_id、不用于新建会话。
    context_host_id: int | None = None
    # 用户在聊天输入框已上传的附件 UUID 列表（图片/文本/Markdown 等）。
    # 这些附件将被：1) 绑定到该会话；2) 以 Markdown 清单追加到用户消息末尾，便于 AI 引用/读取；
    # AI 可调用 read_chat_attachment(uuid) 获取文本内容或图片 data URL。
    attachment_uuids: list[str] = []
    # 界面语言 / 浏览器语言（BCP-47 或 I18n 当前有效 locale），供回复语言策略在「用户/站点都未设默认」时回退
    ui_locale: str | None = None


class SessionRuntimeControlRequest(BaseModel):
    action: str = "supplement"  # supplement | pause | resume | stop
    message: str = ""


class UpdateSessionRequest(BaseModel):
    title: str | None = None
    low_interaction_mode: bool | None = None


class UpdateSessionMessageRequest(BaseModel):
    content: str = ""


class SessionPromptRequest(BaseModel):
    prompt: str = ""


class HostPromptRequest(BaseModel):
    prompt: str = ""


class ClearMessagesRequest(BaseModel):
    clear: str = "all"   # "all" | "after"
    keep_n: int = 0      # 当 clear == "after" 时：保留前 keep_n 条，删除其后的全部


async def _get_system_ai_settings_from_db(db) -> dict:
    """从 settings 表读取系统 AI 配置（用于「将系统配置应用到用户」）。"""
    keys = [
        "ai_api_key", "ai_base_url", "ai_model", "ai_system_prompt",
        "ai_auto_approve", "ai_assistant_enabled", "ai_context_size",
        "ai_agent_max_steps", "ai_assistant_max_rounds", "ai_output_locale",
    ]
    out = {}
    for k in keys:
        rows = await db.execute_fetchall("SELECT value FROM settings WHERE key = ?", (k,))
        out[k] = (rows[0]["value"] if rows else "") or ""
    return out


async def _get_system_key_and_base(db) -> tuple[str, str]:
    """返回 (system_api_key, system_base_url)。优先从 settings 表读取（管理员在系统设置中配置），否则从 config/环境变量读取。用于新用户未配置 KEY 时自动使用系统配置。"""
    raw = await _get_system_ai_settings_from_db(db)
    key = (raw.get("ai_api_key") or "").strip() or (getattr(_config, "AI_API_KEY", "") or "").strip()
    base = (raw.get("ai_base_url") or "").strip().rstrip("/") or (getattr(_config, "AI_BASE_URL", "") or "").strip().rstrip("/")
    return key, base


def _allow_system_shared_api_key(
    system_key: str,
    system_base: str,
    *,
    resolved_base_url: str,
) -> bool:
    """未配置自有 Key 时是否可走系统共享 Key：必须有系统 Key；API 地址可由当前已解析的 base_url（用户/全局/环境）或系统侧的 base 任一提供。"""
    if not (system_key or "").strip():
        return False
    rb = (resolved_base_url or "").strip().rstrip("/")
    sb = (system_base or "").strip().rstrip("/")
    return bool(rb or sb)


async def _peek_system_ai_usage(db, user_id: int) -> int:
    """仅查询当前用户已消耗的系统 KEY 次数，不做任何修改。"""
    rows = await db.execute_fetchall(
        "SELECT call_count FROM user_system_ai_usage WHERE user_id = ?", (user_id,)
    )
    return (rows[0]["call_count"] if rows else 0) or 0


async def _consume_system_ai_usage(db, user_id: int) -> dict:
    """检查并消费一次系统共享 Key 的配额计数。

    返回一个 dict：
    - {"exhausted": True,  "used": N, "limit": L, "remaining": 0}  已用尽，未扣减
    - {"exhausted": False, "used": N+1, "limit": L, "remaining": L - N - 1}  扣减成功

    仅当用户使用系统 KEY 时调用；配置了自己 KEY 的用户直接跳过此函数。
    """
    call_count = await _peek_system_ai_usage(db, user_id)
    if call_count >= SYSTEM_AI_USAGE_LIMIT:
        return {
            "exhausted": True,
            "used": call_count,
            "limit": SYSTEM_AI_USAGE_LIMIT,
            "remaining": 0,
        }
    await db.execute(
        """INSERT INTO user_system_ai_usage (user_id, call_count, updated_at) VALUES (?, 1, CURRENT_TIMESTAMP)
           ON CONFLICT(user_id) DO UPDATE SET call_count = call_count + 1, updated_at = CURRENT_TIMESTAMP""",
        (user_id,),
    )
    await db.commit()
    return {
        "exhausted": False,
        "used": call_count + 1,
        "limit": SYSTEM_AI_USAGE_LIMIT,
        "remaining": max(0, SYSTEM_AI_USAGE_LIMIT - (call_count + 1)),
    }


def _format_trial_banner(trial_info: dict, output_locale: str = "zh-CN") -> str:
    """生成一行 Markdown 配额提示横幅，拼接在 AI 本轮首条回复最前面，让用户实时看到剩余额度。"""
    used = trial_info.get("used", 0)
    limit = trial_info.get("limit", SYSTEM_AI_USAGE_LIMIT)
    remaining = trial_info.get("remaining", max(0, limit - used))
    if output_locale == "en":
        return (
            f"> 💡 **Shared system KEY**: **{remaining}/{limit}** calls left. "
            f"Configure your own model to remove the limit, or ask an admin to reset quota.\n\n"
        )
    return (
        f"> 💡 **系统共享 KEY**，剩余 **{remaining}/{limit}** 次。"
        f"配置自己的模型解除限制，或联系管理员重置配额。\n\n"
    )


def _format_trial_exhausted_message(
    trial_info: dict | None = None, output_locale: str = "zh-CN"
) -> str:
    """生成用尽后 AI 自动回复的固定文案。"""
    limit = (trial_info or {}).get("limit", SYSTEM_AI_USAGE_LIMIT)
    if output_locale == "en":
        return (
            f"❌ **Shared system KEY quota exhausted** (0/{limit} calls left).\n\n"
            f"Configure your own Base URL / API Key / Model in **Settings → My AI config** "
            f"to remove the limit, or ask an admin to reset the shared quota."
        )
    return (
        f"❌ **系统共享 KEY 配额已用尽**（剩余 0/{limit} 次）。\n\n"
        f"请在「系统设置 → 我的 AI 配置」填写自有 Base URL / API Key / Model 解除限制，"
        f"或联系管理员重置共享配额。"
    )


def _format_empty_assistant_summary(output_locale: str = "zh-CN") -> str:
    if output_locale == "en":
        return "(Tool calls finished, but the model did not return a text summary.)"
    return "（已按上述工具执行完成，但模型未返回文字总结。）"


@router.get("/config")
async def get_ai_config(user_id: int | None = None, user=Depends(get_current_user)):
    """获取 AI 配置。当前用户始终可获取自己的配置；管理员可传 user_id 获取指定用户的配置。"""
    import config
    db = await get_db()
    target_id = user["id"]
    if user_id is not None and _is_admin_role(user.get("role")):
        target_id = user_id
    settings = await _get_user_ai_settings(db, target_id)
    def _safe_int(s: str | None) -> int:
        try:
            return int((s or "0").strip() or "0")
        except (TypeError, ValueError):
            return 0
    result = {
        "api_key": settings.get("ai_api_key") or "",
        "base_url": settings.get("ai_base_url") or "",
        "model": settings.get("ai_model") or "",
        "system_prompt": settings.get("ai_system_prompt") or "",
        "auto_approve": (settings.get("ai_auto_approve") or "false").lower() == "true",
        "assistant_enabled": (settings.get("ai_assistant_enabled") or "false").lower() == "true",
        "context_size": int(settings.get("ai_context_size") or "0"),
        "provider": (settings.get("ai_provider") or "").strip(),
        # 0 表示沿用全局默认；前端展示时同样用 0/空 表达"未设"
        "agent_max_steps": _safe_int(settings.get("ai_agent_max_steps")),
        "assistant_max_rounds": _safe_int(settings.get("ai_assistant_max_rounds")),
        # 视觉识图开关（默认 True）：关闭后不再把图片以多模态 image_url 段内联到 user 消息
        "vision_enabled": (settings.get("ai_vision_enabled") or "true").lower() != "false",
        # 空=不覆盖站点/界面语言链路；en/zh-CN=无法从用户输入判断时的个人默认
        "output_locale": (settings.get("ai_output_locale") or "").strip(),
    }
    model_types = getattr(config, "MODEL_TYPES", [])
    context_size_options = getattr(config, "CONTEXT_SIZE_OPTIONS", [0, 4000, 8000, 16000, 32000])
    context_size_max = getattr(config, "CONTEXT_SIZE_MAX", 8 * 1024 * 1024)
    return {
        "success": True,
        "config": result,
        "model_types": model_types,
        "context_size_options": context_size_options,
        "context_size_max": context_size_max,
        # 暴露默认与硬上限给前端展示与校验
        "agent_max_steps_default": getattr(config, "AGENT_MAX_STEPS", 100),
        "assistant_max_rounds_default": getattr(config, "ASSISTANT_MAX_ROUNDS", 100),
        "agent_max_steps_cap": getattr(config, "AGENT_MAX_STEPS_CAP", 1000),
        "assistant_max_rounds_cap": getattr(config, "ASSISTANT_MAX_ROUNDS_CAP", 1000),
    }


@router.get("/config/system")
async def get_system_ai_config(user=Depends(require_admin)):
    """管理员：获取系统默认 AI 配置（来自全局设置），用于「将系统配置应用到用户」等。"""
    db = await get_db()
    raw = await _get_system_ai_settings_from_db(db)
    result = {
        "api_key": raw.get("ai_api_key") or "",
        "base_url": raw.get("ai_base_url") or "",
        "model": raw.get("ai_model") or "",
        "system_prompt": raw.get("ai_system_prompt") or "",
        "auto_approve": (raw.get("ai_auto_approve") or "false").lower() == "true",
        "assistant_enabled": (raw.get("ai_assistant_enabled") or "false").lower() == "true",
        "context_size": int(raw.get("ai_context_size") or "0"),
        "provider": "",
        "output_locale": (raw.get("ai_output_locale") or "").strip(),
    }
    return {"success": True, "config": result}


@router.post("/config/apply-system")
async def apply_system_ai_config_to_user(user_id: int, user=Depends(require_admin)):
    """管理员：将系统默认 AI 配置（全局设置）直接写入指定用户的 user_ai_config。"""
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id FROM users WHERE id = ?", (user_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="用户不存在")
    raw = await _get_system_ai_settings_from_db(db)
    await db.execute(
        """INSERT INTO user_ai_config (user_id, api_key, base_url, model, system_prompt, auto_approve, assistant_enabled, context_size, agent_max_steps, assistant_max_rounds, provider, vision_enabled, ai_output_locale, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(user_id) DO UPDATE SET
           api_key=excluded.api_key, base_url=excluded.base_url, model=excluded.model,
           system_prompt=excluded.system_prompt, auto_approve=excluded.auto_approve,
           assistant_enabled=excluded.assistant_enabled, context_size=excluded.context_size,
           agent_max_steps=excluded.agent_max_steps, assistant_max_rounds=excluded.assistant_max_rounds,
           provider=excluded.provider, vision_enabled=excluded.vision_enabled, ai_output_locale=excluded.ai_output_locale, updated_at=CURRENT_TIMESTAMP""",
        (
            user_id,
            (raw.get("ai_api_key") or "").strip(),
            (raw.get("ai_base_url") or "").strip().rstrip("/"),
            (raw.get("ai_model") or "").strip(),
            (raw.get("ai_system_prompt") or "").strip(),
            (raw.get("ai_auto_approve") or "false").strip().lower(),
            (raw.get("ai_assistant_enabled") or "false").strip().lower(),
            (raw.get("ai_context_size") or "0").strip(),
            (raw.get("ai_agent_max_steps") or "").strip(),
            (raw.get("ai_assistant_max_rounds") or "").strip(),
            "",
            "true",
            (raw.get("ai_output_locale") or "").strip(),
        ),
    )
    await db.commit()
    return {"success": True, "message": "已将该用户的 AI 配置设为系统默认"}


@router.get("/config/trial")
async def get_trial_status(user_id: int | None = None, user=Depends(get_current_user)):
    """查询系统共享 Key 配额状态：已用次数、上限、剩余、是否已配置自有 KEY。

    - 普通用户只能查自己；
    - 管理员可传 user_id 查指定用户。
    """
    db = await get_db()
    target_id = user["id"]
    if user_id is not None and _is_admin_role(user.get("role")):
        target_id = int(user_id)
        rows = await db.execute_fetchall("SELECT id FROM users WHERE id = ?", (target_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="用户不存在")
    # 是否已配置自己的 KEY（有则不受共享 Key 次数限制）
    rows = await db.execute_fetchall(
        "SELECT api_key, base_url FROM user_ai_config WHERE user_id = ?", (target_id,)
    )
    own_key = bool(rows and (rows[0]["api_key"] or "").strip())
    own_base = bool(rows and (rows[0]["base_url"] or "").strip())
    used = await _peek_system_ai_usage(db, target_id)
    limit = SYSTEM_AI_USAGE_LIMIT
    remaining = max(0, limit - used)
    # 系统是否配置了默认 AI（决定未填自有 Key 时是否真能走共享 Key）
    system_key, system_base = await _get_system_key_and_base(db)
    merged = await _get_user_ai_settings(db, target_id)
    resolved_base = (merged.get("ai_base_url") or "").strip().rstrip("/") or (
        getattr(_config, "AI_BASE_URL", "") or ""
    ).strip().rstrip("/")
    return {
        "success": True,
        "user_id": target_id,
        "has_own_key": own_key,
        "has_own_base": own_base,
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "exhausted": (not own_key) and used >= limit,
        "system_key_available": _allow_system_shared_api_key(
            system_key, system_base, resolved_base_url=resolved_base
        ),
    }


@router.post("/config/trial/reset")
async def reset_trial_usage(user_id: int, user=Depends(require_admin)):
    """管理员：清零指定用户的系统共享 Key 调用计数（删除 user_system_ai_usage 中该用户行）。"""
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, username FROM users WHERE id = ?", (int(user_id),))
    if not rows:
        raise HTTPException(status_code=404, detail="用户不存在")
    username = rows[0]["username"] if rows else ""
    await db.execute("DELETE FROM user_system_ai_usage WHERE user_id = ?", (int(user_id),))
    await db.commit()
    return {
        "success": True,
        "message": f"已重置用户 {username}（ID={user_id}）的系统共享 Key 计数，该用户可继续使用共享 Key 调用至多 {SYSTEM_AI_USAGE_LIMIT} 次。",
    }


@router.post("/config/trial/unlock")
async def unlock_trial_mode(user_id: int, user=Depends(require_admin)):
    """管理员：将系统默认 AI 配置写入用户并解除共享 Key 次数限制。

    做法：把当前系统默认 AI 配置（`settings.ai_*` 或环境变量的 `AI_API_KEY`/`AI_BASE_URL`）
    复制写入该用户自己的 `user_ai_config`，使该用户之后再发 AI 请求时走自有配置项中的
    KEY（与管理员配的系统 KEY 可为同一套值，但计入用户 own_key），**不再受共享 Key 计数限制**。
    同时清零 user_system_ai_usage 计数。
    """
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id, username FROM users WHERE id = ?", (int(user_id),))
    if not rows:
        raise HTTPException(status_code=404, detail="用户不存在")
    username = rows[0]["username"] if rows else ""
    raw = await _get_system_ai_settings_from_db(db)
    sys_key = (raw.get("ai_api_key") or "").strip() or (getattr(_config, "AI_API_KEY", "") or "").strip()
    sys_base = (raw.get("ai_base_url") or "").strip().rstrip("/") or (getattr(_config, "AI_BASE_URL", "") or "").strip().rstrip("/")
    if not sys_key or not sys_base:
        raise HTTPException(
            status_code=400,
            detail="系统默认 AI 配置未填写（settings.ai_api_key / ai_base_url 或环境变量 AI_API_KEY / AI_BASE_URL 皆为空），无法写入用户配置。请先在「全局设置 → 系统 AI 配置」中填写可用的 Key/URL。",
        )
    await db.execute(
        """INSERT INTO user_ai_config (user_id, api_key, base_url, model, system_prompt, auto_approve, assistant_enabled, context_size, agent_max_steps, assistant_max_rounds, provider, vision_enabled, ai_output_locale, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(user_id) DO UPDATE SET
           api_key=excluded.api_key, base_url=excluded.base_url, model=excluded.model,
           system_prompt=excluded.system_prompt, auto_approve=excluded.auto_approve,
           assistant_enabled=excluded.assistant_enabled, context_size=excluded.context_size,
           agent_max_steps=excluded.agent_max_steps, assistant_max_rounds=excluded.assistant_max_rounds,
           provider=excluded.provider, vision_enabled=excluded.vision_enabled, ai_output_locale=excluded.ai_output_locale, updated_at=CURRENT_TIMESTAMP""",
        (
            int(user_id),
            sys_key,
            sys_base,
            (raw.get("ai_model") or "").strip(),
            (raw.get("ai_system_prompt") or "").strip(),
            (raw.get("ai_auto_approve") or "false").strip().lower(),
            (raw.get("ai_assistant_enabled") or "false").strip().lower(),
            (raw.get("ai_context_size") or "0").strip(),
            (raw.get("ai_agent_max_steps") or "").strip(),
            (raw.get("ai_assistant_max_rounds") or "").strip(),
            "",
            "true",
            (raw.get("ai_output_locale") or "").strip(),
        ),
    )
    # 同步清零共享 Key 计数，避免残留
    await db.execute("DELETE FROM user_system_ai_usage WHERE user_id = ?", (int(user_id),))
    await db.commit()
    return {
        "success": True,
        "message": f"已为用户 {username}（ID={user_id}）写入系统默认 AI 配置并清零共享 Key 计数，该用户后续 AI 调用不再受次数限制。",
    }


@router.post("/config")
async def update_ai_config(req: AIConfigRequest, user=Depends(get_current_user)):
    """更新 AI 配置。当前用户可更新自己的；管理员可在请求体中传 user_id 更新指定用户的配置。"""
    db = await get_db()
    target_id = user["id"]
    if getattr(req, "user_id", None) is not None and _is_admin_role(user.get("role")):
        target_id = req.user_id
        rows = await db.execute_fetchall("SELECT id FROM users WHERE id = ?", (target_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="用户不存在")
    provider = (getattr(req, "provider", None) or "").strip()
    if provider not in ("aliyun", "ollama", "openai"):
        provider = ""
    ctx = max(0, getattr(req, "context_size", 0))
    import config as _cfg
    ctx_max = getattr(_cfg, "CONTEXT_SIZE_MAX", 8 * 1024 * 1024)
    if ctx > ctx_max:
        ctx = ctx_max
    # agent_max_steps / assistant_max_rounds：0 表示"未设、用全局默认"，>0 时按硬上限截断
    agent_cap = getattr(_cfg, "AGENT_MAX_STEPS_CAP", 1000)
    rounds_cap = getattr(_cfg, "ASSISTANT_MAX_ROUNDS_CAP", 1000)
    raw_steps = max(0, int(getattr(req, "agent_max_steps", 0) or 0))
    raw_rounds = max(0, int(getattr(req, "assistant_max_rounds", 0) or 0))
    if raw_steps > agent_cap:
        raw_steps = agent_cap
    if raw_rounds > rounds_cap:
        raw_rounds = rounds_cap
    vision_val = "true" if getattr(req, "vision_enabled", True) else "false"
    out_loc = (getattr(req, "output_locale", None) or "").strip()
    if out_loc not in ("", "en", "zh-CN"):
        out_loc = ""
    await db.execute(
        """INSERT INTO user_ai_config (user_id, api_key, base_url, model, system_prompt, auto_approve, assistant_enabled, context_size, agent_max_steps, assistant_max_rounds, provider, vision_enabled, ai_output_locale, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(user_id) DO UPDATE SET
           api_key=excluded.api_key, base_url=excluded.base_url, model=excluded.model,
           system_prompt=excluded.system_prompt, auto_approve=excluded.auto_approve,
           assistant_enabled=excluded.assistant_enabled, context_size=excluded.context_size,
           agent_max_steps=excluded.agent_max_steps, assistant_max_rounds=excluded.assistant_max_rounds,
           provider=excluded.provider, vision_enabled=excluded.vision_enabled, ai_output_locale=excluded.ai_output_locale, updated_at=CURRENT_TIMESTAMP""",
        (
            target_id,
            (req.api_key or "").strip(),
            (req.base_url or "").strip().rstrip("/"),
            (req.model or "").strip(),
            (req.system_prompt or "").strip(),
            "true" if req.auto_approve else "false",
            "true" if getattr(req, "assistant_enabled", False) else "false",
            str(ctx),
            str(raw_steps) if raw_steps > 0 else "",
            str(raw_rounds) if raw_rounds > 0 else "",
            provider,
            vision_val,
            out_loc,
        ),
    )
    await db.commit()
    return {"success": True}


@router.get("/sessions")
async def list_sessions(
    user=Depends(get_current_user),
    host_id: int | None = None,
    scope: str | None = None,
):
    """host_id 不为空时返回该主机的 AI 运维会话；否则按 scope：default=全局 AI 助手，local=本机管理 AI 会话。本机管理会话列表仅管理员可请求。"""
    try:
        db = await get_db()
        scope_val = (scope or "default").strip().lower() or "default"
        if scope_val == "local" and not _is_admin_role(user.get("role")):
            raise HTTPException(status_code=403, detail="本机管理仅管理员可用")
        if host_id is not None:
            rows = await db.execute_fetchall(
                    """SELECT id, title, created_at, updated_at, host_id,
                              COALESCE(session_scope, 'default') AS session_scope,
                              COALESCE(low_interaction_mode, 'false') AS low_interaction_mode,
                              COALESCE(session_prompt, '') AS session_prompt
                   FROM ai_chat_sessions
                   WHERE user_id = ? AND host_id = ? AND COALESCE(session_scope, 'default') NOT IN ('integration', 'mcp_orchestrate', 'mcp_runtime')
                   ORDER BY updated_at DESC""",
                (user["id"], host_id),
            )
        else:
            if scope_val == "local":
                rows = await db.execute_fetchall(
                    "SELECT id, title, created_at, updated_at, host_id, COALESCE(session_scope, 'default') AS session_scope, COALESCE(low_interaction_mode, 'false') AS low_interaction_mode, COALESCE(session_prompt, '') AS session_prompt FROM ai_chat_sessions WHERE user_id = ? AND (host_id IS NULL OR host_id = 0) AND (session_scope = 'local') ORDER BY updated_at DESC",
                    (user["id"],),
                )
            else:
                rows = await db.execute_fetchall(
                    "SELECT id, title, created_at, updated_at, host_id, COALESCE(session_scope, 'default') AS session_scope, COALESCE(low_interaction_mode, 'false') AS low_interaction_mode, COALESCE(session_prompt, '') AS session_prompt FROM ai_chat_sessions WHERE user_id = ? AND (host_id IS NULL OR host_id = 0) AND (COALESCE(session_scope, 'default') = 'default') ORDER BY updated_at DESC",
                    (user["id"],),
                )
        return {"success": True, "sessions": [dict(r) for r in rows]}
    except Exception as e:
        logger.warning("list_sessions failed: %s", e)
        return {"success": True, "sessions": []}


@router.post("/sessions")
async def create_session(
    user=Depends(get_current_user),
    title: str = "default",
    host_id: int | None = None,
    scope: str = "default",
):
    """host_id 为空时创建全局或本机管理会话；scope=local 为本机管理 AI 会话，仅管理员可创建。"""
    db = await get_db()
    raw = (title or "").strip()
    if not raw or raw in EDGEOPS_SESSION_TITLE_CLIENT_PLACEHOLDERS:
        raw = EDGEOPS_TEMP_SESSION_PREFIX + datetime.now().strftime("%Y%m%d%H%M%S")
    scope_val = (scope or "default").strip().lower() or "default"
    if scope_val not in ("default", "local"):
        scope_val = "default"
    if scope_val == "local" and not _is_admin_role(user.get("role")):
        raise HTTPException(status_code=403, detail="本机管理仅管理员可用")
    await db.execute(
        "INSERT INTO ai_chat_sessions (user_id, host_id, title, session_scope) VALUES (?, ?, ?, ?)",
        (user["id"], host_id, raw[:200], scope_val),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    row = await cur.fetchone()
    return {"success": True, "session_id": row[0]}


@router.post("/sessions/clear")
async def clear_sessions(
    user=Depends(get_current_user),
    host_id: int | None = None,
    scope: str = "default",
):
    """host_id 不为空时清空该主机会话；否则按 scope 清空全局或本机管理会话。清空本机管理会话仅管理员可操作。"""
    db = await get_db()
    scope_val = (scope or "default").strip().lower() or "default"
    if scope_val == "local" and not _is_admin_role(user.get("role")):
        raise HTTPException(status_code=403, detail="本机管理仅管理员可用")
    if host_id is not None:
        await db.execute("DELETE FROM ai_chat_sessions WHERE user_id = ? AND host_id = ?", (user["id"], host_id))
    else:
        await db.execute(
            "DELETE FROM ai_chat_sessions WHERE user_id = ? AND (host_id IS NULL OR host_id = 0) AND (COALESCE(session_scope, 'default') = ?)",
            (user["id"], scope_val),
        )
    await db.commit()
    return {"success": True}


@router.get("/sessions/{session_id}")
async def get_session(session_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, title, created_at, updated_at, COALESCE(session_prompt, '') AS session_prompt, COALESCE(session_scope, 'default') AS session_scope, COALESCE(low_interaction_mode, 'false') AS low_interaction_mode FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="会话不存在")
    session = dict(rows[0])
    if (session.get("session_scope") or "default").strip().lower() == "local" and not _is_admin_role(user.get("role")):
        raise HTTPException(status_code=404, detail="会话不存在")
    msg_rows = await db.execute_fetchall(
        "SELECT id, role, content, created_at FROM ai_chat_messages WHERE session_id = ? ORDER BY id ASC",
        (session_id,),
    )
    session["messages"] = [
        {
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "created_at": r["created_at"],
        }
        for r in msg_rows
    ]
    try:
        _rt_doc = prune_runtime_document(await load_session_runtime(db, session_id))
        _hid = session.get("host_id")
        session["runtime_active"] = list_active_items(
            _rt_doc,
            focus_host_id=int(_hid) if _hid else None,
        )
    except Exception:
        session["runtime_active"] = []
    return {"success": True, "session": session}


@router.post("/sessions/{session_id}/runtime-control")
async def push_session_runtime_control(
    session_id: int,
    req: SessionRuntimeControlRequest,
    user=Depends(get_current_user),
):
    """向运行中的会话注入控制指令（stop/pause/resume/supplement/choice）。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="会话不存在")
    action = (req.action or "supplement").strip().lower()
    if action not in _RUNTIME_ACTIONS:
        raise HTTPException(status_code=400, detail="action 仅支持 supplement/pause/resume/stop/choice")
    message = (req.message or "").strip()
    await _push_runtime_control(session_id, action, message)
    return {"success": True, "session_id": session_id, "action": action}


@router.get("/sessions/{session_id}/tool-result-caches")
async def list_session_tool_result_caches(
    session_id: int,
    user=Depends(get_current_user),
    limit: int = 30,
    offset: int = 0,
):
    db = await get_db()
    srows = await db.execute_fetchall(
        "SELECT id FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    if not srows:
        raise HTTPException(status_code=404, detail="会话不存在")
    limit = max(1, min(int(limit or 30), 200))
    offset = max(0, int(offset or 0))
    scan_limit = max(200, limit * 8)
    rows = await db.execute_fetchall(
        """SELECT id, operation, params, result, source, created_at, COALESCE(details, '') AS details
           FROM operation_logs
           WHERE user_id = ? AND operation LIKE 'ai_tool:%' AND source IN ('ai_agent', 'ops_integration')
           ORDER BY id DESC
           LIMIT ? OFFSET ?""",
        (user["id"], scan_limit, offset),
    )
    out = []
    for r in rows:
        sid = _extract_session_id_from_log_params(r["params"] or "")
        if sid != int(session_id):
            continue
        item = dict(r)
        item["tool"] = (item.get("operation") or "").replace("ai_tool:", "", 1)
        item["result_preview"] = _tool_result_preview(item.get("details") or "", max_chars=2400)
        try:
            p = json.loads(item.get("params") or "{}")
        except Exception:
            p = {}
        item["cache_truncated"] = bool((p or {}).get("cache_truncated"))
        item["cache_raw_chars"] = int((p or {}).get("cache_raw_chars") or 0)
        item["tool_call_id"] = (p or {}).get("tool_call_id") or ""
        item.pop("details", None)
        out.append(item)
        if len(out) >= limit:
            break
    return {"success": True, "items": out, "limit": limit, "offset": offset}


@router.get("/sessions/{session_id}/tool-result-caches/{cache_id}")
async def get_session_tool_result_cache(
    session_id: int,
    cache_id: int,
    user=Depends(get_current_user),
):
    db = await get_db()
    srows = await db.execute_fetchall(
        "SELECT id FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    if not srows:
        raise HTTPException(status_code=404, detail="会话不存在")
    rows = await db.execute_fetchall(
        """SELECT id, operation, params, result, source, created_at, COALESCE(details, '') AS details
           FROM operation_logs
           WHERE id = ? AND user_id = ? AND operation LIKE 'ai_tool:%'""",
        (cache_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="缓存不存在")
    row = dict(rows[0])
    sid = _extract_session_id_from_log_params(row.get("params") or "")
    if sid != int(session_id):
        raise HTTPException(status_code=404, detail="缓存不存在")
    try:
        params_obj = json.loads(row.get("params") or "{}")
    except Exception:
        params_obj = {}
    return {
        "success": True,
        "cache": {
            "id": row["id"],
            "tool": (row.get("operation") or "").replace("ai_tool:", "", 1),
            "result": row.get("result") or "",
            "source": row.get("source") or "",
            "created_at": row.get("created_at"),
            "tool_args": (params_obj or {}).get("tool_args") or {},
            "tool_call_id": (params_obj or {}).get("tool_call_id") or "",
            "cache_truncated": bool((params_obj or {}).get("cache_truncated")),
            "cache_raw_chars": int((params_obj or {}).get("cache_raw_chars") or 0),
            "details": row.get("details") or "",
        },
    }


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: int, req: UpdateSessionRequest, user=Depends(get_current_user)
):
    if req.title is None and req.low_interaction_mode is None:
        return {"success": True}
    db = await get_db()
    if req.title is not None:
        await db.execute(
            "UPDATE ai_chat_sessions SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            ((req.title or "")[:200], session_id, user["id"]),
        )
    if req.low_interaction_mode is not None:
        await db.execute(
            "UPDATE ai_chat_sessions SET low_interaction_mode = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            ("true" if bool(req.low_interaction_mode) else "false", session_id, user["id"]),
        )
    await db.commit()
    return {"success": True}


@router.patch("/sessions/{session_id}/messages/{message_id}")
async def update_session_message(
    session_id: int,
    message_id: int,
    req: UpdateSessionMessageRequest,
    user=Depends(get_current_user),
):
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT m.id, m.role
           FROM ai_chat_messages m
           JOIN ai_chat_sessions s ON s.id = m.session_id
           WHERE m.id = ? AND m.session_id = ? AND s.user_id = ?""",
        (message_id, session_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="消息不存在")
    row = dict(rows[0])
    if row.get("role") != "assistant":
        raise HTTPException(status_code=400, detail="仅助手消息支持自动修复写回")
    content = (req.content or "")[:AI_MESSAGE_SAVE_MAX]
    await db.execute(
        "UPDATE ai_chat_messages SET content = ? WHERE id = ? AND session_id = ?",
        (content, message_id, session_id),
    )
    await db.execute(
        "UPDATE ai_chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    await db.commit()
    return {"success": True, "session_id": session_id, "message_id": message_id}


@router.get("/sessions/{session_id}/prompt")
async def get_session_prompt(session_id: int, user=Depends(get_current_user)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT COALESCE(session_prompt, '') AS session_prompt FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"success": True, "prompt": (rows[0]["session_prompt"] or "")}


@router.put("/sessions/{session_id}/prompt")
async def update_session_prompt(
    session_id: int, req: SessionPromptRequest, user=Depends(get_current_user)
):
    db = await get_db()
    await db.execute(
        "UPDATE ai_chat_sessions SET session_prompt = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
        ((req.prompt or "")[:50000], session_id, user["id"]),
    )
    await db.commit()
    return {"success": True}


@router.post("/sessions/{session_id}/prompt/summarize")
async def summarize_session_prompt(
    session_id: int,
    user=Depends(get_current_user),
    action: str = "replace",  # replace | append
):
    """由 AI 根据当前会话聊天内容总结出会话级提示词，并替换或追加到会话提示词。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, COALESCE(session_prompt, '') AS session_prompt FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="会话不存在")
    current_prompt = rows[0]["session_prompt"] or ""

    # 取最近 20 条消息、每条前 800 字，总长限制 6000 字，减轻负载与超时
    msg_rows = await db.execute_fetchall(
        "SELECT role, SUBSTR(content, 1, 800) AS content FROM ai_chat_messages WHERE session_id = ? ORDER BY id DESC LIMIT 20",
        (session_id,),
    )
    msg_rows = list(reversed(msg_rows))
    if not msg_rows:
        raise HTTPException(status_code=400, detail="会话暂无消息，请先发送几条对话后再总结")

    settings = await _get_user_ai_settings(db, user["id"])
    base_url = (settings.get("ai_base_url") or "").strip().rstrip("/")
    if not base_url:
        base_url = (getattr(_config, "AI_BASE_URL", "") or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="AI 未配置服务地址，请在「AI 配置」中填写")
    api_key = (settings.get("ai_api_key") or "").strip()
    provider = _effective_provider(settings, base_url)
    if require_api_key(provider, api_key) and not api_key:
        system_key, system_base = await _get_system_key_and_base(db)
        if _allow_system_shared_api_key(system_key, system_base, resolved_base_url=base_url):
            trial = await _consume_system_ai_usage(db, user["id"])
            if trial.get("exhausted"):
                raise HTTPException(
                    status_code=403,
                    detail=f"系统共享 Key 调用配额已用尽（上限 {trial.get('limit', SYSTEM_AI_USAGE_LIMIT)} 次）。请在「系统设置 → 我的 AI 配置」填写自有 Base URL / API Key / Model 以解除次数限制，或联系管理员重置配额计数。",
                )
            api_key = system_key
            if not (settings.get("ai_base_url") or "").strip() and (system_base or "").strip():
                base_url = (system_base or "").strip().rstrip("/")
        else:
            raise HTTPException(status_code=400, detail="AI 未配置 API Key，请在「AI 配置」中填写")

    # 只把「用户要求」和「助手的指令/决策」传给 AI，不传程序输出的日志
    lines = []
    for r in msg_rows:
        if r["role"] == "user":
            lines.append(f"用户: {(r['content'] or '').strip()}")
        else:
            lines.append(f"助手: {_assistant_content_for_summary(r['content'] or '')}")
    conv_text = "\n".join(lines)
    ask = f"""请根据以下对话内容，归纳出一段「会话级提示词」（用于约束 AI 在本会话中的行为，如：只做某类任务、用某种风格、禁止某类操作等）。以下仅包含用户要求和助手的指令/决策，已不含程序输出或日志。要求：
1. 输出请使用 **Markdown 格式**（如 ## 小标题、- 列表、`行内代码`、``` 代码块等），便于查看与阅读。
2. 尽量简洁，总长控制在 500 字以内。
3. 若对话与运维/操作无关或无法归纳，可输出「无」或简短说明。

当前会话已有会话级提示词：
{current_prompt or '(无)'}

对话内容（仅用户要求与助手指令）：
{conv_text[:6000]}
"""
    api_url = ensure_chat_completions_url(base_url)
    model = normalize_model(provider, settings.get("ai_model") or "")
    headers = prepare_headers(provider, api_key)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
            resp = await client.post(
                api_url,
                headers=headers,
                json={"model": model, "messages": [{"role": "user", "content": ask}], "max_tokens": _resolve_request_max_tokens({}), "stream": False},
            )
        if resp.status_code != 200:
            err_detail = resp.text[:500] if resp.text else "未知错误"
            try:
                err_json = resp.json()
                err_detail = err_json.get("error", {}).get("message", err_json.get("message", err_detail)) or err_detail
            except Exception:
                pass
            raise HTTPException(status_code=502, detail=f"AI 服务返回错误 ({resp.status_code}): {err_detail}")
        try:
            result = resp.json()
            msg, _ = parse_chat_response(result)
            summary = (extract_message_content(msg) or "").strip().replace("（无）", "").replace("(无)", "").strip()[:4000]
        except Exception as parse_err:
            logger.warning("Summarize response parse error: %s", parse_err)
            raise HTTPException(status_code=502, detail="AI 返回格式异常，请稍后重试")
        if not summary or summary.lower() in ("无", "none", "n/a"):
            return {"success": True, "prompt": current_prompt, "skipped": True}
        if action == "append" and current_prompt:
            new_prompt = (current_prompt.rstrip() + "\n\n" + summary).strip()[:50000]
        else:
            new_prompt = summary[:50000]
        await db.execute(
            "UPDATE ai_chat_sessions SET session_prompt = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (new_prompt, session_id, user["id"]),
        )
        await db.commit()
        return {"success": True, "prompt": new_prompt}
    except HTTPException:
        raise
    except httpx.TimeoutException as e:
        logger.warning("Summarize session prompt timeout: %s", e)
        raise HTTPException(status_code=504, detail="请求超时，请稍后重试或减少会话消息后再总结")
    except httpx.RequestError as e:
        logger.warning("Summarize session prompt request error: %s", e)
        raise HTTPException(status_code=502, detail="无法连接 AI 服务，请检查网络或配置: " + str(e)[:200])
    except Exception as e:
        logger.exception("Summarize session prompt failed: %s", e)
        raise HTTPException(status_code=500, detail="总结失败: " + str(e)[:200])


# ── 主机级 AI 提示词（按 用户 × 主机 维度独立保存；分享给其他用户时提示词不共用） ──
async def _load_host_for_prompt(db, host_id: int, user: dict) -> dict:
    """读取主机，并校验当前用户对该主机具备访问权限；否则抛 404。"""
    rows = await db.execute_fetchall(
        "SELECT id, name, host, port, created_by FROM hosts WHERE id = ?",
        (host_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="主机不存在")
    host_row = dict(rows[0])
    if not await _can_access_host_with_shares(db, host_row, user):
        raise HTTPException(status_code=404, detail="主机不存在")
    return host_row


@router.get("/hosts/{host_id}/prompt")
async def get_host_prompt(host_id: int, user=Depends(get_current_user)):
    """读取当前用户在指定主机下的主机级 AI 提示词。"""
    db = await get_db()
    await _load_host_for_prompt(db, host_id, user)
    rows = await db.execute_fetchall(
        "SELECT COALESCE(content, '') AS content, updated_at FROM ai_host_prompts WHERE host_id = ? AND user_id = ?",
        (host_id, user["id"]),
    )
    if not rows:
        return {"success": True, "prompt": "", "updated_at": None}
    return {
        "success": True,
        "prompt": rows[0]["content"] or "",
        "updated_at": rows[0]["updated_at"],
    }


@router.put("/hosts/{host_id}/prompt")
async def update_host_prompt(
    host_id: int, req: HostPromptRequest, user=Depends(get_current_user)
):
    """写入/覆盖当前用户在指定主机下的主机级 AI 提示词。"""
    db = await get_db()
    await _load_host_for_prompt(db, host_id, user)
    content = (req.prompt or "")[:50000]
    await db.execute(
        """INSERT INTO ai_host_prompts (host_id, user_id, content, updated_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(host_id, user_id) DO UPDATE SET content = excluded.content, updated_at = CURRENT_TIMESTAMP""",
        (host_id, user["id"], content),
    )
    await db.commit()
    return {"success": True, "prompt": content}


@router.post("/hosts/{host_id}/prompt/summarize")
async def summarize_host_prompt(
    host_id: int,
    user=Depends(get_current_user),
    action: str = "replace",  # replace | append
):
    """由 AI 结合该主机下当前用户的近期会话内容，归纳出一段「主机级提示词」，并替换或追加保存。"""
    db = await get_db()
    await _load_host_for_prompt(db, host_id, user)
    rows = await db.execute_fetchall(
        "SELECT COALESCE(content, '') AS content FROM ai_host_prompts WHERE host_id = ? AND user_id = ?",
        (host_id, user["id"]),
    )
    current_prompt = (rows[0]["content"] if rows else "") or ""

    sid_rows = await db.execute_fetchall(
        """SELECT id FROM ai_chat_sessions
           WHERE user_id = ? AND host_id = ?
           ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 3""",
        (user["id"], host_id),
    )
    sid_list = [r["id"] for r in sid_rows]
    msg_rows: list = []
    if sid_list:
        placeholders = ",".join(["?"] * len(sid_list))
        msg_rows = await db.execute_fetchall(
            f"""SELECT role, SUBSTR(content, 1, 800) AS content
                FROM ai_chat_messages
                WHERE session_id IN ({placeholders})
                ORDER BY id DESC LIMIT 40""",
            sid_list,
        )
        msg_rows = list(reversed(msg_rows))

    if not msg_rows:
        raise HTTPException(status_code=400, detail="该主机暂无对话记录，无法总结；请先在该主机下发起 AI 对话，再进行总结")

    settings = await _get_user_ai_settings(db, user["id"])
    base_url = (settings.get("ai_base_url") or "").strip().rstrip("/")
    if not base_url:
        base_url = (getattr(_config, "AI_BASE_URL", "") or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="AI 未配置服务地址，请在「AI 配置」中填写")
    api_key = (settings.get("ai_api_key") or "").strip()
    provider = _effective_provider(settings, base_url)
    if require_api_key(provider, api_key) and not api_key:
        system_key, system_base = await _get_system_key_and_base(db)
        if _allow_system_shared_api_key(system_key, system_base, resolved_base_url=base_url):
            trial = await _consume_system_ai_usage(db, user["id"])
            if trial.get("exhausted"):
                raise HTTPException(
                    status_code=403,
                    detail=f"系统共享 Key 调用配额已用尽（上限 {trial.get('limit', SYSTEM_AI_USAGE_LIMIT)} 次）。请在「系统设置 → 我的 AI 配置」填写自有 Base URL / API Key / Model 以解除次数限制，或联系管理员重置配额计数。",
                )
            api_key = system_key
            if not (settings.get("ai_base_url") or "").strip() and (system_base or "").strip():
                base_url = (system_base or "").strip().rstrip("/")
        else:
            raise HTTPException(status_code=400, detail="AI 未配置 API Key，请在「AI 配置」中填写")

    lines = []
    for r in msg_rows:
        if r["role"] == "user":
            lines.append(f"用户: {(r['content'] or '').strip()}")
        else:
            lines.append(f"助手: {_assistant_content_for_summary(r['content'] or '')}")
    conv_text = "\n".join(lines)
    ask = f"""请根据以下该「主机」下的近期对话内容，归纳出一段「主机级提示词」。主机级提示词的目的是让 AI 下次在此主机上工作时，更准确了解此主机的**独有规则 / 能力 / 配置 / 工具链 / 特殊约束**（例如：已安装 gh cli、cursor cli、opencode；数据目录位于 /data/app；禁止重启 nginx；默认使用 zsh + homebrew；时区 Asia/Shanghai 等）。

要求：
1. 输出使用 **Markdown 格式**（## 小标题、- 列表、`行内代码`、``` 代码块等），便于查看。
2. 只归纳**与该主机相关的规则/能力/配置/工具链/注意事项**；不要输出终端命令原文或程序日志。
3. **不要**把账号密码、Token、私钥等机密信息写入（这类机密请放到「主机知识库」而非主机级提示词）。
4. 总长控制在 800 字以内。若无法归纳出有效信息，输出「无」。

当前已有的主机级提示词（供参考，避免重复）：
{current_prompt or '(无)'}

对话内容（用户要求 + 助手指令）：
{conv_text[:6000]}
"""
    api_url = ensure_chat_completions_url(base_url)
    model = normalize_model(provider, settings.get("ai_model") or "")
    headers = prepare_headers(provider, api_key)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
            resp = await client.post(
                api_url,
                headers=headers,
                json={"model": model, "messages": [{"role": "user", "content": ask}], "max_tokens": _resolve_request_max_tokens({}), "stream": False},
            )
        if resp.status_code != 200:
            err_detail = resp.text[:500] if resp.text else "未知错误"
            try:
                err_json = resp.json()
                err_detail = err_json.get("error", {}).get("message", err_json.get("message", err_detail)) or err_detail
            except Exception:
                pass
            raise HTTPException(status_code=502, detail=f"AI 服务返回错误 ({resp.status_code}): {err_detail}")
        try:
            result = resp.json()
            msg, _ = parse_chat_response(result)
            summary = (extract_message_content(msg) or "").strip().replace("（无）", "").replace("(无)", "").strip()[:4000]
        except Exception as parse_err:
            logger.warning("Summarize host prompt parse error: %s", parse_err)
            raise HTTPException(status_code=502, detail="AI 返回格式异常，请稍后重试")
        if not summary or summary.lower() in ("无", "none", "n/a"):
            return {"success": True, "prompt": current_prompt, "skipped": True}
        if action == "append" and current_prompt:
            new_prompt = (current_prompt.rstrip() + "\n\n" + summary).strip()[:50000]
        else:
            new_prompt = summary[:50000]
        await db.execute(
            """INSERT INTO ai_host_prompts (host_id, user_id, content, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(host_id, user_id) DO UPDATE SET content = excluded.content, updated_at = CURRENT_TIMESTAMP""",
            (host_id, user["id"], new_prompt),
        )
        await db.commit()
        return {"success": True, "prompt": new_prompt}
    except HTTPException:
        raise
    except httpx.TimeoutException as e:
        logger.warning("Summarize host prompt timeout: %s", e)
        raise HTTPException(status_code=504, detail="请求超时，请稍后重试")
    except httpx.RequestError as e:
        logger.warning("Summarize host prompt request error: %s", e)
        raise HTTPException(status_code=502, detail="无法连接 AI 服务，请检查网络或配置: " + str(e)[:200])
    except Exception as e:
        logger.exception("Summarize host prompt failed: %s", e)
        raise HTTPException(status_code=500, detail="总结失败: " + str(e)[:200])


@router.delete("/sessions/{session_id}/messages")
async def clear_session_messages(
    session_id: int, req: ClearMessagesRequest, user=Depends(get_current_user)
):
    """清空当前会话的全部消息，或保留前 N 条、删除 N 条以后的全部。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="会话不存在")
    if req.clear == "all":
        await db.execute("DELETE FROM ai_chat_messages WHERE session_id = ?", (session_id,))
    else:
        n = max(1, min(int(req.keep_n), 10000))
        # 保留前 n 条，删除其后的全部（ORDER BY id ASC LIMIT -1 OFFSET n 得到第 n+1 条及以后）
        sub = "SELECT id FROM ai_chat_messages WHERE session_id = ? ORDER BY id ASC LIMIT -1 OFFSET ?"
        await db.execute(
            "DELETE FROM ai_chat_messages WHERE session_id = ? AND id IN (" + sub + ")",
            (session_id, session_id, n),
        )
    await db.commit()
    return {"success": True}


@router.get("/skills")
async def get_skills(user=Depends(get_current_user)):
    """返回 毛竹 AI 工具清单（名称与描述）。"""
    return get_skills_summary()


@router.post("/sessions/{session_id}/summarize-title")
async def summarize_session_title(session_id: int, user=Depends(get_current_user)):
    """根据会话内容调用 AI 归纳生成简短标题并更新。需已配置 AI 且会话属于当前用户。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="会话不存在")
    settings = await _get_user_ai_settings(db, user["id"])
    base_url = (settings.get("ai_base_url") or "").strip().rstrip("/")
    if not base_url:
        base_url = (getattr(_config, "AI_BASE_URL", "") or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="请先在 AI 配置中填写服务地址")
    api_key = (settings.get("ai_api_key") or "").strip()
    provider = _effective_provider(settings, base_url)
    if require_api_key(provider, api_key) and not api_key:
        system_key, system_base = await _get_system_key_and_base(db)
        if _allow_system_shared_api_key(system_key, system_base, resolved_base_url=base_url):
            trial = await _consume_system_ai_usage(db, user["id"])
            if trial.get("exhausted"):
                raise HTTPException(
                    status_code=403,
                    detail=f"系统共享 Key 调用配额已用尽（上限 {trial.get('limit', SYSTEM_AI_USAGE_LIMIT)} 次）。请在「系统设置 → 我的 AI 配置」填写自有 Base URL / API Key / Model 以解除次数限制，或联系管理员重置配额计数。",
                )
            api_key = system_key
            if not (settings.get("ai_base_url") or "").strip() and (system_base or "").strip():
                base_url = (system_base or "").strip().rstrip("/")
        else:
            raise HTTPException(status_code=400, detail="请先在 AI 配置中填写 API Key")
    model = normalize_model(provider, settings.get("ai_model") or "")
    # 先检查是否有消息，避免用户等待后才发现无消息
    msg_count = await db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM ai_chat_messages WHERE session_id = ?", (session_id,)
    )
    if not msg_count or (msg_count[0]["c"] or 0) == 0:
        raise HTTPException(status_code=400, detail="会话暂无消息，请先发送几条对话后再生成名称")
    title, err = await _summarize_session_title(
        session_id, api_key, base_url, model, force=True
    )
    if not title:
        raise HTTPException(status_code=400, detail=err or "无法生成标题")
    # 写入维护历史：由主机AI对话 / AI助手对话 总结构成，综述=会话标题，操作人=当前用户，时间=当前
    sess_rows = await db.execute_fetchall(
        "SELECT host_id FROM ai_chat_sessions WHERE id = ?", (session_id,)
    )
    host_id = sess_rows[0]["host_id"] if sess_rows else None
    if host_id:
        h_rows = await db.execute_fetchall("SELECT name, host FROM hosts WHERE id = ?", (host_id,))
        host_str = (h_rows[0]["host"] or h_rows[0]["name"] or str(host_id)) if h_rows else str(host_id)
        category = "主机AI对话"
    else:
        host_str = "AI助手"
        category = "AI助手对话"
    details_json = json.dumps({"session_id": session_id, "source": "ai_session_summary"})
    await db.execute(
        """INSERT INTO server_maintenance_history (host, port, category, content, details, created_by)
           VALUES (?, 22, ?, ?, ?, ?)""",
        (host_str, category, (title or "")[:500], details_json, user["id"]),
    )
    await db.commit()
    return {"success": True, "title": title}


def _cot_merge_assistant_reasoning_for_stream(msg: dict) -> str:
    """合并助手消息中可能承载「规划/推理」的字段，供 CoT SSE（部分厂商单独放 reasoning/thinking）。"""
    parts: list[str] = []
    base = (extract_message_content(msg) or "").strip()
    if base:
        parts.append(base)
    if isinstance(msg, dict):
        for key in ("reasoning", "reasoning_content", "thinking"):
            raw = msg.get(key)
            if isinstance(raw, str) and raw.strip():
                parts.append(raw.strip())
            elif isinstance(raw, dict):
                for subk in ("text", "content", "value", "summary"):
                    txt = raw.get(subk)
                    if isinstance(txt, str) and txt.strip():
                        parts.append(txt.strip())
                        break
    out: list[str] = []
    for p in parts:
        if p and p not in out:
            out.append(p)
    return "\n\n".join(out).strip()


def _cot_fallback_tool_plan_text(tool_names: list[str], output_locale: str) -> str:
    """模型未返回可见说明时生成一条规划句，避免只有工具步骤、没有推理步骤。"""
    names = [str(n).strip() for n in tool_names if str(n).strip()]
    if not names:
        return ""
    loc = (output_locale or "").lower()
    if loc.startswith("zh"):
        return "本轮未返回自然语言说明，计划依次调用：" + "、".join(names) + "。"
    return "No natural-language plan in the model reply; will call: " + ", ".join(names) + "."


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int, user=Depends(get_current_user)):
    db = await get_db()
    await db.execute(
        "DELETE FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    await db.commit()
    return {"success": True}


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_keepalive() -> str:
    """周期性下行的极小 `data:` 帧（非注释）。部分代理/中间层会丢弃 SSE 注释行 `:`，
    导致仅靠注释无法保活；用 JSON 空事件维持 chunked 流上的字节活动。前端忽略未知字段即可。"""
    return _sse({"_edgeops_ping": 1})


def _format_poll_wait_aborted_message(reason: str, output_locale: str = "zh-CN") -> str:
    """轮询等待被中断（用户停止、页面断开等）时写入会话的助手说明。"""
    r = (reason or "").strip().lower()
    if output_locale == "en":
        if r == "client_disconnected":
            return (
                "**Wait cancelled** — the connection closed (page refresh or navigation). "
                "This run was interrupted. Send a new message to continue the task."
            )
        if r == "user_pause":
            return (
                "**Wait paused** — you paused during the countdown. "
                "This run was interrupted. Send a new message when you want to continue."
            )
        return (
            "**Wait stopped** — you cancelled the countdown. "
            "This run was interrupted. Send a new message to continue the task."
        )
    if r == "client_disconnected":
        return (
            "**等待已取消**：连接已断开（页面刷新或离开）。本次 AI 执行已中断，"
            "请重新发送指令以继续任务。"
        )
    if r == "user_pause":
        return (
            "**等待已暂停**：您在倒计时期间选择了暂停。本次 AI 执行已中断，"
            "需要时请重新发送消息继续。"
        )
    return (
        "**等待已停止**：您已放弃本次倒计时等待。AI 执行已中断，"
        "请重新说明需求或发送「继续」以接续任务。"
    )


async def _poll_wait_sse(
    total_seconds: int,
    *,
    http_request: Request | None,
    consume_runtime_control,
    out_status: list[str],
):
    """分段 sleep 并推送 waiting 事件；可被断开连接或运行时 stop/pause 打断。"""
    out_status[:] = ["continue"]
    total = max(1, min(3600, int(total_seconds or 0)))
    chunk = max(1, min(5, int(AGENT_POLL_WAIT_CHUNK_SEC or 1)))
    elapsed = 0

    def _tick(rem: int) -> str:
        return _sse({
            "action": "waiting",
            "seconds": total,
            "wait_elapsed": elapsed,
            "wait_remaining": rem,
        })

    yield _tick(total)
    while elapsed < total:
        if http_request is not None:
            try:
                if await http_request.is_disconnected():
                    out_status[:] = ["client_disconnected"]
                    yield _sse({
                        "action": "waiting_aborted",
                        "reason": "client_disconnected",
                        "wait_elapsed": elapsed,
                    })
                    return
            except Exception:
                pass
        if consume_runtime_control is not None:
            ctrl = await consume_runtime_control()
            if ctrl:
                act = (ctrl.get("action") or "").strip().lower()
                if act == "stop":
                    out_status[:] = ["user_stop"]
                    yield _sse({
                        "runtime_control": {"action": "stop", "accepted": True, "during_wait": True},
                    })
                    yield _sse({
                        "action": "waiting_aborted",
                        "reason": "user_stop",
                        "wait_elapsed": elapsed,
                    })
                    return
                if act == "pause":
                    out_status[:] = ["user_pause"]
                    yield _sse({
                        "runtime_control": {"action": "pause", "accepted": True, "during_wait": True},
                    })
                    yield _sse({
                        "action": "waiting_aborted",
                        "reason": "user_pause",
                        "wait_elapsed": elapsed,
                    })
                    return
                if act == "supplement":
                    _sup_msg = (ctrl.get("message") or "").strip()
                    out_status[:] = ["supplement", _sup_msg]
                    yield _sse({
                        "runtime_control": {"action": "supplement", "accepted": True, "during_wait": True},
                    })
                    return
        step = min(chunk, total - elapsed)
        await asyncio.sleep(step)
        elapsed += step
        rem = max(0, total - elapsed)
        yield _tick(rem)
        if rem > 0:
            yield _sse_keepalive()
    out_status[:] = ["continue"]


async def _poll_wait_blocking(
    total_seconds: int,
    *,
    session_id: int | None = None,
) -> str:
    """非 SSE 路径的分段等待；返回 continue 或中断原因。"""
    total = max(1, min(3600, int(total_seconds or 0)))
    chunk = max(1, min(5, int(AGENT_POLL_WAIT_CHUNK_SEC or 1)))
    elapsed = 0
    while elapsed < total:
        if session_id is not None:
            ctrl = await _pull_runtime_control_nowait(session_id)
            if isinstance(ctrl, dict):
                act = (ctrl.get("action") or "").strip().lower()
                if act == "stop":
                    return "user_stop"
                if act == "pause":
                    return "user_pause"
                if act == "supplement":
                    return "supplement:" + ((ctrl.get("message") or "").strip())
        step = min(chunk, total - elapsed)
        await asyncio.sleep(step)
        elapsed += step
    return "continue"


def _sanitize_system_prompt_local_scope(text: str, *, session_scope: str, user: dict) -> str:
    """从系统提示中移除旧版「本机管理」整段描述（仅在本机管理会话且管理员下保留原文）。
    避免 AI 助手 / 主机详情 / 集成等会话中误调 local_chat_data_paths 等本机专用工具。"""
    s = text or ""
    if session_scope == "local" and _is_admin_role(user.get("role")):
        return s
    start = s.find("**本机管理**（仅管理员")
    if start < 0:
        start = s.find("**本机管理**")
    if start < 0:
        return s
    anchor = "\n本系统中「控制台」"
    end = s.find(anchor, start)
    if end < 0:
        return s
    note = (
        "\n【会话与工具范围】本机专用工具（如 local_chat_data_paths、local_exec、create_local_console、local_fs_*、process_*）"
        "仅出现在「本机管理」会话且为管理员账号时；**当前会话不要调用这些名称**。"
        "生成 HTML、落盘文件请用 **fs_***（web/fs 当前用户目录）或 **create_chat_artifact**。\n"
    )
    return s[:start] + note + s[end:]


_PROMPT_ENTITY_RESOLUTION_RULES = """
【名词解析 / 资产与功能映射 — 执行前必读，此条不可跳过】
当用户要求你做某事，且消息里出现**项目名、环境名、服务名、组件名、口语称呼、缩写、代号、业务名词**等（未必是正式主机名或 IP）时，**禁止凭猜测**直接选主机或执行命令。必须先做「用户名词 → 哪台主机 / 哪项功能 / 哪条约定」的对齐，再动手。

**提示词与约定检索（按优先级）**：
1. **会话级约束**（若 system 已注入「会话级约束」块）— 用户对本会话的专用约定，**必须遵守**。
2. **主机级提示词**（若 system 已注入「主机级提示词」块）— 该机独有规则、工具链、服务/功能映射；**必须遵守**。未注入时对该候选 host_id 调用 `get_host_prompt(host_id)` 读取全文。
3. **用户自定义 system 提示词**（本消息靠前的主体规则）— 全局约定与术语表。
4. **主机知识库** — 机密凭据与内部连接信息；用 `get_host_knowledge(host_id)` 按需读取，**严禁**在回复中泄露。

**主机资产检索（名词可能是别名 / 标签 / 用途 / 分组）**：
- `search_hosts(query=名词, tag_ids?, group_id?, regex?, limit?)` — 匹配 **name、IP、port、remark、aliases、tag_names** 等。
- `list_hosts(q=名词, tag_ids?, group_id?)` — 同上，适合轻量模糊搜。
- `list_host_tags` → 用 tag id 配合 `search_hosts` / `list_hosts` — 用户说「生产」「网关」「K8s」等**标签语义**时优先。
- `get_host_groups_tree(host_q=名词)` — 在分组树中定位主机集合。
- 「当前主机列表」JSON 上下文 — 快速核对 id、name、host、**aliases**、**tag_names**、remark。

**功能 / 能力映射（名词写在主机提示词或最佳实践里）**：
- `search_hosts_by_prompt(query=名词, group_id?, tag_ids?)` — 在**当前用户维护的各主机级提示词**中搜索（如「Redis 主库」「装了 gh cli」「网关」「禁止重启 nginx」）。
- 命中后对该 host_id 再调 `get_host_prompt` / `get_host_knowledge` 读全文，判断用户说的功能/服务对应哪台机、哪条操作约定。
- 涉及安装、配置、部署、排障等通用流程时，用 `get_best_practices(category 或 keyword=名词)` 查是否有现成推荐，并在回复中可简要说明参考了哪条。

**解析结果怎么用**：
- **唯一命中**：在回复或执行前用一句话说明依据（如「已将『网关』解析为 host_id=3，依据：别名 gateway-prod + 主机提示词含 nginx 网关约定」），再 `ssh_execute` / 控制台操作。
- **多台候选**：列出候选（id、name、aliases、tag_names、remark 摘要、提示词命中片段），请用户确认或给出最可能项及依据；**不要**默认选第一台。
- **未命中**：说明已检索的层（别名/标签/提示词/最佳实践），再请用户补充主机名、IP 或 host_id。

**跨主机**：即使本会话已绑定一台主机，用户若提到**另一台**或**多台**上的名词，仍须对**每一台**分别检索提示词/别名/标签，不得用绑定机约定代替其它机器。
"""


def _build_system_prompt() -> str:
    brand = _config.PRODUCT_NAME_ZH
    pd = _config.PRODUCT_DISPLAY
    header = (
        f"你是{brand}，通过 SSH 管理远程主机；你所接入的产品是{pd}。"
        f"向用户介绍自己时**仅自称「{brand}」**（说明产品时可写{pd}），"
        f"勿称 {'Edge' + 'Ops'}、勿称「{'Edge' + 'Ops'} 的 AI 运维助手」、勿用「AI 运维助手」等其它旧称代指你自己。\n\n"
    )
    return header + """你拥有一组工具：列出/查询主机、在主机上执行 SSH 命令、向用户当前打开的控制台注入输入、查看维护历史与主机分组；主机分享管理（share_host、revoke_host_share、list_host_shares、list_received_host_shares）；主机标签管理（list_host_tags、create_host_tag、update_host_tag、delete_host_tag、set_host_tags，标签按用户隔离）；以主机为维度的 AI 知识（get_host_knowledge / update_host_knowledge / append_host_knowledge，偏机密凭据，严禁回复中展示）；**主机级 AI 提示词**（get_host_prompt / update_host_prompt / append_host_prompt / search_hosts_by_prompt，偏可展示的主机**独有规则 / 能力 / 工具链 / 配置**，按 user×host 独立保存，可跨主机搜索）；主机侧 .edgeops 工作区（edgeops_init_workspace、edgeops_save_script、edgeops_read_workspace_context、edgeops_append_task_log、edgeops_write_rule、edgeops_write_info）；**web/fs 文件系统**（**web/fs/当前用户名** 的目录内容，用于脚本/缓存/上传到主机等）：fs_list、**get_chats_workspace_dir**（当日 chats/UTC 工作前缀）、fs_read_file（支持 offset/size）、fs_write_file（支持 overwrite/append/定位写；自动归位 chats/UTC 并加 UUID 文件名）、fs_read_binary、fs_write_binary（二进制内容支持 encoding=base64|hex）、fs_mkdir、fs_pack_tgz、fs_unpack_tgz、fs_delete、fs_copy；批量任务（batch_create、list_batch_operations、get_batch_detail、batch_cancel、batch_retry、clear_batches，支持 scope_type=tag 按标签批量）；操作日志可查询（list_logs）与清空（clear_logs）；最佳实践、凭证、用户与 AI 配置等；**操作帮助文档**（get_aihelp_index、list_aihelp_files、get_aihelp_file，仅管理员可写：write_aihelp_file、update_aihelp_index）。主机分享只共享主机访问权限，不共享双方历史聊天会话与聊天记录。

通用数据处理工具：你可以直接调用 **regex_process** 做正则搜索/提取/替换预览，**string_process** 做字符串清洗、编码、哈希与行数统计，**math_calculate** 做数学/科学计算（**NumPy 数组与批量数据集+公式**、**SymPy 符号**、统计、单位换算），**data_query** 解析并搜索/分析 JSON、YAML 等结构化数据，**markup_query** 解析并搜索/提取 XML、HTML 标签、文本、属性、链接，**crypto_toolkit** 做常见密码/证书操作（MD5/SHA*、HEX/二进制转换、AES/DES、RSA/ECC 签验、证书生成/解析/校验）。遇到数值批处理、公式推导、统计汇总时**优先 math_calculate**（尤其 `operation=batch`：给 dataset + expression）；大量文本/JSON 用 data_query；若数据量巨大再写脚本。

**web/fs 工作目录（须遵守）**：与当前聊天相关的工作产物——**本地脚本、中间数据、拉取结果、分析报告**——**必须**落在 **`chats/<UTC年>/<月>/<日>/`** 下（与附件、工具 spill、artifact 同级 UTC 分卷习惯一致），**禁止**默认写到用户 fs **根目录**或根下裸的 `scripts/`、`2026/`、`data/` 等（除非用户明确要求在根目录）。文件名形态 **`{标准UUID}-{简短英文或拼音描述}.{后缀}`**，描述用 ASCII/kebab-case 或下划线，避免空格。可先 **`get_chats_workspace_dir`** 取得当日准确前缀。**fs_write_file** / **fs_write_binary** 会为你的 path **自动归位**到 `chats/<UTC日期>/` 并加 UUID 前缀；**scp_pull** 未写 `chats/` 时也会自动补上当日目录并规范文件名。主机上的 **edgeops_save_script** 写的是远端 `~/.edgeops`，与本地 web/fs 本条无关。**例外 — Agent Skills**：`skills/<name>/` 下的 SKILL.md 与附属文件**不走 chats 归位**；须用 **save_user_skill**、**write_user_skill_file**、**read_user_skill_file**、**list_user_skill_files**、**delete_user_skill_file** 等 Skill 专用工具，**禁止**用 fs_write_file/fs_mkdir/fs_delete 操作 `skills/` 或 `chats/.../skills/` 路径。

**大数据与「下载 → 结构化 → 分析」**：对服务器上**大量**或**复杂**数据，优先在远端 **粗加工 / 聚合**（awk、grep、sort、python -c 等）重定向到文件，**scp_pull**（支持大文件/目录，有传输进度）到 `chats/今日/`，再在本地把它转成 **csv / jsonl / 规整列** 后再用 **data_query**、**fs_read_file(offset/size)**、小段 **regex_process** 分析；**非结构化 / 半结构化**日志与杂文本**先抽取字段**（时间、主机、级别、消息主体等）变结构化再统计，少吃 LLM 上下文。答复用户时用摘要 + 文件路径引用，避免把整文件粘进 assistant 正文。

**本机管理类工具（local_*、local_chat_*、process_*）**：仅在「本机管理」专属会话且当前账号为**管理员**时，才会出现在你的可调工具列表并由系统注入详细用法。**若当前为 AI 助手 / 主机运维 / 集成等普通会话**：不要尝试调用上述名称；写天气页、HTML、脚本产物等请用 **fs_***（`web/fs/<当前用户名>`）或 **create_chat_artifact**。不要臆造工具名。

**主机分组权限**：任意登录用户都可用 **create_group** 创建**自己的**分组；用 **add_hosts_to_group** 将**自己有访问权的主机**（自己创建的 + 他人 **share_host** 分享给你的）加入**自己创建的**分组。**update_group** / **delete_group** / **remove_host_from_group** 仅要求你对目标分组有操作权（你是分组创建者，或你是管理员）。**不要**向用户谎称「只有管理员能建组或把主机加组」；若工具返回无权，再根据错误区分是「分组不归当前用户」还是「对某台主机无访问权」。

本系统中「控制台」与「终端」同义。**AI 助手页**为 SSH 控制台：可用 create_console(host_id) 打开、close_console(slot) 关闭（仅可关闭 AI 创建的控制台）；用户也可在界面点击「+ 新建控制台」或标签旁 × 关闭。**上传**到主机：**scp_push**（SFTP 流式；`content` 适合小文本，`local_path` 适合大文件/目录，目录需 `recursive=true`，调用卡显示进度）。**从主机拉回**：**scp_pull**（SFTP 流式，支持大文件/目录，有进度）。**大输出策略**：预期 stdout/stderr 很大时，优先在远端重定向到文件（如 `> /tmp/out.log`），再 **scp_pull** 到 web/fs；若需在机器上聚合/过滤大数据，可在 web/fs 写 `.py`/`.sh`，**scp_push** 上机执行，结果再写入另一远端文件后 **scp_pull**，减少经对话上下文的流量。

【防幻觉 / 执行必须经 tool_call，此条不可违反】
- 本系统只有在你在当轮回复中发起 tool_call 时才会真正执行操作。任何「执行类」动作（在主机上跑命令、向控制台发输入、上传文件、创建/修改/删除主机或凭证或分组、写文件、批量任务、写主机知识、写最佳实践等）都必须通过调用对应工具完成，不能省略。
- 禁止在纯文本中声称已执行某操作而未实际发起 tool_call。例如：不能说「我已经执行了 ls」「已为您在主机上安装了 nginx」而本轮没有调用 ssh_execute / send_to_terminal / batch_create / fs_write_file / fs_delete 等**当前会话工具列表里实际存在**的工具。不要声称已调用 **local_***、**process_*** 等本机专用工具，除非你在本机管理会话且工具列表中能看到它们。未调用工具即表示未执行，用户会认为被欺骗。
- 若需要执行某动作：必须先发起一次（或多次）工具调用，等工具返回结果后，再根据真实返回结果向用户汇报。不得根据「假设的执行结果」或想象的结果来回复。
- 仅当用户只是咨询、查看、总结等不改变系统状态或主机状态时，可以只回复文字不调用工具；一旦涉及「做某件事」（执行命令、上传、创建、修改、删除、发送输入等），必须且只能通过 tool_call 完成，这一步不能省。

【数据清单 / 表格防编造，此条不可违反】
- 主机、虚拟机、进程、端口、漏洞、告警、资产、日志条目等**枚举类、清单类**回复：**必须先**通过 ssh_execute / get_terminal_buffer / list_hosts / data_query / regex_process / fs_read_file / read_chat_data 等工具取得**真实原始数据**，再整理成表格或列表；禁止凭记忆、推断或「补全感」增删行、增删列、合并两列、臆造备注/用途/状态。
- Markdown 表格中**每一行、每一格**都必须能在当轮或本轮对话中**已返回的工具结果**里找到依据；工具结果里没有的字段填「—」或**省略该列**，禁止猜测、编造、用相近项凑数。
- 工具返回含「已省略」「已截断」或总数对不上时：**不得**输出看似完整的表格；应说明「已获取 X / 共 Y 条（或未知总数）」并继续 read_chat_data 分页 / 重跑命令 / scp_pull 落盘后再汇总；未完成前可只给摘要，勿硬凑满表。
- **禁止**为解释数据矛盾（如 ID 重复、行数不符、列错位）而编造 NOTE、脚注或「可能原因」；若解析异常，应重读原始输出、换 regex/data_query 再解析，或明确标注「解析失败，见原始输出路径」。
- 推荐流程：**命令/查询 → 原始输出落盘（大结果）→ regex_process / data_query 结构化 → 对账（行数、唯一键）→ 再输出 Markdown 表或 create_chat_artifact(CSV)**；不要让模型在正文里「手搓」大表。
- 与上方「清单/列表防漏项」配合：防漏项强调**不遗漏**；本条强调**不编造**——二者均须遵守。

操作流程：
1. 理解用户意图；若消息含业务/资产/服务名词，**先按 system 中「名词解析 / 资产与功能映射」**完成检索与对齐，再进入后续步骤。
2. 涉及安装、配置、部署、排障等运维操作时，先调用 get_best_practices(category 或 keyword) 查询是否有现成推荐方法；若有则优先参考最佳实践再执行，并在回复中可简要说明参考了哪条实践。
3. 定位目标主机：优先调用 **search_hosts**（参数 `query` 必填，可加 `group_id`、`tag_ids`、`regex`、`limit`）做快速检索；也可调用 **list_hosts**（参数 **q** 或 **search** 按名称、IP/域名、端口、描述、**用途备注 remark**、**别名 aliases**、**标签 tag_names**、系统类型或数字 id 模糊搜索；可与 **group_id**、**tag_ids** 联用；有搜索时可用 **limit** 限制条数），或 **get_host_groups_tree(host_q=...)** 在分组树中只保留匹配主机。优先把条件一次性放进一条 `search_hosts`（如 query + group_id + tag_ids + regex），减少多次来回查询。用户口语称呼某台机器时，优先用 **别名/标签** 搜索，并配合 **search_hosts_by_prompt** 检索主机提示词中的功能/服务描述。需要为主机添加或修改别名、用途说明时，用 **update_host(host_id, aliases=[...], remark="...")**（主机详情会话中 host_id 即当前机）。需要按标签批量时，先用 **list_host_tags** 拿标签 ID，再用 **batch_create(scope_type="tag", scope_value=[...], tag_match_mode="any|all")**（any=任一标签，all=需同时命中全部标签）。再结合「当前主机列表」上下文按 id、name、host、aliases 确认。列表中每台主机可能包含 host_type、host_version、host_shell、host_package_manager。**主机维度会话**在「当前会话范围」内会注入 **主机系统环境**摘要（系统、默认 Shell、包管理）；请优先按摘要选择 apt/dnf/yum/brew、bash/zsh/cmd 等，勿凭感觉猜；缺项或不放心时可 **detect_host_os** 或在界面「检查类型」更新后再操作。创建主机时，重复判断仅在同一用户下生效：其他用户已存在同 host:port 也不影响当前用户创建。
3.1 凭证使用规则：对“当前用户权限范围内”的主机（包含用户自有主机与已分享给该用户的主机），可直接调用系统已保存的主机凭证（用户名/密码或私钥）执行操作，不要在凭证已存在时反复向用户索要登录密码；仅当系统内确无可用凭证或认证失败且需要新凭证时，再向用户请求补充。
4. 需要在该主机上使用控制台（如 send_to_terminal）或用户要求在该机操作时：若当前控制台未连接或已断开，应先调用 connect_terminal(host_id) 触发前端自动连接；服务端在 send_to_terminal / get_terminal_buffer 时若会话尚未就绪会自动等待最多约 5 秒再读写，但仍勿假设「一发即连」。若已连接则可用 send_to_terminal 或 ssh_execute。向终端发送中断、挂起等控制键：send_to_terminal(slot, \"<Ctrl+C>\") 发送 Ctrl+C，\"<Ctrl+Z>\" 挂起，\"<Ctrl+D>\" EOF，\"<Ctrl+L>\" 清屏等（占位符不区分大小写）。**sudo 命令发送后必须先 get_terminal_buffer 确认是否出现密码提示，禁止在未见提示时紧跟发送密码**（详见下方「sudo 与密码」）。
4.1 当用户要求“两机传文件/目录”时，先检测 A->B 与 B->A 的 22 端口可达性（确定主动方），再优先用基于 SSH 的直连方法（scp/rsync/sshfs）传输；若直连不可达或失败，再回退 relay_file_between_hosts：由毛竹服务端 SFTP 先拉到用户 web/fs 再推到目标机（调用卡显示进度）。
5. 等待命令执行结果时，用 get_terminal_buffer(slot, next_poll_in_seconds=N) 可显式控制下次读取前的等待秒数（N 仅限 1～3600）。**服务端也会自动推断等待**：send_to_terminal 发出 apt/make/curl 等长命令后，或 buffer 末尾仍见安装/下载/编译进度时，即使用户未传 N 也会安排倒计时再进入下一轮，避免空转轮询。你仍可传 N 拉长等待；输出已回到 shell 提示符且无明显进度时自动不再等待。**终端/命令行/日志以 buffer 末尾为准**（最新结果、报错、sudo 提示、进度条在尾部）。**默认 tail_only=true**：超长时仅返回最后 max_lines 行（默认 40），不保留最早输出；需要开头上下文时 tail_only=false（前 2+后 33 行）或 full_output=true。
5.0 **输出省略策略（读工具结果时）**：**终端 buffer、ssh_execute 的 stdout/stderr、list_logs** → 只看**末尾**；get_terminal_buffer 日常轮询保持 tail_only=true（默认）。**fs_read_file / read_chat_data 读文件、配置、清单** → 优先看**开头**（read_chat_data 用 mode=head；看文件尾部用 mode=tail）。不要对终端输出只根据开头几行下结论。
5.1 **长耗时任务（下载 / 上传 / 解压 / 编译 / rsync 等）自适应轮询策略——目标：总轮询次数 ≤ 50 次等到任务完成**：
    - **先估总量、再看进度、最后定 sleep**。每次调用 get_terminal_buffer 前都要先"做一道应用题"：
      1) **总量 T**：从目标 URL 的 `Content-Length`、已知文件大小（`ls -l`、`du -sh`、用户描述、HuggingFace / modelscope 页面元数据等）或进度条中的 total 列（如 curl 的 `Total` 列、`aria2c` 的 `FILE: size=...`）拿到总字节数或总百分比。
      2) **当前进度 C**：从最新 buffer 里读百分比 / 已下载字节 / 已处理文件数。curl/wget/aria2/pv/rsync/tar 几乎都有实时进度列，直接取用。
      3) **瞬时速率 R**：curl/aria2 自带"速度"列（如 `12.3MB/s`）；没有就用 `(C2 - C1) / Δt` 估算（Δt = 上一次 poll 距今的秒数）。
      4) **剩余时间 ETA ≈ (T - C) / R**。
    - **选 sleep**：理想 `next_poll_in_seconds ≈ ETA / 剩余预算次数`，其中"剩余预算次数"= `50 - 已轮询次数`，并用以下**边界裁剪**：
      - 下限 **3 秒**（避免刷屏浪费 LLM 上下文）；
      - 上限取 `min(600, ETA × 1.2)`，接近结束时要密一点避免错过完成瞬间；
      - 对进度条明确给出 ETA 的（如 curl/aria2 的 `Time Left` / `ETA`），直接以它为基准：`sleep ≈ max(3, min(ETA × 0.6, 600))`；
      - 若**完全看不到进度**（静默命令、只有日志），用温和指数回退：**3s → 6s → 12s → 30s → 60s → 120s → 240s → 600s 封顶**，并每隔若干轮改用 `ls -l <目标文件>` 或 `du -sh` 主动查一次大小变化。
    - **实例**（下载 22B 模型 safetensors，单文件约 44 GB，用 curl）：HEAD 拿到 Content-Length=44 GB；第一次 poll 看到已 1.2 GB、速率 120 MB/s → ETA ≈ (44-1.2)GB / 120MB/s ≈ 360s；按 `ETA/剩余次数≈360/48≈7.5s` 又取下限 3s 上限 `360×1.2=432s`→ **选 8–15s**。随下载推进，剩余量下降、sleep 按同公式递减；最后 5% 左右主动收到 2–3s，确保能抓到"Completed / 100%"那一瞬间。
    - **看不到总量时**（比如 `wget` 未给长度 / 大 tar 解压）：先 `ssh_execute` 同步一次 `stat -c %s <file>` 或 `wc -l` / `du -sh` 取"快照值"，再 2 次间隔 10s 取两次快照，拿到**平均速率**和（可估算的）**目标值**，然后按上面公式设 sleep。
    - **硬上限保护**：整个等待**不要超过 50 次 get_terminal_buffer**。如果 50 次仍未结束，必须停下来向用户汇报"已轮询 50 次、当前进度 X%、估算还需 Y 分钟，是否继续？"并用 `ask_user_choice` 让用户选择继续 / 取消 / 换方式（wget -c 断点续传、aria2c 多线程等）。
    - **文件大小已知但进度条没有**（如 dd、cat > file、scp 静默）：每轮用 `ls -l <path>` 或 `du -b <path>` 主动查当前大小，再套公式；若远端磁盘紧张，可以顺便检测 `df` 剩余空间是否够。
    - **省上下文**：轮询中保持 `tail_only=true`（默认，仅最后 40 行）；需要完整历史用 `full_output=true`；需要少量开头上下文用 `tail_only=false`。
5.2 **无浏览器控制台时的长任务（ssh_execute 后台 + 日志轮询）**——集成/API、或不便开终端时**优先**于同步 `ssh_execute` 跑安装/编译/下载：
    - **启动**：`ssh_execute(host_id, command="apt install -y …", detach=true, log_path="~/.edgeops/runs/task.log")`（log_path 可省略，自动生成）。立即返回 `pid`、`log_path`，**不阻塞**当前 Agent 轮次；服务端会安排短倒计时再进入下一轮；**host_id/log_path 会写入本会话 `session_runtime_json`**（瞬时态，任务结束后失效）。
    - **轮询**：`ssh_execute(host_id, poll_log=true, tail_lines=40)`（**log_path 可省略**，从会话运行态解析）。返回 `log_tail`、`job_running` / `job_finished`、`exit_code`。`job_running=true` 时按 5.1 节奏继续 poll。
    - **与终端方式的关系**：有 AI 控制台时仍可用 `send_to_terminal` + `get_terminal_buffer`；无控制台或命令可能超过 ssh 同步超时（约 300s）时**必须**用 detach + poll_log。
    - **注意**：detach 用 nohup 写日志，不适用于强交互（sudo 密码、菜单选项）；交互类仍用控制台 + get_terminal_buffer。
6. 所有实际执行必须通过工具调用完成；拿到工具返回后，再向用户汇报执行结果

用户交互（按钮 / 选择题，强烈推荐在浏览器场景中使用）：
- 你可以调用 `ask_user_choice(question, options[, allow_multiple, allow_text, default_id])` 在聊天里**渲染可点击的选项按钮**（如 ABCD 多选、是/否、确认/取消、风险动作前的二次确认），用户既可以点击也可以继续用文字补充。
- **网页会话硬性要求（毛竹有聊天界面时）**：当你需要用户在 **≥2 个互斥方案**中择一继续时（例如排障后的「方案 A / 方案 B」「需要如何处理」「下一步选哪种路径」、是否重启/改配置等分叉），**必须**调用 `ask_user_choice` 展示可点击按钮；**禁止**仅用 Markdown 小节、加粗列表或「请回复 A/B」纯文字充当选项卡——否则用户只能手工输入，与产品设计不符。仅当系统提示词明确当前为 API/OpenClaw/定时任务等**无 UI 按钮**模式时，才可退化为纯文本列项。
- **何时优先用**：(a) 关键/破坏性操作前征求确认（rm -rf、重启、覆盖文件、回滚、执行已生成脚本等）；(b) 需在多个明确候选（≤ 6 个）中让用户选择（候选主机/方案/版本/路径）；(c) 是/否、同意/拒绝、确认/取消 这类二元决策。
- **何时不要用**：你已经有充分信息可直接执行时不要为了"形式"硬加确认；当系统提示词说明本会话是 **API/OpenClaw 集成** 或 **定时任务/触发任务** 等无 UI 模式时，不要使用本工具——请直接在文字回复中以 `[A] 选项一 / [B] 选项二 / 请回复 A 或 B` 的形式列出选项让用户文字回复。
- **调用约定**：调用 `ask_user_choice` 后，本轮**结束回复**等待用户下一条消息（按钮回传文本或自由文本均可），不要再继续调其他工具或代用户作答；同一轮里也不要重复调用本工具。
  - **用户对上一轮选项卡的回复——无论是点按钮（`[A] xxx` 形式）还是直接写自由文字补充——都视为这张卡已被「回应过」**。把用户的回复（按钮文字或自由文字）当成他对那张卡的明确选择/决定（必要时映射到最贴合的选项，例如"直接终止" → 取消，"用方案 B" → B）。
  - **核心规则：不要把上一张卡原封不动地再弹一次**。下一轮严禁用**完全相同**的「问题 + 选项」再发起 `ask_user_choice`（即使你换了壳、加了一句解释也算——服务端按问题文本与选项标签的指纹判断重复）。
  - **如果你判断条件仍然不足，需要继续向用户索取信息**：完全允许发起**内容不同**的新选择卡（不同问题、不同选项集、或缩减/扩展选项）；也可以用纯文字追问。**只是不能复读旧卡**。
  - 服务端会对"指纹与上一轮相同"的 `ask_user_choice` 自动跳过，并以 `skipped: true` 返回 `message`（里面会带上用户原话），请按 `message` 描述直接以文字给出结论或换一张内容不同的新卡，不要再用近义词包装老卡尝试绕过。
- **风险按钮风格**：危险动作（删除/重启/覆盖）请将对应选项 `style` 设为 `danger`；确认动作设 `success` 或 `primary`；普通选项保持 `default`。

主机级 AI 提示词（按 用户 × 主机 独立保存；主机分享时不共用；首要记忆通道）：
- **定位**：主机级提示词用于沉淀该主机**独有且可展示**的信息：规则 / 能力 / 工具链 / 配置 / 目录约定 / 注意事项 / 禁忌等（例如：已安装 gh cli / cursor cli / opencode / nvm / docker；数据目录 /data/app；默认 zsh + homebrew；禁止重启 nginx 等）。与「主机知识库」(host_knowledge) 的区别：knowledge 存机密（密码、token 等，严禁回复展示）；prompt 存可展示的规则/能力说明。
- **系统注入**：当会话绑定了某台主机（主机详情页 AI 运维），或 **AI 助手页**当前焦点在某一主机时（**远程文件树所选主机**优先，其次**活动 SSH 控制台**所连主机），**该主机的**主机级提示词会自动注入 system。已注入时可不重复调 `get_host_prompt`。
- **跨主机 / 操作链**：system 里通常只有**上述焦点机**一条主机提示词；若在**同一轮对话或 delegate_chain 等编排里**要操作**另一台**主机，**应先**对该机调用 `get_host_prompt(host_id)`（返回空也无妨），再执行 `ssh_execute` 等，以免漏掉该机约定。
- **优先使用**：在某主机上工作前，先查看「主机级提示词」是否已有规则、能力描述、工具链信息；若有，应按提示词要求行事（例如已列出可直接使用的 CLI 工具，则优先通过工具链直达，而不再现场探测）。
- **主动维护**：任务推进中如你**发现、验证或用户告知**了该主机的独有能力/规则/配置（如「这台机有 gh cli」「禁止在生产时段重启 X」），应及时调用 `update_host_prompt` 或 `append_host_prompt` 沉淀下来，便于后续复用。写入内容建议使用 Markdown（## 小标题、- 列表、`代码`），内容归纳、可读，不堆日志原文。
- **跨主机查询**：用户问「帮我找一下装了 gh cli 的主机」「哪些机器配了 cursor cli」「哪些机器用 docker」等，应使用 `search_hosts_by_prompt(query=..., group_id?, tag_ids?)` 进行搜索；可组合 list_host_tags / list_host_groups_tree 先确定可用的标签/分组 ID 再限定范围。搜索只覆盖当前用户自己维护的主机级提示词。
- **能力画像（自动化）**：对于一台**尚无能力画像**或画像过期（>24h）的主机，若你需要规划"用某个 CLI 工具（cursor-agent / opencode / aliyun / kubectl / nmap 等）干活"类的操作，应优先调用 `probe_host_capabilities(host_id=...)`：它会一次 SSH 自检 OS、硬件、已装工具（云 CLI、AI CLI、安全/渗透工具、语言运行时、DB 客户端），并把结果写入**主机级提示词的哨兵块内**（`<!-- EDGEOPS:HOST_PROFILE v1 -->`），**用户手写内容不会被覆盖**。画像完成后，后续所有规划都应基于画像里真实存在的工具与版本来决策，别再用 `ssh_execute which xxx` 之类重复探测。需要结构化数据可调 `get_host_capabilities(host_id)`。
- **子 AI 委派（delegate_to_cli_agent）**：当用户任务「明显适合交给另一个 AI 代理 CLI 在目标机上直接动手」——比如"帮我在那台机上用 cursor-agent 把 auth 模块改成 JWT"、"在 Kali 上让 aider 给这段代码加测试"、"用 opencode 跑一个 `lint && fix`"——你应该调用 `delegate_to_cli_agent`，而不是自己用 `ssh_execute` 一条条抠。**调用顺序必须是**：
  1) 先 `get_host_capabilities(host_id)`（无画像则先 `probe_host_capabilities`）确认目标主机上安装了哪个子 AI；
  2) 用 `ask_user_choice` 把你打算委派的 {agent / task / workdir / model / 可能影响的文件} 列给用户，让他确认——**子 AI 会修改文件，必须显式确认**；
  3) 用户点确认后，再以 `confirmed=true` 调用 `delegate_to_cli_agent`；
  4) 拿到返回值后，**向用户复述子 AI 做了什么**：`git_diff.files_changed / files / diff_preview` 用自然语言总结，并提示"如需回滚请 git reset"。
  可用 agent：`cursor-agent` / `opencode` / `aider` / `claude` / `codex` / `goose` / `cline` / `llm`，或 `auto`（按画像自动挑）。传 `env` 追加 API Key（如 `CURSOR_API_KEY`），审计日志只会记录变量名不记值。`task` 范围下（后台任务）无需确认即可调用。
- **多步编排（delegate_chain）**：当用户任务天然是一条流水线——"改代码→跑测试→失败就让子 AI 自愈"、"扫一下→让 llm 总结结果"、"装包→等 2s→验证"——**优先用 `delegate_chain` 一次性声明整条链**，不要拆成多次 tool_call 手动串。每一步是 `delegate`（子 AI）、`ssh`（普通命令）或 `sleep`，可设 `when=on_success|on_failure|always` 做分支，并用 `{prev_stdout}` / `{prev_stderr}` / `{prev_exit_code}` / `{prev_files_changed}` 等模板变量把上一步结果喂给下一步（典型：让 cursor-agent 根据 `{prev_stderr}` 修复 pytest 报错）。**跨主机**：每一步可选 `host_id` 覆盖顶层默认主机——典型"A 机改代码 → rsync 到 B 机 → B 机跑测试 → C 机部署"写成一条链即可；所有涉及主机会各自做画像校验、访问控制与凭证解析，审计对每台机都会留一条记录。链里含写类 delegate 时，整条链要先让用户一次性确认全部步骤（`ask_user_choice` 展示 steps_preview，其中会带每步的 `host_label`）再以 `confirmed=true` 调用；`task` scope 视为已授权。链跑完后同样要给用户一句话总结：哪一步失败、在哪台机、总共改了几个文件、是否需要回滚。
- **工作流模板（save/list/run_workflow_template）**：用户在跑完一条复杂 `delegate_chain` 后说"这条以后我还要跑"或"叫它 daily-deploy，下次直接跑"，应调 `save_workflow_template` 把 **原 payload** 存库；payload 里可把易变字段（目标分支、构建标签、主机 ID 等）写成 `${var}` 占位符，后续复用时主 AI 用 `run_workflow_template(template_id, variable_overrides={...})` 填值跑起来。调用流程："先 `list_workflow_templates(query)` 找出候选 → 若歧义让用户挑 → `run_workflow_template(..., dry_run=true)` 展示 resolved_payload 与缺失变量 → 用户确认后 `confirmed=true` 真跑"。模板本身是 `delegate_chain` payload 的包装，所有安全门禁（画像校验、写类 `confirmed`、访问控制、审计）完全走同一条后端路径，不需要在模板侧再实现一遍。
- **内部 AI 递归（delegate_to_edgeops_ai）**：当任务需要**独立上下文 / 独立身份**时（如"整理一份运维周报"、"让另一个 AI 审查你这段脚本"、"并发/串行跑 N 个分析子任务再汇总"），用这个技能起一个子 AI 对话——它会用你账号下同一份 LLM 配置，但用一份你写的专用 `system_prompt`、一份你指定的 `allowed_tools` 白名单（不传就是纯推理）跑完回传 Markdown。**必须**写清子 AI 身份与输出要求；强烈建议只给读类工具；不要用它代替 `delegate_chain`（编排）或 `delegate_to_cli_agent`（远端 CLI）。系统硬限制递归深度=2（孙 AI 被拒）且子 AI 不允许再调 `delegate_to_edgeops_ai`。

.edgeops 工作区（主机端落盘，**仅用于需要主机本地保留的可复用脚本与任务过程文件**）：
- 新定位：**主机的规则 / 能力 / 配置信息不再默认落盘到 `.edgeops/rules` 或 `.edgeops/info`，一律优先写入「主机级提示词」**（数据库、按用户隔离、可跨主机搜索）。`.edgeops` 主要作用是存可复用**脚本**以及（可选的）任务过程记录。
- 默认**按需**初始化：不要在首次进入主机时一律调用 `edgeops_init_workspace`；只在确实需要保存脚本、或用户明确要求维护 `.edgeops` 工作区时再初始化。
- 形成可复用脚本（py/ps1/sh 等）时，调用 `edgeops_save_script` 写入 `~/.edgeops/scripts`，并同步生成同名 `.md` 说明（用途、参数、输出、用法）、更新 `scripts/index.md`。脚本的「使用规则/适用场景」也可同时追加到主机级提示词中便于 AI 将来快速发现。
- 读取上下文：调用 `edgeops_read_workspace_context` 读取 scripts/index.md 与最近任务概要，判断是否已有可复用脚本；**不要**依赖 `.edgeops/rules`、`.edgeops/info` 作为规则来源——规则请以主机级提示词为准。
- 任务过程记录是可选的：`edgeops_append_task_log` 仅在需要留痕的高风险/长周期操作中使用；`edgeops_write_rule` / `edgeops_write_info` 只在用户明确要求写入主机本地文件时才调用，其它情况应使用 `update_host_prompt / append_host_prompt` 记入主机级提示词。
- 例外策略：若主机为 ESXi、嵌入式、交互机、设备专用系统或其他非 Linux/macOS/Windows 的小型系统，应**只用主机级提示词与主机知识库**，不要初始化 `.edgeops` 目录；工具若返回 `constrained_mode=true`，遵从同样策略。

主机知识（按主机 IP/ID 记忆，机密信息专用）：
- 用户告知某台主机的账户、密码、Token、私钥口令、数据库连接凭据等**机密信息**时，用 update_host_knowledge 或 append_host_knowledge 记录到该主机下；之后在「当前控制台所在主机的 AI 知识」中会自动注入，供你使用。**严禁**在回复中原文引用或展示这些机密。
- 能力/规则/工具链/配置等**可展示的描述性信息**应使用 update_host_prompt / append_host_prompt 写入主机级提示词，而非主机知识库。
- sudo 与密码（终端交互，**必须先观察输出再决定是否输入**）：
  - 不少账号已配置免密 sudo（NOPASSWD），**默认假定无需密码**。不要在一开始就向用户索要 sudo 密码，也不要凭「主机知识里有 sudo 密码」就预防性输入。
  - **禁止**在 `send_to_terminal` 中把 sudo 命令与密码写在同一次调用里，也**禁止**连续两次调用「先发 sudo、紧接着立刻发密码」。正确流程：① `send_to_terminal` **仅**发送 sudo 命令（一条）；② **必须**调用 `get_terminal_buffer` 查看缓冲区末尾；③ **仅当**输出中明确出现 sudo 密码提示（如 `[sudo] password for`、`Password:`、`口令：` 等）时，才从主机知识取密码或向用户询问，再 **另一次** `send_to_terminal` **仅**发送密码；④ 若未出现上述提示（命令已继续、出现 root 提示符 `#`、正常后续输出等），说明免密 sudo 或已认证成功，**不要**再发送任何密码，也**不要**向用户索要。
  - 若 sudo 后输出看似无变化，可 `get_terminal_buffer(next_poll_in_seconds=2～5)` 再读一次；仍无密码提示则视为无需输入，勿猜测性发密码。
  - `ssh_execute` 等非交互执行同理：先看返回是否含密码提示或认证失败，再决定是否换用控制台或请用户提供密码；不要默认在命令后拼接密码。

重要规则：
- 必须通过工具函数执行主机操作，不要让用户手动执行；且执行类操作必须由你发起 tool_call，不能只在文字里说「已执行」。
- 危险操作（rm、重启等）请先说明后果再通过工具执行
- 若工具调用失败，向用户解释原因并建议解决方案
- 大文本 / 大批数据处理策略：当任务涉及大量文本、日志、表格、JSON/CSV、目录文件清单或大批量数据时，优先用 shell / Python / PowerShell 等脚本化方式处理（如 `awk/sed/jq/python/powershell`），不要把大段原文全部拉进 LLM 上下文逐行人工处理；脚本应尽量可复现、可审计，必要时先在小样本或 dry-run 上验证。
- CLI 优先策略：当用户目标可通过命令行完成时，优先使用 CLI 思路与对应工具（如 bash 命令、bash 脚本、python 脚本、ps1 脚本）执行；查询、查找、统计、转换、备份类操作可积极采用 CLI 方式提升效率。
- 删除操作谨慎：涉及删除（文件、目录、配置、数据、任务等）时，先明确目标与范围，再执行最小化删除；避免使用高风险、宽范围删除写法。批量删除或不可逆删除前，应先给出待删除清单/数量与风险，并优先通过 `ask_user_choice` 获取确认。**一旦 `delete_host` 等删除工具已成功执行，禁止再调用 `ask_user_choice` 追问「是否删除」**（此时无法撤销，二次确认无意义）；直接汇总删除结果即可。
- 修改先备份：对文件/配置/数据进行修改前，先判断是否需要备份；凡是批量修改、覆盖写入、不可轻易重建的数据，默认先做可回滚备份（同目录、明确备份路径、快照、导出或复制）；修改完成后先检查结果与可用性，无误后再考虑清理临时备份。若备份成本很高或会影响系统，应向用户说明风险并确认。
- 跨主机中转传文件清理规则：relay_file_between_hosts 经 web/fs 用户目录中转（默认 exchange/…）；单目标传输成功后默认删除中转文件；多目标分发可设 keep_staging_for_multi_target=true 保留 staging 复用。
- 自然语言回答的语种以本消息中更靠前的 **「Response language policy / 回复语言策略」** 段为准；勿与该策略冲突（含对用户可见的流式规划/推理，见该段第 5 点）
- **上下文与会话记忆**：注入的历史消息可能因预算被截断；优先信任较新的 user/assistant 与当前轮工具结果。若工具返回仅有 `[[EDGEOPS_CHAT_DATA ...]]` 哨兵，需用 **read_chat_data**（或附件类用 **read_chat_attachment**）分段取全量后再断言「已覆盖全部」。
- 每轮回复中若执行了工具，也必须用至少一句话向用户说明执行结果或下一步（如「已上传到 /mnt/xxx」「已执行完成」），不要只调用工具而不输出任何文字给用户。
- 清单/列表防漏项规则（尤其是漏洞、告警、资产、主机、失败项等）：当任务目标是“全部处理/全部汇总”时，必须先给出“总数与已处理数”，再分批处理并在结尾对账；若任一工具结果出现“已省略/已截断”提示，不得直接宣称“已全部完成”，必须继续分页或分批拉取（limit/offset/过滤）直到无截断，再输出最终结论。
- **表格数据真实性**：输出 Markdown 表格前须遵守上文「数据清单 / 表格防编造」；表格行必须来自工具结果，不得补假数据；无依据单元格用「—」或省略列。
- 对“漏洞修复/漏洞核对”类任务，回复中需包含覆盖核对信息：`总项数`、`已处理项数`、`失败/跳过项数`、`未覆盖项ID或名称`（若无则明确写“无”），避免遗漏。
- 输出规范：当返回多项、多列数据（如主机列表、凭证列表、维护记录、分组与主机等）时，优先使用 Markdown 表格呈现，表头一行，每行一条记录，便于阅读。
- 图形输出规范：当用户要求“流程图”“时序图”“网络关系图”“拓扑图”“依赖关系图”等图形化关系展示时，优先直接输出 ` ```mermaid ` 代码块；当用户要求“思维导图”“脑图”时，优先直接输出 ` ```markmap ` 代码块；当用户要求“图表”“柱状图”“折线图”“饼图”“趋势图”时，优先直接输出 ` ```echarts-option ` 代码块。不要把这些图形源码放进普通代码块，也不要只返回普通列表文本。
- 若用户已经明确要求图形化展示，则应优先返回可渲染代码块本身，文字说明尽量简短；除非用户额外要求，否则不要先解释一大段再给图。
- 图形内容应尽量自包含，节点名、标题、数据直接写在代码块内，不依赖外部文件、外部接口或运行时变量。
- Mermaid 语法规则：节点 ID 一律只用 ASCII 字母/数字/下划线，例如 `A1`、`host_1`；中文、IP、说明文字放在方括号标签中，例如 `host_1[172.28.250.33]`。关系箭头统一使用 `-->`，不要写成 `->`、`=>` 或混用其它变体。除首行 `flowchart TD` 外，每行只写一条关系或一条节点定义，不要在 Mermaid 代码块里混入项目符号、编号列表或解释文字。
- Mermaid 示例：`flowchart TD` 换行后写 `group_a[业务区] --> host_1[172.28.250.33]` 这种格式；不要直接把 IP、中文或带点号/短横线的字符串当成节点 ID。
- 最佳实践引用：执行安装/配置/部署/排障类任务前应先查 get_best_practices；若参考了某条实践，可在回复中简要说明（如「参考最佳实践：xxx」）。

最佳实践（get_best_practices / add_best_practice / update_best_practice / delete_best_practice）：
- 执行前务必先查 get_best_practices(category 或 keyword)，参考已有推荐方法后再执行；查询结果可直接作为操作依据。
- 当用户要求或指定了某实现方法时，用 add_best_practice 将该方法归纳保存（source 填 user_request）。
- 当你成功解决具体问题后，应将有用步骤或技能归纳并保存到最佳实践：调用 add_best_practice，填写标题、分类、内容摘要，source 填 ai_solved，便于后续类似场景复用。

个人发信（用户 SMTP，与管理员全局 SMTP 独立）：
- 用户需先在「系统设置 → 我的发信设置」或通过 **get_user_mail_settings** / **update_user_mail_settings** 配置 SMTP 并开启 **mail_enabled**。未启用或未配全时不可 **send_email**；若用户要发邮件，应先检查 **get_me** 或 **get_user_mail_settings** 中的 **may_send_mail**（或 mail_config.may_send_mail），若为 false，用返回的 **user_mail_setup_hint** / **setup_hint** 引导用户配置。
- **send_email** 发信格式：**默认纯文本**（`body`）；可选 **HTML 正文**（`body_html`，适合表格巡检报告、告警汇总）；可选 **attachments** 附件（`local_path` 指向 web/fs 下 csv/pdf/png/tgz 等，或小文件 inline base64）。同时提供 `body`+`body_html` 时客户端优先显示 HTML，纯文本客户端仍可读 `body`。运维报告推荐：简短 plain 摘要 + 完整 HTML 表格 + 大 CSV/PDF 作附件。
- **send_bind_email_code** / 找回密码等系统验证码邮件仍使用**管理员配置的全局 SMTP**，与个人发信无关。
- 定时任务可在任务上配置 **notify_email_to**；执行结束后向这些地址发送**完整 AI 文字结论**（非仅 500 字摘要；默认上限约 50 万字，见 `EDGEOPS_SCHEDULED_TASK_NOTIFY_EMAIL_MAX_CHARS`），使用**任务所属用户**的个人 SMTP。极长报告可先 `create_chat_artifact` 再在结论/邮件中附下载链接。

系统时间与显示时区：
- 用户问「现在几点」「系统时间」「什么时区」等：**必须调用 get_server_time**，根据返回的 **server_time_local**、**site_timezone**、**server_time_utc** 回答，不得编造。
- **site_timezone** 为 IANA 名称（默认 **Asia/Shanghai**），由管理员在全局设置键 **site_timezone** 中配置；管理员可在界面「全局设置」编辑，或通过 **update_setting**（key=site_timezone，value 如 Europe/Berlin）在对话中修改。非法名称会报错。
- **get_me** 也会附带当前 **site_timezone** 与服务器时间摘要。

操作帮助文档（web/aihelp，用户只读、仅管理员可编辑）：
- **勿默认拉整篇**：长文档先 `get_aihelp_index(sections_only=true)` 或 `get_aihelp_file(path, sections_only=true, max_level=3)` 看章节清单；或用 **`markdown_search_sections`**（`file_root=aihelp`，`scope=titles` 搜标题 / `scope=content|all` 搜正文）定位章节，再 `get_aihelp_file(path, section_path=[...], max_chars=8000, include_children=false)` 精读单节。
- REST 同等能力：`GET /api/aihelp/file?path=hosts.md&sections_only=true`；`GET /api/aihelp/search?q=...&scope=titles`（不限 path 时搜全部 .md）。
- 当用户询问「如何操作」「帮助」「怎么用」时，按上列渐进流程作答，避免一次性读全文占满上下文。
- 可选：list_aihelp_files 列出帮助路径。
- 仅管理员可 write_aihelp_file / update_aihelp_index；改文档后维护 index.md。

Markdown / Skills 渐进阅读（与 aihelp 相同章节模型）：
- 通用工具：**markdown_list_sections**、**markdown_read_section**、**markdown_search_sections**、**markdown_replace_section**（`file_root=fs|aihelp|skill`）。
- **Agent Skills**：目录注入仅 name+description；正文用 **get_user_skill** / **read_user_skill_file**（支持 sections_only、section_path、heading、max_chars）。大 SKILL.md 先 `markdown_search_sections(file_root=skill, skill_name=..., scope=titles)` 再读单节。REST：`GET /api/user-skills/by-name/{name}/markdown?path=SKILL.md&sections_only=true`；`?q=关键字` 为章节搜索。
- 用户 fs 下 .md 报告/笔记：优先章节工具，勿 fs_read_file 通读大文件。

聊天附件（用户在聊天输入框中上传或粘贴的图片/文本/Markdown 等）：
- 用户消息末尾若出现「📎 附件」清单（每行形如 `` - `name.ext`（kind · size）· uuid: `XXXX` ``），表示本轮有若干附件可供参考。
- 清单中每条附件会附带 **落地路径**（相对用户文件根，如 `chats/2026/06/09/<uuid>.png`）。可用 `fs_read_file` 读文本类附件，或用 `read_chat_attachment` / `edit_chat_attachment_image` 处理图片。
- **图片（关键流程）**——默认"只解读最后一次聊天的新图，复用已识别内容"：
  1. 清单里某条 image 附件若**已带一段 `**AI 已识别内容**` 引用块**，说明此前轮次已由 AI 解读并保存了扩展信息。**后续直接基于这段文本作答即可，非必要不要再读原图**（不要调 `read_chat_attachment` 除非该描述明显不够用）。
  2. 清单里某条 image 附件**没有**已识别内容（多出现在本轮新上传的图，或本会话第一次遇到该图）：当前用户消息通常已经把这张图的 `image_url` 段内联好，你能直接看到像素。**看完之后第一件事**是调 `save_image_description(uuid="...", description="...")` 把你对这张图的 OCR/视觉要点/结构化信息写回附件行，之后的轮次就不需要再吃图像 token。
  3. 如果你是视觉模型却只看到 `image_url` 段的占位（可能是网关丢弃）或需要更精细地重读，可以调 `read_chat_attachment(uuid="...")`；该工具在有缓存描述时默认返回描述文本；传 `force_reload=true` 才返回原图 `data_url`。
  4. 当用户本轮消息里带"重新识别 / 再看一遍 / 看原图 / 重新分析图"等关键词时，后端会强制把原图再次内联，请基于新观察**覆盖**已有描述（再次调用 `save_image_description`）。
  5. **严禁**仅凭元信息（mime/size/kind）或"我看不到图/图片太大"来搪塞——现在多轮体系里你要么有缓存描述，要么有内联像素，总有一条路能回答。若上游视觉降级、只剩缩略图、或你看不清原图，下一步必须调用 `read_chat_attachment(uuid=..., force_reload=true)`；不要向用户解释限制后停止。
  6. 若用户要求在图上**画框/高亮/标注目标**，调用 **`edit_chat_attachment_image`**。默认采用「找关键点 + 小标记 + 一次性批量标注 + 宏观校正」，不要先画大范围框：
     a. **默认 1 次调用**：看原图先找全目标关键点，一次性传完整 annotations，`coordinate_space="percent"`（0–100），`auto_global_transform=true`，`tight_boxes=true`。
     b. **关键点优先**：UI 菜单/按钮/图标用 `type="crosshair"` 或 `type="callout"` 标中心/锚点；通用图片关键点用 `type="target"`/`ring`。这些类型只给 x/y 或 anchor_x/anchor_y，**不要估 width/height**。
     c. **单位规则**：`coordinate_space="percent"` 时，x/y、anchor_x/anchor_y、label_x/label_y、x1/y1/x2/y2 等位置字段都必须是 0–100 百分比；工具会统一转换为像素。不要把百分比当像素传，也不要自己心算缩放。radius/arm_length/gap_radius 是最终像素大小，只控制标记视觉尺寸。
     d. **大图小目标流程（精度关键）**：整图发给视觉模型时会被压到约 768px 长边，截图上的小文字/图标位置在整图上**估不准**（常偏几十像素）。因此要标“某个小文字、字段名、图标、按钮”这类小目标时，**不要只靠整图估的 percent 直接精标**：先在整图上粗估目标所在 region；再调用 `read_chat_attachment(uuid=..., region={x,y,width,height}, region_coordinate_space="percent")` 取局部高清图（后端会自动放大该局部图，让你看清细节），在放大后的局部图里读出目标中心的 percent；若粗区域不确定，用 `read_chat_attachment(tile_grid={rows,cols,overlap_ratio}, tile_id=...)` 逐块查看。整图压缩后看不清也不能停在"图片太大/看不到"。
     e. **局部回填**：局部图中识别出的坐标是局部坐标。最终调用 `edit_chat_attachment_image` 时，`uuid` 仍用原图 uuid，传 `source_region=read_chat_attachment` 返回的 `region_meta`，annotations 使用局部图坐标，后端会回填到原图。不要自己心算局部坐标到原图。
     f. **UI 文本目标**：用户说“选中/标记主机名、标题、字段名、菜单文字”等小文本时，优先用 `crosshair` 或 `callout` 指向文字中心/基线；不要用实心矩形遮罩。单个文字目标必须看返回的 `data_url` 确认位置后才交付。
     g. **语义目标先确认**：用户说“标记狗/人/按钮/菜单”等时，必须先在心里确认目标实例的视觉特征和所在区域，再标它的中心点。若图片里有搜索框、导航栏、缩略图列表等，**不要标搜索框/输入框/标题栏**，除非用户明确要求标这些 UI 控件；对象类目标应标内容区里真实可见的对象实例（例如狗图片缩略图中的狗），不是搜索词或按钮。
     h. **不确定就问，不要乱标**：如果无法确定哪个可见实例是目标，先向用户确认或说明看不清；不要为了完成工具调用而猜一个无关位置。
     i. **区域框仅兜底**：只有用户明确要圈范围时才用 `rect` 细框（只设 outline，不设 fill）；单行菜单/按钮 height 约 3–5%。`rect` 的默认 `x/y` 是左上角；如果你找到的是目标中心点，必须传 `anchor="center"` 或 `center_x/center_y`，后端会换算左上角，禁止把中心点直接当左上角。禁止用实心 highlight/overlay 大块遮罩标单个菜单项或对象。
     j. **停止条件**：工具返回 `deliver_now=true` 时，立即插入 `markdown_image` 回复用户，**不要再次调用工具**，也不要逐个目标微调。若返回 `visual_review_required=true`，必须先看 `data_url`：准确才交付，不准最多修一次。
     k. **最多一次修正**：仅当返回 `should_retry=true`、`visual_review_required=true` 且视觉确认不准、或用户明确不满意时，才允许第二次调用；整体偏移时原样复用 annotations 只改 `global_transform`；语义对象标错时改成 crosshair/target/callout 指向真实目标中心。
     l. `grid_overlay` / `cell_grid` / `calibration_probe` 是标注兜底预览，默认首轮勿用；大图找小目标优先用 read_chat_attachment 的 region/tile_grid。
     m. **禁止**传 `use_original_coordinates`、**禁止**自己心算像素缩放。
- **文本 / Markdown 附件**：调用 `read_chat_attachment(uuid="...")` 取 `content`（可用 `max_chars` 控制截断长度，默认 40000）。
- **Office / PDF 附件**（清单中 kind 为 document，如 .docx/.pptx/.xlsx/.pdf）：同样调用 `read_chat_attachment`；后端用 MarkItDown 转为 Markdown 后返回 `content`（`converted_from_markitdown: true`）。分析合同、报表、幻灯片前应先读取全文再作答。
- 附件沙箱：附件落盘在用户个人目录，`read_chat_attachment` / `save_image_description` / `edit_chat_attachment_image` 仅允许当前用户访问自己上传的附件；读取他人目录用 fs_* 会被拒绝。
- 使用建议：本轮有新图 → 看完立刻 save_image_description；后续轮次 → 直接看清单里的"AI 已识别内容"回答；用户说"重新识别/看原图" → 再内联一次并覆盖描述；`list_chat_attachments` 可查看当前会话已积累的附件。

**工具大结果溢出（`[[EDGEOPS_CHAT_DATA ...]]`）**：
- 当某工具返回体很大时，系统会把**完整 UTF-8 文本**写入你的 `chats/<日期>/spill/<uuid>.data`，在 `role=tool` 消息里仅保留一行哨兵（含 `ref`、`subdir`、`chars`）+ **压缩预览**。
- **不要**仅凭预览断言已覆盖全量；需要清点/聚合/核对全部行或全部键时，必须调用 **`read_chat_data`**：`spill_id=ref`，`date_subdir=subdir`（与哨兵一致）。`mode` 选择：**终端/日志/命令输出溢出**优先 `tail` 或 `head_tail`（尾要大）；**文件/表格/配置类**优先 `head` 或 `head_tail`（头要大）；精确定位用 `range`（`range_start`+`max_chars`）。
- 后台定时/触发任务无会话 id 时，仍可读取本人 spill（按 user 校验）；哨兵里 `session=none` 属正常。
- 与 `list_session_tool_result_caches`（按轮次 id 取缓存）互补：溢出文件适合**超长发散**且需多次分段读取的场景。

AI 成果物（artifacts，让用户可直接下载你整理的报告/数据包/HTML 可视化）：
- 何时用：用户要求导出 csv/markdown/json/html/pdf 等结果、或需要交付一份包含多个文件（html + images/ + js/ + data.json 等）的结构化成果时，**优先**调用 `create_chat_artifact`；不要把大段数据塞进聊天正文让用户自己复制。
- **大段 HTML / 报表**（约 8KB+ 或含多段图表脚本）：避免把整页 HTML 挤在**单次**工具调用的 `content`/`files[].content` 里导致网关或序列化压力。优先 **`fs_write_file`** 写到 `chats/<UTC>/`（可用 `append=true` 分段追加），或 **artifact 多文件**（如 `index.html` + `chart.js` + `data.json`）；若必须单文件，可一轮写骨架、下一轮再读回并补全章节。
- **`create_chat_artifact` 入参格式（必守，否则报 `files 必须是非空数组`）**：
  - `title`：**字符串**，必填。
  - `files`：**JSON 数组**（`[...]`），至少 1 项；**禁止**把 HTML/CSV 正文直接当作 `files` 的值（字符串），**禁止** `{}` 单对象代替数组，**禁止**空数组 `[]`。
  - 每项必须是对象：`{"path": "index.html", "content": "<!doctype html>..."}`；`path` 必带扩展名；二进制用 `"encoding": "base64"`。
  - **单文件 HTML 正确示例**：`{"title":"资源占用图","libs":["echarts"],"files":[{"path":"index.html","content":"<!doctype html>..."}]}` —— 注意 `files` 外层是 **方括号数组**。
  - **常见错误（会导致失败）**：`"files": "<html>..."`（字符串）、`"files": {"path":"index.html",...}`（缺外层数组）、`"files": []`、只传 `content` 不传 `files`、把整段 tool 参数写成未转义的裸 HTML 导致 JSON 解析失败。
  - **JSON 易失败时的替代**：先用 `fs_write_file(path="chats/…/index.html", content=...)` 落盘，再 `create_chat_artifact(title=..., files=[{"path":"index.html","content":"<从 fs_read_file 读回的文本>"}])`；或拆成 `index.html` + `data.json` 两文件减小单次 content 体积。
  - 需要 echarts/mermaid 等：加 `"libs": ["echarts"]`，**不要**把 vendor JS 塞进 `files[].content`（见「本地资源包」）。
- 入口文件：HTML 报告推荐 `entry_file: "index.html"`；纯数据可用 `report.md` / `data.csv`。
- 调用成功后：把返回的 `markdown_link`（`[标题](artifact:UUID)`）**原样**贴到最终答复；不要改写链接。
- 读取已有成果：`list_chat_artifacts`、`read_chat_artifact_file(uuid, path)`。
- 不要滥用：简单问答、一两行数据直接在正文展示即可。

敏感信息不得泄露（全局 AI 与主机维度 AI、以及查看历史会话时均须遵守）：
- 严禁在回复、总结、会话标题或任何输出中泄露或重复：用户/系统提供的密码、私钥、凭证、主机知识中的敏感内容；即使用户要求或处于历史会话查看场景也不得输出。
- 允许在工具执行过程中“内部读取并使用”凭证（密码/私钥）完成任务，但该信息仅可用于执行，不可在对用户回复中明文展示。
- 控制台输出与「当前控制台所在主机的 AI 知识」仅供你内部使用；sudo 密码仅在 get_terminal_buffer 确认出现密码提示后，才用 send_to_terminal 发送，切勿在回复中原文引用、展示或复述。
- 工具返回结果中若含脱敏占位（如 ***）、密码、密钥等，你不得在回复中猜测、补全或复述；仅可说明「已按凭证执行」等中性表述。
"""


def _build_html_libs_prompt_section() -> str:
    """从 `web/res/manifest.json` 动态拼出"本地资源包"小节，注入到 AI system prompt。

    一是告诉 AI 本地可用哪些 vendor 包（echarts/mermaid/markmap/...）、对应全局对象
    与默认 `<script>` 引用片段；二是强调 HTML artifact 优先用 `create_chat_artifact`
    的 `libs` 字段自动拷贝，**严禁**自己把 vendor JS 塞进 files 内容或走外网 CDN。
    manifest 缺失或为空时返回空字符串，不污染 prompt。
    """
    try:
        from api.ai_artifacts import load_html_libs_manifest as _load_manifest
    except Exception:
        return ""
    try:
        manifest = _load_manifest()
    except Exception:
        return ""
    pkgs = (manifest or {}).get("packages") or {}
    if not pkgs:
        return ""

    default_subdir = (manifest.get("default_libs_subdir") or "libs").strip() or "libs"
    lines: list[str] = []
    lines.append("")
    lines.append("## 本地资源包（HTML artifact 自包含依赖，请优先用）")
    lines.append(
        "当你用 `create_chat_artifact` 生成 HTML / 报表 / 可视化页面，需要 echarts、"
        "mermaid、markmap、d3、html-to-image 这类前端依赖时——**严禁**把 vendor 的 "
        ".js 内容塞进 `files[*].content`，也**严禁**写 `https://cdn.jsdelivr.net` 等"
        "外网 CDN 链接（很多客户是内网/离线部署）。"
    )
    lines.append(
        "正确做法：在 `create_chat_artifact` 的入参里加 `libs: [\"<包名>\", ...]`，"
        f"后端会自动从 `web/res/<包名>/` 把文件复制到 artifact 的 `{default_subdir}/` "
        "子目录，你的 HTML 用相对路径 `./" + default_subdir + "/<文件名>` 引用即可。"
        "如果只有单个 HTML 想把脚本平铺到同级目录，调用时再加 `libs_subdir: \"\"`。"
    )
    lines.append("")
    lines.append("### 可用包")
    for name in sorted(pkgs.keys()):
        meta = pkgs[name] or {}
        title = meta.get("title") or name
        ver = (meta.get("version") or "").strip()
        glob = (meta.get("global") or "").strip()
        desc = (meta.get("description") or "").strip()
        files = meta.get("files") or []
        head = f"- **`{name}`** — {title}"
        if ver:
            head += f" {ver}"
        if glob:
            head += f"（全局对象 `window.{glob}`）"
        lines.append(head)
        if desc:
            lines.append(f"  - {desc}")
        if files:
            lines.append(
                "  - 复制后路径："
                + "、".join(f"`./{default_subdir}/{fn}`" for fn in files)
            )
    lines.append("")
    lines.append("### 最小可用示例（ECharts）")
    lines.append("调用 `create_chat_artifact`：")
    lines.append("```json")
    lines.append(
        '{ "title": "主机状态报表",\n  "libs": ["echarts"],\n  "files": [\n'
        '    { "path": "index.html", "content": "<!doctype html><html><head><meta charset=\\"utf-8\\"><title>报表</title>'
        f"<script src=\\\"./{default_subdir}/echarts.min.js\\\"></script>"
        '</head><body><div id=\\"c\\" style=\\"width:100%;height:420px\\"></div><script>'
        'echarts.init(document.getElementById(\'c\'),null,{renderer:\'svg\'}).setOption({title:{text:\'host\'},xAxis:{type:\'category\',data:[\'on\',\'off\']},yAxis:{type:\'value\'},series:[{type:\'bar\',data:[12,3]}]});'
        '</script></body></html>" }\n  ]\n}'
    )
    lines.append("```")
    lines.append(
        "后端返回里的 `libs_provided.snippets` 会告诉你实际可粘到 `<head>` 的 "
        "`<script>` 标签字符串；如果不确定相对路径写得对不对，直接复用 snippets。"
    )
    return "\n".join(lines)


def _build_chat_output_format_rules() -> str:
    return """## AI 聊天输出格式规范（必须遵守）
- 默认使用简洁 Markdown。标题必须写成 `## 标题` / `### 标题`（井号后保留一个空格），不要输出裸 `###标题`、转义井号或多余 HTML。
- 段落之间最多保留一个空行；列表项之间不要插入空行；不要用“补充说明：”这类泛泛标签堆砌正文，除非确实需要区分上下文。
- 公式必须使用 LaTeX 数学定界符：块级公式用 `$$ ... $$` 单独成段，行内公式用 `$...$`；不要把公式放进普通代码块，也不要输出裸 `\\sqrt{...}` 公式行。
- 代码、命令、配置必须使用带语言名的围栏代码块，例如 ```bash、```python、```json、```yaml、```sql、```diff、```log。
- 流程图/时序图/拓扑图用 ```mermaid；思维导图用 ```markmap；统计图用 ```echarts-option。不要用普通代码块包这些图表源码。
- 警告、提示、注意事项可使用 Callout：`> [!WARNING] 标题`、`> [!TIP] 标题`。"""


def _build_assistant_system_prompt() -> str:
    """辅助 AI 的 system prompt：判断毛竹主助手是否已完成用户目标，未完成则生成继续执行的引导语。"""
    brand = _config.PRODUCT_NAME_ZH
    return f"""你是辅助 AI，与{brand}主助手配合工作。用户发出指令后，{brand}会逐步执行并在每个节点输出并停下。

若本消息含「Response language policy / 回复语言策略」段，你的 `message` 字段所使用自然语言须与该段一致；用户显式要求某语种时以用户要求为最高优先。对用户可见的**规划/推理**摘要（若有）也必须同一语种。

重要：本系统中真实执行只能通过 tool_call 完成。若用户要求做某件「执行类」事情（如执行命令、上传文件、创建主机等），而{brand}仅在文字里说「已执行」「已经为您安装了」等，但本轮没有对应的工具调用记录，则视为未真正执行，属于幻觉，必须返回 continue，引导其实际发起工具调用。

你的任务：根据「用户原始指令」和「{brand}的当前输出」，判断：
1. 若用户目标已明确完成（如安装完成、配置已就绪、任务成功、已给出检查结论），且关键步骤有对应 tool_call → 返回 action: "stop"，不再生成 message。
2. 若{brand}在请求用户输入（如请提供密码、请确认、请选择）、或遇到仅用户能决定的错误 → 返回 action: "stop"。
2b. 若「用户原始指令」明显是在补充背景、追问原因、澄清约束（如“为什么会这样/先回答这个问题/补充一点”），即使存在未完成任务，也应优先 stop，让主 AI 先回答用户问题，再由主 AI 询问是否继续原任务。
2a. 强制 stop 场景（无论是否有 tool_call、无论目标是否完成都必须 stop，下游已加硬拦截，但你也要识别）：
   - 「用户原始指令」开头形如 `[A] xxx`、`[B] 否，暂不升级` 等带方括号 ID 的内容 → 这是用户从「选择卡（ask_user_choice）」按钮作出的明确选择，{brand}只需要据此 ack 一句即可，不存在「未真正执行」的问题，必须 stop。
   - 「{brand}的当前输出」末尾或正文出现 `<!-- EDGEOPS:UI_ACTION` 哨兵注释，或者文字中出现「请选择 A/B」「请回复 A 或 B」「[A] ... [B] ...」之类的选择卡呈现 → 助手已在等待用户作答，必须 stop，绝不可再追加 `请继续`。
   - 用户说「不升级」「取消」「先不做」「保持现状」等明确否定意图，且助手已 ack（如「好的，已确认不升级」） → 必须 stop，不要再追问「是否真的执行了」。
3. 若用户要求执行某操作，但{brand}只用了文字描述「已执行」而未发起工具调用 → 返回 action: "continue"，message 用与上段语言策略一致的自然语言，例如：「请通过工具实际执行该操作并依据工具返回结果再回复，不要仅在文字中声称已执行。」
4. 若{brand}尚未完成用户指令，只是执行到中间步骤并停下 → 返回 action: "continue"，并生成一句简短的引导语。包括但不限于：
   - {brand}说「已请求连接控制台」「已执行某命令」等但未完成后续步骤 → 引导「连接完成后请继续…」或「请根据控制台输出继续下一步」。
   - {brand}说「请稍等查看控制台输出」「请查看控制台结果后告诉我」等，即让用户自己看控制台而未根据控制台给出结论 → 视为未完成，返回 continue，message 为：「请根据当前控制台的最新输出总结结果并直接回复用户（例如是否已安装、命令是否成功）；若未安装或失败，可建议下一步操作。」
5. 其他未完成用户目标的中间状态 → 返回 continue，消息要具体，不要只写「继续」。

你必须且仅返回一个 JSON 对象，不要其他文字。格式：
{{"action": "continue", "message": "具体引导语"}} 或 {{"action": "stop"}}
"""


async def _call_assistant_ai(
    api_key: str,
    base_url: str,
    model: str,
    user_goal: str,
    ops_response: str,
    provider: str | None = None,
    *,
    output_lang_section: str | None = None,
) -> dict:
    """调用辅助 AI，返回 {"action": "continue"|"stop", "message": "..."}。"""
    if not provider or provider not in ("aliyun", "ollama", "openai"):
        provider = detect_provider(base_url)
    api_url = ensure_chat_completions_url(base_url)
    model = normalize_model(provider, model)
    headers = prepare_headers(provider, api_key)
    _ap = _build_assistant_system_prompt()
    system_prompt = f"{(output_lang_section or '').strip()}\n\n{_ap}".strip() if (output_lang_section or "").strip() else _ap
    user_content = f"""用户原始指令：\n{user_goal[:2000]}\n\n{_config.PRODUCT_NAME_ZH}当前输出：\n{(ops_response or '')[:3000]}"""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                api_url,
                headers=headers,
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "max_tokens": _resolve_request_max_tokens({}),
                    "stream": False,
                },
            )
        if resp.status_code != 200:
            logger.warning("辅助 AI 调用失败: %s %s", resp.status_code, resp.text[:200])
            return {"action": "stop"}
        result = resp.json()
        message, _ = parse_chat_response(result)
        content = extract_message_content(message).strip()
        # 尝试解析 JSON
        for start in ("{", "```json", "```"):
            idx = content.find(start)
            if idx >= 0:
                if "```" in start:
                    end = content.find("```", idx + 5)
                    content = content[idx + 7 : end] if end > idx else content[idx:]
                else:
                    content = content[idx:]
                break
        try:
            data = json.loads(content)
            action = (data.get("action") or "stop").lower()
            if action != "continue":
                return {"action": "stop"}
            return {"action": "continue", "message": (data.get("message") or "请继续。")[:500]}
        except json.JSONDecodeError:
            return {"action": "stop"}
    except Exception as e:
        logger.warning("辅助 AI 异常: %s", e)
        return {"action": "stop"}


async def _call_llm_nonstream(
    api_url: str,
    headers: dict,
    model: str,
    system_content: str,
    user_content: str,
) -> tuple[str | None, str | None]:
    """使用运维配置的非流式方式调用 AI。与聊天接口同一套配置与请求格式。
    返回 (content, None) 成功；(None, error_message) 失败。"""
    timeout = httpx.Timeout(connect=15.0, read=60.0, write=15.0, pool=15.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                api_url,
                headers=headers,
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_content},
                        {"role": "user", "content": user_content},
                    ],
                    "max_tokens": _resolve_request_max_tokens({}),
                    "stream": False,
                },
            )
        if resp.status_code != 200:
            err_detail = resp.text[:300]
            try:
                j = resp.json()
                err_detail = (
                    j.get("error", {}).get("message", err_detail)
                    if isinstance(j.get("error"), dict)
                    else j.get("message", j.get("detail", err_detail))
                )
            except Exception:
                pass
            logger.warning("会话标题总结 API 非 200: %s %s", resp.status_code, err_detail)
            return (None, f"AI 服务返回错误 ({resp.status_code})：{(err_detail or resp.reason_phrase or '未知')[:150]}")
        try:
            result = resp.json()
        except Exception as e:
            logger.warning("会话标题总结响应非 JSON: %s", e)
            return (None, "AI 返回格式异常，请检查服务地址是否为 chat/completions 接口")
        message, _ = parse_chat_response(result)
        content = extract_message_content(message).strip()
        if not content:
            return (None, "AI 未返回有效内容，请稍后重试")
        return (content[:200], None)
    except httpx.TimeoutException as e:
        logger.warning("会话标题总结请求超时: %s", e)
        return (None, "AI 请求超时，请检查系统设置中的 AI 服务地址与网络，或稍后重试")
    except Exception as e:
        logger.warning("会话标题总结请求异常: %s", e)
        return (None, "生成失败：" + str(e)[:100])


async def _summarize_session_title(
    session_id: int, api_key: str, base_url: str, model: str, force: bool = False
) -> tuple[str | None, str | None]:
    """根据会话内容调用 AI 总结一句简短标题并更新会话名。force=True 时忽略是否为自动生成的临时标题。
    使用与运维聊天相同的非流式调用方式。返回 (新标题, 错误提示)；成功时错误提示为 None。"""
    try:
        db = await get_db()
        rows = await db.execute_fetchall(
            "SELECT id, title FROM ai_chat_sessions WHERE id = ?", (session_id,)
        )
        if not rows:
            return (None, "会话不存在")
        if not force and not (rows[0]["title"] or "").strip().startswith(EDGEOPS_TEMP_SESSION_PREFIX):
            return (None, None)
        msg_rows = await db.execute_fetchall(
            """SELECT role, content FROM ai_chat_messages WHERE session_id = ? ORDER BY id ASC LIMIT 20""",
            (session_id,),
        )
        if not msg_rows:
            return (None, "会话暂无消息，请先发送几条对话后再生成名称")
        lines = []
        for r in msg_rows:
            role = "用户" if r["role"] == "user" else "助手"
            lines.append(f"{role}: {(r['content'] or '').strip()[:500]}")
        conversation = "\n".join(lines)
        provider = detect_provider(base_url)
        api_url = ensure_chat_completions_url(base_url)
        model_norm = normalize_model(provider, model)
        headers = prepare_headers(provider, api_key)
        system_content = (
            "根据以下对话总结成一句简短的中文会话标题，不超过20字，不要引号不要句号，直接输出标题内容。"
            "严禁在标题中包含、引用或复述任何密码、凭证、私钥、主机账户等敏感信息；仅输出简短中性标题。"
        )
        content, err = await _call_llm_nonstream(
            api_url, headers, model_norm, system_content, conversation
        )
        if err:
            return (None, err)
        if not content:
            return (None, "AI 未返回有效标题，请稍后重试")
        await db.execute(
            "UPDATE ai_chat_sessions SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (content[:200], session_id),
        )
        await db.commit()
        logger.info("会话 %s 标题已更新为: %s", session_id, content[:50])
        return (content[:200], None)
    except Exception as e:
        logger.warning("会话标题总结失败: %s", e)
        return (None, "生成失败：" + str(e)[:100])


@router.post("/chat")
async def chat(req: ChatRequest, request: Request, user=Depends(get_current_user)):
    """AI Agent 对话（SSE 流式响应 + Function Calling，装载当前用户控制台 buffer）"""
    try:
        return await _chat_impl(req, user, http_request=request)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("chat 请求处理异常: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"聊天服务异常，请检查 AI 配置与网络。错误: {str(e)[:200]}",
        )


async def _chat_impl(req: ChatRequest, user: dict, *, http_request: Request | None = None):
    """聊天逻辑实现（与 chat 分离便于捕获异常后返回明确 500 信息）。使用当前用户的 AI 配置。"""
    db = await get_db()
    settings = await _get_user_ai_settings(db, user["id"])
    if not settings.get("ai_agent_max_steps"):
        settings["ai_agent_max_steps"] = str(AGENT_MAX_STEPS)
    if not settings.get("ai_assistant_max_rounds"):
        settings["ai_assistant_max_rounds"] = str(ASSISTANT_MAX_ROUNDS)

    base_url = (settings.get("ai_base_url") or "").strip().rstrip("/")
    if not base_url:
        base_url = (getattr(_config, "AI_BASE_URL", "") or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(
            status_code=400,
            detail="AI 未配置服务地址 (base_url)，请在「系统设置」中填写大模型 API 地址（如阿里云 https://dashscope.aliyuncs.com/compatible-mode/v1、Ollama http://localhost:11434/v1、OpenAI https://api.openai.com/v1）",
        )
    provider = _effective_provider(settings, base_url)
    api_key = (settings.get("ai_api_key") or "").strip()
    # 共享 Key 配额状态：None = 用户有自己的 KEY，完全不受限；dict = 正在走系统共享 Key（含 exhausted/used/remaining/limit）
    trial_info: dict | None = None
    # 仅当用户未配置 API Key 而使用系统 KEY 时计入并限制；用户配置了自己的 KEY 则不受计数限制。系统 KEY 优先从 settings 表（管理员在系统设置中配置）读取。
    if require_api_key(provider, api_key) and not api_key:
        system_key, system_base = await _get_system_key_and_base(db)
        if _allow_system_shared_api_key(system_key, system_base, resolved_base_url=base_url):
            trial_info = await _consume_system_ai_usage(db, user["id"])
            if not trial_info.get("exhausted"):
                api_key = system_key
                if not (settings.get("ai_base_url") or "").strip() and (system_base or "").strip():
                    base_url = (system_base or "").strip().rstrip("/")
        else:
            raise HTTPException(status_code=400, detail="AI 未配置 API Key，请在「系统设置」中配置（Ollama 可留空）")

    provider = _effective_provider(settings, base_url)

    session_id = req.session_id
    scope_val = (getattr(req, "scope", None) or "default").strip().lower() or "default"
    if scope_val not in ("default", "local"):
        scope_val = "default"
    if scope_val == "local" and not _is_admin_role(user.get("role")):
        scope_val = "default"
    if not session_id:
        temp_title = EDGEOPS_TEMP_SESSION_PREFIX + datetime.now().strftime("%Y%m%d%H%M%S")
        await db.execute(
            "INSERT INTO ai_chat_sessions (user_id, host_id, title, session_scope) VALUES (?, ?, ?, ?)",
            (user["id"], req.host_id, temp_title, scope_val),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        session_id = (await cur.fetchone())[0]

    rows = await db.execute_fetchall(
        "SELECT id, host_id, COALESCE(session_prompt, '') AS session_prompt, COALESCE(session_scope, 'default') AS session_scope, COALESCE(low_interaction_mode, 'false') AS low_interaction_mode FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user["id"]),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="会话不存在")
    session_row = dict(rows[0])
    session_host_id = session_row.get("host_id")
    session_prompt = (session_row.get("session_prompt") or "").strip()
    session_scope = (session_row.get("session_scope") or "default").strip().lower()
    session_low_interaction = (session_row.get("low_interaction_mode") or "false").strip().lower() == "true"
    if session_scope == "integration":
        raise HTTPException(
            status_code=400,
            detail="此会话为 OpenClaw/API 集成专用，请使用 POST /api/integration/ops-chat/complete",
        )
    if session_scope in ("mcp_orchestrate", "mcp_runtime"):
        raise HTTPException(
            status_code=400,
            detail="此会话为 MCP 集成专用，请使用 MCP 工具或 /api/integration/mcp/*",
        )
    if session_scope == "local" and not _is_admin_role(user.get("role")):
        raise HTTPException(status_code=403, detail="本机管理仅管理员可用")

    system_prompt = (settings.get("ai_system_prompt") or "").strip() or _build_system_prompt()
    system_prompt = _sanitize_system_prompt_local_scope(system_prompt, session_scope=session_scope, user=user)
    system_prompt = _compact_system_prompt_for_request(system_prompt, req.message)

    # 附件注入：把用户在聊天框中已上传的附件绑定到本会话，并在用户消息末尾追加一份 📎 附件清单，
    # 让 AI 明确知道可调用 read_chat_attachment(uuid) 获取内容；历史上下文中也保留清单。
    # 同时保存 image 附件行，后面将其内联为 OpenAI 视觉多模态格式，让视觉模型直接「看到」图。
    _chat_image_attach_rows: list[dict] = []
    if req.attachment_uuids:
        try:
            from api.chat_attachments import (
                load_attachments_for_user as _load_user_attachments,
                build_attachment_message_suffix as _build_attachment_suffix,
                enrich_image_attachment_meta as _enrich_image_attachment_meta,
            )
            attach_rows = await _load_user_attachments(db, user["id"], req.attachment_uuids)
            if attach_rows:
                uname = (user.get("username") or "default")
                for r in attach_rows:
                    if (r.get("kind") or "").lower() == "image":
                        _enrich_image_attachment_meta(r, uname)
                    if r.get("session_id") != session_id:
                        await db.execute(
                            "UPDATE chat_attachments SET session_id = ? WHERE uuid = ? AND user_id = ?",
                            (session_id, r.get("uuid"), user["id"]),
                        )
                await db.commit()
                suffix = _build_attachment_suffix(attach_rows)
                if suffix:
                    req.message = (req.message or "") + "\n\n" + suffix
                _chat_image_attach_rows = [
                    r for r in attach_rows if (r.get("kind") or "").lower() == "image"
                ]
        except Exception as _attach_exc:  # 附件注入失败不应阻断聊天主流程
            logger.warning("附件注入失败 session_id=%s err=%s", session_id, _attach_exc)

    # 只取最近若干条消息，减少大会话时的 DB 与请求体体积（_apply_context_limits 会再截断条数与长度）
    msg_rows = await db.execute_fetchall(
        """SELECT role, content, created_at FROM ai_chat_messages WHERE session_id = ?
           ORDER BY id DESC LIMIT 80""",
        (session_id,),
    )
    msg_rows = list(reversed(msg_rows))
    conversation = [
        {
            "role": r["role"],
            "content": _with_history_timestamp(
                _strip_assistant_embedded_sentinels(r["content"] or "") if r["role"] == "assistant" else (r["content"] or ""),
                r["created_at"],
            ),
        }
        for r in msg_rows
    ]
    low_interaction_pref = session_low_interaction or _infer_low_interaction_preference(conversation, req.message or "")

    # 主机列表与分组（供 system 注入；普通用户仅看自己的）
    context_size_raw = _resolve_context_budget_chars(int(settings.get("ai_context_size") or "0"), settings)
    # 识图开关 + 图片 token 预算：内联 image_url 会额外吃掉 vision token，这里按单图 2K~8K
    # 估算并从文本 context 预算里扣减，避免多图把 历史/工具/主机知识 的字符预算挤爆。
    _vision_on = (settings.get("ai_vision_enabled") or "true").strip().lower() != "false"
    _vision_tokens, _vision_chars, _vision_count = _estimate_vision_token_reserve(
        _chat_image_attach_rows, conversation, vision_enabled=_vision_on
    )
    context_size = _apply_vision_token_reserve(context_size_raw, _vision_chars)
    if _vision_chars > 0:
        logger.info(
            "vision: 预留图片 token 预算 images=%d tokens=%d chars=%d context %d -> %d",
            _vision_count, _vision_tokens, _vision_chars, context_size_raw, context_size,
        )
    tool_result_limit = _tool_result_message_limit(context_size)
    terminal_scope_id = normalize_terminal_scope_id(req.terminal_scope_id)
    preferred_terminal_slot = req.preferred_terminal_slot
    try:
        preferred_terminal_slot = int(preferred_terminal_slot) if preferred_terminal_slot is not None else None
    except (TypeError, ValueError):
        preferred_terminal_slot = None
    if preferred_terminal_slot is not None:
        preferred_terminal_slot = max(0, min(preferred_terminal_slot, 31))
    prompt_context_host_id: int | None = None
    raw_pch = getattr(req, "context_host_id", None)
    if raw_pch is not None:
        try:
            _pch = int(raw_pch)
        except (TypeError, ValueError):
            _pch = None
        if _pch is not None and _pch > 0:
            try:
                await _load_host_for_prompt(db, _pch, user)
                prompt_context_host_id = _pch
            except HTTPException:
                prompt_context_host_id = None
    if _is_admin_role(user.get("role")):
        host_rows = await db.execute_fetchall(
            "SELECT id, name, host, port, aliases, remark, host_type, host_version, host_shell, host_package_manager FROM hosts ORDER BY name"
        )
        group_rows = await db.execute_fetchall(
            "SELECT id, name, description, parent_id FROM host_groups ORDER BY COALESCE(parent_id, 0), id"
        )
    else:
        host_rows = await db.execute_fetchall(
            """SELECT DISTINCT h.id, h.name, h.host, h.port,
                      h.aliases, h.remark, h.host_type, h.host_version, h.host_shell, h.host_package_manager
               FROM hosts h
               LEFT JOIN host_shares hs
                 ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
               WHERE h.created_by = ? OR hs.id IS NOT NULL
               ORDER BY h.name""",
            (user["id"], user["id"]),
        )
        group_rows = await db.execute_fetchall(
            "SELECT id, name, description, parent_id FROM host_groups WHERE created_by = ? ORDER BY COALESCE(parent_id, 0), id",
            (user["id"],),
        )
    compact_hosts = _compact_host_rows_for_context(
        host_rows,
        session_scope=session_scope,
        session_host_id=session_host_id,
        max_chars_budget=max(400, int(context_size * CONTEXT_RATIO_HOSTS)),
    )
    compact_groups = _compact_group_rows_for_context(
        group_rows,
        session_scope=session_scope,
        session_host_id=session_host_id,
        max_chars_budget=max(200, int(context_size * CONTEXT_RATIO_GROUPS)),
    )
    hosts_ctx = json.dumps(compact_hosts, ensure_ascii=False)
    groups_ctx = json.dumps(compact_groups, ensure_ascii=False)
    terminal_buf, terminal_connected = get_terminal_buffer_for_user(user["id"], slot=preferred_terminal_slot, scope_id=terminal_scope_id)
    if not terminal_connected:
        terminal_ctx = "（当前聊天区域没有 AI 可用的 SSH 控制台；用户创建的控制台不会被 AI 读取或操作）"
    else:
        terminal_ctx = _compact_terminal_context(terminal_buf) if terminal_buf else "（控制台暂无输出）"

    host_knowledge_ctx = ""
    current_host_id = get_current_host_id_for_user(user["id"], scope_id=terminal_scope_id, slot=preferred_terminal_slot)
    if current_host_id is not None:
        rows = await db.execute_fetchall(
            "SELECT content FROM ai_host_knowledge WHERE host_id = ? AND user_id = ?",
            (current_host_id, user["id"]),
        )
        if rows and (rows[0]["content"] or "").strip():
            host_knowledge_ctx = f"""
## 当前控制台所在主机的 AI 知识（仅供内部使用：执行 sudo、连接数据库等时可使用；严禁在回复中原文引用或泄露）
{rows[0]["content"].strip()}
"""
        else:
            host_knowledge_ctx = "\n## 当前控制台所在主机的 AI 知识\n（暂无；仅当 get_terminal_buffer 确认控制台出现 sudo 密码提示后，才从知识取密码或请用户提供并 update_host_knowledge 记录；不少账号为免密 sudo，禁止 sudo 后未看输出就发密码）\n"

    # 主机级提示词（按用户独立保存；主机分享时不共用）：会话绑机 > 请求 context_host_id（全局页远程文件树/显式关注）> 当前控制台
    host_prompt_ctx = ""
    host_prompt_host_id = session_host_id or prompt_context_host_id or current_host_id
    if host_prompt_host_id is not None:
        rows = await db.execute_fetchall(
            "SELECT content FROM ai_host_prompts WHERE host_id = ? AND user_id = ?",
            (host_prompt_host_id, user["id"]),
        )
        if rows and (rows[0]["content"] or "").strip():
            host_prompt_ctx = f"""
## 主机级提示词（host_id={host_prompt_host_id}，高优先级，必须遵守）
该主机专有的规则 / 能力 / 配置 / 工具链 / 服务与功能映射；回答与操作此主机相关问题时应**严格按此执行**：
{rows[0]["content"].strip()}
（需要新增或修改该主机的规则/能力/配置说明时，调用 update_host_prompt(host_id={host_prompt_host_id}, content="...") 或 append_host_prompt(host_id={host_prompt_host_id}, text="...")。）
"""
        else:
            host_prompt_ctx = (
                f"\n## 主机级提示词（host_id={host_prompt_host_id}）\n"
                "（暂无；该主机若具备独有工具链/规则/配置，可用 update_host_prompt(host_id=...) / append_host_prompt(host_id=..., text=...) 记录；"
                "与账号密码等机密无关，机密请用 update_host_knowledge 放入主机知识库。）\n"
            )

    host_scope_note = ""
    if session_scope == "local":
        host_scope_note = f"\n## 当前会话范围（本机管理）\n本会话为「本机管理」会话，仅操作运行{_config.PRODUCT_DISPLAY}的本机。请使用本机工具，勿用 SSH/主机工具（除非用户明确要求操作远程主机）。\n- 本机终端（可多个并行）：create_local_console 可多次调用以创建多个终端，便于同时执行多条任务线；list_terminals 可查看当前本机控制台及 slot；send_to_terminal(slot, text) 向指定 slot 发命令；get_terminal_buffer(slot) 读取某终端输出；close_local_console(slot) 关闭指定槽位。用户也可在界面上点击「+ 新建控制台」或标签上的 × 关闭。你只能关闭自己创建的本机控制台；不要把关闭终端当作固定收尾，默认保留最近使用的控制台给用户查看输出。仅当用户要求关闭、创建了多余临时控制台、或确认输出无需保留且无后续交互时，再调用 close_local_console(slot) 释放控制台。\n- 命令/脚本：local_exec、local_run_script。\n- **本机管理落盘与路径（引导）**：生成脚本或需要落盘时，**宜**先调用 `local_chat_data_paths`（可 `preview_subdir`）取推荐根目录、绝对路径与 `suggested_cwd_for_shell`，在生成的脚本中把**输出/下载/临时结果**指到其下，并在其下**自行**建立子目录与命名。`local_chat_write_file` / `local_chat_write_binary` 的 `path` **仅**写子路径（如 `weather/result.html`），**不要**把 `local/年/月/日` 再写进 `path` 或拼在 `absolute_dir` 后面（避免 `…/local/…/local/…`）。若用户**未**说明存放/数据来源，**一般**用推荐目录；若用户**明确要求**读/写/只处理**其它**路径或数据，则按用户；此外可由 AI 权衡例外。列/读本机磁盘用 `local_fs_list` 等；任意本机绝对路径用 `local_fs_*`。\n- 进程：process_start → process_stdin_write/process_stdout_read/process_stderr_read、process_wait、process_terminate、process_list。\n"
    elif session_host_id:
        host_scope_rows = await db.execute_fetchall(
            "SELECT id, name, host, port, aliases, remark, host_type, host_version, host_shell, host_package_manager FROM hosts WHERE id = ?",
            (session_host_id,),
        )
        if host_scope_rows:
            hp = normalize_host_aliases_in_dict(dict(host_scope_rows[0]))
            al = hp.get("aliases") or []
            rm = (hp.get("remark") or "").strip()
            host_scope_note = (
                f"\n## 当前会话范围\n本会话为「主机维度」AI 运维，仅针对主机 ID={session_host_id}（{hp.get('name')} / "
                f"{hp.get('host')}:{hp.get('port') or 22}）。"
            )
            if al:
                host_scope_note += f" 别名: {', '.join(al)}。"
            if rm:
                host_scope_note += f" 用途说明: {rm}。"
            host_scope_note += _host_env_prompt_snippet(hp)
            host_scope_note += (
                f" 可为当前机添加或修改别名、用途: update_host(host_id={session_host_id}, aliases=[...], remark=\"...\")。"
                " 控制台仅连接该主机；回复与操作请限定在此主机上。\n"
            )
            host_scope_note += (
                "\n### 主机「AI 运维」会话 · 多方案择一（必读）\n"
                f"当前用户在 **{_config.PRODUCT_DISPLAY}网页·主机详情 AI 运维** 中对话，界面**支持可点击选项按钮**。\n"
                "当你根据探查结果需要用户在 **两种及以上互斥处置方案**中作出选择时（典型：「方案 A / 方案 B」、"
                "「需要如何处理」、重启/改配置/回退等多条路径），**必须调用 `ask_user_choice`**，"
                "用 `question` + `options`（每项 `label` 写清方案要点，可选 `description` 补风险说明）渲染按钮。\n"
                "**禁止**仅用 Markdown 列表或纯文字选项让用户手打 「A」「B」——那种做法不会触发选项卡 UI。"
                "若某选项含服务中断、数据改写等风险，请将该选项 `style` 设为 `danger` 或 `primary` 并在文案中标明。\n"
            )
            host_scope_note += _host_dim_remote_data_processing_rules(session_host_id)

    ctx_profile = _infer_context_profile(
        user_message=req.message,
        session_scope=session_scope,
        session_host_id=session_host_id,
        has_terminal=terminal_connected and bool(terminal_buf),
        context_size=context_size,
    )
    # 按配置/自动估算的上下文大小分段截断，防止溢出
    hosts_ctx, groups_ctx, host_knowledge_ctx, terminal_ctx, conversation = _apply_context_limits(
        context_size, hosts_ctx, groups_ctx, host_knowledge_ctx, terminal_ctx, conversation, ctx_profile
    )
    logger.debug(
        "chat context budget=%s hosts=%s groups=%s knowledge=%s terminal=%s history_msgs=%s ratios=%s",
        context_size,
        len(hosts_ctx),
        len(groups_ctx),
        len(host_knowledge_ctx),
        len(terminal_ctx),
        len(conversation),
        json.dumps(ctx_profile.get("ratios") or {}, ensure_ascii=False),
    )

    assistant_enabled = (settings.get("ai_assistant_enabled") or "").strip().lower() == "true"
    auto_approve_enabled = (settings.get("ai_auto_approve") or "").strip().lower() == "true"

    session_prompt_block = ""
    if session_prompt:
        session_prompt_block = f"""
## 会话级约束（高优先级，必须遵守）
以下为本会话的专用约束，优先于通用规则，请严格遵守：
{session_prompt}
"""
    _global_ol = await _fetch_setting_value(db, "ai_output_locale")
    _user_ol = (settings.get("ai_output_locale") or "").strip()
    _ui_raw = (getattr(req, "ui_locale", None) or "").strip() or None
    _output_locale, _, _ = resolve_output_language(
        req.message or "",
        user_output_locale=_user_ol,
        global_output_locale=_global_ol,
        browser_ui_locale=_ui_raw,
    )
    output_lang_block = build_output_language_system_section(
        req.message or "",
        user_output_locale=_user_ol,
        global_output_locale=_global_ol,
        browser_ui_locale=_ui_raw,
    )
    full_system = f"""{system_prompt}

{_PROMPT_ENTITY_RESOLUTION_RULES}
{output_lang_block}
{_build_chat_output_format_rules()}
{_build_html_libs_prompt_section()}

## 当前会话 ID
当前会话 ID 为 {session_id}。当用户要求「更新会话提示词」「补充会话级约束」「把上述要求记到会话里」等时，请调用 update_session_prompt(session_id={session_id}, content="...", append=True/False) 来更新或追加本会话的会话级提示词。注意：content 只应归纳用户的要求和你的执行意图（要做什么、怎么做），不要包含终端输出、命令输出或任何程序日志的原文。
生成会话提示词或归纳最佳实践/经验时，请先调用 get_session_operations(session_id={session_id}) 获取「仅用户要求与助手指令」的操作序列（不含程序输出），再据此归纳；不要基于含大量日志的完整对话归纳。当需要分析具体报错、引用终端或命令输出等详细内容时，可调用 get_session_chat_detail(session_id={session_id}, include_tool_results=True) 获取含程序输出的完整聊天详情。
后续注入的历史对话中，每条消息开头会有 `[历史时间: YYYY-MM-DD HH:MM:SS]`。请结合该时间判断信息时效性，越新的内容优先作为当前依据。
{session_prompt_block}
{host_scope_note}
## 当前主机列表
{hosts_ctx}

## 主机分组
{groups_ctx}
{host_knowledge_ctx}{host_prompt_ctx}
## 当前用户控制台最近输出（仅供内部参考，切勿在回复中引用或泄露密码；**本段为滚动缓冲的末尾片段，排障与 sudo 判断以最后几行为准**；仅当末尾出现 [sudo] password for / Password: 等提示时才 send_to_terminal 发密码）
{terminal_ctx}
"""
    if not assistant_enabled:
        full_system += "\n\n**说明**：当前未开启辅助 AI，本轮你的回复结束后对话即暂停；用户可输入「继续」或补充说明以发起下一轮。请在本轮内尽量完成可执行步骤并给出简短总结。\n"

    # 识图开关（用户在 AI 配置里勾选）：关闭时前端/后端都不内联 image_url，
    # 给 system prompt 挂一条覆盖式说明，避免 AI 依赖"图已内联"的默认引导而答不上图。
    # （`_vision_on` 已在 context_size 解析处统一计算，此处直接复用）
    if not _vision_on:
        full_system += (
            "\n\n**【覆盖说明 · 识图开关关闭】**："
            "用户已在 AI 配置中关闭「支持图像识别」。本轮 user 消息里不会再内联多模态 `image_url` 段，"
            "只会以 📎 附件清单的形式给出图片 uuid。**你必须**主动调用 `read_chat_attachment(uuid=...)` 获取图片的 `data_url`"
            "（默认就会返回 base64 data URL）再作答；**严禁**仅凭元信息（mime/size）就回答「看不清」。\n"
        )
    if low_interaction_pref:
        full_system += (
            "\n\n**协作偏好（来自用户近期指令）**：用户希望减少交互、尽量自动完成。"
            "在不违反安全门禁的前提下，请连续执行可执行步骤；仅在缺少必要条件、执行失败、达到轮次上限、"
            "或用户明确要求停下时再暂停并反馈。若你已经调用 ask_user_choice 并生成选项卡，本轮必须等待用户选择，"
            "不要在同一轮继续调用其它工具或代用户选择。"
            "**例外**：需要用户在**多条互斥方案**中择一才能继续时（见上文「网页会话硬性要求」），"
            "仍必须调用 `ask_user_choice`，不要用纯 Markdown「方案 A/B」代替按钮。\n"
        )
    if not auto_approve_enabled:
        full_system += (
            "\n\n**工具确认策略（高优先级）**：当前用户未启用「AI 调用工具无需用户确认」。"
            "涉及删除、覆盖、重启、批量修改、写文件、创建/删除账号、凭证/权限变更等可能改变系统状态的操作，"
            "必须先用 ask_user_choice 明确征求用户确认；选项卡发出后本轮结束，等待用户点击或文字回复后才能继续。"
        )
    full_system += _USER_MCP_SYSTEM_HINT
    try:
        from services.user_skills_runtime import build_user_skills_system_section

        _skills_sec = await build_user_skills_system_section(
            user,
            session_scope,
            int(session_row.get("host_id")) if session_row and session_row.get("host_id") else None,
        )
        if _skills_sec:
            full_system += _skills_sec
    except Exception as _usk_exc:
        logger.debug("注入 user skills 失败 sid=%s: %s", session_id, _usk_exc)

    _focus_hid = session_row.get("host_id") if session_row else None
    try:
        _runtime_ctx = await get_runtime_context_for_session(
            db,
            session_id,
            focus_host_id=int(_focus_hid) if _focus_hid else None,
            output_locale=_output_locale,
        )
        if _runtime_ctx:
            full_system += "\n\n" + _runtime_ctx
    except Exception as _rtc_exc:
        logger.debug("注入 session_runtime 失败 sid=%s: %s", session_id, _rtc_exc)

    messages = [{"role": "system", "content": full_system}]
    for m in conversation:
        if m.get("role") in ("user", "assistant"):
            messages.append({"role": m["role"], "content": m.get("content", "")})
    # 本轮新上传的图片附件内联为 OpenAI 视觉多模态 content，让模型真正「看到」图；
    # 没有图片时退化为纯字符串，对非视觉模型仍完全兼容；
    # 用户在 AI 配置中关闭了「支持图像识别」时也不内联（避免给非视觉模型硬塞多模态段）。
    _user_content = _build_user_message_content_with_images(
        req.message, _chat_image_attach_rows, user, vision_enabled=_vision_on
    )
    messages.append({"role": "user", "content": _user_content})
    # 跨轮视觉记忆：把最近几条 user 历史消息里出现过的 image 附件也内联，让「比较上一张和这张」
    # 这类多轮图像对比能够工作；失败时静默跳过，不阻断主流程。
    try:
        await _inject_history_image_memory(messages, db, user, vision_enabled=_vision_on)
    except Exception as _vision_hist_exc:  # pragma: no cover - 防御性
        logger.warning("vision: 历史图片记忆注入失败 session_id=%s err=%s", session_id, _vision_hist_exc)

    api_url = ensure_chat_completions_url(base_url)
    model = normalize_model(provider, settings.get("ai_model") or "")

    try:
        agent_max_steps = max(1, min(AGENT_MAX_STEPS_CAP, int(settings.get("ai_agent_max_steps") or 0) or AGENT_MAX_STEPS))
    except (TypeError, ValueError):
        agent_max_steps = AGENT_MAX_STEPS
    try:
        assistant_max_rounds = max(1, min(ASSISTANT_MAX_ROUNDS_CAP, int(settings.get("ai_assistant_max_rounds") or 0) or ASSISTANT_MAX_ROUNDS))
    except (TypeError, ValueError):
        assistant_max_rounds = ASSISTANT_MAX_ROUNDS

    async def agent_stream():
        nonlocal messages
        yield _sse({"session_id": session_id})
        # 把配额状态告知前端，让 UI 可选地做个提示条（即使前端不处理也不会报错）
        if trial_info is not None:
            yield _sse({
                "trial_info": {
                    "exhausted": bool(trial_info.get("exhausted")),
                    "used": trial_info.get("used", 0),
                    "limit": trial_info.get("limit", SYSTEM_AI_USAGE_LIMIT),
                    "remaining": trial_info.get("remaining", 0),
                }
            })
        # —— 共享 Key 配额已用尽：直接用固定文案作为 AI 回复，流式推送后结束，不调任何 LLM —— #
        if trial_info is not None and trial_info.get("exhausted"):
            exhausted_text = _format_trial_exhausted_message(trial_info, _output_locale)
            # 先把当前 user 消息落库
            try:
                await db.execute(
                    "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'user', ?)",
                    (session_id, (req.message or "")[:AI_MESSAGE_SAVE_MAX]),
                )
                await db.commit()
            except Exception as exc:
                logger.warning("保存用户消息（配额用尽分支）失败: %s", exc)
            # 固定文案直接整段下发，不再做"伪打字"切片
            if exhausted_text:
                yield _sse({"content": exhausted_text})
            # 落库 assistant 回复
            try:
                await db.execute(
                    "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                    (session_id, exhausted_text[:AI_MESSAGE_SAVE_MAX]),
                )
                await db.execute(
                    "UPDATE ai_chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (session_id,),
                )
                await db.commit()
            except Exception as exc:
                logger.warning("保存 assistant 回复（配额用尽分支）失败: %s", exc)
            yield "data: [DONE]\n\n"
            return
        timeout = httpx.Timeout(
            connect=15.0,
            read=float(getattr(_config, "AI_CHAT_HTTP_READ_TIMEOUT_SEC", 240)),
            write=15.0,
            pool=15.0,
        )
        headers = prepare_headers(provider, api_key)
        last_user_message = req.message
        assistant_rounds = 0
        force_tool_retries = 0
        # 配额横幅：仅在本次请求的「AI 第一条 assistant 回复」前推送一次
        trial_banner_shown = False
        # 标记当前 last_user_message 是否已落库，保证 AI 失败时仍能保留用户发言与错误痕迹
        pending_user_msg = {"text": last_user_message, "saved": False}
        # 收集本轮（assistant 文本回复之前）的 ui_action（如 ask_user_choice 选择卡），
        # 在 assistant 消息落库时以哨兵注释 `<!-- EDGEOPS:UI_ACTION ... -->` 嵌入 content，
        # 让前端在 loadSession 重渲后可还原选择卡，避免「一闪而过」。每次 assistant 落库后清空。
        pending_ui_actions: list[dict] = []
        # 工具 / 推理步骤：落库进 TOOL_TRACE 哨兵，便于历史会话折叠查看调用流程。
        pending_tool_trace: list[dict] = []

        # 新一轮普通聊天开始时，丢弃上一轮结束后误入队列的运行时控制指令。
        # 否则用户在 UI 已结束但控制条未复位时点击“补充”，下一轮会被误判为插入指令。
        await _clear_runtime_control_queue(session_id)

        async def _persist_pending_user_msg():
            """把当前未保存的 user 消息写入数据库，避免 AI 失败导致消息丢失。"""
            if pending_user_msg["saved"]:
                return
            text = pending_user_msg["text"] or ""
            if not text:
                pending_user_msg["saved"] = True
                return
            try:
                await db.execute(
                    "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'user', ?)",
                    (session_id, text[:AI_MESSAGE_SAVE_MAX]),
                )
                await db.execute(
                    "UPDATE ai_chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (session_id,),
                )
                await db.commit()
                pending_user_msg["saved"] = True
            except Exception as exc:
                logger.warning("提前保存用户消息失败: %s", exc)

        async def _persist_assistant_error(
            err_text: str,
            *,
            tool_trace: list | None = None,
            partial_assistant: str | None = None,
            retry_detail: str | None = None,
        ):
            """把失败信息落库为 assistant 消息；可附带本轮 TOOL_TRACE 与中断前已生成的助手正文。"""
            try:
                await _persist_pending_user_msg()
                blocks: list[str] = [f"[错误] {(err_text or 'AI 请求失败').strip()}"]
                if retry_detail and retry_detail.strip():
                    blocks.append(retry_detail.strip())
                if partial_assistant and partial_assistant.strip():
                    pa = partial_assistant.strip()
                    cap = 85_000
                    if len(pa) > cap:
                        pa = pa[: cap - 24] + "\n\n…（正文过长已截断）"
                    blocks.append("---\n**中断前助手已输出的正文（若有）**\n\n" + pa)
                body = "\n\n".join(blocks)
                body = _embed_tool_trace_into_content(body, list(tool_trace) if tool_trace else [])
                await db.execute(
                    "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                    (session_id, body[:AI_MESSAGE_SAVE_MAX]),
                )
                await db.execute(
                    "UPDATE ai_chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (session_id,),
                )
                await db.commit()
            except Exception as exc:
                logger.warning("保存 AI 错误消息失败: %s", exc)

        async def _consume_runtime_control():
            """读取一条运行时控制指令；无指令返回 None。"""
            ctrl = await _pull_runtime_control_nowait(session_id)
            if not isinstance(ctrl, dict):
                return None
            action = (ctrl.get("action") or "").strip().lower()
            if action not in _RUNTIME_ACTIONS:
                return None
            msg = (ctrl.get("message") or "").strip()
            return {"action": action, "message": msg}

        async def _consume_runtime_stop():
            """长时间等待 LLM 响应时只抢占 stop，避免补充/选择指令被误消费。"""
            ctrl = await _pull_runtime_control_matching(session_id, "stop")
            if not isinstance(ctrl, dict):
                return None
            return {"action": "stop", "message": (ctrl.get("message") or "").strip()}

        # 先把用户消息落库；即便随后 AI 超时/异常，用户发言与错误痕迹仍可通过 loadSession 恢复
        await _persist_pending_user_msg()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                while assistant_rounds < assistant_max_rounds:
                    _ctrl = await _consume_runtime_control()
                    if _ctrl:
                        _a = _ctrl["action"]
                        _m = _ctrl["message"]
                        if _a == "stop":
                            yield _sse({"runtime_control": {"action": "stop", "accepted": True}})
                            yield "data: [DONE]\n\n"
                            return
                        if _a in ("pause", "supplement"):
                            injected = _m or ("用户要求暂停当前执行，请先回答用户问题，再等待继续指令。" if _a == "pause" else "")
                            if injected:
                                last_user_message = injected
                                pending_user_msg = {"text": injected, "saved": False}
                                await _persist_pending_user_msg()
                                messages.append({"role": "user", "content": injected})
                                yield _sse({"runtime_control": {"action": _a, "accepted": True}})
                        # resume 在这里不需要特殊处理（仅用于解除暂停语义，消息由用户下一轮驱动）
                    content = None
                    # 本轮特征用于「辅助 AI 是否需要追问」的硬拦截：
                    # - round_had_tool_call：本轮主 AI 是否发起过任何 tool_call。
                    # - round_had_ui_action：本轮是否触发过 ask_user_choice 等交互卡
                    #   （在 pending_ui_actions 被清空前捕获）。
                    # 见下面 `if round_had_ui_action or ...` 处的拦截逻辑。
                    round_had_tool_call = False
                    round_had_ui_action = False
                    short_final_retry_used = False
                    image_blind_tool_retry_used = False
                    for round_idx in range(agent_max_steps):
                        round_tools = await resolve_chat_tools(
                            get_tools_for_scope(session_scope, user),
                            session_scope,
                            user,
                            session_host_id,
                        )
                        # 视觉降级：provider 报 "input length 超限 / 图片过大" 等错时，
                        # 自动按阶梯压缩 image_url 段并重试；多次仍失败则剥离全部图片再试
                        # 一次，并在 user 消息里追加提示让 AI 改走 read_chat_attachment。
                        _retry_n = max(0, int(getattr(_config, "AI_CHAT_LLM_TIMEOUT_RETRIES", 3)))
                        _max_llm_tries = 1 + _retry_n
                        _read_sec = float(getattr(_config, "AI_CHAT_HTTP_READ_TIMEOUT_SEC", 240))
                        # 真流式：边接收 token 边推到前端，避免"等模型整段生成完才显示"
                        # 造成的"步与步之间长时间空白"。msg / tool_calls 由本段在收到
                        # `done` 事件后由流式累积器重建，与原非流式分支下游格式一致。
                        msg: dict | None = None
                        tool_calls: list = []
                        streamed_content_text = ""
                        streamed_reasoning_text = ""
                        cot_streaming_started = False
                        stream_partial_yielded = False  # 已经向前端推送过模型 token，重试会"接续"，先停止重试
                        for _llm_try in range(_max_llm_tries):
                            # 单次重试前重置流式累积器；只有「上一轮重试连一个 token 都没收到」时
                            # 才会真正复用此分支，复用后继续重试
                            if not stream_partial_yielded:
                                streamed_content_text = ""
                                streamed_reasoning_text = ""
                                cot_streaming_started = False
                            event_queue: asyncio.Queue = asyncio.Queue()

                            async def _stream_producer(_q=event_queue):
                                """把 _stream_chat_with_vision_fallback 的事件 push 到 queue，
                                外层在主协程里一边消费 + yield 一边轮询运行时控制 / 心跳。"""
                                try:
                                    async for ev in _stream_chat_with_vision_fallback(
                                        client,
                                        api_url=api_url,
                                        headers=headers,
                                        payload={
                                            "model": model,
                                            "tools": round_tools,
                                            "tool_choice": "auto",
                                            "max_tokens": _resolve_request_max_tokens(settings),
                                        },
                                        messages=messages,
                                    ):
                                        await _q.put(("ev", ev))
                                except BaseException as _e:
                                    await _q.put(("exc", _e))
                                finally:
                                    await _q.put(("end", None))

                            stream_task = asyncio.create_task(_stream_producer())
                            done_event: dict | None = None
                            http_err_event: dict | None = None
                            stream_exc: BaseException | None = None
                            saw_first_byte = False
                            llm_wait_ticks = 0

                            try:
                                while True:
                                    try:
                                        item = await asyncio.wait_for(event_queue.get(), timeout=0.35)
                                    except asyncio.TimeoutError:
                                        # 模型沉默期：抢占 stop 控制 + 周期性 keepalive；
                                        # 一旦模型开始吐 token，本分支自然不再走（item 立刻可读）
                                        if not saw_first_byte:
                                            _stop_ctrl = await _consume_runtime_stop()
                                            if _stop_ctrl:
                                                stream_task.cancel()
                                                try:
                                                    await stream_task
                                                except (asyncio.CancelledError, Exception):
                                                    pass
                                                if cot_streaming_started:
                                                    yield _sse({"cot": {"phase": "pre_tool", "kind": "reasoning_end"}})
                                                    cot_streaming_started = False
                                                yield _sse({"runtime_control": {"action": "stop", "accepted": True, "during_llm": True}})
                                                yield "data: [DONE]\n\n"
                                                return
                                            llm_wait_ticks += 1
                                            if llm_wait_ticks % 5 == 0:
                                                yield _sse_keepalive()
                                        continue
                                    item_kind, item_payload = item
                                    if item_kind == "end":
                                        break
                                    if item_kind == "exc":
                                        stream_exc = item_payload
                                        # 仍要消费完队列（producer finally 会再 push 一个 end）
                                        continue
                                    ev = item_payload or {}
                                    ev_kind = ev.get("kind")
                                    if ev_kind == "content":
                                        saw_first_byte = True
                                        txt = ev.get("text") or ""
                                        if txt:
                                            # 第一次出现可见正文：先关闭仍开着的 reasoning 段
                                            if cot_streaming_started:
                                                yield _sse({"cot": {"phase": "pre_tool", "kind": "reasoning_end"}})
                                                cot_streaming_started = False
                                            # 共享 Key 配额横幅插在第一行正文之前；本轮只插一次
                                            if (
                                                trial_info is not None
                                                and not trial_info.get("exhausted")
                                                and not trial_banner_shown
                                            ):
                                                _banner = _format_trial_banner(trial_info, _output_locale)
                                                if _banner:
                                                    streamed_content_text += _banner
                                                    yield _sse({"content": _banner})
                                                    trial_banner_shown = True
                                            streamed_content_text += txt
                                            stream_partial_yielded = True
                                            yield _sse({"content": txt})
                                    elif ev_kind == "reasoning":
                                        saw_first_byte = True
                                        txt = ev.get("text") or ""
                                        if txt:
                                            cot_streaming_started = True
                                            streamed_reasoning_text += txt
                                            stream_partial_yielded = True
                                            yield _sse({
                                                "cot": {
                                                    "phase": "pre_tool",
                                                    "kind": "reasoning_chunk",
                                                    "text": txt,
                                                }
                                            })
                                    elif ev_kind == "vision_retry":
                                        # 让前端可选地展示降级提示；前端忽略未知字段不会报错
                                        yield _sse({
                                            "action": "vision_retry",
                                            "stage": ev.get("stage"),
                                            "n": ev.get("n"),
                                            "reason": ev.get("reason"),
                                        })
                                    elif ev_kind == "http_error":
                                        http_err_event = ev
                                    elif ev_kind == "done":
                                        done_event = ev
                            finally:
                                if not stream_task.done():
                                    stream_task.cancel()
                                    try:
                                        await stream_task
                                    except (asyncio.CancelledError, Exception):
                                        pass

                            # === stream 收尾：根据 done / http_error / exc 三种情形分别处理 ===
                            if stream_exc is not None and not done_event and not http_err_event:
                                # httpx 超时 / 网络错：按原非流式同款"超时重试 + 提示"逻辑
                                if isinstance(stream_exc, httpx.TimeoutException):
                                    logger.warning(
                                        "Agent LLM 流式读超时 session_id=%s round=%s try=%s/%s",
                                        session_id, round_idx, _llm_try + 1, _max_llm_tries,
                                    )
                                    # 已经流出过部分 token：放弃重试（再请求模型会从头重发，前端会看到重复/错位）
                                    if stream_partial_yielded:
                                        if cot_streaming_started:
                                            yield _sse({"cot": {"phase": "pre_tool", "kind": "reasoning_end"}})
                                            cot_streaming_started = False
                                        _tail = "\n\n*（上游连接中断，以上为目前已收到的内容）*"
                                        streamed_content_text += _tail
                                        yield _sse({"content": _tail})
                                        msg = {"content": streamed_content_text, "reasoning_content": streamed_reasoning_text}
                                        tool_calls = []
                                        break
                                    if _llm_try + 1 >= _max_llm_tries:
                                        err_full = (
                                            f"AI 服务请求超时（读超时约 {_read_sec:.0f}s，已额外重试 {_retry_n} 次仍失败）"
                                            "，请稍后重试；若常生成大 HTML，可改用 fs_write_file / create_chat_artifact 分段落盘。"
                                        )
                                        await _persist_assistant_error(
                                            err_full,
                                            tool_trace=list(pending_tool_trace),
                                            retry_detail=(
                                                "可通过环境变量 EDGEOPS_AI_CHAT_HTTP_READ_TIMEOUT_SEC、"
                                                "EDGEOPS_AI_CHAT_LLM_TIMEOUT_RETRIES 调整超时与重试次数。"
                                            ),
                                        )
                                        yield _sse({"error": err_full})
                                        yield "data: [DONE]\n\n"
                                        return
                                    _loc = (_output_locale or "").strip().lower()
                                    if _loc == "en" or _loc.startswith("en-"):
                                        _retry_msg = (
                                            f"\n\n*(Upstream slow; retrying LLM request {_llm_try + 2}/{_max_llm_tries}…)*\n\n"
                                        )
                                    else:
                                        _retry_msg = (
                                            f"\n\n*（上游响应较慢，正在第 {_llm_try + 2}/{_max_llm_tries} 次重试…）*\n\n"
                                        )
                                    yield _sse({"content": _retry_msg})
                                    await asyncio.sleep(min(3.0, 0.8 * (_llm_try + 1)))
                                    continue
                                # 其它网络异常：直接报错（保留已显示的 partial 文本）
                                logger.exception("Agent stream 异常: %s", stream_exc)
                                if cot_streaming_started:
                                    yield _sse({"cot": {"phase": "pre_tool", "kind": "reasoning_end"}})
                                    cot_streaming_started = False
                                err_full = f"AI 流式请求异常: {type(stream_exc).__name__}: {stream_exc}"
                                await _persist_assistant_error(
                                    err_full,
                                    tool_trace=list(pending_tool_trace),
                                    partial_assistant=streamed_content_text or None,
                                )
                                yield _sse({"error": err_full})
                                yield "data: [DONE]\n\n"
                                return

                            if http_err_event is not None:
                                err_status = http_err_event.get("status_code") or 502
                                err_body = (http_err_event.get("body") or "")[:500]
                                err_msg = err_body
                                try:
                                    _err_json = json.loads(http_err_event.get("body") or "")
                                    err_msg = (
                                        _err_json.get("error", {}).get("message", err_msg)
                                        if isinstance(_err_json.get("error"), dict)
                                        else _err_json.get("message", err_msg)
                                    )
                                except Exception:
                                    pass
                                if cot_streaming_started:
                                    yield _sse({"cot": {"phase": "pre_tool", "kind": "reasoning_end"}})
                                    cot_streaming_started = False
                                err_full = f"AI 服务返回错误 (HTTP {err_status}): {err_msg}"
                                await _persist_assistant_error(
                                    err_full,
                                    tool_trace=list(pending_tool_trace),
                                    partial_assistant=streamed_content_text or None,
                                )
                                yield _sse({"error": err_full})
                                yield "data: [DONE]\n\n"
                                return

                            if done_event is not None:
                                # 用流式累积出的内容重建一个 message dict，下游 _cot_merge_*
                                # / _looks_like_truncated_assistant_reply 等仍可正常工作。
                                msg = {
                                    "content": streamed_content_text,
                                    "reasoning_content": streamed_reasoning_text,
                                }
                                tool_calls = done_event.get("tool_calls") or []
                                if cot_streaming_started:
                                    yield _sse({"cot": {"phase": "pre_tool", "kind": "reasoning_end"}})
                                    cot_streaming_started = False
                                break

                            # producer 结束但既无 done 也无 http_error 也无 exc：判定为内部异常
                            err_full = "AI 请求未返回有效响应（流提前结束）"
                            await _persist_assistant_error(
                                err_full,
                                tool_trace=list(pending_tool_trace),
                                partial_assistant=streamed_content_text or None,
                            )
                            yield _sse({"error": err_full})
                            yield "data: [DONE]\n\n"
                            return

                        if msg is None:
                            err_full = "AI 请求未返回有效响应（内部状态异常）"
                            await _persist_assistant_error(err_full, tool_trace=list(pending_tool_trace))
                            yield _sse({"error": err_full})
                            yield "data: [DONE]\n\n"
                            return

                        if tool_calls:
                            round_had_tool_call = True
                            full_tool_calls, prepared_tool_calls = await _prepare_tool_calls_for_execution(
                                tool_calls,
                                reorder_confirm_before_irreversible=True,
                            )
                            suppress_stale_delete_asks = _should_suppress_stale_delete_confirm_asks(
                                messages, prepared_tool_calls
                            )
                            # 跨轮去重：用户已对上一轮的选择卡作答（无论点按钮还是写自由文字），
                            # 如果模型这一轮又发起一模一样的 ask_user_choice（指纹相同），本次直接跳过、
                            # 返回合成 tool_result，提示模型基于用户最新消息直接以文字答复。
                            stale_ask_user_choice_fps = _recent_assistant_ask_user_choice_fps(messages)
                            # 取最近一条 user 消息原文，用于在跳过同指纹卡时给模型一句解释（"用户已经用 X 作答了"）。
                            # 不再据此一刀切拦截"本轮所有"卡——若模型这一轮确实想发**内容不同**的新选择卡来追问新条件，
                            # 应当被允许；只有当新卡和上一轮指纹相同（同问题、同选项）时才视为重复并跳过。
                            _last_user_reply_text = ""
                            if stale_ask_user_choice_fps:
                                for _m_back in reversed(messages):
                                    if isinstance(_m_back, dict) and _m_back.get("role") == "user":
                                        _last_user_reply_text = (_m_back.get("content") or "").strip()
                                        break
                            # 诊断日志（debug 级）：每轮记录一次"上一轮卡指纹集合"和当前轮 ask_user_choice 指纹，
                            # 方便排查"同问题的卡没拦下来"这类回归——通常是 question/options 的微小字符差异
                            # 或被 LLM 换成同义词，导致指纹对不上。生产默认日志级别看不到，不会干扰正常运维输出。
                            if stale_ask_user_choice_fps:
                                try:
                                    _cur_round_ask_fps = [
                                        _fingerprint_ask_user_choice_tool_call(_tc)
                                        for _tc, _, _ in prepared_tool_calls
                                        if (_tc.get("function") or {}).get("name") == "ask_user_choice"
                                    ]
                                    logger.debug(
                                        "ask_user_choice cross-round dedupe: stale_fps=%r cur_round_fps=%r last_user=%r",
                                        list(stale_ask_user_choice_fps),
                                        _cur_round_ask_fps,
                                        (_last_user_reply_text or "")[:80],
                                    )
                                except Exception:
                                    pass
                            batch_had_irreversible_success = False
                            messages.append({
                                "role": "assistant",
                                "content": extract_message_content(msg) or "",
                                "tool_calls": full_tool_calls,
                            })
                            max_next_poll_seconds = 0
                            terminal_poll_batch = TerminalPollBatchState()
                            runtime_injected_user_message = None
                            # 工具执行前的"模型随附说明"现在已经在流式阶段一字一字推过了
                            # （见上面 ev_kind == "reasoning" / "content" 分支），这里仅做两件事：
                            # 1) 兜底：模型既没流出 reasoning 也没流出正文却直接给了 tool_calls，
                            #    推一条短规划句，避免前端 cot 区域空白；
                            # 2) 把本轮已收集的 reasoning + content 快照一并写入 trace，方便会话历史折叠展示。
                            pre_tool_text_streamed = (streamed_reasoning_text or streamed_content_text or "").strip()
                            if not pre_tool_text_streamed and prepared_tool_calls:
                                _fallback_text = _cot_fallback_tool_plan_text(
                                    [tc["function"]["name"] for tc, _, _ in prepared_tool_calls],
                                    _output_locale,
                                )
                                if _fallback_text:
                                    yield _sse({
                                        "cot": {
                                            "phase": "pre_tool",
                                            "kind": "reasoning_chunk",
                                            "text": _fallback_text,
                                        }
                                    })
                                    yield _sse({"cot": {"phase": "pre_tool", "kind": "reasoning_end"}})
                                    pre_tool_text_streamed = _fallback_text
                            try:
                                _cot_snap = (pre_tool_text_streamed or "")[:12_000]
                                if _cot_snap.strip():
                                    pending_tool_trace.append(
                                        {"type": "cot", "phase": "pre_tool", "text": _cot_snap}
                                    )
                            except Exception:
                                pass
                            for tc, fn_args, fn_args_preview in prepared_tool_calls:
                                fn_name = tc["function"]["name"]
                                fn_id = tc["id"]
                                yield _sse({"action": "executing", "tool": fn_name, "args": fn_args_preview})
                                try:
                                    pending_tool_trace.append(
                                        {
                                            "type": "tool",
                                            "event": "executing",
                                            "tool": fn_name,
                                            "args": fn_args_preview,
                                        }
                                    )
                                except Exception:
                                    pass
                                logger.info("Agent 执行: %s(%s)", fn_name, json.dumps(fn_args_preview, ensure_ascii=False))
                                # 跨轮重复 ask_user_choice 检测：
                                # 用户已对上一轮选择卡作过任何形式的回应（按钮 `[A] xxx` 或自由文字补充），
                                # 都视为"那张卡已被回应过"。如果模型这一轮又发起**指纹相同**（同问题、同选项）的卡，
                                # 直接跳过并把用户原话回喂给模型，让它把这条回应当成对上一卡的决定来用。
                                # 注意：内容真的不一样的新卡（追问新条件、缩减选项等）**不会**被拦截，允许模型继续推进。
                                _is_repeat_ask = False
                                _repeat_skip_reason = ""
                                if fn_name == "ask_user_choice" and stale_ask_user_choice_fps:
                                    _cur_fp = _fingerprint_ask_user_choice_tool_call(tc)
                                    if _cur_fp and _cur_fp in stale_ask_user_choice_fps:
                                        _is_repeat_ask = True
                                        _reply_preview = (_last_user_reply_text or "")[:200]
                                        _is_button_reply = bool(
                                            _last_user_reply_text
                                            and _USER_CHOICE_REPLY_RE.match(_last_user_reply_text)
                                        )
                                        if _is_button_reply:
                                            _repeat_skip_reason = (
                                                f"用户已在上一轮通过按钮作答：「{_reply_preview}」。"
                                                "请勿用**完全相同**的「问题 + 选项」再弹一次同样的选择卡；"
                                                "请基于这个选择继续后续操作或以文字给出结论。"
                                                "如确实仍需进一步澄清，请发起**内容不同**（不同问题或不同选项）的新选择卡，"
                                                "或换成纯文字提问，**不要原样复读上一张**。"
                                            )
                                        else:
                                            _repeat_skip_reason = (
                                                f"用户没有点按钮，而是直接以文字「{_reply_preview}」对上一张选择卡作答。"
                                                "请把这条文字理解为用户对那张卡的明确回应/选择（必要时映射到最贴合的选项），"
                                                "**严禁原样再发同一张卡**。如果你判断仍需用户进一步明确，请发起**内容不同**的"
                                                "新选择卡（不同问题或不同选项），或改用纯文字追问；不要把刚才那张原封不动重新弹出来。"
                                            )
                                skip_stale_ask_ui = fn_name == "ask_user_choice" and (
                                    suppress_stale_delete_asks or batch_had_irreversible_success or _is_repeat_ask
                                )
                                # 支持流式进度的工具（delegate_to_cli_agent / delegate_chain / run_workflow_template）
                                # 会通过 stream_callback 把每一行/每一步的事件边跑边推过来；其它工具忽略该参数。
                                _streaming_tools = {
                                    "delegate_to_cli_agent",
                                    "delegate_chain",
                                    "run_workflow_template",
                                    "delegate_to_edgeops_ai",
                                    "scp_push",
                                    "scp_pull",
                                    "relay_file_between_hosts",
                                    "transfer_file_between_hosts",
                                }
                                _transfer_cancel = (
                                    threading.Event()
                                    if fn_name in (
                                        "scp_push",
                                        "scp_pull",
                                        "relay_file_between_hosts",
                                        "transfer_file_between_hosts",
                                    )
                                    else None
                                )
                                if skip_stale_ask_ui:
                                    _skip_msg = _repeat_skip_reason or (
                                        "此确认题已由服务端跳过：删除等不可逆操作已在先执行或上下文显示已完成，"
                                        "请勿再向用户展示删除确认选项，直接简洁汇报结果即可。"
                                    )
                                    tool_result = json.dumps(
                                        {
                                            "success": True,
                                            "skipped": True,
                                            "message": _skip_msg,
                                        },
                                        ensure_ascii=False,
                                    )
                                elif fn_name in _streaming_tools:
                                    _stream_q: asyncio.Queue = asyncio.Queue()
                                    _stream_done = object()

                                    async def _sc(ev: dict, _q=_stream_q) -> None:
                                        try:
                                            _q.put_nowait(ev)
                                        except Exception:
                                            pass

                                    async def _run_tool():
                                        try:
                                            return await execute_tool(
                                                fn_name, fn_args, user,
                                                scope=session_scope,
                                                terminal_scope_id=terminal_scope_id,
                                                default_terminal_slot=preferred_terminal_slot,
                                                stream_callback=_sc,
                                                session_id=session_id,
                                                ui_locale=_ui_raw,
                                                transfer_cancel_event=_transfer_cancel,
                                            )
                                        finally:
                                            _stream_q.put_nowait(_stream_done)

                                    _tool_task = asyncio.create_task(_run_tool())
                                    _stream_idle_ticks = 0
                                    while True:
                                        try:
                                            _ev = await asyncio.wait_for(_stream_q.get(), timeout=0.35)
                                        except asyncio.TimeoutError:
                                            _ctrl = await _consume_runtime_control()
                                            if _ctrl and _ctrl["action"] in ("stop", "pause", "supplement"):
                                                if _transfer_cancel is not None:
                                                    _transfer_cancel.set()
                                                _tool_task.cancel()
                                                try:
                                                    await _tool_task
                                                except asyncio.CancelledError:
                                                    pass
                                                except Exception:
                                                    pass
                                                _a = _ctrl["action"]
                                                _m = _ctrl["message"]
                                                yield _sse({"runtime_control": {"action": _a, "accepted": True, "during_tool": fn_name}})
                                                if _a == "stop":
                                                    yield "data: [DONE]\n\n"
                                                    return
                                                runtime_injected_user_message = _m or "用户要求暂停当前任务，请先回答用户的新问题。"
                                                break
                                            _stream_idle_ticks += 1
                                            if _stream_idle_ticks % 5 == 0:
                                                yield _sse_keepalive()
                                            continue
                                        _stream_idle_ticks = 0
                                        if _ev is _stream_done:
                                            break
                                        yield _sse({"tool_stream": _ev, "tool": fn_name})
                                    if runtime_injected_user_message is not None:
                                        tool_result = json.dumps(
                                            {"success": False, "interrupted": True, "reason": "runtime_control"},
                                            ensure_ascii=False,
                                        )
                                    else:
                                        try:
                                            tool_result = await _tool_task
                                        except Exception as _stream_tool_exc:
                                            logger.exception(
                                                "Agent 流式工具任务异常 tool=%s session_id=%s",
                                                fn_name,
                                                session_id,
                                            )
                                            tool_result = json.dumps(
                                                {
                                                    "success": False,
                                                    "error": f"工具执行失败: {type(_stream_tool_exc).__name__}: {_stream_tool_exc}",
                                                },
                                                ensure_ascii=False,
                                            )
                                else:
                                    _tool_task = asyncio.create_task(
                                        execute_tool(
                                            fn_name,
                                            fn_args,
                                            user,
                                            scope=session_scope,
                                            terminal_scope_id=terminal_scope_id,
                                            default_terminal_slot=preferred_terminal_slot,
                                            session_id=session_id,
                                            ui_locale=_ui_raw,
                                        )
                                    )
                                    _tool_idle_ticks = 0
                                    while True:
                                        try:
                                            # wait_for 会在超时时取消底层 task；用 shield 只给等待本身加超时，
                                            # 避免 IQS search_web 等慢工具在第一轮 0.35s 轮询时被误取消。
                                            tool_result = await asyncio.wait_for(asyncio.shield(_tool_task), timeout=0.35)
                                            break
                                        except asyncio.TimeoutError:
                                            _ctrl = await _consume_runtime_control()
                                            if not _ctrl or _ctrl["action"] not in ("stop", "pause", "supplement"):
                                                _tool_idle_ticks += 1
                                                # 约每 1.75s 一次心跳（0.35 * 5），慢于部分代理/浏览器的空闲判定
                                                if _tool_idle_ticks % 5 == 0:
                                                    yield _sse_keepalive()
                                                continue
                                            _tool_task.cancel()
                                            try:
                                                await _tool_task
                                            except asyncio.CancelledError:
                                                pass
                                            except Exception:
                                                pass
                                            _a = _ctrl["action"]
                                            _m = _ctrl["message"]
                                            yield _sse({"runtime_control": {"action": _a, "accepted": True, "during_tool": fn_name}})
                                            if _a == "stop":
                                                yield "data: [DONE]\n\n"
                                                return
                                            runtime_injected_user_message = _m or "用户要求暂停当前任务，请先回答用户的新问题。"
                                            tool_result = json.dumps(
                                                {"success": False, "interrupted": True, "reason": "runtime_control"},
                                                ensure_ascii=False,
                                            )
                                            break
                                        except Exception as _tool_wait_exc:
                                            # wait_for 在子任务抛错时会直接上抛（非 TimeoutError），必须接住以免 ASGI 流异常截断
                                            logger.exception(
                                                "Agent 非流式工具任务异常 tool=%s session_id=%s",
                                                fn_name,
                                                session_id,
                                            )
                                            tool_result = json.dumps(
                                                {
                                                    "success": False,
                                                    "error": f"工具执行失败: {type(_tool_wait_exc).__name__}: {_tool_wait_exc}",
                                                },
                                                ensure_ascii=False,
                                            )
                                            break
                                try:
                                    result_obj = json.loads(tool_result)
                                    is_success = result_obj.get("success", not result_obj.get("error"))
                                    _poll_s, result_obj = apply_terminal_poll_tool_result(
                                        terminal_poll_batch,
                                        fn_name,
                                        fn_args,
                                        result_obj,
                                        success=is_success,
                                    )
                                    if _poll_s > 0:
                                        max_next_poll_seconds = max(max_next_poll_seconds, _poll_s)
                                    if result_obj is not None:
                                        tool_result = json.dumps(result_obj, ensure_ascii=False)
                                except Exception:
                                    result_obj = {}
                                    is_success = False
                                else:
                                    try:
                                        from services.mcp_result_fetch import enrich_tool_result_json_string

                                        tool_result = await enrich_tool_result_json_string(
                                            tool_result, user, session_id=session_id
                                        )
                                        result_obj = json.loads(tool_result)
                                        is_success = result_obj.get(
                                            "success", not result_obj.get("error")
                                        )
                                    except Exception as _enrich_exc:
                                        logger.debug(
                                            "tool result image enrich skipped tool=%s: %s",
                                            fn_name,
                                            _enrich_exc,
                                        )
                                if fn_name in _AGENT_IRREVERSIBLE_TOOL_NAMES and is_success:
                                    batch_had_irreversible_success = True
                                result_cache_id = None
                                try:
                                    result_cache_id = await _store_tool_result_cache(
                                        db,
                                        user_id=user["id"],
                                        session_id=session_id,
                                        tool_name=fn_name,
                                        tool_args=fn_args_preview,
                                        tool_result=tool_result,
                                        is_success=is_success,
                                        source="ai_agent",
                                        tool_call_id=fn_id,
                                    )
                                    await db.commit()
                                except Exception:
                                    result_cache_id = None
                                yield _sse({
                                    "action": "completed" if is_success else "failed",
                                    "tool": fn_name,
                                    "args": fn_args_preview,
                                    "result_preview": _tool_result_preview(tool_result),
                                    "result_cache_id": result_cache_id,
                                })
                                try:
                                    _rp = _tool_result_preview(tool_result) or ""
                                    pending_tool_trace.append(
                                        {
                                            "type": "tool",
                                            "event": "finished",
                                            "tool": fn_name,
                                            "action": "completed" if is_success else "failed",
                                            "args": fn_args_preview,
                                            "result_preview": _rp[:12_000],
                                        }
                                    )
                                except Exception:
                                    pass
                                if result_obj.get("ui_action"):
                                    _ua = result_obj["ui_action"]
                                    if (
                                        result_obj.get("wait_for_user")
                                        and isinstance(_ua, dict)
                                        and low_interaction_pref
                                        and auto_approve_enabled
                                    ):
                                        _ua["auto_decide_in_seconds"] = 30
                                    yield _sse({"ui_action": _ua})
                                    # 记录到本轮，等 assistant 文本落库时一并嵌入哨兵
                                    try:
                                        if isinstance(_ua, dict):
                                            pending_ui_actions.append(_ua)
                                    except Exception:
                                        pass
                                # 写入 messages 时截断 tool 结果，避免多轮后请求体过大
                                tool_content = await _tool_content_for_llm_with_spill(
                                    user,
                                    session_id,
                                    fn_name,
                                    fn_id,
                                    tool_result,
                                    tool_result_limit,
                                    "standard",
                                )
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": fn_id,
                                    "content": tool_content,
                                })
                                if result_obj.get("wait_for_user"):
                                    # ask_user_choice 等交互工具一旦产生等待用户输入的状态，就挂起当前 SSE。
                                    # 前端通过 runtime-control(choice) 把用户选择插回当前轮，避免模型代替用户选择。
                                    wait_text = (result_obj.get("message") or "").strip()
                                    if result_obj.get("ui_action"):
                                        wait_text = wait_text or "请在选项卡中选择后继续。"
                                    else:
                                        wait_text = wait_text or "请回复你的选择后继续。"
                                    if wait_text:
                                        yield _sse({"content": wait_text})
                                    try:
                                        pending_ui_actions = _dedupe_ui_actions_ask_user_choice_keep_last(
                                            pending_ui_actions
                                        )
                                        _content_to_save = _embed_tool_trace_into_content(
                                            _embed_ui_actions_into_content(wait_text, pending_ui_actions),
                                            pending_tool_trace,
                                        )
                                        pending_ui_actions = []
                                        pending_tool_trace = []
                                        await db.execute(
                                            "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                                            (session_id, _content_to_save[:AI_MESSAGE_SAVE_MAX]),
                                        )
                                        await db.execute(
                                            "UPDATE ai_chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                            (session_id,),
                                        )
                                        await db.commit()
                                    except Exception as e:
                                        logger.warning("保存等待用户选择消息失败: %s", e)
                                    ui_action = result_obj.get("ui_action") if isinstance(result_obj.get("ui_action"), dict) else None
                                    auto_choice_at = (
                                        asyncio.get_running_loop().time() + 30
                                        if (low_interaction_pref and auto_approve_enabled)
                                        else None
                                    )
                                    yield _sse({
                                        "waiting_for_user": {
                                            "kind": "choice",
                                            "auto_decide_in_seconds": 30 if auto_choice_at is not None else None,
                                        }
                                    })
                                    wait_ticks = 0
                                    choice_text = ""
                                    while True:
                                        ctrl = await _consume_runtime_control()
                                        if ctrl:
                                            _a = ctrl["action"]
                                            _m = ctrl["message"]
                                            if _a == "stop":
                                                yield _sse({"runtime_control": {"action": "stop", "accepted": True, "during_wait": "choice"}})
                                                yield "data: [DONE]\n\n"
                                                return
                                            if _a in ("choice", "supplement") and _m:
                                                choice_text = _m
                                                yield _sse({"runtime_control": {"action": "choice", "accepted": True}})
                                                break
                                            if _a == "pause":
                                                yield _sse({"runtime_control": {"action": "pause", "accepted": True, "during_wait": "choice"}})
                                                yield "data: [DONE]\n\n"
                                                return
                                        if auto_choice_at is not None and asyncio.get_running_loop().time() >= auto_choice_at:
                                            choice_text = _default_choice_reply_from_ui_action(ui_action)
                                            if choice_text:
                                                yield _sse({"runtime_control": {"action": "choice", "accepted": True, "auto": True}})
                                                break
                                            auto_choice_at = None
                                        await asyncio.sleep(1)
                                        wait_ticks += 1
                                        if wait_ticks % 5 == 0:
                                            yield _sse_keepalive()
                                    last_user_message = choice_text
                                    pending_user_msg = {"text": choice_text, "saved": False}
                                    await _persist_pending_user_msg()
                                    messages.append({"role": "user", "content": choice_text})
                                    runtime_injected_user_message = None
                                    continue
                                if runtime_injected_user_message is not None:
                                    break
                            _trail_poll = terminal_poll_batch.trailing_send_poll()
                            if _trail_poll > 0:
                                max_next_poll_seconds = max(max_next_poll_seconds, _trail_poll)
                            if runtime_injected_user_message is not None:
                                last_user_message = runtime_injected_user_message
                                pending_user_msg = {"text": runtime_injected_user_message, "saved": False}
                                await _persist_pending_user_msg()
                                messages.append({"role": "user", "content": runtime_injected_user_message})
                                continue
                            if max_next_poll_seconds > 0:
                                _wait_out: list[str] = ["continue", ""]
                                async for _wait_line in _poll_wait_sse(
                                    max_next_poll_seconds,
                                    http_request=http_request,
                                    consume_runtime_control=_consume_runtime_control,
                                    out_status=_wait_out,
                                ):
                                    yield _wait_line
                                if _wait_out[0] == "supplement":
                                    _sup_msg = (_wait_out[1] if len(_wait_out) > 1 else "").strip()
                                    if not _sup_msg:
                                        _sup_msg = "用户补充了新信息，请结合后继续。"
                                    last_user_message = _sup_msg
                                    pending_user_msg = {"text": _sup_msg, "saved": False}
                                    await _persist_pending_user_msg()
                                    messages.append({"role": "user", "content": _sup_msg})
                                    runtime_injected_user_message = None
                                    continue
                                if _wait_out[0] != "continue":
                                    abort_body = _format_poll_wait_aborted_message(
                                        _wait_out[0], _output_locale
                                    )
                                    if abort_body:
                                        yield _sse({"content": abort_body})
                                    try:
                                        await _persist_pending_user_msg()
                                        await db.execute(
                                            "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                                            (session_id, abort_body[:AI_MESSAGE_SAVE_MAX]),
                                        )
                                        await db.execute(
                                            "UPDATE ai_chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                            (session_id,),
                                        )
                                        await db.commit()
                                    except Exception as _wait_persist_exc:
                                        logger.warning(
                                            "保存轮询等待中断消息失败: %s", _wait_persist_exc
                                        )
                                    yield _sse({"stream_status": {"phase": "completed"}})
                                    yield "data: [DONE]\n\n"
                                    return
                            continue

                        content = extract_message_content(msg) or ""
                        if (
                            not round_had_tool_call
                            and not image_blind_tool_retry_used
                            and _messages_indicate_image_attachment_or_degradation(messages)
                            and _looks_like_image_blind_reply(content)
                        ):
                            image_blind_tool_retry_used = True
                            logger.info(
                                "Agent image blind reply intercepted; forcing read_chat_attachment workflow session_id=%s",
                                session_id,
                            )
                            messages.append({"role": "assistant", "content": content or ""})
                            messages.append({
                                "role": "user",
                                "content": (
                                    "你上一条没有真正读取图片。当前任务涉及图片时，禁止回答“看不到/图片太大”后停止。"
                                    "请立即调用 `read_chat_attachment(uuid=..., force_reload=true)` 获取压缩后的 data_url 做粗识别；"
                                    "如果整图仍看不清小目标，请调用 `read_chat_attachment(tile_grid={\"rows\":2,\"cols\":2,\"overlap_ratio\":0.12}, tile_id=1)` "
                                    "并继续按 tile_id 逐块查看，或用 `region` 裁剪局部高清图。"
                                    "完成识别后再回答用户；不要要求用户重新上传。"
                                ),
                            })
                            yield _sse({
                                "content": (
                                    "\n\n*（检测到没有真正读取图片，正在自动改用附件读取/分块识别流程继续分析…）*\n\n"
                                )
                            })
                            continue
                        if (
                            round_had_tool_call
                            and not short_final_retry_used
                            and _looks_like_truncated_assistant_reply(content)
                        ):
                            short_final_retry_used = True
                            logger.warning(
                                "Agent final reply looks truncated after tools: session_id=%s content=%r",
                                session_id,
                                content[:80],
                            )
                            messages.append({"role": "assistant", "content": content or ""})
                            messages.append({
                                "role": "user",
                                "content": (
                                    "你上一条回复明显不完整。请基于刚才已经获得的工具结果，"
                                    "不要重复执行工具，直接用完整中文回答用户：说明结论、关键证据和下一步建议。"
                                ),
                            })
                            continue
                        break

                    if content is None:
                        err_full = f"Agent 达到最大执行轮数 ({agent_max_steps})，已停止"
                        await _persist_assistant_error(
                            err_full,
                            tool_trace=list(pending_tool_trace),
                        )
                        yield _sse({"error": err_full})
                        yield "data: [DONE]\n\n"
                        return

                    # 模型有时只返回 tool_calls 不返回文本，导致 content 为空、前端显示空气泡；
                    # 此时在流式阶段也确实没向前端推过任何正文 token，需补一次兜底文案后再 yield，
                    # 避免最终消息一片空白。
                    if not (content or "").strip():
                        content = _format_empty_assistant_summary(_output_locale)
                        if content:
                            yield _sse({"content": content})

                    # 模型偶尔会从历史上下文复读 `<!-- EDGEOPS:UI_ACTION:v1 ... -->` 哨兵；落库时统一
                    # 剥离，避免下次 loadSession 时把它当 Markdown HTML 注释直接渲染出来。
                    # 流式期间不剥离（哨兵基本不会在新生成中出现，且增量剥离极易把跨 chunk 边界
                    # 切坏），仅作用于即将写入数据库的版本。
                    content = _strip_assistant_embedded_sentinels(content)
                    try:
                        from services.mcp_result_fetch import rewrite_markdown_remote_images_in_text

                        _rewritten = await rewrite_markdown_remote_images_in_text(
                            content, user, session_id=session_id
                        )
                        if _rewritten != content:
                            content = _rewritten
                            if streamed_content_text != content:
                                yield _sse({"content_refresh": content})
                    except Exception as _img_rw_exc:
                        logger.warning(
                            "assistant markdown image rewrite failed sid=%s: %s",
                            session_id,
                            _img_rw_exc,
                        )

                    # 注意：trial_banner 已在流式分支「第一个 content 帧之前」推送过（并已并入
                    # streamed_content_text，因此 msg["content"] 自带 banner），这里不再重复拼接，
                    # 落库时直接保存 content 即可。

                    try:
                        # 用户消息已在流开始前落库；仅在辅助 AI 触发的后续轮次中需要补写
                        if not pending_user_msg["saved"]:
                            await db.execute(
                                "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'user', ?)",
                                (session_id, (pending_user_msg["text"] or "")[:AI_MESSAGE_SAVE_MAX]),
                            )
                            pending_user_msg["saved"] = True
                        # 把本轮采集到的 ui_action（如 ask_user_choice 选择卡）作为哨兵注释
                        # 一并写进 assistant content，前端 loadSession 重渲后可还原；写完即清空。
                        round_had_ui_action = bool(pending_ui_actions)
                        pending_ui_actions = _dedupe_ui_actions_ask_user_choice_keep_last(pending_ui_actions)
                        _content_to_save = _embed_tool_trace_into_content(
                            _embed_ui_actions_into_content(content, pending_ui_actions),
                            pending_tool_trace,
                        )
                        pending_ui_actions = []
                        pending_tool_trace = []
                        await db.execute(
                            "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                            (session_id, _content_to_save[:AI_MESSAGE_SAVE_MAX]),
                        )
                        await db.execute(
                            "UPDATE ai_chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (session_id,),
                        )
                        await db.commit()
                        cur_count = await db.execute(
                            "SELECT COUNT(*) FROM ai_chat_messages WHERE session_id = ? AND role = 'user'",
                            (session_id,),
                        )
                        user_msg_count = (await cur_count.fetchone())[0]
                        if user_msg_count >= 3:
                            rows = await db.execute_fetchall(
                                "SELECT title FROM ai_chat_sessions WHERE id = ?", (session_id,)
                            )
                            if rows and (rows[0]["title"] or "").strip().startswith(EDGEOPS_TEMP_SESSION_PREFIX):
                                asyncio.create_task(
                                    _summarize_session_title(session_id, api_key, base_url, model)
                                )
                    except Exception as e:
                        logger.warning("保存会话消息失败: %s", e)

                    if not assistant_enabled:
                        yield _sse({"stream_status": {"phase": "completed"}})
                        yield "data: [DONE]\n\n"
                        return

                    # 硬拦截：本轮主 AI 已通过 ask_user_choice 等待用户输入，
                    # 或者用户上一条本身就是选择卡作答 + 本轮主 AI 没有任何 tool_call
                    # （= 纯文字 ack 终态），就直接结束。否则辅助 AI 会把这种场景
                    # 误判为「未真正执行」并追加 `请继续`，导致主 AI 重新走一遍
                    # 流程、再问一次同样的问题（如「是否升级」反复弹两遍）。
                    if round_had_ui_action or (
                        _is_user_choice_reply(last_user_message) and not round_had_tool_call
                    ):
                        yield _sse({"stream_status": {"phase": "completed"}})
                        yield "data: [DONE]\n\n"
                        return

                    # 可执行类请求却零 tool_call 且正文像在「将要去干」：同 SSE 内自动续跑一轮，逼模型调工具。
                    if (
                        not round_had_tool_call
                        and force_tool_retries < _MAX_FORCE_TOOL_RETRIES
                        and _looks_like_actionable_user_request(last_user_message)
                        and _reply_suggests_pending_work(content, last_user_message)
                        and not _looks_like_reason_or_newinfo(last_user_message)
                    ):
                        force_tool_retries += 1
                        yield _sse({"stream_status": {"phase": "auto_continuing"}})
                        notice = _format_force_tool_notice(_output_locale)
                        if notice.strip():
                            yield _sse({"content": notice})
                        nudge = _force_tool_nudge_user_message(last_user_message, _output_locale)
                        messages.append({"role": "assistant", "content": content})
                        messages.append({"role": "user", "content": nudge})
                        last_user_message = nudge
                        pending_user_msg = {"text": nudge, "saved": False}
                        await _persist_pending_user_msg()
                        continue

                    assistant_rounds += 1
                    _asst_lang = build_output_language_system_section(
                        last_user_message or "",
                        user_output_locale=_user_ol,
                        global_output_locale=_global_ol,
                        browser_ui_locale=_ui_raw,
                    )
                    assistant_result = await _call_assistant_ai(
                        api_key,
                        base_url,
                        model,
                        last_user_message,
                        content,
                        provider=provider,
                        output_lang_section=_asst_lang,
                    )
                    if assistant_result.get("action") != "continue":
                        yield _sse({"stream_status": {"phase": "completed"}})
                        yield "data: [DONE]\n\n"
                        return
                    continuation = (assistant_result.get("message") or "请继续。")[:500]
                    # 续跑策略：
                    # 1) 用户在问原因/补充信息时：优先停下，先回答，再给是否继续选项；
                    # 2) 用户明确希望减少交互且允许自动审批：尽量自动续跑；
                    # 3) 用户本轮明确说继续：可续跑；
                    # 4) 其它情况：保持“先确认再继续”。
                    asked_reason_or_newinfo = _looks_like_reason_or_newinfo(last_user_message)
                    wants_continue_now = _looks_like_user_wants_continue(last_user_message)
                    should_auto_continue = (not asked_reason_or_newinfo) and (
                        wants_continue_now or (low_interaction_pref and auto_approve_enabled)
                    )
                    if not should_auto_continue:
                        yield _sse({"stream_status": {"phase": "awaiting_user_confirm"}})
                        yield _sse({"assistant_continue": continuation, "requires_user_confirm": True})
                        followup = _build_continue_confirmation_message(continuation, _output_locale)
                        if followup:
                            yield _sse({"content": followup})
                        try:
                            _save_followup = _embed_ui_actions_into_content(followup, [])
                            await db.execute(
                                "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                                (session_id, _save_followup[:AI_MESSAGE_SAVE_MAX]),
                            )
                            await db.execute(
                                "UPDATE ai_chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (session_id,),
                            )
                            await db.commit()
                        except Exception as e:
                            logger.warning("保存继续确认消息失败: %s", e)
                        yield "data: [DONE]\n\n"
                        return
                    yield _sse({"stream_status": {"phase": "auto_continuing"}})
                    yield _sse({"assistant_continue": continuation, "requires_user_confirm": False})
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": continuation})
                    last_user_message = continuation
                    pending_user_msg = {"text": continuation, "saved": False}
                    await _persist_pending_user_msg()

                yield _sse({"stream_status": {"phase": "completed"}})
                yield "data: [DONE]\n\n"
                return
        except httpx.ConnectError as e:
            err_full = f"无法连接 AI 服务: {api_url} ({e})"
            await _persist_assistant_error(err_full, tool_trace=list(pending_tool_trace))
            yield _sse({"error": err_full})
        except httpx.TimeoutException:
            err_full = "AI 服务请求超时，请稍后重试"
            await _persist_assistant_error(err_full, tool_trace=list(pending_tool_trace))
            yield _sse({"error": err_full})
        except Exception as e:
            logger.exception("Agent 异常: %s", e)
            err_full = f"Agent 异常: {type(e).__name__}: {e}"
            await _persist_assistant_error(err_full, tool_trace=list(pending_tool_trace))
            yield _sse({"error": err_full})
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        agent_stream(),
        media_type="text/event-stream",
        headers={
            # 避免 CDN/Nginx 缓冲 SSE，导致慢工具（IQS）期间长时间无字节下发到浏览器而被掐断
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── OpenClaw / 第三方集成：纯后台运维对话（无浏览器控制台上下文，与 POST /api/ai/chat 分离）──
_OPS_INTEGRATION_MODE_RULES = """
## 运行模式（API / OpenClaw 集成 — 纯后台）
- 当前请求**不是**浏览器里的 AI 助手页：没有与用户屏幕同步的 SSH 控制台缓冲，也**不要依赖** `send_to_terminal` 所依赖的、必须由前端先连上的交互式终端。
- 对远程主机执行检查、安装、排障时：**非交互短命令**用 ssh_execute（安装/编译/下载等长任务须 detach+polling）；**需要交互**（sudo 密码、vi/nano、top、多步向导、Ctrl+C）**必须用 ssh_channel_***（create → send → read/has_new → close）。
- **ssh_channel 自管理**：ssh_channel_list(all_open=true) 列全部 open 通道；info 含 IP/别名/用途/主机提示词摘要；close 手工关；默认 **600s** 无读写自动关（Web 浏览器会话创建仍为 300s）。
- **大输出**：read_lines/read_length/dump_output 过大时 spill 到用户文件区，用 read_chat_data 分段读。
- 若工具返回要求用户操作界面（如 ui_action / connect_terminal），在纯集成模式下仍可按工具契约调用，但请在回复中说明「集成通道可能无实时界面」，并尽量用 ssh_channel_* 或 ssh_execute 完成可一步完成的操作。
- 仍须遵守：凡真实执行必须通过 tool_call，禁止仅在文字中声称已执行。
- **不要使用** `ask_user_choice`：本环境无法渲染按钮（即使调用，工具也会返回 `ui_capable=false` 的纯文本回退）；如需用户确认或选择，请直接在回复中以「[A] 选项一 / [B] 选项二 / 请回复 A 或 B」的纯文本形式呈现，并等待用户文字回复。
## 集成模式 · 名词与主机绑定（必读）
- 本会话**未必**已绑定 host_id：若 system 中**没有**「主机级提示词 / 主机 AI 知识」块，**不得**假设默认主机；对用户消息中的每个资产/服务名词，必须先按「名词解析 / 资产与功能映射」用 `search_hosts`、`search_hosts_by_prompt`、`list_host_tags` 等工具对齐，再 `ssh_execute`。
- OpenClaw 等上游若已知 host_id，应在新建会话时传入；若未传入，你须在毛竹（Moso）侧自行解析并在回复中说明解析依据（别名 / 标签 / 主机提示词片段等）。
## 回复呈现（集成下游 / OpenClaw、IM、插件聊天等）
- 调用方 UI **不一定**具备毛竹网页同等能力：**不要默认**对方能渲染 Mermaid、ECharts、内联 HTML/SVG、依赖 JS 的图表或复杂交互面板。
- **优先**使用普通 Markdown：标题、列表、粗体、行内/围栏代码块、列数较少的 GFM 表格；复杂关系用短段落或精简分点/ASCII 示意。
- 若用户消息中已明确要求「仅文字 / 无图表 / OpenClaw」等，须严格遵守，并避免输出仅在富前端才可读的形态。
"""


_OPS_INTEGRATION_TERMINAL_PLACEHOLDER = (
    "（集成模式：无浏览器控制台实时输出缓冲；交互式操作用 ssh_channel_*，非交互短命令用 ssh_execute。）"
)


async def run_ops_integration_chat_complete(
    db,
    user: dict,
    message: str,
    session_id: int | None,
    host_id: int | None,
    skip_secondary_assistant: bool = True,
    attachment_uuids: list[str] | None = None,
    ui_locale: str | None = None,
) -> dict:
    """
    执行一轮集成运维对话，非流式，返回 JSON 友好结构。
    会话 session_scope 固定为 integration，不在网页 AI 助手列表中展示（或与网页会话隔离）。
    """
    msg_in = (message or "").strip()
    if not msg_in:
        return {"success": False, "error": "message 不能为空", "session_id": session_id}

    settings = await _get_user_ai_settings(db, user["id"])
    if not settings.get("ai_agent_max_steps"):
        settings["ai_agent_max_steps"] = str(AGENT_MAX_STEPS)
    if not settings.get("ai_assistant_max_rounds"):
        settings["ai_assistant_max_rounds"] = str(ASSISTANT_MAX_ROUNDS)

    base_url = (settings.get("ai_base_url") or "").strip().rstrip("/")
    if not base_url:
        base_url = (getattr(_config, "AI_BASE_URL", "") or "").strip().rstrip("/")
    if not base_url:
        return {
            "success": False,
            "error": "AI 未配置服务地址 (base_url)，请在系统设置中填写",
            "session_id": session_id,
        }

    provider = _effective_provider(settings, base_url)
    api_key = (settings.get("ai_api_key") or "").strip()
    if require_api_key(provider, api_key) and not api_key:
        system_key, system_base = await _get_system_key_and_base(db)
        if _allow_system_shared_api_key(system_key, system_base, resolved_base_url=base_url):
            trial = await _consume_system_ai_usage(db, user["id"])
            if trial.get("exhausted"):
                return {
                    "success": False,
                    "error": f"系统共享 Key 调用配额已用尽（上限 {trial.get('limit', SYSTEM_AI_USAGE_LIMIT)} 次），请配置自有 API Key 以解除次数限制，或联系管理员重置配额计数",
                    "trial": trial,
                    "session_id": session_id,
                }
            api_key = system_key
            if not (settings.get("ai_base_url") or "").strip() and (system_base or "").strip():
                base_url = (system_base or "").strip().rstrip("/")
        else:
            return {
                "success": False,
                "error": "AI 未配置 API Key",
                "session_id": session_id,
            }

    sid = session_id

    if not sid:
        if host_id is not None:
            try:
                hid = int(host_id)
            except (TypeError, ValueError):
                return {"success": False, "error": "host_id 无效", "session_id": None}
            bind_host = await _get_host_row(hid)
            if not bind_host or not await _can_access_host_with_shares(db, bind_host, user):
                return {
                    "success": False,
                    "error": "主机不存在或无权绑定该主机",
                    "session_id": None,
                }
        temp_title = "集成/API-" + datetime.now().strftime("%Y%m%d%H%M%S")
        await db.execute(
            "INSERT INTO ai_chat_sessions (user_id, host_id, title, session_scope) VALUES (?, ?, ?, 'integration')",
            (user["id"], host_id, temp_title),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        sid = (await cur.fetchone())[0]

    rows = await db.execute_fetchall(
        "SELECT id, host_id, COALESCE(session_prompt, '') AS session_prompt, COALESCE(session_scope, 'default') AS session_scope, COALESCE(low_interaction_mode, 'false') AS low_interaction_mode "
        "FROM ai_chat_sessions WHERE id = ? AND user_id = ?",
        (sid, user["id"]),
    )
    if not rows:
        return {"success": False, "error": "会话不存在", "session_id": sid}
    session_row = dict(rows[0])
    session_host_id = session_row.get("host_id")
    session_prompt = (session_row.get("session_prompt") or "").strip()
    session_scope = (session_row.get("session_scope") or "default").strip().lower()
    session_low_interaction = (session_row.get("low_interaction_mode") or "false").strip().lower() == "true"
    if session_scope != "integration":
        return {
            "success": False,
            "error": "该 session_id 不是集成模式会话，请不传 session_id 以新建集成会话",
            "session_id": sid,
        }

    # 附件注入（与 /api/ai/chat 一致）：把用户已上传的附件绑定到该会话，并追加 📎 附件清单到消息末尾；
    # image 附件同时收集下来，后面内联成视觉多模态 content。
    _ops_image_attach_rows: list[dict] = []
    if attachment_uuids:
        try:
            from api.chat_attachments import (
                load_attachments_for_user as _load_user_attachments,
                build_attachment_message_suffix as _build_attachment_suffix,
                enrich_image_attachment_meta as _enrich_image_attachment_meta,
            )
            attach_rows = await _load_user_attachments(db, user["id"], list(attachment_uuids or []))
            if attach_rows:
                uname = (user.get("username") or "default")
                for r in attach_rows:
                    if (r.get("kind") or "").lower() == "image":
                        _enrich_image_attachment_meta(r, uname)
                    if r.get("session_id") != sid:
                        await db.execute(
                            "UPDATE chat_attachments SET session_id = ? WHERE uuid = ? AND user_id = ?",
                            (sid, r.get("uuid"), user["id"]),
                        )
                await db.commit()
                suffix = _build_attachment_suffix(attach_rows)
                if suffix:
                    msg_in = (msg_in or "") + "\n\n" + suffix
                _ops_image_attach_rows = [
                    r for r in attach_rows if (r.get("kind") or "").lower() == "image"
                ]
        except Exception as _attach_exc:
            logger.warning("集成聊天附件注入失败 sid=%s err=%s", sid, _attach_exc)

    msg_rows = await db.execute_fetchall(
        """SELECT role, content, created_at FROM ai_chat_messages WHERE session_id = ?
           ORDER BY id DESC LIMIT 80""",
        (sid,),
    )
    msg_rows = list(reversed(msg_rows))
    conversation = [
        {
            "role": r["role"],
            "content": _with_history_timestamp(
                _strip_assistant_embedded_sentinels(r["content"] or "") if r["role"] == "assistant" else (r["content"] or ""),
                r["created_at"],
            ),
        }
        for r in msg_rows
    ]
    low_interaction_pref = session_low_interaction or _infer_low_interaction_preference(conversation, msg_in or "")

    if _is_admin_role(user.get("role")):
        host_rows = await db.execute_fetchall(
                    "SELECT id, name, host, port, aliases, remark, host_type, host_version, host_shell, host_package_manager FROM hosts ORDER BY name"
        )
        group_rows = await db.execute_fetchall(
            "SELECT id, name, description, parent_id FROM host_groups ORDER BY COALESCE(parent_id, 0), id"
        )
    else:
        host_rows = await db.execute_fetchall(
                    """SELECT DISTINCT h.id, h.name, h.host, h.port,
                              h.aliases, h.remark, h.host_type, h.host_version, h.host_shell, h.host_package_manager
               FROM hosts h
               LEFT JOIN host_shares hs
                 ON hs.host_id = h.id AND hs.shared_with_user_id = ? AND hs.revoked_at IS NULL
               WHERE h.created_by = ? OR hs.id IS NOT NULL
               ORDER BY h.name""",
            (user["id"], user["id"]),
        )
        group_rows = await db.execute_fetchall(
            "SELECT id, name, description, parent_id FROM host_groups WHERE created_by = ? ORDER BY COALESCE(parent_id, 0), id",
            (user["id"],),
        )
    raw_ctx = int(settings.get("ai_context_size") or "0")
    context_size = _resolve_context_budget_chars(raw_ctx, settings)
    if raw_ctx <= 0 and bool(getattr(_config, "AI_INTEGRATION_ENFORCE_MIN_CONTEXT", True)):
        context_size = max(context_size, int(getattr(_config, "AI_INTEGRATION_CONTEXT_MIN", 48_000)))
    # 识图开关 + 图片 token 预算：先于 context 分配扣减，避免多图挤爆文本分段
    _ops_vision_on = (settings.get("ai_vision_enabled") or "true").strip().lower() != "false"
    _ops_vision_tokens, _ops_vision_chars, _ops_vision_count = _estimate_vision_token_reserve(
        _ops_image_attach_rows, conversation, vision_enabled=_ops_vision_on
    )
    if _ops_vision_chars > 0:
        _ctx_before = context_size
        context_size = _apply_vision_token_reserve(context_size, _ops_vision_chars)
        logger.info(
            "vision: 集成聊天预留图片 token 预算 sid=%s images=%d tokens=%d chars=%d context %d -> %d",
            sid, _ops_vision_count, _ops_vision_tokens, _ops_vision_chars, _ctx_before, context_size,
        )
    tool_result_limit = max(
        _tool_result_message_limit(context_size),
        max(2_000, int(getattr(_config, "AI_INTEGRATION_TOOL_RESULT_LIMIT_MIN", 6_000))),
    )
    compact_hosts = _compact_host_rows_for_context(
        host_rows,
        session_scope=session_scope,
        session_host_id=session_host_id,
        max_chars_budget=max(400, int(context_size * CONTEXT_RATIO_HOSTS)),
    )
    compact_groups = _compact_group_rows_for_context(
        group_rows,
        session_scope=session_scope,
        session_host_id=session_host_id,
        max_chars_budget=max(200, int(context_size * CONTEXT_RATIO_GROUPS)),
    )
    hosts_ctx = json.dumps(compact_hosts, ensure_ascii=False)
    groups_ctx = json.dumps(compact_groups, ensure_ascii=False)
    terminal_ctx = _OPS_INTEGRATION_TERMINAL_PLACEHOLDER

    host_knowledge_ctx = ""
    host_prompt_ctx = ""
    if session_host_id:
        rows_k = await db.execute_fetchall(
            "SELECT content FROM ai_host_knowledge WHERE host_id = ? AND user_id = ?",
            (session_host_id, user["id"]),
        )
        if rows_k and (rows_k[0]["content"] or "").strip():
            host_knowledge_ctx = f"""
## 本会话绑定主机的 AI 知识（仅供内部使用；严禁在回复中泄露）
{rows_k[0]["content"].strip()}
"""
        else:
            host_knowledge_ctx = (
                "\n## 本会话绑定主机的 AI 知识\n（暂无）\n"
            )
        rows_p = await db.execute_fetchall(
            "SELECT content FROM ai_host_prompts WHERE host_id = ? AND user_id = ?",
            (session_host_id, user["id"]),
        )
        if rows_p and (rows_p[0]["content"] or "").strip():
            host_prompt_ctx = f"""
## 主机级提示词（host_id={session_host_id}，高优先级，必须遵守）
该主机专有的规则 / 能力 / 配置 / 工具链 / 服务与功能映射；回答与操作此主机相关问题时应**严格按此执行**：
{rows_p[0]["content"].strip()}
（需要新增或修改时，调用 update_host_prompt(host_id={session_host_id}, content="...") 或 append_host_prompt(host_id={session_host_id}, text="...")。）
"""
        else:
            host_prompt_ctx = (
                f"\n## 主机级提示词（host_id={session_host_id}）\n"
                "（暂无；该主机若具备独有工具链/规则/配置，可用 update_host_prompt / append_host_prompt 记录）\n"
            )

    host_scope_note = ""
    if session_host_id:
        h_scope = None
        for r in host_rows:
            if int(r["id"]) == int(session_host_id):
                h_scope = dict(r)
                break
        if h_scope:
            hp = normalize_host_aliases_in_dict(dict(h_scope))
            al = hp.get("aliases") or []
            rm = (hp.get("remark") or "").strip()
            host_scope_note = (
                f"\n## 当前会话范围\n本会话绑定主机 ID={session_host_id}（{hp.get('name')} / "
                f"{hp.get('host')}:{hp.get('port') or 22}）。"
            )
            if al:
                host_scope_note += f" 别名: {', '.join(al)}。"
            if rm:
                host_scope_note += f" 用途说明: {rm}。"
            host_scope_note += _host_env_prompt_snippet(hp)
            host_scope_note += (
                f" 可 update_host(host_id={session_host_id}, aliases=[...], remark=\"...\") 维护别名与用途。\n"
            )
            host_scope_note += _host_dim_remote_data_processing_rules(session_host_id)
        else:
            host_scope_rows = await db.execute_fetchall(
                "SELECT id, name, host, port, aliases, remark, created_by, host_type, host_version, host_shell, host_package_manager FROM hosts WHERE id = ?",
                (session_host_id,),
            )
            if host_scope_rows:
                h = dict(host_scope_rows[0])
                if await _can_access_host_with_shares(db, h, user):
                    hp = normalize_host_aliases_in_dict(h)
                    al = hp.get("aliases") or []
                    rm = (hp.get("remark") or "").strip()
                    host_scope_note = (
                        f"\n## 当前会话范围\n本会话绑定主机 ID={session_host_id}（{hp.get('name')} / "
                        f"{hp.get('host')}:{hp.get('port') or 22}）。"
                    )
                    if al:
                        host_scope_note += f" 别名: {', '.join(al)}。"
                    if rm:
                        host_scope_note += f" 用途说明: {rm}。"
                    host_scope_note += _host_env_prompt_snippet(hp)
                    host_scope_note += (
                        f" 可 update_host(host_id={session_host_id}, aliases=[...], remark=\"...\") 维护别名与用途。\n"
                    )
                    host_scope_note += _host_dim_remote_data_processing_rules(session_host_id)

    ctx_profile = _infer_context_profile(
        user_message=msg_in,
        session_scope=session_scope,
        session_host_id=session_host_id,
        has_terminal=False,
        context_size=context_size,
    )
    hosts_ctx, groups_ctx, host_knowledge_ctx, terminal_ctx, conversation = _apply_context_limits(
        context_size,
        hosts_ctx,
        groups_ctx,
        host_knowledge_ctx,
        terminal_ctx,
        conversation,
        ctx_profile,
        summarize_old_assistant=bool(getattr(_config, "AI_INTEGRATION_SUMMARIZE_OLD_ASSISTANT", False)),
    )
    logger.debug(
        "integration context budget=%s hosts=%s groups=%s knowledge=%s terminal=%s history_msgs=%s ratios=%s",
        context_size,
        len(hosts_ctx),
        len(groups_ctx),
        len(host_knowledge_ctx),
        len(terminal_ctx),
        len(conversation),
        json.dumps(ctx_profile.get("ratios") or {}, ensure_ascii=False),
    )

    assistant_enabled_raw = (settings.get("ai_assistant_enabled") or "").strip().lower() == "true"
    assistant_enabled = assistant_enabled_raw and not skip_secondary_assistant
    auto_approve_enabled = (settings.get("ai_auto_approve") or "").strip().lower() == "true"

    session_prompt_block = ""
    if session_prompt:
        session_prompt_block = f"""
## 会话级约束（高优先级，必须遵守）
以下为本会话的专用约束，优先于通用规则，请严格遵守：
{session_prompt}
"""
    _ig_ol = await _fetch_setting_value(db, "ai_output_locale")
    _iu_ol = (settings.get("ai_output_locale") or "").strip()
    _iui = (ui_locale or "").strip() or None
    _output_locale, _, _ = resolve_output_language(
        msg_in,
        user_output_locale=_iu_ol,
        global_output_locale=_ig_ol,
        browser_ui_locale=_iui,
    )
    _integ_lang = build_output_language_system_section(
        msg_in,
        user_output_locale=_iu_ol,
        global_output_locale=_ig_ol,
        browser_ui_locale=_iui,
    )
    system_prompt = (settings.get("ai_system_prompt") or "").strip() or _build_system_prompt()
    system_prompt = _sanitize_system_prompt_local_scope(system_prompt, session_scope=session_scope, user=user)
    if session_host_id:
        _integration_host_binding_note = (
            f"\n## 本会话主机绑定状态\n"
            f"本会话已绑定 host_id={session_host_id}；若下方已注入「主机级提示词 / 主机 AI 知识」，**必须优先遵守**。"
            "用户若提到**其它**主机上的名词，仍须对其它 host_id 单独检索（别名 / 标签 / search_hosts_by_prompt），不得混用绑定机约定。\n"
        )
    else:
        _integration_host_binding_note = (
            "\n## 本会话主机绑定状态\n"
            "本会话**尚未绑定** host_id，system 中不会出现某台机的「主机级提示词 / 主机 AI 知识」自动注入块。\n"
            "对用户消息中的主机/服务/环境/业务名词，**必须先**按「名词解析 / 资产与功能映射」完成检索（search_hosts、search_hosts_by_prompt、list_host_tags 等），"
            "确认 host_id 与对应约定后再 ssh_execute。\n"
        )
    full_system = f"""{system_prompt}

{_PROMPT_ENTITY_RESOLUTION_RULES}
{_integ_lang}
{_OPS_INTEGRATION_MODE_RULES}
{_build_html_libs_prompt_section()}
## 当前会话 ID
当前会话 ID 为 {sid}。需要更新会话级约束时可调用 update_session_prompt(session_id={sid}, ...)。
后续注入的历史对话中，每条消息开头会有 `[历史时间: YYYY-MM-DD HH:MM:SS]`。请结合该时间判断信息时效性，越新的内容优先作为当前依据。
{session_prompt_block}
{_integration_host_binding_note}
{host_scope_note}
## 当前主机列表
{hosts_ctx}

## 主机分组
{groups_ctx}
{host_knowledge_ctx}{host_prompt_ctx}
## 控制台上下文（集成模式）
{terminal_ctx}
"""
    if not assistant_enabled:
        full_system += (
            "\n\n**说明**：当前为单次集成轮次（或未启用辅助 AI 续跑），请在本轮内尽量完成可执行步骤并给出简短总结。\n"
        )

    # （`_ops_vision_on` 已在 context_size 解析处统一计算，此处直接复用）
    if not _ops_vision_on:
        full_system += (
            "\n\n**【覆盖说明 · 识图开关关闭】**："
            "用户已在 AI 配置中关闭「支持图像识别」。本轮 user 消息里不会再内联多模态 `image_url` 段，"
            "只会以 📎 附件清单的形式给出图片 uuid。**你必须**主动调用 `read_chat_attachment(uuid=...)` 获取图片的 `data_url`"
            "（默认就会返回 base64 data URL）再作答；**严禁**仅凭元信息（mime/size）就回答「看不清」。\n"
        )
    if low_interaction_pref:
        full_system += (
            "\n\n**协作偏好（来自用户近期指令）**：用户希望减少交互、尽量自动完成。"
            "在不违反安全门禁的前提下，请连续执行可执行步骤；仅在缺少必要条件、执行失败、达到轮次上限、"
            "或用户明确要求停下时再暂停并反馈。若你已经提出需要用户选择或确认的问题，本轮必须等待用户回复，"
            "不要在同一轮继续调用其它工具或代用户选择。"
        )
    if not auto_approve_enabled:
        full_system += (
            "\n\n**工具确认策略（高优先级）**：当前用户未启用「AI 调用工具无需用户确认」。"
            "涉及删除、覆盖、重启、批量修改、写文件、创建/删除账号、凭证/权限变更等可能改变系统状态的操作，"
            "必须先明确征求用户确认；提出选择或确认问题后本轮结束，等待用户回复后才能继续。"
        )
    full_system += _USER_MCP_SYSTEM_HINT
    try:
        from services.user_skills_runtime import build_user_skills_system_section

        _skills_sec = await build_user_skills_system_section(
            user,
            session_scope,
            int(session_host_id) if session_host_id else None,
        )
        if _skills_sec:
            full_system += _skills_sec
    except Exception as _usk_exc:
        logger.debug("集成注入 user skills 失败 sid=%s: %s", sid, _usk_exc)

    try:
        _integ_rt = await get_runtime_context_for_session(
            db,
            sid,
            focus_host_id=int(session_host_id) if session_host_id else None,
            output_locale=_output_locale,
        )
        if _integ_rt:
            full_system += "\n\n" + _integ_rt
    except Exception as _irt_exc:
        logger.debug("集成注入 session_runtime 失败 sid=%s: %s", sid, _irt_exc)

    messages: list[dict] = [{"role": "system", "content": full_system}]
    for m in conversation:
        if m.get("role") in ("user", "assistant"):
            messages.append({"role": m["role"], "content": m.get("content", "")})
    _ops_user_content = _build_user_message_content_with_images(
        msg_in, _ops_image_attach_rows, user, vision_enabled=_ops_vision_on
    )
    messages.append({"role": "user", "content": _ops_user_content})
    try:
        await _inject_history_image_memory(messages, db, user, vision_enabled=_ops_vision_on)
    except Exception as _vision_hist_exc:  # pragma: no cover - 防御性
        logger.warning("vision: 集成聊天历史图片记忆注入失败 sid=%s err=%s", sid, _vision_hist_exc)

    api_url = ensure_chat_completions_url(base_url)
    model = normalize_model(provider, settings.get("ai_model") or "")

    try:
        agent_max_steps = max(1, min(AGENT_MAX_STEPS_CAP, int(settings.get("ai_agent_max_steps") or 0) or AGENT_MAX_STEPS))
    except (TypeError, ValueError):
        agent_max_steps = AGENT_MAX_STEPS
    try:
        assistant_max_rounds = max(1, min(ASSISTANT_MAX_ROUNDS_CAP, int(settings.get("ai_assistant_max_rounds") or 0) or ASSISTANT_MAX_ROUNDS))
    except (TypeError, ValueError):
        assistant_max_rounds = ASSISTANT_MAX_ROUNDS

    tool_scope = "integration"
    exec_scope = "default"
    timeout = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
    headers = prepare_headers(provider, api_key)
    last_user_message = msg_in
    assistant_rounds = 0

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            while assistant_rounds < assistant_max_rounds:
                content: str | None = None
                # 同 agent_stream：拦截「用户上一条来自选择卡作答 + 本轮无 tool_call」
                # 的纯文字 ack 终态，避免辅助 AI 误追问导致同一问题被问两遍。
                round_had_tool_call = False
                for _round_idx in range(agent_max_steps):
                    round_tools = await resolve_chat_tools(
                        get_tools_for_scope(tool_scope, user),
                        tool_scope,
                        user,
                        session_host_id,
                    )
                    # 视觉降级：同 agent_stream，上游报 input_too_long / 图过大 / 不支持图像
                    # 时自动压缩或剥离 image_url 后重试，避免直接把错误冒泡给用户。
                    resp = await _post_chat_with_vision_fallback(
                        client,
                        api_url=api_url,
                        headers=headers,
                        payload={
                            "model": model,
                            "tools": round_tools,
                            "tool_choice": "auto",
                            "max_tokens": _resolve_request_max_tokens(settings),
                            "stream": False,
                        },
                        messages=messages,
                    )
                    if resp.status_code != 200:
                        err_msg = resp.text[:500]
                        try:
                            err_json = resp.json()
                            err_msg = (
                                err_json.get("error", {}).get("message", err_msg)
                                or err_json.get("message", err_msg)
                            )
                        except Exception:
                            pass
                        return {
                            "success": False,
                            "error": f"AI 服务返回错误 (HTTP {resp.status_code}): {err_msg}",
                            "session_id": sid,
                        }

                    result = await asyncio.to_thread(resp.json)
                    msg, tool_calls = parse_chat_response(result)

                    if tool_calls:
                        round_had_tool_call = True
                        full_tool_calls, prepared_tool_calls = await _prepare_tool_calls_for_execution(tool_calls)
                        messages.append({
                            "role": "assistant",
                            "content": extract_message_content(msg) or "",
                            "tool_calls": full_tool_calls,
                        })
                        max_next_poll_seconds = 0
                        terminal_poll_batch = TerminalPollBatchState()
                        for tc, fn_args, fn_args_preview in prepared_tool_calls:
                            fn_name = tc["function"]["name"]
                            fn_id = tc["id"]
                            logger.info(
                                "Integration Agent 执行: %s(%s)",
                                fn_name,
                                json.dumps(fn_args_preview, ensure_ascii=False),
                            )
                            tool_result = await execute_tool(
                                fn_name,
                                fn_args,
                                user,
                                scope=exec_scope,
                                terminal_scope_id=None,
                                default_terminal_slot=None,
                                ui_capable=False,
                                session_id=sid,
                            )
                            try:
                                result_obj = json.loads(tool_result)
                                is_success = result_obj.get("success", not result_obj.get("error"))
                                _poll_s, result_obj = apply_terminal_poll_tool_result(
                                    terminal_poll_batch,
                                    fn_name,
                                    fn_args,
                                    result_obj,
                                    success=is_success,
                                )
                                if _poll_s > 0:
                                    max_next_poll_seconds = max(max_next_poll_seconds, _poll_s)
                                tool_result = json.dumps(result_obj, ensure_ascii=False)
                            except Exception:
                                result_obj = {}
                                is_success = False
                            try:
                                _ = await _store_tool_result_cache(
                                    db,
                                    user_id=user["id"],
                                    session_id=sid,
                                    tool_name=fn_name,
                                    tool_args=fn_args_preview,
                                    tool_result=tool_result,
                                    is_success=is_success,
                                    source="ops_integration",
                                    tool_call_id=fn_id,
                                )
                            except Exception:
                                pass
                            tool_content = await _tool_content_for_llm_with_spill(
                                user,
                                sid,
                                fn_name,
                                fn_id,
                                tool_result,
                                tool_result_limit,
                                "integration",
                            )
                            messages.append({
                                "role": "tool",
                                "tool_call_id": fn_id,
                                "content": tool_content,
                            })
                            if result_obj.get("wait_for_user"):
                                wait_text = (result_obj.get("message") or "").strip() or "请回复你的选择后继续。"
                                try:
                                    await db.commit()
                                    await db.execute(
                                        "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'user', ?)",
                                        (sid, last_user_message[:AI_MESSAGE_SAVE_MAX]),
                                    )
                                    await db.execute(
                                        "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                                        (sid, wait_text[:AI_MESSAGE_SAVE_MAX]),
                                    )
                                    await db.execute(
                                        "UPDATE ai_chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                        (sid,),
                                    )
                                    await db.commit()
                                except Exception as e:
                                    logger.warning("集成会话保存等待用户选择消息失败: %s", e)
                                return {"success": True, "reply": wait_text, "session_id": sid}
                        _trail_poll = terminal_poll_batch.trailing_send_poll()
                        if _trail_poll > 0:
                            max_next_poll_seconds = max(max_next_poll_seconds, _trail_poll)
                        try:
                            await db.commit()
                        except Exception:
                            pass
                        if max_next_poll_seconds > 0:
                            _wait_status = await _poll_wait_blocking(
                                max_next_poll_seconds, session_id=sid
                            )
                            if _wait_status.startswith("supplement:"):
                                _sup_msg = _wait_status.split(":", 1)[1].strip() or "用户补充了新信息，请结合后继续。"
                                last_user_message = _sup_msg
                                messages.append({"role": "user", "content": _sup_msg})
                                continue
                            if _wait_status != "continue":
                                abort_body = _format_poll_wait_aborted_message(
                                    _wait_status, _output_locale
                                )
                                try:
                                    await db.execute(
                                        "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                                        (sid, abort_body[:AI_MESSAGE_SAVE_MAX]),
                                    )
                                    await db.execute(
                                        "UPDATE ai_chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                        (sid,),
                                    )
                                    await db.commit()
                                except Exception as _int_wait_exc:
                                    logger.warning(
                                        "集成会话保存等待中断消息失败: %s", _int_wait_exc
                                    )
                                return {
                                    "success": True,
                                    "reply": abort_body,
                                    "session_id": sid,
                                    "interrupted": True,
                                }
                        continue

                    content = extract_message_content(msg) or ""
                    break

                if content is None:
                    return {
                        "success": False,
                        "error": f"Agent 达到最大执行步数 ({agent_max_steps})，已停止",
                        "session_id": sid,
                    }

                if not (content or "").strip():
                    content = "（已按上述工具执行完成；若需继续可发送下一条消息。）"

                # 模型偶尔会复读历史中的 `<!-- EDGEOPS:UI_ACTION:v1 ... -->` 哨兵注释，
                # 直接返回会污染集成方的文本展示；统一剥离。
                content = _strip_assistant_embedded_sentinels(content)

                try:
                    await db.execute(
                        "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'user', ?)",
                        (sid, last_user_message[:AI_MESSAGE_SAVE_MAX]),
                    )
                    await db.execute(
                        "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                        (sid, content[:AI_MESSAGE_SAVE_MAX]),
                    )
                    await db.execute(
                        "UPDATE ai_chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (sid,),
                    )
                    await db.commit()
                    cur_count = await db.execute(
                        "SELECT COUNT(*) FROM ai_chat_messages WHERE session_id = ? AND role = 'user'",
                        (sid,),
                    )
                    user_msg_count = (await cur_count.fetchone())[0]
                    if user_msg_count >= 3:
                        rows_t = await db.execute_fetchall(
                            "SELECT title FROM ai_chat_sessions WHERE id = ?", (sid,)
                        )
                        tit = (rows_t[0]["title"] or "").strip() if rows_t else ""
                        if tit.startswith("集成/API-"):
                            asyncio.create_task(
                                _summarize_session_title(sid, api_key, base_url, model)
                            )
                except Exception as e:
                    logger.warning("集成会话保存消息失败: %s", e)

                if not assistant_enabled:
                    return {"success": True, "reply": content, "session_id": sid}

                # 与 agent_stream 同源的硬拦截：用户上一条若是选择卡作答（`[A] xxx`
                # 等格式）且本轮主 AI 无 tool_call，直接返回，避免辅助 AI 误追问。
                if _is_user_choice_reply(last_user_message) and not round_had_tool_call:
                    return {"success": True, "reply": content, "session_id": sid}

                assistant_rounds += 1
                _asst_lang_i = build_output_language_system_section(
                    last_user_message or "",
                    user_output_locale=_iu_ol,
                    global_output_locale=_ig_ol,
                    browser_ui_locale=_iui,
                )
                assistant_result = await _call_assistant_ai(
                    api_key,
                    base_url,
                    model,
                    last_user_message,
                    content,
                    provider=provider,
                    output_lang_section=_asst_lang_i,
                )
                if assistant_result.get("action") != "continue":
                    return {"success": True, "reply": content, "session_id": sid}
                continuation = (assistant_result.get("message") or "请继续。")[:500]
                asked_reason_or_newinfo = _looks_like_reason_or_newinfo(last_user_message)
                wants_continue_now = _looks_like_user_wants_continue(last_user_message)
                should_auto_continue = (not asked_reason_or_newinfo) and (
                    wants_continue_now or (low_interaction_pref and auto_approve_enabled)
                )
                if not should_auto_continue:
                    followup = _build_continue_confirmation_message(continuation, None)
                    return {"success": True, "reply": content + "\n\n" + followup, "session_id": sid}
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": continuation})
                last_user_message = continuation

            return {
                "success": True,
                "reply": content or "",
                "session_id": sid,
                "note": "已达到辅助 AI 最大轮次上限",
            }
    except httpx.ConnectError as e:
        return {
            "success": False,
            "error": f"无法连接 AI 服务: {api_url} ({e})",
            "session_id": sid,
        }
    except httpx.TimeoutException:
        return {"success": False, "error": "AI 服务请求超时", "session_id": sid}
    except Exception as e:
        logger.exception("Integration Agent 异常: %s", e)
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "session_id": sid,
        }

