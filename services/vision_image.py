"""识图：将图片编码为受控体积的 JPEG data URL，适配网关 input length 上限。"""
from __future__ import annotations

import base64
import io
import logging
from typing import Optional

import config

logger = logging.getLogger("edgeops.vision_image")

_DEFAULT_MAX_B64 = 640_000

# (最长边像素, JPEG 质量) — 逐级收紧直至 data URL 低于上限
_VISION_ENCODE_STAGES: tuple[tuple[int, int], ...] = (
    (1536, 85),
    (1280, 80),
    (1024, 75),
    (768, 70),
    (512, 60),
    (384, 50),
    (256, 45),
)


def vision_inline_max_b64_chars() -> int:
    return max(32_000, int(getattr(config, "VISION_INLINE_MAX_B64_CHARS", _DEFAULT_MAX_B64)))


def encode_image_bytes_for_vision_data_url(
    raw: bytes,
    *,
    mime: Optional[str] = None,
    max_b64_chars: Optional[int] = None,
) -> tuple[str, str, int]:
    """将图片转为 JPEG data URL，按阶梯缩小直至低于 max_b64_chars。

    返回 (data_url, mime_used, jpeg_byte_len)。
    Pillow 不可用时退回原始 base64（调用方应走剥离/工具兜底）。
    """
    cap = max_b64_chars if max_b64_chars is not None else vision_inline_max_b64_chars()
    mime_in = (mime or "image/png").strip() or "image/png"

    try:
        from PIL import Image
    except Exception as exc:
        logger.warning("vision_image: Pillow 不可用，原样 base64 err=%s", exc)
        url = f"data:{mime_in};base64," + base64.b64encode(raw).decode("ascii")
        return url, mime_in, len(raw)

    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception as exc:
        logger.warning("vision_image: 无法解码图片 err=%s", exc)
        url = f"data:{mime_in};base64," + base64.b64encode(raw).decode("ascii")
        return url, mime_in, len(raw)

    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    w, h = im.size
    longest_src = max(w, h) or 1

    best_url: str | None = None
    best_jpeg = 0

    for max_side, quality in _VISION_ENCODE_STAGES:
        im_work = im
        if longest_src > max_side:
            scale = max_side / float(longest_src)
            im_work = im.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.LANCZOS,
            )
        buf = io.BytesIO()
        im_work.save(buf, format="JPEG", quality=int(quality), optimize=True)
        data = buf.getvalue()
        b64 = base64.b64encode(data).decode("ascii")
        url = f"data:image/jpeg;base64,{b64}"
        if len(url) <= cap:
            if len(raw) != len(data) or len(url) > len(raw) * 4 // 3 + 64:
                logger.info(
                    "vision_image: 已压缩 raw=%d -> jpeg=%d url_chars=%d side<=%d q=%d",
                    len(raw),
                    len(data),
                    len(url),
                    max_side,
                    quality,
                )
            return url, "image/jpeg", len(data)
        if best_url is None or len(url) < len(best_url):
            best_url = url
            best_jpeg = len(data)

    logger.warning(
        "vision_image: 阶梯压缩后仍超限 cap=%d best_url_chars=%d raw=%d",
        cap,
        len(best_url or ""),
        len(raw),
    )
    return best_url or f"data:image/jpeg;base64,", "image/jpeg", best_jpeg


def reencode_data_url_for_vision(data_url: str, *, max_b64_chars: Optional[int] = None) -> str | None:
    """将已有 data URL 重新编码；非 data URL 或解码失败返回 None。"""
    url = (data_url or "").strip()
    if not url.startswith("data:") or ";base64," not in url:
        return None
    header, b64 = url.split(",", 1)
    mime = "image/png"
    if header.startswith("data:"):
        mime = header[5:].split(";", 1)[0].strip() or mime
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    new_url, _, _ = encode_image_bytes_for_vision_data_url(raw, mime=mime, max_b64_chars=max_b64_chars)
    return new_url
