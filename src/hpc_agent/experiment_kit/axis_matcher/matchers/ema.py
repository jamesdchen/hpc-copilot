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


def _references_complement(term: ast.expr, param_name: str) -> bool:
    """True if *term* contains the complementary factor ``(1 - param_name)``.

    Detects ``1 - β`` / ``1.0 - β`` anywhere in *term* (e.g. the ``(1-β)``
    in ``(1 - beta) * x``). This is the structural signal that distinguishes a
    genuine EMA (``β·state + (1-β)·x``, where the two weights sum to 1 so the
    recurrence is a convex combination and therefore bounded) from an
    unconstrained accumulator like ``gain·state + drive`` whose free ``gain``
    may be ≥ 1 and diverge.
    """
    for sub in ast.walk(term):
        if (
            isinstance(sub, ast.BinOp)
            and isinstance(sub.op, ast.Sub)
            and isinstance(sub.left, ast.Constant)
            and isinstance(sub.left.value, int | float)
            and float(sub.left.value) == 1.0
            and isinstance(sub.right, ast.Name)
            and sub.right.id == param_name
        ):
            return True
    return False


def _classify_ema_rhs(rhs: ast.expr, state_name: str):
    """Classify an EMA right-hand side.

    Returns one of:

    - ``"unbounded"`` for ``state + x`` (β = 1, no smoothing).
    - ``("literal", <float β>)`` for ``0.9 * state + 0.1 * x`` etc.
    - ``("param", "<name>")`` for ``beta * state + (1-beta) * x`` — ONLY when the
      input term carries the complementary ``(1 - beta)`` factor, proving the
      weights sum to 1 (a true convex-combination EMA). A bare parameter
      coefficient WITHOUT that complement (e.g. ``gain * state + drive``) is an
      unconstrained recurrence that may diverge, so it is rejected here and the
      loop falls through to the safe ``sequential`` classification.
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
        # (The β ∈ (0,1) range is enforced by the caller; a literal coefficient
        # below 1 guarantees the homogeneous part decays, so boundedness holds
        # regardless of the input term's coefficient.)
        if isinstance(coef, float):
            return ("literal", coef)
        # A bare parameter coefficient is only a bounded EMA when the input term
        # carries the complementary (1 - coef) weight; otherwise the parameter is
        # unconstrained (possibly ≥ 1) and the recurrence may diverge. Without
        # the complement, fall through to the next candidate / None so the loop
        # classifies as the safe `sequential` rather than a wrong `bounded_halo`.
        if isinstance(coef, str) and _references_complement(input_term, coef):
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
