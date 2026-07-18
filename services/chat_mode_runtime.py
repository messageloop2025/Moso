"""聊天模式门禁 + 严格确认 + Hook 的运行时辅助。"""
from __future__ import annotations

import json
import logging
from typing import Any

from database import get_db
from services.chat_mode_gate import (
    build_strict_confirm_body,
    is_qa_blocked,
    is_strict_allow_cached,
    needs_strict_confirm,
    normalize_chat_mode,
    parse_strict_allow_cache,
    dump_strict_allow_cache,
    qa_blocked_tool_result,
    strict_allow_cache_key,
)
from services.user_skills_hooks import (
    check_allowed_tools,
    run_hooks_for_skills,
    run_pre_tool_use_hooks_for_skills,
)

logger = logging.getLogger("edgeops.chat_mode_runtime")


def build_strict_confirm_ui_action(
    *,
    tool_name: str,
    args: dict | None,
    intent: str = "",
    reason: str = "",
    assistant_note: str = "",
) -> dict[str, Any]:
    """
    严格模式独立确认事件（不走 ask_user_choice / 不经模型文案）。
    前端应按 action=strict_command_confirm 弹出独立模态，而不是聊天内选择卡。
    """
    from services.chat_mode_gate import extract_command_payload

    note = (assistant_note or reason or "").strip()
    body = (intent or "").strip()
    if not body:
        body = build_strict_confirm_body(tool_name, args, assistant_note=note)
    elif reason and reason not in body:
        body = f"{body}\n\n（补充：{reason}）"
    target, cmd = extract_command_payload(tool_name, args)
    # SSE/前端只需摘要字段；完整 args 可能含不可序列化对象，勿原样下发
    safe_args: dict[str, Any] = {}
    if isinstance(args, dict):
        for k in ("host_id", "channel_id", "slot", "command", "text"):
            if k in args and args[k] is not None:
                safe_args[k] = args[k]
    return {
        # 独立通路：勿复用 ask_user_choice，避免落入过程输出/选择题气泡
        "action": "strict_command_confirm",
        "kind": "strict_command_confirm",
        "tool": tool_name,
        "tool_args": safe_args,
        "target": target,
        "command": (cmd or "").strip(),
        "reason": note[:800] if note else "",
        "intent": body,
        "question": f"【严格确认】是否允许执行下列操作？\n\n{body}",
        "options": [
            {"id": "allow", "label": "允许", "style": "primary"},
            {"id": "always_allow", "label": "总是", "style": "success"},
            {"id": "deny", "label": "拒绝", "style": "danger"},
        ],
        "allow_multiple": False,
        "allow_text": False,
        "default_id": "deny",
    }


async def load_session_chat_mode_fields(session_id: int, user_id: int) -> dict[str, Any]:
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT COALESCE(chat_mode, 'normal') AS chat_mode,
                  COALESCE(strict_allow_cache_json, '') AS strict_allow_cache_json
           FROM ai_chat_sessions WHERE id = ? AND user_id = ?""",
        (session_id, user_id),
    )
    if not rows:
        return {"chat_mode": "normal", "strict_allow_cache_json": ""}
    return dict(rows[0])


async def persist_strict_allow_key(session_id: int, user_id: int, key: str) -> None:
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT COALESCE(strict_allow_cache_json, '') AS strict_allow_cache_json
           FROM ai_chat_sessions WHERE id = ? AND user_id = ?""",
        (session_id, user_id),
    )
    if not rows:
        return
    keys = parse_strict_allow_cache(rows[0]["strict_allow_cache_json"])
    if key not in keys:
        keys.append(key)
    raw = dump_strict_allow_cache(keys)
    await db.execute(
        """UPDATE ai_chat_sessions SET strict_allow_cache_json = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ? AND user_id = ?""",
        (raw, session_id, user_id),
    )
    await db.commit()


async def audit_chat_mode_decision(
    *,
    user_id: int,
    session_id: int,
    mode: str,
    tool_name: str,
    args: dict | None,
    decision: str,
    intent: str = "",
    source: str = "chat_mode_gate",
) -> None:
    try:
        db = await get_db()
        params = json.dumps(
            {
                "session_id": session_id,
                "mode": mode,
                "tool": tool_name,
                "tool_args": args or {},
                "decision": decision,
                "intent": (intent or "")[:800],
                "source": source,
            },
            ensure_ascii=False,
            default=str,
        )
        await db.execute(
            """INSERT INTO operation_logs (user_id, operation, params, result, details, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                f"chat_mode:{decision}",
                params[:4000],
                "ok" if decision in ("allow", "always_allow", "qa_suggest") else "denied",
                (intent or decision)[:2000],
                source,
            ),
        )
        await db.commit()
    except Exception as e:
        logger.warning("chat_mode audit failed: %s", e)


def parse_strict_choice(choice_text: str) -> str:
    """从 `[allow] 允许` / `[always_allow] 总是` 等解析决策。"""
    raw = choice_text or ""
    t = raw.strip().lower()
    if t.startswith("["):
        end = t.find("]")
        if end > 1:
            t = t[1:end].strip().lower()
    if t in ("always_allow", "always", "always-allow", "一直允许", "总是"):
        return "always_allow"
    if t in ("allow", "yes", "ok", "允许"):
        return "allow"
    if t in ("deny", "no", "拒绝"):
        return "deny"
    # 标签兜底（注意「总是」优先于含「允许」的句子）
    if "总是" in raw or "一直允许" in raw:
        return "always_allow"
    if "允许" in raw and "拒绝" not in raw:
        return "allow"
    return "deny"


# 措辞刻意不提「严格模式」，避免模型复读/编造确认流程；仅陈述用户对本次 tool 的态度
_STRICT_DECISION_NOTES: dict[str, str] = {
    "deny": (
        "用户未批准本次工具调用，操作未执行。"
        "请勿自动重试同一操作；可简要说明已取消，并询问是否改做其他事。"
    ),
    "allow": (
        "用户已批准本次工具调用；以下为本轮实际执行结果。可据此继续任务。"
    ),
    "always_allow": (
        "用户已批准本次工具调用，并授权本会话内该工具可继续使用；以下为本轮实际执行结果。"
    ),
    "always_allow_cached": (
        "用户此前已授权本会话内使用该工具；以下为本轮实际执行结果。"
    ),
}


def annotate_tool_result_with_strict_decision(
    tool_result: str | dict | None,
    decision: str | None,
    *,
    tool_name: str = "",
) -> str:
    """
    将用户严格确认决策写入 tool 返回 JSON，供模型区分「用户同意 / 拒绝 / 会话内总是」。
    decision: allow | always_allow | deny | always_allow_cached
    """
    d = (decision or "").strip().lower()
    if d not in _STRICT_DECISION_NOTES:
        if isinstance(tool_result, str):
            return tool_result
        try:
            return json.dumps(tool_result or {}, ensure_ascii=False, default=str)
        except Exception:
            return json.dumps({"success": False, "error": "invalid_tool_result"}, ensure_ascii=False)

    obj: dict[str, Any]
    if isinstance(tool_result, dict):
        obj = dict(tool_result)
    else:
        raw = (tool_result or "").strip()
        if not raw:
            obj = {"success": d != "deny"}
        else:
            try:
                parsed = json.loads(raw)
                obj = parsed if isinstance(parsed, dict) else {"success": True, "result": parsed}
            except Exception:
                obj = {"success": d != "deny", "raw": raw[:2000]}

    note = _STRICT_DECISION_NOTES[d]
    obj["mode"] = "strict"
    obj["user_decision"] = d
    obj["user_decision_note"] = note
    if tool_name:
        obj["user_decision_tool"] = tool_name
    if d == "deny":
        obj["success"] = False
        obj.setdefault("error", "用户拒绝执行该操作（严格确认）")
        obj["cancelled_by_user"] = True
    return json.dumps(obj, ensure_ascii=False, default=str)


async def evaluate_pre_tool_gate(
    *,
    chat_mode: str,
    tool_name: str,
    args: dict | None,
    strict_allow_cache_json: str = "",
    hook_skills: list[dict] | None = None,
    assistant_note: str = "",
    strict_allow_glob: bool = False,
    force_skill_allowed_tools: str | list | None = None,
) -> dict[str, Any]:
    """
    返回:
      {action: execute}
      {action: block, tool_result: dict}
      {action: confirm, ui_action: dict, intent: str, reason: str, source: str}
    """
    mode = normalize_chat_mode(chat_mode)
    name = (tool_name or "").strip()
    a = args if isinstance(args, dict) else {}

    if mode == "qa" and is_qa_blocked(name):
        blocked = qa_blocked_tool_result(name, a, assistant_note=assistant_note)
        return {"action": "block", "tool_result": blocked, "decision": "qa_block"}

    # Skill 级 allowed_tools（显式唤起 / 配置了白名单时）
    if force_skill_allowed_tools:
        at = check_allowed_tools(force_skill_allowed_tools, name)
        if not at.get("allowed"):
            return {
                "action": "block",
                "tool_result": {
                    "success": False,
                    "error": at.get("reason") or "allowed_tools 拒绝",
                    "mode": mode,
                },
                "decision": "allowed_tools_deny",
                "source": "allowed_tools",
            }
    for sk in hook_skills or []:
        raw_at = sk.get("allowed_tools") or ""
        if not raw_at:
            continue
        # 仅对 hooks_enabled 或 slash 显式 skill 强制；有白名单即生效
        at = check_allowed_tools(raw_at, name)
        if not at.get("allowed"):
            return {
                "action": "block",
                "tool_result": {
                    "success": False,
                    "error": f"Skill `{sk.get('name')}`: {at.get('reason')}",
                    "mode": mode,
                },
                "decision": "allowed_tools_deny",
                "source": "allowed_tools",
            }

    # Hooks（所有模式）
    hook_dec = run_pre_tool_use_hooks_for_skills(
        hook_skills or [],
        name,
        a,
        chat_mode=mode,
    )
    if hook_dec.get("decision") == "deny":
        return {
            "action": "block",
            "tool_result": {
                "success": False,
                "error": f"Skill Hook preToolUse 拒绝执行 `{name}`"
                + (f"：{hook_dec.get('reason')}" if hook_dec.get("reason") else ""),
                "mode": mode,
                "hook": hook_dec,
            },
            "decision": "hook_deny",
            "source": "hook",
        }
    if hook_dec.get("decision") == "ask":
        intent = build_strict_confirm_body(name, a, assistant_note=assistant_note or "")
        return {
            "action": "confirm",
            "ui_action": build_strict_confirm_ui_action(
                tool_name=name,
                args=a,
                intent=intent,
                reason=str(hook_dec.get("reason") or "Skill Hook 要求确认"),
                assistant_note=assistant_note or "",
            ),
            "intent": intent,
            "reason": hook_dec.get("reason") or "",
            "source": "hook",
        }

    if mode == "strict" and needs_strict_confirm(name):
        if is_strict_allow_cached(
            strict_allow_cache_json, name, a, allow_glob=strict_allow_glob
        ):
            return {"action": "execute", "decision": "strict_cached_allow"}
        intent = build_strict_confirm_body(name, a, assistant_note=assistant_note or "")
        return {
            "action": "confirm",
            "ui_action": build_strict_confirm_ui_action(
                tool_name=name,
                args=a,
                intent=intent,
                assistant_note=assistant_note or "",
            ),
            "intent": intent,
            "reason": "",
            "source": "strict",
        }

    return {"action": "execute", "decision": "allow"}


def slash_skill_token(message: str) -> str | None:
    """解析消息开头的 /skill-name（或首个斜杠 token）。"""
    s = (message or "").lstrip()
    if not s.startswith("/"):
        return None
    # 取第一行第一个 token
    first = s.splitlines()[0].strip()
    tok = first.split()[0] if first else ""
    if len(tok) < 2:
        return None
    name = tok[1:].strip()
    if not name or name != name.lower():
        return None
    if not all(c.isalnum() or c in "-_" for c in name):
        return None
    if not name[0].isalpha():
        return None
    return name


async def resolve_slash_skill_force_load(
    user: dict,
    message: str,
    session_scope: str | None = None,
    session_host_id: int | None = None,
) -> dict[str, Any] | None:
    """若用户以 /name 唤起 Skill，返回强制加载的正文块信息。"""
    from services.user_skills_registry import (
        get_user_skills_root,
        list_chat_enabled_skills_for_context,
        normalize_skill_name,
        parse_skill_markdown,
        read_skill_content,
        user_skills_feature_enabled,
    )

    token = slash_skill_token(message)
    if not token:
        return None
    db = await get_db()
    uid = int(user.get("id") or 0)
    if not await user_skills_feature_enabled(db, uid):
        return None
    rows = await list_chat_enabled_skills_for_context(
        db, uid, user, session_scope, session_host_id
    )
    hit = None
    try:
        want = normalize_skill_name(token)
    except ValueError:
        want = token
    for r in rows:
        name = (r.get("name") or "").strip().lower()
        slash = (r.get("slash_name") or "").strip().lower()
        if name == want or slash == want or slash == f"/{want}":
            hit = r
            break
    if not hit:
        # 也允许按磁盘名强制（即使 chat_enabled 关？计划说强制加载 — 仍要求存在）
        try:
            content = await read_skill_content(user, want)
        except Exception:
            return None
        meta, body = parse_skill_markdown(content)
        return {
            "name": want,
            "meta": meta,
            "body": body,
            "content": content,
            "explicit_invoke": True,
            "slash": f"/{want}",
        }
    try:
        content = await read_skill_content(user, hit["name"])
    except Exception:
        return None
    meta, body = parse_skill_markdown(content)
    root = get_user_skills_root(user) / hit["name"]
    return {
        "name": hit["name"],
        "meta": meta,
        "body": body,
        "content": content,
        "explicit_invoke": True,
        "slash": f"/{want}",
        "skill_dir": str(root),
        "hooks_enabled": bool(hit.get("hooks_enabled")),
        "pre_tool_use_matcher": hit.get("pre_tool_use_matcher") or "",
    }


def format_slash_skill_injection(info: dict[str, Any]) -> str:
    name = info.get("name") or ""
    body = (info.get("body") or "").strip()
    desc = ""
    meta = info.get("meta") or {}
    if isinstance(meta, dict):
        desc = str(meta.get("description") or "").strip()
    parts = [
        "\n\n**【用户显式斜杠唤起 Skill】**",
        f"用户通过 `{info.get('slash') or ('/' + name)}` 强制加载下列 Skill 全文（绕过仅目录披露）。",
        f"\n### Skill: `{name}`（显式唤起）",
    ]
    if desc:
        parts.append(f"\n> {desc}")
    if body:
        parts.append(f"\n\n{body[:24000]}")
    return "".join(parts)
