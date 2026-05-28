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
