"""(De)serialize a classified :data:`DataAxis` to / from the ``axes.yaml``
``executors`` block.

A :data:`~hpc_agent.incorporation.template.axis.DataAxis` is a live object — and for
:class:`~hpc_agent.incorporation.template.axis.BoundedHalo` it carries a *callable*
``halo_fn``. To persist a classification across submits it must
round-trip through the plain-data ``executors.<run>.data_axis`` shape
(see :mod:`hpc_agent._wire.fixtures.axes`):

.. code-block:: yaml

    data_axis:
      kind: bounded_halo
      halo: { expr: "train_window * 48" }

:func:`data_axis_from_config` turns that dict into a live ``DataAxis``;
:func:`config_from_data_axis` is the inverse used when writing.

**Halo expressions are never** ``eval()``\\ **ed.** The halo ``expr`` is
walked with a restricted :mod:`ast` interpreter (:func:`eval_halo_expr`)
that admits only: bare names (resolved from the task-params dict),
numeric literals, the ``+`` ``-`` ``*`` ``//`` operators, and calls to
``min`` / ``max``. Anything else — an attribute access, an
``__import__``, a comprehension — raises :class:`HaloExprError`.

This module is **submit-side** — it is *not* inlined into executors.
It uses only the stdlib (:mod:`ast`) and :mod:`hpc_agent.incorporation.template.axis`
(itself stdlib-only).
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from typing import Any

from hpc_agent.incorporation.template.axis import (
    MOMENTS,
    SUM,
    Associative,
    BoundedHalo,
    DataAxis,
    Independent,
    Sequential,
)

__all__ = [
    "HaloExprError",
    "config_from_data_axis",
    "data_axis_from_config",
    "eval_halo_expr",
]


class HaloExprError(ValueError):
    """A halo expression is malformed, unsafe, or failed to evaluate."""


# BinOp operators the restricted interpreter admits. Division is
# ``FloorDiv`` only — a halo is an integer row count, and ``//`` keeps
# the result integral without a surprising float.
_ALLOWED_BINOPS: tuple[type[ast.operator], ...] = (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv)

#: The only callables a halo expression may invoke.
_ALLOWED_CALLS = frozenset({"min", "max"})


def _eval_node(node: ast.AST, params: dict[str, Any]) -> float:
    """Recursively evaluate one restricted-AST node. Raises on anything unsafe."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, params)

    if isinstance(node, ast.Constant):
        # bool is an int subclass; exclude it so `True * 48` can't sneak in.
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise HaloExprError(f"halo expr literal must be numeric, got {node.value!r}")
        return node.value

    if isinstance(node, ast.Name):
        if node.id not in params:
            raise HaloExprError(
                f"halo expr references {node.id!r}, which is not a run() parameter "
                f"(available: {sorted(params)})"
            )
        value = params[node.id]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HaloExprError(f"halo expr parameter {node.id!r} is not numeric: {value!r}")
        return float(value)

    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            raise HaloExprError(
                f"halo expr operator {type(node.op).__name__} is not allowed (only + - * // are)"
            )
        left = _eval_node(node.left, params)
        right = _eval_node(node.right, params)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        # FloorDiv.
        if right == 0:
            raise HaloExprError("halo expr divides by zero")
        return left // right

    if isinstance(node, ast.Call):
        func = node.func
        if not isinstance(func, ast.Name) or func.id not in _ALLOWED_CALLS:
            raise HaloExprError("halo expr may only call min() or max()")
        if node.keywords:
            raise HaloExprError(f"halo expr {func.id}() takes no keyword arguments")
        if not node.args:
            raise HaloExprError(f"halo expr {func.id}() needs at least one argument")
        args = [_eval_node(a, params) for a in node.args]
        return min(args) if func.id == "min" else max(args)

    raise HaloExprError(f"halo expr node {type(node).__name__} is not allowed")


def eval_halo_expr(expr: str, params: dict[str, Any]) -> int:
    """Evaluate a halo *expr* against *params* and return an ``int`` row count.

    *params* is one sweep point — a mapping of ``run()`` parameter names
    to values. The expression's bare names are resolved from it. Raises
    :class:`HaloExprError` for an unparseable, unsafe, or non-evaluable
    expression.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise HaloExprError(f"halo expr is not a valid Python expression: {expr!r}") from exc
    return int(_eval_node(tree, params))


def _validate_halo_expr(expr: str) -> None:
    """Parse + structurally validate *expr* with an empty params dict.

    Catches a syntax error or a disallowed node at classification time —
    before any sweep point exists — so a bad expression never reaches the
    cluster. A bare-name reference does *not* fail here (no params yet);
    it surfaces at :func:`eval_halo_expr` time instead.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise HaloExprError(f"halo expr is not a valid Python expression: {expr!r}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Load)):
            continue
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise HaloExprError(f"halo expr literal must be numeric, got {node.value!r}")
            continue
        if isinstance(node, ast.Name):
            continue
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BINOPS):
                raise HaloExprError(f"halo expr operator {type(node.op).__name__} is not allowed")
            continue
        if isinstance(node, _ALLOWED_BINOPS):
            continue
        if isinstance(node, ast.Call):
            func = node.func
            if not isinstance(func, ast.Name) or func.id not in _ALLOWED_CALLS:
                raise HaloExprError("halo expr may only call min() or max()")
            continue
        raise HaloExprError(f"halo expr node {type(node).__name__} is not allowed")


def _make_halo_fn(expr: str) -> Callable[[dict[str, Any]], int]:
    """Build a ``halo_fn`` closure for *expr*, with the source carried back.

    The returned callable has a ``halo_expr`` attribute holding *expr*
    verbatim, so :func:`config_from_data_axis` can recover the source for
    a clean round-trip.
    """
    _validate_halo_expr(expr)

    def halo_fn(params: dict[str, Any]) -> int:
        return eval_halo_expr(expr, params)

    halo_fn.halo_expr = expr  # type: ignore[attr-defined]
    return halo_fn


def data_axis_from_config(cfg: dict[str, Any]) -> DataAxis:
    """Turn a serialized ``data_axis`` block into a live :data:`DataAxis`.

    *cfg* is the ``executors.<run>.data_axis`` mapping —
    ``{kind, halo?, monoid?}``. For ``bounded_halo`` the ``halo.expr``
    is compiled into a safe :func:`eval_halo_expr`-backed ``halo_fn``.
    """
    kind = cfg.get("kind")
    if kind == "independent":
        return Independent()
    if kind == "sequential":
        return Sequential()
    if kind == "associative":
        monoid = cfg.get("monoid") or "moments"
        if monoid not in ("sum", "moments"):
            raise ValueError(f"unknown associative monoid: {monoid!r}")
        return Associative(SUM if monoid == "sum" else MOMENTS)
    if kind == "bounded_halo":
        halo = cfg.get("halo") or {}
        expr = halo.get("expr")
        if not expr:
            raise HaloExprError("data_axis kind 'bounded_halo' requires halo.expr")
        return BoundedHalo(_make_halo_fn(expr))
    raise ValueError(f"unknown data_axis kind: {kind!r}")


def config_from_data_axis(axis: DataAxis) -> dict[str, Any]:
    """Serialize a live :data:`DataAxis` to the ``data_axis`` block shape.

    The inverse of :func:`data_axis_from_config`. A
    :class:`~hpc_agent.incorporation.template.axis.BoundedHalo` only round-trips if its
    ``halo_fn`` was produced by :func:`data_axis_from_config` (which
    carries the source ``expr`` on the closure); a hand-built
    ``BoundedHalo(lambda p: ...)`` cannot be serialized and raises.
    """
    if isinstance(axis, Independent):
        return {"kind": "independent"}
    if isinstance(axis, Sequential):
        return {"kind": "sequential"}
    if isinstance(axis, Associative):
        if axis.monoid is SUM:
            monoid = "sum"
        elif axis.monoid is MOMENTS:
            monoid = "moments"
        else:
            raise ValueError(
                "config_from_data_axis: Associative carries a custom Monoid that is "
                "neither SUM nor MOMENTS — only the two built-ins serialize"
            )
        return {"kind": "associative", "monoid": monoid}
    if isinstance(axis, BoundedHalo):
        expr = getattr(axis.halo_fn, "halo_expr", None)
        if not isinstance(expr, str):
            raise ValueError(
                "config_from_data_axis: BoundedHalo.halo_fn carries no halo_expr — "
                "build the axis via data_axis_from_config so the expression "
                "round-trips, or classify it through the classify-axis primitive"
            )
        return {"kind": "bounded_halo", "halo": {"expr": expr}}
    raise TypeError(f"unknown DataAxis type: {type(axis).__name__}")
