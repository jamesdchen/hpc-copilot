"""Stdlib-only AST pattern-matcher — the fast path for ``hpc-classify-axis``.

The ``hpc-classify-axis`` skill historically walked the full DataAxis
decision tree with LLM reasoning on every cold-start submit, even for
the boilerplate cases (``for x in xs: out.append(f(x))`` is obviously
``Independent``; ``u[i] = u[i-1] + dt * f(u[i-1])`` is obviously a
first-order stencil with halo 1). That spent the agent's context budget
on cases a half-page of AST pattern-matching can settle deterministically.

This module is that pattern-matcher. Its autonomous classification
scope is narrow on purpose:

| Kind | When committed | Halo expr |
|---|---|---|
| ``independent`` | Loop has no carried state — outer-scope writes are append-only | n/a |
| ``bounded_halo`` | Loop matches one of the pattern library shapes (below) | derived |
| ``sequential`` | Carried state present but no recognized bounded-halo pattern | n/a |
| ``unclassifiable`` | Anything the matcher can't confidently categorize | n/a |
| ``no_loop_detected`` | No for/while in function body (and no pandas rolling op) | n/a |
| ``function_not_found`` | ``run_name`` doesn't match any FunctionDef | n/a |

**Associative is NOT detected autonomously.** The framework already
provides task-array map-reduce via the ``combine-wave`` machinery —
users who want to parallelize an inner reduction express it as a sweep
dimension in their ``task_generator``. So the matcher leaves
Associative classification to user-expressed task generators rather
than guessing it from a loop's accumulator shape.

The bounded-halo pattern library covers five shapes:

1. **First-order stencil** — ``x[i] = f(..., x[i-1], ...)``; halo = 1.
2. **Finite-order stencil** — same with offsets up to ``x[i-K]``; halo = K.
3. **Bounded-window deque** — ``deque(maxlen=W)`` filled in the loop;
   halo = W (literal or parameter name).
4. **Pandas rolling** — ``.rolling(window=W).<aggregator>()``, either
   inside a loop or vectorized at module scope; halo = W.
5. **EMA / exponential smoothing** — ``state = β * state + (1-β) * x``;
   halo = ``ceil(5 / (1 - β))`` for literal β (heuristic effective halo)
   or ``100`` for parameter β (conservative default).

The defining characteristic of BoundedHalo is **iteration N reads
iteration N-1's computed OUTPUT** (carried state). Input-array
windowing like ``data[i-W:i]``, where the loop body refits from scratch
each iteration, is **Independent** — no output is carried.

The bias is **conservative**: if no pattern matches but carried state
is present, the matcher returns ``sequential`` (the framework runs the
inner loop serially — safe, just slower). A misclassified series is
silent corruption (the elision gate catches it eventually, but the
wrong classification has already written ``axes.yaml``); a fallback to
``sequential`` is merely slower.

The skill handles the long tail (novel carried-state shapes, including
the Associative cases the matcher does not detect autonomously). The
matcher handles the bulk (~80% of common cases).
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field
from pathlib import Path

from hpc_agent.experiment_kit.axis_matcher._ast_utils import (
    _append_only_receivers,  # noqa: F401 — re-export for backwards compat
    _called_name,
    _carried_state_names,
    _find_function,
    _loop_var_name,
    _parent_map,  # noqa: F401 — re-export for backwards compat
    _read_source,
    _references,
    _top_level_loops,
)
from hpc_agent.experiment_kit.axis_matcher.matchers.stencil import (
    _extract_lookback_offset,  # noqa: F401 — re-export for backwards compat
    _match_stencil,
)
from hpc_agent.experiment_kit.axis_matcher.matchers.pandas_rolling import (
    _match_pandas_rolling,
)
from hpc_agent.experiment_kit.axis_matcher.matchers.window import (
    _match_bounded_window_deque,
    _render_halo_token,  # noqa: F401 — re-export for backwards compat
)

__all__ = ["MatcherResult", "classify_axis_easy"]


@dataclass(frozen=True)
class MatcherResult:
    """The outcome of one classification attempt.

    Attributes
    ----------
    kind:
        One of ``"independent"``, ``"bounded_halo"``, ``"sequential"``,
        ``"unclassifiable"``, ``"no_loop_detected"``,
        ``"function_not_found"``.
    evidence:
        One-line natural-language reasoning suitable for an interview
        transcript entry — e.g. ``"single for-loop body is x.append(...)
        only; no carried state"``.
    halo_expr:
        For ``kind="bounded_halo"``: a string in the halo-expression
        syntax (numeric literals, bare parameter names, ``+ - * //``,
        ``min`` / ``max``). ``None`` for every other ``kind``.
    tried:
        Ordered tuple of the named pattern checks the matcher walked.
        Useful when ``kind`` is ``"unclassifiable"`` / ``"sequential"``
        — the skill knows which cheap patterns were already ruled out.
    """

    kind: str
    evidence: str
    halo_expr: str | None = None
    tried: tuple[str, ...] = field(default_factory=tuple)


# Pattern-check name constants — kept as a single ordered list so the
# matcher's ``tried`` tuple agrees with the skill's documented order.
_PATTERN_PANDAS_ROLLING = "pandas_rolling"
_PATTERN_FIRST_ORDER_STENCIL = "first_order_stencil"
_PATTERN_FINITE_ORDER_STENCIL = "finite_order_stencil"
_PATTERN_BOUNDED_WINDOW_DEQUE = "bounded_window_deque"
_PATTERN_EMA_SMOOTHING = "ema_smoothing"


def classify_axis_easy(source_path: Path, run_name: str) -> MatcherResult:
    """Pattern-match the body of *run_name* in *source_path*.

    Returns a :class:`MatcherResult` whose ``kind`` is one of the six
    documented values. The function is total — it never raises on a
    syntax error or a missing function; both surface as a structured
    result (``unclassifiable`` / ``function_not_found``).
    """
    source = _read_source(source_path)
    if source is None:
        return MatcherResult(
            kind="unclassifiable",
            evidence=f"could not read source at {source_path}",
        )
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return MatcherResult(
            kind="unclassifiable",
            evidence=f"source did not parse: {exc.msg}",
        )

    func = _find_function(tree, run_name)
    if func is None:
        return MatcherResult(
            kind="function_not_found",
            evidence=f"no function named {run_name!r} at module scope",
        )

    tried: list[str] = []

    loops = _top_level_loops(func)

    # Pandas rolling can appear with or without a surrounding loop —
    # the `.rolling(window=W).<agg>()` call structure is the signature.
    # Check it first so a vectorized rolling op is recognized even
    # without a for-loop, and an in-loop rolling is recognized even
    # when the loop body has no other carried state.
    tried.append(_PATTERN_PANDAS_ROLLING)
    rolling_hit = _match_pandas_rolling(func)
    if rolling_hit is not None:
        halo_expr, evidence = rolling_hit
        prefix = "" if loops else "vectorized "
        return MatcherResult(
            kind="bounded_halo",
            evidence=f"{prefix}{evidence}",
            halo_expr=halo_expr,
            tried=tuple(tried),
        )

    # No loop: nothing more to try.
    if not loops:
        return MatcherResult(
            kind="no_loop_detected",
            evidence="function body contains no for/while loop",
            tried=tuple(tried),
        )

    # Multiple top-level loops: too complex to autonomously classify.
    if len(loops) > 1:
        return MatcherResult(
            kind="unclassifiable",
            evidence=(
                f"function body has {len(loops)} top-level loops; "
                "multi-loop bodies need explicit reasoning about which loop carries the series"
            ),
            tried=tuple(tried),
        )

    loop = loops[0]
    loop_var = _loop_var_name(loop)

    # Identify carried state.
    carried = _carried_state_names(loop)

    # No carried state → Independent.
    if not carried:
        return MatcherResult(
            kind="independent",
            evidence="loop body has no outer-scope read-then-write across iterations",
            tried=tuple(tried),
        )

    # Carried state present. Try each remaining BoundedHalo pattern.
    # 1+2. First-order / finite-order stencil.
    tried.append(_PATTERN_FIRST_ORDER_STENCIL)
    if loop_var is not None:
        stencil_hit = _match_stencil(loop, loop_var, carried)
        if stencil_hit is not None:
            halo_expr, evidence = stencil_hit
            if halo_expr != "1":
                tried.append(_PATTERN_FINITE_ORDER_STENCIL)
            return MatcherResult(
                kind="bounded_halo",
                evidence=evidence,
                halo_expr=halo_expr,
                tried=tuple(tried),
            )
        tried.append(_PATTERN_FINITE_ORDER_STENCIL)

    # 3. Bounded-window deque.
    tried.append(_PATTERN_BOUNDED_WINDOW_DEQUE)
    deque_hit = _match_bounded_window_deque(func, loop)
    if deque_hit is not None:
        halo_expr, evidence = deque_hit
        return MatcherResult(
            kind="bounded_halo",
            evidence=evidence,
            halo_expr=halo_expr,
            tried=tuple(tried),
        )

    # 4. EMA / exponential smoothing.
    tried.append(_PATTERN_EMA_SMOOTHING)
    ema_hit = _match_ema_smoothing(loop, carried)
    if ema_hit is not None:
        # ema_halo_expr is Optional: the unbounded β=1 branch returns None
        # alongside kind="sequential" (no bounded halo). MatcherResult.halo_expr
        # accepts None, so it propagates straight through.
        ema_kind, ema_halo_expr, ema_evidence = ema_hit
        return MatcherResult(
            kind=ema_kind,
            evidence=ema_evidence,
            halo_expr=ema_halo_expr,
            tried=tuple(tried),
        )

    # Carried state, no pattern matched → Sequential (safe default).
    return MatcherResult(
        kind="sequential",
        evidence=(
            f"loop carries outer-scope state {sorted(carried)!r} but no bounded-halo "
            "pattern matched; framework should run the inner loop serially"
        ),
        tried=tuple(tried),
    )


# ─── helpers live in _ast_utils ─────────────────────────────────────────
#
# Source / function discovery, carried-state detection, the append-only
# safety-net, the parent-map builder, and the _references walker all
# live in :mod:`hpc_agent.experiment_kit.axis_matcher._ast_utils` and
# are imported at the top of this module. They re-export above so any
# downstream code that reached in via axis_matcher._foo keeps
# working.


# ─── Pattern 1+2: first-order / finite-order stencil ────────────────────


# ``_match_stencil`` and ``_extract_lookback_offset`` live in
# :mod:`hpc_agent.experiment_kit.axis_matcher.matchers.stencil`.
# They re-export above so the dispatcher (and any legacy attribute-
# access caller) sees them on this module.


# ─── Pattern 3: bounded-window deque ────────────────────────────────────


# ``_match_bounded_window_deque`` and ``_render_halo_token`` live in
# :mod:`hpc_agent.experiment_kit.axis_matcher.matchers.window`. The
# pandas-rolling matcher (next door) also imports ``_render_halo_token``
# from there.


# ─── Pattern 4: pandas rolling ──────────────────────────────────────────


# ``_match_pandas_rolling`` lives in
# :mod:`hpc_agent.experiment_kit.axis_matcher.matchers.pandas_rolling`.


# ─── Pattern 5: EMA / exponential smoothing ─────────────────────────────


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


