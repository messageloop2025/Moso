"""搜索服务 Provider 注册表。

新增一个 provider 的步骤：
1. 在本目录新建 your_provider.py，继承 SearchProvider 实现 search()。
2. 在本文件下方的 `_register_builtin_providers()` 里 import 并注册即可。

注册表本身是「按 name 索引的字典」，前端配置页 / AI 工具 / 后端 REST 都从这里派生：
    - list_providers() → 返回元数据（display_name / requires_key / config_schema），用于前端动态渲染。
    - get_provider(name) → 取实例，调用 search() / test_key()。
"""
from __future__ import annotations

from .base import ConfigField, SearchProvider, SearchResultItem

_REGISTRY: dict[str, SearchProvider] = {}


def register(provider: SearchProvider) -> None:
    """注册一个 provider 实例（重复注册以最后一次为准）。"""
    if not provider.name:
        raise ValueError("provider.name 不能为空")
    _REGISTRY[provider.name] = provider


def get_provider(name: str) -> SearchProvider | None:
    """按短码获取 provider 实例。"""
    if not name:
        return None
    return _REGISTRY.get(name.strip().lower())


def list_providers() -> list[SearchProvider]:
    """返回所有已注册的 provider 实例（按 name 排序，方便前端稳定渲染）。"""
    return [_REGISTRY[k] for k in sorted(_REGISTRY.keys())]


def provider_meta(provider: SearchProvider) -> dict:
    """把一个 provider 的元数据序列化成前端友好的 dict。"""
    return {
        "name": provider.name,
        "display_name": provider.display_name,
        "description": provider.description,
        "docs_url": provider.docs_url,
        "requires_key": provider.requires_key,
        "config_schema": [
            {
                "key": f.key,
                "label": f.label,
                "type": f.type,
                "placeholder": f.placeholder,
                "required": f.required,
                "secret": f.secret,
                "options": f.options or [],
                "help": f.help,
            }
            for f in provider.config_schema
        ],
    }


def _register_builtin_providers() -> None:
    """import + 注册所有内建 provider。新增 provider 在此处加一行。"""
    from .github import GitHubProvider
    from .iqs import AliyunIQSProvider

    register(GitHubProvider())
    register(AliyunIQSProvider())


_register_builtin_providers()


__all__ = [
    "ConfigField",
    "SearchProvider",
    "SearchResultItem",
    "register",
    "get_provider",
    "list_providers",
    "provider_meta",
]
