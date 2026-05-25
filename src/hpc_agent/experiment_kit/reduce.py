"""Monoid-reduce glue for :class:`~hpc_agent.experiment_kit.Associative` axes.

When a series axis is :class:`~hpc_agent.experiment_kit.Associative`, each
chunk emits a :class:`~hpc_agent.experiment_kit.Monoid` partial instead of a
final scalar. These helpers fold the partials back to the serial
result.

This sits next to :mod:`hpc_agent.models.mapreduce.reduce`, which reduces
per-task ``metrics.json`` sidecars by *weighted mean*. Weighted mean is
itself one specific monoid; :func:`reduce_monoid` generalises it to any
associative summary — the sufficient statistics needed for
non-associative aggregates (variance, Sharpe, QLIKE).

Stdlib-only.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from hpc_agent.experiment_kit.axis import Monoid

__all__ = ["reduce_monoid", "reduce_monoid_sidecars"]


def reduce_monoid(partials: Iterable[Any], monoid: Monoid) -> Any:
    """Fold *partials* with *monoid*, starting from its identity.

    Associativity means the fold order does not matter — the result is
    identical to the serial run regardless of how the chunks were
    scheduled.
    """
    acc = monoid.identity
    for p in partials:
        acc = monoid.combine(acc, p)
    return acc


def reduce_monoid_sidecars(
    result_dirs: Iterable[str | Path],
    monoid: Monoid,
    *,
    decode: Callable[[Any], Any] = lambda x: x,
    filename: str = "metrics.json",
) -> Any:
    """Read a monoid partial from each task's sidecar and fold them.

    Each task is expected to have written its partial to
    ``<result_dir>/<filename>`` as JSON. *decode* reconstructs the
    monoid element from the parsed JSON (e.g. ``lambda d: Moments(**d)``).
    Missing or corrupt sidecars are skipped — same tolerance as
    :func:`hpc_agent.models.mapreduce.reduce.reduce_metrics`.
    """
    partials: list[Any] = []
    for rdir in result_dirs:
        path = Path(rdir) / filename
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        partials.append(decode(raw))
    return reduce_monoid(partials, monoid)
