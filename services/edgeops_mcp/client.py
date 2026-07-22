"""毛竹 MCP — 与 claw-ops 同 REST / Bearer 鉴权（工具层 HTTP 客户端）。"""

import json
import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from services.edgeops_mcp.context import resolve_access_token, resolve_api_base_url

logger = logging.getLogger("edgeops.mcp.client")

DEFAULT_TIMEOUT = httpx.Timeout(330.0, connect=30.0)
SHORT_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


class EdgeOpsRestClient:
    """调用毛竹 HTTP API（Bearer JWT 或 eop_ token，与 claw-ops 一致）。"""

    def __init__(self, base_url: str, access_token: str):
        self.base_url = (base_url or "").rstrip("/")
        self.access_token = (access_token or "").strip()
        if not self.base_url:
            raise ValueError("毛竹 API base URL is required")
        if not self.access_token:
            raise ValueError("毛竹 access token is required")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-EdgeOps-Client": "mcp",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> Any:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"毛竹 HTTP request failed: {exc}") from exc
        text = resp.text or ""
        if resp.status_code >= 400:
            raise RuntimeError(
                f"毛竹 HTTP {resp.status_code}: {text[:1000] or resp.reason_phrase}"
            )
        if not text.strip():
            return {}
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"毛竹 returned non-JSON ({text[:280]}…)") from exc

    async def get_version(self) -> Any:
        return await self._request("GET", "/api/version", timeout=httpx.Timeout(15.0, connect=10.0))

    async def list_hosts(self, page: int = 1, page_size: int = 100) -> Any:
        page_size = max(1, min(100, page_size))
        return await self._request(
            "GET",
            "/api/hosts",
            params={"page": page, "page_size": page_size},
            timeout=SHORT_TIMEOUT,
        )

    async def search_hosts(
        self,
        query: str,
        *,
        group_id: int | None = None,
        tag_ids: list[int] | None = None,
        regex: str = "",
        case_sensitive: bool = False,
        limit: int = 50,
    ) -> Any:
        params: dict[str, Any] = {
            "query": (query or "").strip(),
            "limit": max(1, min(200, limit)),
        }
        if group_id is not None:
            params["group_id"] = group_id
        if tag_ids:
            params["tag_ids"] = tag_ids
        if regex:
            params["regex"] = regex
        if case_sensitive:
            params["case_sensitive"] = "true"
        return await self._request("GET", "/api/hosts/search", params=params, timeout=SHORT_TIMEOUT)

    async def search_hosts_by_prompt(
        self,
        query: str,
        *,
        group_id: int | None = None,
        tag_ids: list[int] | None = None,
        limit: int = 30,
        snippet_chars: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {
            "query": (query or "").strip(),
            "limit": max(1, min(100, limit)),
        }
        if group_id is not None:
            params["group_id"] = group_id
        if tag_ids:
            params["tag_ids"] = tag_ids
        if snippet_chars is not None:
            params["snippet_chars"] = snippet_chars
        return await self._request(
            "GET",
            "/api/integration/hosts/search-by-prompt",
            params=params,
            timeout=SHORT_TIMEOUT,
        )

    async def get_host(self, host_id: int) -> Any:
        return await self._request("GET", f"/api/hosts/{host_id}", timeout=SHORT_TIMEOUT)

    async def get_host_prompt(self, host_id: int) -> Any:
        return await self._request("GET", f"/api/ai/hosts/{host_id}/prompt", timeout=SHORT_TIMEOUT)

    async def list_host_tags(self) -> Any:
        return await self._request("GET", "/api/host-tags", timeout=SHORT_TIMEOUT)

    async def host_alive(self, host_id: int) -> Any:
        return await self._request("GET", f"/api/hosts/{host_id}/alive", timeout=httpx.Timeout(20.0, connect=10.0))

    async def host_stats(self) -> Any:
        return await self._request("GET", "/api/hosts/stats", timeout=httpx.Timeout(15.0, connect=10.0))

    async def search_best_practices(
        self,
        *,
        keyword: str | None = None,
        category: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Any:
        params: dict[str, Any] = {
            "page": max(1, page),
            "page_size": max(1, min(100, page_size)),
        }
        if keyword and keyword.strip():
            params["keyword"] = keyword.strip()
        if category and category.strip():
            params["category"] = category.strip()
        return await self._request("GET", "/api/best-practices", params=params, timeout=SHORT_TIMEOUT)

    async def ops_chat_complete(
        self,
        message: str,
        *,
        session_id: int | None = None,
        host_id: int | None = None,
        skip_secondary_assistant: bool = True,
        attachment_uuids: list[str] | None = None,
        ui_locale: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {
            "message": (message or "").strip(),
            "skip_secondary_assistant": skip_secondary_assistant,
            "attachment_uuids": list(attachment_uuids or []),
        }
        if session_id is not None:
            body["session_id"] = session_id
        if host_id is not None:
            body["host_id"] = host_id
        if ui_locale:
            body["ui_locale"] = ui_locale
        return await self._request(
            "POST",
            "/api/integration/ops-chat/complete",
            json_body=body,
        )

    async def ssh_channel_create(
        self,
        host_id: int,
        *,
        session_id: int | None = None,
        idle_close_sec: int | None = None,
    ) -> Any:
        body: dict[str, Any] = {"host_id": host_id}
        if session_id is not None:
            body["session_id"] = session_id
        if idle_close_sec is not None:
            body["idle_close_sec"] = idle_close_sec
        return await self._request("POST", "/api/ssh-channel", json_body=body, timeout=httpx.Timeout(120.0, connect=30.0))

    async def ssh_channel_list(
        self,
        *,
        all_open: bool | None = None,
        owner_type: str | None = None,
        owner_id: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if all_open:
            params["all_open"] = "true"
        if owner_type:
            params["owner_type"] = owner_type
        if owner_id:
            params["owner_id"] = owner_id
        return await self._request("GET", "/api/ssh-channel", params=params or None, timeout=SHORT_TIMEOUT)

    async def ssh_channel_info(self, channel_id: int, check_alive: bool | None = None) -> Any:
        params = {"check_alive": "1"} if check_alive else None
        return await self._request(
            "GET",
            f"/api/ssh-channel/{channel_id}",
            params=params,
            timeout=SHORT_TIMEOUT,
        )

    async def ssh_channel_send(self, channel_id: int, content: str) -> Any:
        return await self._request(
            "POST",
            f"/api/ssh-channel/{channel_id}/send",
            json_body={"content": content},
            timeout=SHORT_TIMEOUT,
        )

    async def ssh_channel_read_lines(
        self,
        channel_id: int,
        *,
        since_line: int | None = None,
        last_n: int | None = None,
        from_line: int | None = None,
        to_line: int | None = None,
        session_id: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if since_line is not None:
            params["since_line"] = since_line
        if last_n is not None:
            params["last_n"] = last_n
        if from_line is not None:
            params["from_line"] = from_line
        if to_line is not None:
            params["to_line"] = to_line
        if session_id is not None:
            params["session_id"] = session_id
        return await self._request(
            "GET",
            f"/api/ssh-channel/{channel_id}/lines",
            params=params or None,
            timeout=httpx.Timeout(120.0, connect=30.0),
        )

    async def ssh_channel_read(
        self,
        channel_id: int,
        *,
        max_chars: int | None = None,
        session_id: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if max_chars is not None:
            params["max_chars"] = max_chars
        if session_id is not None:
            params["session_id"] = session_id
        return await self._request(
            "GET",
            f"/api/ssh-channel/{channel_id}/read",
            params=params or None,
            timeout=httpx.Timeout(120.0, connect=30.0),
        )

    async def ssh_channel_has_new(self, channel_id: int, after_line: int | None = None) -> Any:
        params = {"after_line": after_line} if after_line is not None else None
        return await self._request(
            "GET",
            f"/api/ssh-channel/{channel_id}/has-new",
            params=params,
            timeout=SHORT_TIMEOUT,
        )

    async def ssh_channel_close(self, channel_id: int) -> Any:
        return await self._request("DELETE", f"/api/ssh-channel/{channel_id}", timeout=SHORT_TIMEOUT)

    async def ssh_channel_dump(
        self,
        channel_id: int,
        *,
        session_id: int | None = None,
        max_chars: int | None = None,
    ) -> Any:
        body: dict[str, Any] = {}
        if session_id is not None:
            body["session_id"] = session_id
        if max_chars is not None:
            body["max_chars"] = max_chars
        return await self._request(
            "POST",
            f"/api/ssh-channel/{channel_id}/dump",
            json_body=body,
            timeout=httpx.Timeout(120.0, connect=30.0),
        )

    async def ssh_channel_close_batch(
        self,
        *,
        session_id: int | None = None,
        owner_type: str | None = None,
        owner_id: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {}
        if session_id is not None:
            body["session_id"] = session_id
        if owner_type:
            body["owner_type"] = owner_type
        if owner_id:
            body["owner_id"] = owner_id
        return await self._request(
            "POST",
            "/api/ssh-channel/close-batch",
            json_body=body,
            timeout=SHORT_TIMEOUT,
        )

    async def read_spill(
        self,
        spill_id: str,
        date_subdir: str,
        *,
        mode: str = "head_tail",
        session_id: int | None = None,
        range_start: int | None = None,
        max_chars: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {
            "spill_id": spill_id.strip(),
            "date_subdir": date_subdir.strip(),
            "mode": mode or "head_tail",
        }
        if session_id is not None:
            params["session_id"] = session_id
        if range_start is not None:
            params["range_start"] = range_start
        if max_chars is not None:
            params["max_chars"] = max_chars
        return await self._request(
            "GET",
            "/api/integration/spill/read",
            params=params,
            timeout=httpx.Timeout(120.0, connect=30.0),
        )

    async def ssh_execute(
        self,
        host_id: int,
        command: str,
        *,
        timeout: int | None = None,
        detach: bool = False,
        poll_log: bool = False,
        log_path: str | None = None,
        tail_lines: int | None = None,
        session_id: int | None = None,
    ) -> Any:
        body: dict[str, Any] = {
            "host_id": host_id,
            "command": (command or "").strip(),
            "detach": detach,
            "poll_log": poll_log,
        }
        if timeout is not None:
            body["timeout"] = timeout
        if log_path:
            body["log_path"] = log_path
        if tail_lines is not None:
            body["tail_lines"] = tail_lines
        if session_id is not None:
            body["session_id"] = session_id
        return await self._request(
            "POST",
            "/api/integration/mcp/ssh-execute",
            json_body=body,
            timeout=httpx.Timeout(330.0, connect=30.0),
        )

    async def list_host_groups(self) -> Any:
        return await self._request("GET", "/api/host-groups", timeout=SHORT_TIMEOUT)

    async def get_host_groups_tree(self) -> Any:
        return await self._request("GET", "/api/host-groups/tree", timeout=SHORT_TIMEOUT)

    async def get_group_hosts(self, group_id: int) -> Any:
        return await self._request(
            "GET",
            f"/api/host-groups/{group_id}/hosts",
            timeout=SHORT_TIMEOUT,
        )

    async def probe_host_capabilities(
        self,
        host_id: int,
        *,
        refresh: bool = False,
        max_age_hours: int = 24,
        timeout: int = 40,
    ) -> Any:
        return await self._request(
            "POST",
            f"/api/integration/mcp/hosts/{host_id}/capabilities/probe",
            json_body={
                "refresh": refresh,
                "max_age_hours": max_age_hours,
                "timeout": timeout,
            },
            timeout=httpx.Timeout(120.0, connect=30.0),
        )

    async def get_host_capabilities(self, host_id: int) -> Any:
        return await self._request(
            "GET",
            f"/api/integration/mcp/hosts/{host_id}/capabilities",
            timeout=SHORT_TIMEOUT,
        )

    async def update_host_prompt(self, host_id: int, content: str) -> Any:
        return await self._request(
            "PUT",
            f"/api/integration/mcp/hosts/{host_id}/prompt",
            json_body={"content": content},
            timeout=SHORT_TIMEOUT,
        )

    async def append_host_prompt(self, host_id: int, text: str) -> Any:
        return await self._request(
            "POST",
            f"/api/integration/mcp/hosts/{host_id}/prompt/append",
            json_body={"text": text},
            timeout=SHORT_TIMEOUT,
        )

    async def list_maintenance_history(
        self,
        *,
        host: str | None = None,
        category: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Any:
        params: dict[str, Any] = {"page": max(1, page), "page_size": max(1, min(100, page_size))}
        if host:
            params["host"] = host
        if category:
            params["category"] = category
        return await self._request(
            "GET",
            "/api/maintenance-history",
            params=params,
            timeout=SHORT_TIMEOUT,
        )

    async def list_operation_logs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        host_id: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {"page": max(1, page), "page_size": max(1, min(100, page_size))}
        if host_id is not None:
            params["host_id"] = host_id
        return await self._request("GET", "/api/logs", params=params, timeout=SHORT_TIMEOUT)

    async def ops_orchestrate_capabilities(self) -> Any:
        return await self._request(
            "GET",
            "/api/integration/mcp/orchestrate/capabilities",
            timeout=httpx.Timeout(15.0, connect=10.0),
        )

    async def ops_orchestrate_chat(
        self,
        message: str,
        *,
        session_id: int | None = None,
        host_id: int | None = None,
    ) -> Any:
        body: dict[str, Any] = {"message": (message or "").strip()}
        if session_id is not None:
            body["session_id"] = session_id
        if host_id is not None:
            body["host_id"] = host_id
        return await self._request(
            "POST",
            "/api/integration/mcp/orchestrate/chat",
            json_body=body,
            timeout=httpx.Timeout(120.0, connect=30.0),
        )

    async def ops_task_list(
        self,
        *,
        session_id: int | None = None,
        status: str | None = None,
        limit: int = 30,
    ) -> Any:
        params: dict[str, Any] = {"limit": max(1, min(100, limit))}
        if session_id is not None:
            params["session_id"] = session_id
        if status:
            params["status"] = status
        return await self._request(
            "GET",
            "/api/integration/mcp/orchestrate/tasks",
            params=params,
            timeout=SHORT_TIMEOUT,
        )

    async def ops_task_output(self, task_id: int) -> Any:
        return await self._request(
            "GET",
            f"/api/integration/mcp/orchestrate/tasks/{task_id}",
            timeout=SHORT_TIMEOUT,
        )

    async def ops_task_control(
        self,
        task_id: int,
        action: str,
        *,
        message: str = "",
    ) -> Any:
        return await self._request(
            "POST",
            f"/api/integration/mcp/orchestrate/tasks/{task_id}/control",
            json_body={"action": action, "message": message},
            timeout=SHORT_TIMEOUT,
        )

    async def remote_fs_list(self, host_id: int, path: str = "/") -> Any:
        return await self._request(
            "GET",
            "/api/integration/mcp/remote-fs/list",
            params={"host_id": host_id, "path": path},
            timeout=SHORT_TIMEOUT,
        )

    async def remote_fs_read(self, host_id: int, path: str) -> Any:
        return await self._request(
            "GET",
            "/api/integration/mcp/remote-fs/read",
            params={"host_id": host_id, "path": path},
            timeout=httpx.Timeout(120.0, connect=30.0),
        )

    async def remote_fs_write(self, host_id: int, path: str, content: str) -> Any:
        return await self._request(
            "POST",
            "/api/integration/mcp/remote-fs/write",
            json_body={"host_id": host_id, "path": path, "content": content},
            timeout=httpx.Timeout(120.0, connect=30.0),
        )

    async def list_batch_jobs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        operation_type: str | None = None,
        status: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {"page": max(1, page), "page_size": max(1, min(100, page_size))}
        if operation_type:
            params["operation_type"] = operation_type
        if status:
            params["status"] = status
        return await self._request("GET", "/api/batch", params=params, timeout=SHORT_TIMEOUT)

    async def get_batch_job(self, batch_id: int) -> Any:
        return await self._request("GET", f"/api/batch/{batch_id}", timeout=SHORT_TIMEOUT)

    async def list_scheduled_tasks(self) -> Any:
        return await self._request("GET", "/api/scheduled-tasks", timeout=SHORT_TIMEOUT)

    async def get_scheduled_task(self, task_id: int) -> Any:
        return await self._request(
            "GET",
            f"/api/scheduled-tasks/{task_id}",
            timeout=SHORT_TIMEOUT,
        )

    async def list_triggered_tasks(self) -> Any:
        return await self._request("GET", "/api/triggered-tasks", timeout=SHORT_TIMEOUT)

    async def get_triggered_task(self, task_id: int) -> Any:
        return await self._request(
            "GET",
            f"/api/triggered-tasks/{task_id}",
            timeout=SHORT_TIMEOUT,
        )

    async def list_session_messages(self, session_id: int, *, limit: int = 50) -> Any:
        return await self._request(
            "GET",
            f"/api/integration/mcp/sessions/{session_id}/messages",
            params={"limit": max(1, min(200, limit))},
            timeout=SHORT_TIMEOUT,
        )

    async def service_credentials_enabled(self) -> Any:
        return await self._request(
            "GET",
            "/api/service-credentials/enabled",
            timeout=httpx.Timeout(15.0, connect=10.0),
        )

    async def list_service_credentials(
        self,
        *,
        command_hint: str | None = None,
        service: str | None = None,
        address: str | None = None,
        port: int | None = None,
        service_username: str | None = None,
        keyword: str | None = None,
        sort_by: str | None = "last_accessed_at",
        sort_order: str | None = "desc",
        limit: int | None = 50,
    ) -> Any:
        params: dict[str, Any] = {}
        if command_hint and command_hint.strip():
            params["command_hint"] = command_hint.strip()
        if service and service.strip():
            params["service"] = service.strip()
        if address is not None:
            params["address"] = address.strip() if isinstance(address, str) else address
        if port is not None:
            params["port"] = port
        if service_username and service_username.strip():
            params["service_username"] = service_username.strip()
        if keyword and keyword.strip():
            params["keyword"] = keyword.strip()
        if sort_by:
            params["sort_by"] = sort_by
        if sort_order:
            params["sort_order"] = sort_order
        if limit is not None:
            params["limit"] = max(1, min(200, int(limit)))
        return await self._request(
            "GET",
            "/api/service-credentials",
            params=params or None,
            timeout=SHORT_TIMEOUT,
        )

    async def send_service_password(
        self,
        *,
        credential_id: int | None = None,
        target: str,
        host_id: int | None = None,
        channel_id: int | None = None,
        slot: int | None = None,
        require_password_prompt: bool = True,
        use_host_login: bool = False,
    ) -> Any:
        body: dict[str, Any] = {
            "target": (target or "terminal").strip().lower(),
            "require_password_prompt": bool(require_password_prompt),
            "use_host_login": bool(use_host_login),
        }
        if credential_id is not None:
            body["credential_id"] = int(credential_id)
        if host_id is not None:
            body["host_id"] = host_id
        if channel_id is not None:
            body["channel_id"] = channel_id
        if slot is not None:
            body["slot"] = slot
        return await self._request(
            "POST",
            "/api/service-credentials/inject",
            json_body=body,
            timeout=SHORT_TIMEOUT,
        )

    async def http_request(
        self,
        *,
        url: str,
        method: str = "GET",
        headers: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: str | None = None,
        body_encoding: str = "text",
        timeout: int | None = None,
        max_response_bytes: int | None = None,
        follow_redirects: bool = True,
        session_id: int | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "url": url,
            "method": method,
            "body_encoding": body_encoding,
            "follow_redirects": follow_redirects,
        }
        if headers:
            payload["headers"] = headers
        if query:
            payload["query"] = query
        if body is not None:
            payload["body"] = body
        if timeout is not None:
            payload["timeout"] = timeout
        if max_response_bytes is not None:
            payload["max_response_bytes"] = max_response_bytes
        if session_id is not None:
            payload["session_id"] = session_id
        return await self._request(
            "POST",
            "/api/integration/mcp/http-request",
            json_body=payload,
            timeout=httpx.Timeout(float(timeout or 120), connect=30.0),
        )

    async def http_download(
        self,
        *,
        url: str,
        local_path: str,
        headers: dict[str, Any] | None = None,
        session_managed: bool | None = None,
        max_bytes: int | None = None,
        chunked: bool = False,
        chunk_size: int | None = None,
        chunk_index: int | None = None,
        merge_chunks: bool = True,
        delete_parts: bool = True,
        timeout: int | None = None,
        follow_redirects: bool = True,
        session_id: int | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "url": url,
            "local_path": local_path,
            "follow_redirects": follow_redirects,
            "chunked": chunked,
            "merge_chunks": merge_chunks,
            "delete_parts": delete_parts,
        }
        if headers:
            payload["headers"] = headers
        if session_managed is not None:
            payload["session_managed"] = session_managed
        if max_bytes is not None:
            payload["max_bytes"] = max_bytes
        if chunk_size is not None:
            payload["chunk_size"] = chunk_size
        if chunk_index is not None:
            payload["chunk_index"] = chunk_index
        if timeout is not None:
            payload["timeout"] = timeout
        if session_id is not None:
            payload["session_id"] = session_id
        return await self._request(
            "POST",
            "/api/integration/mcp/http-download",
            json_body=payload,
            timeout=httpx.Timeout(float(timeout or 3600), connect=30.0),
        )

    async def http_download_merge(
        self,
        *,
        local_path: str,
        part_paths: list[str] | None = None,
        delete_parts: bool = True,
        session_id: int | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "local_path": local_path,
            "delete_parts": delete_parts,
        }
        if part_paths:
            payload["part_paths"] = part_paths
        if session_id is not None:
            payload["session_id"] = session_id
        return await self._request(
            "POST",
            "/api/integration/mcp/http-download-merge",
            json_body=payload,
            timeout=httpx.Timeout(3600.0, connect=30.0),
        )

    async def http_upload(
        self,
        *,
        url: str,
        local_path: str,
        method: str = "POST",
        headers: dict[str, Any] | None = None,
        field_name: str = "file",
        form_fields: dict[str, Any] | None = None,
        content_type: str | None = None,
        multipart: bool = True,
        max_bytes: int | None = None,
        timeout: int | None = None,
        follow_redirects: bool = True,
        session_id: int | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "url": url,
            "local_path": local_path,
            "method": method,
            "field_name": field_name,
            "multipart": multipart,
            "follow_redirects": follow_redirects,
        }
        if headers:
            payload["headers"] = headers
        if form_fields:
            payload["form_fields"] = form_fields
        if content_type:
            payload["content_type"] = content_type
        if max_bytes is not None:
            payload["max_bytes"] = max_bytes
        if timeout is not None:
            payload["timeout"] = timeout
        if session_id is not None:
            payload["session_id"] = session_id
        return await self._request(
            "POST",
            "/api/integration/mcp/http-upload",
            json_body=payload,
            timeout=httpx.Timeout(float(timeout or 3600), connect=30.0),
        )


def create_client(*, ctx: Any | None = None) -> EdgeOpsRestClient:
    token = resolve_access_token(ctx)
    return EdgeOpsRestClient(resolve_api_base_url(), token)
