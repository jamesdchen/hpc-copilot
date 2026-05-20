"""Tests for ``forecast.drain_simulator.simulate_drain``.

Pure simulation; tests construct a synthetic queue + capacity and
assert the predicted start time + event trace.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hpc_agent.forecast.drain_simulator import simulate_drain
from hpc_agent.forecast.squeue_priority_field import QueuedJob

_NOW = "2026-04-15T10:00:00+00:00"
_NOW_DT = datetime(2026, 4, 15, 10, 0, 0, tzinfo=timezone.utc)


def _running(job_id: str, time_left_sec: int, partition: str = "gpu") -> QueuedJob:
    return QueuedJob(
        job_id=job_id,
        priority=999,
        partition=partition,
        user="u",
        state="RUNNING",
        time_left_sec=time_left_sec,
    )


def _pending(job_id: str, priority: int, partition: str = "gpu") -> QueuedJob:
    return QueuedJob(
        job_id=job_id,
        priority=priority,
        partition=partition,
        user="u",
        state="PENDING",
        time_left_sec=None,
    )


# ─── front of queue ───────────────────────────────────────────────────


def test_empty_partition_full_capacity_starts_immediately() -> None:
    """No running jobs, no pendings ahead → hypothetical starts at now."""
    out = simulate_drain(
        now_iso=_NOW,
        queue=[],
        partition="gpu",
        partition_slot_count=4,
        hypothetical_priority=100,
        hypothetical_walltime_sec=3600,
        pending_walltime_default_sec=3600,
    )
    assert out.hypothetical_starts_at_iso == _NOW.split("+")[0] + "+00:00"
    assert out.slots_pending_ahead == 0


def test_higher_priority_pending_blocks_hypothetical() -> None:
    """One pending with higher priority claims the only free slot;
    hypothetical waits."""
    queue = [
        _pending("a", priority=200),  # higher → claims first slot at now
    ]
    out = simulate_drain(
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        hypothetical_priority=100,  # below 200
        hypothetical_walltime_sec=3600,
        pending_walltime_default_sec=7200,  # 'a' runs 2h
    )
    # 'a' starts at now, runs 2h; hypothetical starts 2h later.
    expected = (_NOW_DT + timedelta(hours=2)).isoformat(timespec="seconds")
    assert out.hypothetical_starts_at_iso == expected
    assert out.slots_pending_ahead == 1


# ─── waiting for running jobs to drain ─────────────────────────────────


def test_running_jobs_drain_in_order_freeing_slots() -> None:
    """Two running jobs with different end times; hypothetical takes
    the slot of whichever ends first."""
    queue = [
        _running("r1", time_left_sec=1800),  # 30min
        _running("r2", time_left_sec=7200),  # 2h
    ]
    out = simulate_drain(
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=2,
        hypothetical_priority=100,
        hypothetical_walltime_sec=3600,
        pending_walltime_default_sec=3600,
    )
    # r1 frees its slot at now+30min; hypothetical starts then.
    expected = (_NOW_DT + timedelta(minutes=30)).isoformat(timespec="seconds")
    assert out.hypothetical_starts_at_iso == expected


def test_event_trace_records_drains_and_starts() -> None:
    queue = [_running("r1", time_left_sec=600)]
    out = simulate_drain(
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        hypothetical_priority=100,
        hypothetical_walltime_sec=3600,
        pending_walltime_default_sec=3600,
    )
    kinds = [(e.kind, e.job_id) for e in out.events]
    assert kinds == [("job_ended", "r1"), ("job_started", "__hypothetical__")]


# ─── priority insertion ───────────────────────────────────────────────


def test_hypothetical_inserted_at_priority_position() -> None:
    """Three pendings (priorities 300, 200, 50). Hypothetical priority
    150 lands behind 300 and 200, ahead of 50."""
    queue = [
        _pending("a", priority=300),
        _pending("b", priority=200),
        _pending("c", priority=50),
    ]
    out = simulate_drain(
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        hypothetical_priority=150,
        hypothetical_walltime_sec=3600,
        pending_walltime_default_sec=3600,
    )
    # 2 pendings (a, b) ahead.
    assert out.slots_pending_ahead == 2


# ─── partition isolation ───────────────────────────────────────────────


def test_other_partitions_dont_affect_simulation() -> None:
    """Running jobs on a different partition don't compete for slots."""
    queue = [
        _running("other", time_left_sec=99999, partition="cpu"),
        _running("here", time_left_sec=600, partition="gpu"),
    ]
    out = simulate_drain(
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        hypothetical_priority=100,
        hypothetical_walltime_sec=3600,
        pending_walltime_default_sec=3600,
    )
    expected = (_NOW_DT + timedelta(minutes=10)).isoformat(timespec="seconds")
    assert out.hypothetical_starts_at_iso == expected


# ─── degenerate / safety ───────────────────────────────────────────────


def test_zero_capacity_returns_no_start() -> None:
    """A partition with no slots can never run anything; predict None
    rather than spinning."""
    out = simulate_drain(
        now_iso=_NOW,
        queue=[],
        partition="gpu",
        partition_slot_count=0,
        hypothetical_priority=100,
        hypothetical_walltime_sec=3600,
        pending_walltime_default_sec=3600,
    )
    assert out.hypothetical_starts_at_iso is None


def test_indefinite_running_jobs_return_no_start() -> None:
    """Running jobs without ``time_left_sec`` are treated as
    indefinite — the simulation never frees their slots, so the
    hypothetical's start is unpredictable. Surface as None."""
    queue = [
        QueuedJob(
            job_id="r1",
            priority=999,
            partition="gpu",
            user="u",
            state="RUNNING",
            time_left_sec=None,  # no end time available
        ),
    ]
    out = simulate_drain(
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        hypothetical_priority=100,
        hypothetical_walltime_sec=3600,
        pending_walltime_default_sec=3600,
    )
    assert out.hypothetical_starts_at_iso is None


def test_pending_walltime_overrides_used_per_job() -> None:
    """If the caller has runtime-prior estimates per-job, they
    override the partition default. Verify a high-priority pending
    with a SHORT override frees its slot earlier than the default
    would predict."""
    queue = [_pending("a", priority=200)]  # ahead of hypo (priority 100)
    out = simulate_drain(
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        hypothetical_priority=100,
        hypothetical_walltime_sec=3600,
        pending_walltime_default_sec=86400,  # default 24h
        pending_walltime_overrides={"a": 600},  # but 'a' only runs 10min
    )
    expected = (_NOW_DT + timedelta(minutes=10)).isoformat(timespec="seconds")
    assert out.hypothetical_starts_at_iso == expected


# ─── backfill mode (lesson 7 integration) ──────────────────────────────


def test_backfill_lower_priority_short_job_lands_in_shadow() -> None:
    """The headline backfill case: front-of-queue (high priority)
    can't start now (no slot free); a lower-priority job with a SHORT
    walltime fits in the shadow before the running job ends. With
    backfill enabled, the short job starts NOW; without backfill, it
    waits behind the high-priority front."""
    queue = [
        _running("r1", time_left_sec=3600),  # blocks slot for 1h
        _pending("front", priority=999),  # high priority — needs the slot
        _pending("hypo_target", priority=50),  # would fit in the 1h shadow
    ]
    # Hypothetical priority 25 lands at the back of pending, behind
    # 'front' (999) AND 'hypo_target' (50). With backfill enabled, the
    # hypothetical's 30-minute walltime fits in r1's 1h shadow.
    out = simulate_drain(
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        hypothetical_priority=25,
        hypothetical_walltime_sec=1800,  # 30min — fits in 1h shadow
        pending_walltime_default_sec=86400,  # 'front' / 'hypo_target' run long
        enable_backfill=True,
    )
    # Without backfill, hypo would wait ~24h+ for 'front' and
    # 'hypo_target' to drain. With backfill, hypo starts NOW.
    assert out.hypothetical_starts_at_iso == _NOW.split("+")[0] + "+00:00"


def test_backfill_disabled_by_default_keeps_fifo_behavior() -> None:
    """``enable_backfill=False`` (the default) should reproduce the
    pre-backfill FIFO semantics. A lower-priority short pending does
    NOT jump ahead."""
    queue = [
        _running("r1", time_left_sec=3600),
        _pending("front", priority=999),
        _pending("hypo_target", priority=50),
    ]
    out = simulate_drain(
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        hypothetical_priority=25,
        hypothetical_walltime_sec=1800,
        pending_walltime_default_sec=86400,
        # enable_backfill defaults to False
    )
    # No backfill → hypo waits for 'front' + 'hypo_target' to drain.
    assert out.hypothetical_starts_at_iso != _NOW.split("+")[0] + "+00:00"


def test_backfill_phantom_slot_invariant() -> None:
    """The phantom-slot model's invariant: when backfill is enabled,
    the simulation can return AS SOON AS the hypothetical's start
    time is determined — it doesn't have to walk the entire timeline.
    The backfilled hypothetical lands at ``now``; ``front`` would
    start later when r1 ends. We don't trace that far because the
    headline forecast (when does hypo start) is already known."""
    queue = [
        _running("r1", time_left_sec=3600),
        _pending("front", priority=999),
    ]
    out = simulate_drain(
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        hypothetical_priority=25,
        hypothetical_walltime_sec=1800,
        pending_walltime_default_sec=7200,
        enable_backfill=True,
    )
    # Hypo backfills immediately — that's the headline answer.
    assert out.hypothetical_starts_at_iso == _NOW.split("+")[0] + "+00:00"
    # Phantom-slot semantics: backfilled jobs don't compete with the
    # main running queue, so 'front' is unaffected by the backfill
    # (its start time would still be +1h when r1 ends, but we don't
    # walk that far in the simulation).


def test_backfill_candidate_too_long_for_shadow_doesnt_run() -> None:
    """Lower-priority pending whose walltime EXCEEDS the shadow window
    must NOT backfill — it would delay the front-of-queue."""
    queue = [
        _running("r1", time_left_sec=600),  # 10min shadow
        _pending("front", priority=999),
        _pending("too_long", priority=50),
    ]
    out = simulate_drain(
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        hypothetical_priority=25,
        hypothetical_walltime_sec=86400,  # 24h — way too long for 10min shadow
        pending_walltime_default_sec=7200,  # 'too_long' runs 2h, can't fit either
        enable_backfill=True,
    )
    # Hypothetical's walltime far exceeds the 10min shadow → no
    # backfill; waits for r1 → front → too_long to drain.
    assert out.hypothetical_starts_at_iso != _NOW.split("+")[0] + "+00:00"


def test_backfill_uses_per_job_walltime_overrides() -> None:
    """The shadow-fit check honors per-job overrides — a job with a
    SHORT runtime-prior estimate can backfill where the partition
    default would say it can't."""
    queue = [
        _running("r1", time_left_sec=600),  # 10min shadow
        _pending("front", priority=999),
        _pending("would_fit", priority=50),
    ]
    # Default 1h would NOT fit in the 10min shadow; per-job override
    # of 5min DOES fit.
    out = simulate_drain(
        now_iso=_NOW,
        queue=queue,
        partition="gpu",
        partition_slot_count=1,
        hypothetical_priority=25,
        hypothetical_walltime_sec=300,  # 5min — fits 10min shadow
        pending_walltime_default_sec=3600,
        pending_walltime_overrides={"would_fit": 300},
        enable_backfill=True,
    )
    assert out.hypothetical_starts_at_iso == _NOW.split("+")[0] + "+00:00"
