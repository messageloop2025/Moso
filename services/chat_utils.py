"""聊天历史提取：区分「用户要求 + 助手指令」与「程序输出」，供生成提示词/经验与详情查看。"""

from __future__ import annotations

import base64 as _b64
import json as _json
import re as _re

# 助手消息只保留「指令」部分的最大长度
SUMMARY_ASSISTANT_INSTRUCTION_MAX = 500
# 助手内容中视为「程序输出开始」的标记，此前视为指令
SUMMARY_LOG_START_MARKERS = (
    "执行结果", "输出如下", "运行结果", "命令输出", "终端输出",
    "```\n", "```", "stdout", "stderr", "日志如下", "输出：",
)

# 解码 TOOL_TRACE 时的体积控制（给 AI 自查用，勿把完整 spill 塞回）
TOOL_TRACE_DECODE_MAX_STEPS = 80
TOOL_TRACE_DECODE_PREVIEW_CHARS = 800
TOOL_TRACE_DECODE_ARGS_CHARS = 400

# UI Action / TOOL_TRACE / RUN_STATS 哨兵（与 api/ai_agent.py 同源）
_UI_ACTION_SENTINEL_RE = _re.compile(r"<!--\s*EDGEOPS:UI_ACTION:v1\s+[A-Za-z0-9+/=]+\s*-->")
_TOOL_TRACE_SENTINEL_RE = _re.compile(r"<!--\s*EDGEOPS:TOOL_TRACE:v1\s+([A-Za-z0-9+/=]+)\s*-->")
_RUN_STATS_SENTINEL_RE = _re.compile(r"<!--\s*EDGEOPS:RUN_STATS:v1\s+[A-Za-z0-9+/=]+\s*-->")


def strip_ui_action_sentinels(raw: str) -> str:
    """去掉 assistant 内容中嵌入的 ui_action 哨兵注释，避免污染摘要 / LLM 上下文。"""
    if not raw:
        return raw or ""
    return _UI_ACTION_SENTINEL_RE.sub("", raw)


def strip_tool_trace_sentinels(raw: str) -> str:
    """去掉 TOOL_TRACE 哨兵注释。"""
    if not raw:
        return raw or ""
    return _TOOL_TRACE_SENTINEL_RE.sub("", raw)


def strip_run_stats_sentinels(raw: str) -> str:
    """去掉 RUN_STATS 哨兵注释。"""
    if not raw:
        return raw or ""
    return _RUN_STATS_SENTINEL_RE.sub("", raw)


def strip_assistant_embedded_sentinels(raw: str) -> str:
    """剥离 UI_ACTION + TOOL_TRACE + RUN_STATS，保留可读助手正文。"""
    text = strip_run_stats_sentinels(strip_tool_trace_sentinels(strip_ui_action_sentinels(raw or "")))
    return (text or "").rstrip()


def extract_tool_trace_steps(raw: str, *, max_steps: int = TOOL_TRACE_DECODE_MAX_STEPS) -> list[dict]:
    """从 assistant 落库 content 中解码 TOOL_TRACE 哨兵为可读步骤列表。

    每步常见字段：type/tool/event/action/args/result_preview（与落库轨迹一致）。
    """
    if not raw or "EDGEOPS:TOOL_TRACE:v1" not in raw:
        return []
    steps: list[dict] = []
    for m in _TOOL_TRACE_SENTINEL_RE.finditer(raw):
        b64 = (m.group(1) or "").strip()
        if not b64:
            continue
        try:
            meta = _json.loads(_b64.b64decode(b64).decode("utf-8"))
        except Exception:
            continue
        chunk = meta.get("steps") if isinstance(meta, dict) else None
        if not isinstance(chunk, list):
            continue
        for step in chunk:
            if not isinstance(step, dict):
                continue
            item = dict(step)
            # 截断过长字段，避免详情工具一次返回过大
            args = item.get("args")
            if isinstance(args, str) and len(args) > TOOL_TRACE_DECODE_ARGS_CHARS:
                item["args"] = args[:TOOL_TRACE_DECODE_ARGS_CHARS] + "…"
            elif args is not None and not isinstance(args, str):
                try:
                    args_s = _json.dumps(args, ensure_ascii=False)
                except Exception:
                    args_s = str(args)
                if len(args_s) > TOOL_TRACE_DECODE_ARGS_CHARS:
                    args_s = args_s[:TOOL_TRACE_DECODE_ARGS_CHARS] + "…"
                item["args"] = args_s
            rp = item.get("result_preview")
            if isinstance(rp, str) and len(rp) > TOOL_TRACE_DECODE_PREVIEW_CHARS:
                item["result_preview"] = rp[:TOOL_TRACE_DECODE_PREVIEW_CHARS] + "…"
            steps.append(item)
            if max_steps > 0 and len(steps) >= max_steps:
                return steps
    return steps


def assistant_content_for_chat_detail(raw: str, *, include_tool_results: bool) -> dict:
    """组装 get_session_chat_detail 单条助手消息。

    include_tool_results=True 时：正文剥哨兵 + 附带解码后的 tool_trace。
    False 时：与 summary 一致，仅指令摘要。
    """
    if not include_tool_results:
        return {
            "content": assistant_content_for_summary(raw),
            "tool_trace": [],
            "tool_trace_step_count": 0,
        }
    tool_trace = extract_tool_trace_steps(raw or "")
    content = strip_assistant_embedded_sentinels(raw or "").strip()
    return {
        "content": content,
        "tool_trace": tool_trace,
        "tool_trace_step_count": len(tool_trace),
    }


def assistant_content_for_summary(raw: str) -> str:
    """仅保留助手的「指令/决策」部分，去掉程序输出日志。供生成会话提示词、经验或 get_session_operations 使用。"""
    raw = strip_assistant_embedded_sentinels(raw or "")
    if not (raw or "").strip():
        return "助手已执行相关操作"
    text = (raw or "").strip()
    first_log = len(text)
    for marker in SUMMARY_LOG_START_MARKERS:
        idx = text.find(marker)
        if idx >= 0:
            first_log = min(first_log, idx)
    if first_log <= 0:
        return "助手已执行相关操作"
    instruction = text[:first_log].strip()
    if not instruction:
        return "助手已执行相关操作"
    truncated = len(text) > first_log
    instruction = instruction[:SUMMARY_ASSISTANT_INSTRUCTION_MAX]
    if len(instruction) >= SUMMARY_ASSISTANT_INSTRUCTION_MAX:
        truncated = True
    if truncated:
        instruction = instruction.rstrip()
        if instruction and not instruction.endswith("…") and not instruction.endswith("。"):
            instruction += "…"
    return instruction or "助手已执行相关操作"
