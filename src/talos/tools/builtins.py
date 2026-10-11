from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from datetime import UTC, datetime

_OPERATORS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def calculator(expression: str) -> float:
    """Evaluate a bounded arithmetic expression without executing code."""
    if len(expression) > 200:
        raise ValueError("Expression is too long")

    try:
        return _evaluate(ast.parse(expression, mode="eval").body)
    except (SyntaxError, OverflowError, ZeroDivisionError) as exc:
        raise ValueError("Invalid arithmetic expression") from exc


def _evaluate(node: ast.AST, depth: int = 0) -> float:
    if depth > 12:
        raise ValueError("Expression is too complex")
    numeric_value = _numeric_value(node)
    if numeric_value is not None:
        result = numeric_value
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate(node.operand, depth + 1)
        result = value if isinstance(node.op, ast.UAdd) else -value
    elif isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        result = _OPERATORS[type(node.op)](
            _evaluate(node.left, depth + 1), _evaluate(node.right, depth + 1)
        )
    else:
        raise ValueError("Unsupported arithmetic expression")
    if not -1e12 <= result <= 1e12:
        raise ValueError("Arithmetic result is outside the supported range")
    return result


def _numeric_value(node: ast.AST) -> float | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    return None


def current_utc_time() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(UTC).isoformat()
