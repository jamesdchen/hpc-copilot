"""The serial-elision harness — the backstop that makes inference safe.

Classifying a series axis (by hand or by an LLM reading ``run()``) is
program analysis, and program analysis is sometimes wrong. A wrong
:data:`DataAxis` produces a job that runs fine and returns *plausible
but incorrect* numbers — the worst kind of bug.

:func:`check_elision` is the defence. It runs an experiment fixture once
**whole** and once **split** ``chunks`` ways, then asserts the two agree
(exactly, or within a declared float tolerance). "Elision" because a
correct parallelization is one where the partition can be *elided* — the
split run is fungible with the serial run.

Wire :func:`assert_elision_equivalent` into a downstream repo's CI as a
required gate and a misclassified axis fails the build instead of
silently corrupting results.

Contract for the ``run`` callable: it takes the experiment's own kwargs
(never ``start`` / ``end`` / ``halo``), calls
:func:`hpc_agent.incorporation.template.load_series` for its data, and returns either

- a per-row output **sequence covering its loaded (haloed) slice** — for
  :class:`Independent` / :class:`BoundedHalo` axes; the harness trims the
  warm-up prefix itself — or
- a :class:`~hpc_agent.incorporation.template.Monoid` partial — for an
  :class:`Associative` axis.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hpc_agent.incorporation.template import series
from hpc_agent.incorporation.template.axis import (
    Associative,
    BoundedHalo,
    DataAxis,
    Independent,
    Sequential,
)
from hpc_agent.incorporation.template.plan import plan_tasks
from hpc_agent.incorporation.template.reduce import reduce_monoid
from hpc_agent.incorporation.template.series import SliceSpec

__all__ = ["ElisionReport", "check_elision", "assert_elision_equivalent"]

_SLICE_KEYS = ("start", "end", "halo")


@dataclass
class ElisionReport:
    """Outcome of a :func:`check_elision` run."""

    passed: bool
    axis: str
    chunks: int
    detail: str
    whole: Any = None
    split: Any = None

    def __bool__(self) -> bool:
        return self.passed


def check_elision(
    run: Callable[..., Any],
    params: dict[str, Any],
    data_axis: DataAxis,
    *,
    chunks: int,
    series_length: int,
    tol: float = 0.0,
) -> ElisionReport:
    """Run *run* whole vs. split and report whether the results agree.

    Parameters
    ----------
    run:
        The experiment callable (see the module docstring for its
        contract).
    params:
        The sweep-point kwargs passed to *run* — never slice keys.
    data_axis:
        The :data:`DataAxis` classification under test.
    chunks:
        How many chunks to split into for the split run.
    series_length:
        Length of the series *run* loads.
    tol:
        Absolute float tolerance for the whole-vs-split comparison.
        ``0.0`` demands bit-exact equality.
    """
    params = dict(params)
    whole = _run_one(run, SliceSpec(0, -1, 0), params)

    if isinstance(data_axis, Sequential):
        return ElisionReport(
            passed=True,
            axis="Sequential",
            chunks=1,
            detail="Sequential axis is not split; the serial run is the only run.",
            whole=whole,
            split=whole,
        )

    plan = plan_tasks([params], data_axis, chunks=chunks, series_length=series_length)
    pieces: list[tuple[SliceSpec, Any]] = []
    for i in range(plan.total()):
        task = plan.resolve(i)
        spec = SliceSpec(task["start"], task["end"], task["halo"])
        run_kwargs = {k: v for k, v in task.items() if k not in _SLICE_KEYS}
        pieces.append((spec, _run_one(run, spec, run_kwargs)))

    combined = _combine(pieces, data_axis)
    passed = _approx_equal(whole, combined, tol)
    tol_note = "bit-exact" if tol == 0.0 else f"within tol={tol}"
    if passed:
        detail = f"split into {plan.total()} chunks reproduces the whole-series run ({tol_note})."
    else:
        detail = (
            f"split into {plan.total()} chunks DIVERGED from the whole-series run "
            f"({tol_note}). The {type(data_axis).__name__} classification is unsafe for "
            "this experiment — the partition cuts an unaccounted data dependency. "
            "Reclassify (a wider BoundedHalo, or Sequential)."
        )
    return ElisionReport(
        passed=passed,
        axis=type(data_axis).__name__,
        chunks=plan.total(),
        detail=detail,
        whole=whole,
        split=combined,
    )


def assert_elision_equivalent(
    run: Callable[..., Any],
    params: dict[str, Any],
    data_axis: DataAxis,
    *,
    chunks: int,
    series_length: int,
    tol: float = 0.0,
) -> ElisionReport:
    """Like :func:`check_elision` but raise :class:`AssertionError` on divergence.

    The form to call from a CI gate. Returns the passing report so the
    caller can log ``report.detail``.
    """
    report = check_elision(
        run, params, data_axis, chunks=chunks, series_length=series_length, tol=tol
    )
    if not report.passed:
        raise AssertionError(f"serial-elision gate failed: {report.detail}")
    return report


def _run_one(run: Callable[..., Any], spec: SliceSpec, kwargs: dict[str, Any]) -> Any:
    token = series.activate_slice(spec)
    try:
        return run(**kwargs)
    finally:
        series.deactivate_slice(token)


def _combine(pieces: list[tuple[SliceSpec, Any]], data_axis: DataAxis) -> Any:
    if isinstance(data_axis, Associative):
        return reduce_monoid([out for _, out in pieces], data_axis.monoid)
    if isinstance(data_axis, (Independent, BoundedHalo)):
        out: list[Any] = []
        for spec, piece in pieces:
            trimmed = piece[spec.halo :] if spec.halo > 0 else piece
            out.extend(trimmed)
        return out
    raise TypeError(f"cannot combine pieces for axis {type(data_axis).__name__}")


def _approx_equal(a: Any, b: Any, tol: float) -> bool:
    if dataclasses.is_dataclass(a) and not isinstance(a, type):
        a = dataclasses.asdict(a)
    if dataclasses.is_dataclass(b) and not isinstance(b, type):
        b = dataclasses.asdict(b)
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a == b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tol
    if isinstance(a, str) or isinstance(b, str):
        return bool(a == b)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_approx_equal(a[k], b[k], tol) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(_approx_equal(x, y, tol) for x, y in zip(a, b, strict=True))
    return bool(a == b)
