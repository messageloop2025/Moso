"""MCP 工具结果：自动拉取临时外链（OSS 签名 URL 等）为 毛竹 聊天附件。"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

import config
from api.chat_attachments import save_bytes_as_chat_attachment

logger = logging.getLogger("edgeops.mcp_result_fetch")

_URL_TRAIL_CHARS = ".,);]）」、。，；：:!?\"'"
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
_IMAGE_PATH_RE = re.compile(r"\.(png|jpe?g|gif|webp|bmp|tif|tiff|svg)(\?|$)", re.IGNORECASE)
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)
_SIGNED_OSS_HOST_RE = re.compile(
    r"(oss[-.]cn-|dashscope-result|aliyuncs\.com|cloudfront\.net|amazonaws\.com)",
    re.IGNORECASE,
)
_URL_KEY_NAMES = frozenset({
    "url", "image_url", "imageurl", "image", "href", "download_url", "file_url",
    "output_url", "result_url", "signed_url", "resource_url",
})
_BASE64_IMAGE_KEYS = frozenset({
    "b64_json", "base64", "image_base64", "data", "image_data", "content",
})


def _strip_url_trail(url: str) -> str:
    return (url or "").strip().rstrip(_URL_TRAIL_CHARS)


def _detect_image_bytes(data: bytes) -> tuple[str, str] | None:
    if not data or len(data) < 12:
        return None
    for magic, mime, ext in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime, ext
    if data[:4] == b"RIFF" and len(data) > 12 and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def _try_decode_base64_image(value: str) -> tuple[bytes, str] | None:
    s = (value or "").strip()
    if not s or len(s) < 32:
        return None
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    s = re.sub(r"\s+", "", s)
    try:
        raw = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        return None
    detected = _detect_image_bytes(raw)
    if detected:
        return raw, detected[0]
    return None


def _extract_image_bytes_from_json_obj(obj: Any) -> tuple[bytes, str] | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            if isinstance(v, str):
                if kl in _BASE64_IMAGE_KEYS or kl.endswith("_base64") or kl.endswith("_b64"):
                    got = _try_decode_base64_image(v)
                    if got:
                        return got
                if kl in _URL_KEY_NAMES and _looks_like_fetchable_image_url(v):
                    return None  # URL handled by download path
            else:
                got = _extract_image_bytes_from_json_obj(v)
                if got:
                    return got
    elif isinstance(obj, list):
        for item in obj:
            got = _extract_image_bytes_from_json_obj(item)
            if got:
                return got
    return None


def _looks_like_fetchable_image_url(url: str) -> bool:
    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return False
    if _IMAGE_PATH_RE.search(u):
        return True
    if _SIGNED_OSS_HOST_RE.search(u):
        return True
    return False


def _collect_urls_from_obj(obj: Any, out: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and k.lower() in _URL_KEY_NAMES and _looks_like_fetchable_image_url(v):
                out.append(v.strip())
            else:
                _collect_urls_from_obj(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_urls_from_obj(item, out)
    elif isinstance(obj, str):
        for m in _URL_IN_TEXT_RE.finditer(obj):
            u = _strip_url_trail(m.group(0))
            if _looks_like_fetchable_image_url(u):
                out.append(u)


def extract_fetchable_urls(payload: dict[str, Any]) -> list[str]:
    """从 MCP 工具 JSON 结果中提取应拉取的外链（去重、保序）。"""
    found: list[str] = []
    _collect_urls_from_obj(payload, found)
    for key in ("content", "structured"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            try:
                parsed = json.loads(val.lstrip("\ufeff"))
                _collect_urls_from_obj(parsed, found)
            except json.JSONDecodeError:
                for m in _URL_IN_TEXT_RE.finditer(val):
                    u = _strip_url_trail(m.group(0))
                    if _looks_like_fetchable_image_url(u):
                        found.append(u)
        elif isinstance(val, (dict, list)):
            _collect_urls_from_obj(val, found)
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _guess_filename(url: str, content_type: str) -> str:
    try:
        path_part = urlparse(url).path or ""
        name = Path(path_part).name.split("?")[0]
        if name and "." in name:
            return name[:120]
    except Exception:
        pass
    ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ".png"
    return f"mcp-fetch{ext}"


def _inject_local_urls(obj: Any, mapping: dict[str, str]) -> None:
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str) and k.lower() in _URL_KEY_NAMES and v in mapping:
                obj["source_url"] = v
                obj[k] = mapping[v]
            else:
                _inject_local_urls(v, mapping)
    elif isinstance(obj, list):
        for item in obj:
            _inject_local_urls(item, mapping)


def _rewrite_payload_urls(payload: dict[str, Any], mapping: dict[str, str]) -> None:
    if not mapping:
        return
    for key in ("structured",):
        val = payload.get(key)
        if val is not None:
            _inject_local_urls(val, mapping)
    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        try:
            parsed = json.loads(content.lstrip("\ufeff"))
            _inject_local_urls(parsed, mapping)
            payload["content"] = json.dumps(parsed, ensure_ascii=False)
        except json.JSONDecodeError:
            out = content
            for remote, local in mapping.items():
                out = out.replace(remote, local)
            payload["content"] = out


async def _download_url(url: str) -> tuple[bytes, str]:
    timeout = httpx.Timeout(connect=20.0, read=120.0, write=20.0, pool=20.0)
    headers = {"User-Agent": "Moso-MCP-Fetch/1.0", "Accept": "image/*,application/octet-stream,*/*"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.content
        max_bytes = int(getattr(config, "MCP_REMOTE_FETCH_MAX_BYTES", 20 * 1024 * 1024))
        if len(data) > max_bytes:
            raise ValueError(f"远程文件过大（>{max_bytes // (1024 * 1024)} MB）")
        detected = _detect_image_bytes(data)
        if detected:
            return data, detected[0]
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype in ("application/json", "text/json", "text/plain"):
            try:
                text = data.decode("utf-8-sig")
                parsed = json.loads(text)
                nested = _extract_image_bytes_from_json_obj(parsed)
                if nested:
                    return nested
                for u in extract_fetchable_urls({"structured": parsed}):
                    if u != url:
                        return await _download_url(u)
            except UnicodeDecodeError as exc:
                raise ValueError(f"响应正文不是有效 UTF-8: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise ValueError(f"响应 JSON 无法解析: {exc}") from exc
        if ctype and not ctype.startswith("image/") and ctype not in (
            "application/octet-stream",
            "binary/octet-stream",
        ):
            if _looks_like_fetchable_image_url(url):
                raise ValueError(f"下载内容不是图片（Content-Type: {ctype or 'unknown'}）")
            raise ValueError(f"非图片内容类型: {ctype}")
        return data, ctype or "application/octet-stream"


async def enrich_mcp_tool_payload(
    payload: dict[str, Any],
    user: dict,
    *,
    session_id: int | None = None,
) -> dict[str, Any]:
    """拉取 MCP 结果中的临时图片 URL，写入聊天附件并回填 local_url。"""
    if not getattr(config, "MCP_REMOTE_FETCH_ENABLED", True):
        return payload
    if not payload.get("success"):
        return payload

    # 工具直接返回 base64 图片时，先落盘为附件（避免再走 HTTP 下载）
    inline_b64 = _extract_image_bytes_from_json_obj(payload)
    if inline_b64:
        try:
            data, ctype = inline_b64
            attachment = await save_bytes_as_chat_attachment(
                user,
                data,
                original_name="mcp-generated.png",
                mime=ctype,
                session_id=session_id,
            )
            local_url = attachment.get("url") or f"/api/ai/attachments/{attachment.get('uuid')}"
            payload["fetched_assets"] = [{
                "success": True,
                "uuid": attachment.get("uuid"),
                "local_url": local_url,
                "markdown_image": f"![{attachment.get('name') or 'image'}]({local_url})",
                "source": "inline_base64",
            }]
            payload["display_hint"] = (
                "图片已保存为聊天附件；向用户展示请用 markdown_image 或 fetched_assets[0].local_url。"
            )
            return payload
        except Exception as exc:
            logger.warning("inline base64 image save failed user=%s: %s", user.get("id"), exc)
            payload["inline_image_save_error"] = str(exc)[:500]

    urls = extract_fetchable_urls(payload)
    if not urls:
        return payload

    max_urls = max(1, int(getattr(config, "MCP_REMOTE_FETCH_MAX_URLS", 5)))
    urls = urls[:max_urls]

    fetched: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}

    for url in urls:
        item: dict[str, Any] = {"source_url": url}
        try:
            data, ctype = await _download_url(url)
            attachment = await save_bytes_as_chat_attachment(
                user,
                data,
                original_name=_guess_filename(url, ctype),
                mime=ctype,
                session_id=session_id,
                source_url=url,
            )
            local_url = attachment.get("url") or f"/api/ai/attachments/{attachment.get('uuid')}"
            item.update(
                {
                    "success": True,
                    "uuid": attachment.get("uuid"),
                    "local_url": local_url,
                    "kind": attachment.get("kind"),
                    "mime": attachment.get("mime"),
                    "size": attachment.get("size"),
                    "markdown_image": f"![{attachment.get('name') or 'image'}]({local_url})",
                }
            )
            mapping[url] = local_url
        except Exception as e:
            logger.warning("MCP fetch url failed user=%s url=%s: %s", user.get("id"), url[:120], e)
            item["success"] = False
            err = str(e)
            if "UTF-8" in err or "codec" in err.lower() or "decode" in err.lower():
                item["error"] = "图片下载后正文编码无法识别（非 UTF-8 或损坏），请直接向用户展示 source_url 链接"
            else:
                item["error"] = err[:500]
        fetched.append(item)

    if fetched:
        payload["fetched_assets"] = fetched
        _rewrite_payload_urls(payload, mapping)
        ok_n = sum(1 for x in fetched if x.get("success"))
        if mapping:
            payload["display_hint"] = (
                "临时外链已拉取为毛竹（Moso）聊天附件；向用户展示图片请用 fetched_assets[].local_url "
                "或 markdown_image（/api/ai/attachments/…），勿直接贴 source_url（OSS 签名 URL 在浏览器中易失效）。"
            )
        elif ok_n == 0:
            payload["display_hint"] = (
                "自动下载图片均未成功（见 fetched_assets[].error）。请把工具返回的 HTTPS 图片链接直接给用户，"
                "或说明需用户在本机浏览器打开；勿谎称已内嵌展示。"
            )
    return payload


_MD_IMAGE_URL_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s\"']+)\)", re.IGNORECASE)
_HTML_IMG_SRC_RE = re.compile(
    r"""<img\b[^>]*\bsrc\s*=\s*["'](https?://[^"']+)["']""",
    re.IGNORECASE,
)


async def _persist_remote_image_url(
    url: str,
    user: dict,
    *,
    session_id: int | None,
    alt: str = "",
) -> str | None:
    u = (url or "").strip()
    if not _looks_like_fetchable_image_url(u):
        return None
    try:
        data, ctype = await _download_url(u)
        attachment = await save_bytes_as_chat_attachment(
            user,
            data,
            original_name=_guess_filename(u, ctype) or (alt or "image"),
            mime=ctype,
            session_id=session_id,
            source_url=u,
        )
        return attachment.get("url") or f"/api/ai/attachments/{attachment.get('uuid')}"
    except Exception as e:
        logger.warning(
            "rewrite markdown image failed user=%s url=%s: %s",
            user.get("id"),
            u[:120],
            e,
        )
        return None


async def rewrite_markdown_remote_images_in_text(
    text: str,
    user: dict,
    *,
    session_id: int | None = None,
) -> str:
    """把 assistant 正文里的临时外链图片改为 /api/ai/attachments/<uuid>。"""
    if not text or not getattr(config, "MCP_REMOTE_FETCH_ENABLED", True):
        return text
    out = text
    seen: set[str] = set()

    for m in list(_MD_IMAGE_URL_RE.finditer(out)):
        remote = m.group(2).strip()
        if remote in seen:
            continue
        seen.add(remote)
        local = await _persist_remote_image_url(
            remote, user, session_id=session_id, alt=m.group(1) or ""
        )
        if local:
            out = out.replace(remote, local)

    for m in list(_HTML_IMG_SRC_RE.finditer(out)):
        remote = m.group(1).strip()
        if remote in seen:
            continue
        seen.add(remote)
        local = await _persist_remote_image_url(remote, user, session_id=session_id)
        if local:
            out = out.replace(remote, local)

    return out


async def enrich_tool_result_json_string(
    tool_result: str,
    user: dict,
    *,
    session_id: int | None = None,
) -> str:
    """对任意工具 JSON 结果做外链图片拉取（与 MCP 工具共用逻辑）。"""
    raw = tool_result or ""
    if not raw.strip():
        return raw
    try:
        obj = json.loads(raw.lstrip("\ufeff"))
    except json.JSONDecodeError:
        return raw
    if not isinstance(obj, dict):
        return raw
    enriched = await enrich_mcp_tool_payload(obj, user, session_id=session_id)
    return json.dumps(enriched, ensure_ascii=False)
