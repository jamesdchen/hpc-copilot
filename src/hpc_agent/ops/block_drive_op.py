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

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.lifecycle.block_drive import run_tick
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.block_drive import BlockDriveResult, BlockDriveSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["block_drive"]


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
    ],
    side_effects=[
        SideEffect("spawns-subprocess", "hpc-agent <block verb> per chained span"),
        SideEffect("writes-journal", "<run_id> pending_decision marker + watchdog tick"),
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
    """
    result, _exit_code = run_tick(
        experiment_dir,
        run_id=spec.run_id,
        workflow=spec.workflow,
        dry_run=spec.dry_run,
    )
    return result
