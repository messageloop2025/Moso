"""搜索服务 Provider 基类。

所有具体搜索服务（GitHub / Aliyun IQS / 未来 Bing 等）继承本基类，
通过 services.search_providers.register 注册到全局注册表，前端配置页 / AI 工具
均自动从注册表派生，做到「新增一个 provider 只新增一个文件」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConfigField:
    """单个配置字段的元数据，前端用于动态渲染表单。"""
    key: str
    label: str
    type: str = "text"  # text / password / select / number / bool
    placeholder: str = ""
    required: bool = False
    secret: bool = False  # True 时前端用 password 输入框，HTTP 接口不回显原值
    options: list[dict[str, str]] | None = None  # 仅 type=select 用，[{value, label}]
    help: str = ""


@dataclass
class SearchResultItem:
    """所有 provider 的统一搜索结果格式（不同 provider 可在 raw 里塞原始字段）。"""
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "extra": self.extra,
        }


class SearchProvider:
    """搜索服务 Provider 抽象基类。子类必须重写 search()；其它字段按需重写。

    约定：
    - name：英文短码，全小写，与数据库 user_search_config.provider 一致。
    - requires_key：True 表示「不配 key 不可用」；False 表示「无 key 也能用，配 key 可解锁更多/提速」。
    - config_schema：配置字段元数据列表，前端据此渲染表单；至少包含 api_key（如有）。
    - test_key：可选的「测试连通性」逻辑；返回 (ok, message)。
    """

    name: str = ""
    display_name: str = ""
    description: str = ""
    docs_url: str = ""
    requires_key: bool = True
    config_schema: list[ConfigField] = []

    async def search(
        self,
        query: str,
        *,
        api_key: str = "",
        extra: dict | None = None,
        options: dict | None = None,
    ) -> dict:
        """执行搜索并返回统一格式的结果。

        参数：
          query：搜索词。
          api_key：用户配置的 API Key（可能为空）。
          extra：用户配置中保存的非密字段（JSON），由 user_search_config.extra 解析得到。
          options：本次调用的临时参数（如 limit / engine_type / time_range 等），由 AI 工具透传。

        返回：
          {"success": True, "items": [SearchResultItem.to_dict(), ...], "raw": ..., "provider": name}
          或 {"success": False, "error": "..."}
        """
        raise NotImplementedError

    async def test_key(self, api_key: str, extra: dict | None = None) -> tuple[bool, str]:
        """测试 API Key 是否可用。默认实现：跑一次最小搜索，看是否成功。"""
        try:
            res = await self.search("test", api_key=api_key, extra=extra, options={"limit": 1})
            if res.get("success"):
                return True, "连通正常"
            return False, str(res.get("error") or "未知错误")
        except Exception as e:
            return False, f"调用失败：{e}"
