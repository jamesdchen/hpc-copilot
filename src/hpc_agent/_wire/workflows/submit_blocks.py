"""Pydantic models for the submit S1–S4 human-amplification block verbs.

The submit workflow, decomposed (``docs/design/human-amplification-blocks.md``
§3) into four **blocks**. Each block chains deterministically in code as far as
code can go, then TERMINATES at a human decision point carrying code-digested
evidence — a *brief*. No decision point is resolved by the LLM: the block hands
back the brief; the LLM drafts a proposal over it; the human answers ``y`` or a
natural-language nudge (§2).

The four blocks map ~1:1 onto the existing submit rings:

* **S1 — resolve.** ``submit-preflight`` + ``walk-submit-ambiguities`` (+
  ``resolve-submit-inputs`` once the walk is clean). Each ambiguity's
  ``safe_default`` is surfaced in the brief as a PRE-FILLED RECOMMENDATION (§6,
  line 181) — NOT auto-applied into ``resolved`` (``apply-safe-defaults`` is the
  silent actor this kills, so the block never calls it). Terminator: full
  decision brief → ``y``/nudge.
* **S2 — stage & canary.** ``submit-and-verify`` STOPPED after a verified canary
  (``stop_after_canary=True``), plus the ``estimate-core-hours`` footprint from
  the submit spec. Terminator: "canary green, est. N core-hours" → ``y``/nudge.
* **S3 — submit & watch.** Phase-2 main-array launch + ``monitor-flow`` to a
  terminal/anomaly state + ``decide-monitor-arm``. Runs UNATTENDED — no human
  boundary inside; an anomaly is itself a block terminator (§5).
* **S4 — harvest.** ``aggregate-flow`` → a code-extracted results table + a slot
  for the LLM's proposed interpretations. Terminator: results table → ``y``/nudge.

All four share :class:`SubmitBlockResult`; the per-block Spec models embed the
existing wire specs verbatim (``SubmitAndVerifySpec`` / ``MonitorFlowSpec`` /
``AggregateFlowSpec`` / ``WalkSubmitAmbiguitiesInput`` / ``ResolveSubmitInputsSpec``)
rather than re-enumerating their knobs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire.queries.walk_submit_ambiguities import WalkSubmitAmbiguitiesInput
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec
from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec

# The union of every terminator a submit block can stop at, modelled as data
# (#231 decision-points-as-data). ``needs_decision`` on the result says whether
# a human must answer here; ``stage_reached`` carries the finer detail.
BlockStage = Literal[
    # S1 — resolve.
    "needs_resolution",  # walk found ambiguities → recommendations in the brief.
    "prior_run_found",  # resolve leg hit a live prior run (resume-vs-fresh).
    "needs_scaffold_interview",  # resolve leg needs the scaffold sub-interview.
    "resolved",  # clean: submit-flow spec built + sidecar written.
    # S2 — stage & canary.
    "canary_verified",  # canary green; est core-hours attached; ready for S3.
    "canary_failed",  # canary failed verification (anomaly terminator).
    "deduped",  # the run already exists — no fresh canary fired.
    # S3 — submit & watch.
    "watching_terminal",  # main array reached a clean terminal state.
    "watching_anomaly",  # failed / abandoned → §5 anomaly terminator.
    "watching_timeout",  # wall-clock budget hit; cluster jobs may run on.
    # S4 — harvest.
    "harvested",  # every wave combined; results table ready.
    "harvest_partial",  # some waves escalated; partial results table.
    # Detach-by-contract (design §3): the block spawned a durable detached
    # worker and returned immediately — the brief arrives on completion, read
    # from the journal (never held in a process).
    "detached",
]


class SubmitBlockResult(BaseModel):
    """Shared ``data`` block for every submit block (S1–S4).

    The ``brief`` is the code-digested evidence the LLM drafts a proposal over
    (§2): the digested envelope for S1, "canary green + est core-hours" for S2,
    the terminal status + arm decision for S3, the results table + interpretation
    slot for S4. ``needs_decision`` marks a ``y``/nudge terminator (True for the
    S1/S2/S4 briefs and for an S3 anomaly; False when S3 reached a clean terminal
    and simply suggests S4).
    """

    model_config = ConfigDict(extra="forbid", title="submit-block output data")

    block: Literal["s1", "s2", "s3", "s4"] = Field(
        description="Which submit block produced this result.",
    )
    stage_reached: BlockStage = Field(
        description="The terminator the block stopped at (decision-as-data, #231).",
    )
    needs_decision: bool = Field(
        description=(
            "True when a human must answer here (the y/nudge terminator). False "
            "only when the block ran clean to an unattended terminal (S3 complete)."
        ),
    )
    reason: str = Field(
        default="",
        description="Human-readable one-line summary of the terminator.",
    )
    relay: str = Field(
        default="",
        description=(
            "The human-facing one-liner CODE renders from this block's OWN "
            "structured evidence (run_id, canary state, verified flag, est "
            "core-hours, status counts, cluster) — the agent relays it VERBATIM "
            "(design §5.3, finding 15), never reconstructing numbers/state from "
            "memory. Because the string IS the journal's rendering it cannot "
            "contradict the record, so the relay-audit Stop hook (conduct rule "
            "10) fires almost never. The S2 canary summary renders the CANARY's "
            "1 task, NEVER the main array's total (the exact finding-15 bleed). "
            "Empty only for a stage with no renderable line."
        ),
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
            "block's CLI verb; ``spec_hint`` is the minimal next-spec skeleton "
            "(run_id etc.). The LLM surfaces it the way /sync is proposed; the "
            "human's `y` greenlights THAT named verb; append-decision journals it "
            "under ``resolved.next_block``; the successor block's gate verifies the "
            "journaled greenlight names it. NEVER free-prose — a mis-sequenced call "
            "fails loudly."
        ),
    )
    started: bool = Field(
        default=False,
        description=(
            "Detach-by-contract handle (design §3): True when the block spawned a "
            "durable detached worker and returned immediately instead of blocking "
            "the chat on the scheduler. The gate + drift guard already fired "
            "synchronously BEFORE the detach; the brief is not held in a process — "
            "read it from the journal via status-snapshot / the completion "
            "notification. False on the synchronous (detach=False) path."
        ),
    )
    watch: str | None = Field(
        default=None,
        description=(
            'How to learn the detached block\'s outcome — ``"journal"`` when '
            "``started`` is True (the detached worker stamps the per-run journal "
            "record as it polls; poll it cluster-free). None on the synchronous path."
        ),
    )
    detached_pid: int | None = Field(
        default=None,
        description=(
            "The detached worker's OS process id (informational — do NOT wait on "
            "it; read the journal). None on the synchronous path. A dead worker is "
            "detected by the §5 watchdog via a lapsed next_tick_due, not by this pid."
        ),
    )


class SubmitS1Spec(BaseModel):
    """Inputs to ``submit-s1`` (resolve).

    ``walk`` drives the deterministic ambiguity walk (the envelope machinery,
    unchanged). ``resolve`` is the OPTIONAL post-resolution input-resolution
    chain — supplied on a re-invocation after the human resolved the S1
    ambiguities, so the block can reach a clean ``resolved`` (submit-flow spec
    built) or a ``prior_run_found`` terminator in one pass.
    """

    model_config = ConfigDict(extra="forbid", title="submit-s1 input spec")

    walk: WalkSubmitAmbiguitiesInput = Field(
        description=(
            "The walk-submit-ambiguities inputs. Each unresolved AUTO_RESOLVABLE "
            "field's safe_default is surfaced in the brief as a RECOMMENDATION, "
            "never auto-applied into resolved."
        ),
    )
    run_preflight: bool = Field(
        default=True,
        description=(
            "Run submit-preflight (install-commands → load-context → "
            "check-preflight ∥ resolve-resources) before the walk and fold its "
            "overall pass/fail into the brief. Disable for a pure local resolve."
        ),
    )
    resolve: ResolveSubmitInputsSpec | None = Field(
        default=None,
        description=(
            "Optional resolve-submit-inputs spec. Run ONLY when the walk found no "
            "ambiguities (all fields caller-supplied or auto-resolved), to reach "
            "the clean resolved / prior_run_found / needs_scaffold_interview "
            "terminator. Null stops S1 at the ambiguity brief."
        ),
    )


class SubmitS2Spec(BaseModel):
    """Inputs to ``submit-s2`` (stage & canary).

    Embeds the full ``submit-and-verify`` spec; S2 runs it with
    ``stop_after_canary`` so the main array does NOT launch — the human reviews
    "canary green, est. N core-hours" first. The cost estimate is computed from
    the same ``submit.submit`` (total_tasks × walltime × cores).
    """

    model_config = ConfigDict(extra="forbid", title="submit-s2 input spec")

    submit: SubmitAndVerifySpec = Field(
        description=(
            "The submit-and-verify spec. Must have submit.canary=True — S2 gates "
            "on a verified canary. S2 stops after the canary; S3 launches main."
        ),
    )
    detach: bool = Field(
        default=True,
        description=(
            "Detach-by-contract (design §3): default ON — never-stall is the norm. "
            "When True the greenlight gate fires SYNCHRONOUSLY (gate → detach), then "
            "S2 spawns a durable detached worker to own the canary poll and returns "
            "immediately with a {started, watch: journal, detached_pid} handle; the "
            "'canary green, est N core-hours' brief is read from the journal on "
            "completion, never held in a process. Set False to run the canary poll "
            "synchronously in-process (the current path — tests / CI)."
        ),
    )


class SubmitS3Spec(BaseModel):
    """Inputs to ``submit-s3`` (submit & watch).

    Launches the main array (Phase-2 of the two-phase gate — the canary was
    already verified in S2), then monitors to a terminal/anomaly state and arms
    the next monitor tick. No human boundary inside.
    """

    model_config = ConfigDict(extra="forbid", title="submit-s3 input spec")

    submit: SubmitAndVerifySpec = Field(
        description=(
            "The SAME submit-and-verify spec S2 used. S3 launches its main array "
            "via the Phase-2 path (canary off; rsync/deploy/preflight already "
            "paid by S2's canary submit)."
        ),
    )
    canary_run_id: str | None = Field(
        default=None,
        description="The verified canary's run_id from S2 (threaded onto the result).",
    )
    canary_job_ids: list[str] | None = Field(
        default=None,
        description="The verified canary's scheduler ids from S2.",
    )
    monitor: MonitorFlowSpec = Field(
        description="The monitor-flow spec used to watch the main array to terminal/timeout.",
    )
    invocation_argv: str = Field(
        min_length=1,
        description=(
            "The exact /monitor-hpc argv the next tick should re-invoke; stamped "
            "into decide-monitor-arm's cron args."
        ),
    )
    user_invoked_via_loop: bool | None = Field(
        default=None,
        description="True iff this tick runs under /loop (the user drives cadence; no cron armed).",
    )
    detach: bool = Field(
        default=True,
        description=(
            "Detach-by-contract (design §3): default ON — never-stall is the norm. "
            "When True the greenlight gate + canary-validated gate + tree-drift "
            "guard ALL fire synchronously (gate → drift → detach) BEFORE the main "
            "array launches, then S3 spawns a durable detached worker that owns the "
            "launch + monitor-to-terminal poll and returns immediately with a "
            "{started, watch: journal, detached_pid} handle. The detached worker "
            "stamps the journal each poll so the §5 doctor/watchdog covers its death "
            "via a lapsed next_tick_due. Set False to run launch+monitor "
            "synchronously in-process (the current path — tests / CI)."
        ),
    )


class SubmitS4Spec(BaseModel):
    """Inputs to ``submit-s4`` (harvest).

    Runs the existing aggregate path to produce a code-extracted results table.
    (Unit B is adding a ``harvest_on_terminal`` guarantee in parallel; S4 calls
    the EXISTING ``aggregate-flow`` entry until that lands.)
    """

    model_config = ConfigDict(extra="forbid", title="submit-s4 input spec")

    aggregate: AggregateFlowSpec = Field(
        description="The aggregate-flow spec — ensures waves combined, pulls partials, reduces.",
    )
    detach: bool = Field(
        default=True,
        description=(
            "Detach-by-contract (design §3): default ON — never-stall is the norm. "
            "When True the greenlight gate fires SYNCHRONOUSLY (gate → detach), then "
            "S4 spawns a durable detached worker to own the harvest (per-wave combine "
            "SSH + rsync pull can ride a throttled cluster or an open breaker's "
            "wait-and-retry for many minutes) and returns immediately with a "
            "{started, watch: journal, detached_pid} handle; the results-table brief "
            "is read from the journal on completion, never held in a process. Set "
            "False to run the harvest synchronously in-process (tests / CI)."
        ),
    )
