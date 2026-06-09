"""vision_region 大图局部裁剪与坐标回填。"""

from __future__ import annotations

import io

from PIL import Image

from services.vision_region import (
    build_tile_regions,
    crop_image_region,
    map_local_annotations_to_original,
    normalize_region_to_pixels,
)


def _sample_png(width: int = 1000, height: int = 800) -> bytes:
    img = Image.new("RGB", (width, height), (40, 90, 150))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_normalize_region_percent_to_pixels():
    region = normalize_region_to_pixels({"x": 10, "y": 20, "width": 30, "height": 40}, 1000, 800, "percent")
    assert {k: region[k] for k in ("x", "y", "width", "height", "left", "top", "right", "bottom", "coordinate_space")} == {
        "x": 100,
        "y": 160,
        "width": 300,
        "height": 320,
        "left": 100,
        "top": 160,
        "right": 400,
        "bottom": 480,
        "coordinate_space": "pixel",
    }


def test_normalize_region_norm1000_to_pixels():
    region = normalize_region_to_pixels({"x": 100, "y": 250, "width": 200, "height": 500}, 1000, 800, "norm1000")
    assert region["x"] == 100
    assert region["y"] == 200
    assert region["width"] == 200
    assert region["height"] == 400


def test_normalize_region_auto_prefers_percent_for_small_values():
    region = normalize_region_to_pixels({"x": 10, "y": 20, "width": 30, "height": 40}, 1000, 800, "auto")
    assert region["x"] == 100
    assert region["y"] == 160
    assert region["input_coordinate_space"] == "percent"


def test_crop_image_region_returns_region_meta():
    raw = _sample_png()
    # 关闭放大时，裁剪输出与区域像素一致
    cropped, meta = crop_image_region(
        raw, {"x": 10, "y": 20, "width": 30, "height": 40}, coordinate_space="percent", magnify_min_side=0,
    )
    img = Image.open(io.BytesIO(cropped))
    assert img.size == (300, 320)
    assert meta["x"] == 100
    assert meta["y"] == 160
    assert meta["width"] == 300
    assert meta["height"] == 320
    assert meta["source_width"] == 1000
    assert meta["source_height"] == 800
    assert meta["magnify"] == 1.0


def test_crop_image_region_magnifies_small_crop():
    raw = _sample_png()
    cropped, meta = crop_image_region(
        raw, {"x": 10, "y": 20, "width": 20, "height": 10}, coordinate_space="percent", magnify_min_side=1024,
    )
    img = Image.open(io.BytesIO(cropped))
    # 区域元信息仍记录原图像素，放大只作用于渲染尺寸
    assert meta["width"] == 200
    assert meta["height"] == 80
    assert meta["magnify"] > 1.0
    assert max(img.size) >= 800
    assert img.size == (meta["rendered_width"], meta["rendered_height"])


def test_magnified_crop_percent_backfill_unaffected():
    # 放大后用 percent 回填，结果应与未放大时完全一致（分辨率无关）
    region_meta = {"x": 100, "y": 160, "width": 200, "height": 80, "magnify": 3.2}
    anns = [{"type": "crosshair", "x": 50, "y": 50}]
    out = map_local_annotations_to_original(anns, region_meta, coordinate_space="percent")
    assert out[0]["x"] == 200
    assert out[0]["y"] == 200


def test_map_local_percent_annotations_to_original():
    region_meta = {"x": 100, "y": 160, "width": 300, "height": 320}
    anns = [
        {"type": "target", "x": 50, "y": 25, "radius": 8},
        {"type": "rect", "x": 10, "y": 10, "width": 20, "height": 30},
        {"type": "callout", "anchor_x": 40, "anchor_y": 50, "label_x": 60, "label_y": 70},
    ]
    out = map_local_annotations_to_original(anns, region_meta, coordinate_space="percent")
    assert out[0]["x"] == 250
    assert out[0]["y"] == 240
    assert out[0]["radius"] == 8
    assert out[1]["x"] == 130
    assert out[1]["y"] == 192
    assert out[1]["width"] == 60
    assert out[1]["height"] == 96
    assert out[2]["anchor_x"] == 220
    assert out[2]["anchor_y"] == 320
    assert out[2]["label_x"] == 280
    assert out[2]["label_y"] == 384


def test_map_local_pixel_annotations_with_reference_dimensions():
    region_meta = {"x": 100, "y": 160, "width": 300, "height": 320}
    anns = [{"type": "target", "x": 150, "y": 80}]
    out = map_local_annotations_to_original(
        anns,
        region_meta,
        coordinate_space="pixel",
        reference_width=600,
        reference_height=640,
    )
    assert out[0]["x"] == 175
    assert out[0]["y"] == 200


def test_build_tile_regions_cover_image_with_overlap():
    tiles = build_tile_regions(1000, 800, rows=2, cols=2, overlap_ratio=0.1)
    assert len(tiles) == 4
    assert tiles[0]["tile_id"] == 1
    assert tiles[0]["x"] == 0
    assert tiles[0]["y"] == 0
    assert tiles[-1]["right"] == 1000
    assert tiles[-1]["bottom"] == 800
    assert tiles[1]["x"] < 500
    assert tiles[2]["y"] < 400
