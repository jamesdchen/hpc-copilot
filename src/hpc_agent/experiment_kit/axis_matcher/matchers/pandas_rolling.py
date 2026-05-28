"""Pandas rolling matcher.

Detects ``<obj>.rolling(window=W).<aggregator>()`` — either inside a
loop or vectorised at module scope. The halo equals ``W``. Aggregator
names mirror the public ``pandas.core.window.rolling.Rolling`` API.

Extracted from the original 839-line :mod:`axis_matcher` so each
pattern has its own home.
"""

from __future__ import annotations

import ast

from hpc_agent.experiment_kit.axis_matcher.matchers.window import _render_halo_token

__all__ = ["_match_pandas_rolling"]


_ROLLING_AGGREGATORS = frozenset(
    {"apply", "mean", "sum", "std", "var", "min", "max", "median", "count", "agg"}
)


def _match_pandas_rolling(scope: ast.AST) -> tuple[str, str] | None:
    """Detect ``<obj>.rolling(window=W).<aggregator>()`` anywhere in *scope*.

    Returns ``(halo_expr, evidence)``. *scope* may be a loop, a
    function, or any AST node — the search walks recursively.
    """
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if not isinstance(callee, ast.Attribute):
            continue
        if callee.attr not in _ROLLING_AGGREGATORS:
            continue
        # callee.value should be a Call to .rolling(window=W).
        rolling_call = callee.value
        if not isinstance(rolling_call, ast.Call):
            continue
        rolling_callee = rolling_call.func
        if not (isinstance(rolling_callee, ast.Attribute) and rolling_callee.attr == "rolling"):
            continue
        # Extract window argument (kwarg `window=` or first positional).
        window_expr: ast.expr | None = None
        for kw in rolling_call.keywords:
            if kw.arg == "window":
                window_expr = kw.value
                break
        if window_expr is None and rolling_call.args:
            window_expr = rolling_call.args[0]
        if window_expr is None:
            continue
        halo_expr = _render_halo_token(window_expr)
        if halo_expr is None:
            continue
        evidence = (
            f"pandas rolling(window={halo_expr}).{callee.attr}() — "
            f"bounded-window aggregation; halo = {halo_expr}"
        )
        return halo_expr, evidence
    return None
