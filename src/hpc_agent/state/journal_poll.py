"""Journal-read polling helper — learn a run's outcome WITHOUT touching the cluster.

The connection-storm lesson (the 0.10.63 ban): an LLM that *sits in the
connection loop* — submits, schedules a wake-up, polls SSH in prose — is the
hazard. The deterministic composites (``status-pipeline`` / ``submit-pipeline``)
already own the connection and run to terminal in plain code; the fix this
module supports is to run that composite in a DETACHED subprocess of the CLI
(see :mod:`hpc_agent._kernel.lifecycle.detached`) and have the orchestrator
learn the outcome by *reading the journal*, never by opening its own SSH.

So this helper is deliberately **read-only and cluster-free**: it reads the
per-run journal record (``~/.claude/hpc/<repo_hash>/runs/<run_id>.json``) — the
same on-disk state ``monitor_flow`` writes as it polls — and reports whether the
run has reached a terminal :class:`JournalStatus`. The orchestrator loops over
:func:`poll_until_terminal` (or its own ``sleep`` + :func:`read_run_status`)
while the detached runner drives the connection; the model is out of the loop,
exactly as :mod:`hpc_agent.infra.retry` states the principle.

Why the JOURNAL status, not the monitor-flow ``lifecycle_state`` envelope: a
timed-out run's cluster jobs may still be live, so monitor-flow returns
``lifecycle_state='timeout'`` but the journal record stays ``in_flight`` (see
``state.journal._RESUBMITTABLE_TERMINAL_STATUSES``). The journal status is the
durable "is this run done?" signal the submit/dedup paths already key off, so
the poller keys off it too — a timed-out-but-still-live run is correctly NOT
terminal here, and the caller keeps waiting (or re-arms the detached runner).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hpc_agent._kernel.contract.vocabulary import TERMINAL_STATUSES
from hpc_agent.state.journal import load_run

__all__ = [
    "RunStatusSnapshot",
    "read_run_status",
    "poll_until_terminal",
]


@dataclass(frozen=True)
class RunStatusSnapshot:
    """A single read of a run's journal status — cluster-free.

    ``status`` is the journal :class:`JournalStatus` value (``in_flight`` /
    ``complete`` / ``failed`` / ``abandoned``), or ``None`` when no record
    exists yet (the detached runner may not have written it). ``terminal`` is
    True iff ``status`` is one of :data:`TERMINAL_STATUSES`. ``found`` is False
    only when the record is missing/unreadable.
    """

    run_id: str
    status: str | None
    terminal: bool
    found: bool


def read_run_status(experiment_dir: Path, run_id: str) -> RunStatusSnapshot:
    """Read *run_id*'s current journal status — one read, no SSH, no poll loop.

    Returns a :class:`RunStatusSnapshot`. A missing/unreadable record yields
    ``found=False`` (``status=None``, ``terminal=False``) rather than raising —
    the detached runner may not have created the record yet, which the caller
    treats as "keep waiting", not an error.
    """
    record = load_run(experiment_dir, run_id)
    if record is None:
        return RunStatusSnapshot(run_id=run_id, status=None, terminal=False, found=False)
    status = record.status
    return RunStatusSnapshot(
        run_id=run_id,
        status=status,
        terminal=status in TERMINAL_STATUSES,
        found=True,
    )


def poll_until_terminal(
    experiment_dir: Path,
    run_id: str,
    *,
    poll_interval_seconds: float = 2.0,
    timeout_seconds: float = 86400.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> RunStatusSnapshot:
    """Poll the journal until *run_id* is terminal or *timeout_seconds* elapses.

    Reads the journal record every *poll_interval_seconds* and returns as soon
    as the run reaches a terminal :class:`JournalStatus`. NEVER touches the
    cluster — the detached deterministic runner owns the connection and writes
    the journal; this only reads it. Returns the last :class:`RunStatusSnapshot`
    when the local *timeout_seconds* budget elapses first (``terminal=False``);
    the caller decides whether to re-arm the runner or give up.

    *sleep* and *now* are injectable so a test can drive the loop with no real
    time elapsed (mirrors :func:`hpc_agent.infra.retry.run_with_retry`).

    A negative or zero *poll_interval_seconds* is clamped to a small floor so a
    misconfigured caller can't busy-spin on the journal directory.

    Rank 21 (poll-loop constant hygiene): the default cadence is 2s, matching the
    pid-based sibling ``ops.monitor.wait_detached`` (``wait-detached``, a 2s pid
    probe). The read is a local millisecond journal load, so a 30s interval bought
    nothing but up to 30s of dead air between the detached runner's terminal write
    and the orchestrator noticing it. The EVENT-DRIVEN rendezvous — block on the
    detached worker's lease pid, then read the journal ONCE — is
    ``wait-detached``; this timed poller is the cluster-free fallback for callers
    that cannot await a pid.
    """
    interval = max(float(poll_interval_seconds), 1.0)
    deadline = now() + max(float(timeout_seconds), 0.0)
    while True:
        snap = read_run_status(experiment_dir, run_id)
        if snap.terminal:
            return snap
        if now() >= deadline:
            return snap
        sleep(interval)
