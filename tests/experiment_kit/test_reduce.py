"""Monoid-reduce glue (Layer 2)."""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent.experiment_kit import (
    MOMENTS,
    SUM,
    Moments,
    reduce_monoid,
    reduce_monoid_sidecars,
)


def test_reduce_monoid_sum() -> None:
    assert reduce_monoid([1, 2, 3, 4], SUM) == 10


def test_reduce_monoid_empty_returns_identity() -> None:
    assert reduce_monoid([], SUM) == 0.0


def test_reduce_monoid_moments_recovers_serial_mean_and_variance() -> None:
    whole = Moments.of(range(20))
    combined = reduce_monoid([Moments.of(range(0, 10)), Moments.of(range(10, 20))], MOMENTS)
    assert combined == whole
    assert combined.mean == whole.mean


def test_reduce_monoid_sidecars(tmp_path: Path) -> None:
    dirs: list[Path] = []
    for i, chunk in enumerate(([1, 2], [3, 4], [5])):
        d = tmp_path / f"task_{i}"
        d.mkdir()
        m = Moments.of(chunk)
        (d / "metrics.json").write_text(json.dumps({"n": m.n, "total": m.total, "sumsq": m.sumsq}))
        dirs.append(d)
    combined = reduce_monoid_sidecars(dirs, MOMENTS, decode=lambda d: Moments(**d))
    assert combined == Moments.of([1, 2, 3, 4, 5])


def test_reduce_monoid_sidecars_skips_missing(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    (good / "metrics.json").write_text(json.dumps({"n": 1, "total": 5.0, "sumsq": 25.0}))
    missing = tmp_path / "missing"
    missing.mkdir()
    combined = reduce_monoid_sidecars([good, missing], MOMENTS, decode=lambda d: Moments(**d))
    assert combined == Moments(n=1, total=5.0, sumsq=25.0)
