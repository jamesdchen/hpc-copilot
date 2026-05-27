"""Tests for ``hpc_agent.experiment_kit.axis_matcher`` — the AST fast-path classifier."""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.experiment_kit.axis_matcher import MatcherResult, classify_axis_easy


def _write(tmp_path: Path, source: str) -> Path:
    """Write *source* to a tmp .py file and return the path."""
    p = tmp_path / "module.py"
    p.write_text(source, encoding="utf-8")
    return p


# ─── Independent ─────────────────────────────────────────────────────────


def test_independent_loop(tmp_path: Path) -> None:
    """``for x in xs: results.append(f(x))`` is the canonical Independent shape."""
    src = _write(
        tmp_path,
        """
def run(xs):
    results = []
    for x in xs:
        y = f(x)
        results.append(y)
    return results
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "independent", result
    assert "append" in result.evidence or "no carried state" in result.evidence
    # The matcher should have ruled out reduce/accumulate and rolling-window first.
    assert "reduce_or_accumulate" in result.tried
    assert "rolling_window" in result.tried
    assert "independent_loop" in result.tried


def test_independent_loop_bare_append(tmp_path: Path) -> None:
    """Even without an intermediate assignment, append-only is Independent."""
    src = _write(
        tmp_path,
        """
def run(xs):
    out = []
    for x in xs:
        out.append(x * 2)
    return out
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "independent"


# ─── Associative ─────────────────────────────────────────────────────────


def test_associative_via_functools_reduce(tmp_path: Path) -> None:
    """``functools.reduce(operator.add, xs, 0)`` → Associative / sum."""
    src = _write(
        tmp_path,
        """
import functools
import operator

def run(xs):
    return functools.reduce(operator.add, xs, 0)
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "associative", result
    assert result.monoid == "sum"
    assert "reduce" in result.evidence


def test_associative_via_simple_accumulator(tmp_path: Path) -> None:
    """``total = 0; for x in xs: total += x`` → Associative / sum."""
    src = _write(
        tmp_path,
        """
def run(xs):
    total = 0
    for x in xs:
        total += x
    return total
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "associative", result
    assert result.monoid == "sum"
    assert "simple_accumulator" in result.tried


def test_associative_via_lambda_reduce(tmp_path: Path) -> None:
    """``functools.reduce(lambda a, b: a + b, xs)`` → Associative / sum."""
    src = _write(
        tmp_path,
        """
import functools

def run(xs):
    return functools.reduce(lambda a, b: a + b, xs)
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "associative", result
    assert result.monoid == "sum"


def test_associative_via_accumulate(tmp_path: Path) -> None:
    """``itertools.accumulate(xs)`` defaults to additive — Associative / sum."""
    src = _write(
        tmp_path,
        """
import itertools

def run(xs):
    return list(itertools.accumulate(xs))
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "associative"
    assert result.monoid == "sum"


def test_associative_explicit_acc_eq_acc_plus_x(tmp_path: Path) -> None:
    """``acc = acc + x`` is the same pattern as ``acc += x``."""
    src = _write(
        tmp_path,
        """
def run(xs):
    acc = 0
    for x in xs:
        acc = acc + x
    return acc
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "associative"
    assert result.monoid == "sum"


# ─── BoundedHalo (needs_halo_expr) ───────────────────────────────────────


def test_needs_halo_expr_via_iloc_slice(tmp_path: Path) -> None:
    """``df.iloc[i - W : i]`` is the rolling-window shape."""
    src = _write(
        tmp_path,
        """
def run(df, W):
    out = []
    for i in range(len(df)):
        window = df.iloc[i - W:i]
        out.append(window.mean())
    return out
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "needs_halo_expr", result
    assert "rolling window" in result.evidence
    assert "rolling_window" in result.tried


def test_needs_halo_expr_via_max_zero_slice(tmp_path: Path) -> None:
    """``arr[max(0, i - W) : i]`` is also the rolling-window shape."""
    src = _write(
        tmp_path,
        """
def run(arr, W):
    out = []
    for i in range(len(arr)):
        window = arr[max(0, i - W):i]
        out.append(sum(window))
    return out
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "needs_halo_expr", result


def test_needs_halo_expr_via_bare_data_slice(tmp_path: Path) -> None:
    """Plain ``data[i - W : i]`` — no ``.iloc``, no ``max(0, ...)``."""
    src = _write(
        tmp_path,
        """
def run(data, W):
    results = []
    for i in range(len(data)):
        window = data[i - W:i]
        results.append(window)
    return results
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "needs_halo_expr"


# ─── no_loop_detected ────────────────────────────────────────────────────


def test_no_loop_detected(tmp_path: Path) -> None:
    """A function with no for/while is structurally not a series loop."""
    src = _write(
        tmp_path,
        """
def run(xs):
    return sum(xs) / len(xs)
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "no_loop_detected", result
    assert "no for/while" in result.evidence


# ─── unclassifiable ──────────────────────────────────────────────────────


def test_multiple_loops_unclassifiable(tmp_path: Path) -> None:
    """Two top-level loops → unclassifiable (which loop carries the series?)."""
    src = _write(
        tmp_path,
        """
def run(xs, ys):
    out = []
    for x in xs:
        out.append(x)
    for y in ys:
        out.append(y)
    return out
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "unclassifiable", result
    assert "2 top-level loops" in result.evidence or "multi-loop" in result.evidence


def test_unknown_pattern_unclassifiable(tmp_path: Path) -> None:
    """A stateful update we don't recognise (attribute write) → unclassifiable."""
    src = _write(
        tmp_path,
        """
def run(xs, state):
    for x in xs:
        state.foo = compute(state.bar, x)
    return state
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "unclassifiable", result
    # The matcher should have walked all four named patterns.
    assert "reduce_or_accumulate" in result.tried
    assert "rolling_window" in result.tried
    assert "independent_loop" in result.tried
    assert "simple_accumulator" in result.tried


# ─── function_not_found ──────────────────────────────────────────────────


def test_function_not_found(tmp_path: Path) -> None:
    """Searching for a name not present at module scope."""
    src = _write(
        tmp_path,
        """
def other(xs):
    return xs
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "function_not_found", result
    assert "run" in result.evidence


# ─── MatcherResult shape ─────────────────────────────────────────────────


def test_matcher_result_is_frozen_dataclass() -> None:
    """The result must be hashable / immutable for clean envelope serialisation."""
    r = MatcherResult(kind="independent", evidence="x", monoid=None, tried=("a", "b"))
    with pytest.raises(Exception):  # noqa: B017,PT011 — frozen dataclass FrozenInstanceError
        r.kind = "associative"  # type: ignore[misc]


def test_unreadable_source_returns_unclassifiable(tmp_path: Path) -> None:
    """A missing source file surfaces as unclassifiable, not a crash."""
    missing = tmp_path / "does_not_exist.py"
    result = classify_axis_easy(missing, "run")
    assert result.kind == "unclassifiable"
    assert "could not read source" in result.evidence
