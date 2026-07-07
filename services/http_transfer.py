"""HTTP 出站请求 / 下载 / 上传：流式传输、进度回调、协作取消、SSRF 防护、分块下载合并。"""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import mimetypes
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urlparse, urlunparse

import httpx

import config

ProgressEmit = Callable[[dict[str, Any]], Any]
CancelCheck = Callable[[], bool]

_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
})
_METADATA_IPS = frozenset({"169.254.169.254", "169.254.170.2", "fd00:ec2::254"})
_CONTENT_RANGE_RE = re.compile(r"bytes\s+\d+-\d+/(\d+|\*)", re.IGNORECASE)


@dataclass
class HttpTransferResult:
    success: bool
    error: Optional[str] = None
    status_code: Optional[int] = None
    response_headers: dict[str, str] = field(default_factory=dict)
    body_text: Optional[str] = None
    body_base64: Optional[str] = None
    content_type: Optional[str] = None
    bytes_transferred: int = 0
    duration_sec: float = 0.0
    interrupted: bool = False
    local_path: Optional[str] = None
    url: Optional[str] = None
    truncated: bool = False
    content_length: Optional[int] = None
    chunks_total: Optional[int] = None
    chunk_index: Optional[int] = None
    chunk_size: Optional[int] = None
    chunk_paths: list[str] = field(default_factory=list)
    merged: bool = False
    accept_ranges: bool = False


@dataclass
class _ProgressState:
    direction: str
    url: str = ""
    started_at: float = field(default_factory=time.time)
    total_bytes: int = 0
    transferred_bytes: int = 0
    current_file: str = ""
    last_emit_at: float = 0.0
    last_emit_pct: float = -1.0
    emit: Optional[ProgressEmit] = None
    cancel: Optional[CancelCheck] = None

    def check_cancel(self) -> bool:
        return bool(self.cancel and self.cancel())

    def _speed_bps(self) -> float:
        elapsed = max(0.001, time.time() - self.started_at)
        return self.transferred_bytes / elapsed

    def _eta_sec(self) -> Optional[float]:
        speed = self._speed_bps()
        if self.total_bytes <= 0 or speed <= 0:
            return None
        remain = max(0, self.total_bytes - self.transferred_bytes)
        return remain / speed

    async def emit_progress(self, *, force: bool = False, phase: str = "running") -> None:
        if not self.emit:
            return
        now = time.time()
        pct = (
            round(100.0 * self.transferred_bytes / self.total_bytes, 1)
            if self.total_bytes > 0
            else 0.0
        )
        if not force:
            if now - self.last_emit_at < 0.45 and abs(pct - self.last_emit_pct) < 0.8:
                return
        self.last_emit_at = now
        self.last_emit_pct = pct
        eta = self._eta_sec()
        ev = {
            "kind": "transfer_progress",
            "phase": phase,
            "direction": self.direction,
            "transferred": self.transferred_bytes,
            "total": self.total_bytes,
            "percent": pct,
            "file": self.current_file or self.url,
            "file_index": 1,
            "files_total": 1,
            "elapsed_sec": round(now - self.started_at, 1),
            "speed_bps": int(self._speed_bps()),
            "eta_sec": round(eta, 1) if eta is not None else None,
        }
        result = self.emit(ev)
        if asyncio.iscoroutine(result):
            await result


def _http_enabled() -> bool:
    return bool(getattr(config, "HTTP_TOOL_ENABLED", True))


def _allow_insecure() -> bool:
    return bool(getattr(config, "HTTP_TOOL_ALLOW_INSECURE", False))


def _block_private() -> bool:
    return bool(getattr(config, "HTTP_TOOL_SSRF_BLOCK_PRIVATE", True))


def _default_timeout() -> int:
    return max(5, int(getattr(config, "HTTP_TOOL_DEFAULT_TIMEOUT_SEC", 60)))


def _max_timeout() -> int:
    return max(_default_timeout(), int(getattr(config, "HTTP_TOOL_MAX_TIMEOUT_SEC", 3600)))


def _max_response_bytes() -> int:
    return max(1024, int(getattr(config, "HTTP_TOOL_MAX_RESPONSE_BYTES", 5 * 1024 * 1024)))


def _default_download_chunk_size() -> int:
    return max(1024 * 1024, int(getattr(config, "HTTP_TOOL_DOWNLOAD_CHUNK_SIZE", 64 * 1024 * 1024)))


def _resolve_transfer_cap(explicit: int | None, config_attr: str) -> int | None:
    """解析传输体积上限；None 表示不限制（配置或参数为 0）。"""
    if explicit is not None:
        try:
            v = int(explicit)
        except (TypeError, ValueError):
            v = int(getattr(config, config_attr, 0))
        else:
            return None if v <= 0 else v
    cfg = int(getattr(config, config_attr, 0))
    return None if cfg <= 0 else cfg


def _clamp_timeout(timeout: int | None) -> int:
    base = _default_timeout() if timeout is None else int(timeout)
    return max(5, min(base, _max_timeout()))


def _make_cancel_check(cancel_event: Any) -> CancelCheck:
    def _cancel() -> bool:
        if cancel_event is None:
            return False
        if hasattr(cancel_event, "is_set"):
            return bool(cancel_event.is_set())
        return False

    return _cancel


def _chunk_part_path(output_path: Path, index: int) -> Path:
    return output_path.parent / f"{output_path.name}.part{index:06d}"


def _list_chunk_part_paths(output_path: Path) -> list[Path]:
    parent = output_path.parent
    prefix = output_path.name + ".part"
    parts = [p for p in parent.iterdir() if p.is_file() and p.name.startswith(prefix)]
    return sorted(parts, key=lambda p: p.name)


def _parse_total_from_content_range(value: str | None) -> int | None:
    if not value:
        return None
    m = _CONTENT_RANGE_RE.search(value.strip())
    if not m:
        return None
    total = m.group(1)
    if total == "*":
        return None
    try:
        return int(total)
    except ValueError:
        return None


def merge_http_download_chunks(
    output_path: Path,
    part_paths: list[Path],
    *,
    delete_parts: bool = True,
) -> int:
    """按顺序合并分块文件，返回写入总字节。"""
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with output_path.open("wb") as out:
        for part in part_paths:
            part = part.resolve()
            if not part.is_file():
                raise FileNotFoundError(f"分块不存在: {part}")
            with part.open("rb") as inp:
                while True:
                    buf = inp.read(1024 * 1024)
                    if not buf:
                        break
                    out.write(buf)
                    total += len(buf)
    if delete_parts:
        for part in part_paths:
            try:
                part.unlink()
            except OSError:
                pass
    return total


async def _http_probe_download_meta(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
) -> tuple[int | None, bool, int]:
    """返回 (content_length, accept_ranges, status_code)。"""
    req_headers = dict(headers)
    status = 0
    content_length: int | None = None
    accept_ranges = False
    try:
        head = await client.head(url, headers=req_headers)
        status = head.status_code
        if status < 400:
            cl = head.headers.get("content-length")
            if cl and str(cl).isdigit():
                content_length = int(cl)
            accept_ranges = (head.headers.get("accept-ranges") or "").strip().lower() == "bytes"
            if accept_ranges or content_length is not None:
                return content_length, accept_ranges, status
    except httpx.HTTPError:
        pass
    probe_headers = dict(req_headers)
    probe_headers["Range"] = "bytes=0-0"
    async with client.stream("GET", url, headers=probe_headers) as resp:
        status = resp.status_code
        if status >= 400:
            text = (await resp.aread())[:500].decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {status}: {text or resp.reason_phrase}")
        cr = resp.headers.get("content-range")
        total = _parse_total_from_content_range(cr)
        if total is not None:
            content_length = total
        if resp.status_code == 206:
            accept_ranges = True
        elif (resp.headers.get("accept-ranges") or "").strip().lower() == "bytes":
            accept_ranges = True
        cl = resp.headers.get("content-length")
        if content_length is None and cl and str(cl).isdigit() and resp.status_code == 200:
            content_length = int(cl)
    return content_length, accept_ranges, status


async def _stream_download_to_file(
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    local_path: Path,
    cap: int | None,
    prog: _ProgressState,
    cancel: CancelCheck,
) -> tuple[int, int, dict[str, str]]:
    transferred = 0
    status_code = 0
    resp_headers: dict[str, str] = {}
    async with client.stream("GET", url, headers=headers or None) as resp:
        status_code = resp.status_code
        resp_headers = _response_headers_dict(resp)
        if resp.status_code >= 400:
            text = (await resp.aread())[:2000].decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {resp.status_code}: {text or resp.reason_phrase}")
        total_hdr = resp.headers.get("content-length")
        if total_hdr and str(total_hdr).isdigit():
            total = int(total_hdr)
            prog.total_bytes = min(total, cap) if cap is not None else total
        elif cap is not None:
            prog.total_bytes = cap
        else:
            prog.total_bytes = 0
        with local_path.open("wb") as fh:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                if cancel():
                    await prog.emit_progress(force=True, phase="cancelled")
                    raise _TransferInterrupted()
                if not chunk:
                    continue
                if cap is not None and transferred + len(chunk) > cap:
                    remain = cap - transferred
                    if remain > 0:
                        fh.write(chunk[:remain])
                        transferred += remain
                    await prog.emit_progress(force=True, phase="done")
                    raise _TransferCapExceeded(cap)
                fh.write(chunk)
                transferred += len(chunk)
                prog.transferred_bytes = transferred
                await prog.emit_progress()
    return transferred, status_code, resp_headers


async def _download_range_to_file(
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    start: int,
    end: int,
    dest_path: Path,
    prog: _ProgressState,
    cancel: CancelCheck,
    cap: int | None,
) -> int:
    req_headers = dict(headers)
    req_headers["Range"] = f"bytes={start}-{end}"
    transferred = 0
    async with client.stream("GET", url, headers=req_headers) as resp:
        if resp.status_code not in (200, 206):
            text = (await resp.aread())[:2000].decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {resp.status_code}: {text or resp.reason_phrase}")
        expected = end - start + 1
        if cap is not None:
            expected = min(expected, max(0, cap - prog.transferred_bytes))
        with dest_path.open("wb") as fh:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                if cancel():
                    await prog.emit_progress(force=True, phase="cancelled")
                    raise _TransferInterrupted()
                if not chunk:
                    continue
                if cap is not None and prog.transferred_bytes + len(chunk) > cap:
                    remain = cap - prog.transferred_bytes
                    if remain > 0:
                        fh.write(chunk[:remain])
                        transferred += remain
                        prog.transferred_bytes += remain
                    raise _TransferCapExceeded(cap)
                fh.write(chunk)
                transferred += len(chunk)
                prog.transferred_bytes += len(chunk)
                await prog.emit_progress()
        if expected > 0 and transferred < expected and resp.status_code == 206:
            raise RuntimeError(f"分块下载不完整: 期望 {expected} 字节，实际 {transferred} 字节")
    return transferred


class _TransferInterrupted(Exception):
    pass


class _TransferCapExceeded(Exception):
    def __init__(self, cap: int):
        self.cap = cap
        super().__init__(f"超过上限 {cap} 字节")


def _is_blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if str(addr) in _METADATA_IPS:
        return True
    if not _block_private():
        return addr.is_loopback
    return bool(
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _resolve_host_ips(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"无法解析主机名 {hostname!r}: {exc}") from exc
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise ValueError(f"无法解析主机名 {hostname!r}")
    return ips


def validate_outbound_url(url: str, *, allow_insecure: bool | None = None) -> str:
    """校验出站 URL，返回规范化后的 URL。禁止 SSRF 目标。"""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("url 不能为空")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("仅支持 http:// 或 https:// URL")
    insecure_ok = _allow_insecure() if allow_insecure is None else bool(allow_insecure)
    if parsed.scheme == "http" and not insecure_ok:
        raise ValueError("HTTP 明文请求已禁用，请使用 https:// 或设置 EDGEOPS_HTTP_TOOL_ALLOW_INSECURE=true")
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise ValueError("URL 缺少主机名")
    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(".localhost"):
        raise ValueError(f"禁止访问主机: {hostname}")
    if hostname == "0.0.0.0":
        raise ValueError("禁止访问 0.0.0.0")
    for ip_str in _resolve_host_ips(hostname):
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_blocked_ip(addr):
            raise ValueError(f"禁止访问内网/保留地址: {ip_str} ({hostname})")
    netloc = hostname
    if parsed.port:
        netloc = f"{hostname}:{parsed.port}"
    clean = urlunparse((parsed.scheme, netloc, parsed.path or "/", parsed.params, parsed.query, parsed.fragment))
    return clean


def _normalize_headers(headers: Any) -> dict[str, str]:
    if not headers:
        return {}
    if not isinstance(headers, dict):
        raise ValueError("headers 必须是对象")
    out: dict[str, str] = {}
    for k, v in headers.items():
        key = str(k).strip()
        if not key:
            continue
        out[key] = str(v) if v is not None else ""
    return out


def _append_query(url: str, query: dict[str, str]) -> str:
    if not query:
        return url
    parsed = urlparse(url)
    merged = urlencode({**dict(httpx.QueryParams(parsed.query)), **query})
    return urlunparse(parsed._replace(query=merged))


def _response_headers_dict(resp: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in resp.headers.items()}


def _decode_body(data: bytes, content_type: str | None, *, max_chars: int) -> tuple[str | None, str | None, bool]:
    if not data:
        return "", None, False
    ct = (content_type or "").lower()
    if "json" in ct or "text" in ct or "xml" in ct or "javascript" in ct or "html" in ct:
        text = data.decode("utf-8", errors="replace")
        if len(text) > max_chars:
            return text[:max_chars], None, True
        return text, None, False
    try:
        text = data.decode("utf-8")
        if len(text) <= max_chars and "\x00" not in text:
            return text, None, False
    except UnicodeDecodeError:
        pass
    b64 = base64.b64encode(data).decode("ascii")
    if len(b64) > max_chars:
        return None, b64[:max_chars], True
    return None, b64, False


async def http_request_async(
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    body: Any = None,
    body_encoding: str = "text",
    timeout: int | None = None,
    max_response_bytes: int | None = None,
    follow_redirects: bool = True,
    stream_callback: Optional[ProgressEmit] = None,
    cancel_event: Any = None,
) -> HttpTransferResult:
    if not _http_enabled():
        return HttpTransferResult(success=False, error="HTTP 工具已禁用")
    started = time.time()
    m = (method or "GET").strip().upper()
    if m not in _HTTP_METHODS:
        return HttpTransferResult(success=False, error=f"不支持的 HTTP 方法: {method}")
    try:
        safe_url = validate_outbound_url(url)
        safe_url = _append_query(safe_url, query or {})
        req_headers = _normalize_headers(headers)
        to = _clamp_timeout(timeout)
        cap = max(1024, int(max_response_bytes or _max_response_bytes()))
    except ValueError as exc:
        return HttpTransferResult(success=False, error=str(exc))

    cancel = _make_cancel_check(cancel_event)
    content: str | bytes | None = None
    json_body: Any = None
    enc = (body_encoding or "text").strip().lower()
    if body is not None and m not in ("GET", "HEAD", "OPTIONS"):
        if enc == "json":
            if isinstance(body, str):
                try:
                    json_body = json.loads(body)
                except json.JSONDecodeError as exc:
                    return HttpTransferResult(success=False, error=f"body JSON 无效: {exc}")
            else:
                json_body = body
        elif enc == "base64":
            try:
                content = base64.b64decode(str(body), validate=True)
            except (binascii.Error, ValueError) as exc:
                return HttpTransferResult(success=False, error=f"body base64 无效: {exc}")
        else:
            content = body if isinstance(body, (bytes, str)) else str(body)
            if isinstance(content, str):
                content = content.encode("utf-8")

    timeout_cfg = httpx.Timeout(float(to), connect=min(30.0, float(to)))
    try:
        async with httpx.AsyncClient(timeout=timeout_cfg, follow_redirects=follow_redirects) as client:
            if cancel():
                return HttpTransferResult(success=False, error="已取消", interrupted=True, url=safe_url)
            resp = await client.request(
                m,
                safe_url,
                headers=req_headers or None,
                content=content if json_body is None else None,
                json=json_body,
            )
            if cancel():
                return HttpTransferResult(success=False, error="已取消", interrupted=True, url=safe_url)
            data = resp.content
            truncated = len(data) > cap
            if truncated:
                data = data[:cap]
            ct = resp.headers.get("content-type", "")
            body_text, body_b64, decode_truncated = _decode_body(data, ct, max_chars=cap)
            return HttpTransferResult(
                success=True,
                status_code=resp.status_code,
                response_headers=_response_headers_dict(resp),
                body_text=body_text,
                body_base64=body_b64,
                content_type=ct or None,
                bytes_transferred=len(data),
                duration_sec=round(time.time() - started, 3),
                url=safe_url,
                truncated=truncated or decode_truncated,
            )
    except httpx.HTTPError as exc:
        return HttpTransferResult(
            success=False,
            error=f"HTTP 请求失败: {exc}",
            duration_sec=round(time.time() - started, 3),
            url=safe_url,
        )


async def http_download_async(
    *,
    url: str,
    local_path: Path,
    headers: dict[str, str] | None = None,
    timeout: int | None = None,
    max_bytes: int | None = None,
    follow_redirects: bool = True,
    chunk_size: int | None = None,
    chunked: bool = False,
    chunk_index: int | None = None,
    merge_chunks: bool = True,
    delete_parts: bool = True,
    stream_callback: Optional[ProgressEmit] = None,
    cancel_event: Any = None,
) -> HttpTransferResult:
    if not _http_enabled():
        return HttpTransferResult(success=False, error="HTTP 工具已禁用")
    started = time.time()
    cancel = _make_cancel_check(cancel_event)

    try:
        safe_url = validate_outbound_url(url)
        req_headers = _normalize_headers(headers)
        to = _clamp_timeout(timeout)
        cap = _resolve_transfer_cap(max_bytes, "HTTP_TOOL_MAX_DOWNLOAD_BYTES")
        local_path = local_path.resolve()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if chunk_size is not None:
            chunk_size = max(1024 * 1024, int(chunk_size))
        elif chunked:
            chunk_size = _default_download_chunk_size()
        if chunk_index is not None:
            chunk_index = max(0, int(chunk_index))
    except (ValueError, OSError) as exc:
        return HttpTransferResult(success=False, error=str(exc))

    prog = _ProgressState(
        direction="download",
        url=safe_url,
        current_file=local_path.name,
        emit=stream_callback,
        cancel=cancel,
    )
    await prog.emit_progress(force=True, phase="start")
    timeout_cfg = httpx.Timeout(float(to), connect=min(30.0, float(to)))

    try:
        async with httpx.AsyncClient(timeout=timeout_cfg, follow_redirects=follow_redirects) as client:
            if chunk_size:
                content_length, accept_ranges, _status = await _http_probe_download_meta(
                    client, safe_url, req_headers
                )
                if not accept_ranges:
                    chunk_size = None
                else:
                    return await _http_download_chunked(
                        client=client,
                        url=safe_url,
                        headers=req_headers,
                        local_path=local_path,
                        cap=cap,
                        chunk_size=chunk_size,
                        chunk_index=chunk_index,
                        merge_chunks=merge_chunks,
                        delete_parts=delete_parts,
                        content_length=content_length,
                        prog=prog,
                        cancel=cancel,
                        started=started,
                    )

            transferred, status_code, resp_headers = await _stream_download_to_file(
                client=client,
                url=safe_url,
                headers=req_headers,
                local_path=local_path,
                cap=cap,
                prog=prog,
                cancel=cancel,
            )
            await prog.emit_progress(force=True, phase="done")
            return HttpTransferResult(
                success=True,
                status_code=status_code,
                response_headers=resp_headers,
                bytes_transferred=transferred,
                local_path=str(local_path),
                url=safe_url,
                duration_sec=round(time.time() - started, 3),
                content_length=transferred,
            )
    except _TransferInterrupted:
        return HttpTransferResult(
            success=False,
            error="下载已取消",
            interrupted=True,
            bytes_transferred=prog.transferred_bytes,
            local_path=str(local_path),
            url=safe_url,
            duration_sec=round(time.time() - started, 3),
        )
    except _TransferCapExceeded as exc:
        return HttpTransferResult(
            success=False,
            error=str(exc),
            bytes_transferred=prog.transferred_bytes,
            local_path=str(local_path),
            url=safe_url,
            truncated=True,
            duration_sec=round(time.time() - started, 3),
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        return HttpTransferResult(
            success=False,
            error=f"HTTP 下载失败: {exc}",
            bytes_transferred=prog.transferred_bytes,
            local_path=str(local_path),
            url=safe_url,
            duration_sec=round(time.time() - started, 3),
        )


async def _http_download_chunked(
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    local_path: Path,
    cap: int | None,
    chunk_size: int,
    chunk_index: int | None,
    merge_chunks: bool,
    delete_parts: bool,
    content_length: int | None,
    prog: _ProgressState,
    cancel: CancelCheck,
    started: float,
) -> HttpTransferResult:
    effective_total = content_length
    if cap is not None:
        effective_total = min(content_length, cap) if content_length is not None else cap
    if effective_total is not None and effective_total > 0:
        chunks_total = (effective_total + chunk_size - 1) // chunk_size
        prog.total_bytes = effective_total
    else:
        chunks_total = None

    if chunk_index is not None:
        if chunks_total is not None and chunk_index >= chunks_total:
            return HttpTransferResult(
                success=False,
                error=f"chunk_index 超出范围（共 {chunks_total} 块）",
                url=url,
            )
        start = chunk_index * chunk_size
        if cap is not None:
            start = min(start, max(0, cap - 1))
        end = start + chunk_size - 1
        if effective_total is not None:
            end = min(end, effective_total - 1)
        if cap is not None:
            end = min(end, cap - 1)
        part_path = _chunk_part_path(local_path, chunk_index)
        prog.current_file = part_path.name
        prog.transferred_bytes = 0
        if effective_total is not None:
            prog.total_bytes = end - start + 1
        await _download_range_to_file(
            client=client,
            url=url,
            headers=headers,
            start=start,
            end=end,
            dest_path=part_path,
            prog=prog,
            cancel=cancel,
            cap=cap,
        )
        await prog.emit_progress(force=True, phase="done")
        return HttpTransferResult(
            success=True,
            status_code=206,
            bytes_transferred=prog.transferred_bytes,
            local_path=str(part_path),
            url=url,
            duration_sec=round(time.time() - started, 3),
            content_length=content_length,
            chunks_total=chunks_total,
            chunk_index=chunk_index,
            chunk_size=chunk_size,
            chunk_paths=[str(part_path)],
            accept_ranges=True,
        )

    part_paths: list[Path] = []
    idx = 0
    prog.transferred_bytes = 0
    while True:
        if cancel():
            await prog.emit_progress(force=True, phase="cancelled")
            return HttpTransferResult(
                success=False,
                error="下载已取消",
                interrupted=True,
                bytes_transferred=prog.transferred_bytes,
                local_path=str(local_path),
                url=url,
                chunk_paths=[str(p) for p in part_paths],
                duration_sec=round(time.time() - started, 3),
                chunks_total=chunks_total,
                chunk_size=chunk_size,
                accept_ranges=True,
            )
        start = idx * chunk_size
        if cap is not None and start >= cap:
            break
        if effective_total is not None and start >= effective_total:
            break
        end = start + chunk_size - 1
        if effective_total is not None:
            end = min(end, effective_total - 1)
        if cap is not None:
            end = min(end, cap - 1)
        part_path = _chunk_part_path(local_path, idx)
        prog.current_file = part_path.name
        got = await _download_range_to_file(
            client=client,
            url=url,
            headers=headers,
            start=start,
            end=end,
            dest_path=part_path,
            prog=prog,
            cancel=cancel,
            cap=cap,
        )
        part_paths.append(part_path)
        idx += 1
        if got < (end - start + 1):
            break
        if chunks_total is not None and idx >= chunks_total:
            break
        if cap is not None and prog.transferred_bytes >= cap:
            break

    if not part_paths:
        return HttpTransferResult(success=False, error="未下载任何分块", url=url)

    merged = False
    output_path = local_path
    if merge_chunks:
        if len(part_paths) == 1:
            part_paths[0].replace(output_path)
            merged = True
            if delete_parts:
                pass
        else:
            total_written = await asyncio.to_thread(
                merge_http_download_chunks,
                output_path,
                part_paths,
                delete_parts=delete_parts,
            )
            merged = True
            prog.transferred_bytes = total_written
    await prog.emit_progress(force=True, phase="done")
    return HttpTransferResult(
        success=True,
        status_code=206,
        bytes_transferred=prog.transferred_bytes,
        local_path=str(output_path if merged else part_paths[-1]),
        url=url,
        duration_sec=round(time.time() - started, 3),
        content_length=content_length,
        chunks_total=len(part_paths) if chunks_total is None else chunks_total,
        chunk_size=chunk_size,
        chunk_paths=[str(p) for p in part_paths],
        merged=merged,
        accept_ranges=True,
    )


async def http_download_merge_async(
    *,
    output_path: Path,
    part_paths: list[Path] | None = None,
    delete_parts: bool = True,
) -> HttpTransferResult:
    """合并已下载的分块文件（`<output>.part000000` …）为最终文件。"""
    started = time.time()
    try:
        output_path = output_path.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        parts = list(part_paths or _list_chunk_part_paths(output_path))
        if not parts:
            return HttpTransferResult(
                success=False,
                error=f"未找到分块文件: {output_path.name}.part*",
                local_path=str(output_path),
            )
        total = await asyncio.to_thread(
            merge_http_download_chunks,
            output_path,
            parts,
            delete_parts=delete_parts,
        )
        return HttpTransferResult(
            success=True,
            bytes_transferred=total,
            local_path=str(output_path),
            chunk_paths=[str(p) for p in parts],
            merged=True,
            duration_sec=round(time.time() - started, 3),
        )
    except (OSError, ValueError) as exc:
        return HttpTransferResult(
            success=False,
            error=str(exc),
            local_path=str(output_path),
            duration_sec=round(time.time() - started, 3),
        )


async def http_upload_async(
    *,
    url: str,
    local_path: Path,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    field_name: str = "file",
    form_fields: dict[str, str] | None = None,
    content_type: str | None = None,
    timeout: int | None = None,
    max_bytes: int | None = None,
    follow_redirects: bool = True,
    multipart: bool = True,
    stream_callback: Optional[ProgressEmit] = None,
    cancel_event: Any = None,
) -> HttpTransferResult:
    if not _http_enabled():
        return HttpTransferResult(success=False, error="HTTP 工具已禁用")
    started = time.time()
    m = (method or "POST").strip().upper()
    if m not in _HTTP_METHODS:
        return HttpTransferResult(success=False, error=f"不支持的 HTTP 方法: {method}")

    cancel = _make_cancel_check(cancel_event)

    try:
        safe_url = validate_outbound_url(url)
        req_headers = _normalize_headers(headers)
        to = _clamp_timeout(timeout)
        cap = _resolve_transfer_cap(max_bytes, "HTTP_TOOL_MAX_UPLOAD_BYTES")
        local_path = local_path.resolve()
        if not local_path.is_file():
            return HttpTransferResult(success=False, error=f"本地文件不存在: {local_path}")
        file_size = local_path.stat().st_size
        if cap is not None and file_size > cap:
            return HttpTransferResult(success=False, error=f"文件超过上传上限 {cap} 字节")
    except (ValueError, OSError) as exc:
        return HttpTransferResult(success=False, error=str(exc))

    prog = _ProgressState(
        direction="upload",
        url=safe_url,
        total_bytes=file_size,
        current_file=local_path.name,
        emit=stream_callback,
        cancel=cancel,
    )
    await prog.emit_progress(force=True, phase="start")

    timeout_cfg = httpx.Timeout(float(to), connect=min(30.0, float(to)))
    transferred = 0

    class _UploadCancelled(Exception):
        pass

    async def _iter_file():
        nonlocal transferred
        with local_path.open("rb") as fh:
            while True:
                if cancel():
                    raise _UploadCancelled()
                chunk = fh.read(65536)
                if not chunk:
                    break
                transferred += len(chunk)
                prog.transferred_bytes = transferred
                await prog.emit_progress()
                yield chunk

    try:
        async with httpx.AsyncClient(timeout=timeout_cfg, follow_redirects=follow_redirects) as client:
            if multipart:
                guessed, _ = mimetypes.guess_type(local_path.name)
                files = {
                    field_name: (
                        local_path.name,
                        _iter_file(),
                        content_type or guessed or "application/octet-stream",
                    )
                }
                data = form_fields or None
                resp = await client.request(m, safe_url, headers=req_headers or None, data=data, files=files)
            else:
                ct = content_type or mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
                hdrs = dict(req_headers)
                if "Content-Type" not in hdrs:
                    hdrs["Content-Type"] = ct
                resp = await client.request(
                    m,
                    safe_url,
                    headers=hdrs,
                    content=_iter_file(),
                )
        if cancel():
            await prog.emit_progress(force=True, phase="cancelled")
            return HttpTransferResult(
                success=False,
                error="上传已取消",
                interrupted=True,
                status_code=resp.status_code,
                bytes_transferred=transferred,
                local_path=str(local_path),
                url=safe_url,
                duration_sec=round(time.time() - started, 3),
            )
        data = resp.content
        cap_resp = _max_response_bytes()
        truncated = len(data) > cap_resp
        if truncated:
            data = data[:cap_resp]
        ct = resp.headers.get("content-type", "")
        body_text, body_b64, decode_truncated = _decode_body(data, ct, max_chars=cap_resp)
        await prog.emit_progress(force=True, phase="done")
        return HttpTransferResult(
            success=resp.status_code < 400,
            status_code=resp.status_code,
            response_headers=_response_headers_dict(resp),
            body_text=body_text,
            body_base64=body_b64,
            content_type=ct or None,
            bytes_transferred=transferred,
            local_path=str(local_path),
            url=safe_url,
            duration_sec=round(time.time() - started, 3),
            truncated=truncated or decode_truncated,
            error=None if resp.status_code < 400 else f"HTTP {resp.status_code}",
        )
    except _UploadCancelled:
        await prog.emit_progress(force=True, phase="cancelled")
        return HttpTransferResult(
            success=False,
            error="上传已取消",
            interrupted=True,
            bytes_transferred=transferred,
            local_path=str(local_path),
            url=safe_url,
            duration_sec=round(time.time() - started, 3),
        )
    except httpx.HTTPError as exc:
        return HttpTransferResult(
            success=False,
            error=f"HTTP 上传失败: {exc}",
            bytes_transferred=transferred,
            local_path=str(local_path),
            url=safe_url,
            duration_sec=round(time.time() - started, 3),
        )
