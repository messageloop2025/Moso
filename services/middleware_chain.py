"""中间件管道：统一工具执行前后拦截管线。按会话创建独立实例。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import config

logger = logging.getLogger("edgeops.middleware_chain")

logger = logging.getLogger("edgeops.middleware_chain")

MiddlewareFn = Callable[..., Any]


@dataclass
class MiddlewareContext:
    """中间件上下文，携带请求全链路信息。"""
    user: dict[str, Any] = field(default_factory=dict)
    session_id: int | None = None
    tool_name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    chat_mode: str = "normal"
    session_scope: str | None = None
    host_id: int | None = None

    # 额外元数据
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user.get("id"),
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "chat_mode": self.chat_mode,
        }


class MiddlewareChain:
    """按会话实例化的中间件管道。

    使用方式：
        chain = MiddlewareChain(session_id=123)
        chain.use(qa_gate_middleware)
        chain.use(strict_gate_middleware)
        result = await chain.run(ctx, tool_executor)
    """

    def __init__(self, *, session_id: int | None = None, user_id: int | None = None):
        self.session_id = session_id
        self.user_id = user_id
        self._middlewares: list[MiddlewareFn] = []
        self._enabled: bool = getattr(config, "MIDDLEWARE_ENABLED", True)

    def use(self, middleware: MiddlewareFn) -> None:
        """注册中间件（先注册先执行）。"""
        self._middlewares.append(middleware)

    def remove(self, middleware: MiddlewareFn) -> None:
        """移除中间件。"""
        try:
            self._middlewares.remove(middleware)
        except ValueError:
            pass

    async def run(
        self,
        ctx: MiddlewareContext,
        executor: Callable[[MiddlewareContext], Any],
    ) -> Any:
        """按序执行中间件链，最后调用 executor。

        中间件签名：async (ctx: MiddlewareContext, next_call: callable) -> result
        - 中间件可通过 next_call() 继续往下执行
        - 中间件可返回自定义结果来短路管道
        """
        if not self._enabled or not self._middlewares:
            return await executor(ctx) if _is_async(executor) else executor(ctx)

        # 构建洋葱模型管道
        async def _dispatch(index: int) -> Any:
            if index >= len(self._middlewares):
                # 最后一环：调用实际 executor
                return await executor(ctx) if _is_async(executor) else executor(ctx)
            mw = self._middlewares[index]
            # 调用中间件，传入 ctx 和 next 闭包
            return await mw(ctx, lambda: _dispatch(index + 1))

        return await _dispatch(0)


def _is_async(fn: Callable) -> bool:
    import inspect
    return inspect.iscoroutinefunction(fn)


# 会话级实例缓存（避免重复创建）
_session_chains: dict[int, MiddlewareChain] = {}


def get_middleware_chain(session_id: int, user_id: int | None = None) -> MiddlewareChain:
    """获取或创建会话级中间件管道实例。首次创建时自动加载用户的内置中间件配置。"""
    if session_id not in _session_chains:
        chain = MiddlewareChain(session_id=session_id, user_id=user_id)
        _session_chains[session_id] = chain
        # 异步加载用户中间件配置并注册
        import asyncio
        asyncio.ensure_future(_load_user_middleware(session_id, user_id, chain))
    return _session_chains[session_id]


async def _load_user_middleware(session_id: int, user_id: int | None, chain: MiddlewareChain) -> None:
    """从 user_middleware_config 表加载用户启用的中间件并注册到链中。"""
    if not user_id:
        return
    try:
        from database import get_db
        db = await get_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM user_middleware_config WHERE user_id = ? AND enabled = 1 ORDER BY middleware_name",
            (user_id,),
        )
        if not rows:
            return
        from services.middleware_builtin import BUILTIN_MIDDLEWARE_MAP
        for row in rows:
            mw_name = str(row.get("middleware_name") or "").strip()
            mw_fn = BUILTIN_MIDDLEWARE_MAP.get(mw_name)
            if mw_fn:
                chain.use(mw_fn)
                logger.info("middleware loaded session=%s user=%s mw=%s", session_id, user_id, mw_name)
    except Exception as e:
        logger.debug("_load_user_middleware 失败 sid=%s: %s", session_id, e)


def remove_middleware_chain(session_id: int) -> None:
    """会话结束时清理中间件管道。"""
    _session_chains.pop(session_id, None)
