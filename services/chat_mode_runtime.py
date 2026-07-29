"""聊天模式门禁 + 严格确认 + Hook 的运行时辅助。"""
from __future__ import annotations

import json
import logging
import re
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

_SLASH_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


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

    # Skill 级 allowed_tools：仅本轮斜杠显式唤起的 Skill（避免「开了 Hook 的 Skill」锁死整会话工具面）
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
    # hook ask → confirm 仅在严格模式下弹出确认框；
    # 普通/问答模式下 EventBus 新引擎已独立处理 ask 流程（while True 等待+独立模态），
    # 此处降级为 allow 避免重复弹出严格确认对话框。
    if hook_dec.get("decision") == "ask":
        if mode == "strict":
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
        # 非严格模式：EventBus 已处理，放行
        return {"action": "execute", "decision": "hook_ask_allow", "source": "hook"}

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


def parse_slash_invocation(message: str) -> dict[str, Any] | None:
    """解析消息开头的 `/name [args…]`。

    返回 `{name, slash, args_raw, args_list}`；不符合斜杠命令则 None。
    `args_raw` 为命令名后的全部剩余文本（含换行）；`args_list` 为空白分词。
    """
    s = (message or "").lstrip()
    if not s.startswith("/"):
        return None
    lines = s.splitlines()
    first = (lines[0] or "").strip()
    if not first.startswith("/") or len(first) < 2:
        return None
    parts = first.split(None, 1)
    tok = parts[0]
    name = tok[1:].strip()
    if not name or not _SLASH_NAME_RE.match(name):
        return None
    rest_first = parts[1].strip() if len(parts) > 1 else ""
    rest_lines = "\n".join(lines[1:]).strip()
    if rest_first and rest_lines:
        args_raw = rest_first + "\n" + rest_lines
    else:
        args_raw = rest_first or rest_lines
    args_list = args_raw.split() if args_raw else []
    return {
        "name": name,
        "slash": f"/{name}",
        "args_raw": args_raw,
        "args_list": args_list,
    }


def slash_skill_token(message: str) -> str | None:
    """解析消息开头的 /skill-name（兼容旧调用）。"""
    inv = parse_slash_invocation(message)
    return inv["name"] if inv else None


def apply_slash_arg_placeholders(
    text: str,
    args_raw: str = "",
    args_list: list[str] | None = None,
) -> str:
    """替换 `{{arg}}` / `$ARGUMENTS` / `{{argN}}` / `$ARGN` 占位。"""
    out = text or ""
    raw = args_raw or ""
    alist = list(args_list or [])
    if not alist and raw:
        alist = raw.split()
    out = out.replace("{{arg}}", raw)
    out = out.replace("{{args}}", raw)
    out = out.replace("$ARGUMENTS", raw)
    out = out.replace("${ARGUMENTS}", raw)
    for i, a in enumerate(alist, 1):
        out = out.replace(f"{{{{arg{i}}}}}", a)
        out = out.replace(f"$ARG{i}", a)
        out = out.replace(f"${{ARG{i}}}", a)
    return out


async def resolve_slash_skill_force_load(
    user: dict,
    message: str,
    session_scope: str | None = None,
    session_host_id: int | None = None,
) -> dict[str, Any] | None:
    """若用户以 /name 唤起 Skill（或 commands/ 别名），返回强制加载的正文块信息。"""
    from services.user_skills_registry import (
        get_user_skills_root,
        iter_skill_command_files,
        list_chat_enabled_skills_for_context,
        normalize_skill_name,
        parse_skill_markdown,
        read_skill_command_file,
        read_skill_content,
        user_skills_feature_enabled,
    )

    inv = parse_slash_invocation(message)
    if not inv:
        return None
    token = inv["name"]
    db = await get_db()
    uid = int(user.get("id") or 0)
    if not await user_skills_feature_enabled(db, uid):
        return None
    rows = await list_chat_enabled_skills_for_context(
        db, uid, user, session_scope, session_host_id
    )
    hit = None
    command_hit: dict[str, Any] | None = None
    try:
        want = normalize_skill_name(token)
    except ValueError:
        want = token
    for r in rows:
        name = (r.get("name") or "").strip().lower()
        slash = (r.get("slash_name") or "").strip().lower().lstrip("/")
        if name == want or slash == want:
            hit = r
            break
    if not hit:
        # skills/<name>/commands/<alias>.md 别名（与 slash-commands 菜单一致）
        for r in rows:
            for cmd in iter_skill_command_files(user, r.get("name") or ""):
                if cmd.get("alias") == want:
                    hit = r
                    command_hit = cmd
                    break
            if hit:
                break
    base_info = {
        "explicit_invoke": True,
        "slash": inv.get("slash") or f"/{want}",
        "args_raw": inv.get("args_raw") or "",
        "args_list": list(inv.get("args_list") or []),
    }
    if not hit:
        # 组织 Skills（与 slash-commands 菜单中 source=org 对齐）
        try:
            org_rows = await db.execute_fetchall(
                "SELECT name, display_name, description, content, slash_name, allowed_tools "
                "FROM org_skills WHERE enabled=1"
            )
            for orow in org_rows or []:
                oname = (orow["name"] or "").strip().lower()
                oslash = (orow["slash_name"] or "").strip().lower().lstrip("/")
                if oname == want or oslash == want:
                    content = (orow["content"] or "").strip()
                    meta, body = parse_skill_markdown(content)
                    if not meta.get("description") and orow["description"]:
                        meta = {**meta, "description": orow["description"]}
                    return {
                        **base_info,
                        "name": orow["name"],
                        "meta": meta,
                        "body": body or content,
                        "content": content,
                        "source": "org",
                        "allowed_tools": (orow["allowed_tools"] or "").strip(),
                    }
        except Exception as e:
            logger.debug("斜杠 resolve org_skills 跳过: %s", e)
        # 也允许按磁盘名强制（即使 chat_enabled 关 — 仍要求存在）
        try:
            content = await read_skill_content(user, want)
        except Exception:
            return None
        meta, body = parse_skill_markdown(content)
        return {
            **base_info,
            "name": want,
            "meta": meta,
            "body": body,
            "content": content,
            "source": "skill",
        }
    try:
        content = await read_skill_content(user, hit["name"])
    except Exception:
        return None
    meta, body = parse_skill_markdown(content)
    root = get_user_skills_root(user) / hit["name"]
    info: dict[str, Any] = {
        **base_info,
        "name": hit["name"],
        "meta": meta,
        "body": body,
        "content": content,
        "skill_dir": str(root),
        "hooks_enabled": bool(hit.get("hooks_enabled")),
        "pre_tool_use_matcher": hit.get("pre_tool_use_matcher") or "",
        "pre_tool_use_decision": hit.get("pre_tool_use_decision") or "ask",
        "allowed_tools": hit.get("allowed_tools") or "",
        "source": "skill",
    }
    if command_hit:
        cmd_text = read_skill_command_file(user, hit["name"], want) or ""
        cmd_meta, cmd_body = parse_skill_markdown(cmd_text)
        info["source"] = "commands"
        info["command_file"] = command_hit.get("rel") or ""
        info["command_alias"] = want
        info["body"] = (cmd_body or cmd_text).strip()
        info["content"] = cmd_text
        if isinstance(cmd_meta, dict) and cmd_meta.get("description"):
            info["meta"] = {**(meta if isinstance(meta, dict) else {}), **cmd_meta}
        elif isinstance(meta, dict):
            # 保留父 Skill 描述作补充
            info["parent_description"] = str(meta.get("description") or "").strip()
    return info


def format_slash_skill_injection(info: dict[str, Any]) -> str:
    name = info.get("name") or ""
    args_raw = str(info.get("args_raw") or "")
    args_list = list(info.get("args_list") or [])
    body = apply_slash_arg_placeholders(
        (info.get("body") or "").strip(), args_raw, args_list
    )
    desc = ""
    meta = info.get("meta") or {}
    if isinstance(meta, dict):
        desc = str(meta.get("description") or "").strip()
    slash = info.get("slash") or ("/" + name)
    source = info.get("source") or "skill"
    parts = [
        "\n\n**【用户显式斜杠唤起 Skill】**",
        f"用户通过 `{slash}` 强制加载下列内容（绕过仅目录披露）。",
    ]
    if args_raw:
        parts.append(f"\n用户参数（已替换 `{{{{arg}}}}` / `$ARGUMENTS` 等占位）：`{args_raw[:2000]}`")
    if source == "commands":
        rel = info.get("command_file") or f"commands/{info.get('command_alias') or ''}"
        parts.append(f"\n### Command: `{slash}` → Skill `{name}`（`{rel}`）")
        parent_desc = str(info.get("parent_description") or "").strip()
        if parent_desc and parent_desc != desc:
            parts.append(f"\n> 所属 Skill：{parent_desc}")
    else:
        parts.append(f"\n### Skill: `{name}`（显式唤起）")
    if desc:
        parts.append(f"\n> {desc}")
    if body:
        parts.append(f"\n\n{body[:24000]}")
    return "".join(parts)
