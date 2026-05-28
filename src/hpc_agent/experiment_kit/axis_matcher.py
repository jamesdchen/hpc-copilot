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


# ─── source / function discovery ────────────────────────────────────────


def _read_source(path: Path) -> str | None:
    """Read *path* as text. Returns ``None`` on any I/O error."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
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


def _called_name(func_expr: ast.expr) -> str:
    """Flatten ``ast.Name`` / ``ast.Attribute`` to a dotted string, ``""`` otherwise."""
    if isinstance(func_expr, ast.Name):
        return func_expr.id
    if isinstance(func_expr, ast.Attribute):
        parent = _called_name(func_expr.value)
        return f"{parent}.{func_expr.attr}" if parent else func_expr.attr
    return ""


# ─── carried-state detection ────────────────────────────────────────────


def _carried_state_names(loop: ast.For | ast.While) -> set[str]:
    """Return the set of names that represent carried state across iterations.

    A name is carried state iff:

    - It is both Load-ed (read) and Store-ed (written) somewhere in the
      loop body, AND
    - It is NOT a loop-local temporary — i.e. it is not the case that
      every Store of the name appears strictly before every Load (which
      would mean each iteration freshly binds the name before reading
      it, so no value crosses iterations), AND
    - It is NOT the receiver of append-only side effects — calls like
      ``results.append(...)`` are output-only writes that don't carry
      state back into the loop body.

    The loop variable itself (``for i in range(N)``) is excluded — it's
    iteration-local by Python's semantics.
    """
    # Step 1: collect names that are both Loaded and Stored in the body.
    # We distinguish two kinds of "Store":
    #
    #   - **Fresh-bind**: a plain ``name = ...`` rebinds the local name.
    #     If a fresh-bind happens BEFORE the first Load, the name is a
    #     loop-local temporary (each iteration starts fresh; nothing
    #     carries).
    #   - **Mutation**: ``arr[i] = ...`` / ``obj.attr = ...`` mutates an
    #     existing structure named by an outer-scope binding. Mutation
    #     does NOT make a name loop-local — the structure persists across
    #     iterations, and a write in iteration N is visible to iteration
    #     N+1.
    #
    # AST notes:
    #   - `x = y`         → Name(x, Store), Name(y, Load).
    #   - `x[i] = y`      → Subscript(Name(x, Load), ..., ctx=Store) —
    #                        the bare Name(x) has Load ctx in the AST;
    #                        we ignore that as bookkeeping (not a real
    #                        value read) and treat the Subscript itself
    #                        as a Mutation of `x`.
    #   - `x.attr = y`    → Attribute(Name(x, Load), ..., ctx=Store) —
    #                        same: ignore the implicit Load, treat the
    #                        Attribute as a Mutation of `x`.
    #   - `x += y`        → AugAssign(target=Name(x, Store)) — both a
    #                        Load and a fresh-bind Store on `x`.
    loads: set[str] = set()
    fresh_binds: set[str] = set()
    mutations: set[str] = set()
    first_load_index: dict[str, int] = {}
    first_fresh_bind_index: dict[str, int] = {}

    # We walk statements in source order, and for each statement we
    # traverse the parts in **execution order** (value RHS before target
    # LHS for assignments). A single counter gives a total order over
    # all reads/writes that respects Python's evaluation order.
    counter = 0

    def _visit_load(node: ast.AST) -> None:
        nonlocal counter
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                loads.add(sub.id)
                first_load_index.setdefault(sub.id, counter)
                counter += 1

    def _visit_target(target: ast.expr) -> None:
        nonlocal counter
        if isinstance(target, ast.Name):
            fresh_binds.add(target.id)
            first_fresh_bind_index.setdefault(target.id, counter)
            counter += 1
        elif isinstance(target, ast.Subscript):
            # `arr[i] = ...`: first evaluate the subscript expression
            # (Load of arr and the index), then store into the slot.
            if isinstance(target.value, ast.Name):
                # The implicit Load of the array name is bookkeeping;
                # the actual index expression is a real Load.
                pass
            _visit_load(target.slice)
            if isinstance(target.value, ast.Name):
                mutations.add(target.value.id)
                counter += 1
        elif isinstance(target, ast.Attribute):
            if isinstance(target.value, ast.Name):
                mutations.add(target.value.id)
                counter += 1
        elif isinstance(target, ast.Tuple | ast.List):
            for elt in target.elts:
                _visit_target(elt)
        else:
            # Anything else: fall back to walking it as a Load (best-effort).
            _visit_load(target)

    for stmt in loop.body:
        if isinstance(stmt, ast.Assign):
            _visit_load(stmt.value)
            for tgt in stmt.targets:
                _visit_target(tgt)
        elif isinstance(stmt, ast.AugAssign):
            # `x += y`: read x and y, then write x.
            if isinstance(stmt.target, ast.Name):
                loads.add(stmt.target.id)
                first_load_index.setdefault(stmt.target.id, counter)
                counter += 1
                _visit_load(stmt.value)
                fresh_binds.add(stmt.target.id)
                first_fresh_bind_index.setdefault(stmt.target.id, counter)
                counter += 1
            else:
                _visit_load(stmt.value)
                _visit_target(stmt.target)
        elif isinstance(stmt, ast.AnnAssign):
            if stmt.value is not None:
                _visit_load(stmt.value)
            _visit_target(stmt.target)
        else:
            # Expression statements, conditionals, etc: walk as Load,
            # but pay attention to Subscript/Attribute Stores nested
            # inside (rare in our patterns, but handle them).
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    loads.add(sub.id)
                    first_load_index.setdefault(sub.id, counter)
                    counter += 1
                elif isinstance(sub, ast.Subscript | ast.Attribute) and isinstance(
                    sub.ctx, ast.Store
                ):
                    if isinstance(sub.value, ast.Name):
                        mutations.add(sub.value.id)
                        counter += 1
                elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    fresh_binds.add(sub.id)
                    first_fresh_bind_index.setdefault(sub.id, counter)
                    counter += 1

    # ``buf.append(x)`` mutates ``buf``. We treat it as a mutation iff
    # ``buf`` is read elsewhere in the loop body as a value (not as
    # another append-receiver) — i.e. its contents are actually carried
    # into the next iteration's computation. This is the deque case.
    append_only = _append_only_receivers(loop)
    for node in ast.walk(loop):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
        ):
            continue
        recv = node.func.value.id
        if recv in append_only:
            continue  # pure output-only sink
        mutations.add(recv)

    stores = fresh_binds | mutations

    candidates = loads & stores

    # Step 2: exclude append-only outer-scope writes. A name appears only
    # as the receiver of ``.append(...)`` (or similar list mutators that
    # are output-only by convention) — those Loads are method-resolution
    # loads, not state reads. We treat such a name as NOT carried state.
    append_only_receivers = _append_only_receivers(loop)
    candidates -= append_only_receivers

    # Step 3: exclude loop-local temporaries — names whose first
    # fresh-bind precedes their first Load. Mutations don't make a name
    # loop-local: ``arr[i] = ...`` mutates an outer-scope structure;
    # the binding survives across iterations.
    locals_: set[str] = set()
    for name in candidates:
        if name in mutations:
            continue
        fs = first_fresh_bind_index.get(name)
        fl = first_load_index.get(name)
        if fs is not None and fl is not None and fs < fl:
            locals_.add(name)
    candidates -= locals_

    # Step 4: exclude the loop variable itself.
    if isinstance(loop, ast.For) and isinstance(loop.target, ast.Name):
        candidates.discard(loop.target.id)

    return candidates


def _append_only_receivers(loop: ast.For | ast.While) -> set[str]:
    """Return outer-scope names whose ONLY in-body usage is as the receiver of an append.

    In Python's AST, ``<name>.append(...)`` is ``Call(Attribute(Name(name,
    Load), 'append'), ...)`` — the receiver Name has Load ctx. So a
    bare-append loop won't put `name` in our ``stores`` set, and it
    won't be a carried-state candidate to begin with. This filter is a
    safety net for the rare case where a name is *also* mutated (e.g.
    ``results[-1] = ...``) — if the only in-body Load of the name is
    as an append receiver, treat it as append-only despite the mutation.
    """
    # For each Name(Load), is its parent a `.append(...)` attribute?
    parents = _parent_map(loop)
    name_usages: dict[str, list[bool]] = {}  # name -> list of is_append_receiver

    for node in ast.walk(loop):
        if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
            continue
        is_append_recv = False
        parent = parents.get(id(node))
        if isinstance(parent, ast.Attribute) and parent.attr == "append" and parent.value is node:
            grandparent = parents.get(id(parent))
            if isinstance(grandparent, ast.Call) and grandparent.func is parent:
                is_append_recv = True
        name_usages.setdefault(node.id, []).append(is_append_recv)

    return {name for name, usages in name_usages.items() if usages and all(usages)}


def _parent_map(root: ast.AST) -> dict[int, ast.AST]:
    """Build a child-id → parent map for *root*."""
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(root):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


# ─── Pattern 1+2: first-order / finite-order stencil ────────────────────


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


# ─── Pattern 3: bounded-window deque ────────────────────────────────────


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


# ─── Pattern 4: pandas rolling ──────────────────────────────────────────


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


def _references(node: ast.expr, name: str) -> bool:
    """True if *node* references the Name *name* anywhere within."""
    return any(isinstance(sub, ast.Name) and sub.id == name for sub in ast.walk(node))
