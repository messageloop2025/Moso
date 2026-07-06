"""**子 AI（毛竹 递归）**：主 AI 用 `delegate_to_edgeops_ai` 起一次独立的内部
Agent 对话——走同一个 LLM 配置、同一套 TOOLS、但系统提示/工具子集可自定义。

和 `delegate_to_cli_agent` 的区别：
- `delegate_to_cli_agent`：把任务甩给**远端主机上的另一个 AI CLI**（cursor-agent 等），
  用 SSH 执行，捕获 stdout/git diff 回来。
- `delegate_to_edgeops_ai`：**不出本机**，主 AI 自己起一个短生命周期的「子会话」，
  带独立 system prompt 和工具子集，跑完把最终回复（Markdown/JSON）返回给主 AI。

典型用途：
- 「把刚才这些工具输出**整理成一份运维报告**」→ 起一个只允许读的子 AI，给它一份
  专注于"写报告"的 system prompt，避免污染主会话上下文；
- 「让另一个 AI 代理审查你写的这段脚本是否有安全风险」→ 子 AI 扮演 reviewer；
- 「并发跑多个分析子任务，结果聚合」→ 主 AI 用 `delegate_sub_tasks_batch` 一次发起。

为了安全：
- **递归深度限制**：用 ContextVar 追踪当前嵌套层数，默认最多 2 层，防止 AI 互相递归
  把 token 烧完；
- **工具白名单**：由调用方在 skill 参数里显式声明 `allowed_tools`，不传则走「只读
  子集」（与 `ask` / 规划类工具）；
- **步数上限**：默认 10 轮，调用方可覆盖（上限 30）；
- **超时**：总 timeout（墙钟），默认 120s，上限 600s。
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import time
from typing import Any, Awaitable, Callable

import httpx

from database import get_db
from services.llm_adapter import (
    detect_provider,
    ensure_chat_completions_url,
    extract_message_content,
    normalize_model,
    parse_chat_response,
    prepare_headers,
)
from services.ai_output_language import build_output_language_system_section


# 递归深度追踪：主 AI 调 delegate_to_edgeops_ai 时 depth=1；子 AI 又去调一次 depth=2 …
_SUB_AI_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("sub_ai_depth", default=0)

# 默认「只读」子集——这些工具都是读类 / 规划类，不会改主机或数据库：
DEFAULT_READONLY_TOOLS = {
    # 主机与凭证相关（读）
    "list_hosts", "get_host_detail", "search_hosts_by_prompt", "get_host_prompt",
    "get_host_capabilities", "list_credentials",
    # 聊天会话 / 对话记忆 / spill 分段读取
    "list_recent_tool_results", "get_recent_tool_result",
    "read_chat_data", "read_chat_attachment",
    "list_ai_sessions", "get_ai_session", "get_session_chat_detail",
    # 本机工作区读
    "fs_read_file", "fs_list_dir",
    # 最佳实践 / 知识库
    "list_best_practices", "get_best_practice", "search_best_practices",
    # 工作流模板（读）
    "list_workflow_templates",
    # 用户自己
    "whoami",
    # 仅规划 / 展示
    "ask_user_choice",
    # SSH 查询类（读）——保留 ssh_execute 但调用方应在提示里写明「只读」
    "ssh_execute",
    # 基础文件读取
    "read_file_from_host",
}


# 上限 / 默认值
DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_STEPS = 10
HARD_MAX_STEPS = 30
DEFAULT_TIMEOUT = 120
HARD_MAX_TIMEOUT = 600
DEFAULT_MAX_TOKENS = 4096
HARD_MAX_BATCH = 8
DEFAULT_MAX_PARALLEL = 3
HARD_MAX_PARALLEL = 5

# 子 AI 内禁止再起的委派类工具
_SUB_AI_FORBIDDEN_TOOLS = frozenset({"delegate_to_edgeops_ai", "delegate_sub_tasks_batch"})


def current_depth() -> int:
    return _SUB_AI_DEPTH.get()


async def _resolve_ai_settings(db, user_id: int) -> dict[str, Any]:
    """与主聊天一致：优先当前激活 Profile。"""
    from api.ai_agent import _get_user_ai_settings  # noqa: WPS433

    return await _get_user_ai_settings(db, user_id)


def _filter_tools(all_tools: list[dict], allowed: set[str] | None) -> list[dict]:
    """从完整 tools 清单里只留 allowed 里声明的。如果 allowed 为空/None，返回空列表。"""
    if not allowed:
        return []
    out = []
    for t in all_tools:
        try:
            name = t["function"]["name"]
        except Exception:
            continue
        if name in allowed:
            out.append(t)
    return out


async def run_sub_ai(
    *,
    user: dict,
    scope: str,
    task: str,
    system_prompt: str,
    allowed_tools: list[str] | set[str] | None,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    timeout_sec: int = DEFAULT_TIMEOUT,
    context_hint: str = "",
    browser_ui_locale: str | None = None,
    on_step: Callable[[dict], Awaitable[None]] | None = None,
    task_id: int | None = None,
    session_id: int | None = None,
) -> dict[str, Any]:
    """运行一次子 AI 会话，返回 {success, final_text, steps, tool_calls_summary, truncated, error}。

    参数：
    - `task`：给子 AI 的用户消息（"请帮我做 XXX"）
    - `system_prompt`：子 AI 自己的 system prompt（主 AI 决定它扮演谁）
    - `allowed_tools`：工具白名单；None/空 则不让它调任何工具（纯文本推理）
    - `context_hint`：额外贴在 system 末尾的上下文（"上面这堆日志是..."）
    - `browser_ui_locale`：可选，与主会话一致的界面语言（zh-CN / en），注入「回复语言策略」时作为浏览器回退链
    - `on_step`：可选回调，每一轮 LLM 调用/工具执行后送事件给调用方（用于 SSE 流推进度）
    - `task_id` / `session_id`：可选，传给子 AI 内部工具（后台 task scope 的 SSH 通道绑定、spill 归属）
    """
    # 递归深度卫兵
    depth = _SUB_AI_DEPTH.get()
    if depth >= max(1, min(5, int(max_depth or DEFAULT_MAX_DEPTH))):
        return {
            "success": False,
            "final_text": "",
            "steps": 0,
            "tool_calls_summary": [],
            "truncated": False,
            "error": f"达到 AI 递归深度上限 depth={depth}>={max_depth}，拒绝再起子 AI",
            "depth": depth,
        }

    # 读 LLM 配置
    db = await get_db()
    settings = await _resolve_ai_settings(db, int(user["id"]))
    base_url = (settings.get("ai_base_url") or "").strip().rstrip("/")
    if not base_url:
        return {"success": False, "error": "用户未配置 AI 服务地址，子 AI 无法启动", "final_text": "", "steps": 0}
    api_key = (settings.get("ai_api_key") or "").strip()
    provider = (settings.get("ai_provider") or "").strip() or detect_provider(base_url)
    api_url = ensure_chat_completions_url(base_url)
    model = normalize_model(provider, settings.get("ai_model") or "")
    headers = prepare_headers(provider, api_key)

    # 工具白名单
    from services.ai_skills import TOOLS as _ALL_TOOLS, execute_tool as _execute_tool, get_tools_for_scope
    scope_tools = get_tools_for_scope(scope or "default", user)
    allowed = set(allowed_tools or [])
    # 把 scope_tools 与 allowed 做交集：scope 已经限制了，再叠加用户白名单
    if allowed:
        filtered = [t for t in scope_tools if t.get("function", {}).get("name") in allowed]
    else:
        filtered = []
    tools_for_subai = filtered

    final_system = system_prompt.strip() or (
        "你是毛竹。请按主 AI 指令完成任务，向用户介绍自己时仅自称「毛竹」；"
        "最后用 Markdown 给出结论。"
    )
    rows_gol = await db.execute_fetchall("SELECT value FROM settings WHERE key = 'ai_output_locale'")
    _gol = ((rows_gol[0]["value"] if rows_gol else "") or "").strip()
    _user_ol = (settings.get("ai_output_locale") or "").strip()
    _lang_block = build_output_language_system_section(
        task,
        user_output_locale=_user_ol,
        global_output_locale=_gol,
        browser_ui_locale=(browser_ui_locale or "").strip() or None,
    )
    final_system += f"\n\n{_lang_block}"
    try:
        from api.ai_agent import _build_active_model_runtime_ctx, _resolve_context_budget_chars

        try:
            _ctx_cfg = int(settings.get("ai_context_size") or "0")
        except (TypeError, ValueError):
            _ctx_cfg = 0
        _ctx_budget = _resolve_context_budget_chars(_ctx_cfg, settings)
        final_system += "\n\n" + await _build_active_model_runtime_ctx(
            db,
            int(user["id"]),
            settings=settings,
            base_url=base_url,
            provider=provider,
            model=model,
            context_configured=_ctx_cfg,
            context_budget_chars=_ctx_budget,
            trial_info=None,
        )
    except Exception:
        pass
    if context_hint:
        final_system += f"\n\n# 上下文\n{context_hint.strip()}"
    final_system += (
        f"\n\n# 子 AI 约束\n"
        f"- 当前你作为一个子 AI 被主 AI 调起，递归深度 = {depth + 1}（上限 {max_depth}）。"
        f"\n- **不要**再调用 `delegate_to_edgeops_ai` 或 `delegate_sub_tasks_batch`，主 AI 已禁止更深一层递归。"
        f"\n- 工具白名单：{sorted(allowed) if allowed else '（无，请纯文本推理）'}。"
        f"\n- 步数上限：{max_steps}；墙钟超时：{timeout_sec}s。"
        f"\n- 完成后用一段 Markdown 总结交付；不要输出闲聊。"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": final_system},
        {"role": "user", "content": task},
    ]

    t0 = time.time()
    steps = 0
    tool_calls_summary: list[dict[str, Any]] = []
    truncated = False
    err: str | None = None
    final_text = ""

    # 深度 +1 的 token
    tok = _SUB_AI_DEPTH.set(depth + 1)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(min(HARD_MAX_TIMEOUT, timeout_sec))) as client:
            while steps < max(1, min(HARD_MAX_STEPS, max_steps)):
                if time.time() - t0 > timeout_sec:
                    err = f"子 AI 超时（{timeout_sec}s），已中止"
                    truncated = True
                    break
                steps += 1
                payload = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": DEFAULT_MAX_TOKENS,
                }
                if tools_for_subai:
                    payload["tools"] = tools_for_subai
                    payload["tool_choice"] = "auto"
                try:
                    resp = await client.post(api_url, headers=headers, json=payload)
                except Exception as e:
                    err = f"LLM 请求异常：{type(e).__name__}: {e}"
                    break
                if resp.status_code != 200:
                    err_detail = resp.text[:300]
                    try:
                        j = resp.json()
                        err_detail = j.get("error", {}).get("message", err_detail) if isinstance(j.get("error"), dict) else j.get("message", err_detail)
                    except Exception:
                        pass
                    err = f"LLM 返回 HTTP {resp.status_code}：{err_detail}"
                    break
                try:
                    result = resp.json()
                except Exception:
                    err = "LLM 返回非 JSON"
                    break
                msg, tool_calls = parse_chat_response(result)

                if tool_calls:
                    messages.append({
                        "role": "assistant",
                        "content": extract_message_content(msg) or "",
                        "tool_calls": tool_calls,
                    })
                    if on_step:
                        try:
                            await on_step({"kind": "sub_ai_step", "step": steps, "tool_calls": [tc["function"]["name"] for tc in tool_calls]})
                        except Exception:
                            pass
                    for tc in tool_calls:
                        fn_name = tc["function"]["name"]
                        fn_id = tc["id"]
                        try:
                            fn_args = json.loads(tc["function"]["arguments"])
                        except json.JSONDecodeError:
                            fn_args = {}
                        if fn_name in _SUB_AI_FORBIDDEN_TOOLS:
                            tool_result = json.dumps({
                                "success": False,
                                "error": f"子 AI 不允许调用 {fn_name}（递归深度受限）",
                            }, ensure_ascii=False)
                        elif allowed and fn_name not in allowed:
                            tool_result = json.dumps({
                                "success": False,
                                "error": f"子 AI 无权调用 {fn_name}（不在 allowed_tools 白名单内）",
                            }, ensure_ascii=False)
                        else:
                            try:
                                tool_result = await _execute_tool(
                                    fn_name, fn_args, user,
                                    scope=scope, task_id=task_id, ui_capable=False,
                                    session_id=session_id,
                                    ui_locale=(browser_ui_locale or "").strip() or None,
                                )
                            except Exception as e:
                                tool_result = json.dumps({"success": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
                        tool_calls_summary.append({
                            "step": steps,
                            "tool": fn_name,
                            "args_keys": sorted(list(fn_args.keys())) if isinstance(fn_args, dict) else [],
                            "result_preview": (tool_result or "")[:400],
                        })
                        if on_step:
                            try:
                                await on_step({"kind": "sub_ai_tool", "step": steps, "tool": fn_name, "preview": (tool_result or "")[:200]})
                            except Exception:
                                pass
                        from api.ai_agent import _compact_tool_result_for_messages, _tool_result_message_limit
                        from services.chat_tool_spill import spill_and_wrap_tool_message
                        _lim = _tool_result_message_limit(262_144)
                        _compact = _compact_tool_result_for_messages(fn_name, tool_result or "", _lim, "standard")
                        _body = await spill_and_wrap_tool_message(user, session_id, fn_name, fn_id, tool_result or "", _compact)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": fn_id,
                            "content": _body,
                        })
                    continue

                final_text = (extract_message_content(msg) or "").strip()
                if on_step and final_text:
                    try:
                        await on_step({"kind": "sub_ai_done", "step": steps, "preview": final_text[:400]})
                    except Exception:
                        pass
                break
    finally:
        _SUB_AI_DEPTH.reset(tok)

    if not final_text and not err:
        err = "子 AI 结束但无最终文本（可能是 tool_call 循环/达到步数上限）"
        truncated = truncated or (steps >= max_steps)

    return {
        "success": err is None and bool(final_text),
        "final_text": final_text,
        "steps": steps,
        "duration_sec": round(time.time() - t0, 2),
        "tool_calls_summary": tool_calls_summary,
        "truncated": truncated,
        "depth": depth + 1,
        "error": err,
    }


async def run_sub_ai_batch(
    *,
    user: dict,
    scope: str,
    tasks: list[dict[str, Any]],
    shared_system_prompt: str = "",
    default_allowed_tools: list[str] | None = None,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    timeout_sec: int = DEFAULT_TIMEOUT,
    browser_ui_locale: str | None = None,
    on_step: Callable[[dict], Awaitable[None]] | None = None,
    task_id: int | None = None,
    session_id: int | None = None,
) -> dict[str, Any]:
    """并发运行多个子 AI 子任务，返回聚合结果。"""
    if not tasks:
        return {"success": False, "error": "tasks 不能为空", "results": [], "total": 0}
    if len(tasks) > HARD_MAX_BATCH:
        return {
            "success": False,
            "error": f"子任务数 {len(tasks)} 超过上限 {HARD_MAX_BATCH}",
            "results": [],
            "total": len(tasks),
        }

    parallel = max(1, min(HARD_MAX_PARALLEL, int(max_parallel or DEFAULT_MAX_PARALLEL)))
    sem = asyncio.Semaphore(parallel)
    shared_sys = (shared_system_prompt or "").strip()
    default_allowed = [str(x) for x in (default_allowed_tools or [])]
    results: list[dict[str, Any] | None] = [None] * len(tasks)
    t0 = time.time()

    async def _run_one(idx: int, spec: dict[str, Any]) -> None:
        name = (spec.get("name") or f"task-{idx + 1}").strip()
        task_text = (spec.get("task") or "").strip()
        sys_p = (spec.get("system_prompt") or shared_sys or "").strip()
        if not task_text or not sys_p:
            results[idx] = {
                "index": idx,
                "name": name,
                "success": False,
                "error": "task 与 system_prompt（或 shared_system_prompt）不能都为空",
            }
            return
        allowed = spec.get("allowed_tools")
        if allowed is None:
            allowed = default_allowed
        try:
            steps_ = int(spec.get("max_steps") or max_steps)
        except (TypeError, ValueError):
            steps_ = max_steps
        try:
            timeout_ = int(spec.get("timeout_sec") or timeout_sec)
        except (TypeError, ValueError):
            timeout_ = timeout_sec
        context_hint = str(spec.get("context_hint") or "")

        async def _on_step(ev: dict) -> None:
            if on_step:
                payload = dict(ev)
                payload["task_index"] = idx
                payload["task_name"] = name
                await on_step(payload)

        if on_step:
            try:
                await on_step({"kind": "sub_ai_batch_start", "task_index": idx, "task_name": name})
            except Exception:
                pass

        async with sem:
            out = await run_sub_ai(
                user=user,
                scope=scope,
                task=task_text,
                system_prompt=sys_p,
                allowed_tools=[str(x) for x in allowed] if allowed else [],
                max_steps=steps_,
                max_depth=max_depth,
                timeout_sec=timeout_,
                context_hint=context_hint,
                browser_ui_locale=browser_ui_locale,
                on_step=_on_step,
                task_id=task_id,
                session_id=session_id,
            )

        results[idx] = {
            "index": idx,
            "name": name,
            "success": bool(out.get("success")),
            "final_text": out.get("final_text", ""),
            "steps_used": out.get("steps", 0),
            "duration_sec": out.get("duration_sec", 0),
            "truncated": out.get("truncated", False),
            "tool_calls_summary": out.get("tool_calls_summary", []),
            "error": out.get("error"),
        }
        if on_step:
            try:
                await on_step({
                    "kind": "sub_ai_batch_end",
                    "task_index": idx,
                    "task_name": name,
                    "success": bool(out.get("success")),
                    "duration_sec": out.get("duration_sec", 0),
                    "preview": (out.get("final_text") or "")[:300],
                    "error": out.get("error"),
                })
            except Exception:
                pass

    await asyncio.gather(*[_run_one(i, spec) for i, spec in enumerate(tasks)])

    ok_count = sum(1 for r in results if r and r.get("success"))
    return {
        "success": ok_count == len(tasks),
        "total": len(tasks),
        "succeeded": ok_count,
        "failed": len(tasks) - ok_count,
        "duration_sec": round(time.time() - t0, 2),
        "max_parallel": parallel,
        "results": [r for r in results if r is not None],
    }
