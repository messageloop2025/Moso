"""图片标注坐标校准：校准线拟合 + 多坐标系自动推断（含 0–1000 归一化）。"""
from __future__ import annotations

from typing import Any

CALIBRATION_MIN_POINTS = 2

CALIBRATION_STRATEGY_PROMPT = """
**图片标注坐标（后端自动换算，AI 禁止心算缩放）**
1. **推荐**：直接 `annotations` + 同一所见坐标系的 `calibration_observations`（至少 2 条 id）。
2. **可选探测**：`calibration_probe=true` 得带编号角标（①②③④）的参考图与 `calibration_reference`。
3. `calibration_observations` 填 `{id,x,y}`（校准线/角标在你所见画面中的左上角像素）。
4. 未传观测时后端会**自动**在 norm1000 / 模型视图 / 识图尺寸 / 原图 等坐标系中选最优映射。
5. 许多 VLM 使用 **0–1000 归一化坐标**（LayoutLM、Nova、RynnBrain 等）；后端已支持。
6. 勿传 `use_original_coordinates`；最终输出不含校准线。
""".strip()


def _float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def parse_global_transform(gt: Any) -> tuple[float, float, float, float] | None:
    """解析整组标注的全局微调（宏观缩放+平移）。

    支持字段：scale（统一缩放）/ scale_x / scale_y / offset_x / offset_y。
    返回 (scale_x, scale_y, offset_x, offset_y)；恒等变换或无效输入返回 None。
    offset 单位与 annotations 所在坐标系一致（percent 下为百分点）。
    """
    if not isinstance(gt, dict):
        return None
    scale = gt.get("scale")
    base = _float(scale, 1.0) if scale is not None else 1.0
    sx = _float(gt.get("scale_x"), base)
    sy = _float(gt.get("scale_y"), base)
    ox = _float(gt.get("offset_x"), 0.0)
    oy = _float(gt.get("offset_y"), 0.0)
    if sx <= 0:
        sx = 1.0
    if sy <= 0:
        sy = 1.0
    if abs(sx - 1.0) < 1e-9 and abs(sy - 1.0) < 1e-9 and abs(ox) < 1e-9 and abs(oy) < 1e-9:
        return None
    return (sx, sy, ox, oy)


def normalized_space_divisor(space: Any) -> float | None:
    """把显式坐标系名解析为「除数」：x_px = x / divisor * W。"""
    s = (str(space or "")).strip().lower()
    if s in ("percent", "pct", "percentage", "%", "百分比"):
        return 100.0
    if s in ("norm", "normalized", "unit", "ratio", "fraction", "0-1", "0..1", "0~1"):
        return 1.0
    if s in ("norm1000", "0-1000", "0~1000", "thousand", "qwen", "gemini"):
        return 1000.0
    return None


def apply_normalized_space(
    annotations: list[dict] | None,
    src_w: int,
    src_h: int,
    space: Any,
) -> tuple[list[dict] | None, float, float] | None:
    """按显式坐标系（percent / 0-1 / 0-1000）确定性换算到原图像素。"""
    div = normalized_space_divisor(space)
    if div is None or not annotations or src_w <= 0 or src_h <= 0:
        return None
    sx = float(src_w) / div
    sy = float(src_h) / div
    out = apply_calibration_transform_to_annotations(annotations, sx, sy, 0.0, 0.0)
    return out, sx, sy


CELL_GRID_DEFAULT_COLS = 12
CELL_GRID_DEFAULT_ROWS = 8


def cell_grid_dims(cols: Any, rows: Any) -> tuple[int, int]:
    try:
        c = int(cols)
    except (TypeError, ValueError):
        c = CELL_GRID_DEFAULT_COLS
    try:
        r = int(rows)
    except (TypeError, ValueError):
        r = CELL_GRID_DEFAULT_ROWS
    c = max(2, min(40, c))
    r = max(2, min(40, r))
    return c, r


def build_cell_grid_plan(
    width: int,
    height: int,
    cols: Any = CELL_GRID_DEFAULT_COLS,
    rows: Any = CELL_GRID_DEFAULT_ROWS,
) -> tuple[list[dict], dict]:
    """生成带编号的单元网格（Set-of-Mark 风格）：返回 (绘制annotations, grid_meta)。

    单元编号从 1 开始，从左到右、从上到下；AI 只需回答目标覆盖了哪些编号，
    后端用 cells_to_bbox 把编号映射为精确像素框（确定性，无需 AI 估坐标）。
    """
    w = max(1, int(width or 1))
    h = max(1, int(height or 1))
    c, r = cell_grid_dims(cols, rows)
    cell_w = w / float(c)
    cell_h = h / float(r)
    size = max(11, min(24, int(min(cell_w, cell_h) // 4)))
    draw: list[dict] = []
    # 网格线
    for i in range(1, c):
        x = int(round(i * cell_w))
        draw.append({"type": "line", "x1": x, "y1": 0, "x2": x, "y2": h, "color": "#00e0ff", "width": 1, "opacity": 0.4})
    for j in range(1, r):
        y = int(round(j * cell_h))
        draw.append({"type": "line", "x1": 0, "y1": y, "x2": w, "y2": y, "color": "#00e0ff", "width": 1, "opacity": 0.4})
    # 每格左上角编号
    cell_id = 1
    for row in range(r):
        for col in range(c):
            tx = int(round(col * cell_w)) + 2
            ty = int(round(row * cell_h)) + 1
            draw.append({"type": "text", "x": tx, "y": ty, "text": str(cell_id), "color": "#ff2d55", "size": size})
            cell_id += 1
    grid_meta = {
        "cols": c,
        "rows": r,
        "count": c * r,
        "cell_w": cell_w,
        "cell_h": cell_h,
        "width": w,
        "height": h,
    }
    return draw, grid_meta


def cells_to_bbox(cells: Any, grid_meta: dict) -> dict | None:
    """把选中的单元编号（1-based）并成最小外接矩形像素框。"""
    if not cells or not isinstance(grid_meta, dict):
        return None
    cols = int(grid_meta.get("cols") or 0)
    rows = int(grid_meta.get("rows") or 0)
    if cols <= 0 or rows <= 0:
        return None
    cell_w = float(grid_meta.get("cell_w") or 0)
    cell_h = float(grid_meta.get("cell_h") or 0)
    if cell_w <= 0 or cell_h <= 0:
        return None
    ids: list[int] = []
    for v in cells:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= cols * rows:
            ids.append(n)
    if not ids:
        return None
    min_col = cols
    max_col = -1
    min_row = rows
    max_row = -1
    for n in ids:
        idx = n - 1
        col = idx % cols
        row = idx // cols
        min_col = min(min_col, col)
        max_col = max(max_col, col)
        min_row = min(min_row, row)
        max_row = max(max_row, row)
    x1 = int(round(min_col * cell_w))
    y1 = int(round(min_row * cell_h))
    x2 = int(round((max_col + 1) * cell_w))
    y2 = int(round((max_row + 1) * cell_h))
    w = int(grid_meta.get("width") or 0)
    hgt = int(grid_meta.get("height") or 0)
    if w:
        x2 = min(x2, w)
    if hgt:
        y2 = min(y2, hgt)
    return {"x": x1, "y": y1, "width": max(1, x2 - x1), "height": max(1, y2 - y1)}


def build_percent_grid_annotations(width: int, height: int, step: int = 10) -> list[dict]:
    """生成百分比刻度网格（细线 + 边缘数字），供 AI 读取相对位置。"""
    w = max(1, int(width or 1))
    h = max(1, int(height or 1))
    step = max(5, min(25, int(step or 10)))
    size = max(12, min(30, w // 60))
    draw: list[dict] = []
    for pct in range(0, 101, step):
        x = int(round(w * pct / 100.0))
        y = int(round(h * pct / 100.0))
        x = min(x, w - 1)
        y = min(y, h - 1)
        draw.append({
            "type": "line", "x1": x, "y1": 0, "x2": x, "y2": h,
            "color": "#00e0ff", "width": 1, "opacity": 0.45,
        })
        draw.append({
            "type": "line", "x1": 0, "y1": y, "x2": w, "y2": y,
            "color": "#00e0ff", "width": 1, "opacity": 0.45,
        })
        label = str(pct)
        tx = min(max(0, x + 2), w - size)
        draw.append({
            "type": "text", "x": tx, "y": 2,
            "text": label, "color": "#ff0066", "size": size,
        })
        draw.append({
            "type": "text", "x": 2, "y": min(max(0, y + 2), h - size),
            "text": label, "color": "#ff0066", "size": size,
        })
    return draw


def fit_scale_offset(known: list[float], observed: list[float]) -> tuple[float, float] | None:
    """最小二乘拟合 known ≈ scale * observed + offset。"""
    if not known or len(known) != len(observed):
        return None
    n = len(known)
    if n == 1:
        return 1.0, known[0] - observed[0]
    mx = sum(observed) / n
    my = sum(known) / n
    num = sum((observed[i] - mx) * (known[i] - my) for i in range(n))
    den = sum((observed[i] - mx) ** 2 for i in range(n))
    if abs(den) < 1e-9:
        return 1.0, my - mx
    scale = num / den
    offset = my - scale * mx
    return float(scale), float(offset)


def build_calibration_plan(width: int, height: int) -> tuple[list[dict], list[dict]]:
    """生成校准参考点（原图坐标）与 Set-of-Mark 风格绘制 annotations。"""
    w = max(1, int(width or 1))
    h = max(1, int(height or 1))
    margin = max(32, min(w, h) // 32)
    arm = max(96, min(w, h) // 8)
    refs: list[dict] = []
    draw: list[dict] = []

    def add_corner(mark_id: str, label: str, x: int, y: int, color: str) -> None:
        refs.append({
            "id": mark_id,
            "x": int(x),
            "y": int(y),
            "width": arm,
            "height": 6,
            "anchor": "top-left",
            "label": label,
            "hint": f"角标 {label}（{mark_id}）左上角",
        })
        refs.append({
            "id": f"{mark_id}-v",
            "x": int(x),
            "y": int(y),
            "width": 6,
            "height": arm,
            "anchor": "top-left",
            "label": label,
        })
        for ann in (
            {
                "type": "rect", "x": x, "y": y, "width": arm, "height": 6,
                "fill": color, "opacity": 0.95, "outline": "#000000", "line_width": 2,
            },
            {
                "type": "rect", "x": x, "y": y, "width": 6, "height": arm,
                "fill": color, "opacity": 0.95, "outline": "#000000", "line_width": 2,
            },
            {
                "type": "text", "x": x + 8, "y": max(0, y - 28),
                "text": label, "color": "#ffffff", "size": 22,
            },
        ):
            draw.append(ann)

    add_corner("cal-1", "①", margin, margin, "#ff0066")
    add_corner("cal-2", "②", w - margin - arm, margin, "#00ccff")
    add_corner("cal-3", "③", margin, h - margin - arm, "#ffcc00")
    add_corner("cal-4", "④", w - margin - 6, h - margin - arm, "#00ff66")
    cx, cy = w // 2, h // 2
    refs.append({"id": "cal-c", "x": cx - arm // 2, "y": cy - 3, "width": arm, "height": 6, "anchor": "top-left", "label": "中"})
    draw.extend([
        {"type": "rect", "x": cx - arm // 2, "y": cy - 3, "width": arm, "height": 6, "fill": "#ffffff", "opacity": 0.9, "outline": "#333333", "line_width": 2},
        {"type": "rect", "x": cx - 3, "y": cy - arm // 2, "width": 6, "height": arm, "fill": "#ffffff", "opacity": 0.9, "outline": "#333333", "line_width": 2},
    ])
    return refs, draw


def compute_calibration_transform(
    reference: list[dict],
    observations: list[dict],
    *,
    min_points: int = CALIBRATION_MIN_POINTS,
) -> tuple[float, float, float, float, int] | None:
    """根据校准观测拟合 x/y 线性变换：orig = sx * obs + ox。"""
    if not reference or not observations:
        return None
    obs_map: dict[str, dict] = {}
    for item in observations:
        if not isinstance(item, dict):
            continue
        mid = (item.get("id") or "").strip()
        if mid:
            obs_map[mid] = item
    known_x: list[float] = []
    obs_x: list[float] = []
    known_y: list[float] = []
    obs_y: list[float] = []
    for ref in reference:
        if not isinstance(ref, dict):
            continue
        mid = (ref.get("id") or "").strip()
        obs = obs_map.get(mid)
        if not obs:
            continue
        rx = _float(ref.get("x"))
        ry = _float(ref.get("y"))
        rw = _float(ref.get("width"), 0)
        rh = _float(ref.get("height"), 0)
        ox = _float(obs.get("x"))
        oy = _float(obs.get("y"))
        anchor = (obs.get("anchor") or ref.get("anchor") or "top-left").strip().lower()
        if anchor == "center":
            ox += rw / 2 if rw else 0
            oy += rh / 2 if rh else 0
        known_x.append(rx)
        known_y.append(ry)
        obs_x.append(ox)
        obs_y.append(oy)
    matched = len(known_x)
    if matched < min_points:
        return None
    fit_x = fit_scale_offset(known_x, obs_x)
    fit_y = fit_scale_offset(known_y, obs_y)
    if not fit_x or not fit_y:
        return None
    sx, ox = fit_x
    sy, oy = fit_y
    return sx, sy, ox, oy, matched


def apply_calibration_transform_to_annotations(
    annotations: list[dict] | None,
    sx: float,
    sy: float,
    ox: float,
    oy: float,
) -> list[dict] | None:
    """将 AI 所见坐标系 annotations 换算到原图：coord_orig = scale * coord_obs + offset。"""
    if not annotations:
        return annotations
    out: list[dict] = []
    for ann in annotations:
        if not isinstance(ann, dict):
            continue
        item = dict(ann)
        for key in ("x", "x1", "left"):
            if key in item and item[key] is not None:
                item[key] = int(round(sx * _float(item[key]) + ox))
        for key in ("x2", "right"):
            if key in item and item[key] is not None:
                item[key] = int(round(sx * _float(item[key]) + ox))
        for key in ("y", "y1", "top"):
            if key in item and item[key] is not None:
                item[key] = int(round(sy * _float(item[key]) + oy))
        for key in ("y2", "bottom"):
            if key in item and item[key] is not None:
                item[key] = int(round(sy * _float(item[key]) + oy))
        for key in ("width", "w"):
            if key in item and item[key] is not None:
                item[key] = max(1, int(round(abs(sx) * _float(item[key]))))
        for key in ("height", "h"):
            if key in item and item[key] is not None:
                item[key] = max(1, int(round(abs(sy) * _float(item[key]))))
        pts = item.get("points")
        if isinstance(pts, list):
            new_pts = []
            for p in pts:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    new_pts.append([
                        int(round(sx * _float(p[0]) + ox)),
                        int(round(sy * _float(p[1]) + oy)),
                    ])
                else:
                    new_pts.append(p)
            item["points"] = new_pts
        out.append(item)
    return out


def _annotation_centers(annotations: list[dict]) -> list[tuple[float, float]]:
    centers: list[tuple[float, float]] = []
    for ann in annotations:
        if not isinstance(ann, dict):
            continue
        x = _float(ann.get("x"), 0)
        y = _float(ann.get("y"), 0)
        w = _float(ann.get("width", ann.get("w")), 20)
        h = _float(ann.get("height", ann.get("h")), 20)
        centers.append((x + w / 2, y + h / 2))
    return centers


def _margin_fractions(
    annotations: list[dict],
    src_w: int,
    src_h: int,
    sx: float,
    sy: float,
    ox: float,
    oy: float,
) -> tuple[float, float, float]:
    """返回 (left_frac, right_frac, in_bounds_frac)。"""
    if not annotations or src_w <= 0 or src_h <= 0:
        return 0.0, 0.0, 0.0
    transformed = apply_calibration_transform_to_annotations(annotations, sx, sy, ox, oy) or []
    n = max(1, len(transformed))
    in_bounds = 0
    left = 0
    right = 0
    for ann in transformed:
        x = _float(ann.get("x"), 0)
        y = _float(ann.get("y"), 0)
        w = max(1.0, _float(ann.get("width", ann.get("w")), 1))
        h = max(1.0, _float(ann.get("height", ann.get("h")), 1))
        cx = x + w / 2
        if -src_w * 0.02 <= x and -src_h * 0.02 <= y and x + w <= src_w * 1.05 and y + h <= src_h * 1.05:
            in_bounds += 1
        if cx < src_w * 0.12:
            left += 1
        if cx > src_w * 0.88:
            right += 1
    return left / n, right / n, in_bounds / n


def _score_transform(
    annotations: list[dict],
    src_w: int,
    src_h: int,
    sx: float,
    sy: float,
    ox: float,
    oy: float,
) -> float:
    """变换后标注越分散、越在画面内，得分越高。"""
    if not annotations:
        return 0.0
    transformed = apply_calibration_transform_to_annotations(annotations, sx, sy, ox, oy) or []
    centers: list[tuple[float, float]] = []
    in_bounds = 0
    for ann in transformed:
        x = _float(ann.get("x"), 0)
        y = _float(ann.get("y"), 0)
        w = max(1.0, _float(ann.get("width", ann.get("w")), 1))
        h = max(1.0, _float(ann.get("height", ann.get("h")), 1))
        cx, cy = x + w / 2, y + h / 2
        centers.append((cx, cy))
        if -src_w * 0.02 <= x and -src_h * 0.02 <= y and x + w <= src_w * 1.05 and y + h <= src_h * 1.05:
            in_bounds += 1
    n = max(1, len(centers))
    score = in_bounds * 12.0
    xs = [c[0] for c in centers]
    left_frac = sum(1 for cx in xs if cx < src_w * 0.12) / n
    right_frac = sum(1 for cx in xs if cx > src_w * 0.88) / n
    score -= left_frac * 45.0
    score -= right_frac * 35.0
    if len(xs) >= 2:
        x_spread = (max(xs) - min(xs)) / max(1, src_w)
        score += min(x_spread * 25.0, 18.0)
        if left_frac > 0.6 and right_frac > 0.3:
            score -= 20.0
    return score


def _optimize_transform_offset(
    annotations: list[dict],
    src_w: int,
    src_h: int,
    sx: float,
    sy: float,
) -> tuple[float, float, float]:
    """在固定缩放下搜索 ox/oy，缓解 pillarbox / 内容区偏移。"""
    best_ox, best_oy, best_score = 0.0, 0.0, _score_transform(annotations, src_w, src_h, sx, sy, 0.0, 0.0)
    if not annotations:
        return best_ox, best_oy, best_score
    xs = [_float(a.get("x"), 0) + _float(a.get("width", a.get("w")), 20) / 2 for a in annotations if isinstance(a, dict)]
    ys = [_float(a.get("y"), 0) + _float(a.get("height", a.get("h")), 20) / 2 for a in annotations if isinstance(a, dict)]
    if not xs:
        return best_ox, best_oy, best_score
    cx_obs = sum(xs) / len(xs)
    cy_obs = sum(ys) / len(ys)
    target_cx = src_w / 2.0
    target_cy = src_h / 2.0
    ox_hint = target_cx - sx * cx_obs
    oy_hint = target_cy - sy * cy_obs
    span_x = max(8, int(src_w * 0.35))
    span_y = max(8, int(src_h * 0.25))
    steps = 9
    for ix in range(steps):
        for iy in range(steps):
            ox = ox_hint + (ix / max(1, steps - 1) - 0.5) * 2 * span_x
            oy = oy_hint + (iy / max(1, steps - 1) - 0.5) * 2 * span_y
            sc = _score_transform(annotations, src_w, src_h, sx, sy, ox, oy)
            if sc > best_score:
                best_score = sc
                best_ox, best_oy = ox, oy
    return best_ox, best_oy, best_score


def assess_transform_quality(
    annotations: list[dict],
    src_w: int,
    src_h: int,
    sx: float,
    sy: float,
    ox: float,
    oy: float,
) -> dict:
    """评估变换后是否仍像「边距偏移」。"""
    left, right, in_bounds = _margin_fractions(annotations, src_w, src_h, sx, sy, ox, oy)
    suspicious = (left + right) >= 0.45 or in_bounds < 0.6
    return {
        "margin_left_frac": round(left, 3),
        "margin_right_frac": round(right, 3),
        "in_bounds_frac": round(in_bounds, 3),
        "likely_offset_error": suspicious,
    }


def pick_best_auto_transform(
    annotations: list[dict],
    src_w: int,
    src_h: int,
    dim_info: dict | None,
) -> tuple[str, float, float, float, float, int, int] | None:
    """无 calibration_observations 时，在多种业界常见坐标系中选最优线性映射。"""
    if not annotations or src_w <= 0 or src_h <= 0:
        return None
    meta = dim_info or {}
    ow, oh = int(src_w), int(src_h)
    vw = int(meta.get("vision_width") or ow)
    vh = int(meta.get("vision_height") or oh)
    mw = int(meta.get("model_view_width") or vw)
    mh = int(meta.get("model_view_height") or vh)

    candidates: list[tuple[str, int, int]] = [
        ("norm1000", 1000, 1000),
        ("model_view", mw, mh),
        ("vision_encode", vw, vh),
        ("original", ow, oh),
    ]
    best: tuple[str, float, float, float, float, int, int] | None = None
    best_score = -1e18
    for name, rw, rh in candidates:
        if rw <= 0 or rh <= 0:
            continue
        sx = ow / float(rw)
        sy = oh / float(rh)
        ox, oy, score = _optimize_transform_offset(annotations, ow, oh, sx, sy)
        if name == "original":
            score += 2.0
        if name == "norm1000":
            max_x, max_y = _annotation_max_extent(annotations)
            if max_x <= 1005 and max_y <= 1005:
                score += 8.0
        if score > best_score:
            best_score = score
            best = (name, sx, sy, ox, oy, rw, rh)
    return best


def _annotation_max_extent(annotations: list[dict]) -> tuple[float, float]:
    max_x = 0.0
    max_y = 0.0
    for ann in annotations:
        if not isinstance(ann, dict):
            continue
        x = _float(ann.get("x"), 0)
        y = _float(ann.get("y"), 0)
        w = _float(ann.get("width", ann.get("w")), 0)
        h = _float(ann.get("height", ann.get("h")), 0)
        max_x = max(max_x, x + w, _float(ann.get("x2"), 0))
        max_y = max(max_y, y + h, _float(ann.get("y2"), 0))
    return max_x, max_y


def resolve_annotation_transform(
    annotations: list[dict],
    src_w: int,
    src_h: int,
    dim_info: dict | None,
    cal_reference: list[dict],
    cal_observations: list[dict] | None,
    *,
    coordinate_space: Any = None,
    reference_width: int | None = None,
    reference_height: int | None = None,
    offset_x: float = 0,
    offset_y: float = 0,
    use_original: bool = False,
) -> tuple[list[dict] | None, str, dict | None]:
    """统一坐标换算：校准观测 > 显式坐标系 > reference > 自动推断。"""
    if not annotations or src_w <= 0 or src_h <= 0:
        return annotations, "", None

    if cal_observations:
        cal = compute_calibration_transform(cal_reference, cal_observations)
        if not cal:
            return None, "", {"error": "calibration_observations 有效点不足"}
        sx, sy, ox, oy, matched = cal
        out = apply_calibration_transform_to_annotations(annotations, sx, sy, ox, oy)
        note = (
            f"校准拟合（{matched}点）sx={sx:.4f} sy={sy:.4f} ox={ox:.1f} oy={oy:.1f}"
        )
        return out, note, {"method": "calibration", "sx": sx, "sy": sy, "ox": ox, "oy": oy, "points": matched}

    space_res = apply_normalized_space(annotations, src_w, src_h, coordinate_space)
    if space_res:
        out, sx, sy = space_res
        return out, f"显式坐标系 {coordinate_space} → 原图 {src_w}×{src_h}", {
            "method": "explicit_space", "space": str(coordinate_space), "sx": sx, "sy": sy,
        }

    if use_original:
        return list(annotations), "use_original_coordinates（未换算）", {"method": "original"}

    meta = dim_info or {}
    vw = meta.get("vision_width")
    vh = meta.get("vision_height")
    mw = meta.get("model_view_width") or vw
    mh = meta.get("model_view_height") or vh
    ref_w, ref_h = reference_width, reference_height
    if ref_w and ref_h and vw and vh and int(ref_w) == int(vw) and int(ref_h) == int(vh):
        if mw and mh and (int(mw) != int(vw) or int(mh) != int(vh)):
            ref_w, ref_h = int(mw), int(mh)

    if ref_w and ref_h and ref_w > 0 and ref_h > 0:
        if int(ref_w) != int(src_w) or int(ref_h) != int(src_h) or offset_x or offset_y:
            from services.image_edit import scale_annotations
            sx = src_w / float(ref_w)
            sy = src_h / float(ref_h)
            out = scale_annotations(annotations, sx, sy, offset_x=offset_x, offset_y=offset_y)
            return out, f"reference {ref_w}×{ref_h} → 原图 {src_w}×{src_h}", {
                "method": "reference", "ref_w": ref_w, "ref_h": ref_h,
            }

    auto = pick_best_auto_transform(annotations, src_w, src_h, meta)
    if auto:
        name, sx, sy, ox, oy, rw, rh = auto
        out = apply_calibration_transform_to_annotations(annotations, sx, sy, ox, oy)
        quality = assess_transform_quality(annotations, src_w, src_h, sx, sy, ox, oy)
        note = f"自动坐标系 {name}（{rw}×{rh}）→ 原图 {src_w}×{src_h}"
        if ox or oy:
            note += f" ox={ox:.1f} oy={oy:.1f}"
        meta_out: dict = {
            "method": "auto",
            "space": name,
            "sx": sx,
            "sy": sy,
            "ox": ox,
            "oy": oy,
            "ref_w": rw,
            "ref_h": rh,
            **quality,
        }
        if quality.get("likely_offset_error"):
            meta_out["recommend_calibration_probe"] = True
        return out, note, meta_out

    return list(annotations), "未换算（无法推断坐标系）", {"method": "none"}
