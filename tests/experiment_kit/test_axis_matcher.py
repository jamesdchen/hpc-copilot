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
    assert "no outer-scope read-then-write" in result.evidence or "no carried" in result.evidence


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


def test_input_slicing_is_independent(tmp_path: Path) -> None:
    """``for t: train = data[t-W:t]; model = fit(train); pred = model.predict(...)``.

    Input-array windowing where the loop body refits a model from scratch
    each iteration is Independent — no iteration reads any prior
    iteration's *output*. The defining characteristic of BoundedHalo is
    carried state (output → input), NOT input slicing.
    """
    src = _write(
        tmp_path,
        """
def run(data, W, N):
    results = []
    for t in range(W, N):
        train = data[t-W:t]
        model = fit(train)
        pred = model.predict(data[t])
        results.append(pred)
    return results
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "independent", result


def test_loop_local_temporaries_dont_count_as_carry(tmp_path: Path) -> None:
    """``for x in xs: temp = compute(x); results.append(temp)``.

    ``temp`` is freshly bound at the top of each iteration before any
    Load — it's a loop-local temporary, not carried state.
    """
    src = _write(
        tmp_path,
        """
def run(xs):
    results = []
    for x in xs:
        temp = compute(x)
        results.append(temp)
    return results
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "independent", result


# ─── BoundedHalo: first-order stencil ────────────────────────────────────


def test_first_order_stencil(tmp_path: Path) -> None:
    """``u[i] = u[i-1] + dt * f(u[i-1])`` is the canonical first-order stencil."""
    src = _write(
        tmp_path,
        """
def run(u, N, dt):
    for i in range(1, N):
        u[i] = u[i-1] + dt * f(u[i-1])
    return u
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "bounded_halo", result
    assert result.halo_expr == "1"
    assert "first" in result.evidence.lower() or "stencil" in result.evidence.lower()


# ─── BoundedHalo: finite-order stencil ───────────────────────────────────


def test_finite_order_stencil_K2(tmp_path: Path) -> None:
    """``u[i] = a*u[i-1] + b*u[i-2] + ...`` is a finite-order stencil (K=2)."""
    src = _write(
        tmp_path,
        """
def run(u, data, N, a, b, c):
    for i in range(2, N):
        u[i] = a * u[i-1] + b * u[i-2] + c * data[i]
    return u
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "bounded_halo", result
    assert result.halo_expr == "2"


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


# ─── BoundedHalo: pandas rolling ─────────────────────────────────────────


def test_pandas_rolling_inside_loop(tmp_path: Path) -> None:
    """``for t: df.rolling(window=W).mean()`` — rolling op inside a loop."""
    src = _write(
        tmp_path,
        """
def run(df, W, N):
    results = []
    for t in range(N):
        window = df.rolling(window=W).mean()
        results.append(window.iloc[t])
    return results
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "bounded_halo", result
    assert result.halo_expr == "W"


def test_pandas_rolling_vectorized(tmp_path: Path) -> None:
    """``df.rolling(window=10).mean()`` with no explicit loop — still BoundedHalo."""
    src = _write(
        tmp_path,
        """
def run(df):
    rolling_mean = df.rolling(window=10).mean()
    return rolling_mean
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "bounded_halo", result
    assert result.halo_expr == "10"


# ─── BoundedHalo: EMA / exponential smoothing ────────────────────────────


def test_ema_literal_beta(tmp_path: Path) -> None:
    """``state = 0.9 * state + 0.1 * data[t]`` — literal β=0.9 → halo ≈ 50."""
    src = _write(
        tmp_path,
        """
def run(data, N):
    state = 0.0
    results = []
    for t in range(N):
        state = 0.9 * state + 0.1 * data[t]
        results.append(state)
    return results
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "bounded_halo", result
    # ceil(5 / (1 - 0.9)) — float precision puts the quotient at
    # 50.000000000000014, so the ceil rounds up to 51 rather than 50.
    # The halo is meant to be conservative; either value is acceptable.
    assert result.halo_expr in {"50", "51"}


def test_ema_param_beta(tmp_path: Path) -> None:
    """``state = beta * state + (1-beta) * data[t]`` — param β → conservative halo=100."""
    src = _write(
        tmp_path,
        """
def run(data, N, beta):
    state = 0.0
    results = []
    for t in range(N):
        state = beta * state + (1 - beta) * data[t]
        results.append(state)
    return results
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "bounded_halo", result
    assert result.halo_expr == "100"


def test_ema_unbounded_accumulator(tmp_path: Path) -> None:
    """``state = state + data[t]`` (β=1) is unbounded → Sequential.

    Also pins the EMA-branch destructure: ``_match_ema_smoothing`` returns
    ``(kind, halo_expr, evidence)`` with ``halo_expr=None`` for the unbounded
    case, and the caller must propagate that None straight into
    ``MatcherResult.halo_expr`` without losing the kind/evidence alignment.
    """
    src = _write(
        tmp_path,
        """
def run(data, N):
    state = 0.0
    results = []
    for t in range(N):
        state = state + data[t]
        results.append(state)
    return results
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "sequential", result
    assert result.halo_expr is None
    # Evidence must come from the EMA branch (mentions "unbounded accumulation"),
    # not the generic "no pattern matched" sequential fallback — proves the
    # destructure landed all three fields, not just kind.
    assert "unbounded accumulation" in result.evidence, result.evidence


# ─── Sequential: carried state, no recognized pattern ───────────────────


def test_outer_scope_state_not_recognized(tmp_path: Path) -> None:
    """``state.foo = compute(state.bar, x)`` — carried state via attribute mutation.

    No pattern in our library matches; the safe default is Sequential
    (framework runs the inner loop serially).
    """
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
    assert result.kind == "sequential", result
    assert result.halo_expr is None


def test_simple_accumulator_is_sequential(tmp_path: Path) -> None:
    """``total = 0; for x in xs: total += x`` is carried-state with no halo pattern.

    The matcher does NOT autonomously detect this as Associative — users
    express associative parallelism via ``task_generator`` sweeps. So
    the accumulator falls through to Sequential.
    """
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
    assert result.kind == "sequential", result


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
    r = MatcherResult(kind="independent", evidence="x", halo_expr=None, tried=("a", "b"))
    with pytest.raises(Exception):  # noqa: B017,PT011 — frozen dataclass FrozenInstanceError
        r.kind = "bounded_halo"  # type: ignore[misc]


def test_unreadable_source_returns_unclassifiable(tmp_path: Path) -> None:
    """A missing source file surfaces as unclassifiable, not a crash."""
    missing = tmp_path / "does_not_exist.py"
    result = classify_axis_easy(missing, "run")
    assert result.kind == "unclassifiable"
    assert "could not read source" in result.evidence


# ─── Regression: rolling-window-style input slicing is NOT BoundedHalo ──


def test_iloc_input_slicing_is_independent(tmp_path: Path) -> None:
    """``window = df.iloc[i-W:i]; out.append(window.mean())``.

    Previously (incorrectly) flagged as ``needs_halo_expr``. Each
    iteration reads a slice of the *input* and computes from scratch —
    no carried state, no halo, no output dependency. This is
    Independent.
    """
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
    assert result.kind == "independent", result


def test_max_zero_input_slicing_is_independent(tmp_path: Path) -> None:
    """``arr[max(0, i-W):i]`` — input slicing, NOT a carried-state halo."""
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
    assert result.kind == "independent", result


def test_bare_data_input_slicing_is_independent(tmp_path: Path) -> None:
    """Plain ``data[i - W : i]`` input slicing — Independent."""
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
    assert result.kind == "independent", result
