"""GitHub 搜索 Provider。

走 GitHub REST API：https://docs.github.com/en/rest/search/search
- 无 token 也可用（匿名限速 60 次/小时）；配置个人 PAT 后限速提升到 5000 次/小时。
- 支持仓库 / 代码 / Issue / 用户 四种搜索类型，options.type 控制。
"""
from __future__ import annotations

import logging

import httpx

from .base import ConfigField, SearchProvider, SearchResultItem, SEARCH_HTTP_USER_AGENT

logger = logging.getLogger("edgeops.search.github")

_GITHUB_API = "https://api.github.com"
_VALID_TYPES = ("repositories", "code", "issues", "users")


class GitHubProvider(SearchProvider):
    name = "github"
    display_name = "GitHub search"
    description = (
        "Search repositories, code, issues, and users via the GitHub REST API. "
        "Works without a token (60 req/h); with a personal access token (PAT), "
        "5,000 req/h and private repo access."
    )
    docs_url = "https://docs.github.com/en/rest/search/search"
    requires_key = False  # 关键：未配 key 也可用
    config_schema = [
        ConfigField(
            key="api_key",
            label="Personal Access Token (PAT)",
            type="password",
            placeholder="ghp_xxx or github_pat_xxx (optional)",
            required=False,
            secret=True,
            help=(
                "Optional. Create at https://github.com/settings/tokens. "
                "For public content, public_repo is enough; for private repos, use repo."
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
        opts = options or {}
        search_type = (opts.get("type") or "repositories").strip().lower()
        if search_type not in _VALID_TYPES:
            return {
                "success": False,
                "error": f"type 无效：{search_type}，可选 {', '.join(_VALID_TYPES)}",
            }
        try:
            limit = max(1, min(int(opts.get("limit") or 10), 50))
        except (TypeError, ValueError):
            limit = 10
        params: dict[str, str | int] = {
            "q": query.strip(),
            "per_page": limit,
        }
        if opts.get("sort"):
            params["sort"] = str(opts["sort"])
        if opts.get("order"):
            params["order"] = str(opts["order"])

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": SEARCH_HTTP_USER_AGENT,
        }
        if api_key and api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"

        url = f"{_GITHUB_API}/search/{search_type}"
        try:
            timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=headers, params=params)
        except httpx.HTTPError as e:
            return {"success": False, "error": f"GitHub 请求失败：{e}"}

        if resp.status_code == 401:
            return {"success": False, "error": "GitHub 鉴权失败：API Token 无效或已过期"}
        if resp.status_code == 403:
            # 403 一般是限速；GitHub 在 X-RateLimit-Remaining 里说明
            remaining = resp.headers.get("X-RateLimit-Remaining", "?")
            reset = resp.headers.get("X-RateLimit-Reset", "?")
            msg = "GitHub 限速触发"
            if not api_key:
                msg += "（匿名访问 60 次/小时；建议配置 PAT 提升至 5000 次/小时）"
            else:
                msg += f"（剩余 {remaining}，重置时间戳 {reset}）"
            return {"success": False, "error": msg}
        if resp.status_code == 422:
            return {"success": False, "error": f"GitHub 拒绝查询语法：{resp.text[:200]}"}
        if resp.status_code != 200:
            return {
                "success": False,
                "error": f"GitHub HTTP {resp.status_code}: {resp.text[:300]}",
            }
        try:
            data = resp.json()
        except Exception as e:
            return {"success": False, "error": f"GitHub 响应解析失败：{e}"}

        items_raw = data.get("items") or []
        items = [self._normalize(it, search_type) for it in items_raw]
        return {
            "success": True,
            "provider": self.name,
            "type": search_type,
            "total_count": data.get("total_count"),
            "incomplete_results": data.get("incomplete_results"),
            "items": [it.to_dict() for it in items],
        }

    @staticmethod
    def _normalize(it: dict, search_type: str) -> SearchResultItem:
        """把 GitHub 原始结果统一成 SearchResultItem 风格。"""
        if search_type == "repositories":
            return SearchResultItem(
                title=it.get("full_name") or it.get("name") or "",
                url=it.get("html_url") or "",
                snippet=(it.get("description") or "")[:300],
                source="github:repo",
                extra={
                    "stars": it.get("stargazers_count"),
                    "forks": it.get("forks_count"),
                    "language": it.get("language"),
                    "updated_at": it.get("updated_at"),
                    "owner": (it.get("owner") or {}).get("login"),
                    "default_branch": it.get("default_branch"),
                    "clone_url": it.get("clone_url"),
                    "ssh_url": it.get("ssh_url"),
                },
            )
        if search_type == "code":
            return SearchResultItem(
                title=f"{(it.get('repository') or {}).get('full_name', '')}/{it.get('path', '')}",
                url=it.get("html_url") or "",
                snippet=(it.get("name") or ""),
                source="github:code",
                extra={
                    "path": it.get("path"),
                    "repo": (it.get("repository") or {}).get("full_name"),
                    "score": it.get("score"),
                },
            )
        if search_type == "issues":
            return SearchResultItem(
                title=it.get("title") or "",
                url=it.get("html_url") or "",
                snippet=(it.get("body") or "")[:300],
                source="github:issue",
                extra={
                    "state": it.get("state"),
                    "user": (it.get("user") or {}).get("login"),
                    "comments": it.get("comments"),
                    "created_at": it.get("created_at"),
                },
            )
        if search_type == "users":
            return SearchResultItem(
                title=it.get("login") or "",
                url=it.get("html_url") or "",
                snippet=(it.get("type") or ""),
                source="github:user",
                extra={
                    "score": it.get("score"),
                    "avatar_url": it.get("avatar_url"),
                },
            )
        return SearchResultItem(title=str(it.get("name") or ""), url=str(it.get("html_url") or ""))
