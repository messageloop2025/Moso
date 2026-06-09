"""大图局部识别：区域裁剪、分块与局部坐标回填。"""

from __future__ import annotations

import io
from typing import Any


def _float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _int(val: Any, default: int = 0) -> int:
    try:
        return int(round(float(val)))
    except (TypeError, ValueError):
        return default


def normalize_region_to_pixels(
    region: dict | None,
    src_w: int,
    src_h: int,
    coordinate_space: str | None = "auto",
    *,
    pad_ratio: float = 0.0,
) -> dict | None:
    """把 region 转为原图像素 bbox。

    支持 x/y/width/height 或 left/top/right/bottom。返回字段为
    x/y/width/height/left/top/right/bottom，均为原图像素。
    """
    if not isinstance(region, dict) or src_w <= 0 or src_h <= 0:
        return None
    space = (coordinate_space or "auto").strip().lower()
    if "left" in region or "top" in region or "right" in region or "bottom" in region:
        x = _float(region.get("left", region.get("x")), 0)
        y = _float(region.get("top", region.get("y")), 0)
        right = _float(region.get("right"), x + _float(region.get("width", region.get("w")), src_w))
        bottom = _float(region.get("bottom"), y + _float(region.get("height", region.get("h")), src_h))
        w = right - x
        h = bottom - y
    else:
        x = _float(region.get("x"), 0)
        y = _float(region.get("y"), 0)
        w = _float(region.get("width", region.get("w")), src_w)
        h = _float(region.get("height", region.get("h")), src_h)
    if space in ("auto", ""):
        vals = [x, y, w, h]
        if all(0.0 <= v <= 1.0 for v in vals):
            space = "norm"
        elif 0.0 <= x <= 100.0 and 0.0 <= y <= 100.0 and 0.0 < w <= 100.0 and 0.0 < h <= 100.0 and x + w <= 110.0 and y + h <= 110.0:
            space = "percent"
        elif 0.0 <= x <= 1000.0 and 0.0 <= y <= 1000.0 and 0.0 < w <= 1000.0 and 0.0 < h <= 1000.0 and x + w <= 1100.0 and y + h <= 1100.0:
            space = "norm1000"
        else:
            space = "pixel"
    if space in ("percent", "pct", "percentage", "%"):
        x, y, w, h = x / 100.0 * src_w, y / 100.0 * src_h, w / 100.0 * src_w, h / 100.0 * src_h
    elif space in ("norm", "normalized", "unit", "ratio", "fraction", "0-1", "0..1", "0~1"):
        x, y, w, h = x * src_w, y * src_h, w * src_w, h * src_h
    elif space in ("norm1000", "0-1000", "0~1000", "thousand"):
        x, y, w, h = x / 1000.0 * src_w, y / 1000.0 * src_h, w / 1000.0 * src_w, h / 1000.0 * src_h
    # pixel and unknown spaces are treated as pixel to keep the helper permissive.

    pad = max(0.0, min(1.0, _float(pad_ratio, 0.0)))
    if pad:
        px = w * pad
        py = h * pad
        x -= px
        y -= py
        w += px * 2
        h += py * 2

    left = max(0, min(src_w - 1, int(round(x))))
    top = max(0, min(src_h - 1, int(round(y))))
    right = max(left + 1, min(src_w, int(round(x + max(1.0, w)))))
    bottom = max(top + 1, min(src_h, int(round(y + max(1.0, h)))))
    return {
        "x": left,
        "y": top,
        "width": max(1, right - left),
        "height": max(1, bottom - top),
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "coordinate_space": "pixel",
        "input_coordinate_space": space,
    }


def _region_meta(pixel: dict, src_w: int, src_h: int, *, source: str = "region") -> dict:
    x = int(pixel["x"])
    y = int(pixel["y"])
    w = int(pixel["width"])
    h = int(pixel["height"])
    return {
        "source": source,
        "source_width": int(src_w),
        "source_height": int(src_h),
        "original_width": int(src_w),
        "original_height": int(src_h),
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "left": x,
        "top": y,
        "right": x + w,
        "bottom": y + h,
        "offset_x": x,
        "offset_y": y,
        "region_width": w,
        "region_height": h,
        "percent": {
            "x": round(x / max(1, src_w) * 100.0, 4),
            "y": round(y / max(1, src_h) * 100.0, 4),
            "width": round(w / max(1, src_w) * 100.0, 4),
            "height": round(h / max(1, src_h) * 100.0, 4),
        },
    }


def crop_image_region(
    raw: bytes,
    region: dict | None,
    *,
    coordinate_space: str | None = "pixel",
    pad_ratio: float = 0.0,
    magnify_min_side: int = 1024,
    magnify_max_factor: float = 4.0,
) -> tuple[bytes, dict]:
    """从原图裁剪局部区域，返回 (png_bytes, region_meta)。

    小裁剪块会按 LANCZOS 放大到 ``magnify_min_side`` 长边（上限 ``magnify_max_factor`` 倍），
    让视觉模型能看清细节、精确定位。由于回填用百分比（分辨率无关），放大不影响坐标换算。
    """
    from PIL import Image

    im = Image.open(io.BytesIO(raw))
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    src_w, src_h = im.size
    pixel = normalize_region_to_pixels(region, src_w, src_h, coordinate_space, pad_ratio=pad_ratio)
    if not pixel:
        pixel = normalize_region_to_pixels({"x": 0, "y": 0, "width": src_w, "height": src_h}, src_w, src_h)
    assert pixel is not None
    cropped = im.crop((pixel["left"], pixel["top"], pixel["right"], pixel["bottom"]))
    cw, ch = cropped.size
    magnify = 1.0
    target = max(0, int(magnify_min_side or 0))
    if target and cw > 0 and ch > 0:
        long_side = max(cw, ch)
        if long_side < target:
            magnify = min(float(magnify_max_factor or 1.0), target / float(long_side))
            if magnify > 1.001:
                cropped = cropped.resize(
                    (max(1, int(round(cw * magnify))), max(1, int(round(ch * magnify)))),
                    Image.LANCZOS,
                )
    out = io.BytesIO()
    cropped.save(out, format="PNG", optimize=True)
    meta = _region_meta(pixel, src_w, src_h)
    meta["magnify"] = round(magnify, 4)
    meta["rendered_width"] = cropped.size[0]
    meta["rendered_height"] = cropped.size[1]
    return out.getvalue(), meta


def map_local_annotations_to_original(
    annotations: list[dict] | None,
    region_meta: dict | None,
    *,
    coordinate_space: str | None = "percent",
    reference_width: int | float | None = None,
    reference_height: int | float | None = None,
) -> list[dict] | None:
    """把局部图上的 annotations 映射回原图像素坐标。"""
    if not annotations:
        return annotations
    if not isinstance(region_meta, dict):
        return annotations
    rx = _float(region_meta.get("x", region_meta.get("left")), 0)
    ry = _float(region_meta.get("y", region_meta.get("top")), 0)
    rw = max(1.0, _float(region_meta.get("width", region_meta.get("region_width")), 1))
    rh = max(1.0, _float(region_meta.get("height", region_meta.get("region_height")), 1))
    space = (coordinate_space or "percent").strip().lower()
    if space in ("percent", "pct", "percentage", "%"):
        sx, sy, ox, oy = rw / 100.0, rh / 100.0, rx, ry
    elif space in ("norm", "normalized", "unit", "ratio", "fraction", "0-1", "0..1", "0~1"):
        sx, sy, ox, oy = rw, rh, rx, ry
    elif space in ("norm1000", "0-1000", "0~1000", "thousand"):
        sx, sy, ox, oy = rw / 1000.0, rh / 1000.0, rx, ry
    else:
        ref_w = _float(reference_width, rw)
        ref_h = _float(reference_height, rh)
        sx, sy, ox, oy = rw / max(1.0, ref_w), rh / max(1.0, ref_h), rx, ry

    out: list[dict] = []
    for ann in annotations:
        if not isinstance(ann, dict):
            continue
        item = dict(ann)
        for key in ("x", "x1", "x2", "left", "right", "anchor_x", "label_x"):
            if key in item and item[key] is not None:
                item[key] = int(round(ox + _float(item[key]) * sx))
        for key in ("y", "y1", "y2", "top", "bottom", "anchor_y", "label_y"):
            if key in item and item[key] is not None:
                item[key] = int(round(oy + _float(item[key]) * sy))
        for key in ("width", "w"):
            if key in item and item[key] is not None:
                item[key] = max(1, int(round(_float(item[key]) * sx)))
        for key in ("height", "h"):
            if key in item and item[key] is not None:
                item[key] = max(1, int(round(_float(item[key]) * sy)))
        pts = item.get("points")
        if isinstance(pts, list):
            new_pts = []
            for p in pts:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    new_pts.append([int(round(ox + _float(p[0]) * sx)), int(round(oy + _float(p[1]) * sy))])
                else:
                    new_pts.append(p)
            item["points"] = new_pts
        out.append(item)
    return out


def build_tile_regions(
    src_w: int,
    src_h: int,
    rows: int = 2,
    cols: int = 2,
    overlap_ratio: float = 0.08,
) -> list[dict]:
    """生成覆盖整图的分块区域列表。"""
    w, h = max(1, int(src_w)), max(1, int(src_h))
    r = max(1, min(12, int(rows or 1)))
    c = max(1, min(12, int(cols or 1)))
    overlap = max(0.0, min(0.45, _float(overlap_ratio, 0.08)))
    tile_w = w / float(c)
    tile_h = h / float(r)
    tiles: list[dict] = []
    tid = 1
    for row in range(r):
        for col in range(c):
            x = col * tile_w
            y = row * tile_h
            pixel = normalize_region_to_pixels(
                {"x": x, "y": y, "width": tile_w, "height": tile_h},
                w,
                h,
                "pixel",
                pad_ratio=overlap,
            )
            if not pixel:
                continue
            meta = _region_meta(pixel, w, h, source="tile")
            meta["tile_id"] = tid
            meta["row"] = row
            meta["col"] = col
            tiles.append(meta)
            tid += 1
    return tiles
