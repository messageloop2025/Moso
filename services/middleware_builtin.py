"""内置中间件实现：从 chat_mode_enforce / chat_mode_gate 拆分的门禁逻辑。"""
from __future__ import annotations

import json
import logging
from typing import Any

from services.middleware_chain import MiddlewareContext, MiddlewareFn

logger = logging.getLogger("edgeops.middleware_builtin")


async def auth_check_middleware(ctx: MiddlewareContext, next_call) -> Any:
    """身份验证中间件：确认 user 存在且活跃。"""
    user = ctx.user
    if not user or not user.get("id"):
        return json.dumps({"success": False, "error": "未认证"}, ensure_ascii=False)
    status = str(user.get("status") or "active")
    if status != "active":
        return json.dumps({"success": False, "error": "账户已停用"}, ensure_ascii=False)
    return await next_call()


async def rate_limit_middleware(ctx: MiddlewareContext, next_call) -> Any:
    """速率限制中间件：会话级调用频率控制。"""
    rate_limit = int(ctx.extra.get("rate_limit_per_session", 0))
    if rate_limit <= 0:
        return await next_call()

    # 简单计数器实现（整合 session_runtime 的计数逻辑）
    counter_key = f"rate_limit_{ctx.session_id}"
    try:
        from services.agent_runtime_control import _session_counter
        count = _session_counter.get(counter_key, 0)
        if count >= rate_limit:
            return json.dumps({
                "success": False,
                "error": f"会话工具调用次数已达上限 ({rate_limit})，请开启新会话",
                "code": "rate_limited",
            }, ensure_ascii=False)
        _session_counter[counter_key] = count + 1
    except Exception:
        pass
    return await next_call()


async def qa_gate_middleware(ctx: MiddlewareContext, next_call) -> Any:
    """QA 模式门禁：拦截写类工具。"""
    chat_mode = str(ctx.chat_mode or "normal").strip().lower()
    if chat_mode != "qa":
        return await next_call()

    try:
        from services.chat_mode_enforce import enforce_qa_tool_block
        result = await enforce_qa_tool_block(
            ctx.tool_name,
            ctx.args,
            session_id=ctx.session_id,
            chat_mode=ctx.chat_mode,
        )
        if result is not None:
            return result
    except Exception as e:
        logger.debug("qa_gate 中间件异常（fail-open）: %s", e)
    return await next_call()


async def strict_gate_middleware(ctx: MiddlewareContext, next_call) -> Any:
    """Strict 模式门禁：需要确认的工具拦截。"""
    chat_mode = str(ctx.chat_mode or "normal").strip().lower()
    if chat_mode != "strict":
        return await next_call()

    try:
        from services.chat_mode_enforce import enforce_strict_tool_block
        result = await enforce_strict_tool_block(
            ctx.tool_name,
            ctx.args,
            session_id=ctx.session_id,
            chat_mode=ctx.chat_mode,
        )
        if result is not None:
            return result
    except Exception as e:
        logger.debug("strict_gate 中间件异常（fail-open）: %s", e)
    return await next_call()


async def audit_log_middleware(ctx: MiddlewareContext, next_call) -> Any:
    """审计日志中间件：记录工具执行结果。"""
    result = await next_call()
    try:
        ctx.extra["_audit_tool_result"] = result[:200] if isinstance(result, str) else str(result)[:200]
    except Exception:
        pass
    return result


# 内置中间件名称→函数映射（供 get_middleware_chain 自动加载）
BUILTIN_MIDDLEWARE_MAP: dict[str, MiddlewareFn] = {
    "auth_check": auth_check_middleware,
    "rate_limit": rate_limit_middleware,
    "qa_gate": qa_gate_middleware,
    "strict_gate": strict_gate_middleware,
    "audit_log": audit_log_middleware,
}
