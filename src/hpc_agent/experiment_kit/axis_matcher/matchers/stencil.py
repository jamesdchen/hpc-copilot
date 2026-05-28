"""First-order / finite-order stencil matcher.

Detects ``x[i] = f(..., x[i-K], ...)`` for a carried name ``x``. The
halo equals the largest literal-integer offset K read in any
``x[i-K]`` reference. Halo = 1 is the first-order case; higher Ks are
finite-order.

Extracted from the original 839-line :mod:`axis_matcher` so each
pattern has its own home.
"""

from __future__ import annotations

import ast

__all__ = ["_extract_lookback_offset", "_match_stencil"]


def _match_stencil(
    loop: ast.For | ast.While, loop_var: str, carried: set[str]
) -> tuple[str, str] | None:
    """Detect ``x[i] = f(..., x[i-K], ...)`` for the carried name ``x``.

    Returns ``(halo_expr, evidence)`` on a hit. The halo is the largest
    literal-integer offset K found in any ``x[i-K]`` read for any
    carried-state array name.
    """
    # For each carried name, find:
    #  - Stores of the form `name[<i>] = ...`
    #  - Loads of the form `name[i - K]` for literal K
    max_offset: int | None = None
    matched_name: str | None = None

    for node in ast.walk(loop):
        if not isinstance(node, ast.Subscript):
            continue
        # Identify the array name.
        if not isinstance(node.value, ast.Name):
            continue
        arr = node.value.id
        if arr not in carried:
            continue
        # Reads: arr[i - K] in Load context.
        if isinstance(node.ctx, ast.Load):
            offset = _extract_lookback_offset(node.slice, loop_var)
            if offset is not None and offset >= 1 and (max_offset is None or offset > max_offset):
                max_offset = offset
                matched_name = arr

    if max_offset is None:
        return None

    # Also require that the array is Store-d at `[<something involving i>]`
    # at least once, so we know iteration N writes the array (otherwise
    # it's a pure read of a pre-existing array, not a stencil).
    has_indexed_store = False
    for node in ast.walk(loop):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == matched_name
        ):
            has_indexed_store = True
            break
    if not has_indexed_store:
        return None

    halo_expr = str(max_offset)
    evidence = (
        f"loop assigns {matched_name}[i] from {matched_name}[i-{max_offset}] "
        f"({'first' if max_offset == 1 else 'finite'}-order stencil; halo = {max_offset})"
    )
    return halo_expr, evidence


def _extract_lookback_offset(slc: ast.AST, loop_var: str) -> int | None:
    """If *slc* is ``loop_var - K`` for literal K, return K; else None."""
    # Direct subscript like x[i - K] (slc is the expression, not a Slice).
    if (
        isinstance(slc, ast.BinOp)
        and isinstance(slc.op, ast.Sub)
        and isinstance(slc.left, ast.Name)
        and slc.left.id == loop_var
        and isinstance(slc.right, ast.Constant)
        and isinstance(slc.right.value, int)
    ):
        return slc.right.value
    return None
