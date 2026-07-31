"""Incremental-harvest streaming gate — WHEN the watch pulls finished results.

U4 (``docs/plans/trainwreck-audit-2026-07-30.md``). Harvest today is
all-or-nothing at array completion: the 2026-07-30 forensic run left 1741 of
2100 finished task results on cluster scratch, unreadable for 2h+, while the
human asked "why aren't you streaming the results back?". The bytes existed;
nothing moved them until the last task landed.

This module owns the *policy* half of the fix — the pure, testable decision of
whether THIS tick should stream — while the *mechanism* half (the pull itself)
stays exactly one function, the aggregate-side
:func:`hpc_agent.ops.aggregate_flow.prefetch_per_task_results`, which pulls the
same shape into the same mirror the terminal harvest re-verifies. Never a
second pull engine.

Three properties the split buys:

* **Cluster etiquette is a pure function.** The batch threshold, the staleness
  interval and the hard spacing floor are computed off numbers the poll loop
  already holds — no clock reads, no I/O, no ssh.
* **The breaker pauses the stream, not the watch.** :func:`stream_blocked_by`
  is a READ-ONLY consult of the per-host circuit
  (:func:`~hpc_agent.infra.ssh_circuit.effective_state_for_host`). An open
  circuit means "skip this tick's stream", never an exception and never a
  claim on the half-open probe slot — the watch's own poll owns that slot.
* **Bytes only.** Nothing here reduces, aggregates or decides. Partial-set
  aggregation stays gated exactly as today (``decide-partial-handling`` and
  ``aggregate-run``'s terminal-or-explicitly-partial invariant own it).

Opting out: ``HPC_INCREMENTAL_HARVEST=0`` (metered/paid links, cluster
etiquette emergencies) or, per run, ``MonitorFlowSpec.incremental_harvest``
— the spec wins over the env, and both are pure latency knobs: a run with
streaming off harvests exactly as it does today, just later.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = [
    "INCREMENTAL_HARVEST_BATCH_ENV",
    "INCREMENTAL_HARVEST_ENV",
    "INCREMENTAL_HARVEST_FLOOR_ENV",
    "INCREMENTAL_HARVEST_INTERVAL_ENV",
    "STREAM_BATCH_TASKS",
    "STREAM_MIN_INTERVAL_SEC",
    "STREAM_SPACING_FLOOR_SEC",
    "incremental_harvest_enabled",
    "render_stream_lag",
    "should_stream",
    "stream_batch_tasks",
    "stream_blocked_by",
    "stream_disclosure",
    "stream_min_interval_sec",
    "stream_spacing_floor_sec",
]

#: Master opt-out. ``"0"`` disables streaming for every run in the process.
#: Shared spelling with :data:`hpc_agent.ops.aggregate_flow.PER_TASK_PREFETCH_ENV`
#: so the mechanism and the policy can never disagree about which switch is off
#: (the pull itself re-checks it, defence in depth for a direct caller).
INCREMENTAL_HARVEST_ENV = "HPC_INCREMENTAL_HARVEST"

#: How many NEWLY-complete tasks accrue before a stream fires on size alone.
#: 25 is a deliberate compromise: on the trainwreck's ~44 s/task × 2100 array
#: that is a pull roughly every couple of minutes at full width — frequent
#: enough that the mirror is never hours behind, sparse enough that a login
#: node never sees a pull per poll.
STREAM_BATCH_TASKS = 25
INCREMENTAL_HARVEST_BATCH_ENV = "HPC_INCREMENTAL_HARVEST_BATCH"

#: How long a NON-empty backlog may sit unstreamed before a stream fires on
#: staleness alone. Bounds the lag for a slow trickle (a 3-task-per-hour tail
#: would otherwise never reach the batch threshold).
STREAM_MIN_INTERVAL_SEC = 300.0
INCREMENTAL_HARVEST_INTERVAL_ENV = "HPC_INCREMENTAL_HARVEST_INTERVAL_SEC"

#: Hard minimum spacing between two streams, applied to BOTH triggers. A burst
#: completion (10k tasks finishing in one tick) must not turn the batch
#: threshold into a pull-per-poll storm.
STREAM_SPACING_FLOOR_SEC = 60.0
INCREMENTAL_HARVEST_FLOOR_ENV = "HPC_INCREMENTAL_HARVEST_FLOOR_SEC"


def _env_int(name: str, default: int) -> int:
    """Read a positive int env var, falling back to *default* on unset/invalid.

    Fail-safe like :func:`hpc_agent.ops.monitor_flow._env_float`: a typo in an
    operator's shell degrades to the shipped default rather than disabling the
    stream or hammering the cluster.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def _env_float(name: str, default: float) -> float:
    """Read a non-negative float env var, falling back on unset/invalid.

    ``0`` IS accepted here (unlike :func:`_env_int`): zeroing an interval or
    the spacing floor is a meaningful operator request ("stream every tick
    that has anything new"), whereas a zero batch size has no meaning.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val >= 0 else default


def _host_of(ssh_target: str) -> str:
    """Host key for *ssh_target* — the same ``user@host`` normalization
    :func:`hpc_agent.infra.ssh_circuit._host` and the throttle use, so the
    read seam keys the same doc the breaker writes. Local copy rather than a
    private cross-module import, matching ``ops/submit_flow._host_of``.
    """
    return ssh_target.rsplit("@", 1)[-1].strip()


def stream_batch_tasks() -> int:
    """Newly-complete-task threshold for a size-triggered stream."""
    return _env_int(INCREMENTAL_HARVEST_BATCH_ENV, STREAM_BATCH_TASKS)


def stream_min_interval_sec() -> float:
    """Seconds a non-empty backlog may sit before a staleness-triggered stream."""
    return _env_float(INCREMENTAL_HARVEST_INTERVAL_ENV, STREAM_MIN_INTERVAL_SEC)


def stream_spacing_floor_sec() -> float:
    """Hard minimum seconds between two streams (applies to both triggers)."""
    return _env_float(INCREMENTAL_HARVEST_FLOOR_ENV, STREAM_SPACING_FLOOR_SEC)


def incremental_harvest_enabled(
    spec_flag: bool | None = None, *, backend: str | None = None
) -> bool:
    """Whether the watch should stream finished results mid-flight.

    The ONE enablement decision, so no caller can assemble a different one:

    * *backend* — a pure-API backend has no remote tree to stream FROM (its
      results arrive through ``fetch_results``), so streaming is off by
      construction rather than by a failed pull. Checked FIRST: neither knob
      can turn on a capability the backend does not have.
    * *spec_flag* — ``MonitorFlowSpec.incremental_harvest``. ``None`` (the
      default) defers to the env; an explicit ``True``/``False`` wins over it.
      The per-run knob has to win — a metered-link run and a fat-pipe run
      share one operator shell, and the RUN is the thing that knows.
    * the env — ``HPC_INCREMENTAL_HARVEST=0`` is the process-wide opt-out.
    """
    if backend is not None:
        from hpc_agent.infra.backends import backend_requires_ssh

        if not backend_requires_ssh(backend):
            return False
    if spec_flag is not None:
        return bool(spec_flag)
    return os.environ.get(INCREMENTAL_HARVEST_ENV) != "0"


def should_stream(
    *,
    complete: int,
    last_streamed_complete: int,
    seconds_since_last: float,
    batch: int | None = None,
    min_interval: float | None = None,
    spacing_floor: float | None = None,
) -> bool:
    """Whether THIS tick should stream the finished per-task results.

    Pure. Every input is a number the poll loop already holds:

    * *complete* — this tick's cumulative complete count.
    * *last_streamed_complete* — the complete count at the last SUCCESSFUL
      stream, or a negative sentinel when none has run. This is what makes
      the pull never double-work: an unchanged count is not a backlog, so an
      idle tail streams exactly zero times no matter how long it idles.
    * *seconds_since_last* — monotonic seconds since the last stream ATTEMPT
      (attempt, not success: a failed pull must be spaced too, or a broken
      host gets hammered every tick).

    Fires when there IS a backlog and either trigger is satisfied — size
    (``new >= batch``) or staleness (``elapsed >= min_interval``) — and the
    hard spacing floor has lapsed. The floor gates both triggers, so a burst
    completion cannot turn the batch threshold into a pull-per-poll storm.
    """
    if complete <= 0:
        return False
    new = complete - max(0, last_streamed_complete)
    if new <= 0:
        return False
    if seconds_since_last < (
        stream_spacing_floor_sec() if spacing_floor is None else spacing_floor
    ):
        return False
    if new >= (stream_batch_tasks() if batch is None else batch):
        return True
    return seconds_since_last >= (
        stream_min_interval_sec() if min_interval is None else min_interval
    )


def stream_blocked_by(ssh_target: str | None) -> str | None:
    """Why the stream must skip this tick, or ``None`` when it may run.

    READ-ONLY consult of the per-host breaker. An OPEN circuit means the host
    is in cooldown: the watch's own poll owns the sanctioned half-open probe
    slot, and an opportunistic byte-mover must never spend it — so the stream
    PAUSES (a disclosed skip on the tick row) and the watch continues
    untouched. ``half_open_eligible`` also pauses: the probe belongs to the
    poll, not to us.

    Fail-open by construction — :func:`effective_state_for_host` reads an
    absent/unreadable circuit doc as ``closed`` — so a breaker-state read can
    never be the thing that silently stops streaming.
    """
    if not ssh_target:
        return "no_ssh_target"
    from hpc_agent.infra.ssh_circuit import effective_state_for_host

    state = effective_state_for_host(_host_of(ssh_target))
    if state == "closed":
        return None
    return f"ssh_circuit_{state}"


def render_stream_lag(complete: int, mirrored: int, total: int | None = None) -> str:
    """The honest one-line pull-lag disclosure: ``N complete, M pulled locally``.

    HONEST means the gap is stated, never smoothed: *mirrored* is counted off
    the local mirror, so a stream that is paused, opted out, or simply behind
    reads as a visible shortfall rather than as silence. When the mirror is
    ahead of the census (markers are written before the results settle, and a
    sibling run may have warmed the mirror) the lag line says so instead of
    rendering a negative backlog.
    """
    head = f"{complete} complete" + (f"/{total}" if total else "")
    line = f"{head}, {mirrored} pulled locally"
    lag = complete - mirrored
    if lag > 0:
        return f"{line} ({lag} not yet pulled)"
    return line


def stream_disclosure(state: Any) -> dict[str, Any]:
    """Project the loop's streaming counters into the brief/envelope block.

    One shape for every consumer (the ``MonitorFlowResult`` envelope, the S3
    brief, the monitor summary line), so no surface can invent its own
    accounting of what was streamed.
    """
    return {
        "enabled": bool(getattr(state, "stream_enabled", False)),
        "pulls": int(getattr(state, "stream_pulls", 0)),
        "files_pulled": int(getattr(state, "stream_files", 0)),
        "bytes_pulled": int(getattr(state, "stream_bytes", 0)),
        "tasks_mirrored": int(getattr(state, "stream_tasks_mirrored", 0)),
        "paused_reason": getattr(state, "stream_paused_reason", None),
        "last_error": getattr(state, "stream_error", None),
    }
