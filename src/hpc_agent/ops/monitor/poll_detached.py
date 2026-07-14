"""``poll-detached`` — instant, non-blocking snapshot of a detached worker.

The read-only, MCP-safe complement to ``wait-detached``
(``ops/monitor/wait_detached.py``). ``wait-detached`` BLOCKS on a worker's lease
pid so the harness wakes the agent once at completion; over MCP that would wedge
the single-threaded in-process server, so it is refused there
(``_kernel/extension/mcp_server.py::_refuse_blocking_over_mcp``) and routed
through backgrounded Bash instead. ``poll-detached`` is the other half: one
INSTANT read of "where is this detached worker right now?", returning at once —
so an MCP caller (or any caller unwilling to hold a turn open) can observe a
detach-by-contract block (design §3, ``docs/design/human-amplification-blocks.md``)
without SSH and without blocking.

It fuses the three durable, cluster-free signals a detached worker leaves on
disk — reusing each source, never reimplementing it:

* the filesystem LEASE the launcher stamps
  (``_kernel/lifecycle/detached.py``: ``<verb>-<run_id>.lease.json`` under the
  global ``_detached/`` home, carrying the worker ``pid``) — read via
  ``detached._read_lease_holder_pid``; pid-liveness via the ONE liveness probe
  ``infra.proc.pid_alive`` (the same definition ``detached.pid_alive`` forwards
  to);
* the per-run JOURNAL status (``state.journal_poll.read_run_status``);
* the block TERMINAL record (``state.block_terminal.read_terminal_with_fallback``,
  migration-aware over the pre-2026-07-07 short-key window).

Zero SSH by construction: every read is a local filesystem stat/read, so the
snapshot is instant and safe on an unattended tick. Not detach-required over MCP
(it never blocks). ``verb="query"``, ``idempotent`` (writes nothing),
``side_effects=[]``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.poll_detached import PollDetachedInput, PollDetachedResult
from hpc_agent.cli._dispatch import CliShape, SchemaRef

# THE single PID-liveness definition (``docs/internals/engineering-principles.md``
# one-definition rule); imported at module scope so tests can monkeypatch it on
# THIS module's namespace. Light (psutil only) and SSH-free — importing it never
# pulls a transport module.
from hpc_agent.infra.proc import pid_alive

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="poll-detached",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    # A pure read: writes nothing, so re-running it is always safe.
    idempotent=True,
    cli=CliShape(
        help=(
            "Snapshot a detached worker's state right now (lease pid-liveness + "
            "journal status + block-terminal presence; local, no SSH, "
            "non-blocking). The instant, MCP-safe complement to wait-detached: "
            "reports running / exited_recorded / exited_unrecorded / no_lease."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=PollDetachedInput,
        schema_ref=SchemaRef(input="poll_detached"),
    ),
    agent_facing=True,
)
def poll_detached(*, experiment_dir: Path, spec: PollDetachedInput) -> PollDetachedResult:
    """Read the ``(run_id, block)`` detached worker's live state in one shot.

    Derives ``state`` from three cluster-free signals:

    * ``no_lease`` — the ``<block>-<run_id>.lease.json`` file is absent (worker
      never launched, or its lease was reclaimed).
    * ``running`` — the lease exists and its recorded pid is alive.
    * ``exited_recorded`` — the lease pid is dead AND a block terminal is on disk
      (a re-invoke replays it; read the terminal/journal for the verdict).
    * ``exited_unrecorded`` — the lease pid is dead but NO terminal was recorded
      (the run-#12 dead-worker gap; escalate to the doctor / re-arm).

    A present-but-corrupt lease counts as ``lease_present`` with ``pid=None`` →
    treated as an exited worker (a worker WAS launched), never as ``no_lease``.
    """
    from hpc_agent._kernel.lifecycle.detached import _read_lease_holder_pid
    from hpc_agent.state.block_terminal import read_terminal_with_fallback
    from hpc_agent.state.journal_poll import read_run_status
    from hpc_agent.state.run_record import current_homedir

    # Lease home is the GLOBAL journal home's ``_detached/`` (where the launcher
    # writes it), NOT the experiment tree — file-name convention owned by
    # ``detached._spawn_detached`` / ``_guard_single_lease``.
    detached_dir = current_homedir() / "_detached"
    lease_path = detached_dir / f"{spec.block}-{spec.run_id}.lease.json"
    lease_present = lease_path.exists()
    pid = _read_lease_holder_pid(detached_dir, spec.block, spec.run_id)
    alive = pid_alive(pid) if pid is not None else False

    # Independent durable signals — both experiment-tree reads, both fail-open
    # (missing/corrupt → None, never raising).
    journal_status = read_run_status(experiment_dir, spec.run_id).status
    terminal_recorded = (
        read_terminal_with_fallback(experiment_dir, spec.run_id, spec.block) is not None
    )

    state: Literal["running", "exited_recorded", "exited_unrecorded", "no_lease"]
    if not lease_present:
        state = "no_lease"
    elif alive:
        state = "running"
    elif terminal_recorded:
        state = "exited_recorded"
    else:
        state = "exited_unrecorded"

    return PollDetachedResult(
        run_id=spec.run_id,
        block=spec.block,
        lease_present=lease_present,
        pid=pid,
        pid_alive=alive,
        journal_status=journal_status,
        terminal_recorded=terminal_recorded,
        state=state,
        watch="journal",
    )
