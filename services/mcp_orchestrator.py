"""MCP 专用编排式 ops：主编排快响 + 后台子 Agent（无 Web UI 依赖）。

与 run_ops_integration_chat_complete / claw-ops 完全分离：
- 会话 scope = mcp_orchestrate
- 任务表 = mcp_agent_tasks
- 仅由 /api/integration/mcp/orchestrate/* + MCP 工具调用
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

import httpx

import config as _config
from config import AGENT_MAX_STEPS, AGENT_MAX_STEPS_CAP, SYSTEM_AI_USAGE_LIMIT
from database import get_db
from services.ai_skills import execute_tool, get_tools_for_scope
from services.llm_adapter import (
    ensure_chat_completions_url,
    extract_message_content,
    normalize_model,
    parse_chat_response,
    prepare_headers,
    require_api_key,
)

logger = logging.getLogger("edgeops.mcp_orchestrator")

AI_MESSAGE_SAVE_MAX = 200_000
_TASK_CALLBACK_BEGIN = "<!-- EDGEOPS:MCP_TASK_CALLBACK:v1"
_TASK_CALLBACK_END = "<!-- /EDGEOPS:MCP_TASK_CALLBACK:v1 -->"
_RUNNING_STATUSES = frozenset({"pending", "running"})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

_MCP_ORCHESTRATE_RULES = """
## 运行模式（MCP 编排 — 纯 API，无 Web 界面）
- 当前通道为 MCP 客户端：无浏览器控制台、无 SSE、无按钮/卡片 UI。
- 禁止依赖 connect_terminal、send_to_terminal、ask_user_choice；子 Agent 工具链已排除交互型工具。
- 远程操作：非交互短命令用 ssh_execute（长任务 detach + poll_log）；交互式用 ssh_channel_*。
- 大输出用 read_chat_data 分段读取 spill。
- 需要用户确认时，在回复中以纯文本列出选项，等待下轮消息；勿假设有 UI 控件。
"""


def _parse_planner_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    if fence:
        raw = fence.group(1)
    else:
        brace = re.search(r"\{[\s\S]*\}", raw)
        if brace:
            raw = brace.group(0)
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


async def _get_user_ai_settings(db, user_id: int) -> dict[str, str]:
    from api.ai_agent import _get_user_ai_settings as _load

    return await _load(db, user_id)


async def _resolve_ai_credentials(db, user: dict, settings: dict[str, str]) -> tuple[str, str, str, dict | None]:
    from api.ai_agent import (
        _allow_system_shared_api_key,
        _consume_system_ai_usage,
        _effective_provider,
        _get_system_key_and_base,
    )

    base_url = (settings.get("ai_base_url") or "").strip().rstrip("/")
    if not base_url:
        base_url = (getattr(_config, "AI_BASE_URL", "") or "").strip().rstrip("/")
    if not base_url:
        return "", "", "", {"success": False, "error": "AI 未配置服务地址 (base_url)"}

    provider = _effective_provider(settings, base_url)
    api_key = (settings.get("ai_api_key") or "").strip()
    trial = None
    if require_api_key(provider, api_key) and not api_key:
        system_key, system_base = await _get_system_key_and_base(db)
        if _allow_system_shared_api_key(system_key, system_base, resolved_base_url=base_url):
            trial = await _consume_system_ai_usage(db, user["id"])
            if trial.get("exhausted"):
                return "", "", "", {
                    "success": False,
                    "error": f"系统共享 Key 配额已用尽（上限 {trial.get('limit', SYSTEM_AI_USAGE_LIMIT)}）",
                }
            api_key = system_key
            if not (settings.get("ai_base_url") or "").strip() and (system_base or "").strip():
                base_url = (system_base or "").strip().rstrip("/")
        else:
            return "", "", "", {"success": False, "error": "AI 未配置 API Key"}
    return base_url, api_key, provider, None


async def _ensure_mcp_orchestrate_session(
    db,
    user: dict,
    session_id: int | None,
    host_id: int | None,
) -> tuple[int | None, dict | None]:
    from api.ai_agent import _can_access_host_with_shares, _get_host_row

    sid = session_id
    if not sid:
        if host_id is not None:
            try:
                hid = int(host_id)
            except (TypeError, ValueError):
                return None, {"success": False, "error": "host_id 无效", "session_id": None}
            bind_host = await _get_host_row(hid)
            if not bind_host or not await _can_access_host_with_shares(db, bind_host, user):
                return None, {"success": False, "error": "主机不存在或无权绑定", "session_id": None}
        title = "MCP编排-" + datetime.now().strftime("%Y%m%d%H%M%S")
        await db.execute(
            "INSERT INTO ai_chat_sessions (user_id, host_id, title, session_scope) VALUES (?, ?, ?, 'mcp_orchestrate')",
            (user["id"], host_id, title),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        sid = (await cur.fetchone())[0]

    rows = await db.execute_fetchall(
        "SELECT id, host_id, COALESCE(session_scope,'default') AS session_scope FROM ai_chat_sessions WHERE id=? AND user_id=?",
        (sid, user["id"]),
    )
    if not rows:
        return None, {"success": False, "error": "会话不存在", "session_id": sid}
    row = dict(rows[0])
    if (row.get("session_scope") or "").strip().lower() != "mcp_orchestrate":
        return None, {
            "success": False,
            "error": "该 session_id 不是 MCP 编排会话；请省略 session_id 以新建",
            "session_id": sid,
        }
    return int(sid), None


async def _collect_task_completions(db, user_id: int, session_id: int) -> list[dict]:
    rows = await db.execute_fetchall(
        """SELECT id, title, status, result_text, error_text, finished_at
           FROM mcp_agent_tasks
           WHERE user_id=? AND session_id=? AND callback_delivered=0 AND status IN ('completed','failed','cancelled')
           ORDER BY id ASC LIMIT 20""",
        (user_id, session_id),
    )
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        summary = (d.get("result_text") or d.get("error_text") or "")[:1200]
        out.append(
            {
                "task_id": d["id"],
                "title": d.get("title") or "",
                "status": d.get("status"),
                "summary": summary,
                "finished_at": d.get("finished_at"),
            }
        )
        await db.execute(
            "UPDATE mcp_agent_tasks SET callback_delivered=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (d["id"],),
        )
    if out:
        await db.commit()
    return out


async def _count_running_tasks(db, user_id: int, session_id: int) -> int:
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM mcp_agent_tasks WHERE user_id=? AND session_id=? AND status IN ('pending','running')",
        (user_id, session_id),
    )
    return int(rows[0]["n"]) if rows else 0


def _append_progress(task_id: int, progress: list, kind: str, detail: str) -> list:
    entry = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "kind": kind,
        "detail": (detail or "")[:4000],
    }
    progress.append(entry)
    if len(progress) > 200:
        progress = progress[-200:]
    return progress


async def _pull_task_control(db, task_id: int) -> dict | None:
    rows = await db.execute_fetchall(
        "SELECT id, action, message FROM mcp_agent_task_controls WHERE task_id=? AND consumed=0 ORDER BY id ASC LIMIT 1",
        (task_id,),
    )
    if not rows:
        return None
    ctrl = dict(rows[0])
    await db.execute("UPDATE mcp_agent_task_controls SET consumed=1 WHERE id=?", (ctrl["id"],))
    await db.commit()
    return ctrl


async def _run_sub_agent_task(task_id: int) -> None:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM mcp_agent_tasks WHERE id=?", (task_id,))
    if not rows:
        return
    task = dict(rows[0])
    status = (task.get("status") or "").strip().lower()
    if status in _TERMINAL_STATUSES:
        return
    if status == "running":
        return

    user_rows = await db.execute_fetchall("SELECT * FROM users WHERE id=?", (task["user_id"],))
    if not user_rows:
        await db.execute(
            "UPDATE mcp_agent_tasks SET status='failed', error_text='用户不存在', finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (task_id,),
        )
        await db.commit()
        return
    user = dict(user_rows[0])

    resume_note = ""
    ctrl = await _pull_task_control(db, task_id)
    if ctrl:
        act = (ctrl.get("action") or "").strip().lower()
        if act == "supplement":
            resume_note = (ctrl.get("message") or "").strip()
        elif act == "stop":
            await db.execute(
                "UPDATE mcp_agent_tasks SET status='cancelled', error_text='用户中止', finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (task_id,),
            )
            await db.commit()
            await _deliver_task_callback(db, task_id)
            return

    cur = await db.execute(
        "UPDATE mcp_agent_tasks SET status='running', started_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
        (task_id,),
    )
    await db.commit()
    if cur.rowcount == 0:
        return

    settings = await _get_user_ai_settings(db, user["id"])
    base_url, api_key, provider, err = await _resolve_ai_credentials(db, user, settings)
    if err:
        await db.execute(
            "UPDATE mcp_agent_tasks SET status='failed', error_text=?, finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (err.get("error") or "AI 配置错误", task_id),
        )
        await db.commit()
        await _deliver_task_callback(db, task_id)
        return

    try:
        agent_max_steps = max(
            1,
            min(AGENT_MAX_STEPS_CAP, int(settings.get("ai_agent_max_steps") or 0) or AGENT_MAX_STEPS),
        )
    except (TypeError, ValueError):
        agent_max_steps = AGENT_MAX_STEPS

    sid = int(task["session_id"])
    instruction = (task.get("instruction") or "").strip()
    if resume_note:
        instruction = f"{instruction}\n\n【用户补充】{resume_note}"

    progress: list = []
    try:
        progress_raw = task.get("progress_json") or "[]"
        progress = json.loads(progress_raw) if progress_raw else []
        if not isinstance(progress, list):
            progress = []
    except Exception:
        progress = []

    progress = _append_progress(task_id, progress, "start", f"子任务开始：{task.get('title') or instruction[:80]}")
    await db.execute(
        "UPDATE mcp_agent_tasks SET progress_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (json.dumps(progress, ensure_ascii=False), task_id),
    )
    await db.commit()

    system = f"""你是毛竹。当前在毛竹（Moso）MCP 后台担任运维子 Agent。无 Web UI；通过工具完成 SSH/文件/查询等操作。
{_MCP_ORCHESTRATE_RULES}
当前会话 ID={sid}；子任务 ID={task_id}。
"""
    if task.get("host_id"):
        system += f"优先操作 host_id={task['host_id']}。\n"
    system += f"\n任务指令：\n{instruction}\n"

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": instruction},
    ]

    api_url = ensure_chat_completions_url(base_url)
    model = normalize_model(provider, settings.get("ai_model") or "")
    headers = prepare_headers(provider, api_key)
    timeout = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
    final_reply = ""
    failed = False
    err_text = ""

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for step in range(agent_max_steps):
                ctrl = await _pull_task_control(db, task_id)
                if ctrl and (ctrl.get("action") or "").strip().lower() == "stop":
                    await db.execute(
                        "UPDATE mcp_agent_tasks SET status='cancelled', error_text='用户中止', progress_json=?, finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (json.dumps(progress, ensure_ascii=False), task_id),
                    )
                    await db.commit()
                    await _deliver_task_callback(db, task_id)
                    return

                resp = await client.post(
                    api_url,
                    headers=headers,
                    json={
                        "model": model,
                        "messages": messages,
                        "tools": get_tools_for_scope("task", user),
                        "tool_choice": "auto",
                        "stream": False,
                    },
                )
                if resp.status_code != 200:
                    err_text = resp.text[:800]
                    failed = True
                    break

                result = resp.json()
                msg, tool_calls = parse_chat_response(result)
                if tool_calls:
                    from api.ai_agent import _prepare_tool_calls_for_execution

                    full_tool_calls, prepared = await _prepare_tool_calls_for_execution(tool_calls)
                    messages.append(
                        {
                            "role": "assistant",
                            "content": extract_message_content(msg) or "",
                            "tool_calls": full_tool_calls,
                        }
                    )
                    for tc, fn_args, fn_preview in prepared:
                        fn_name = tc["function"]["name"]
                        fn_id = tc["id"]
                        progress = _append_progress(
                            task_id,
                            progress,
                            "tool",
                            f"{fn_name}({json.dumps(fn_preview, ensure_ascii=False)[:200]})",
                        )
                        tool_result = await execute_tool(
                            fn_name,
                            fn_args,
                            user,
                            scope="default",
                            ui_capable=False,
                            session_id=sid,
                            task_id=task_id,
                        )
                        messages.append(
                            {"role": "tool", "tool_call_id": fn_id, "content": tool_result[:120_000]}
                        )
                    await db.execute(
                        "UPDATE mcp_agent_tasks SET progress_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (json.dumps(progress, ensure_ascii=False), task_id),
                    )
                    await db.commit()
                    continue

                final_reply = extract_message_content(msg) or ""
                break
            else:
                failed = True
                err_text = f"达到最大步数 ({agent_max_steps})"
    except Exception as exc:
        failed = True
        err_text = str(exc) or "子 Agent 执行异常"
        logger.exception("mcp sub-agent task_id=%s failed", task_id)

    if failed:
        progress = _append_progress(task_id, progress, "error", err_text)
        await db.execute(
            """UPDATE mcp_agent_tasks SET status='failed', error_text=?, progress_json=?,
               finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (err_text[:8000], json.dumps(progress, ensure_ascii=False), task_id),
        )
    else:
        progress = _append_progress(task_id, progress, "done", (final_reply or "")[:500])
        await db.execute(
            """UPDATE mcp_agent_tasks SET status='completed', result_text=?, progress_json=?,
               finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            ((final_reply or "（子任务已完成）")[:AI_MESSAGE_SAVE_MAX], json.dumps(progress, ensure_ascii=False), task_id),
        )
    await db.commit()
    await _deliver_task_callback(db, task_id)


async def _deliver_task_callback(db, task_id: int) -> None:
    rows = await db.execute_fetchall("SELECT * FROM mcp_agent_tasks WHERE id=?", (task_id,))
    if not rows:
        return
    task = dict(rows[0])
    sid = task.get("session_id")
    status = task.get("status") or ""
    title = task.get("title") or f"任务 #{task_id}"
    body = task.get("result_text") if status == "completed" else (task.get("error_text") or "")
    callback = (
        f"{_TASK_CALLBACK_BEGIN} task_id={task_id} status={status} -->\n"
        f"### MCP 后台任务完成 · #{task_id} · {title}\n"
        f"**状态**: {status}\n\n"
        f"{(body or '')[:8000]}\n"
        f"{_TASK_CALLBACK_END}"
    )
    try:
        await db.execute(
            "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'assistant', ?)",
            (sid, callback[:AI_MESSAGE_SAVE_MAX]),
        )
        await db.execute(
            "UPDATE ai_chat_sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (sid,),
        )
        await db.commit()
    except Exception as exc:
        logger.warning("mcp task callback write failed task_id=%s: %s", task_id, exc)


def _schedule_sub_agent_task(task_id: int) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run_sub_agent_task(task_id))
    except RuntimeError:
        asyncio.run(_run_sub_agent_task(task_id))


async def run_mcp_orchestrate_chat(
    db,
    user: dict,
    message: str,
    session_id: int | None = None,
    host_id: int | None = None,
) -> dict:
    msg_in = (message or "").strip()
    if not msg_in:
        return {"success": False, "error": "message 不能为空", "session_id": session_id}

    sid, err = await _ensure_mcp_orchestrate_session(db, user, session_id, host_id)
    if err:
        return err

    completions = await _collect_task_completions(db, user["id"], sid)
    running = await _count_running_tasks(db, user["id"], sid)

    settings = await _get_user_ai_settings(db, user["id"])
    base_url, api_key, provider, cred_err = await _resolve_ai_credentials(db, user, settings)
    if cred_err:
        cred_err["session_id"] = sid
        return cred_err

    planner_system = f"""你是毛竹。当前在毛竹（Moso）MCP 担任主编排器。根据用户消息决定：
1. 若可直接回答（知识、说明、状态解读）→ {{"action":"reply","text":"..."}}
2. 若需 SSH/部署/排查等耗时操作 → {{"action":"delegate","title":"简短标题","instruction":"给子 Agent 的完整指令","host_id":可选整数}}

{_MCP_ORCHESTRATE_RULES}
当前 MCP 编排会话 ID={sid}；后台运行中任务数={running}。
只输出一个 JSON 对象，不要其它文字。"""

    api_url = ensure_chat_completions_url(base_url)
    model = normalize_model(provider, settings.get("ai_model") or "")
    headers = prepare_headers(provider, api_key)
    timeout = httpx.Timeout(connect=20.0, read=90.0, write=20.0, pool=20.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            api_url,
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": planner_system},
                    {"role": "user", "content": msg_in},
                ],
                "stream": False,
                "max_tokens": 4096,
            },
        )
        if resp.status_code != 200:
            return {
                "success": False,
                "error": f"主编排 LLM 错误 HTTP {resp.status_code}: {resp.text[:400]}",
                "session_id": sid,
            }
        plan_msg, _ = parse_chat_response(resp.json())
        content = extract_message_content(plan_msg) or ""

    plan = _parse_planner_json(content)
    if not plan:
        # 降级：整段当直接回复
        plan = {"action": "reply", "text": content or "（未能解析编排计划，请重试）"}

    action = (plan.get("action") or plan.get("mode") or "reply").strip().lower()
    if action in ("reply", "reply_direct"):
        reply = (plan.get("text") or plan.get("reply") or content or "").strip()
        try:
            await db.execute(
                "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'user', ?)",
                (sid, msg_in[:AI_MESSAGE_SAVE_MAX]),
            )
            await db.execute(
                "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                (sid, reply[:AI_MESSAGE_SAVE_MAX]),
            )
            await db.execute(
                "UPDATE ai_chat_sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (sid,),
            )
            await db.commit()
        except Exception as exc:
            logger.warning("mcp orchestrate save messages failed: %s", exc)
        return {
            "success": True,
            "session_id": sid,
            "mode": "reply_direct",
            "reply": reply,
            "task_completions": completions,
            "tasks_running": running,
        }

    if action in ("delegate", "create_background_task", "delegate_task"):
        title = (plan.get("title") or msg_in[:60]).strip()
        instruction = (plan.get("instruction") or plan.get("task") or msg_in).strip()
        task_host = plan.get("host_id", host_id)
        try:
            task_host = int(task_host) if task_host is not None else None
        except (TypeError, ValueError):
            task_host = host_id

        await db.execute(
            """INSERT INTO mcp_agent_tasks (user_id, session_id, host_id, title, instruction, status)
               VALUES (?, ?, ?, ?, ?, 'pending')""",
            (user["id"], sid, task_host, title[:200], instruction[:50_000]),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        task_id = (await cur.fetchone())[0]

        try:
            await db.execute(
                "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'user', ?)",
                (sid, msg_in[:AI_MESSAGE_SAVE_MAX]),
            )
            ack = (
                f"已创建 MCP 后台任务 **#{task_id}**（{title}）。"
                f"请用 edgeops_ops_task_output / edgeops_ops_task_list 查看进度。"
            )
            await db.execute(
                "INSERT INTO ai_chat_messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                (sid, ack[:AI_MESSAGE_SAVE_MAX]),
            )
            await db.execute(
                "UPDATE ai_chat_sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (sid,),
            )
            await db.commit()
        except Exception as exc:
            logger.warning("mcp orchestrate ack save failed: %s", exc)

        _schedule_sub_agent_task(int(task_id))
        return {
            "success": True,
            "session_id": sid,
            "mode": "background_task",
            "reply": ack,
            "task_ids": [int(task_id)],
            "task_completions": completions,
            "tasks_running": running + 1,
        }

    return {
        "success": False,
        "error": f"未知编排 action: {action}",
        "session_id": sid,
        "raw_plan": plan,
    }


async def list_mcp_agent_tasks(
    db,
    user: dict,
    *,
    session_id: int | None = None,
    status: str | None = None,
    limit: int = 30,
) -> dict:
    limit = max(1, min(100, limit))
    conds = ["user_id=?"]
    params: list[Any] = [user["id"]]
    if session_id is not None:
        conds.append("session_id=?")
        params.append(session_id)
    if status:
        conds.append("status=?")
        params.append(status.strip())
    where = " AND ".join(conds)
    rows = await db.execute_fetchall(
        f"""SELECT id, session_id, host_id, title, status, created_at, updated_at, started_at, finished_at
            FROM mcp_agent_tasks WHERE {where} ORDER BY id DESC LIMIT ?""",
        (*params, limit),
    )
    return {"success": True, "tasks": [dict(r) for r in rows], "count": len(rows)}


async def get_mcp_agent_task_output(db, user: dict, task_id: int) -> dict:
    rows = await db.execute_fetchall(
        "SELECT * FROM mcp_agent_tasks WHERE id=? AND user_id=?",
        (task_id, user["id"]),
    )
    if not rows:
        return {"success": False, "error": "任务不存在"}
    task = dict(rows[0])
    progress: list = []
    try:
        progress = json.loads(task.get("progress_json") or "[]")
    except Exception:
        progress = []
    return {
        "success": True,
        "task_id": task_id,
        "session_id": task.get("session_id"),
        "host_id": task.get("host_id"),
        "title": task.get("title"),
        "status": task.get("status"),
        "result_text": task.get("result_text") or "",
        "error_text": task.get("error_text") or "",
        "progress": progress,
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
    }


async def control_mcp_agent_task(
    db,
    user: dict,
    task_id: int,
    action: str,
    message: str = "",
) -> dict:
    act = (action or "").strip().lower()
    if act not in ("stop", "supplement"):
        return {"success": False, "error": "action 须为 stop 或 supplement"}
    rows = await db.execute_fetchall(
        "SELECT id, status FROM mcp_agent_tasks WHERE id=? AND user_id=?",
        (task_id, user["id"]),
    )
    if not rows:
        return {"success": False, "error": "任务不存在"}
    status = (dict(rows[0]).get("status") or "").strip().lower()
    if status in _TERMINAL_STATUSES:
        return {"success": False, "error": f"任务已结束 ({status})，无法控制"}
    await db.execute(
        "INSERT INTO mcp_agent_task_controls (task_id, action, message) VALUES (?, ?, ?)",
        (task_id, act, (message or "")[:8000]),
    )
    await db.commit()
    if act == "stop" and status == "pending":
        await db.execute(
            "UPDATE mcp_agent_tasks SET status='cancelled', error_text='用户中止（pending）', finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (task_id,),
        )
        await db.commit()
        await _deliver_task_callback(db, task_id)
    return {"success": True, "task_id": task_id, "action": act, "queued": True}
