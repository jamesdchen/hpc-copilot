"""Priority-queue drain simulator (lesson 7, integration layer).

Builds on :func:`hpc_agent_pro.forecast.squeue_priority_field.parse_squeue_priority_field`'s
parsed ``QueuedJob`` list. Simulates the partition's drain: as
running jobs end (their ``time_left_sec`` elapses), pending jobs
take their slots in priority order. The hypothetical job lands at
its rank-position; predicted start time falls out of the simulation.

Scope of this version:

* **Per-partition.** Pass one partition + its slot count; the sim
  runs against the subset of the queue in that partition.
* **First-fit FIFO by priority** — pending jobs land in priority
  order as slots open.
* **No backfill** — SLURM's backfill scheduler can land lower-
  priority jobs ahead of higher-priority ones in walltime shadows;
  this version doesn't model that. Adding backfill is a separate
  layer that needs the partition's ``BackfillScheduler`` config and
  per-job walltime estimates from the runtime prior. Without
  backfill the predicted start is a UPPER BOUND — actual start may
  be earlier in real life.
* **Pending walltimes default** to a caller-supplied value (typically
  the partition's ``DefaultTime`` or the user's runtime-prior p95).
  Per-job walltimes can override via ``pending_walltime_overrides``.

Returns a dataclass with the predicted start time + an event trace
the agent can render for diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from hpc_agent_pro.forecast.squeue_priority_field import QueuedJob


@dataclass(frozen=True)
class DrainEvent:
    """One step of the simulation timeline."""

    at_iso: str
    job_id: str
    kind: Literal["job_ended", "job_started"]


@dataclass(frozen=True)
class DrainResult:
    """Output of :func:`simulate_drain`.

    * ``hypothetical_starts_at_iso`` — predicted start of the
      hypothetical new job. ``None`` when the simulation didn't reach
      it within the available running-job timeline (caller should
      treat this as "starts no earlier than the latest event ISO").
    * ``slots_pending_ahead`` — how many pending jobs were ahead of
      the hypothetical at simulation start. Useful for the rank
      surface even when we can't predict a clock time.
    * ``events`` — chronological trace.
    """

    hypothetical_starts_at_iso: str | None
    slots_pending_ahead: int
    events: tuple[DrainEvent, ...] = field(default_factory=tuple)


_HYPO_JOB_ID = "__hypothetical__"


def _parse_iso(s: str) -> datetime:
    """Parse an ISO timestamp and force tz-aware UTC; raises ``ValueError`` on garbage.

    The simulator builds tz-aware sentinels (``datetime.max`` with UTC
    tzinfo) and would otherwise raise ``TypeError`` on naive-vs-aware
    comparisons. Naive inputs are treated as already-UTC.
    """
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def simulate_drain(
    *,
    now_iso: str,
    queue: list[QueuedJob],
    partition: str,
    partition_slot_count: int,
    hypothetical_priority: int,
    hypothetical_walltime_sec: int,
    pending_walltime_default_sec: int,
    pending_walltime_overrides: dict[str, int] | None = None,
    enable_backfill: bool = False,
) -> DrainResult:
    """Simulate the drain of *partition* and predict the hypothetical
    job's start time.

    Two modes:

    * **FIFO-by-priority** (default, ``enable_backfill=False``).
      Pending jobs claim slots strictly in priority order; when the
      front of the pending queue can't start (no slot free), the
      simulation waits for a running job to end. Predicted start is
      an UPPER BOUND for SLURM clusters where the backfill scheduler
      would land lower-priority jobs in walltime shadows.
    * **SLURM backfill** (``enable_backfill=True``). When a
      higher-priority pending job can't start now, the simulator
      computes its earliest possible start (the next-running-end) and
      checks lower-priority pendings for backfill candidates whose
      walltime fits in the shadow ``[now, earliest_start)``. By
      definition, backfill never delays a higher-priority job. This
      models SLURM's ``BackfillScheduler``; SGE / PBS schedule
      differently and the FIFO mode is the right approximation there.
      Per-job walltime estimates (via ``pending_walltime_overrides``
      or ``pending_walltime_default_sec``) drive the shadow-fit check.
    """
    if partition_slot_count < 1:
        return DrainResult(
            hypothetical_starts_at_iso=None,
            slots_pending_ahead=0,
        )

    overrides = pending_walltime_overrides or {}
    now = _parse_iso(now_iso)

    in_partition = [j for j in queue if j.partition == partition]
    running = [j for j in in_partition if j.state == "RUNNING"]
    pending_real = sorted(
        (j for j in in_partition if j.state == "PENDING"),
        key=lambda j: -j.priority,
    )

    # Insert the hypothetical at its priority rank.
    hypo = QueuedJob(
        job_id=_HYPO_JOB_ID,
        priority=hypothetical_priority,
        partition=partition,
        user="<hypothetical>",
        state="PENDING",
        time_left_sec=hypothetical_walltime_sec,
    )
    insert_idx = 0
    for j in pending_real:
        if j.priority > hypothetical_priority:
            insert_idx += 1
        else:
            break
    pending: list[QueuedJob] = list(pending_real)
    pending.insert(insert_idx, hypo)
    slots_pending_ahead = insert_idx

    # Seed running slots: (end_dt, job_id).
    running_slots: list[tuple[datetime, str]] = []
    for r in running:
        if r.time_left_sec is None or r.time_left_sec < 0:
            # Treat as occupying a slot indefinitely — never frees.
            # Distant-future sentinel keeps the comparison total.
            # Must carry the same tzinfo as `now` so the subsequent
            # `running_slots.sort()` doesn't mix naive and aware.
            running_slots.append((datetime.max.replace(tzinfo=timezone.utc), r.job_id))
        else:
            running_slots.append((now + timedelta(seconds=r.time_left_sec), r.job_id))

    events: list[DrainEvent] = []

    def _walltime_for(job: QueuedJob) -> int:
        """Resolve a pending job's expected walltime via fallback chain:
        hypothetical → caller override → squeue TimeLimit → partition default.
        """
        if job.job_id == _HYPO_JOB_ID:
            return hypothetical_walltime_sec
        if job.job_id in overrides:
            return overrides[job.job_id]
        if job.time_limit_sec is not None and job.time_limit_sec > 0:
            return job.time_limit_sec
        return pending_walltime_default_sec

    def _start_job(job: QueuedJob) -> None:
        """Move *job* out of ``pending`` into a running slot at the current ``now``."""
        pending.remove(job)
        walltime = _walltime_for(job)
        running_slots.append((now + timedelta(seconds=walltime), job.job_id))
        events.append(
            DrainEvent(
                at_iso=now.isoformat(timespec="seconds"),
                job_id=job.job_id,
                kind="job_started",
            )
        )

    while pending:
        front = pending[0]
        if len(running_slots) < partition_slot_count:
            # A slot is free; the front of the pending queue takes it.
            _start_job(front)
            if front.job_id == _HYPO_JOB_ID:
                return DrainResult(
                    hypothetical_starts_at_iso=now.isoformat(timespec="seconds"),
                    slots_pending_ahead=slots_pending_ahead,
                    events=tuple(events),
                )
            continue

        # No slot free. Compute the front-of-queue's earliest start
        # (when the next running slot frees) — this is the shadow
        # boundary for backfill candidates.
        running_slots.sort(key=lambda s: s[0])
        next_end_dt, next_end_id = running_slots[0]
        if next_end_dt == datetime.max.replace(tzinfo=timezone.utc):
            # All running slots are indefinite — the hypothetical
            # never starts in this simulation horizon.
            return DrainResult(
                hypothetical_starts_at_iso=None,
                slots_pending_ahead=slots_pending_ahead,
                events=tuple(events),
            )

        if enable_backfill:
            # Walk lower-priority pendings (skip ``front``) for
            # candidates whose walltime fits in the shadow window
            # ``[now, next_end_dt)``. By definition, a backfill
            # candidate finishes before ``next_end_dt`` and therefore
            # does not delay ``front``.
            #
            # Simplification vs. real SLURM: backfilled jobs run in a
            # "phantom slot" that does NOT compete with
            # ``running_slots`` for capacity. Real SLURM backfill
            # depends on multi-resource accounting (cpus, nodes,
            # GRES) per-job that this simulator does not model. The
            # phantom-slot approximation is safe for the headline
            # forecast question ("when does the hypothetical start?")
            # because the front-of-queue's start time is unchanged.
            # It WILL over-predict throughput for clusters where
            # backfill is genuinely capacity-limited.
            shadow_sec = max(int((next_end_dt - now).total_seconds()), 0)
            for j in list(pending[1:]):
                if _walltime_for(j) > shadow_sec:
                    continue
                pending.remove(j)
                events.append(
                    DrainEvent(
                        at_iso=now.isoformat(timespec="seconds"),
                        job_id=j.job_id,
                        kind="job_started",
                    )
                )
                if j.job_id == _HYPO_JOB_ID:
                    return DrainResult(
                        hypothetical_starts_at_iso=now.isoformat(timespec="seconds"),
                        slots_pending_ahead=slots_pending_ahead,
                        events=tuple(events),
                    )

        # Advance to the next running-end.
        running_slots.pop(0)
        now = next_end_dt
        events.append(
            DrainEvent(
                at_iso=now.isoformat(timespec="seconds"),
                job_id=next_end_id,
                kind="job_ended",
            )
        )

    # Pending exhausted without seeing the hypothetical — defensive,
    # shouldn't happen since we always insert it.
    return DrainResult(
        hypothetical_starts_at_iso=None,
        slots_pending_ahead=slots_pending_ahead,
        events=tuple(events),
    )


__all__ = ["DrainEvent", "DrainResult", "simulate_drain"]
