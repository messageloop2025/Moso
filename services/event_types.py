"""Agent Event 类型常量：进程内事件总线的标准化事件名。"""
from __future__ import annotations


class AgentEvent:
    """Agent 生命周期事件（domain:agent:action 命名）"""
    # Step 级别
    STEP_START = "agent:step:start"
    STEP_END = "agent:step:end"

    # Tool 级别（升级自 preToolUse / postToolUse / postToolUseFailure）
    TOOL_PRE = "agent:tool:pre"
    TOOL_POST = "agent:tool:post"
    TOOL_ERROR = "agent:tool:error"

    # LLM 调用
    LLM_PRE = "agent:llm:pre"
    LLM_POST = "agent:llm:post"
    LLM_CHUNK = "agent:llm:chunk"

    # 逐 Token 流式
    TOKEN = "agent:token"

    # Turn 级别（每轮 user/assistant 交互）
    TURN_START = "agent:turn:start"
    TURN_END = "agent:turn:end"

    # Agent 全局生命周期
    START = "agent:start"
    COMPLETE = "agent:complete"
    ERROR = "agent:error"
    CANCEL = "agent:cancel"
    PAUSE = "agent:pause"
    RESUME = "agent:resume"


class SessionEvent:
    """会话生命周期事件"""
    CREATE = "session:create"
    DELETE = "session:delete"


class MCPEvent:
    """MCP 工具执行事件"""
    PRE = "mcp:pre"


# 向后兼容事件名映射：旧 hooks.json 事件 → 新事件常量
_LEGACY_EVENT_MAP: dict[str, str] = {
    "preToolUse": AgentEvent.TOOL_PRE,
    "postToolUse": AgentEvent.TOOL_POST,
    "postToolUseFailure": AgentEvent.TOOL_ERROR,
    "sessionStart": SessionEvent.CREATE,
    "sessionEnd": SessionEvent.DELETE,
    "beforeMCPExecution": MCPEvent.PRE,
}

# 所有新事件名列表（供 UI 使用）
ALL_EVENTS: list[str] = [
    AgentEvent.STEP_START,
    AgentEvent.STEP_END,
    AgentEvent.TOOL_PRE,
    AgentEvent.TOOL_POST,
    AgentEvent.TOOL_ERROR,
    AgentEvent.LLM_PRE,
    AgentEvent.LLM_POST,
    AgentEvent.LLM_CHUNK,
    AgentEvent.TOKEN,
    AgentEvent.TURN_START,
    AgentEvent.TURN_END,
    AgentEvent.START,
    AgentEvent.COMPLETE,
    AgentEvent.ERROR,
    AgentEvent.CANCEL,
    AgentEvent.PAUSE,
    AgentEvent.RESUME,
    SessionEvent.CREATE,
    SessionEvent.DELETE,
    MCPEvent.PRE,
]

# 事件分组（供 UI 下拉菜单/筛选使用）
EVENT_GROUPS: dict[str, list[str]] = {
    "Agent 生命周期": [
        AgentEvent.START,
        AgentEvent.COMPLETE,
        AgentEvent.ERROR,
        AgentEvent.CANCEL,
        AgentEvent.PAUSE,
        AgentEvent.RESUME,
    ],
    "Step / Turn": [
        AgentEvent.STEP_START,
        AgentEvent.STEP_END,
        AgentEvent.TURN_START,
        AgentEvent.TURN_END,
    ],
    "Tool 调 用": [
        AgentEvent.TOOL_PRE,
        AgentEvent.TOOL_POST,
        AgentEvent.TOOL_ERROR,
    ],
    "LLM / 流式": [
        AgentEvent.LLM_PRE,
        AgentEvent.LLM_POST,
        AgentEvent.LLM_CHUNK,
        AgentEvent.TOKEN,
    ],
    "会话": [
        SessionEvent.CREATE,
        SessionEvent.DELETE,
    ],
    "MCP": [
        MCPEvent.PRE,
    ],
}


def normalize_event_name(event: str) -> str:
    """将旧事件名映射为新事件名；已是新事件名则原样返回。"""
    return _LEGACY_EVENT_MAP.get(event, event)


def is_legacy_event(event: str) -> bool:
    """判断是否为旧 hooks.json 事件名。"""
    return event in _LEGACY_EVENT_MAP
