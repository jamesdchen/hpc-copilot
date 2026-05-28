"""Bounded-window deque matcher.

Detects ``buf = deque(maxlen=W)`` (or ``collections.deque``) constructed
in the function body, plus a ``buf.append(...)`` inside the loop. The
halo equals ``W`` — a literal integer or a bare parameter name (the
halo-expression language).

The ``_render_halo_token`` helper is shared with the pandas-rolling
matcher, which lives next door under ``matchers/pandas_rolling.py``.

Extracted from the original 839-line :mod:`axis_matcher` so each
pattern has its own home.
"""

from __future__ import annotations

import ast

from hpc_agent.experiment_kit.axis_matcher._ast_utils import _called_name

__all__ = ["_match_bounded_window_deque", "_render_halo_token"]


def _match_bounded_window_deque(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    loop: ast.For | ast.While,
) -> tuple[str, str] | None:
    """Detect ``buf = deque(maxlen=W)`` in func + ``buf.append(...)`` in loop.

    Returns ``(halo_expr, evidence)`` where halo_expr is ``W`` (literal
    integer or bare parameter name).
    """
    # Find deque(maxlen=W) constructions in the function body (outside the loop).
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        called = _called_name(node.value.func)
        if called not in {"deque", "collections.deque"}:
            continue
        # Extract maxlen keyword.
        maxlen_expr: ast.expr | None = None
        for kw in node.value.keywords:
            if kw.arg == "maxlen":
                maxlen_expr = kw.value
                break
        if maxlen_expr is None:
            continue
        halo_expr = _render_halo_token(maxlen_expr)
        if halo_expr is None:
            continue
        # Identify the bound name.
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        buf_name = node.targets[0].id
        # Confirm the loop body appends to buf_name.
        for sub in ast.walk(loop):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "append"
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == buf_name
            ):
                evidence = (
                    f"loop appends to bounded-window deque {buf_name!r} "
                    f"(maxlen={halo_expr}; halo = {halo_expr})"
                )
                return halo_expr, evidence
    return None


def _render_halo_token(node: ast.expr) -> str | None:
    """Render a halo expression token (literal int or bare param name)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return str(node.value)
    if isinstance(node, ast.Name):
        return node.id
    return None
