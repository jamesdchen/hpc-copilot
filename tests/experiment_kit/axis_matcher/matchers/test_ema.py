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


def test_ema_param_without_complement_is_sequential(tmp_path: Path) -> None:
    """``state = gain * state + drive[t]`` — a bare-param coefficient with NO
    complementary ``(1 - gain)`` weight is an unconstrained recurrence that may
    diverge (gain ≥ 1). It must NOT be classified as a bounded halo; the safe
    fallback is Sequential."""
    src = _write(
        tmp_path,
        """
def run(drive, N, gain):
    state = 0.0
    results = []
    for t in range(N):
        state = gain * state + drive[t]
        results.append(state)
    return results
""",
    )
    result = classify_axis_easy(src, "run")
    assert result.kind == "sequential", result
    assert result.halo_expr is None


def test_ema_unbounded_accumulator(tmp_path: Path) -> None:
    """``state = state + data[t]`` (β=1) is unbounded → Sequential."""
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
