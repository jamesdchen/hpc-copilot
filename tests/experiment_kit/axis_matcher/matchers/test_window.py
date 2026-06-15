"""Tests for ``hpc_agent.experiment_kit.axis_matcher`` — the AST fast-path classifier.

The matcher's autonomous classification scope is narrow:

- ``independent``    — loop with no carried outer-scope state.
- ``bounded_halo``   — loop matches one of five recognized pattern-library
                       shapes (first-order stencil, finite-order stencil,
                       bounded-window deque, pandas rolling, EMA).
- ``sequential``     — loop has carried state but no recognized pattern.
- ``unclassifiable`` — multiple loops, parse error, or unreadable source.
- ``no_loop_detected`` — no for/while in body (and no vectorized rolling).
- ``function_not_found`` — ``run_name`` doesn't match any FunctionDef.

``associative`` is **not** an autonomous output — the matcher leaves
associative parallelism to user-expressed ``task_generator`` sweeps.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent.experiment_kit.axis_matcher import classify_axis_easy


def _write(tmp_path: Path, source: str) -> Path:
    """Write *source* to a tmp .py file and return the path."""
    p = tmp_path / "module.py"
    p.write_text(source, encoding="utf-8")
    return p


# ─── BoundedHalo: bounded-window deque ───────────────────────────────────


def test_bounded_window_deque(tmp_path: Path) -> None:
    """``buffer = deque(maxlen=W); for: buffer.append(...); state = compute(buffer)``."""
    src = _write(
        tmp_path,
        """
import collections

def run(data, W, N):
    buffer = collections.deque(maxlen=W)
    results = []
    for t in range(N):
        buffer.append(data[t])
        state = compute(buffer)
        results.append(state)
    return results
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "bounded_halo", result
    assert result.halo_expr == "W"
    assert "deque" in result.evidence.lower() or "window" in result.evidence.lower()


def test_bounded_window_deque_literal_maxlen(tmp_path: Path) -> None:
    """``deque(maxlen=10)`` — literal halo."""
    src = _write(
        tmp_path,
        """
from collections import deque

def run(data, N):
    buf = deque(maxlen=10)
    out = []
    for t in range(N):
        buf.append(data[t])
        out.append(sum(buf))
    return out
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "bounded_halo"
    assert result.halo_expr == "10"


def test_deque_rebuilt_inside_loop_is_not_bounded_window(tmp_path: Path) -> None:
    """A ``deque(maxlen=W)`` constructed INSIDE the loop is re-created every
    iteration and carries no cross-iteration window, so it must NOT be matched
    as a bounded-window halo. The per-iteration ``buf`` reassignment is still
    seen as carried state with no proven bounded pattern, so the matcher
    conservatively reports ``sequential`` (the safe direction) — never
    ``bounded_halo``."""
    src = _write(
        tmp_path,
        """
from collections import deque

def run(data, W, N):
    out = []
    for t in range(N):
        buf = deque(maxlen=W)
        buf.append(data[t])
        out.append(sum(buf))
    return out
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind != "bounded_halo", result
    assert result.kind == "sequential", result
