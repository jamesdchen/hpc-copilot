"""Reusable ``never-blocking`` contract helper.

Cross-plan reuse ledger: data-manifest is the first lander.

The evidence-memory / accept-with-disclosure pattern: a DISCLOSURE path must
observe and surface, never raise or gate. This helper AST-walks a callable's
source and asserts it contains no ``raise`` statement (the gate/refusal syntax) —
so a future edit that sneaks a ``raise`` into a disclosure surface trips the pin.

Other plans with a disclosure-only surface should import this rather than
re-inlining an AST walk::

    from tests.contracts.never_blocking import assert_never_blocking
    assert_never_blocking(render_manifest_disclosure)

``allow_reraise=True`` tolerates a bare ``raise`` inside an ``except`` handler
(re-raising is not a NEW gate); by default even that is rejected, since a pure
disclosure path should not have raising branches at all.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any


def _find_raises(func: Any, *, allow_reraise: bool) -> list[int]:
    """Return the (1-based, source-relative) line numbers of ``raise`` statements."""
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise):
            if allow_reraise and node.exc is None:
                continue  # bare `raise` inside an except: re-raise, not a new gate
            hits.append(node.lineno)
    return hits


def assert_never_blocking(func: Any, *, allow_reraise: bool = False) -> None:
    """Assert *func*'s source contains no ``raise`` (it never gates/blocks).

    Raises ``AssertionError`` naming the offending line(s) so the failure points
    straight at the introduced gate.
    """
    hits = _find_raises(func, allow_reraise=allow_reraise)
    assert not hits, (
        f"{getattr(func, '__qualname__', func)!r} is a disclosure path and must "
        f"never raise/gate, but contains raise statement(s) at source line(s) {hits}"
    )
