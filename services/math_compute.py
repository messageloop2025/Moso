"""AI 通用数学计算：标准库 math、NumPy 数值、SymPy 符号，含数据集批量运算。"""
from __future__ import annotations

import ast
import json
import math
import re
import statistics
from typing import Any

MAX_BATCH_ROWS = 10_000
MAX_BATCH_COLS = 64
MAX_EXPRESSION_LEN = 4_000
MAX_NUMBERS = 50_000

_MATH_NAMES = {name: getattr(math, name) for name in dir(math) if not name.startswith("_")}
_MATH_NAMES.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow})

_LINEAR_UNIT_TO_BASE = {
    "mm": ("length", 0.001), "cm": ("length", 0.01), "m": ("length", 1.0), "km": ("length", 1000.0),
    "in": ("length", 0.0254), "ft": ("length", 0.3048), "yd": ("length", 0.9144), "mi": ("length", 1609.344),
    "mg": ("mass", 0.000001), "g": ("mass", 0.001), "kg": ("mass", 1.0), "t": ("mass", 1000.0),
    "oz": ("mass", 0.028349523125), "lb": ("mass", 0.45359237),
    "n": ("force", 1.0), "kn": ("force", 1000.0), "mn": ("force", 1000000.0), "lbf": ("force", 4.4482216152605),
    "ms": ("time", 0.001), "s": ("time", 1.0), "min": ("time", 60.0), "h": ("time", 3600.0), "day": ("time", 86400.0),
}

_ALLOWED_AST_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Load, ast.Attribute,
    ast.Constant, ast.Tuple, ast.List, ast.Subscript, ast.Slice,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.UAdd, ast.USub,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.BoolOp, ast.And, ast.Or, ast.Not,
    ast.IfExp,
)


def _require_numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("NumPy 未安装，请 pip install numpy") from exc
    return np


def _require_sympy():
    try:
        import sympy as sp
    except ImportError as exc:
        raise RuntimeError("SymPy 未安装，请 pip install sympy") from exc
    return sp


def _numpy_namespace(np) -> dict[str, Any]:
    allowed = (
        "array", "asarray", "mean", "median", "std", "var", "sum", "min", "max", "abs",
        "sqrt", "exp", "log", "log10", "sin", "cos", "tan", "arcsin", "arccos", "arctan",
        "sinh", "cosh", "tanh", "power", "dot", "matmul", "linalg", "clip", "where",
        "round", "floor", "ceil", "percentile", "cumsum", "cumprod", "argmin", "argmax",
        "histogram", "polyfit", "polyval", "linspace", "arange", "zeros", "ones", "full",
        "vstack", "hstack", "column_stack", "transpose", "T",
    )
    ns = {"np": np, "pi": np.pi, "e": np.e}
    for name in allowed:
        if hasattr(np, name):
            ns[name] = getattr(np, name)
    return ns


def _validate_expression(expression: str) -> str:
    expr = (expression or "").strip()
    if not expr:
        raise ValueError("expression 不能为空")
    if len(expr) > MAX_EXPRESSION_LEN:
        raise ValueError(f"expression 过长（>{MAX_EXPRESSION_LEN} 字符）")
    return expr


def _validate_ast(tree: ast.AST, *, allowed_names: set[str], allow_attr: bool = False) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST_NODES):
            raise ValueError(f"不支持的表达式节点: {type(node).__name__}")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in allowed_names:
                    raise ValueError(f"不允许调用: {node.func.id}")
            elif isinstance(node.func, ast.Attribute):
                if not allow_attr:
                    raise ValueError("不允许属性调用（如 np.xxx 请用 batch_vector / numpy 模式）")
                base = node.func.value
                if not (isinstance(base, ast.Name) and base.id in allowed_names):
                    raise ValueError("不允许的属性调用")
            else:
                raise ValueError("不允许的调用形式")
            if node.keywords:
                raise ValueError("不支持关键字参数")
        if isinstance(node, ast.Name) and node.id not in allowed_names:
            raise ValueError(f"未知名称: {node.id}")
        if isinstance(node, ast.Attribute) and not allow_attr:
            raise ValueError("不允许属性访问")


def _safe_eval_expression(expression: str, namespace: dict[str, Any], *, allowed_names: set[str], allow_attr: bool = False) -> Any:
    expr = _validate_expression(expression)
    tree = ast.parse(expr, mode="eval")
    _validate_ast(tree, allowed_names=allowed_names, allow_attr=allow_attr)
    return eval(compile(tree, "<math_calculate>", "eval"), {"__builtins__": {}}, namespace)


def safe_math_eval(expression: str) -> Any:
    return _safe_eval_expression(expression, dict(_MATH_NAMES), allowed_names=set(_MATH_NAMES.keys()))


def convert_unit(value: float, from_unit: str, to_unit: str) -> float:
    f = (from_unit or "").strip().lower()
    t = (to_unit or "").strip().lower()
    if f in ("c", "celsius", "°c") and t in ("f", "fahrenheit", "°f"):
        return value * 9 / 5 + 32
    if f in ("f", "fahrenheit", "°f") and t in ("c", "celsius", "°c"):
        return (value - 32) * 5 / 9
    if f in ("c", "celsius", "°c") and t in ("k", "kelvin"):
        return value + 273.15
    if f in ("k", "kelvin") and t in ("c", "celsius", "°c"):
        return value - 273.15
    if f in ("f", "fahrenheit", "°f") and t in ("k", "kelvin"):
        return (value - 32) * 5 / 9 + 273.15
    if f in ("k", "kelvin") and t in ("f", "fahrenheit", "°f"):
        return (value - 273.15) * 9 / 5 + 32
    if f not in _LINEAR_UNIT_TO_BASE or t not in _LINEAR_UNIT_TO_BASE:
        raise ValueError("暂不支持该单位换算")
    cat_f, factor_f = _LINEAR_UNIT_TO_BASE[f]
    cat_t, factor_t = _LINEAR_UNIT_TO_BASE[t]
    if cat_f != cat_t:
        raise ValueError(f"单位维度不一致: {from_unit} -> {to_unit}")
    return value * factor_f / factor_t


def _coerce_number(val: Any) -> float | None:
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return float(val)
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).strip())
    except (TypeError, ValueError):
        return None


def _normalize_dataset(raw: Any) -> tuple[str, list[dict] | dict[str, list]]:
    """返回 (mode, data)；mode=rows|columns。"""
    if raw is None:
        raise ValueError("dataset 不能为空")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, list):
        if not raw:
            raise ValueError("dataset 行数组不能为空")
        if len(raw) > MAX_BATCH_ROWS:
            raise ValueError(f"dataset 行数超过上限 {MAX_BATCH_ROWS}")
        rows = [dict(x) if isinstance(x, dict) else {"value": x} for x in raw]
        return "rows", rows
    if isinstance(raw, dict):
        cols: dict[str, list] = {}
        for k, v in raw.items():
            key = str(k).strip()
            if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                raise ValueError(f"列名非法: {k!r}（需字母/数字/下划线且不以数字开头）")
            if not isinstance(v, list):
                raise ValueError(f"列 {key} 必须是数组")
            cols[key] = v
        if not cols:
            raise ValueError("dataset 列字典不能为空")
        if len(cols) > MAX_BATCH_COLS:
            raise ValueError(f"列数超过上限 {MAX_BATCH_COLS}")
        n = len(next(iter(cols.values())))
        for key, arr in cols.items():
            if len(arr) != n:
                raise ValueError(f"列 {key} 长度 {len(arr)} 与其它列不一致")
            if len(arr) > MAX_BATCH_ROWS:
                raise ValueError(f"列长度超过上限 {MAX_BATCH_ROWS}")
        return "columns", cols
    raise ValueError("dataset 须为行对象数组，或列名字典 {col: [values...]}")


def _jsonable(val: Any) -> Any:
    if val is None or isinstance(val, (bool, str)):
        return val
    if isinstance(val, (int, float)):
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return str(val)
        return val
    np = None
    try:
        np = _require_numpy()
    except RuntimeError:
        pass
    if np is not None and isinstance(val, np.ndarray):
        if val.ndim == 0:
            return _jsonable(val.item())
        return [_jsonable(x) for x in val.tolist()]
    if isinstance(val, (list, tuple)):
        return [_jsonable(x) for x in val]
    if isinstance(val, dict):
        return {str(k): _jsonable(v) for k, v in val.items()}
    return str(val)


def op_eval(arguments: dict) -> dict:
    expr = str(arguments.get("expression") or "")
    result = safe_math_eval(expr)
    return {"success": True, "operation": "eval", "expression": expr, "result": _jsonable(result)}


def op_stats(arguments: dict) -> dict:
    nums = [_coerce_number(x) for x in (arguments.get("numbers") or [])]
    nums = [x for x in nums if x is not None]
    if not nums:
        raise ValueError("numbers 不能为空")
    if len(nums) > MAX_NUMBERS:
        raise ValueError(f"numbers 超过上限 {MAX_NUMBERS}")
    np = _require_numpy()
    arr = np.asarray(nums, dtype=float)
    payload = {
        "count": int(arr.size),
        "sum": float(np.sum(arr)),
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "std": float(np.std(arr, ddof=1)) if arr.size >= 2 else 0.0,
        "var": float(np.var(arr, ddof=1)) if arr.size >= 2 else 0.0,
        "pstdev": float(statistics.pstdev(nums)),
        "percentile_25": float(np.percentile(arr, 25)),
        "percentile_75": float(np.percentile(arr, 75)),
    }
    return {"success": True, "operation": "stats", "stats": payload}


def op_unit_convert(arguments: dict) -> dict:
    value = float(arguments.get("value"))
    fu = str(arguments.get("from_unit") or "")
    tu = str(arguments.get("to_unit") or "")
    result = convert_unit(value, fu, tu)
    return {
        "success": True,
        "operation": "unit_convert",
        "value": value,
        "from_unit": fu,
        "to_unit": tu,
        "result": result,
    }


def op_numpy(arguments: dict) -> dict:
    np = _require_numpy()
    expr = _validate_expression(str(arguments.get("expression") or ""))
    ns = _numpy_namespace(np)
    numbers = arguments.get("numbers")
    if numbers is not None:
        if not isinstance(numbers, list):
            raise ValueError("numbers 必须是数组")
        if len(numbers) > MAX_NUMBERS:
            raise ValueError(f"numbers 超过上限 {MAX_NUMBERS}")
        ns["arr"] = np.asarray([_coerce_number(x) or 0.0 for x in numbers], dtype=float)
        ns["x"] = ns["arr"]
    allowed = set(ns.keys())
    result = _safe_eval_expression(expr, ns, allowed_names=allowed, allow_attr=True)
    return {
        "success": True,
        "operation": "numpy",
        "expression": expr,
        "result": _jsonable(result),
    }


def op_batch(arguments: dict) -> dict:
    """数据集 + 表达式 → 批量结果。支持逐行 (batch) 与列向量 (batch_vector)。"""
    np = _require_numpy()
    mode_hint = (arguments.get("mode") or "auto").strip().lower()
    dataset_raw = arguments.get("dataset")
    if dataset_raw is None and arguments.get("data") is not None:
        dataset_raw = arguments.get("data")
    struct_mode, data = _normalize_dataset(dataset_raw)
    if mode_hint == "vector" or (mode_hint == "auto" and struct_mode == "columns"):
        return _batch_vector(np, data, arguments)
    if struct_mode == "columns":
        col_data: dict[str, list] = data
        rows = [
            {k: (col_data[k][i] if i < len(col_data[k]) else None) for k in col_data}
            for i in range(len(next(iter(col_data.values()))))
        ]
    else:
        rows = data
    return _batch_rows(np, rows, arguments)


def _batch_rows(np, rows: list[dict], arguments: dict) -> dict:
    expr = _validate_expression(str(arguments.get("expression") or arguments.get("algorithm") or ""))
    out_col = (arguments.get("output_column") or arguments.get("result_column") or "result").strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", out_col):
        raise ValueError("output_column 命名非法")
    allowed = set(_MATH_NAMES.keys())
    results: list[Any] = []
    output_rows: list[dict] = []
    for i, row in enumerate(rows):
        ns = dict(_MATH_NAMES)
        for k, v in row.items():
            key = str(k)
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                continue
            num = _coerce_number(v)
            if num is not None:
                ns[key] = num
            elif isinstance(v, str):
                ns[key] = v
        allowed_row = set(ns.keys())
        try:
            val = _safe_eval_expression(expr, ns, allowed_names=allowed_row)
        except Exception as exc:
            raise ValueError(f"第 {i} 行计算失败: {exc}") from exc
        results.append(_jsonable(val))
        merged = dict(row)
        merged[out_col] = _jsonable(val)
        output_rows.append(merged)
    return {
        "success": True,
        "operation": "batch",
        "mode": "rows",
        "expression": expr,
        "row_count": len(results),
        "output_column": out_col,
        "results": results,
        "dataset_with_results": output_rows,
    }


def _batch_vector(np, columns: dict[str, list], arguments: dict) -> dict:
    expr = _validate_expression(str(arguments.get("expression") or arguments.get("algorithm") or ""))
    out_col = (arguments.get("output_column") or arguments.get("result_column") or "result").strip()
    ns = _numpy_namespace(np)
    col_arrays: dict[str, Any] = {}
    for key, values in columns.items():
        nums = []
        for v in values:
            n = _coerce_number(v)
            if n is None and v is not None and v != "":
                raise ValueError(f"列 {key} 含非数值: {v!r}")
            nums.append(n if n is not None else np.nan)
        col_arrays[key] = np.asarray(nums, dtype=float)
        ns[key] = col_arrays[key]
    allowed = set(ns.keys())
    try:
        result = _safe_eval_expression(expr, ns, allowed_names=allowed, allow_attr=True)
    except Exception as exc:
        raise ValueError(f"向量批量计算失败: {exc}") from exc
    result_list = _jsonable(result)
    if not isinstance(result_list, list):
        result_list = [result_list]
    out_dataset = dict(columns)
    out_dataset[out_col] = result_list
    return {
        "success": True,
        "operation": "batch",
        "mode": "vector",
        "expression": expr,
        "row_count": len(result_list),
        "output_column": out_col,
        "results": result_list,
        "dataset_with_results": out_dataset,
    }


def op_symbolic(arguments: dict) -> dict:
    sp = _require_sympy()
    sub = (arguments.get("symbolic_op") or arguments.get("sub_operation") or "simplify").strip().lower()
    expr_s = str(arguments.get("expression") or "").strip()
    if not expr_s:
        raise ValueError("expression 不能为空")
    var_names = arguments.get("variables") or arguments.get("variable")
    if isinstance(var_names, str):
        var_names = [v.strip() for v in var_names.replace(",", " ").split() if v.strip()]
    elif not var_names:
        var_names = []
    locals_map = {}
    for v in var_names:
        locals_map[str(v)] = sp.symbols(str(v))
    try:
        expr = sp.sympify(expr_s, locals=locals_map)
    except Exception as exc:
        raise ValueError(f"无法解析表达式: {exc}") from exc

    if sub == "simplify":
        out = sp.simplify(expr)
    elif sub == "expand":
        out = sp.expand(expr)
    elif sub == "factor":
        out = sp.factor(expr)
    elif sub == "diff":
        wrt = arguments.get("wrt") or (var_names[0] if var_names else None)
        if not wrt:
            raise ValueError("diff 需要 wrt 或 variables")
        out = sp.diff(expr, sp.symbols(str(wrt)))
    elif sub == "integrate":
        wrt = arguments.get("wrt") or (var_names[0] if var_names else None)
        if not wrt:
            raise ValueError("integrate 需要 wrt 或 variables")
        out = sp.integrate(expr, sp.symbols(str(wrt)))
    elif sub == "solve":
        if not var_names:
            raise ValueError("solve 需要 variables")
        syms = [sp.symbols(str(v)) for v in var_names]
        if isinstance(expr, sp.Equality):
            eq = expr
        else:
            eq = sp.Eq(expr, 0)
        sol = sp.solve(eq, syms, dict=True)
        out = sol
    elif sub == "limit":
        wrt = arguments.get("wrt") or (var_names[0] if var_names else "x")
        point = arguments.get("point")
        if point is None:
            raise ValueError("limit 需要 point")
        sym = sp.symbols(str(wrt))
        out = sp.limit(expr, sym, sp.sympify(str(point)))
    elif sub == "subs":
        subs_map = arguments.get("substitutions") or arguments.get("subs") or {}
        if not isinstance(subs_map, dict) or not subs_map:
            raise ValueError("subs 需要 substitutions 字典")
        rep = {sp.sympify(k): sp.sympify(v) for k, v in subs_map.items()}
        out = expr.subs(rep)
    else:
        raise ValueError(f"不支持的 symbolic_op: {sub}")

    return {
        "success": True,
        "operation": "symbolic",
        "symbolic_op": sub,
        "expression": expr_s,
        "result": _jsonable(out),
        "result_latex": sp.latex(out) if hasattr(sp, "latex") else str(out),
    }


def run_math_calculate(arguments: dict | None) -> dict:
    args = arguments or {}
    op = (args.get("operation") or "").strip().lower()
    if op == "eval":
        return op_eval(args)
    if op == "stats":
        return op_stats(args)
    if op == "unit_convert":
        return op_unit_convert(args)
    if op == "numpy":
        return op_numpy(args)
    if op in ("batch", "batch_vector"):
        if op == "batch_vector":
            args = dict(args)
            args["mode"] = "vector"
        return op_batch(args)
    if op == "symbolic":
        return op_symbolic(args)
    raise ValueError(
        "operation 不支持；可用: eval, stats, unit_convert, numpy, batch, batch_vector, symbolic"
    )


MATH_CALCULATE_USAGE_HINT = """
**math_calculate 用法摘要**
- `eval`：标量表达式（sqrt, sin, log…）
- `stats`：数组统计（NumPy）
- `unit_convert`：单位换算
- `numpy`：数组/向量表达式，可用 `numbers` + `np.mean(arr)` 等
- `batch`：**数据集 + 算法**批量计算
  - `dataset` 为行数组 `[{x:1,y:2},…]` 或列字典 `{x:[1,2], y:[3,4]}`
  - `expression` / `algorithm`：公式，引用列名/字段名，如 `x*y+bonus`、`sqrt(x**2+y**2)`
  - `output_column`：结果列名（默认 result）
  - 列字典时自动 **vector** 向量化；行数组时 **逐行** 计算
- `symbolic`：SymPy 符号运算（simplify/expand/factor/diff/integrate/solve/limit/subs）
""".strip()
