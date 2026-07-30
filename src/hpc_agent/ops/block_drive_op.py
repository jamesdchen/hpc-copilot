"""``block-drive`` primitive — the agent-facing wrapper over the wave-4 tick.

Thin ``@primitive`` boilerplate around
:func:`hpc_agent._kernel.lifecycle.block_drive.run_tick` (mirroring the block-op
modules ``ops/status_blocks.py`` / ``ops/aggregate_blocks.py``). Parse the wire
spec, run one tick, return the :class:`BlockDriveResult`.

The tick itself is deliberately NOT a pure JSON-in/JSON-out atom — it *drives*:
it chains deterministic block spans in code (spawning ``hpc-agent <verb>``
subprocesses) and parks at a human decision point by writing the journal's
pending-decision marker (block-drive.md §2–§5). This wrapper exists only to give
that driver a registry-native, agent-facing surface (and the MCP projection); the
console-script :func:`hpc_agent._kernel.lifecycle.block_drive.main` is the
detach-child / out-of-session entry to the same code (design §7 "the CLI is the
invariant substrate").
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.lifecycle.block_drive import run_tick
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.block_drive import BlockDriveResult, BlockDriveSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state.journal import DRIVE_PROGRESS_ACTIONS, stamp_drive_attempt

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["block_drive"]

_log = logging.getLogger(__name__)


def _count_drive_attempt(experiment_dir: Path, result: BlockDriveResult) -> None:
    """Stamp this tick's futile-drive counter — the retryable(n) write point.

    THE increment point for ``RunRecord.drive_attempts``
    (``docs/plans/run-queue-placement-2026-07-28.md`` §7): a drain plan may give
    a failed drivable item at most *n* drive attempts, and that budget must be
    KERNEL state read off disk, never a variable the plan carries across
    relaunches. Placed on the AGENT-FACING seat deliberately — this is the verb a
    drain pass relays, so the number counts relayed attempts, which is exactly
    what the policy bounds. The console-script / detach-child entry
    (``hpc-block-drive``) reaches ``run_tick`` directly and is NOT counted: a
    detached child's poll is not a pass relaying a tick, and counting it would
    let a healthy long wait exhaust an item's budget.

    Classification lives in ``state/journal.DRIVE_PROGRESS_ACTIONS`` (one home);
    this seat only reports which side of it the tick landed on.

    Never raises — and "never" is load-bearing, so the catch is ``Exception``
    rather than a tuple of the failures anyone thought of. A tick that drove no
    run (``run_id`` is null on a ``skip``), or one whose record does not exist
    yet, has nothing to count against; and a bookkeeping failure must not turn a
    successful drive into an error envelope, because the counter is an advisory
    budget and the drive ALREADY HAPPENED. The narrower ``(FileNotFoundError,
    OSError)`` this started as was false advertising: a type-corrupt record
    (``drive_attempts: "three"``) raises ``ValueError`` out of ``int()`` in
    :func:`~hpc_agent.state.journal.stamp_drive_attempt` and crashed the drive it
    was supposed to be advisory to. Same posture as the queue's janitor
    (``ops/queue/maintenance.py``): hygiene reports, it never vetoes.
    """
    run_id = (result.run_id or "").strip()
    if not run_id:
        return
    try:
        stamp_drive_attempt(
            run_id,
            progressed=result.action in DRIVE_PROGRESS_ACTIONS,
            experiment_dir=experiment_dir,
        )
    except Exception as exc:  # noqa: BLE001 — an advisory counter must never fail a drive
        _log.warning("block-drive: could not stamp drive_attempts for %s (%s)", run_id, exc)


@primitive(
    name="block-drive",
    verb="workflow",
    composes=[
        "submit-s1",
        "submit-s2",
        "submit-s3",
        "submit-s4",
        "status-snapshot",
        "status-watch",
        "aggregate-check",
        "aggregate-run",
        "campaign-greenlight",
        "campaign-watch",
        "campaign-complete",
        # The audit family (P2.b) — the notebook-audit loop's deterministic verbs.
        "audit-preflight",
        "notebook-lint",
        "notebook-auto-clear",
        "notebook-audit-view",
        "notebook-status",
        "audit-handoff",
    ],
    side_effects=[
        SideEffect("spawns-subprocess", "hpc-agent <block verb> per chained span"),
        SideEffect(
            "writes-journal",
            "<run_id> pending_decision marker + watchdog tick + drive_attempts counter",
        ),
    ],
    error_codes=[errors.SpecInvalid, errors.JournalCorrupt],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "block-drive (wave 4): one stateless resumable tick that DRIVES a block "
            "chain. Chains the deterministic spans in code (S1-resolve → decision; "
            "S2-canary → decision; …) and, at a human decision point, writes "
            "{brief, pending marker, resume cursor} to durable state and EXITS. On a "
            "resume it consumes the approved 'resolved' spec (never a nudge string) "
            "and routes by identity + ownership (§4): advance / rerun / "
            "advance_carrying. The LLM no longer executes the transition."
        ),
        spec_arg=True,
        spec_model=BlockDriveSpec,
        experiment_dir_arg=True,
        schema_ref=SchemaRef(input="block_drive", output="block_drive"),
    ),
    agent_facing=True,
)
def block_drive(experiment_dir: Path, *, spec: BlockDriveSpec) -> BlockDriveResult:
    """Advance the block chain by one tick (block-drive.md §2–§5).

    Delegates to :func:`run_tick` — the driver reads the durable position
    (``read_pending_decision`` + the decision journal), plans the §4 route in the
    pure :func:`plan_block_action`, then chains deterministic spans or parks at a
    decision. Returns the :class:`BlockDriveResult` record of what the tick did;
    the tick's process exit code is only consumed by the console-script entry
    (``hpc-block-drive`` / detach children), not this agent-facing surface.

    After the tick, stamps the run's ``drive_attempts`` budget
    (:func:`_count_drive_attempt`) — the one write point for the drain loop's
    retryable(n) policy. A ``dry_run`` preview is deliberately NOT counted: it
    executes no span, so charging it would let a plan burn an item's budget by
    looking at it.
    """
    result, _exit_code = run_tick(
        experiment_dir,
        run_id=spec.run_id,
        workflow=spec.workflow,
        dry_run=spec.dry_run,
        approve=spec.approve,
        audit=spec.audit.model_dump(mode="json") if spec.audit is not None else None,
    )
    if not spec.dry_run:
        _count_drive_attempt(experiment_dir, result)
    return result
