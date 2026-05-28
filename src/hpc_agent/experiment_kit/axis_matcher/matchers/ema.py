"""EMA / exponential-smoothing matcher.

Detects ``state = β * state + (1-β) * x`` (or equivalent symmetric
forms). The effective halo depends on β:

- literal β in (0, 1) → ``ceil(5 / (1 - β))`` (heuristic effective halo)
- bare-parameter β → ``100`` (conservative fixed default)
- ``state = state + x`` (β = 1) → ``sequential`` (no bounded halo)

Extracted from the original 839-line :mod:`axis_matcher` so each
pattern has its own home.
"""

from __future__ import annotations

import ast
import math

from hpc_agent.experiment_kit.axis_matcher._ast_utils import _references

__all__ = ["_classify_ema_rhs", "_extract_state_coef", "_match_ema_smoothing"]


def _match_ema_smoothing(
    loop: ast.For | ast.While, carried: set[str]
) -> tuple[str, str | None, str] | None:
    """Detect ``state = β * state + (1-β) * x`` (or equivalents).

    Returns ``(kind, halo_expr, evidence)``. Outcomes:

    - β literal in (0, 1) → ``("bounded_halo", str(ceil(5/(1-β))), ...)``.
    - β a bare parameter name → ``("bounded_halo", "100", ...)`` (conservative).
    - ``state = state + x`` (β = 1) → ``("sequential", None, ...)`` (unbounded).
    - No EMA shape found → ``None``.
    """
    # Look for an Assign of the form `state = <expr>` where `state` is
    # carried-state and the RHS is a binary expression involving state.
    for node in ast.walk(loop):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        state_name = node.targets[0].id
        if state_name not in carried:
            continue
        rhs = node.value
        ema = _classify_ema_rhs(rhs, state_name)
        if ema is None:
            continue
        if ema == "unbounded":
            return (
                "sequential",
                None,
                f"loop assigns {state_name} = {state_name} + ... — "
                "unbounded accumulation; no bounded halo",
            )
        if ema[0] == "literal":
            beta = ema[1]
            if not (0.0 < beta < 1.0):
                continue
            # Heuristic effective halo: ceil(5 / (1 - β)).
            halo_int = max(1, int(math.ceil(5.0 / (1.0 - beta))))
            return (
                "bounded_halo",
                str(halo_int),
                f"loop applies exponential smoothing on {state_name} with β={beta} "
                f"(effective halo ≈ {halo_int})",
            )
        if ema[0] == "param":
            param_name = ema[1]
            # Halo-expression syntax disallows `ceil` and `/`; fall back to
            # a conservative fixed default.
            return (
                "bounded_halo",
                "100",
                f"loop applies exponential smoothing on {state_name} with β={param_name} "
                "(parameter; conservative halo = 100)",
            )
    return None


def _classify_ema_rhs(rhs: ast.expr, state_name: str):
    """Classify an EMA right-hand side.

    Returns one of:

    - ``"unbounded"`` for ``state + x`` (β = 1, no smoothing).
    - ``("literal", <float β>)`` for ``0.9 * state + 0.1 * x`` etc.
    - ``("param", "<name>")`` for ``beta * state + (1-beta) * x``.
    - ``None`` otherwise.
    """
    # Unbounded: state + <anything not involving state>.
    if (
        isinstance(rhs, ast.BinOp)
        and isinstance(rhs.op, ast.Add)
        and isinstance(rhs.left, ast.Name)
        and rhs.left.id == state_name
        and not _references(rhs.right, state_name)
    ):
        return "unbounded"
    # Symmetric: x + state.
    if (
        isinstance(rhs, ast.BinOp)
        and isinstance(rhs.op, ast.Add)
        and isinstance(rhs.right, ast.Name)
        and rhs.right.id == state_name
        and not _references(rhs.left, state_name)
    ):
        return "unbounded"

    # EMA shape: <β-term> + <(1-β)-term>.
    if not (isinstance(rhs, ast.BinOp) and isinstance(rhs.op, ast.Add)):
        return None
    left, right = rhs.left, rhs.right
    # Either side may be the `<coef> * state` part.
    candidates = (
        (left, right),
        (right, left),
    )
    for state_term, input_term in candidates:
        coef = _extract_state_coef(state_term, state_name)
        if coef is None:
            continue
        # The input_term must NOT reference state (it's the new sample's contribution).
        if _references(input_term, state_name):
            continue
        # If the coef is a literal float β ∈ (0, 1), we have a literal EMA.
        if isinstance(coef, float):
            return ("literal", coef)
        # If the coef is a bare parameter name, we have a param EMA.
        if isinstance(coef, str):
            return ("param", coef)
    return None


def _extract_state_coef(term: ast.expr, state_name: str):
    """If *term* is ``<coef> * <state_name>`` (or ``<state_name> * <coef>``), return coef.

    Returns a float for literal coefficients, a string for bare-name
    parameter coefficients, or ``None`` if the shape doesn't match.
    """
    if not (isinstance(term, ast.BinOp) and isinstance(term.op, ast.Mult)):
        return None
    for left, right in ((term.left, term.right), (term.right, term.left)):
        if isinstance(right, ast.Name) and right.id == state_name:
            # left is the coefficient.
            if isinstance(left, ast.Constant) and isinstance(left.value, int | float):
                return float(left.value)
            if isinstance(left, ast.Name):
                return left.id
    return None
