"""阿里云信息查询服务（IQS）Provider —— UnifiedSearch 接口。

文档：https://help.aliyun.com/zh/document_detail/2883041.html
端点：POST https://cloud-iqs.aliyuncs.com/search/unified
鉴权：Authorization: Bearer $API_KEY
开通与创建 Key：https://help.aliyun.com/zh/document_detail/2872258.html
（管理员与普通用户均需各自配置个人 Key 后方可使用。）
"""
from __future__ import annotations

import logging

import httpx

from .base import ConfigField, SearchProvider, SearchResultItem, SEARCH_HTTP_USER_AGENT

logger = logging.getLogger("edgeops.search.iqs")

_IQS_ENDPOINT = "https://cloud-iqs.aliyuncs.com/search/unified"

_ENGINE_TYPES = [
    {"value": "Generic", "label": "Standard (~10 results)"},
    {"value": "GenericAdvanced", "label": "Advanced (~50 results, paid)"},
    {"value": "LiteAdvanced", "label": "Fast semantic (1–50 results)"},
    {"value": "Deep", "label": "Deep search (complex queries, 1–50, higher latency)"},
]
_TIME_RANGES = [
    {"value": "NoLimit", "label": "No limit"},
    {"value": "OneDay", "label": "Past 24 hours"},
    {"value": "OneWeek", "label": "Past week"},
    {"value": "OneMonth", "label": "Past month"},
    {"value": "OneYear", "label": "Past year"},
]


class AliyunIQSProvider(SearchProvider):
    name = "iqs"
    display_name = "Alibaba Cloud IQS search"
    description = (
        "Alibaba Cloud Information Query Service (UnifiedSearch) for open-domain web search. "
        "Configure an API key in the Alibaba Cloud console (service must be enabled)."
    )
    docs_url = "https://help.aliyun.com/zh/document_detail/2883041.html"
    requires_key = True
    config_schema = [
        ConfigField(
            key="api_key",
            label="IQS API key",
            type="password",
            placeholder="Create in Alibaba Cloud IQS console (~5 min to take effect)",
            required=True,
            secret=True,
            help=(
                "Enable the service and create a key: "
                "https://help.aliyun.com/zh/document_detail/2872258.html. "
                "Each user configures their own key; there is no shared key."
            ),
        ),
        ConfigField(
            key="default_engine_type",
            label="Default engine type",
            type="select",
            placeholder="Generic",
            required=False,
            secret=False,
            options=_ENGINE_TYPES,
            help=(
                "Used when a call does not specify an engine. "
                "GenericAdvanced is billed by usage."
            ),
        ),
    ]

    async def search(
        self,
        query: str,
        *,
        api_key: str = "",
        extra: dict | None = None,
        options: dict | None = None,
    ) -> dict:
        if not query or not query.strip():
            return {"success": False, "error": "query 不能为空"}
        if len(query) > 500:
            return {"success": False, "error": "query 长度不能超过 500 字符"}
        if not api_key or not api_key.strip():
            return {
                "success": False,
                "error": "尚未配置 IQS API Key，请到「设置 / 搜索服务」配置后再使用",
            }
        opts = options or {}
        ext = extra or {}

        engine_type = (
            opts.get("engine_type")
            or ext.get("default_engine_type")
            or "Generic"
        )
        time_range = opts.get("time_range") or "NoLimit"
        try:
            limit = max(1, min(int(opts.get("limit") or 10), 50))
        except (TypeError, ValueError):
            limit = 10

        body: dict = {
            "query": query.strip(),
            "engineType": engine_type,
            "timeRange": time_range,
            "contents": {
                "mainText": bool(opts.get("with_main_text", False)),
                "markdownText": bool(opts.get("with_markdown", False)),
                "summary": bool(opts.get("with_summary", False)),
                "rerankScore": True,
            },
        }
        if opts.get("category"):
            body["category"] = str(opts["category"])
        # IQS 用 page/rows 控制结果数；UnifiedSearch 文档里 LiteAdvanced/Deep/GenericAdvanced 支持 1-50
        if engine_type in ("LiteAdvanced", "Deep", "GenericAdvanced"):
            body["rows"] = limit

        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
            "User-Agent": SEARCH_HTTP_USER_AGENT,
        }
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
        transport_err: Exception | None = None
        resp = None
        for _attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(_IQS_ENDPOINT, headers=headers, json=body)
                transport_err = None
                break
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
                transport_err = e
                continue
            except httpx.HTTPError as e:
                return {"success": False, "error": f"IQS 请求失败：{e}"}
        if transport_err is not None or resp is None:
            return {
                "success": False,
                "error": "IQS 网络异常，请稍后重试；若持续失败，请检查本机网络、代理或防火墙设置",
            }

        if resp.status_code == 401 or resp.status_code == 403:
            return {
                "success": False,
                "error": "IQS 鉴权失败：API Key 无效、过期或服务未开通（创建 Key 后需等约 5 分钟生效）",
            }
        if resp.status_code != 200:
            return {
                "success": False,
                "error": f"IQS HTTP {resp.status_code}: {resp.text[:300]}",
            }
        try:
            data = resp.json()
        except Exception as e:
            return {"success": False, "error": f"IQS 响应解析失败：{e}"}

        page_items = data.get("pageItems") or []
        scene_items = data.get("sceneItems") or []
        items: list[SearchResultItem] = []
        for it in page_items[:limit]:
            items.append(
                SearchResultItem(
                    title=it.get("title") or "",
                    url=it.get("link") or "",
                    snippet=(it.get("snippet") or "")[:500],
                    source=it.get("hostname") or "iqs",
                    extra={
                        "published_time": it.get("publishedTime"),
                        "rerank_score": it.get("rerankScore"),
                        "host_authority": it.get("hostAuthorityScore"),
                        "main_text": it.get("mainText"),
                        "markdown_text": it.get("markdownText"),
                        "summary": it.get("summary"),
                    },
                )
            )
        return {
            "success": True,
            "provider": self.name,
            "engine_type": engine_type,
            "time_range": time_range,
            "request_id": data.get("requestId"),
            "items": [it.to_dict() for it in items],
            "scene_items": scene_items,
            "search_information": data.get("searchInformation"),
        }
