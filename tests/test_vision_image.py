"""vision_image 模型视图尺寸与标注 reference。"""

from __future__ import annotations

import base64
import io

from PIL import Image

from services.vision_image import (
    compress_image_bytes_for_vision,
    effective_model_view_dimensions,
    encode_image_bytes_for_vision_data_url,
    resize_to_max_side,
)
from services.vision_region import crop_image_region


def test_effective_model_view_low_is_512_max_side():
    mw, mh = effective_model_view_dimensions(1280, 688, "low")
    ew, eh = resize_to_max_side(1280, 688, 512)
    assert (mw, mh) == (ew, eh)
    assert max(mw, mh) <= 512


def test_effective_model_view_high_is_768_max_side():
    mw, mh = effective_model_view_dimensions(1536, 825, "high")
    ew, eh = resize_to_max_side(1536, 825, 768)
    assert (mw, mh) == (ew, eh)
    assert max(mw, mh) <= 768


def test_1920_screenshot_model_view_vs_wrong_encode_ref():
    """1920×1032 截图：若误用 encode 1536 换算而模型视图约 768，scale 差约 2.5×。"""
    _, vh = resize_to_max_side(1920, 1032, 1536)
    mw, mh = effective_model_view_dimensions(1536, vh, "high")
    assert mw < 1536
    assert 1920 / mw > 1920 / 1536 * 1.5


def _sample_png(width: int = 1600, height: int = 900) -> bytes:
    img = Image.new("RGB", (width, height), (30, 80, 140))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_compress_image_bytes_for_vision_respects_b64_cap():
    raw = _sample_png()
    data, mime, size, meta = compress_image_bytes_for_vision(raw, mime="image/png", max_b64_chars=80_000)
    url_chars = len("data:image/jpeg;base64,") + len(base64.b64encode(data).decode("ascii"))
    assert mime == "image/jpeg"
    assert size == len(data)
    assert url_chars <= 80_000
    assert meta["original_width"] == 1600
    assert meta["original_height"] == 900
    assert max(meta["vision_width"], meta["vision_height"]) <= 1536
    assert meta["encoded_bytes"] == size


def test_data_url_encoder_reuses_compressed_bytes_meta():
    raw = _sample_png()
    url, mime, size, meta = encode_image_bytes_for_vision_data_url(raw, mime="image/png", max_b64_chars=80_000)
    assert url.startswith("data:image/jpeg;base64,")
    assert mime == "image/jpeg"
    assert size == meta["encoded_bytes"]
    assert len(url) <= 80_000


def test_cropped_region_can_be_compressed_for_vision():
    raw = _sample_png(2000, 1200)
    cropped, region_meta = crop_image_region(
        raw,
        {"x": 40, "y": 20, "width": 20, "height": 30},
        coordinate_space="percent",
    )
    data, mime, size, meta = compress_image_bytes_for_vision(cropped, mime="image/png", max_b64_chars=80_000)
    assert mime == "image/jpeg"
    assert size == len(data)
    # 小裁剪块会被放大以便模型看清；渲染尺寸记录在 region_meta，区域像素仍为原图坐标
    assert region_meta["magnify"] >= 1.0
    assert meta["original_width"] == region_meta["rendered_width"]
    assert meta["original_height"] == region_meta["rendered_height"]
    assert region_meta["rendered_width"] >= region_meta["width"]
