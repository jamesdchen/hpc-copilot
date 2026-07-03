"""Pydantic models for the aggregate human-amplification block verbs.

The aggregate workflow, decomposed (``docs/design/human-amplification-blocks.md``
§3, the finer grain of submit's S4) into two **blocks**. Each block chains
deterministically in code as far as code can go, then TERMINATES at a human
decision point carrying code-digested evidence — a *brief*. No decision point is
resolved by the LLM: the block hands back the brief; the LLM drafts a proposal
over it; the human answers ``y`` or a natural-language nudge (§2).

The two blocks split aggregation at its one real seam — *is this run safe to
reduce* vs *reduce it and hand over the numbers*:

* **aggregate-check — readiness + integrity.** ``aggregate-preflight``
  (install ∥ load-context, optional reconcile) + the terminal-status readiness
  gate + the ``verify-aggregation-complete`` integrity gate. Integrity issues
  (missing waves/tasks, cross-run contamination, provenance/column violations)
  are NEVER auto-masked (§2, the #355 doctrine): each is surfaced as a decision
  point carrying a conservative ``recommendation`` the human greenlights or
  nudges. ``needs_decision`` is True when a readiness gate fails or an integrity
  issue exists; False (``ready``) when the run is clean to reduce.
* **aggregate-run — combine + reduce + extract.** The deterministic
  ``aggregate-flow`` pipeline (ensure waves combined → pull partials → reduce)
  to a code-extracted results table + an error-sweep summary + an EMPTY
  ``proposed_interpretations`` slot the LLM fills at the ``y``/nudge boundary.
  Code extracts the results; the human concludes from them — results are never
  interpreted raw by the LLM (§2). Terminator: results table → ``y``/nudge
  (``harvested`` / ``harvest_partial``).

Both blocks share :class:`AggregateBlockResult`; the per-block Spec models embed
the existing wire specs verbatim (``AggregateFlowSpec``) rather than
re-enumerating their knobs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec

# The union of every terminator an aggregate block can stop at, modelled as data
# (#231 decision-points-as-data). ``needs_decision`` on the result says whether a
# human must answer here; ``stage_reached`` carries the finer detail.
AggregateBlockStage = Literal[
    # aggregate-check — readiness + integrity.
    "ready",  # terminal + preflight clean + no integrity issues → safe to reduce.
    "not_ready",  # readiness gate failed (run not terminal / preflight fail / no record).
    "integrity_review",  # integrity issue(s) surfaced — never auto-masked, human decides.
    # aggregate-run — combine + reduce + extract.
    "harvested",  # every wave combined; results table ready.
    "harvest_partial",  # some waves escalated; partial results table.
]


class AggregateBlockResult(BaseModel):
    """Shared ``data`` block for every aggregate block (check / run).

    The ``brief`` is the code-digested evidence the LLM drafts a proposal over
    (§2): the readiness + integrity digest for ``aggregate-check`` (each
    integrity issue carrying a conservative ``recommendation``, never
    auto-masked), the results table + error-sweep summary + empty
    interpretation slot for ``aggregate-run``. ``needs_decision`` marks a
    ``y``/nudge terminator (True for every readiness/integrity block and for
    both harvest terminators; False only when ``aggregate-check`` finds the run
    clean and simply suggests ``aggregate-run``).
    """

    model_config = ConfigDict(extra="forbid", title="aggregate-block output data")

    block: Literal["check", "run"] = Field(
        description="Which aggregate block produced this result.",
    )
    stage_reached: AggregateBlockStage = Field(
        description="The terminator the block stopped at (decision-as-data, #231).",
    )
    needs_decision: bool = Field(
        description=(
            "True when a human must answer here (the y/nudge terminator). False "
            "only when aggregate-check found the run clean (ready) and simply "
            "suggests aggregate-run."
        ),
    )
    reason: str = Field(
        default="",
        description="Human-readable one-line summary of the terminator.",
    )
    run_id: str | None = Field(
        default=None,
        description="The run this block operated on, when one exists yet.",
    )
    brief: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Code-digested evidence the LLM drafts a proposal over (§2). Shape "
            "varies per block; never interpreted raw by the LLM."
        ),
    )
    next_block: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The DETERMINISTICALLY-computed next block — ``{verb, why, spec_hint}`` "
            "— or null at a terminal / human-branch terminator (design §2, the "
            "``_next_step_hint`` pattern generalized). ``verb`` names the next "
            "block's CLI verb; ``spec_hint`` carries the minimal next-spec skeleton "
            "(run_id etc.). Surfaced, greenlit, journaled under "
            "``resolved.next_block``, and enforced by the successor gate — never "
            "free-prose."
        ),
    )


class AggregateCheckSpec(BaseModel):
    """Inputs to ``aggregate-check`` (readiness + integrity).

    Runs the aggregate preflight (install ∥ load-context, optional reconcile),
    the terminal-status readiness gate, and — when a local ``_combiner/`` has
    already been pulled — the ``verify-aggregation-complete`` integrity gate.
    Every integrity issue is surfaced in the brief as a NEVER-auto-masked
    decision point with a conservative ``recommendation``.
    """

    model_config = ConfigDict(extra="forbid", title="aggregate-check input spec")

    run_id: RunIdStrict
    run_preflight: bool = Field(
        default=True,
        description=(
            "Run aggregate-preflight (install-commands ∥ load-context, optional "
            "reconcile) before the gates and fold its overall pass/fail into the "
            "brief. Disable for a pure local readiness+integrity check."
        ),
    )
    reconcile_scheduler: str | None = Field(
        default=None,
        description=(
            "Forwarded to aggregate-preflight. When supplied AND load-context "
            "reports next_step_hint == 'monitor', reconcile the journal-only "
            "in-flight run against the cluster before the readiness gate trusts "
            "the journal. Omit to skip reconcile."
        ),
    )
    allow_partial: bool = Field(
        default=False,
        description=(
            "The operator's stance on a partial aggregate. When False (default), "
            "missing waves are a blocking integrity decision (partial usually "
            "masks a real cluster failure); when True, the missing-waves issue is "
            "still SURFACED (never auto-masked) but its recommendation reflects "
            "the operator's explicit choice to proceed. Contamination / provenance "
            "/ column issues block regardless."
        ),
    )


class AggregateRunSpec(BaseModel):
    """Inputs to ``aggregate-run`` (combine + reduce + extract).

    Runs the existing ``aggregate-flow`` pipeline to a code-extracted results
    table. aggregate-run OWNS the terminal-or-explicitly-partial invariant via
    the composed ``aggregate-flow`` gate — it never assumes ``aggregate-check``
    ran first.
    """

    model_config = ConfigDict(extra="forbid", title="aggregate-run input spec")

    aggregate: AggregateFlowSpec = Field(
        description=(
            "The aggregate-flow spec — ensures waves combined, pulls partials, "
            "reduces. Its terminal-status precondition gate is aggregate-run's "
            "own invariant (ensure_all_combined + non-terminal → precondition "
            "failure); ensure_all_combined=false is the deliberate-partial opt-in."
        ),
    )
