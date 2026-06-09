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


def test_scale_annotations_vision_to_original():
    from services.image_edit import scale_annotations

    # 原图 2000×1000，内联识图约 1536×768；AI 在识图空间标 (100, 50, 200×100)
    anns = [{"type": "rect", "x": 100, "y": 50, "width": 200, "height": 100}]
    scaled = scale_annotations(anns, 2000 / 1536.0, 1000 / 768.0)
    assert scaled[0]["x"] == int(round(100 * 2000 / 1536))
    assert scaled[0]["y"] == int(round(50 * 1000 / 768))
    assert scaled[0]["width"] == int(round(200 * 2000 / 1536))
    assert scaled[0]["height"] == int(round(100 * 1000 / 768))


def test_scale_annotations_with_offset():
    from services.image_edit import scale_annotations

    anns = [{"type": "rect", "x": 10, "y": 20, "width": 30, "height": 40}]
    scaled = scale_annotations(anns, 2.0, 2.0, offset_x=5, offset_y=10)
    assert scaled[0]["x"] == int(round((10 - 5) * 2))
    assert scaled[0]["y"] == int(round((20 - 10) * 2))


def test_infer_annotation_reference_size():
    from services.image_edit import infer_annotation_reference_size

    anns = [{"type": "rect", "x": 100, "y": 50, "width": 200, "height": 100}]
    # 坐标落在识图范围内、超出识图宽度的原图坐标不应误判
    assert infer_annotation_reference_size(anns, 1920, 1032, 1280, 688) == (1280, 688)
    orig_anns = [{"type": "rect", "x": 1500, "y": 900, "width": 200, "height": 100}]
    assert infer_annotation_reference_size(orig_anns, 1920, 1032, 1280, 688) is None


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


def test_pin_annotation_draws_marker():
    raw = _blank_png()
    out, _ = apply_image_edits(
        raw,
        annotations=[{"type": "pin", "x": 100, "y": 50, "color": "#ff0000", "radius": 10}],
    )
    img = Image.open(__import__("io").BytesIO(out)).convert("RGBA")
    # 中心为白色内点，外圈应为红色
    samples = [img.getpixel((100 + dx, 50 + dy)) for dx, dy in ((8, 0), (0, 8), (-8, 0), (0, -8))]
    assert any(r > 200 and g < 80 for r, g, b, _ in samples)


def test_crosshair_leaves_center_clear():
    raw = _blank_png()
    out, _ = apply_image_edits(
        raw,
        annotations=[{"type": "crosshair", "x": 100, "y": 50, "color": "#ff0000", "arm_length": 14, "gap_radius": 5}],
    )
    img = Image.open(__import__("io").BytesIO(out)).convert("RGBA")
    assert img.getpixel((100, 50))[:3] == (240, 240, 240)
    samples = [img.getpixel((100 + dx, 50 + dy)) for dx, dy in ((12, 0), (-12, 0), (0, 12), (0, -12))]
    assert any(r > 200 and g < 80 for r, g, b, _ in samples)


def test_target_ring_hollow_interior():
    raw = _blank_png()
    out, _ = apply_image_edits(
        raw,
        annotations=[{"type": "target", "x": 100, "y": 50, "color": "#ff0000", "radius": 10, "ring_width": 2}],
    )
    img = Image.open(__import__("io").BytesIO(out)).convert("RGBA")
    assert img.getpixel((100, 50))[:3] == (240, 240, 240)
    r, g, b, _ = img.getpixel((110, 50))
    assert r > 200 and g < 80


def test_callout_draws_leader_and_label():
    raw = _blank_png(260, 120)
    out, _ = apply_image_edits(
        raw,
        annotations=[
            {
                "type": "callout",
                "anchor_x": 40,
                "anchor_y": 60,
                "label_x": 90,
                "label_y": 30,
                "text": "target",
                "color": "#ff0000",
                "anchor_style": "none",
                "line_width": 1,
            }
        ],
    )
    img = Image.open(__import__("io").BytesIO(out)).convert("RGBA")
    assert any(img.getpixel((x, 45))[0] > 200 and img.getpixel((x, 45))[1] < 100 for x in range(55, 80))
    label_samples = [img.getpixel((x, y)) for x in range(90, 130, 3) for y in range(30, 45, 3)]
    assert any(r > 200 and g < 100 for r, g, b, _ in label_samples)


def test_scale_annotations_keypoint_fields():
    from services.image_edit import annotation_max_extent, scale_annotations

    anns = [
        {
            "type": "callout",
            "anchor_x": 10,
            "anchor_y": 20,
            "label_x": 40,
            "label_y": 50,
            "radius": 4,
            "arm_length": 6,
            "gap_radius": 2,
        }
    ]
    scaled = scale_annotations(anns, 2.0, 3.0)
    assert scaled[0]["anchor_x"] == 20
    assert scaled[0]["anchor_y"] == 60
    assert scaled[0]["label_x"] == 80
    assert scaled[0]["label_y"] == 150
    assert scaled[0]["radius"] == 10
    assert scaled[0]["arm_length"] == 15
    max_x, max_y = annotation_max_extent(scaled)
    assert max_x >= 90
    assert max_y >= 160
