"""Auto-daisy-chain planning — survives the cluster's hard walltime ceiling.

A campus task whose walltime ask exceeds the cluster's hard scheduler
ceiling fails outright. Daisy-chaining splits the ask into N segments
where each segment N+1 starts only after segment N exits (regardless of
exit status — preempted segment N still triggers segment N+1, so the
chain works under PR-A's preemption protocol).

Default-off when checkpointing isn't detected so we don't silently
waste compute: a chained job whose stage-1 doesn't write checkpoints
dies on preemption and stage-2 starts from scratch. The detector in
``checkpoint_detect`` walks past run output dirs for checkpoint-shaped
files; only when it returns True (or the cluster yaml explicitly opts
in via ``auto_daisy_chain: true``) does the chain actually fire.

The 1h queue-wait buffer subtracted from the cluster's max walltime
absorbs scheduling variance between segments — a chain of N×24h
segments would routinely overlap the next-segment start with the
current segment's residual time without it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "QUEUE_WAIT_BUFFER_SEC",
    "DaisyChainPlan",
    "compute_daisy_chain_plan",
    "should_daisy_chain",
    "format_dependency_flag",
]

# Subtracted from ``max_walltime_sec`` when computing both the trigger
# threshold and the per-segment walltime. The 1h cushion absorbs
# queue-wait variance between segments.
QUEUE_WAIT_BUFFER_SEC = 3600


@dataclass(frozen=True)
class DaisyChainPlan:
    """Resolved chain plan for a (walltime_ask, cluster) pair.

    Attributes
    ----------
    n_segments : int
        Number of dependency-chained submissions. ``1`` means no chain
        was needed (ask fit within ``max_walltime_sec - 1h``).
    segment_walltime_sec : int
        Per-segment walltime ask sent to the scheduler. Always
        ``<= max_walltime_sec - QUEUE_WAIT_BUFFER_SEC`` so the segment
        fits with queue-wait headroom on the next segment.
    total_walltime_sec : int
        Original (un-chunked) ask, echoed back for caller convenience.
    """

    n_segments: int
    segment_walltime_sec: int
    total_walltime_sec: int


def should_daisy_chain(walltime_ask_sec: int, max_walltime_sec: int) -> bool:
    """Return True when the ask exceeds ``max_walltime_sec - 1h``.

    The 1h buffer absorbs queue-wait variance between segments, so the
    trigger threshold is ``max - 1h`` rather than ``max`` exactly.
    """
    return walltime_ask_sec > max_walltime_sec - QUEUE_WAIT_BUFFER_SEC


def compute_daisy_chain_plan(
    walltime_ask_sec: int,
    *,
    max_walltime_sec: int,
) -> DaisyChainPlan:
    """Compute the segment count and per-segment walltime for a chain.

    Splits *walltime_ask_sec* into ``ceil(ask / (max - 1h))`` segments.
    Each segment is sized at ``max - 1h`` (the per-segment cap) so the
    chain accommodates queue-wait variance without overlapping.

    No segment cap — a 7-day task on a 24h cluster becomes ~8 segments;
    a 30-day task becomes ~31. Bounded only by checkpointing actually
    working (which the detection layer enforces upstream of this
    helper).
    """
    if walltime_ask_sec <= 0:
        raise ValueError(f"walltime_ask_sec must be positive, got {walltime_ask_sec}")
    if max_walltime_sec <= QUEUE_WAIT_BUFFER_SEC:
        raise ValueError(
            f"max_walltime_sec ({max_walltime_sec}) must exceed the queue-wait buffer "
            f"({QUEUE_WAIT_BUFFER_SEC}); chain cannot make progress otherwise"
        )
    per_segment = max_walltime_sec - QUEUE_WAIT_BUFFER_SEC
    if walltime_ask_sec <= per_segment:
        # Not actually chained; the caller can read n_segments==1 as
        # "no chain needed" without checking should_daisy_chain again.
        return DaisyChainPlan(
            n_segments=1,
            segment_walltime_sec=walltime_ask_sec,
            total_walltime_sec=walltime_ask_sec,
        )
    n_segments = math.ceil(walltime_ask_sec / per_segment)
    return DaisyChainPlan(
        n_segments=n_segments,
        segment_walltime_sec=per_segment,
        total_walltime_sec=walltime_ask_sec,
    )


def format_dependency_flag(scheduler: str, prev_jobid: str) -> str:
    """Return the scheduler-specific dependency flag holding on *prev_jobid*.

    SLURM: ``--dependency=afterany:<id>`` (NOT ``afterok`` — preempted
    segment N exits 130 from PR-A's signal handler and that exit code
    must still trigger segment N+1; ``afterok`` would deadlock the
    chain). SGE: ``-hold_jid <id>``.

    Raises ``ValueError`` for unknown schedulers so a typo'd cluster
    yaml fails loudly rather than silently dropping the dependency.
    """
    s = (scheduler or "").lower()
    if s == "slurm":
        return f"--dependency=afterany:{prev_jobid}"
    if s == "sge":
        return f"-hold_jid {prev_jobid}"
    raise ValueError(
        f"daisy-chain dependency flag not implemented for scheduler {scheduler!r}; "
        f"expected 'slurm' or 'sge'"
    )
