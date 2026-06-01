"""聊天历史提取：区分「用户要求 + 助手指令」与「程序输出」，供生成提示词/经验与详情查看。"""

import re as _re

# 助手消息只保留「指令」部分的最大长度
SUMMARY_ASSISTANT_INSTRUCTION_MAX = 500
# 助手内容中视为「程序输出开始」的标记，此前视为指令
SUMMARY_LOG_START_MARKERS = (
    "执行结果", "输出如下", "运行结果", "命令输出", "终端输出",
    "```\n", "```", "stdout", "stderr", "日志如下", "输出：",
)

# UI Action 哨兵注释（与 api/ai_agent.py 同源），用于持久化 ask_user_choice 等卡片
_UI_ACTION_SENTINEL_RE = _re.compile(r"<!--\s*EDGEOPS:UI_ACTION:v1\s+[A-Za-z0-9+/=]+\s*-->")


def strip_ui_action_sentinels(raw: str) -> str:
    """去掉 assistant 内容中嵌入的 ui_action 哨兵注释，避免污染摘要 / LLM 上下文。"""
    if not raw:
        return raw or ""
    return _UI_ACTION_SENTINEL_RE.sub("", raw)


def assistant_content_for_summary(raw: str) -> str:
    """仅保留助手的「指令/决策」部分，去掉程序输出日志。供生成会话提示词、经验或 get_session_operations 使用。"""
    raw = strip_ui_action_sentinels(raw or "")
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
