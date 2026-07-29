"""Event Hook 引擎：整合 hooks.json + DB event_rules 表 + 多 Skill 聚合。"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from database import get_db
from services.event_bus import event_bus
from services.event_types import (
    AgentEvent,
    MCPEvent,
    SessionEvent,
    normalize_event_name,
)

logger = logging.getLogger("edgeops.event_hook_engine")


def _parse_matcher_list(raw: str | None) -> list[str]:
    s = (raw or "").strip()
    if not s:
        return ["*"]
    parts: list[str] = []
    for line in s.replace(",", "\n").splitlines():
        p = line.strip()
        if p:
            parts.append(p)
    return parts or ["*"]


def _tool_matches(matcher: list[str] | str | None, tool_name: str) -> bool:
    import fnmatch
    patterns = matcher if isinstance(matcher, list) else _parse_matcher_list(str(matcher) if matcher else None)
    name = (tool_name or "").strip()
    for pat in patterns:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


async def load_event_rules_from_db(
    user_id: int | None = None,
    session_id: int | None = None,
) -> list[dict[str, Any]]:
    """从 event_rules 表加载启用的规则。"""
    db = await get_db()
    rows: list[dict[str, Any]] = []
    try:
        sql = "SELECT * FROM event_rules WHERE enabled = 1"
        params: list[Any] = []
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY priority DESC, id ASC"
        rows = [dict(r) for r in (await db.execute_fetchall(sql, tuple(params)) or [])]
    except Exception:
        pass
    return rows


def load_hooks_json_skill(skill_dir: Path) -> dict[str, Any]:
    """加载 Skill 目录下的 hooks.json。"""
    p = skill_dir / "hooks.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("hooks.json 解析失败 %s: %s", p, e)
        return {}


async def resolve_hook_decision(
    *,
    event: str,
    tool_name: str = "",
    args: dict[str, Any] | None = None,
    chat_mode: str = "normal",
    hook_skills: list[dict[str, Any]] | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """综合决策：DB event_rules + Skill hooks.json + DB matcher。

    聚合策略：deny > ask > allow（fail-open）。
    返回 {"decision", "reason", "source", "event"}
    """
    _ = (args, chat_mode)
    ev = normalize_event_name(event)

    deny_reason: str = ""
    ask_reason: str = ""

    # === 1. DB event_rules 表 ===
    try:
        db_rules = await load_event_rules_from_db(user_id=user_id)
        for rule in db_rules:
            rule_event = normalize_event_name(str(rule.get("event_name", "")))
            if rule_event != ev:
                continue
            matcher = str(rule.get("matcher") or "*")
            if tool_name and not _tool_matches(matcher, tool_name):
                continue
            decision = str(rule.get("decision") or "allow").strip().lower()
            if decision == "deny":
                return {
                    "decision": "deny",
                    "reason": str(rule.get("reason") or "event_rule_deny")[:500],
                    "source": "event_rules",
                    "event": ev,
                }
            if decision == "ask":
                ask_reason = ask_reason or str(rule.get("reason") or "event_rule_ask")
    except Exception:
        pass

    # === 2. Skill hooks.json（仅 hooks_enabled=True 时生效） ===
    for sk in (hook_skills or []):
        try:
            if not sk.get("hooks_enabled"):
                continue
            skill_dir_raw = sk.get("skill_dir") or sk.get("dir") or sk.get("path") or ""
            hj: dict[str, Any] = {}
            if skill_dir_raw:
                hj = load_hooks_json_skill(Path(str(skill_dir_raw)))

            block = hj.get(event) or hj.get(ev)
            if not block:
                from services.event_types import _LEGACY_EVENT_MAP as _lem
                _rev_map = {v: k for k, v in _lem.items()}
                _legacy = _rev_map.get(event) or _rev_map.get(ev)
                if _legacy:
                    block = hj.get(_legacy)
            if not block:
                continue

            # 列表规则
            if isinstance(block, list):
                for rule in block:
                    if not isinstance(rule, dict):
                        continue
                    m = str(rule.get("matcher") or rule.get("tools") or "*")
                    if tool_name and not _tool_matches(m, tool_name):
                        continue
                    dec = str(rule.get("decision") or rule.get("action") or "allow").strip().lower()
                    if dec == "deny":
                        return {
                            "decision": "deny",
                            "reason": str(rule.get("reason") or rule.get("message") or "")[:500],
                            "source": "hooks.json",
                            "event": ev,
                        }
                    if dec == "ask":
                        ask_reason = ask_reason or str(rule.get("reason") or rule.get("message") or "")
                    break
                continue

            # 单条规则
            if isinstance(block, dict) and block:
                m = str(block.get("matcher") or "*")
                if tool_name and not _tool_matches(m, tool_name):
                    continue
                dec = str(block.get("decision") or block.get("action") or "allow").strip().lower()
                if dec == "deny":
                    return {
                        "decision": "deny",
                        "reason": str(block.get("reason") or block.get("message") or "")[:500],
                        "source": "hooks.json",
                        "event": ev,
                    }
                if dec == "ask":
                    ask_reason = ask_reason or str(block.get("reason") or block.get("message") or "")
        except Exception as e:
            logger.debug("Skill hook resolve 异常: %s", e)
            continue

    # === 3. DB matcher（仅 preToolUse / agent:tool:pre 时生效） ===
    if event in ("preToolUse", AgentEvent.TOOL_PRE):
        for sk in (hook_skills or []):
            matcher = sk.get("pre_tool_use_matcher") or sk.get("preToolUseMatcher") or ""
            if not matcher:
                continue
            if not _tool_matches(matcher, tool_name):
                continue
            dec = str(sk.get("pre_tool_use_decision") or sk.get("preToolUseDecision") or "ask").strip().lower()
            if dec == "deny":
                return {
                    "decision": "deny",
                    "reason": f"preToolUse matcher deny (Skill: {sk.get('name', '')})",
                    "source": "db_matcher",
                    "event": ev,
                }
            if dec == "ask":
                ask_reason = ask_reason or f"preToolUse matcher: {sk.get('name', '')}"

    if ask_reason:
        return {
            "decision": "ask",
            "reason": ask_reason[:500],
            "source": "aggregate",
            "event": ev,
        }

    return {
        "decision": "allow",
        "reason": "all_allow",
        "source": "default",
        "event": ev,
    }


async def apply_post_tool_hook_decision(
    result_obj: dict[str, Any] | None,
    hook_dec: dict[str, Any] | None,
    *,
    redact_keys: tuple[str, ...] = ("output", "stdout", "result", "data", "content"),
) -> tuple[dict[str, Any], bool]:
    """将 postToolUse / postToolUseFailure 决策应用到工具结果。

    deny：标记失败、附原因，省略大段输出。
    返回 (result_obj, is_success)。
    """
    obj = dict(result_obj or {})
    dec = hook_dec or {}
    decision = str(dec.get("decision") or "allow").strip().lower()
    if decision != "deny":
        if dec and dec.get("decision") not in (None, "", "allow"):
            obj["hook_post"] = dec
        return obj, bool(obj.get("success", not obj.get("error")))

    reason = str(dec.get("reason") or "").strip()
    note = "Event Hook postToolUse 拒绝采纳本次工具结果"
    if reason:
        note += f"：{reason}"
    obj["hook_post"] = dec
    obj["hook_post_denied"] = True
    obj["success"] = False
    prev_err = str(obj.get("error") or "").strip()
    obj["error"] = f"{prev_err} [{note}]".strip() if prev_err else note
    for k in redact_keys:
        if k in obj and obj[k] not in (None, "", {}, []):
            obj[k] = "[已由 Event Hook postToolUse 拒绝采纳，正文已省略]"
    return obj, False


# 启动时注册核心事件监听器（预留扩展点）
def _on_agent_error(event: str, **payload: Any) -> None:
    """默认Agent错误处理器：记录日志。"""
    trace_ctx = payload.get("trace_ctx")
    error = payload.get("error")
    logger.warning("Agent error [%s]: %s", getattr(trace_ctx, "session_id", "?"), error)


event_bus.on(AgentEvent.ERROR, _on_agent_error)


# —— 运行时监听器（记录 Agent 执行轨迹、统计等）——

def _on_step_start(event: str, **payload: Any) -> None:
    """记录每步开始。"""
    trace_ctx = payload.get("trace_ctx")
    round_idx = payload.get("round_idx", "?")
    logger.debug("Agent step start [%s] round=%s", getattr(trace_ctx, "session_id", "?"), round_idx)


def _on_step_end(event: str, **payload: Any) -> None:
    """记录每步结束。"""
    trace_ctx = payload.get("trace_ctx")
    had_tool = payload.get("had_tool_call", False)
    logger.debug("Agent step end [%s] had_tool=%s", getattr(trace_ctx, "session_id", "?"), had_tool)


def _on_tool_pre(event: str, **payload: Any) -> None:
    """工具执行前记录。"""
    trace_ctx = payload.get("trace_ctx")
    tool_name = payload.get("tool_name", "?")
    logger.info("Agent tool pre [%s] tool=%s", getattr(trace_ctx, "session_id", "?"), tool_name)


def _on_tool_post(event: str, **payload: Any) -> None:
    """工具执行后记录。"""
    trace_ctx = payload.get("trace_ctx")
    tool_name = payload.get("tool_name", "?")
    logger.info("Agent tool post [%s] tool=%s success", getattr(trace_ctx, "session_id", "?"), tool_name)


def _on_agent_lifecycle(event: str, **payload: Any) -> None:
    """记录 Agent 生命周期事件。"""
    trace_ctx = payload.get("trace_ctx")
    reason = payload.get("reason", "")
    logger.info("Agent lifecycle [%s] event=%s reason=%s", getattr(trace_ctx, "session_id", "?"), event, reason)


# 注册运行时监听器
event_bus.on(AgentEvent.STEP_START, _on_step_start)
event_bus.on(AgentEvent.STEP_END, _on_step_end)
event_bus.on(AgentEvent.TOOL_PRE, _on_tool_pre)
event_bus.on(AgentEvent.TOOL_POST, _on_tool_post)
event_bus.on(AgentEvent.TOOL_ERROR, _on_tool_post)
event_bus.on(AgentEvent.START, _on_agent_lifecycle)
event_bus.on(AgentEvent.COMPLETE, _on_agent_lifecycle)


# —— Turn / LLM 监听器（Phase 2 → 已激活）——

def _on_turn_start(event: str, **payload: Any) -> None:
    trace_ctx = payload.get("trace_ctx")
    round_idx = payload.get("round_idx", "?")
    logger.debug("Agent turn start [%s] round=%s", getattr(trace_ctx, "session_id", "?"), round_idx)


def _on_turn_end(event: str, **payload: Any) -> None:
    trace_ctx = payload.get("trace_ctx")
    round_idx = payload.get("round_idx", "?")
    logger.debug("Agent turn end [%s] round=%s", getattr(trace_ctx, "session_id", "?"), round_idx)


def _on_llm_pre(event: str, **payload: Any) -> None:
    trace_ctx = payload.get("trace_ctx")
    round_idx = payload.get("round_idx", "?")
    logger.debug("Agent LLM pre [%s] round=%s", getattr(trace_ctx, "session_id", "?"), round_idx)


def _on_llm_post(event: str, **payload: Any) -> None:
    trace_ctx = payload.get("trace_ctx")
    finish_reason = payload.get("finish_reason", "")
    logger.debug("Agent LLM post [%s] finish=%s", getattr(trace_ctx, "session_id", "?"), finish_reason)


def _on_llm_chunk(event: str, **payload: Any) -> None:
    trace_ctx = payload.get("trace_ctx")
    kind = payload.get("chunk_kind", "")
    # 高频事件，不单独打日志；由注册方按需处理


def _on_token(event: str, **payload: Any) -> None:
    trace_ctx = payload.get("trace_ctx")
    usage = payload.get("usage", {})
    logger.debug(
        "Agent token [%s] prompt=%s completion=%s total=%s",
        getattr(trace_ctx, "session_id", "?"),
        usage.get("prompt_tokens", "?"),
        usage.get("completion_tokens", "?"),
        usage.get("total_tokens", "?"),
    )


event_bus.on(AgentEvent.TURN_START, _on_turn_start)
event_bus.on(AgentEvent.TURN_END, _on_turn_end)
event_bus.on(AgentEvent.LLM_PRE, _on_llm_pre)
event_bus.on(AgentEvent.LLM_POST, _on_llm_post)
event_bus.on(AgentEvent.LLM_CHUNK, _on_llm_chunk)
event_bus.on(AgentEvent.TOKEN, _on_token)
