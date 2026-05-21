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
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "ResourceTuple",
    "BackfillProbe",
    "CalibratedProbe",
    "CALIBRATION_FACTOR_FLOOR",
    "CALIBRATION_FACTOR_CEILING",
    "recommend_walltime_sec",
    "recommend_mem_mb",
    "recommend_cpus",
    "build_lattice",
    "probe_lattice",
    "pick_earliest",
    "calibrate_probes",
    "pick_earliest_calibrated",
    "reshape_array_size_for_backfill",
    "split_walltime_into_segments",
    "WalltimeSegments",
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

    usable = _gather_usable(quantiles, gpu_types_in_constraint, min_samples)

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


def recommend_mem_mb(
    mem_quantiles_mb: dict[str, dict[str, int]],
    gpu_types_in_constraint: list[str],
    *,
    user_default_mb: int,
    safety_mult: float = 1.50,
    floor_mb: int = 512,
    min_samples: int = 10,
    cold_start_buffer: float = 0.0,
    ceiling_mb: int | None = None,
) -> tuple[int, str]:
    """Recommend ``--mem`` in MB from the host-memory prior.

    Footprint shrinking is risky — under-asking host memory triggers an
    OOM kill, not a mere slowdown — so the defaults are conservative:
    ``safety_mult=1.50`` (50% pad over p95) and ``min_samples=10`` (twice
    the walltime threshold).

    Two regimes:

    * **Priors available** (``≥min_samples`` per GPU type) — quantile-
      based shrink: pick worst-case p95 across the constraint pool,
      multiply by ``safety_mult``, never grow past ``user_default_mb``.
      ``cold_start_buffer`` is *not* applied here; the priors already
      encode the right safety margin via walltime-drift calibration.
    * **Cold start** (no usable prior) — the campus user's first run on
      a fresh ``(profile, cluster, cmd_sha)`` has no idea how much
      memory the job will use. ``cold_start_buffer`` (0.0–N) multiplies
      the user's ask by ``(1 + cold_start_buffer)`` so the OOM daemon
      doesn't bump them mid-write and leave a corrupt result dir
      behind. Default ``0.0`` preserves legacy behavior; set
      ``cold_start_buffer=0.15`` for a 15% headroom pad.

    *user_default_mb* is the floor for the prior-driven path: we never
    *shrink-recommend* more than the user's ask. The cold-start buffer
    is the only path that grows the ask, and only when we genuinely
    have no prior to draw from.

    *ceiling_mb* (B-M5) is the cluster's per-node memory cap. When the
    grown buffer would exceed it, we clamp back down — survives the
    "240GB ask × 1.15 buffer = 276GB on a 256GB node → ReqNodeNotAvail
    forever" footgun. Only applied when set; default ``None`` preserves
    legacy behavior.
    """
    if user_default_mb <= 0:
        raise ValueError("user_default_mb must be positive")
    if safety_mult <= 0:
        raise ValueError("safety_mult must be positive")
    if cold_start_buffer < 0:
        raise ValueError("cold_start_buffer must be non-negative")
    if ceiling_mb is not None and ceiling_mb <= 0:
        raise ValueError("ceiling_mb must be positive when set")

    usable = _gather_usable(mem_quantiles_mb, gpu_types_in_constraint, min_samples)
    if not usable:
        if cold_start_buffer > 0:
            buffered = max(int(round(user_default_mb * (1.0 + cold_start_buffer))), floor_mb)
            if ceiling_mb is not None and buffered > ceiling_mb:
                clamped = max(ceiling_mb, floor_mb)
                return clamped, (
                    f"no usable mem prior (need ≥{min_samples} samples per GPU type); "
                    f"applied cold-start buffer ×{1.0 + cold_start_buffer:.2f} "
                    f"({user_default_mb}MB → {buffered}MB) then clamped to "
                    f"{clamped}MB (per-node cap) for OOM-daemon survival "
                    f"without ReqNodeNotAvail"
                )
            return buffered, (
                f"no usable mem prior (need ≥{min_samples} samples per GPU type); "
                f"applied cold-start buffer ×{1.0 + cold_start_buffer:.2f} "
                f"({user_default_mb}MB → {buffered}MB) for OOM-daemon survival"
            )
        # No buffer applied; still respect the per-node cap so a user-
        # default ask larger than the node never sits Pending.
        if ceiling_mb is not None and user_default_mb > ceiling_mb:
            clamped = max(ceiling_mb, floor_mb)
            return clamped, (
                f"no usable mem prior (need ≥{min_samples} samples per GPU type); "
                f"clamped user default {user_default_mb}MB → {clamped}MB "
                f"(per-node cap)"
            )
        return user_default_mb, (
            f"no usable mem prior (need ≥{min_samples} samples per GPU type); "
            f"kept user default {user_default_mb}MB"
        )

    worst_gpu, worst_p95, worst_n = max(usable, key=lambda x: x[1])
    raw = int(round(worst_p95 * safety_mult))
    # Only shrink: never recommend more than the user asked for.
    capped = min(raw, user_default_mb)
    clamped = max(capped, floor_mb)
    # Apply per-node cap last — even a prior-driven shrink could sit
    # above the cap if the user_default_mb already does (shouldn't
    # happen on a well-configured cluster, but make the behavior
    # consistent with the cold-start path).
    if ceiling_mb is not None and clamped > ceiling_mb:
        clamped = max(ceiling_mb, floor_mb)
        rationale = (
            f"p95×{safety_mult:.2f}, n={worst_n} {worst_gpu} samples; "
            f"clamped to {clamped}MB (per-node cap)"
        )
        return clamped, rationale
    if raw >= user_default_mb:
        rationale = (
            f"prior p95 ({worst_p95}MB) × {safety_mult:.2f} ≥ user default; "
            f"keeping user default {user_default_mb}MB"
        )
    else:
        rationale = (
            f"p95×{safety_mult:.2f}, n={worst_n} {worst_gpu} samples (was {user_default_mb}MB)"
        )
    return clamped, rationale


def recommend_cpus(
    cpu_cores_quantiles: dict[str, dict[str, int]],
    gpu_types_in_constraint: list[str],
    *,
    user_default_cpus: int,
    safety_pad: int = 1,
    floor_cpus: int = 1,
    min_samples: int = 10,
) -> tuple[int, str]:
    """Recommend ``--cpus-per-task`` from the cores-used prior.

    Adds *safety_pad* extra cores on top of the prior's p95 instead of a
    multiplicative safety margin, because integer core counts at low
    values (1–8) round noisily under a multiplier. Only shrinks (never
    asks for more than *user_default_cpus*). Returns the user default
    with a "no usable prior" rationale when fewer than *min_samples*
    samples exist.
    """
    if user_default_cpus <= 0:
        raise ValueError("user_default_cpus must be positive")

    usable = _gather_usable(cpu_cores_quantiles, gpu_types_in_constraint, min_samples)
    if not usable:
        return user_default_cpus, (
            f"no usable cpu prior (need ≥{min_samples} samples per GPU type); "
            f"kept user default {user_default_cpus} cores"
        )

    worst_gpu, worst_p95, worst_n = max(usable, key=lambda x: x[1])
    raw = max(floor_cpus, worst_p95 + max(0, safety_pad))
    capped = min(raw, user_default_cpus)
    if raw >= user_default_cpus:
        rationale = (
            f"prior p95 ({worst_p95}) + {safety_pad} ≥ user default; "
            f"keeping {user_default_cpus} cores"
        )
    else:
        rationale = (
            f"p95+{safety_pad}, n={worst_n} {worst_gpu} samples (was {user_default_cpus} cores)"
        )
    return capped, rationale


def _gather_usable(
    quantiles: dict[str, dict[str, int]],
    gpu_types: list[str],
    min_samples: int,
) -> list[tuple[str, int, int]]:
    """Collect ``(gpu, p95, n_samples)`` triples for GPU types that clear
    *min_samples*. Used by both recommend_mem_mb and recommend_cpus."""
    out: list[tuple[str, int, int]] = []
    for gpu in gpu_types:
        entry = quantiles.get(gpu)
        if not entry:
            continue
        n = int(entry.get("n_samples", 0))
        p95 = int(entry.get("p95", 0))
        if n >= min_samples and p95 > 0:
            out.append((gpu, p95, n))
    return out


def build_lattice(
    base: ResourceTuple,
    *,
    walltime_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0),
    mem_multipliers: tuple[float, ...] = (1.0,),
    walltime_ceiling_sec: int | None = None,
    mem_floor_mb: int = 512,
    max_probes: int = 12,
) -> list[ResourceTuple]:
    """Expand *base* into a probe lattice over walltime × mem multipliers.

    The mem axis defaults to a single point (``(1.0,)``) so the v1
    behavior — walltime-only sweep — is preserved when no mem
    recommendation is plumbed in. Pass ``mem_multipliers=(1.0, 1.5)`` to
    probe both the right-sized mem ask and a softer fallback. We do
    *not* sweep the cpus axis because integer core counts at low values
    round noisily under multiplication and the scheduler's
    ``--cpus-per-task`` rarely changes the predicted backfill window
    independently of mem.

    Deduplicates after clamps so redundant points collapse. Caps the
    output at *max_probes* to bound SSH round-trips.
    """
    out: list[ResourceTuple] = []
    seen: set[tuple[int, int]] = set()
    walltime_mults = walltime_multipliers or (1.0,)
    mem_mults = mem_multipliers or (1.0,)
    for wm in walltime_mults:
        if wm <= 0:
            continue
        wt = int(round(base.walltime_sec * wm))
        if walltime_ceiling_sec is not None:
            wt = min(wt, walltime_ceiling_sec)
        if wt <= 0:
            continue
        for mm in mem_mults:
            if mm <= 0:
                continue
            mem = max(int(round(base.mem_mb * mm)), mem_floor_mb)
            key = (wt, mem)
            if key in seen:
                continue
            seen.add(key)
            out.append(dataclasses.replace(base, walltime_sec=wt, mem_mb=mem))
            if len(out) >= max_probes:
                break
        if len(out) >= max_probes:
            break
    if not out:
        out.append(base)
    return out


# ─── array reshape ─────────────────────────────────────────────────────────


def reshape_array_size_for_backfill(
    *,
    current_max_array_size: int,
    target_window_sec: int | None,
    est_per_task_sec: int | None,
    floor_array_size: int = 1,
) -> tuple[int, str]:
    """Pick a smaller array size to make individual jobs more backfillable.

    SLURM schedules array elements largely independently for backfill
    purposes, but the array submission itself is one queue entry whose
    accounting and priority are coupled. Smaller arrays =⇒ more
    independent queue entries the scheduler can place into separate
    gaps. The trade-off is per-array submit overhead (~1s) and a higher
    sacct/scontrol footprint.

    Heuristic: if the user has supplied a *target_window_sec* (e.g.,
    they observed many 30-minute gaps on this cluster) and we know the
    per-task runtime, shrink ``max_array_size`` so each batch's
    *concurrent* slot demand fits the window. If we have neither piece
    of information, halve the current size as a mild reshape.

    Returns ``(new_size, rationale)``. The function never grows the
    array — it only reshapes downward.
    """
    if current_max_array_size <= floor_array_size:
        return current_max_array_size, (f"already at floor ({current_max_array_size}); no reshape")
    if target_window_sec and est_per_task_sec and est_per_task_sec > 0:
        # If per-task runtime already fits the target window, smaller
        # arrays only add overhead — skip the reshape.
        if est_per_task_sec <= target_window_sec:
            return current_max_array_size, (
                f"per-task runtime {est_per_task_sec}s already fits "
                f"target window {target_window_sec}s; no reshape"
            )
        # Pick a smaller array such that the total array footprint is
        # approximately one window's worth of concurrent slots.
        ratio = max(1, est_per_task_sec // max(1, target_window_sec))
        new_size = max(floor_array_size, current_max_array_size // (1 + ratio))
        return new_size, (
            f"reshape {current_max_array_size}→{new_size} "
            f"for {target_window_sec}s backfill window "
            f"(per-task ~{est_per_task_sec}s)"
        )
    # Fallback heuristic: mild halving to encourage finer-grained
    # backfill placement without requiring the user to know window sizes.
    new_size = max(floor_array_size, current_max_array_size // 2)
    return new_size, f"mild halving reshape ({current_max_array_size}→{new_size})"


# ─── walltime segment splitting (job splitting) ────────────────────────────


@dataclasses.dataclass(frozen=True)
class WalltimeSegments:
    """Plan for splitting a long walltime into chained shorter segments.

    Each segment is submitted as a separate job with
    ``--dependency=afterany:<prev>`` (afterany — not afterok — so a
    timeout boundary still triggers the next segment to resume from
    checkpoint). The *requires_checkpointing* field is a hard prereq:
    without application-level checkpoint/resume, a segment boundary
    just kills work and starts over from scratch.
    """

    n_segments: int
    segment_walltime_sec: int
    total_walltime_sec: int
    requires_checkpointing: bool
    rationale: str


def split_walltime_into_segments(
    walltime_sec: int,
    target_window_sec: int,
    *,
    max_segments: int = 8,
    floor_segment_sec: int = 600,
) -> WalltimeSegments:
    """Split *walltime_sec* into N segments each ≤ *target_window_sec*.

    Use case: a 6-hour job won't backfill on a cluster where most gaps
    are 30 minutes. Splitting into 12 chained 30-minute jobs lets each
    segment slot into a backfill window. The trade-off is dependency
    chaining overhead and the absolute requirement that the workload
    can checkpoint and resume.

    Floors each segment at *floor_segment_sec* (10 minutes) because
    sub-segments shorter than that spend most of their time on job
    spin-up. Caps the total at *max_segments* to keep the dependency
    chain manageable.
    """
    if walltime_sec <= 0:
        raise ValueError("walltime_sec must be positive")
    if target_window_sec <= 0:
        raise ValueError("target_window_sec must be positive")
    if walltime_sec <= target_window_sec:
        return WalltimeSegments(
            n_segments=1,
            segment_walltime_sec=walltime_sec,
            total_walltime_sec=walltime_sec,
            requires_checkpointing=False,
            rationale=(
                f"walltime {walltime_sec}s already fits target window "
                f"{target_window_sec}s; no split"
            ),
        )
    target = max(target_window_sec, floor_segment_sec)
    n = (walltime_sec + target - 1) // target  # ceil division
    n = min(n, max_segments)
    seg = (walltime_sec + n - 1) // n  # even split, ceil
    seg = max(seg, floor_segment_sec)
    total = seg * n
    return WalltimeSegments(
        n_segments=n,
        segment_walltime_sec=seg,
        total_walltime_sec=total,
        requires_checkpointing=True,
        rationale=(
            f"split {walltime_sec}s into {n} × {seg}s segments "
            f"(target backfill window {target_window_sec}s); "
            "REQUIRES executor-side checkpoint/resume"
        ),
    )


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

# Migrated to :class:`TTLCache` (B6). Same 60-second horizon and same
# (cluster, constraint, walltime_minute_bucket) key shape as the
# pre-refactor module-level dict; the generic cache adds bounded LRU
# eviction and a ``clear_all()`` test hook shared with infra.inspect.
from hpc_agent.infra.cache import TTLCache as _TTLCache  # noqa: E402

_PROBE_CACHE: _TTLCache[tuple[str, str, int, int, int], BackfillProbe] = _TTLCache(
    "job.backfill.probe", ttl_sec=60.0, max_size=512
)


def _cache_get(cluster_name: str, t: ResourceTuple) -> BackfillProbe | None:
    # mem_mb and cpus are part of the key — two ResourceTuples that
    # differ only in mem or cpu count would otherwise collide and
    # return a stale ETA from a different resource shape.
    key = (cluster_name, t.constraint, t.walltime_sec // 60, t.mem_mb, t.cpus)
    return _PROBE_CACHE.get(key)


def _cache_put(cluster_name: str, probe: BackfillProbe) -> None:
    t = probe.tuple_
    key = (cluster_name, t.constraint, t.walltime_sec // 60, t.mem_mb, t.cpus)
    _PROBE_CACHE.put(key, probe)


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


# Calibration helpers moved to backfill_calibration.py for navigability;
# re-export so any caller that imports from backfill keeps working.
from hpc_agent_pro.forecast.backfill_calibration import (  # noqa: E402
    CALIBRATION_FACTOR_CEILING,
    CALIBRATION_FACTOR_FLOOR,
    CalibratedProbe,
    calibrate_probes,
    pick_earliest_calibrated,
)
