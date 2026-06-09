"""image_calibration 坐标拟合与自动推断。"""

from __future__ import annotations

from services.image_calibration import (
    apply_calibration_transform_to_annotations,
    apply_normalized_space,
    assess_transform_quality,
    build_calibration_plan,
    build_cell_grid_plan,
    build_percent_grid_annotations,
    cells_to_bbox,
    clamp_tight_box_sizes,
    compute_calibration_transform,
    estimate_auto_global_transform,
    fit_scale_offset,
    normalized_space_divisor,
    parse_global_transform,
    pick_best_auto_transform,
    resolve_annotation_transform,
)


def test_parse_global_transform_variants():
    assert parse_global_transform(None) is None
    assert parse_global_transform({}) is None
    assert parse_global_transform({"scale": 1, "offset_x": 0, "offset_y": 0}) is None
    assert parse_global_transform({"scale": 0.9}) == (0.9, 0.9, 0.0, 0.0)
    assert parse_global_transform({"offset_x": 8}) == (1.0, 1.0, 8.0, 0.0)
    assert parse_global_transform({"scale_x": 1.1, "scale_y": 0.8, "offset_x": -5, "offset_y": 3}) == (1.1, 0.8, -5.0, 3.0)


def test_estimate_auto_global_shift_left_cluster():
    # 模拟 AI 给的 percent 框整体偏左（应在内容区），auto 应给出正 offset_x
    anns = [
        {"type": "rect", "x": 2, "y": 30, "width": 8, "height": 10},
        {"type": "rect", "x": 2, "y": 55, "width": 8, "height": 10},
        {"type": "rect", "x": 2, "y": 80, "width": 8, "height": 10},
    ]
    res = estimate_auto_global_transform(anns, 1024, 547, space="percent")
    assert res is not None
    sx, sy, ox, oy, sp, _ = res
    assert ox > 2.0
    assert sp == "percent"


def test_percent_content_mapping():
    anns = [{"type": "rect", "x": 0, "y": 0, "width": 50, "height": 50}]
    cb = {"x": 100, "y": 50, "width": 800, "height": 400}
    out, _, _ = apply_normalized_space(anns, 1024, 547, "percent_content", cb)
    assert out[0]["x"] == 100
    assert out[0]["width"] == 400


def test_global_transform_then_percent_pipeline():
    # AI 给相对布局（percent），整组右移 10% + 放大 1.1，全局微调后再换算到像素
    anns = [
        {"type": "rect", "x": 10, "y": 20, "width": 5, "height": 5},
        {"type": "rect", "x": 50, "y": 20, "width": 5, "height": 5},
    ]
    gsx, gsy, gox, goy = parse_global_transform({"scale": 1.1, "offset_x": 10})
    adjusted = apply_calibration_transform_to_annotations(anns, gsx, gsy, gox, goy)
    assert adjusted[0]["x"] == 21
    assert adjusted[1]["x"] == 65
    out, _, meta = resolve_annotation_transform(adjusted, 1000, 800, {}, [], None, coordinate_space="percent")
    assert meta["method"] == "explicit_space"
    assert out[0]["x"] == 210


def test_percent_space_deterministic():
    anns = [{"type": "rect", "x": 25, "y": 50, "width": 10, "height": 20}]
    out, sx, sy = apply_normalized_space(anns, 2000, 1000, "percent")
    assert out[0]["x"] == 500
    assert out[0]["y"] == 500
    assert out[0]["width"] == 200
    assert out[0]["height"] == 200


def test_percent_space_converts_keypoint_fields():
    anns = [
        {
            "type": "callout",
            "anchor_x": 25,
            "anchor_y": 50,
            "label_x": 30,
            "label_y": 40,
            "x2": 75,
            "y2": 80,
            "radius": 8,
        }
    ]
    out, _, _ = apply_normalized_space(anns, 2000, 1000, "percent")
    assert out[0]["anchor_x"] == 500
    assert out[0]["anchor_y"] == 500
    assert out[0]["label_x"] == 600
    assert out[0]["label_y"] == 400
    assert out[0]["x2"] == 1500
    assert out[0]["y2"] == 800
    # 视觉尺寸仍按像素控制标记大小，不随 percent 坐标放大成巨型标记。
    assert out[0]["radius"] == 8


def test_percent_content_converts_keypoint_fields():
    anns = [{"type": "callout", "anchor_x": 50, "anchor_y": 25, "label_x": 60, "label_y": 35}]
    cb = {"x": 100, "y": 50, "width": 800, "height": 400}
    out, _, _ = apply_normalized_space(anns, 1024, 547, "percent_content", cb)
    assert out[0]["anchor_x"] == 500
    assert out[0]["anchor_y"] == 150
    assert out[0]["label_x"] == 580
    assert out[0]["label_y"] == 190


def test_global_transform_adjusts_keypoint_fields_before_percent_mapping():
    anns = [{"type": "callout", "anchor_x": 10, "anchor_y": 20, "label_x": 15, "label_y": 25}]
    gsx, gsy, gox, goy = parse_global_transform({"scale": 1.1, "offset_x": 10, "offset_y": -2})
    adjusted = apply_calibration_transform_to_annotations(anns, gsx, gsy, gox, goy)
    assert adjusted[0]["anchor_x"] == 21
    assert adjusted[0]["anchor_y"] == 20
    assert adjusted[0]["label_x"] == 26
    assert adjusted[0]["label_y"] == 26


def test_normalized_space_divisor_variants():
    assert normalized_space_divisor("percent") == 100.0
    assert normalized_space_divisor("norm") == 1.0
    assert normalized_space_divisor("norm1000") == 1000.0
    assert normalized_space_divisor("pixel") is None


def test_resolve_explicit_percent_beats_auto():
    anns = [{"type": "rect", "x": 30, "y": 40, "width": 10, "height": 10}]
    out, note, meta = resolve_annotation_transform(
        anns, 1920, 1032, {}, [], None, coordinate_space="percent"
    )
    assert meta and meta.get("method") == "explicit_space"
    assert out[0]["x"] == round(0.30 * 1920)


def test_percent_grid_has_lines_and_labels():
    grid = build_percent_grid_annotations(1000, 800)
    assert any(a["type"] == "line" for a in grid)
    assert any(a["type"] == "text" and a["text"] == "50" for a in grid)


def test_cell_grid_plan_numbering_and_count():
    draw, meta = build_cell_grid_plan(1200, 800, 12, 8)
    assert meta["cols"] == 12 and meta["rows"] == 8 and meta["count"] == 96
    # 每格一个编号文字
    texts = [a for a in draw if a.get("type") == "text"]
    assert len(texts) == 96
    assert any(a["text"] == "1" for a in texts)
    assert any(a["text"] == "96" for a in texts)


def test_cells_to_bbox_merges_adjacent_cells():
    _, meta = build_cell_grid_plan(1200, 800, 12, 8)  # cell 100x100
    # 编号 1 = (col0,row0), 2=(col1,row0), 13=(col0,row1), 14=(col1,row1)
    box = cells_to_bbox([1, 2, 13, 14], meta)
    assert box == {"x": 0, "y": 0, "width": 200, "height": 200}


def test_cells_to_bbox_single_cell_center():
    _, meta = build_cell_grid_plan(1200, 800, 12, 8)
    # 编号 14 = col1,row1 → x=100,y=100,100x100
    box = cells_to_bbox([14], meta)
    assert box == {"x": 100, "y": 100, "width": 100, "height": 100}


def test_cells_to_bbox_ignores_out_of_range():
    _, meta = build_cell_grid_plan(1200, 800, 12, 8)
    assert cells_to_bbox([9999], meta) is None
    assert cells_to_bbox([], meta) is None


def test_fit_scale_offset_linear():
    known = [10.0, 35.0, 60.0]
    obs = [0.0, 10.0, 20.0]
    sx, ox = fit_scale_offset(known, obs)
    assert abs(sx - 2.5) < 0.001
    assert abs(ox - 10.0) < 0.001


def test_compute_calibration_transform_corners():
    refs, _ = build_calibration_plan(1920, 1032)
    pick = [r for r in refs if r["id"] in ("cal-1", "cal-3", "cal-c")]
    obs = [{"id": r["id"], "x": r["x"] / 1.25, "y": r["y"] / 1.25} for r in pick]
    result = compute_calibration_transform(refs, obs)
    assert result is not None
    sx, sy, ox, oy, n = result
    assert n >= 2
    assert abs(sx - 1.25) < 0.08


def test_pick_best_auto_norm1000():
    anns = [
        {"type": "rect", "x": 80, "y": 50, "width": 40, "height": 50},
        {"type": "rect", "x": 450, "y": 300, "width": 40, "height": 50},
        {"type": "rect", "x": 850, "y": 600, "width": 40, "height": 50},
    ]
    dim = {"vision_width": 1536, "vision_height": 825, "model_view_width": 768, "model_view_height": 412}
    best = pick_best_auto_transform(anns, 1920, 1032, dim)
    assert best is not None
    name, sx, sy, ox, oy, rw, rh = best
    assert name == "norm1000"
    out = apply_calibration_transform_to_annotations(anns, sx, sy, ox, oy)
    assert out[0]["x"] > 100


def test_resolve_prefers_calibration():
    refs, _ = build_calibration_plan(1000, 800)
    anns = [{"type": "rect", "x": 100, "y": 100, "width": 50, "height": 50}]
    cal_obs = [
        {"id": "cal-1", "x": 50, "y": 50},
        {"id": "cal-3", "x": 50, "y": 450},
    ]
    out, note, meta = resolve_annotation_transform(
        anns, 1000, 800, {}, refs, cal_obs, use_original=False
    )
    assert meta and meta.get("method") == "calibration"
    assert "校准" in note


def test_auto_offset_search_reduces_margin_cluster():
    # 模拟 AI 在 0-1000 空间标注，但内容区相当于 x 偏移约 200px
    anns = [
        {"type": "rect", "x": 120, "y": 200, "width": 40, "height": 40},
        {"type": "rect", "x": 480, "y": 200, "width": 40, "height": 40},
        {"type": "rect", "x": 840, "y": 200, "width": 40, "height": 40},
    ]
    dim = {"vision_width": 1536, "vision_height": 825, "model_view_width": 768, "model_view_height": 412}
    best = pick_best_auto_transform(anns, 1920, 1032, dim)
    assert best is not None
    name, sx, sy, ox, oy, _, _ = best
    q = assess_transform_quality(anns, 1920, 1032, sx, sy, ox, oy)
    assert name == "norm1000"
    assert q["margin_left_frac"] + q["margin_right_frac"] < 0.5


def test_clamp_tight_box_sizes_percent():
    anns = [
        {"type": "highlight", "x": 2, "y": 40, "width": 25, "height": 18, "fill": "#ff0000"},
        {"type": "pin", "x": 10, "y": 50},
        {"type": "crosshair", "x": 20, "y": 50, "width": 50, "height": 30},
        {"type": "target", "x": 30, "y": 50, "width": 50, "height": 30},
        {"type": "callout", "anchor_x": 30, "anchor_y": 50, "label_x": 40, "label_y": 40, "width": 50, "height": 30},
    ]
    out, notes = clamp_tight_box_sizes(anns, space="percent", max_width=18.0, max_height=5.5)
    assert out[0]["height"] == 5.5
    assert out[0]["width"] == 18.0
    assert out[1]["x"] == 10
    assert out[2]["height"] == 30
    assert out[3]["width"] == 50
    assert out[4]["height"] == 30
    assert len(notes) == 1


def test_clamp_respects_allow_large():
    anns = [{"type": "rect", "x": 0, "y": 0, "width": 50, "height": 30, "allow_large": True}]
    out, notes = clamp_tight_box_sizes(anns, space="percent")
    assert out[0]["height"] == 30
    assert notes == []
