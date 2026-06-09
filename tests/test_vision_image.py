"""vision_image 模型视图尺寸与标注 reference。"""

from __future__ import annotations

from services.vision_image import effective_model_view_dimensions, resize_to_max_side


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
