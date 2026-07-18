"""TOOLS 插件化注册表：外部模块可 append，减少直接改 ai_skills.TOOLS。"""
from __future__ import annotations

from typing import Any, Callable

_EXTRA_TOOLS: list[dict[str, Any]] = []
_EXTRA_HANDLERS: dict[str, Callable] = {}


def register_tool(tool_def: dict[str, Any], handler: Callable | None = None) -> None:
    """注册额外工具定义；可选绑定同名 handler（由 execute_tool 查表）。"""
    name = ((tool_def.get("function") or {}).get("name") or "").strip()
    if not name:
        raise ValueError("tool 缺少 function.name")
    # 去重
    _EXTRA_TOOLS[:] = [
        t for t in _EXTRA_TOOLS if ((t.get("function") or {}).get("name") or "") != name
    ]
    _EXTRA_TOOLS.append(tool_def)
    if handler is not None:
        _EXTRA_HANDLERS[name] = handler


def list_extra_tools() -> list[dict[str, Any]]:
    return list(_EXTRA_TOOLS)


def get_extra_handler(name: str) -> Callable | None:
    return _EXTRA_HANDLERS.get((name or "").strip())


def merge_tools(base_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并基础 TOOLS 与插件工具（同名插件覆盖基础）。"""
    by_name: dict[str, dict] = {}
    for t in base_tools or []:
        n = ((t.get("function") or {}).get("name") or "").strip()
        if n:
            by_name[n] = t
    for t in _EXTRA_TOOLS:
        n = ((t.get("function") or {}).get("name") or "").strip()
        if n:
            by_name[n] = t
    return list(by_name.values())
