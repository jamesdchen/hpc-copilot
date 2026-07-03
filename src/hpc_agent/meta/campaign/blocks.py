"""``campaign-greenlight`` · ``campaign-watch`` · ``campaign-complete`` — the
campaign flow as human-amplification blocks.

The campaign flow, decomposed (docs/design/human-amplification-blocks.md §4)
into the three touchpoints a campaign actually has. A campaign is NOT a linear
per-run chain like submit (S1–S4): its spec is **greenlit once at start**, and
execution is then **fully asynchronous** — reconcile ticks self-chain while
healthy, the strategy picks next batches deterministically, and there is **no
per-iteration human boundary**. So there are exactly three blocks, one per §4
touchpoint:

* ``campaign-greenlight`` (start) — digest the greenlit-once spec into a brief
  for the ``y``/nudge; a ``confirm`` re-invocation records the greenlight.
* ``campaign-watch`` (async execution surface) — a read-only digest of the
  running campaign; it OBSERVES (never runs a tick — ticks self-chain via the
  existing driver) and surfaces anomaly / health / hand-off terminators.
* ``campaign-complete`` (end) — the completion brief: spend vs budget,
  iterations, stop reason, a code-extracted outcome table, and an EMPTY
  ``proposed_interpretations`` slot.

Each block is a THIN orchestrator over existing primitives / atoms
(``campaign-advance``, ``campaign-status``, ``campaign-budget``,
``mark_greenlit``, the decision journal). It composes and digests; it never
reimplements campaign logic, and — per the fork's hard rule — it never
resolves a decision itself: it digests, or records a caller-supplied one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.campaign_blocks import (
    CampaignBlockResult,
    CampaignCompleteSpec,
    CampaignGreenlightSpec,
    CampaignWatchSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.block_chain import next_block_hint

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["campaign_complete", "campaign_greenlight", "campaign_watch"]

# ``campaign-advance`` decisions grouped by the watch terminator they map to.
# HEALTHY: the campaign is nominal (self-chaining ticks, no human boundary).
# ANOMALY: a §5 loud-fail guard tripped, or a budget halt needs a human. A
# ``stop_converged`` maps to the COMPLETE hand-off (handled separately).
_WATCH_HEALTHY: frozenset[str] = frozenset({"continue", "wait_in_flight", "refill"})
_WATCH_ANOMALY: frozenset[str] = frozenset(
    {"stop_circuit_breaker", "stop_resubmit_cap", "stop_over_budget"}
)


def _next_block(
    current_verb: str, stage_reached: str, why: str, **spec_hint: Any
) -> dict[str, Any] | None:
    """Delegate to the ``block_chain`` successor table (design §6/§8).

    Mirrors ``ops/submit_blocks._next_block``: the successor VERB is re-homed into
    ``block_chain.SUCCESSORS``; this thin helper keeps the emitted
    ``{verb, why, spec_hint}`` shape unchanged and returns ``None`` at a terminal /
    human-branch terminator. A campaign has three §4 touchpoints, so the only
    deterministic successors are greenlight→watch and watch(complete)→complete.
    """
    return next_block_hint(current_verb, stage_reached, why=why, **spec_hint)


# ── greenlight helpers ───────────────────────────────────────────────────────


def _digest_spec(manifest: dict[str, Any]) -> dict[str, Any]:
    """Digest the greenlit-once campaign spec into the brief's shape (§4).

    Pulls exactly the fields the design names as the campaign contract —
    goal / budget / strategy / stop_criteria / anomaly_policy / async_refill —
    plus the greenlight provenance marker so the brief shows whether it is
    already stamped. Round-tripped verbatim: the block never interprets them.
    """
    return {
        "goal": manifest.get("goal", ""),
        "budget": manifest.get("budget"),
        "strategy": manifest.get("strategy"),
        "stop_criteria": manifest.get("stop_criteria"),
        "anomaly_policy": manifest.get("anomaly_policy"),
        "async_refill": bool(manifest.get("async_refill", False)),
        "max_in_flight": manifest.get("max_in_flight"),
        "greenlit": bool(manifest.get("greenlit", False)),
        "greenlit_at": manifest.get("greenlit_at"),
    }


# ── complete helpers ─────────────────────────────────────────────────────────


def _outcome_table(status: dict[str, Any]) -> list[dict[str, Any]]:
    """Digest the per-iteration reduced metrics into a code-extracted table.

    ``campaign-status`` hands back parallel ``run_ids`` + ``history`` lists
    (oldest-first). The table is a stable row-per-iteration projection the LLM
    renders and proposes interpretations over — code extracts the outcomes; the
    human concludes from them (§2). Never interpreted raw by the LLM.
    """
    run_ids = status.get("run_ids") or []
    history = status.get("history") or []
    rows: list[dict[str, Any]] = []
    for i, metrics in enumerate(history):
        row: dict[str, Any] = {"iteration": i}
        if i < len(run_ids):
            row["run_id"] = run_ids[i]
        row["metrics"] = metrics if isinstance(metrics, dict) else {"value": metrics}
        rows.append(row)
    return rows


# ── greenlight ───────────────────────────────────────────────────────────────


@primitive(
    name="campaign-greenlight",
    verb="workflow",
    # On the confirm path this block genuinely composes ``append-decision``
    # (``state.decision_journal.append_decision``) to journal the human's
    # greenlight, alongside ``mark_greenlit``. Declared so the workflow-spine
    # contract (every workflow primitive composes at least one atom) holds.
    composes=["append-decision"],
    side_effects=[
        SideEffect(
            "writes-campaign-state",
            "<experiment_dir>/.hpc/campaigns/<campaign_id>/ "
            "(manifest greenlit marker + decisions.jsonl, on confirm only)",
        )
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="spec.campaign_id",
    cli=CliShape(
        help=(
            "Campaign block (start): digest the greenlit-once campaign spec "
            "(goal / budget / strategy / stop_criteria / anomaly_policy / "
            "async_refill) into a brief for y/nudge. An un-greenlit manifest "
            "digests to needs_greenlight (nothing stamped); --spec confirm=true "
            "(the post-`y` re-invocation) stamps mark_greenlit + journals the "
            "decision; an already-greenlit manifest is an idempotent re-read."
        ),
        spec_arg=True,
        spec_model=CampaignGreenlightSpec,
        experiment_dir_arg=True,
        schema_ref=SchemaRef(input="campaign_greenlight"),
    ),
    agent_facing=True,
)
def campaign_greenlight(
    experiment_dir: Path, *, spec: CampaignGreenlightSpec
) -> CampaignBlockResult:
    """Start block: digest the campaign spec, or record a caller-supplied greenlight.

    Three terminators (the verb never decides — it digests or records):

    * ``confirm=True`` → stamp ``mark_greenlit`` onto the manifest and journal
      the human's greenlight (``greenlit``, ``needs_decision=False``).
    * an already-greenlit manifest → idempotent re-read (``already_greenlit``,
      ``needs_decision=False``).
    * otherwise → digest the spec into the brief and stop at the once-at-start
      ``y``/nudge boundary (``needs_greenlight``, ``needs_decision=True``).

    Owns its invariant (adding-a-primitive.md): the greenlight marker rides the
    spec, so the manifest MUST exist — an absent manifest is a loud
    :class:`errors.SpecInvalid`, never a silent no-op.
    """
    from hpc_agent.meta.campaign.manifest import mark_greenlit, read_manifest

    cid = spec.campaign_id
    manifest = read_manifest(experiment_dir, campaign_id=cid)
    if manifest is None:
        # The marker rides the spec — greenlighting a campaign with no manifest
        # is a loud failure (write it via campaign-init first), not a no-op.
        raise errors.SpecInvalid(
            f"campaign {cid!r} has no manifest to greenlight; write the spec "
            "(campaign-init / write_manifest) before greenlighting."
        )

    # confirm: RECORD the human's greenlight — stamp the marker + journal it.
    # This path is the post-`y` re-invocation, so the decision is already made
    # (needs_decision=False); the block only persists it.
    if spec.confirm:
        updated = mark_greenlit(experiment_dir, campaign_id=cid)
        brief = _digest_spec(updated)
        if spec.journal:
            from hpc_agent.state.decision_journal import append_decision

            append_decision(
                experiment_dir,
                scope_kind="campaign",
                scope_id=cid,
                block="campaign-greenlight",
                response=spec.response,
                evidence_digest=brief,
                proposal=spec.proposal,
                resolved={"greenlit": True, "greenlit_at": updated.get("greenlit_at")},
            )
        return CampaignBlockResult(
            block="greenlight",
            stage_reached="greenlit",
            needs_decision=False,
            reason=(
                f"campaign {cid!r} greenlit at {updated.get('greenlit_at')}; "
                "execution now runs fully asynchronously against the spec."
            ),
            campaign_id=cid,
            brief=brief,
            next_block=_next_block(
                "campaign-greenlight",
                "greenlit",
                "greenlit; observe the asynchronous execution for health / anomalies.",
                campaign_id=cid,
            ),
        )

    brief = _digest_spec(manifest)

    # Already greenlit (and not re-confirming): an idempotent re-read. Nothing
    # is stamped or journaled — the decision was recorded on the first confirm.
    if manifest.get("greenlit"):
        return CampaignBlockResult(
            block="greenlight",
            stage_reached="already_greenlit",
            needs_decision=False,
            reason=(
                f"campaign {cid!r} was already greenlit at "
                f"{manifest.get('greenlit_at')}; nothing to decide."
            ),
            campaign_id=cid,
            brief=brief,
            next_block=_next_block(
                "campaign-greenlight",
                "already_greenlit",
                "already greenlit and running; observe the asynchronous execution.",
                campaign_id=cid,
            ),
        )

    # Un-greenlit: digest the spec and stop at the once-at-start y/nudge.
    return CampaignBlockResult(
        block="greenlight",
        stage_reached="needs_greenlight",
        needs_decision=True,
        reason=(
            f"campaign {cid!r} spec digested; greenlight it once to start the "
            "asynchronous execution (re-invoke with confirm=true after `y`)."
        ),
        campaign_id=cid,
        brief=brief,
    )


# ── watch ────────────────────────────────────────────────────────────────────


@primitive(
    name="campaign-watch",
    verb="workflow",
    composes=["campaign-advance"],
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="spec.campaign_id",
    cli=CliShape(
        help=(
            "Campaign block (async execution surface): a READ-ONLY digest of the "
            "running campaign for a health / anomaly brief. Composes "
            "campaign-advance's evidence — it OBSERVES, never runs a tick (ticks "
            "self-chain via the driver). Terminators: watching_healthy (no "
            "boundary) / watching_anomaly (loud-fail or budget halt → y/nudge, "
            "surfaces the anomaly_brief) / watching_complete (stop criterion "
            "fired → hand off to campaign-complete)."
        ),
        spec_arg=True,
        spec_model=CampaignWatchSpec,
        experiment_dir_arg=True,
        schema_ref=SchemaRef(input="campaign_watch"),
    ),
    agent_facing=True,
)
def campaign_watch(experiment_dir: Path, *, spec: CampaignWatchSpec) -> CampaignBlockResult:
    """Async surface block: OBSERVE the running campaign; classify the terminator.

    Pure read. Composes ``campaign-advance`` (itself a pure read that folds
    status / budget / convergence / loud-fail guards into one decision) and
    maps that decision onto the three §4 watch terminators. It never runs a
    tick — the driver self-chains reconcile ticks; watch only observes and
    surfaces the anomaly / health / hand-off brief.

    * a loud-fail guard (``stop_circuit_breaker`` / ``stop_resubmit_cap``) or a
      budget halt (``stop_over_budget``) → ``watching_anomaly``
      (``needs_decision=True``), surfacing the drafted ``anomaly_brief``;
    * a fired stop criterion (``stop_converged``) → ``watching_complete``
      (``needs_decision=False``), a hand-off hint to ``campaign-complete``;
    * anything nominal (``continue`` / ``wait_in_flight`` / ``refill``) →
      ``watching_healthy`` (``needs_decision=False``).
    """
    from hpc_agent.meta.campaign.atoms.advance import campaign_advance
    from hpc_agent.meta.campaign.manifest import read_manifest

    cid = spec.campaign_id
    adv = campaign_advance(experiment_dir=experiment_dir, campaign_id=cid)
    decision = str(adv.get("decision", ""))

    manifest = read_manifest(experiment_dir, campaign_id=cid)
    brief: dict[str, Any] = {
        "decision": decision,
        "advance_reason": adv.get("reason"),
        "greenlit": bool(manifest.get("greenlit")) if manifest else False,
        "status": adv.get("status"),
        "budget": adv.get("budget"),
        "converged": adv.get("converged"),
        "circuit_breaker": adv.get("circuit_breaker"),
        "resubmit_cap": adv.get("resubmit_cap"),
        "needs_acknowledgement": adv.get("needs_acknowledgement", False),
        # Non-None only on a loud-fail terminator; the drafted brief for y/nudge.
        "anomaly_brief": adv.get("anomaly_brief"),
    }

    if decision == "stop_converged":
        return CampaignBlockResult(
            block="watch",
            stage_reached="watching_complete",
            needs_decision=False,
            reason=(
                f"campaign {cid!r} met a stop criterion ({adv.get('reason')}); "
                "hand off to campaign-complete for the completion brief."
            ),
            campaign_id=cid,
            brief=brief,
            next_block=_next_block(
                "campaign-watch",
                "watching_complete",
                "a stop criterion fired; build the completion brief.",
                campaign_id=cid,
            ),
        )

    if decision in _WATCH_ANOMALY:
        return CampaignBlockResult(
            block="watch",
            stage_reached="watching_anomaly",
            needs_decision=True,
            reason=(
                f"campaign {cid!r} anomaly: {decision} ({adv.get('reason')}); "
                "surface for a y/nudge decision."
            ),
            campaign_id=cid,
            brief=brief,
        )

    # Nominal — self-chaining ticks continue; no human boundary (§4).
    return CampaignBlockResult(
        block="watch",
        stage_reached="watching_healthy",
        needs_decision=False,
        reason=(
            f"campaign {cid!r} healthy ({decision}); execution self-chains "
            "asynchronously — no boundary."
        ),
        campaign_id=cid,
        brief=brief,
    )


# ── complete ─────────────────────────────────────────────────────────────────


@primitive(
    name="campaign-complete",
    verb="workflow",
    composes=["campaign-status", "campaign-budget", "campaign-advance"],
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="spec.campaign_id",
    cli=CliShape(
        help=(
            "Campaign block (end): the completion brief — spend vs budget, "
            "iterations, stop reason, and a code-extracted per-iteration outcome "
            "table — plus an EMPTY proposed_interpretations slot the LLM fills at "
            "the y/nudge boundary (code extracts outcomes; the human concludes). "
            "Terminates → y/nudge."
        ),
        spec_arg=True,
        spec_model=CampaignCompleteSpec,
        experiment_dir_arg=True,
        schema_ref=SchemaRef(input="campaign_complete"),
    ),
    agent_facing=True,
)
def campaign_complete(experiment_dir: Path, *, spec: CampaignCompleteSpec) -> CampaignBlockResult:
    """End block: build the completion brief over the campaign's durable state.

    A pure read that digests the campaign's own durable state — manifest
    (goal + budget), sidecars (iterations + outcomes), the runtime-prior spend
    join (via ``campaign-budget`` → ``compute_spend``), and the terminal stop
    reason (via ``campaign-advance``) — into a completion brief. The brief
    carries a code-extracted per-iteration outcome table plus an EMPTY
    ``proposed_interpretations`` slot: code extracts the outcomes, the LLM
    proposes interpretations at the ``y``/nudge boundary, the human concludes
    (§2). Always a decision terminator.
    """
    from hpc_agent.meta.campaign.atoms.advance import campaign_advance
    from hpc_agent.meta.campaign.atoms.budget import campaign_budget
    from hpc_agent.meta.campaign.atoms.status import campaign_status
    from hpc_agent.meta.campaign.manifest import read_manifest

    cid = spec.campaign_id
    manifest = read_manifest(experiment_dir, campaign_id=cid) or {}
    status = campaign_status(experiment_dir=experiment_dir, campaign_id=cid)
    budget = campaign_budget(experiment_dir=experiment_dir, campaign_id=cid)
    adv = campaign_advance(experiment_dir=experiment_dir, campaign_id=cid)

    brief: dict[str, Any] = {
        "goal": manifest.get("goal", ""),
        "iterations": status.get("iterations", 0),
        "run_ids": status.get("run_ids", []),
        "spend": budget.get("spent"),
        "budget": budget.get("budget"),
        "remaining": budget.get("remaining"),
        "coverage": budget.get("coverage"),
        "stop_reason": {"decision": adv.get("decision"), "reason": adv.get("reason")},
        "converged": adv.get("converged"),
        "anomaly_brief": adv.get("anomaly_brief"),
        "outcome_table": _outcome_table(status),
        # The slot the LLM fills with proposed interpretations at y/nudge — code
        # hands over an EMPTY list; concluding is the human's decision (§2).
        "proposed_interpretations": [],
    }

    return CampaignBlockResult(
        block="complete",
        stage_reached="complete",
        needs_decision=True,
        reason=(
            f"campaign {cid!r} complete: {status.get('iterations', 0)} iteration(s), "
            f"stop reason {adv.get('decision')!r}; review the outcome table and "
            "choose an interpretation."
        ),
        campaign_id=cid,
        brief=brief,
    )
