"""后台任务执行引擎：触发任务与定时任务运行 AI Agent（SSH Channel + 工具调用）

触发方式：定时任务完成/失败时可自动触发指定触发任务（trigger_conditions JSON 配置）。
"""
import asyncio
import json
import logging
from datetime import datetime

import httpx

from database import get_db
from api.ai_agent import _compact_tool_result_for_messages, _tool_result_message_limit
from services.user_mail import effective_scheduled_task_notify_email_to, parse_notify_emails, send_mail_as_user
from services.ai_skills import TOOLS, execute_tool, get_tools_for_scope
from services.chat_tool_spill import spill_and_wrap_tool_message
from services.llm_adapter import (
    detect_provider,
    ensure_chat_completions_url,
    extract_message_content,
    normalize_model,
    parse_chat_response,
    prepare_headers,
)
from services.ai_output_language import build_output_language_system_section

logger = logging.getLogger("edgeops.task_runner")

# 任务 Agent 最大步数：默认 100，硬上限 1000（与 config.AGENT_MAX_STEPS_CAP 对齐）
TASK_AGENT_MAX_STEPS = 100
AI_MESSAGE_SAVE = 200_000


def _task_run_log_summary(text: str) -> str:
    try:
        from config import TASK_RUN_LOG_SUMMARY_MAX_CHARS as cap
    except Exception:
        cap = 2000
    cap = max(200, int(cap or 2000))
    return (text or "")[:cap]


def _truncate_scheduled_notify_body(text: str) -> str:
    """定时任务通知邮件正文：尽量发完整 AI 输出，仅在超过配置上限时截断并附说明。"""
    text = text or ""
    try:
        from config import SCHEDULED_TASK_NOTIFY_EMAIL_MAX_CHARS as cap
    except Exception:
        cap = 500_000
    cap = int(cap or 0)
    if cap <= 0 or len(text) <= cap:
        return text
    note = (
        f"\n\n---\n（正文过长已截断：原文共 {len(text)} 字，邮件仅含前 {cap} 字。"
        "完整内容请登录 毛竹 → 定时任务 → 该次执行记录查看。）"
    )
    keep = max(0, cap - len(note))
    return text[:keep] + note


def _build_scheduled_task_notify_email_body(
    *,
    task_name: str,
    task_id: int,
    run_id: int,
    status: str,
    result_text: str,
) -> str:
    body = "\n".join(
        [
            f"任务名称: {task_name}",
            f"任务 ID: {task_id}",
            f"执行 ID: {run_id}",
            f"状态: {status}",
            "",
            "执行结果:",
            _truncate_scheduled_notify_body(result_text),
        ]
    )
    return body


async def _trigger_on_scheduled_finish(db, user_id: int, scheduled_task_id: int, status: str) -> None:
    """定时任务结束时，根据 trigger_conditions 自动触发配置了的触发任务。
    trigger_conditions 为 JSON 时支持：on_scheduled_complete: [任务ID], on_scheduled_fail: [任务ID]
    """
    rows = await db.execute_fetchall(
        "SELECT id, name, trigger_conditions FROM triggered_tasks WHERE user_id = ?",
        (user_id,),
    )
    for r in rows:
        cond = (r.get("trigger_conditions") or "").strip()
        if not cond:
            continue
        try:
            data = json.loads(cond)
        except json.JSONDecodeError:
            continue
        trigger = False
        if status == "completed":
            task_ids = data.get("on_scheduled_complete")
            if isinstance(task_ids, list) and scheduled_task_id in task_ids:
                trigger = True
            elif task_ids == "*" or task_ids == scheduled_task_id:
                trigger = True
        elif status == "failed":
            task_ids = data.get("on_scheduled_fail")
            if isinstance(task_ids, list) and scheduled_task_id in task_ids:
                trigger = True
            elif task_ids == "*" or task_ids == scheduled_task_id:
                trigger = True
        if not trigger:
            continue
        tid = r["id"]
        # 查询定时任务名便于记录
        name_rows = await db.execute_fetchall("SELECT name FROM scheduled_tasks WHERE id = ?", (scheduled_task_id,))
        caller_name = name_rows[0]["name"] if name_rows else f"定时任务{scheduled_task_id}"
        await db.execute(
            """INSERT INTO triggered_task_runs (task_id, triggered_by_type, triggered_by_id, caller_task_name, status, instruction)
               VALUES (?, 'scheduled', ?, ?, 'pending', ?)""",
            (tid, str(scheduled_task_id), caller_name, f"定时任务{scheduled_task_id}({status}) 自动触发"),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        run_id = (await cur.fetchone())[0]
        await db.execute(
            "UPDATE triggered_tasks SET last_run_at = CURRENT_TIMESTAMP, last_run_status = 'pending', is_running = 1 WHERE id = ?",
            (tid,),
        )
        await db.commit()
        asyncio.create_task(run_triggered_task(run_id))
        logger.info("Triggered task %s run_id=%s by scheduled task %s %s", tid, run_id, scheduled_task_id, status)


async def _get_user_ai_settings(db, user_id: int) -> dict:
    """获取用户 AI 配置（与 ai_agent 一致）。"""
    keys = [
        "ai_api_key", "ai_base_url", "ai_model", "ai_system_prompt",
        "ai_auto_approve", "ai_context_size", "ai_agent_max_steps", "ai_provider",
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
        out["ai_context_size"] = (r.get("context_size") or "0").strip()
        out["ai_agent_max_steps"] = (r.get("agent_max_steps") or "").strip()
        out["ai_provider"] = (r.get("provider") or "").strip()
        out["ai_output_locale"] = (r.get("ai_output_locale") or "").strip()
    for k in keys:
        if k not in out or out[k] == "":
            if k == "ai_provider":
                out[k] = ""
                continue
            rows = await db.execute_fetchall("SELECT value FROM settings WHERE key = ?", (k,))
            val = (rows[0]["value"] if rows else "") or ("0" if k == "ai_context_size" else "")
            if k == "ai_api_key":
                val = ""
            out[k] = val
    return out


async def _run_agent_loop(
    task_id: int,
    task_content: str,
    instruction: str,
    user: dict,
    settings: dict,
    run_type: str,
    run_id: int,
    db,
) -> tuple[str, list[dict]]:
    """执行一轮 Agent 循环（无流式），返回 (最终回复文本, 消息列表用于保存)。"""
    provider = (settings.get("ai_provider") or "").strip() or detect_provider(settings.get("ai_base_url") or "")
    api_key = (settings.get("ai_api_key") or "").strip()
    base_url = (settings.get("ai_base_url") or "").strip().rstrip("/")
    if not base_url:
        return "未配置 AI 服务地址", []
    try:
        from config import AGENT_MAX_STEPS_CAP as _AGENT_CAP
    except Exception:
        _AGENT_CAP = 1000
    try:
        agent_max_steps = max(1, min(_AGENT_CAP, int(settings.get("ai_agent_max_steps") or 0) or TASK_AGENT_MAX_STEPS))
    except (TypeError, ValueError):
        agent_max_steps = TASK_AGENT_MAX_STEPS

    system_content = """你是毛竹。本任务在毛竹（Moso）平台后台执行。请根据「任务内容」与「指令」完成目标。
- 当前为**后台任务模式**（task_id=%s，无浏览器 UI、无人在场实时交互）。创建 SSH 通道时请使用 owner_type=task、owner_id=%s（或由系统自动填充）。
- 可使用 ssh_channel_*、triggered_task_*、scheduled_task_*、list_hosts、get_host_detail、ssh_execute、fs_*、create_chat_artifact、send_email 等工具；**不要**尝试 local_exec / local_chat_data_paths 等本机专用工具（任务模式下不在工具列表中）。
- **不要使用** `ask_user_choice`：本环境无 UI 渲染按钮（即使调用，工具也会返回 `ui_capable=false` 的纯文本回退）；你需要按预设的"任务内容/触发指令"自主决断后继续执行，不要等待用户输入。
- **发邮件**：任务配置了 notify_email_to 时，系统会在任务结束后**自动**把本次完整文字结论发往该邮箱（plain text）。需要 HTML 排版或附件时，任务内应主动调用 `send_email`（支持 `body_html` 与 `attachments`）。
- 请按步骤执行并给出简要结论。"""
    system_content = system_content % (task_id, task_id)
    glob_rows = await db.execute_fetchall("SELECT value FROM settings WHERE key = 'ai_output_locale'")
    _gol = ((glob_rows[0]["value"] if glob_rows else "") or "").strip()
    _uol = (settings.get("ai_output_locale") or "").strip()
    _um = f"{task_content}\n\n{instruction or ''}"
    system_content = system_content + "\n\n" + build_output_language_system_section(
        _um,
        user_output_locale=_uol,
        global_output_locale=_gol,
        browser_ui_locale=None,
    )
    user_content = f"## 任务内容\n{task_content}\n\n## 指令\n{instruction or '无'}\n\n请开始执行并汇报结果。"
    tools = get_tools_for_scope("task", user)
    api_url = ensure_chat_completions_url(base_url)
    model = normalize_model(provider, settings.get("ai_model") or "")
    headers = prepare_headers(provider, api_key)

    try:
        from config import TASK_AGENT_MAX_OUTPUT_TOKENS as _task_max_out
    except Exception:
        _task_max_out = 16384
    try:
        _max_out = max(1024, min(128_000, int(_task_max_out or 16384)))
    except (TypeError, ValueError):
        _max_out = 16384

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
    steps = 0
    while steps < agent_max_steps:
        steps += 1
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": _max_out,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
                resp = await client.post(api_url, headers=headers, json=payload)
        except Exception as e:
            logger.warning("Task agent HTTP error: %s", e)
            return f"AI 请求失败: {e}", []
        if resp.status_code != 200:
            return f"AI 返回 HTTP {resp.status_code}", []
        try:
            result = resp.json()
        except Exception:
            return "AI 返回非 JSON", []
        msg, tool_calls = parse_chat_response(result)
        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": extract_message_content(msg) or "",
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_id = tc["id"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}
                try:
                    tool_result = await execute_tool(
                        fn_name, fn_args, user,
                        scope="task", task_id=task_id,
                    )
                except Exception as e:
                    tool_result = json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                _lim = _tool_result_message_limit(262_144)
                _compact = _compact_tool_result_for_messages(fn_name, tool_result, _lim, "standard")
                _body = await spill_and_wrap_tool_message(user, None, fn_name, fn_id, tool_result, _compact)
                messages.append({
                    "role": "tool",
                    "tool_call_id": fn_id,
                    "content": _body,
                })
            continue
        content = (extract_message_content(msg) or "").strip()
        return content or "（无文本回复）", messages
    return "达到最大步数未结束", messages


async def run_triggered_task(run_id: int) -> None:
    """执行一条触发任务 run：加载任务与用户，跑 Agent，更新 run 状态。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT r.id, r.task_id, r.instruction, r.status, t.user_id, t.name, t.content
           FROM triggered_task_runs r
           JOIN triggered_tasks t ON t.id = r.task_id
           WHERE r.id = ?""",
        (run_id,),
    )
    if not rows:
        logger.warning("Triggered run not found: %s", run_id)
        return
    r = dict(rows[0])
    task_id = r["task_id"]
    user_id = r["user_id"]
    user_rows = await db.execute_fetchall("SELECT id, username, display_name, role FROM users WHERE id = ?", (user_id,))
    if not user_rows:
        return
    user = dict(user_rows[0])
    settings = await _get_user_ai_settings(db, user_id)
    task_content = r.get("content") or ""
    instruction = r.get("instruction") or ""
    try:
        final_text, msg_list = await _run_agent_loop(
            task_id=task_id,
            task_content=task_content,
            instruction=instruction,
            user=user,
            settings=settings,
            run_type="triggered",
            run_id=run_id,
            db=db,
        )
        status = "completed"
        log_summary = _task_run_log_summary(final_text)
        await db.execute(
            "INSERT INTO triggered_task_run_messages (run_id, role, content) VALUES (?, 'user', ?)",
            (run_id, ((task_content or "") + "\n" + (instruction or "")).strip()[:AI_MESSAGE_SAVE]),
        )
        await db.execute(
            "INSERT INTO triggered_task_run_messages (run_id, role, content) VALUES (?, 'assistant', ?)",
            (run_id, (final_text or "")[:AI_MESSAGE_SAVE]),
        )
    except Exception as e:
        logger.exception("Triggered task run failed: %s", e)
        status = "failed"
        log_summary = _task_run_log_summary(str(e))
    await db.execute(
        "UPDATE triggered_task_runs SET status = ?, log_summary = ? WHERE id = ?",
        (status, log_summary, run_id),
    )
    await db.execute(
        "UPDATE triggered_tasks SET last_run_status = ?, is_running = 0 WHERE id = ?",
        (status, task_id),
    )
    await db.commit()


async def run_scheduled_task(run_id: int) -> None:
    """执行一条定时任务 run：跑 Agent，写入 scheduled_task_run_messages，更新 run 状态。"""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT r.id, r.task_id, t.user_id, t.name, t.content
           FROM scheduled_task_runs r
           JOIN scheduled_tasks t ON t.id = r.task_id
           WHERE r.id = ?""",
        (run_id,),
    )
    if not rows:
        logger.warning("Scheduled run not found: %s", run_id)
        return
    r = dict(rows[0])
    task_id = r["task_id"]
    user_id = r["user_id"]
    user_rows = await db.execute_fetchall("SELECT id, username, display_name, role FROM users WHERE id = ?", (user_id,))
    if not user_rows:
        return
    user = dict(user_rows[0])
    settings = await _get_user_ai_settings(db, user_id)
    task_content = r.get("content") or ""
    final_text = ""
    try:
        final_text, msg_list = await _run_agent_loop(
            task_id=task_id,
            task_content=task_content,
            instruction="",
            user=user,
            settings=settings,
            run_type="scheduled",
            run_id=run_id,
            db=db,
        )
        status = "completed"
        log_summary = _task_run_log_summary(final_text)
        await db.execute(
            "INSERT INTO scheduled_task_run_messages (run_id, role, content) VALUES (?, 'user', ?)",
            (run_id, (task_content or "")[:AI_MESSAGE_SAVE]),
        )
        await db.execute(
            "INSERT INTO scheduled_task_run_messages (run_id, role, content) VALUES (?, 'assistant', ?)",
            (run_id, (final_text or "")[:AI_MESSAGE_SAVE]),
        )
    except Exception as e:
        logger.exception("Scheduled task run failed: %s", e)
        status = "failed"
        final_text = str(e)
        log_summary = _task_run_log_summary(final_text)
    await db.execute(
        "UPDATE scheduled_task_runs SET status = ?, log_summary = ? WHERE id = ?",
        (status, log_summary, run_id),
    )
    await db.execute(
        "UPDATE scheduled_tasks SET last_run_at = datetime('now', 'localtime'), last_run_status = ?, is_running = 0 WHERE id = ?",
        (status, task_id),
    )
    await db.commit()
    # 触发方式 1/2：定时任务完成或失败时，触发配置了 on_scheduled_complete / on_scheduled_fail 的触发任务
    await _trigger_on_scheduled_finish(db, user_id, task_id, status)
    # 若配置了通知邮箱，使用**该用户**的 SMTP 发信（与管理员全局 SMTP 无关）
    try:
        trows = await db.execute_fetchall(
            "SELECT name, notify_email_to, content FROM scheduled_tasks WHERE id = ?", (task_id,),
        )
        if trows:
            tr = dict(trows[0])
            eff = effective_scheduled_task_notify_email_to(
                tr.get("notify_email_to") or "", tr.get("content") or ""
            )
            recipients = parse_notify_emails(eff)
            if recipients:
                subj = f"[毛竹 定时任务] {tr.get('name') or task_id} — {status}"
                notify_result = (final_text or log_summary or "").strip()
                body = _build_scheduled_task_notify_email_body(
                    task_name=str(tr.get("name") or task_id),
                    task_id=task_id,
                    run_id=run_id,
                    status=status,
                    result_text=notify_result,
                )
                ok_mail, err_mail = await send_mail_as_user(db, user_id, recipients, subj, body)
                if not ok_mail:
                    logger.warning("定时任务结果邮件未发送 user_id=%s task_id=%s: %s", user_id, task_id, err_mail)
    except Exception as e:
        logger.warning("定时任务结果邮件异常: %s", e)
