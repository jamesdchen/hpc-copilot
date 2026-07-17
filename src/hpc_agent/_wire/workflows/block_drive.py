"""Pydantic models for the ``block-drive`` stateless resumable tick (wave 4).

``block-drive`` (``docs/design/block-drive.md`` §2–§5) generalizes the campaign
reconcile-tick driver to submit / status / aggregate: one invocation chains the
deterministic block spans IN CODE (S1-resolve → *decision*; S2-canary →
*decision*; …), and at a human decision point writes ``{brief, pending marker,
resume cursor}`` to durable state and EXITS. Nothing is held open between
decisions — the journal + filesystem are the only state carried, exactly like a
campaign tick.

The **rendezvous data contract** (§3): the code's only input on resume is an
approved *spec* — the ``resolved`` block of the latest ``response=="y"`` decision
record — never a nudge string. Routing (§4) is a function of that spec:
``advance`` (unchanged), ``rerun`` (a changed field owned by the current block),
or ``advance_carrying`` (all changed fields owned strictly downstream). This
module carries the wire shapes; the tick itself lives in
:mod:`hpc_agent._kernel.lifecycle.block_drive`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# The action a single tick took, modelled as data (decision-points-as-data,
# #231). Mirrors the ``plan_block_action`` return contract:
#
# * ``awaiting_decision`` — a pending decision exists but no ``response=="y"`` is
#   committed yet; a valid PARKED stop (do nothing, exit 0).
# * ``advanced`` — a plain ``y``: ran the code-determined successor block.
# * ``reran`` — a nudge editing a field owned by the current block; re-ran it.
# * ``chained`` — a fresh / mid-span deterministic span with a code-determined
#   successor; the tick chained on without an LLM round-trip.
# * ``detached`` — a scheduler-bound span returned a ``started/watch`` handle;
#   the detached child owns the poll, so the tick exits returning the handle.
# * ``terminal`` — a genuine end of chain (successor ``None``, no decision).
# * ``skip`` — nothing to do (no workflow / no run / unresolvable position).
BlockDriveAction = Literal[
    "awaiting_decision",
    "advanced",
    "reran",
    "chained",
    "detached",
    "terminal",
    "skip",
]


class BlockDriveSpec(BaseModel):
    """Inputs to one ``block-drive`` tick.

    Both fields are optional so a bare tick can recover its position purely from
    durable state: on a RESUME the ``run_id`` names the parked run whose
    ``pending_decision`` + decision journal carry everything; on a FRESH start the
    ``workflow`` names which chain (``submit`` / ``status`` / ``aggregate`` /
    ``campaign``) to begin. ``dry_run`` prints the planned action without
    executing any span.
    """

    model_config = ConfigDict(extra="forbid", title="block-drive input spec")

    run_id: str | None = Field(
        default=None,
        description=(
            "The run being driven. On a RESUME this keys the pending_decision + "
            "decision-journal reads that recover the parked position; on a FRESH "
            "start it is the run the first span will operate on."
        ),
    )
    workflow: str | None = Field(
        default=None,
        description=(
            "The workflow family to drive (submit / status / aggregate / "
            "campaign). Required for a FRESH chain start (picks the first block "
            "from block_chain.ORDER); recovered from the resume cursor on a RESUME."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="Print the planned action and exit without executing any span.",
    )
    approve: dict[str, Any] | None = Field(
        default=None,
        description=(
            "FUSED commit+advance (L1). An append-decision payload (the SAME "
            "AppendDecisionInput shape: scope_kind / scope_id / block / response / "
            "resolved / etc). When set, this ONE call first commits the human's "
            "y/nudge through the single append_decision definition — every "
            "authorship / brief-provenance / code-derived-field / bare-ack / "
            "unlock-authorship gate fires IDENTICALLY to a standalone "
            "`append-decision` — then advances the driver in the same call, "
            "returning the next parked brief. It removes the agent's mechanical "
            "SECOND call (the old `append-decision` then `block-drive` pair "
            "becomes one). MECHANISM only: the human still utters the y."
        ),
    )


class BlockDriveResult(BaseModel):
    """The ``data`` block one ``block-drive`` tick emits.

    Records WHICH action the tick took (``action``) and the position it acted at
    (``current_verb`` → ``next_verb``), so a cron / ``/loop`` schedule leaves an
    auditable per-tick record. ``brief`` is the code-digested evidence carried
    forward when the tick parked on a decision (``action=="awaiting_decision"`` on
    a re-entry, or the freshly written brief when a span reached a boundary);
    ``reason`` is the one-line human-readable summary.
    """

    model_config = ConfigDict(extra="forbid", title="block-drive output data")

    action: BlockDriveAction = Field(
        description="What this tick did (decision-as-data, #231).",
    )
    run_id: str | None = Field(
        default=None,
        description="The run this tick drove (null when nothing was drivable).",
    )
    workflow: str | None = Field(
        default=None,
        description="The workflow family driven this tick.",
    )
    current_verb: str | None = Field(
        default=None,
        description="The block that parked / last ran (the position the tick acted at).",
    )
    next_verb: str | None = Field(
        default=None,
        description=(
            "The block the tick ran / will run next (the code-determined "
            "successor, the re-run target, or null at a terminal)."
        ),
    )
    stage_reached: str | None = Field(
        default=None,
        description="The terminator of the last executed span, when one ran.",
    )
    brief: dict | None = Field(
        default=None,
        description=(
            "Code-digested evidence for the y/nudge loop when the tick parked on "
            "(or is awaiting) a decision. Never interpreted raw by the LLM (§3)."
        ),
    )
    reason: str = Field(
        default="",
        description="Human-readable one-line summary of the tick's outcome.",
    )
