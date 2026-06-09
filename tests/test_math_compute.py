"""math_compute 单元测试。"""

from __future__ import annotations

import pytest

from services.math_compute import run_math_calculate


def test_eval_scalar():
    out = run_math_calculate({"operation": "eval", "expression": "sqrt(16) + 2"})
    assert out["success"] is True
    assert out["result"] == 6.0


def test_batch_rows():
    out = run_math_calculate({
        "operation": "batch",
        "dataset": [
            {"x": 1, "y": 2, "bonus": 0.5},
            {"x": 3, "y": 4, "bonus": 1.0},
        ],
        "expression": "x * y + bonus",
        "output_column": "total",
    })
    assert out["success"] is True
    assert out["results"] == [2.5, 13.0]
    assert out["dataset_with_results"][0]["total"] == 2.5


def test_batch_vector_columns():
    out = run_math_calculate({
        "operation": "batch",
        "dataset": {"x": [1, 2, 3], "y": [10, 20, 30]},
        "expression": "x + y",
    })
    assert out["success"] is True
    assert out["mode"] == "vector"
    assert out["results"] == [11, 22, 33]


def test_symbolic_simplify():
    sympy = pytest.importorskip("sympy")
    out = run_math_calculate({
        "operation": "symbolic",
        "symbolic_op": "simplify",
        "expression": "(x + 1)**2 - (x**2 + 2*x + 1)",
        "variables": ["x"],
    })
    assert out["success"] is True
    assert str(out["result"]) in ("0", "0.0") or out["result"] == 0


def test_numpy_mean():
    np = pytest.importorskip("numpy")
    out = run_math_calculate({
        "operation": "numpy",
        "numbers": [1, 2, 3, 4],
        "expression": "np.mean(arr)",
    })
    assert out["success"] is True
    assert abs(out["result"] - 2.5) < 1e-9
