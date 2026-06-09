"""image_edit 半透明遮罩与标注。"""

from __future__ import annotations

from PIL import Image

from services.image_edit import apply_image_edits, discover_cjk_font_path


def test_cjk_font_discoverable_on_dev_or_skip():
    path = discover_cjk_font_path(log=False)
    if not path:
        import pytest

        pytest.skip("当前环境未安装中文字体（容器需 fonts-wqy-zenhei）")


def _blank_png(w: int = 200, h: int = 100) -> bytes:
    img = Image.new("RGB", (w, h), (240, 240, 240))
    buf = __import__("io").BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_semi_transparent_overlay():
    raw = _blank_png()
    out, mime = apply_image_edits(
        raw,
        annotations=[
            {
                "type": "overlay",
                "x": 10,
                "y": 10,
                "width": 80,
                "height": 40,
                "fill": "#ff0000",
                "opacity": 0.5,
            }
        ],
    )
    assert mime == "image/png"
    img = Image.open(__import__("io").BytesIO(out)).convert("RGBA")
    # 遮罩区域应被染色（非纯白底）
    r, g, b, a = img.getpixel((50, 30))
    assert r > 200
    assert g < 220


def test_rect_fill_with_opacity_percent():
    raw = _blank_png()
    out, _ = apply_image_edits(
        raw,
        annotations=[
            {
                "type": "rect",
                "x": 0,
                "y": 0,
                "width": 50,
                "height": 50,
                "fill": "#0000ff",
                "opacity": 40,
                "outline": "#000000",
                "line_width": 2,
            }
        ],
    )
    img = Image.open(__import__("io").BytesIO(out)).convert("RGBA")
    _, _, b, _ = img.getpixel((25, 25))
    assert b > 100


def test_chinese_text_annotation_renders():
    path = discover_cjk_font_path(log=False)
    if not path:
        import pytest

        pytest.skip("无中文字体")
    raw = _blank_png(320, 80)
    out, _ = apply_image_edits(
        raw,
        annotations=[{"type": "text", "x": 10, "y": 20, "text": "测试标注", "color": "#ff0000", "size": 24}],
    )
    img = Image.open(__import__("io").BytesIO(out)).convert("RGBA")
    # 有字体时文本区域不应与纯白底完全相同
    samples = [img.getpixel((x, 35)) for x in range(20, 120, 5)]
    assert any(p[0] > 200 and p[1] < 240 for p in samples)
