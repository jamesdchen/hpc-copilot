"""Priority-queue drain simulator (lesson 7, integration layer).

Builds on :func:`claude_hpc.forecast.squeue_priority_field.parse_squeue_priority_field`'s
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
from datetime import datetime, timedelta
from typing import Literal

from claude_hpc.forecast.squeue_priority_field import QueuedJob


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
    """Parse an ISO timestamp; raises ValueError on garbage."""
    return datetime.fromisoformat(s)


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
) -> DrainResult:
    """Simulate the drain of *partition* and predict the hypothetical
    job's start time.

    Algorithm:

    1. Filter queue to *partition* only.
    2. Seed in-flight slots with running jobs' end times
       (``now + time_left_sec``). Running jobs without a usable
       ``time_left_sec`` are skipped (we have no idea when they end).
    3. Insert the hypothetical job into the pending list at its
       priority position; sort pending desc by priority.
    4. Loop: while pending non-hypo jobs exist and the front of the
       pending list isn't the hypothetical:
       - if a slot is free → pop the front, start it, schedule its
         end time;
       - else → advance ``now`` to the earliest scheduled end, free
         that slot.
    5. When the hypothetical reaches the front of the pending list,
       the next free slot is its start time.
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
            running_slots.append((datetime.max, r.job_id))
        else:
            running_slots.append((now + timedelta(seconds=r.time_left_sec), r.job_id))

    events: list[DrainEvent] = []

    def _walltime_for(job: QueuedJob) -> int:
        if job.job_id == _HYPO_JOB_ID:
            return hypothetical_walltime_sec
        if job.job_id in overrides:
            return overrides[job.job_id]
        return pending_walltime_default_sec

    while pending:
        front = pending[0]
        if len(running_slots) < partition_slot_count:
            # A slot is free; the front of the pending queue takes it.
            pending.pop(0)
            walltime = _walltime_for(front)
            end_dt = now + timedelta(seconds=walltime)
            running_slots.append((end_dt, front.job_id))
            if front.job_id == _HYPO_JOB_ID:
                events.append(
                    DrainEvent(
                        at_iso=now.isoformat(timespec="seconds"),
                        job_id=front.job_id,
                        kind="job_started",
                    )
                )
                return DrainResult(
                    hypothetical_starts_at_iso=now.isoformat(timespec="seconds"),
                    slots_pending_ahead=slots_pending_ahead,
                    events=tuple(events),
                )
            events.append(
                DrainEvent(
                    at_iso=now.isoformat(timespec="seconds"),
                    job_id=front.job_id,
                    kind="job_started",
                )
            )
            continue

        # No slot free; advance to the next end.
        running_slots.sort(key=lambda s: s[0])
        next_end_dt, next_end_id = running_slots.pop(0)
        if next_end_dt == datetime.max:
            # All running slots are indefinite — the hypothetical
            # never starts in this simulation horizon.
            return DrainResult(
                hypothetical_starts_at_iso=None,
                slots_pending_ahead=slots_pending_ahead,
                events=tuple(events),
            )
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
