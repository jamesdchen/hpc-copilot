"""Shared AST helpers used by the dispatcher and every matcher.

Extracted from the original 839-line :mod:`hpc_agent.experiment_kit.axis_matcher`
so the matchers can pull just the helpers they need and the source-of-truth
for each helper has a single home.

All helpers here are pure: they read AST nodes and return strings / sets /
maps / dicts. No I/O beyond :func:`_read_source` (which catches every
OSError / UnicodeDecodeError and returns ``None``).
"""

from __future__ import annotations

import ast
from pathlib import Path

__all__ = [
    "_append_only_receivers",
    "_called_name",
    "_carried_state_names",
    "_find_function",
    "_loop_var_name",
    "_parent_map",
    "_read_source",
    "_references",
    "_top_level_loops",
]


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


def _references(node: ast.expr, name: str) -> bool:
    """True if *node* references the Name *name* anywhere within."""
    return any(isinstance(sub, ast.Name) and sub.id == name for sub in ast.walk(node))
