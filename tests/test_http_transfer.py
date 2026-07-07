"""HTTP 出站工具单元测试。"""
from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.http_transfer import (
    http_download_async,
    http_download_merge_async,
    http_request_async,
    http_upload_async,
    merge_http_download_chunks,
    validate_outbound_url,
)


def test_validate_outbound_url_rejects_localhost(monkeypatch):
    monkeypatch.setattr(
        "services.http_transfer._resolve_host_ips",
        lambda hostname: ["127.0.0.1"],
    )
    with pytest.raises(ValueError, match="禁止访问"):
        validate_outbound_url("https://localhost/api")


def test_validate_outbound_url_rejects_http_by_default(monkeypatch):
    monkeypatch.setattr("services.http_transfer._allow_insecure", lambda: False)
    with pytest.raises(ValueError, match="HTTP 明文"):
        validate_outbound_url("http://example.com/")


def test_validate_outbound_url_allows_public_https(monkeypatch):
    monkeypatch.setattr(
        "services.http_transfer._resolve_host_ips",
        lambda hostname: ["93.184.216.34"],
    )
    url = validate_outbound_url("https://example.com/path?q=1")
    assert url.startswith("https://example.com/")


def test_resolve_transfer_cap_unlimited(monkeypatch):
    from services.http_transfer import _resolve_transfer_cap

    monkeypatch.setattr("services.http_transfer.config.HTTP_TOOL_MAX_DOWNLOAD_BYTES", 0)
    assert _resolve_transfer_cap(None, "HTTP_TOOL_MAX_DOWNLOAD_BYTES") is None
    assert _resolve_transfer_cap(0, "HTTP_TOOL_MAX_DOWNLOAD_BYTES") is None


def test_merge_http_download_chunks(tmp_path):
    out = tmp_path / "final.bin"
    p0 = tmp_path / "final.bin.part000000"
    p1 = tmp_path / "final.bin.part000001"
    p0.write_bytes(b"abc")
    p1.write_bytes(b"def")
    total = merge_http_download_chunks(out, [p0, p1], delete_parts=True)
    assert total == 6
    assert out.read_bytes() == b"abcdef"
    assert not p0.exists()
    assert not p1.exists()


@pytest.mark.asyncio
async def test_http_request_async_success(monkeypatch):
    monkeypatch.setattr(
        "services.http_transfer.validate_outbound_url",
        lambda url, **kw: "https://api.example.com/v1",
    )

    class FakeResp:
        status_code = 200
        content = b'{"ok":true}'
        headers = httpx.Headers({"content-type": "application/json"})
        reason_phrase = "OK"

    async def fake_request(method, url, **kwargs):
        return FakeResp()

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client.request = fake_request
        client_cls.return_value = client

        result = await http_request_async(method="GET", url="https://api.example.com/v1")
        assert result.success is True
        assert result.status_code == 200
        assert result.body_text == '{"ok":true}'


@pytest.mark.asyncio
async def test_http_download_async_cancel(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "services.http_transfer.validate_outbound_url",
        lambda url, **kw: "https://cdn.example.com/file.bin",
    )
    cancel = threading.Event()
    cancel.set()
    dest = tmp_path / "file.bin"

    class FakeStreamResp:
        status_code = 200
        headers = httpx.Headers({"content-length": "1000"})
        reason_phrase = "OK"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_bytes(self, chunk_size=65536):
            yield b"abc"
            if False:
                yield b""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def stream(self, method, url, **kwargs):
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=FakeStreamResp())
            ctx.__aexit__ = AsyncMock(return_value=None)
            return ctx

    with patch("httpx.AsyncClient", FakeClient):
        result = await http_download_async(
            url="https://cdn.example.com/file.bin",
            local_path=dest,
            cancel_event=cancel,
        )
    assert result.success is False
    assert result.interrupted is True


@pytest.mark.asyncio
async def test_http_download_merge_async(tmp_path):
    out = tmp_path / "pkg.iso"
    p0 = tmp_path / "pkg.iso.part000000"
    p1 = tmp_path / "pkg.iso.part000001"
    p0.write_bytes(b"12")
    p1.write_bytes(b"34")
    result = await http_download_merge_async(output_path=out, delete_parts=True)
    assert result.success is True
    assert result.merged is True
    assert out.read_bytes() == b"1234"


@pytest.mark.asyncio
async def test_http_upload_async_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "services.http_transfer.validate_outbound_url",
        lambda url, **kw: "https://upload.example.com/",
    )
    missing = tmp_path / "nope.bin"
    result = await http_upload_async(
        url="https://upload.example.com/",
        local_path=missing,
    )
    assert result.success is False
    assert "不存在" in (result.error or "")
