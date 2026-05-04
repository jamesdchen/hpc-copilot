"""Adversarial-scheduler helpers: right-size resources and probe backfill gaps.

SLURM's backfill scheduler will run a pending job ahead of higher-priority
ones if and only if the job fits cleanly into a known-size gap. A 6h ask
walks past every sub-hour gap the cluster opens up, so generous walltimes
are the single biggest reason a job sits in the queue. This module turns
the runtime priors we already collect into right-sized walltime
recommendations and probes ``sbatch --test-only`` over a small lattice of
``(walltime, constraint)`` tuples to find the variant the scheduler
predicts will start earliest.

Three responsibilities:

1. **Right-size walltime** (:func:`recommend_walltime_sec`) — pick the
   smallest walltime that is still safely above the runtime prior, with
   floor/ceiling clamps and a min-samples guard. Returns ``(seconds,
   rationale)`` so the slash command can surface the *why* to the user.
2. **Build a probe lattice** (:func:`build_lattice`) — expand a base
   ``ResourceTuple`` over a small set of walltime multipliers.
3. **Probe and pick** (:func:`probe_lattice`, :func:`pick_earliest`) —
   run ``sbatch --test-only`` for each tuple in parallel over SSH and
   return the one with the earliest predicted start.

This module deliberately does not touch memory or CPU sizing in v1 —
those need a sacct-based prior that is not yet plumbed through
``runtime_prior.append_sample``.
"""

from __future__ import annotations

import dataclasses
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "ResourceTuple",
    "BackfillProbe",
    "recommend_walltime_sec",
    "build_lattice",
    "probe_lattice",
    "pick_earliest",
]


@dataclasses.dataclass(frozen=True)
class ResourceTuple:
    """One point in the (resource × constraint) probe lattice."""

    constraint: str
    walltime_sec: int
    mem_mb: int = 1024
    cpus: int = 1


@dataclasses.dataclass(frozen=True)
class BackfillProbe:
    """Result of one ``sbatch --test-only`` probe."""

    tuple_: ResourceTuple
    eta_sec: int | None
    raw_test_only: str


def recommend_walltime_sec(
    quantiles: dict[str, dict[str, int]],
    gpu_types_in_constraint: list[str],
    *,
    safety_mult: float = 1.30,
    floor_sec: int = 600,
    ceiling_sec: int | None = None,
    fallback_sec: int = 4 * 3600,
    min_samples: int = 5,
) -> tuple[int, str]:
    """Recommend a walltime from runtime-prior quantiles.

    *quantiles* is the ``"quantiles"`` field of
    :func:`runtime_prior.roll_up_quantiles`'s output: a per-GPU-type
    dict of ``{p50, p95, p99, n_samples, ...}`` entries in seconds.

    Picks the **maximum** p95 across the GPU types in the candidate's
    constraint pool (the scheduler may land us on the slowest type), then
    multiplies by *safety_mult*. The result is clamped to
    ``[floor_sec, ceiling_sec]``. If no GPU type in the constraint has at
    least *min_samples* samples we return *fallback_sec* with a rationale
    so the caller can show "no usable prior" to the user instead of
    silently using a too-tight value.

    Returns ``(seconds, rationale)``. The rationale is a one-line human
    string ("p95×1.30, n=23 a100 samples") for surfacing in the submit
    interview.
    """
    if safety_mult <= 0:
        raise ValueError("safety_mult must be positive")
    if floor_sec < 0:
        raise ValueError("floor_sec must be non-negative")

    usable: list[tuple[str, int, int]] = []  # (gpu, p95, n)
    for gpu in gpu_types_in_constraint:
        entry = quantiles.get(gpu)
        if not entry:
            continue
        n = int(entry.get("n_samples", 0))
        p95 = int(entry.get("p95", 0))
        if n >= min_samples and p95 > 0:
            usable.append((gpu, p95, n))

    if not usable:
        rationale = (
            f"no usable prior (need ≥{min_samples} samples per GPU type); "
            f"falling back to {fallback_sec}s"
        )
        clamped = _clamp(fallback_sec, floor_sec, ceiling_sec)
        return clamped, rationale

    # Worst-case across the constraint pool: the scheduler may pick the
    # slowest type, so we size for it.
    worst_gpu, worst_p95, worst_n = max(usable, key=lambda x: x[1])
    raw = int(round(worst_p95 * safety_mult))
    clamped = _clamp(raw, floor_sec, ceiling_sec)
    pieces = [f"p95×{safety_mult:.2f}", f"n={worst_n} {worst_gpu} samples"]
    if clamped != raw:
        pieces.append(f"clamped from {raw}s")
    rationale = ", ".join(pieces)
    return clamped, rationale


def _clamp(value: int, floor_sec: int, ceiling_sec: int | None) -> int:
    out = max(value, floor_sec)
    if ceiling_sec is not None:
        out = min(out, ceiling_sec)
    return out


def build_lattice(
    base: ResourceTuple,
    *,
    walltime_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0),
    walltime_ceiling_sec: int | None = None,
) -> list[ResourceTuple]:
    """Expand *base* into a list of ResourceTuples over walltime multipliers.

    Deduplicates after the ceiling clamp so a multiplier whose product
    exceeds the ceiling collapses onto the ceiling-clamped tuple rather
    than producing a redundant probe. Always emits at least one tuple
    (the base itself) even if *walltime_multipliers* is empty.
    """
    out: list[ResourceTuple] = []
    seen: set[int] = set()
    multipliers = walltime_multipliers or (1.0,)
    for m in multipliers:
        if m <= 0:
            continue
        wt = int(round(base.walltime_sec * m))
        if walltime_ceiling_sec is not None:
            wt = min(wt, walltime_ceiling_sec)
        if wt <= 0 or wt in seen:
            continue
        seen.add(wt)
        out.append(dataclasses.replace(base, walltime_sec=wt))
    if not out:
        out.append(base)
    return out


def probe_lattice(
    lattice: list[ResourceTuple],
    probe_fn: Callable[[ResourceTuple], BackfillProbe],
    *,
    max_parallel: int = 4,
) -> list[BackfillProbe]:
    """Run *probe_fn* over each tuple in *lattice* with bounded parallelism.

    The injected *probe_fn* is responsible for any SSH + ``sbatch
    --test-only`` plumbing — keeping it as a parameter lets unit tests
    exercise the threadpool fan-out without faking the network layer.
    Order is preserved: ``out[i]`` corresponds to ``lattice[i]``.
    """
    if not lattice:
        return []
    workers = max(1, min(max_parallel, len(lattice)))
    if workers == 1:
        return [probe_fn(t) for t in lattice]
    results: list[BackfillProbe | None] = [None] * len(lattice)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe_fn, t): i for i, t in enumerate(lattice)}
        for fut in futures:
            i = futures[fut]
            results[i] = fut.result()
    return [r for r in results if r is not None]


def pick_earliest(probes: list[BackfillProbe]) -> BackfillProbe | None:
    """Return the probe with the smallest non-``None`` ETA, or ``None``.

    Ties are broken by preferring the smaller walltime ask (we want the
    tightest viable request, since it leaves the most cluster headroom
    for the next backfill window). When every ETA is ``None`` we return
    ``None`` rather than guessing — the caller should fall back to its
    pre-adversarial behavior.
    """
    eligible = [p for p in probes if isinstance(p.eta_sec, int)]
    if not eligible:
        return None
    return min(eligible, key=lambda p: (p.eta_sec, p.tuple_.walltime_sec))


# ─── 60s in-process probe cache ────────────────────────────────────────────
#
# Mirrors infra.inspect's ClusterSnapshot caching discipline. The cache key
# buckets walltime by the nearest minute so two probes that differ by a few
# seconds — e.g. from rounding in build_lattice — share a result. The cache
# is keyed by (cluster_name, constraint, walltime_minute_bucket).

_PROBE_CACHE: dict[tuple[str, str, int], tuple[float, BackfillProbe]] = {}
_PROBE_CACHE_TTL_SEC: float = 60.0


def _cache_get(cluster_name: str, t: ResourceTuple) -> BackfillProbe | None:
    key = (cluster_name, t.constraint, t.walltime_sec // 60)
    hit = _PROBE_CACHE.get(key)
    if hit is None:
        return None
    written_at, probe = hit
    if time.monotonic() - written_at > _PROBE_CACHE_TTL_SEC:
        _PROBE_CACHE.pop(key, None)
        return None
    return probe


def _cache_put(cluster_name: str, probe: BackfillProbe) -> None:
    key = (cluster_name, probe.tuple_.constraint, probe.tuple_.walltime_sec // 60)
    _PROBE_CACHE[key] = (time.monotonic(), probe)


def clear_probe_cache() -> None:
    """Drop the in-process probe cache. Intended for tests."""
    _PROBE_CACHE.clear()


def cached_probe(
    cluster_name: str, probe_fn: Callable[[ResourceTuple], BackfillProbe]
) -> Callable[[ResourceTuple], BackfillProbe]:
    """Wrap *probe_fn* with the 60s per-cluster probe cache.

    Used by the planner so a re-call within the cache TTL doesn't re-issue
    expensive ``sbatch --test-only`` round trips for the same lattice.
    """

    def wrapped(t: ResourceTuple) -> BackfillProbe:
        cached = _cache_get(cluster_name, t)
        if cached is not None:
            return cached
        result = probe_fn(t)
        _cache_put(cluster_name, result)
        return result

    return wrapped
