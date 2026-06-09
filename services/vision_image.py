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


def resize_to_max_side(width: int, height: int, max_side: int) -> tuple[int, int]:
    w, h = int(width or 1), int(height or 1)
    longest = max(w, h) or 1
    if longest <= max_side:
        return w, h
    scale = max_side / float(longest)
    return max(1, int(w * scale)), max(1, int(h * scale))


def estimate_vision_display_dimensions(original_width: int, original_height: int) -> tuple[int, int]:
    """估算内联识图时模型所见分辨率（与 encode 首档缩放策略一致）。"""
    max_side = _VISION_ENCODE_STAGES[0][0]
    return resize_to_max_side(original_width, original_height, max_side)


def build_dimension_meta(original_width: int, original_height: int, vision_width: int, vision_height: int) -> dict:
    ow = max(1, int(original_width or 1))
    oh = max(1, int(original_height or 1))
    vw = max(1, int(vision_width or ow))
    vh = max(1, int(vision_height or oh))
    return {
        "original_width": ow,
        "original_height": oh,
        "width": ow,
        "height": oh,
        "vision_width": vw,
        "vision_height": vh,
        "vision_scale_x": round(vw / ow, 6),
        "vision_scale_y": round(vh / oh, 6),
    }


def image_dimension_info(raw: bytes, *, mime: Optional[str] = None) -> dict:
    """读取原图尺寸，并估算内联识图尺寸（供附件清单 / read_chat_attachment 元信息）。"""
    try:
        from PIL import Image
    except Exception:
        return {}
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        ow, oh = im.size
    except Exception:
        return {}
    vw, vh = estimate_vision_display_dimensions(ow, oh)
    return build_dimension_meta(ow, oh, vw, vh)


def inline_vision_dimension_info(raw: bytes, *, mime: Optional[str] = None) -> dict:
    """与内联识图一致的实际尺寸 meta（含 model_view，供标注坐标换算）。"""
    _, _, _, meta = build_inline_vision_meta(raw, mime=mime)
    return meta or {}


def resolve_vision_detail(url_chars: int, cap: int | None = None) -> str:
    """内联 image_url 的 detail 档位。标注场景需空间精度，统一 high。"""
    return "high"


def effective_model_view_dimensions(vision_w: int, vision_h: int, detail: str) -> tuple[int, int]:
    """模型实际「看到」的像素尺寸（在 encode 之后、provider detail 处理之后）。"""
    w, h = int(vision_w or 1), int(vision_h or 1)
    d = (detail or "auto").strip().lower()
    if d == "low":
        return resize_to_max_side(w, h, 512)
    # high / auto：OpenAI 兼容实现会先缩放到最长边约 768
    return resize_to_max_side(w, h, 768)


def build_inline_vision_meta(
    raw: bytes,
    *,
    mime: Optional[str] = None,
    max_b64_chars: Optional[int] = None,
) -> tuple[str, str, int, dict]:
    """encode 内联 JPEG 并补齐 original / vision / model_view 尺寸与 detail。"""
    url, mime_out, jpeg_len, dim_meta = encode_image_bytes_for_vision_data_url(
        raw, mime=mime, max_b64_chars=max_b64_chars
    )
    cap = max_b64_chars if max_b64_chars is not None else vision_inline_max_b64_chars()
    detail = resolve_vision_detail(len(url), cap)
    vw = int(dim_meta.get("vision_width") or dim_meta.get("original_width") or 1)
    vh = int(dim_meta.get("vision_height") or dim_meta.get("original_height") or 1)
    mw, mh = effective_model_view_dimensions(vw, vh, detail)
    dim_meta["vision_detail"] = detail
    dim_meta["model_view_width"] = mw
    dim_meta["model_view_height"] = mh
    return url, mime_out, jpeg_len, dim_meta


def encode_image_bytes_for_vision_data_url(
    raw: bytes,
    *,
    mime: Optional[str] = None,
    max_b64_chars: Optional[int] = None,
) -> tuple[str, str, int, dict]:
    """将图片转为 JPEG data URL，按阶梯缩小直至低于 max_b64_chars。

    返回 (data_url, mime_used, jpeg_byte_len, dimension_meta)。
    dimension_meta 含 original_width/height 与 vision_width/height（模型所见像素）。
    """
    cap = max_b64_chars if max_b64_chars is not None else vision_inline_max_b64_chars()
    mime_in = (mime or "image/png").strip() or "image/png"
    empty_meta: dict = {}

    try:
        from PIL import Image
    except Exception as exc:
        logger.warning("vision_image: Pillow 不可用，原样 base64 err=%s", exc)
        url = f"data:{mime_in};base64," + base64.b64encode(raw).decode("ascii")
        return url, mime_in, len(raw), empty_meta

    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception as exc:
        logger.warning("vision_image: 无法解码图片 err=%s", exc)
        url = f"data:{mime_in};base64," + base64.b64encode(raw).decode("ascii")
        return url, mime_in, len(raw), empty_meta

    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    w, h = im.size
    longest_src = max(w, h) or 1
    dim_meta = build_dimension_meta(w, h, w, h)

    best_url: str | None = None
    best_jpeg = 0
    best_meta = dim_meta

    for max_side, quality in _VISION_ENCODE_STAGES:
        im_work = im
        vw, vh = w, h
        if longest_src > max_side:
            vw, vh = resize_to_max_side(w, h, max_side)
            im_work = im.resize((vw, vh), Image.LANCZOS)
        buf = io.BytesIO()
        im_work.save(buf, format="JPEG", quality=int(quality), optimize=True)
        data = buf.getvalue()
        b64 = base64.b64encode(data).decode("ascii")
        url = f"data:image/jpeg;base64,{b64}"
        stage_meta = build_dimension_meta(w, h, vw, vh)
        if len(url) <= cap:
            if len(raw) != len(data) or len(url) > len(raw) * 4 // 3 + 64:
                logger.info(
                    "vision_image: 已压缩 raw=%d -> jpeg=%d url_chars=%d side<=%d q=%d vision=%dx%d orig=%dx%d",
                    len(raw),
                    len(data),
                    len(url),
                    max_side,
                    quality,
                    vw,
                    vh,
                    w,
                    h,
                )
            return url, "image/jpeg", len(data), stage_meta
        if best_url is None or len(url) < len(best_url):
            best_url = url
            best_jpeg = len(data)
            best_meta = stage_meta

    logger.warning(
        "vision_image: 阶梯压缩后仍超限 cap=%d best_url_chars=%d raw=%d",
        cap,
        len(best_url or ""),
        len(raw),
    )
    return best_url or "data:image/jpeg;base64,", "image/jpeg", best_jpeg, best_meta


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
    new_url, _, _, _ = encode_image_bytes_for_vision_data_url(raw, mime=mime, max_b64_chars=max_b64_chars)
    return new_url
