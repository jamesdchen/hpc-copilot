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
