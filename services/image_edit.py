"""聊天附件图片的简单编辑：旋转/裁剪/缩放/画框画线/半透明遮罩/文字（供 AI 工具调用）。"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("edgeops.image_edit")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RESOLVED_CJK_FONT_PATH: str | None = None
_CJK_FONT_DISCOVERED = False

_HEX_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6}([0-9a-fA-F]{2})?)$")


def _parse_color(val: Any, default: tuple[int, ...] = (255, 0, 0, 255)) -> tuple[int, int, int, int]:
    if val is None:
        d = default if len(default) >= 4 else (*default[:3], 255)
        return (int(d[0]), int(d[1]), int(d[2]), int(d[3]))
    if isinstance(val, (list, tuple)) and len(val) >= 3:
        parts = [max(0, min(255, int(x))) for x in val[:4]]
        if len(parts) == 3:
            parts.append(255)
        return tuple(parts[:4])  # type: ignore[return-value]
    s = str(val).strip()
    if not s or s.lower() in ("none", "transparent"):
        return (0, 0, 0, 0)
    m = _HEX_COLOR_RE.match(s)
    if m:
        hex_s = m.group(1)
        if len(hex_s) == 6:
            return (
                int(hex_s[0:2], 16),
                int(hex_s[2:4], 16),
                int(hex_s[4:6], 16),
                255,
            )
        return (
            int(hex_s[0:2], 16),
            int(hex_s[2:4], 16),
            int(hex_s[4:6], 16),
            int(hex_s[6:8], 16),
        )
    named = {
        "red": (255, 0, 0, 255),
        "green": (0, 200, 0, 255),
        "blue": (0, 120, 255, 255),
        "yellow": (255, 220, 0, 255),
        "orange": (255, 140, 0, 255),
        "white": (255, 255, 255, 255),
        "black": (0, 0, 0, 255),
    }
    d = named.get(s.lower(), default if len(default) >= 4 else (*default[:3], 255))
    return (int(d[0]), int(d[1]), int(d[2]), int(d[3]))


def _normalize_opacity(val: Any, default: float = 1.0) -> float:
    """0~1 为小数透明度；1~100 视为百分比。"""
    if val is None:
        return max(0.0, min(1.0, float(default)))
    try:
        f = float(val)
    except (TypeError, ValueError):
        return max(0.0, min(1.0, float(default)))
    if f > 1.0:
        f = f / 100.0
    return max(0.0, min(1.0, f))


def _resolve_rgba(
    ann: dict,
    *,
    color_keys: tuple[str, ...] = ("fill", "color"),
    default_rgb: tuple[int, int, int] = (255, 0, 0),
    default_opacity: float | None = None,
    require_color: bool = False,
) -> tuple[int, int, int, int] | None:
    color_val = None
    for key in color_keys:
        v = ann.get(key)
        if v not in (None, "", "none", "transparent"):
            color_val = v
            break
    if color_val is None and not require_color:
        return None
    rgba = _parse_color(color_val or default_rgb, (*default_rgb, 255))
    op = ann.get("opacity")
    if op is None:
        op = ann.get("alpha")
    if op is not None:
        rgba = (
            rgba[0],
            rgba[1],
            rgba[2],
            int(round(_normalize_opacity(op) * 255)),
        )
    elif default_opacity is not None:
        rgba = (
            rgba[0],
            rgba[1],
            rgba[2],
            int(round(_normalize_opacity(default_opacity) * 255)),
        )
    elif len(str(color_val or "")) <= 7:
        pass
    return rgba


def _cjk_font_candidate_paths() -> list[str]:
    """按优先级列出可能的中文字体路径（仓库内置 → Linux 容器 → Windows 开发机）。"""
    paths: list[str] = []
    bundled = _PROJECT_ROOT / "resources" / "fonts"
    if bundled.is_dir():
        for pattern in ("*.ttf", "*.ttc", "*.otf", "*.TTF", "*.TTC", "*.OTF"):
            paths.extend(str(p) for p in sorted(bundled.glob(pattern)))
    paths.extend([
        # Debian/Ubuntu：fonts-wqy-zenhei
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        # Debian/Ubuntu：fonts-noto-cjk
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        # 常见国产发行版路径
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        # Windows 开发环境
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/msyhl.ttc",
    ])
    return paths


def discover_cjk_font_path(*, log: bool = True) -> str | None:
    """返回首个可用的中文字体文件路径；容器内应至少安装 fonts-wqy-zenhei 或 fonts-noto-cjk。"""
    global _RESOLVED_CJK_FONT_PATH, _CJK_FONT_DISCOVERED
    if _CJK_FONT_DISCOVERED:
        return _RESOLVED_CJK_FONT_PATH or None
    _CJK_FONT_DISCOVERED = True
    for path in _cjk_font_candidate_paths():
        if Path(path).is_file():
            _RESOLVED_CJK_FONT_PATH = path
            if log:
                logger.info("image_edit: 使用中文字体 %s", path)
            return path
    _RESOLVED_CJK_FONT_PATH = ""
    if log:
        logger.warning(
            "image_edit: 未找到中文字体，图片内中文标注可能显示为方框。"
            "容器请 apt 安装 fonts-wqy-zenhei 或 fonts-noto-cjk；"
            "也可将 .ttf/.ttc/.otf 放到 resources/fonts/"
        )
    return None


def assert_cjk_font_ready() -> str:
    """构建/健康检查：确保至少有一种中文字体可用。"""
    path = discover_cjk_font_path()
    if not path:
        raise RuntimeError(
            "缺少中文字体：请安装 fonts-wqy-zenhei 或 fonts-noto-cjk，"
            "或将字体文件放入 resources/fonts/"
        )
    return path


def _try_load_truetype(path: str, size: int):
    from PIL import ImageFont

    p = Path(path)
    if p.suffix.lower() == ".ttc":
        last_exc: OSError | None = None
        for idx in range(4):
            try:
                return ImageFont.truetype(str(p), size, index=idx)
            except OSError as exc:
                last_exc = exc
        if last_exc:
            raise last_exc
    return ImageFont.truetype(str(p), size)


def _load_font(size: int):
    from PIL import ImageFont

    size = max(8, min(120, int(size or 16)))
    cjk = discover_cjk_font_path(log=False)
    if cjk:
        try:
            return _try_load_truetype(cjk, size)
        except OSError as exc:
            logger.warning("image_edit: 加载中文字体失败 path=%s err=%s", cjk, exc)
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _float(val: Any, default: float = 1.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def scale_annotations(
    annotations: list[dict] | None,
    scale_x: float,
    scale_y: float,
    *,
    offset_x: float = 0,
    offset_y: float = 0,
) -> list[dict] | None:
    """将标注坐标从 reference 空间换算到原图像素空间。"""

    if not annotations:
        return annotations
    sx, sy = float(scale_x or 1), float(scale_y or 1)
    ox, oy = float(offset_x or 0), float(offset_y or 0)
    out: list[dict] = []
    for ann in annotations:
        if not isinstance(ann, dict):
            continue
        item = dict(ann)
        point_keys = (
            "x",
            "y",
            "x1",
            "y1",
            "x2",
            "y2",
            "left",
            "top",
            "right",
            "bottom",
            "anchor_x",
            "anchor_y",
            "label_x",
            "label_y",
        )
        for key in point_keys:
            if key in item and item[key] is not None:
                val = _float(item[key], 0)
                if key in ("x", "x1", "left", "anchor_x", "label_x"):
                    item[key] = int(round((val - ox) * sx))
                elif key in ("y", "y1", "top", "anchor_y", "label_y"):
                    item[key] = int(round((val - oy) * sy))
                elif key in ("x2", "right"):
                    item[key] = int(round((val - ox) * sx))
                elif key in ("y2", "bottom"):
                    item[key] = int(round((val - oy) * sy))
        for key in ("width", "height", "w", "h"):
            if key in item and item[key] is not None:
                val = _float(item[key], 0)
                item[key] = int(round(val * (sx if key in ("width", "w") else sy)))
        for key in ("radius", "ring_width", "arm_length", "gap_radius"):
            if key in item and item[key] is not None:
                item[key] = int(round(_float(item[key], 0) * ((sx + sy) / 2.0)))
        pts = item.get("points")
        if isinstance(pts, list):
            new_pts = []
            for p in pts:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    new_pts.append([
                        int(round((_float(p[0]) - ox) * sx)),
                        int(round((_float(p[1]) - oy) * sy)),
                    ])
                else:
                    new_pts.append(p)
            item["points"] = new_pts
        out.append(item)
    return out


def read_image_pixel_size_from_bytes(raw: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            return int(im.size[0]), int(im.size[1])
    except Exception:
        return None


def annotation_max_extent(annotations: list[dict] | None) -> tuple[float, float]:
    max_x = 0.0
    max_y = 0.0
    if not annotations:
        return max_x, max_y
    for ann in annotations:
        if not isinstance(ann, dict):
            continue
        x = _float(ann.get("x"), 0)
        y = _float(ann.get("y"), 0)
        w = _float(ann.get("width", ann.get("w")), 0)
        h = _float(ann.get("height", ann.get("h")), 0)
        max_x = max(max_x, x + w, _float(ann.get("x2"), 0), _float(ann.get("right"), 0))
        max_y = max(max_y, y + h, _float(ann.get("y2"), 0), _float(ann.get("bottom"), 0))
        radius = max(
            _float(ann.get("radius"), 0),
            _float(ann.get("arm_length"), 0),
            _float(ann.get("gap_radius"), 0),
        )
        for px_key in ("anchor_x", "label_x"):
            px = ann.get(px_key)
            if px is not None:
                max_x = max(max_x, _float(px) + radius)
        for py_key in ("anchor_y", "label_y"):
            py = ann.get(py_key)
            if py is not None:
                max_y = max(max_y, _float(py) + radius)
        for p in ann.get("points") or []:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                max_x = max(max_x, _float(p[0]))
                max_y = max(max_y, _float(p[1]))
    return max_x, max_y


def infer_annotation_reference_size(
    annotations: list[dict] | None,
    src_w: int,
    src_h: int,
    vision_w: int,
    vision_h: int,
) -> tuple[int, int] | None:
    """推断 annotations 坐标所在参考画布；返回 (ref_w, ref_h) 或 None（已是原图坐标）。"""
    if not annotations or src_w <= 0 or src_h <= 0:
        return None
    vw = max(1, int(vision_w or src_w))
    vh = max(1, int(vision_h or src_h))
    if vw == int(src_w) and vh == int(src_h):
        return None
    max_x, max_y = annotation_max_extent(annotations)
    if max_x <= 0 and max_y <= 0:
        return vw, vh
    fits_vision = max_x <= vw * 1.08 and max_y <= vh * 1.08
    fits_source = max_x <= int(src_w) * 1.02 and max_y <= int(src_h) * 1.02
    if fits_source and not fits_vision:
        return None
    if fits_vision and not fits_source:
        return vw, vh
    if fits_vision and fits_source:
        if max_x <= vw * 0.99 and max_y <= vh * 0.99:
            return vw, vh
        return None
    if max_x <= 1000 and max_y <= 1000 and (int(src_w) > 1000 or int(src_h) > 1000):
        return 1000, 1000
    if fits_source:
        return None
    return vw, vh


def _composite_overlay(img, draw_fn: Callable) -> Any:
    from PIL import Image

    if img.mode != "RGBA":
        img = img.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_fn(overlay)
    return Image.alpha_composite(img, overlay)


def _bbox_rect(ann: dict) -> tuple[int, int, int, int] | None:
    x = _int(ann.get("x"), 0)
    y = _int(ann.get("y"), 0)
    w = _int(ann.get("width", ann.get("w")), 0)
    h = _int(ann.get("height", ann.get("h")), 0)
    if w <= 0 or h <= 0:
        return None
    return (x, y, x + w, y + h)


def _bbox_ellipse(ann: dict) -> tuple[int, int, int, int] | None:
    x = _int(ann.get("x"), 0)
    y = _int(ann.get("y"), 0)
    w = _int(ann.get("width", ann.get("w")), 0)
    h = _int(ann.get("height", ann.get("h")), w or 0)
    if w <= 0:
        return None
    return (x, y, x + w, y + (h or w))


def _polygon_points(ann: dict) -> list[tuple[int, int]]:
    pts = ann.get("points") or []
    flat: list[tuple[int, int]] = []
    for p in pts:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            flat.append((_int(p[0]), _int(p[1])))
    return flat


def _draw_fill_shape(img, ann: dict, kind: str, fill_rgba: tuple[int, int, int, int]) -> Any:
    from PIL import ImageDraw

    if fill_rgba[3] <= 0:
        return img

    def _on_overlay(overlay):
        od = ImageDraw.Draw(overlay)
        if kind in ("rect", "rectangle", "box", "overlay", "mask", "highlight"):
            box = _bbox_rect(ann)
            if box:
                od.rectangle(box, fill=fill_rgba)
        elif kind in ("ellipse", "circle"):
            box = _bbox_ellipse(ann)
            if box:
                od.ellipse(box, fill=fill_rgba)
        elif kind == "polygon":
            flat = _polygon_points(ann)
            if len(flat) >= 3:
                od.polygon(flat, fill=fill_rgba)

    return _composite_overlay(img, _on_overlay)


def _draw_stroke_shape(img, ann: dict, kind: str, outline_rgba: tuple[int, int, int, int], lw: int) -> Any:
    from PIL import ImageDraw

    if outline_rgba[3] <= 0 or lw <= 0:
        return img
    draw = ImageDraw.Draw(img)
    outline = outline_rgba[:3]
    if kind in ("rect", "rectangle", "box", "overlay", "mask", "highlight"):
        box = _bbox_rect(ann)
        if box:
            draw.rectangle(box, outline=outline, width=lw)
    elif kind in ("ellipse", "circle"):
        box = _bbox_ellipse(ann)
        if box:
            draw.ellipse(box, outline=outline, width=lw)
    elif kind == "polygon":
        flat = _polygon_points(ann)
        if len(flat) >= 3:
            draw.polygon(flat, outline=outline)
    return img


def _line_width(ann: dict, default: int = 2) -> int:
    v = ann.get("line_width")
    if v is None:
        v = ann.get("stroke_width")
    return max(1, _int(v, default))


def _point_center(ann: dict) -> tuple[int, int]:
    x_key = "anchor_x" if ann.get("anchor_x") is not None else "x"
    y_key = "anchor_y" if ann.get("anchor_y") is not None else "y"
    cx = _int(ann.get(x_key), 0)
    cy = _int(ann.get(y_key), 0)
    w = _int(ann.get("width", ann.get("w")), 0)
    h = _int(ann.get("height", ann.get("h")), 0)
    if w > 0:
        cx += w // 2
    if h > 0:
        cy += h // 2
    return cx, cy


def _marker_rgba(ann: dict) -> tuple[int, int, int, int]:
    return _resolve_rgba(
        ann,
        color_keys=("color", "outline", "stroke", "fill"),
        default_rgb=(255, 0, 0),
        default_opacity=1.0,
    ) or (255, 0, 0, 255)


def _draw_crosshair(img, ann: dict, *, color_rgba: tuple[int, int, int, int] | None = None) -> Any:
    from PIL import ImageDraw

    rgba = color_rgba or _marker_rgba(ann)
    if rgba[3] <= 0:
        return img
    cx, cy = _point_center(ann)
    arm = max(4, _int(ann.get("arm_length"), 12))
    gap = max(1, _int(ann.get("gap_radius"), 3))
    lw = _line_width(ann, 2)
    segments = [
        (cx - arm, cy, cx - gap, cy),
        (cx + gap, cy, cx + arm, cy),
        (cx, cy - arm, cx, cy - gap),
        (cx, cy + gap, cx, cy + arm),
    ]

    def _draw(draw):
        for x1, y1, x2, y2 in segments:
            draw.line((x1, y1, x2, y2), fill=rgba if rgba[3] < 255 else rgba[:3], width=lw)

    if rgba[3] < 255:
        return _composite_overlay(img, lambda overlay: _draw(ImageDraw.Draw(overlay)))
    _draw(ImageDraw.Draw(img))
    return img


def _draw_target_ring(img, ann: dict, *, color_rgba: tuple[int, int, int, int] | None = None) -> Any:
    from PIL import ImageDraw

    rgba = color_rgba or _marker_rgba(ann)
    if rgba[3] <= 0:
        return img
    cx, cy = _point_center(ann)
    radius = max(3, _int(ann.get("radius") or ann.get("size"), 8))
    lw = max(1, _int(ann.get("ring_width") or ann.get("line_width"), _line_width(ann, 2)))
    box = (cx - radius, cy - radius, cx + radius, cy + radius)

    def _draw(draw):
        draw.ellipse(box, outline=rgba if rgba[3] < 255 else rgba[:3], width=lw)

    if rgba[3] < 255:
        return _composite_overlay(img, lambda overlay: _draw(ImageDraw.Draw(overlay)))
    _draw(ImageDraw.Draw(img))
    return img


def _draw_filled_pin(img, ann: dict, *, color_rgba: tuple[int, int, int, int] | None = None) -> Any:
    from PIL import ImageDraw

    rgba = color_rgba or _marker_rgba(ann)
    if rgba[3] <= 0:
        return img
    cx, cy = _point_center(ann)
    radius = max(3, _int(ann.get("radius") or ann.get("size"), 8))
    lw = _line_width(ann, 2)

    def _draw(draw):
        rgb = rgba if rgba[3] < 255 else rgba[:3]
        outer = (cx - radius, cy - radius, cx + radius, cy + radius)
        draw.ellipse(outer, fill=rgb, outline=rgb, width=max(1, lw))
        inner_r = max(1, radius // 3)
        draw.ellipse((cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r), fill=(255, 255, 255, 255))

    if rgba[3] < 255:
        return _composite_overlay(img, lambda overlay: _draw(ImageDraw.Draw(overlay)))
    _draw(ImageDraw.Draw(img))
    return img


def _draw_callout(img, ann: dict) -> Any:
    from PIL import ImageDraw

    rgba = _marker_rgba(ann)
    if rgba[3] <= 0:
        return img
    ax = _int(ann.get("anchor_x", ann.get("x")), 0)
    ay = _int(ann.get("anchor_y", ann.get("y")), 0)
    lx = _int(ann.get("label_x", ann.get("x")), ax + 24)
    ly = _int(ann.get("label_y", ann.get("y")), ay - 24)
    text = str(ann.get("text") or ann.get("label") or "")
    lw = _line_width(ann, 2)
    font = _load_font(_int(ann.get("size"), 16))
    leader = str(ann.get("leader", "line")).strip().lower()
    anchor_style = str(ann.get("anchor_style", "crosshair")).strip().lower()

    def _draw(draw):
        fill = rgba if rgba[3] < 255 else rgba[:3]
        if anchor_style in ("crosshair", "hair"):
            # callout 内联的准星只画引导点，避免额外覆盖目标中心。
            arm = max(4, _int(ann.get("arm_length"), 8))
            gap = max(1, _int(ann.get("gap_radius"), 3))
            for seg in ((ax - arm, ay, ax - gap, ay), (ax + gap, ay, ax + arm, ay), (ax, ay - arm, ax, ay - gap), (ax, ay + gap, ax, ay + arm)):
                draw.line(seg, fill=fill, width=lw)
        elif anchor_style == "dot":
            draw.point((ax, ay), fill=fill)
        elif anchor_style in ("target", "ring"):
            radius = max(3, _int(ann.get("radius"), 6))
            draw.ellipse((ax - radius, ay - radius, ax + radius, ay + radius), outline=fill, width=lw)
        if leader != "none":
            draw.line((ax, ay, lx, ly), fill=fill, width=lw)
        if text:
            draw.text((lx, ly), text, fill=fill, font=font)

    if rgba[3] < 255:
        return _composite_overlay(img, lambda overlay: _draw(ImageDraw.Draw(overlay)))
    _draw(ImageDraw.Draw(img))
    return img


def _apply_annotation(img, ann: dict) -> Any:
    from PIL import ImageDraw

    kind = (ann.get("type") or "").strip().lower()
    if not kind:
        return img

    lw = _line_width(ann, 2)

    if kind in ("overlay", "mask", "highlight"):
        fill_rgba = _resolve_rgba(
            ann,
            color_keys=("fill", "color"),
            default_rgb=(255, 220, 0),
            default_opacity=0.35,
            require_color=False,
        )
        if fill_rgba is None:
            fill_rgba = (255, 220, 0, int(round(0.35 * 255)))
        shape = (ann.get("shape") or "rect").strip().lower()
        if shape in ("ellipse", "circle"):
            kind = "ellipse"
        elif shape == "polygon":
            kind = "polygon"
        else:
            kind = "rect"
        img = _draw_fill_shape(img, ann, kind, fill_rgba)
        outline_rgba = _resolve_rgba(
            ann,
            color_keys=("outline", "stroke"),
            default_rgb=(255, 0, 0),
            default_opacity=1.0,
        )
        if outline_rgba and ann.get("outline") not in (None, "", "none", "transparent"):
            img = _draw_stroke_shape(img, ann, kind, outline_rgba, lw)
        return img

    if kind in ("rect", "rectangle", "box"):
        fill_rgba = _resolve_rgba(ann, color_keys=("fill",))
        if fill_rgba:
            img = _draw_fill_shape(img, ann, kind, fill_rgba)
        outline_rgba = _resolve_rgba(
            ann,
            color_keys=("outline", "color", "stroke"),
            default_rgb=(255, 0, 0),
            default_opacity=1.0,
        )
        if outline_rgba is None:
            outline_rgba = (255, 0, 0, 255)
        return _draw_stroke_shape(img, ann, kind, outline_rgba, lw)

    if kind in ("crosshair", "hair"):
        return _draw_crosshair(img, ann)

    if kind in ("target", "ring"):
        return _draw_target_ring(img, ann)

    if kind == "callout":
        return _draw_callout(img, ann)

    if kind in ("pin", "marker", "point"):
        style = str(ann.get("style") or "").strip().lower()
        if style in ("crosshair", "hair"):
            return _draw_crosshair(img, ann)
        if style in ("target", "ring", "hollow"):
            return _draw_target_ring(img, ann)
        return _draw_filled_pin(img, ann)

    if kind == "line":
        outline_rgba = _resolve_rgba(
            ann,
            color_keys=("color", "outline", "stroke"),
            default_rgb=(255, 0, 0),
            default_opacity=1.0,
        ) or (255, 0, 0, 255)
        lw = max(1, _int(ann.get("width") or ann.get("line_width"), 2))

        def _line_overlay(overlay):
            ImageDraw.Draw(overlay).line(
                [
                    _int(ann.get("x1"), 0),
                    _int(ann.get("y1"), 0),
                    _int(ann.get("x2"), 0),
                    _int(ann.get("y2"), 0),
                ],
                fill=outline_rgba,
                width=lw,
            )

        if outline_rgba[3] < 255:
            return _composite_overlay(img, _line_overlay)
        ImageDraw.Draw(img).line(
            [
                _int(ann.get("x1"), 0),
                _int(ann.get("y1"), 0),
                _int(ann.get("x2"), 0),
                _int(ann.get("y2"), 0),
            ],
            fill=outline_rgba[:3],
            width=lw,
        )
        return img

    if kind in ("ellipse", "circle"):
        fill_rgba = _resolve_rgba(ann, color_keys=("fill",))
        if fill_rgba:
            img = _draw_fill_shape(img, ann, kind, fill_rgba)
        outline_rgba = _resolve_rgba(
            ann,
            color_keys=("outline", "color", "stroke"),
            default_rgb=(255, 0, 0),
            default_opacity=1.0,
        )
        if outline_rgba is None:
            outline_rgba = (255, 0, 0, 255)
        return _draw_stroke_shape(img, ann, kind, outline_rgba, lw)

    if kind == "polygon":
        fill_rgba = _resolve_rgba(ann, color_keys=("fill",))
        if fill_rgba:
            img = _draw_fill_shape(img, ann, kind, fill_rgba)
        outline_rgba = _resolve_rgba(
            ann,
            color_keys=("outline", "color", "stroke"),
            default_rgb=(255, 0, 0),
            default_opacity=1.0,
        )
        if outline_rgba:
            return _draw_stroke_shape(img, ann, kind, outline_rgba, lw)
        return img

    if kind == "text":
        text = str(ann.get("text") or "")
        if not text:
            return img
        font = _load_font(_int(ann.get("size"), 16))
        text_rgba = _resolve_rgba(
            ann,
            color_keys=("color", "fill"),
            default_rgb=(255, 0, 0),
            default_opacity=1.0,
        ) or (255, 0, 0, 255)
        x, y = _int(ann.get("x"), 0), _int(ann.get("y"), 0)

        def _text_overlay(overlay):
            ImageDraw.Draw(overlay).text((x, y), text, fill=text_rgba, font=font)

        if text_rgba[3] < 255:
            return _composite_overlay(img, _text_overlay)
        ImageDraw.Draw(img).text((x, y), text, fill=text_rgba[:3], font=font)
        return img

    return img


def apply_image_edits(
    raw: bytes,
    *,
    rotate: float = 0,
    crop: dict | None = None,
    scale: float | None = None,
    annotations: list[dict] | None = None,
) -> tuple[bytes, str]:
    """对图片字节应用变换与标注，返回 (png_bytes, mime)。"""
    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    angle = _float(rotate, 0)
    if abs(angle) > 0.01:
        img = img.rotate(-angle, expand=True, resample=Image.Resampling.BICUBIC)

    if crop:
        left = _int(crop.get("x", crop.get("left")), 0)
        top = _int(crop.get("y", crop.get("top")), 0)
        width = crop.get("width", crop.get("w"))
        height = crop.get("height", crop.get("h"))
        if width is not None and height is not None:
            right = left + max(1, _int(width, 1))
            bottom = top + max(1, _int(height, 1))
        else:
            right = _int(crop.get("right"), img.width)
            bottom = _int(crop.get("bottom"), img.height)
        left = max(0, min(left, img.width - 1))
        top = max(0, min(top, img.height - 1))
        right = max(left + 1, min(right, img.width))
        bottom = max(top + 1, min(bottom, img.height))
        img = img.crop((left, top, right, bottom))

    sc = _float(scale, 1.0) if scale is not None else 1.0
    if abs(sc - 1.0) > 0.001 and sc > 0:
        nw = max(1, int(img.width * sc))
        nh = max(1, int(img.height * sc))
        img = img.resize((nw, nh), Image.Resampling.LANCZOS)

    if annotations:
        for ann in annotations:
            if isinstance(ann, dict):
                img = _apply_annotation(img, ann)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue(), "image/png"
