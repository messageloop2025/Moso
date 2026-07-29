"""User Skills Hooks：preToolUse / postToolUse / postToolUseFailure / sessionStart|End / beforeMCPExecution。
（向后兼容层：内部委托给 event_hook_engine.py 的新事件引擎。）"""
from __future__ import annotations

import fnmatch
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("edgeops.user_skills_hooks")

HOOK_EVENTS = frozenset({
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "sessionStart",
    "sessionEnd",
    "beforeMCPExecution",
})


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


def tool_matches(matcher: str | list[str] | None, tool_name: str) -> bool:
    patterns = matcher if isinstance(matcher, list) else _parse_matcher_list(matcher)
    name = (tool_name or "").strip()
    for pat in patterns:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def load_hooks_json(skill_dir: Path) -> dict[str, Any]:
    p = skill_dir / "hooks.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("hooks.json 解析失败 %s: %s", p, e)
        return {}


def _normalize_decision(raw: str | None, default: str = "allow") -> str:
    d = str(raw or default).strip().lower()
    if d not in ("allow", "deny", "ask"):
        return default
    return d


def resolve_hook_event(
    *,
    event: str,
    hooks_enabled: bool,
    matcher: str | None,
    hooks_json: dict[str, Any] | None,
    tool_name: str = "",
    args: dict | None = None,
    chat_mode: str = "normal",
    default_decision: str = "allow",
    db_matcher_decision: str = "ask",
) -> dict[str, Any]:
    """
    通用 Hook 解析。返回 {decision, reason, source, fail_open, event}.
    未知/失败 → fail-open allow。
    DB matcher（无 hooks.json 规则）命中时使用 db_matcher_decision（allow/deny/ask）。
    """
    _ = (args, chat_mode)
    ev = (event or "").strip()
    if ev not in HOOK_EVENTS:
        return {
            "decision": "allow",
            "reason": "unknown_event",
            "source": "skip",
            "fail_open": True,
            "event": ev,
        }
    hj = hooks_json or {}
    block = hj.get(ev) or hj.get(
        {
            "preToolUse": "pre_tool_use",
            "postToolUse": "post_tool_use",
            "postToolUseFailure": "post_tool_use_failure",
            "sessionStart": "session_start",
            "sessionEnd": "session_end",
            "beforeMCPExecution": "before_mcp_execution",
        }.get(ev, ev)
    )

    if not hooks_enabled and not block:
        return {
            "decision": "allow",
            "reason": "hooks_disabled",
            "source": "skip",
            "fail_open": True,
            "event": ev,
        }

    if isinstance(block, list):
        for rule in block:
            if not isinstance(rule, dict):
                continue
            m = rule.get("matcher") or rule.get("tools") or matcher or "*"
            m_raw = m if isinstance(m, str) else ",".join(m or [])
            # 会话级事件无 tool_name：直接命中规则；有 tool 则按 matcher
            if tool_name:
                if not tool_matches(m_raw, tool_name):
                    continue
            elif ev not in ("sessionStart", "sessionEnd"):
                if m_raw and m_raw != "*" and not tool_matches(m_raw, tool_name):
                    continue
            return {
                "decision": _normalize_decision(rule.get("decision") or rule.get("action"), default_decision),
                "reason": str(rule.get("reason") or rule.get("message") or "")[:500],
                "source": "hooks.json",
                "fail_open": True,
                "event": ev,
            }
        return {
            "decision": "allow",
            "reason": "no_matching_rule",
            "source": "hooks.json",
            "fail_open": True,
            "event": ev,
        }

    if isinstance(block, dict) and block:
        m = block.get("matcher") or matcher or "*"
        if tool_name and not tool_matches(m if isinstance(m, str) else ",".join(m or []), tool_name):
            return {
                "decision": "allow",
                "reason": "matcher_miss",
                "source": "hooks.json",
                "fail_open": True,
                "event": ev,
            }
        return {
            "decision": _normalize_decision(
                block.get("decision") or block.get("action"),
                "ask" if ev == "preToolUse" else default_decision,
            ),
            "reason": str(block.get("reason") or block.get("message") or "")[:500],
            "source": "hooks.json",
            "fail_open": True,
            "event": ev,
        }

    # DB matcher 仅对 preToolUse
    if ev == "preToolUse" and hooks_enabled and matcher and tool_matches(matcher, tool_name):
        dec = _normalize_decision(db_matcher_decision, "ask")
        return {
            "decision": dec,
            "reason": f"preToolUse matcher hit (db decision={dec})",
            "source": "db_matcher",
            "fail_open": True,
            "event": ev,
        }

    return {
        "decision": "allow",
        "reason": "no_hooks",
        "source": "default",
        "fail_open": True,
        "event": ev,
    }


def resolve_pre_tool_use_decision(
    *,
    hooks_enabled: bool,
    matcher: str | None,
    hooks_json: dict[str, Any] | None,
    tool_name: str,
    args: dict | None = None,
    chat_mode: str = "normal",
    db_matcher_decision: str = "ask",
) -> dict[str, Any]:
    return resolve_hook_event(
        event="preToolUse",
        hooks_enabled=hooks_enabled,
        matcher=matcher,
        hooks_json=hooks_json,
        tool_name=tool_name,
        args=args,
        chat_mode=chat_mode,
        db_matcher_decision=db_matcher_decision,
    )


def run_hooks_for_skills(
    skills: list[dict[str, Any]],
    event: str,
    *,
    tool_name: str = "",
    args: dict | None = None,
    chat_mode: str = "normal",
) -> dict[str, Any]:
    """聚合多 Skill：deny 优先，其次 ask，否则 allow。"""
    ask_hit: dict[str, Any] | None = None
    for sk in skills or []:
        try:
            enabled = bool(sk.get("hooks_enabled"))
            matcher = sk.get("pre_tool_use_matcher") or sk.get("preToolUseMatcher") or ""
            db_dec = (
                sk.get("pre_tool_use_decision")
                or sk.get("preToolUseDecision")
                or sk.get("db_matcher_decision")
                or "ask"
            )
            skill_dir_raw = sk.get("skill_dir") or sk.get("dir") or sk.get("path") or ""
            hj: dict[str, Any] = {}
            if skill_dir_raw:
                hj = load_hooks_json(Path(str(skill_dir_raw)))
            fm_hooks = sk.get("hooks") if isinstance(sk.get("hooks"), dict) else None
            if fm_hooks and not hj:
                hj = fm_hooks
            # hooks.json / 开关 / DB matcher 任一即可评估
            has_any = bool(hj) or enabled or bool(str(matcher or "").strip())
            dec = resolve_hook_event(
                event=event,
                hooks_enabled=has_any,
                matcher=matcher,
                hooks_json=hj,
                tool_name=tool_name,
                args=args,
                chat_mode=chat_mode,
                db_matcher_decision=str(db_dec),
            )
            if dec.get("decision") == "deny":
                return {**dec, "skill_name": sk.get("name") or sk.get("skill_name") or ""}
            if dec.get("decision") == "ask" and ask_hit is None:
                ask_hit = {**dec, "skill_name": sk.get("name") or sk.get("skill_name") or ""}
        except Exception as e:
            logger.warning("hook %s 失败（fail-open）: %s", event, e)
            continue
    if ask_hit:
        return ask_hit
    return {
        "decision": "allow",
        "reason": "all_allow",
        "source": "aggregate",
        "fail_open": True,
        "event": event,
    }


def run_pre_tool_use_hooks_for_skills(
    skills: list[dict[str, Any]],
    tool_name: str,
    args: dict | None = None,
    *,
    chat_mode: str = "normal",
) -> dict[str, Any]:
    return run_hooks_for_skills(
        skills, "preToolUse", tool_name=tool_name, args=args, chat_mode=chat_mode
    )


def check_allowed_tools(
    allowed_tools_raw: str | list[str] | None,
    tool_name: str,
) -> dict[str, Any]:
    """Skill 级工具白名单；空 = 不限制。"""
    name = (tool_name or "").strip()
    if not allowed_tools_raw:
        return {"allowed": True, "reason": "no_whitelist"}
    if isinstance(allowed_tools_raw, list):
        patterns = [str(x).strip() for x in allowed_tools_raw if str(x).strip()]
    else:
        patterns = _parse_matcher_list(str(allowed_tools_raw))
    if not patterns or patterns == ["*"]:
        return {"allowed": True, "reason": "wildcard"}
    if tool_matches(patterns, name):
        return {"allowed": True, "reason": "match"}
    return {
        "allowed": False,
        "reason": f"tool `{name}` 不在本 Skill allowed_tools 白名单内",
        "patterns": patterns,
    }


def apply_post_tool_hook_decision(
    result_obj: dict[str, Any] | None,
    hook_dec: dict[str, Any] | None,
    *,
    redact_keys: tuple[str, ...] = ("output", "stdout", "result", "data", "content"),
) -> tuple[dict[str, Any], bool]:
    """将 postToolUse / postToolUseFailure 决策应用到工具结果。

    deny：标记失败、附原因，并省略大段输出，避免模型继续采信已拒绝结果。
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
    skill = str(dec.get("skill_name") or "").strip()
    note = "Skill Hook postToolUse 拒绝采纳本次工具结果"
    if skill:
        note += f"（Skill `{skill}`）"
    if reason:
        note += f"：{reason}"
    obj["hook_post"] = dec
    obj["hook_post_denied"] = True
    obj["success"] = False
    prev_err = str(obj.get("error") or "").strip()
    obj["error"] = f"{prev_err} [{note}]".strip() if prev_err else note
    for k in redact_keys:
        if k in obj and obj[k] not in (None, "", {}, []):
            obj[k] = "[已由 Skill Hook postToolUse 拒绝采纳，正文已省略]"
    return obj, False
