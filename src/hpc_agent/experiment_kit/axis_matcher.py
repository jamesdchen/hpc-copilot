"""Stdlib-only AST pattern-matcher — the fast path for ``hpc-classify-axis``.

The ``hpc-classify-axis`` skill historically walked the full DataAxis
decision tree with LLM reasoning on every cold-start submit, even for
the boilerplate cases (``for x in xs: out.append(f(x))`` is obviously
``Independent``; ``functools.reduce(operator.add, ...)`` is obviously
``Associative``). That spent the agent's context budget on cases a
half-page of AST pattern-matching can settle deterministically.

This module is that pattern-matcher. It walks the AST of a
``@register_run`` function and tries a fixed sequence of narrow,
*confident* patterns:

1. ``functools.reduce`` / ``itertools.accumulate`` calls → ``Associative``
   (monoid inferred from the reducer; ``sum`` for ``operator.add`` /
   ``a + b`` lambdas, ``moments`` as the conservative default).
2. No ``for`` / ``while`` loop in the body → ``no_loop_detected`` (the
   skill falls back to its LLM tree).
3. More than one top-level loop → ``unclassifiable`` (multi-loop bodies
   need human reasoning about which loop carries the series).
4. A loop body that subscripts ``data[i - N : i]`` /
   ``data.iloc[i - N : i]`` / ``data[max(0, i - N) : i]`` →
   ``needs_halo_expr`` (the rolling-window shape; the skill derives the
   ``halo.expr`` from the run's parameter context).
5. A loop body whose only side effects are ``.append(...)`` on
   outer-scope lists → ``Independent``.
6. A loop with an ``acc += x`` / ``acc = acc + x`` accumulator →
   ``Associative`` (``monoid="sum"``).
7. Anything else → ``unclassifiable`` (the skill walks the LLM tree).

The bias is **conservative**: an uncertain match returns
``unclassifiable`` rather than a wrong-but-confident classification. A
misclassified series is silent corruption (the elision gate catches it
eventually, but the wrong classification has already written
``axes.yaml``); a fallback to the LLM tree is merely slower.

The skill handles the long tail. The matcher handles the bulk
(~80% of common cases).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["MatcherResult", "classify_axis_easy"]


@dataclass(frozen=True)
class MatcherResult:
    """The outcome of one classification attempt.

    Attributes
    ----------
    kind:
        One of ``"independent"``, ``"associative"``, ``"sequential"``,
        ``"needs_halo_expr"``, ``"unclassifiable"``, ``"no_loop_detected"``,
        ``"function_not_found"``. ``sequential`` is reserved — the
        matcher never emits it (Sequential is the LLM tree's fail-safe
        tiebreaker, and the matcher returns ``unclassifiable`` instead
        of guessing it).
    evidence:
        One-line natural-language reasoning suitable for an interview
        transcript entry — e.g. ``"single for-loop body is x.append(...)
        only; no carried state"``.
    monoid:
        For ``kind="associative"``: ``"sum"`` or ``"moments"``. ``None``
        otherwise.
    tried:
        Ordered tuple of the named pattern checks the matcher walked.
        Useful when ``kind="unclassifiable"`` — the skill knows which
        cheap patterns were already ruled out.
    """

    kind: str
    evidence: str
    monoid: str | None = None
    tried: tuple[str, ...] = field(default_factory=tuple)


# Pattern-check name constants — kept as a single ordered list so the
# matcher's ``tried`` tuple agrees with the skill's documented order.
_PATTERN_REDUCE_OR_ACCUMULATE = "reduce_or_accumulate"
_PATTERN_ROLLING_WINDOW = "rolling_window"
_PATTERN_INDEPENDENT_LOOP = "independent_loop"
_PATTERN_SIMPLE_ACCUMULATOR = "simple_accumulator"


def classify_axis_easy(source_path: Path, run_name: str) -> MatcherResult:
    """Pattern-match the body of *run_name* in *source_path*.

    Returns a :class:`MatcherResult` whose ``kind`` is one of the seven
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

    # 1. functools.reduce / itertools.accumulate.
    tried.append(_PATTERN_REDUCE_OR_ACCUMULATE)
    reduce_hit = _match_reduce_or_accumulate(func)
    if reduce_hit is not None:
        monoid, evidence = reduce_hit
        return MatcherResult(
            kind="associative",
            evidence=evidence,
            monoid=monoid,
            tried=tuple(tried),
        )

    # 2. No loop at all.
    loops = _top_level_loops(func)
    if not loops:
        return MatcherResult(
            kind="no_loop_detected",
            evidence="function body contains no for/while loop",
            tried=tuple(tried),
        )

    # 3. Multiple top-level loops.
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

    # 4. Rolling-window pattern.
    tried.append(_PATTERN_ROLLING_WINDOW)
    if loop_var is not None and _has_rolling_window_slice(loop, loop_var):
        return MatcherResult(
            kind="needs_halo_expr",
            evidence=(
                f"loop body subscripts data with a rolling window over {loop_var!r}; "
                "halo expression to be derived from parameter context"
            ),
            tried=tuple(tried),
        )

    # 5. Independent — append-only side effects.
    tried.append(_PATTERN_INDEPENDENT_LOOP)
    if _is_append_only_loop(loop):
        return MatcherResult(
            kind="independent",
            evidence="loop body only appends to outer-scope lists; no carried state",
            tried=tuple(tried),
        )

    # 6. Simple accumulator.
    tried.append(_PATTERN_SIMPLE_ACCUMULATOR)
    if _has_simple_sum_accumulator(loop):
        return MatcherResult(
            kind="associative",
            evidence="loop body augments an accumulator with +=; associative under addition",
            monoid="sum",
            tried=tuple(tried),
        )

    # 7. Default.
    return MatcherResult(
        kind="unclassifiable",
        evidence=(
            "single-loop body matched no cheap pattern; "
            "carried-state structure needs full classification"
        ),
        tried=tuple(tried),
    )


# ─── helpers ─────────────────────────────────────────────────────────────


def _read_source(path: Path) -> str | None:
    """Read *path* as text. Returns ``None`` on any I/O error."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _find_function(
    tree: ast.Module, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the module-level function named *name*, or ``None``."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    return None


def _top_level_loops(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.For | ast.While]:
    """Return the loops at *func*'s body's top level (not nested inside another statement)."""
    return [stmt for stmt in func.body if isinstance(stmt, ast.For | ast.While)]


def _loop_var_name(loop: ast.For | ast.While) -> str | None:
    """Return the name of a ``for var in ...`` loop's target, or ``None``."""
    if isinstance(loop, ast.For) and isinstance(loop.target, ast.Name):
        return loop.target.id
    return None


def _match_reduce_or_accumulate(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, str] | None:
    """Detect a ``functools.reduce`` or ``itertools.accumulate`` call in *func*.

    Returns ``(monoid, evidence)`` on a hit, else ``None``. The monoid
    is inferred from the reducer for ``reduce`` (``"sum"`` for
    ``operator.add`` or ``lambda a, b: a + b``; ``"moments"`` as the
    conservative default); ``accumulate`` defaults to ``"sum"``.
    """
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        called = _called_name(node.func)
        if called in {"functools.reduce", "reduce"}:
            reducer = node.args[0] if node.args else None
            monoid = "sum" if _reducer_is_addition(reducer) else "moments"
            return monoid, f"call to {called}(...) with {monoid!r} monoid"
        if called in {"itertools.accumulate", "accumulate"}:
            # accumulate(iterable, func=operator.add, ...) — default reducer is add.
            reducer = node.args[1] if len(node.args) >= 2 else None
            # An explicit non-add reducer falls back to moments; absence = sum.
            if reducer is None or _reducer_is_addition(reducer):
                return "sum", f"call to {called}(...) with default additive reducer"
            return "moments", f"call to {called}(...) with non-additive reducer"
    return None


def _called_name(func_expr: ast.expr) -> str:
    """Flatten ``ast.Name`` / ``ast.Attribute`` to a dotted string, ``""`` otherwise."""
    if isinstance(func_expr, ast.Name):
        return func_expr.id
    if isinstance(func_expr, ast.Attribute):
        parent = _called_name(func_expr.value)
        return f"{parent}.{func_expr.attr}" if parent else func_expr.attr
    return ""


def _reducer_is_addition(node: ast.expr | None) -> bool:
    """Heuristic: is *node* ``operator.add`` or ``lambda a, b: a + b``?"""
    if node is None:
        return False
    if isinstance(node, ast.Attribute) and _called_name(node) == "operator.add":
        return True
    if isinstance(node, ast.Name) and node.id == "add":
        return True
    if isinstance(node, ast.Lambda):
        body = node.body
        if (
            isinstance(body, ast.BinOp)
            and isinstance(body.op, ast.Add)
            and isinstance(body.left, ast.Name)
            and isinstance(body.right, ast.Name)
        ):
            return True
    return False


def _has_rolling_window_slice(loop: ast.For | ast.While, loop_var: str) -> bool:
    """Detect ``data[loop_var - N : loop_var]`` / ``data[max(0, loop_var - N) : loop_var]``."""
    for node in ast.walk(loop):
        if not isinstance(node, ast.Subscript):
            continue
        slc = node.slice
        if not isinstance(slc, ast.Slice):
            continue
        if not _slice_uses_loop_var_lookback(slc, loop_var):
            continue
        return True
    return False


def _slice_uses_loop_var_lookback(slc: ast.Slice, loop_var: str) -> bool:
    """Is *slc* of the shape ``[<expr using loop_var - N> : loop_var]``?

    Accepts both ``data[i - N : i]`` and ``data[max(0, i - N) : i]``.
    """
    upper = slc.upper
    lower = slc.lower
    if upper is None or lower is None:
        return False
    # The upper bound is the bare loop variable.
    if not (isinstance(upper, ast.Name) and upper.id == loop_var):
        return False
    # The lower bound contains ``loop_var - <something>``.
    return _expr_contains_lookback(lower, loop_var)


def _expr_contains_lookback(node: ast.expr, loop_var: str) -> bool:
    """True if *node* contains a ``loop_var - <expr>`` BinOp anywhere within."""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.BinOp)
            and isinstance(sub.op, ast.Sub)
            and isinstance(sub.left, ast.Name)
            and sub.left.id == loop_var
        ):
            return True
    return False


def _is_append_only_loop(loop: ast.For | ast.While) -> bool:
    """True if every statement in *loop.body* is either a target-local assignment or an
    ``X.append(...)`` expression statement on an outer-scope name.
    """
    # Collect names assigned INSIDE the loop body — these are treated as
    # local to one iteration; assigning to them does not count as
    # outer-scope state mutation.
    body_locals = _collect_local_names(loop)
    if not loop.body:
        return False
    saw_append = False
    for stmt in loop.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            if _is_append_call_on_outer(stmt.value, body_locals):
                saw_append = True
                continue
            return False
        if isinstance(stmt, ast.Assign):
            # Permit assignments only to body-local names (intermediate
            # values used to build the appended element).
            if not all(_assign_target_is_local(t, body_locals) for t in stmt.targets):
                return False
            continue
        # Any other statement (AugAssign, If with side effects, etc.)
        # disqualifies the append-only classification.
        return False
    return saw_append


def _collect_local_names(loop: ast.For | ast.While) -> set[str]:
    """Names introduced as assignment targets inside *loop.body*.

    Includes the loop's own target. Used to distinguish iteration-local
    temporaries from outer-scope state.
    """
    locals_: set[str] = set()
    if isinstance(loop, ast.For) and isinstance(loop.target, ast.Name):
        locals_.add(loop.target.id)
    for node in ast.walk(loop):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    locals_.add(tgt.id)
    return locals_


def _assign_target_is_local(target: ast.expr, body_locals: set[str]) -> bool:
    """Is *target* a bare-name assignment to a body-local name?"""
    return isinstance(target, ast.Name) and target.id in body_locals


def _is_append_call_on_outer(call: ast.Call, body_locals: set[str]) -> bool:
    """True if *call* is ``<outer_name>.append(...)`` (not ``local.append(...)``)."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "append"):
        return False
    receiver = func.value
    # Receiver may be a Name (the common case) or any expression; only
    # accept bare Names so we know it's an outer-scope binding.
    if not isinstance(receiver, ast.Name):
        return False
    return receiver.id not in body_locals


def _has_simple_sum_accumulator(loop: ast.For | ast.While) -> bool:
    """Detect ``acc += x`` or ``acc = acc + x`` inside *loop.body*."""
    for node in ast.walk(loop):
        # acc += x
        if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
            if isinstance(node.target, ast.Name):
                return True
        # acc = acc + x
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            val = node.value
            if (
                isinstance(tgt, ast.Name)
                and isinstance(val, ast.BinOp)
                and isinstance(val.op, ast.Add)
                and isinstance(val.left, ast.Name)
                and val.left.id == tgt.id
            ):
                return True
    return False
