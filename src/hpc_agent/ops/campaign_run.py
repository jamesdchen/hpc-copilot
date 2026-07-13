"""``campaign-run``: one campaign iteration's submit→monitor→aggregate spine.

Folds the deterministic three-stage iteration spine into ONE call — the
control-flow-out-of-the-LLM pattern applied one ring further out than
``submit-pipeline`` / ``status-pipeline``. Those each fold a single
workflow's spine; ``campaign-run`` is a composite-of-composites that chains
them:

    submit-pipeline  →  status-pipeline  →  aggregate-flow

Each stage is a verb call followed by a deterministic branch on its typed
outcome. ``campaign-run`` runs those branches in code and returns a single
``stage_reached``, so the driver stops hand-walking (and hand-branching) the
three composites for every iteration.

Scope: ONE iteration's spine only. The campaign CURSOR / manifest
advancement — advance vs. converge, budget accounting, target checks — is
NOT part of this composite; those stay judgement escalations owned by the
campaign driver. ``campaign-run`` runs the deterministic remainder and hands
the genuine decisions back as data.

Escalation-as-data (#231): ``needs_decision=True`` only on the failure /
budget stages (``submit_failed`` / ``run_failed`` / ``run_timeout`` /
``run_abandoned`` / ``aggregate_failed``). ``complete`` is the clean terminal
the driver proceeds from to its own advance/converge judgement.

**Additive.** Does not replace the per-composite path — it is a new verb the
driver may adopt. Nothing breaks if it is not yet wired in.

I/O contracts:

* Input: ``schemas/campaign_run.input.json`` (from ``CampaignRunSpec``).
* Output: ``schemas/campaign_run.output.json`` (from ``CampaignRunResult``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.campaign_run import (
    CampaignRunResult,
    CampaignRunSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.env_flags import active_env_overrides
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.ops.status_pipeline import status_pipeline
from hpc_agent.ops.submit_pipeline import submit_pipeline
from hpc_agent.state.block_terminal import terminal_block_key
from hpc_agent.state.runs import read_run_cmd_sha

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["campaign_run"]


# The block-terminal store, the detached lease, and the doctor dead-worker scan
# all key a detached campaign-run under its VERB ("campaign-run") — the SAME
# string ``_spawn_detached`` stamps into the lease AND the SAME run_id
# ``_block_spec_run_id`` digs out of the spec (``spec.aggregate.run_id``, since a
# campaign spec carries no ``submit.submit.run_id``). Sourced from the ONE key
# derivation so this recorder can never drift from the replay reader / doctor scan.
_CAMPAIGN_BLOCK_KEY = terminal_block_key("campaign-run")


def _detached_campaign_spec_dict(spec: CampaignRunSpec) -> dict[str, Any]:
    """Serialize *spec* with ``detach`` forced OFF for the detached child.

    The child runs the SAME campaign-run body synchronously (the whole iteration
    spine IS the point), so its spec must carry ``detach=False`` — a truthy detach
    would fork forever.
    """
    return spec.model_copy(update={"detach": False}).model_dump(mode="json")


def _replay_campaign_terminal(experiment_dir: Path, run_id: str) -> CampaignRunResult | None:
    """Return a finished campaign-run worker's recorded terminal for the CURRENT
    tree, else ``None`` (run #7 idempotent re-invoke).

    Replays ONLY when the current sidecar ``cmd_sha`` equals the one recorded with
    the terminal — proof the iteration outcome still applies (a re-invoke must not
    re-submit a fresh array). A moved/absent ``cmd_sha``, an absent record, or a
    corrupt record all return ``None`` so the caller spawns a fresh iteration.
    """
    from hpc_agent.state.block_terminal import read_terminal

    record = read_terminal(experiment_dir, run_id, _CAMPAIGN_BLOCK_KEY)
    if record is None:
        return None
    current_sha = read_run_cmd_sha(experiment_dir, run_id)
    if not current_sha or str(record.get("cmd_sha") or "") != current_sha:
        return None
    try:
        return CampaignRunResult.model_validate(record["result"])
    except (KeyError, TypeError, ValueError):
        return None


def _record_campaign_terminal(
    experiment_dir: Path, *, key_run_id: str, result: CampaignRunResult
) -> None:
    """Record a genuine campaign-run terminal so a re-invoke replays it.

    Keyed on *key_run_id* — ``spec.aggregate.run_id``, the SAME value
    ``_block_spec_run_id`` uses for the lease/poll — NOT ``result.run_id`` (which
    is threaded from the submit sub-stage and may differ), so the parent's replay
    (which only has the spec) reads the same key. The detached HANDLE
    (``stage_reached="detached"``) is never recorded — the child records its real
    terminal, and only that.
    """
    if not key_run_id or result.stage_reached == "detached":
        return
    from hpc_agent.state.block_terminal import record_terminal

    record_terminal(
        experiment_dir,
        run_id=key_run_id,
        block=_CAMPAIGN_BLOCK_KEY,
        cmd_sha=read_run_cmd_sha(experiment_dir, key_run_id),
        result_dump=result.model_dump(mode="json"),
    )


def _detached_campaign_result(*, run_id: str, pid: int, log_path: str | None) -> CampaignRunResult:
    """The immediate-return handle for a detached campaign-run (design §3).

    ``needs_decision`` is False (nothing to decide yet — the iteration outcome, and
    its relay-due marker, arrive on completion, read from the journal).
    ``block_drive._chain`` / ``wait-detached`` exit on this via the
    ``started`` / ``watch`` / ``detached_pid`` handle.
    """
    return CampaignRunResult(
        stage_reached="detached",
        needs_decision=False,
        reason=(
            "campaign-run detached — the whole submit→monitor→aggregate iteration "
            "runs in a durable background worker; its outcome (and relay-due marker) "
            "arrives on completion (read the journal)."
        ),
        run_id=run_id,
        started=True,
        watch="journal",
        detached_pid=pid,
    )


@primitive(
    name="campaign-run",
    verb="workflow",
    composes=["submit-pipeline", "status-pipeline", "aggregate-flow"],
    side_effects=[
        SideEffect("scheduler-submit", "<cluster>"),
        SideEffect("ssh", "<cluster> (canary poll + status polls + aggregate pull)"),
        SideEffect(
            "writes-aggregate-output",
            "<experiment_dir>/_aggregated/<run_id>/ (+ follow-up specs, tick log)",
        ),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="campaign.run.run_id",
    cli=CliShape(
        help=(
            "One campaign iteration's spine in one call: submit-pipeline → "
            "status-pipeline → aggregate-flow. Reports one typed stage_reached "
            "outcome; sets needs_decision only on the failure / budget stages. "
            "Does NOT advance the campaign cursor — that stays a driver judgement."
        ),
        spec_arg=True,
        spec_model=CampaignRunSpec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="campaign_run"),
    ),
    agent_facing=True,
)
def campaign_run(experiment_dir: Path, *, spec: CampaignRunSpec) -> CampaignRunResult:
    """Run one iteration's submit→monitor→aggregate spine; return one outcome.

    Returns a single :class:`CampaignRunResult`; ``stage_reached`` is the
    deterministic dispatch over the three sub-stages, and ``needs_decision``
    is set only on the failure / budget stages. ``complete`` is the clean
    terminal — the driver's advance/converge judgement for the campaign
    CURSOR is NOT part of this composite and runs after it returns.

    RELAY-DUE (run-#10 #13, the omission gate's second source): every
    terminal outcome of this composite — the failure stages AND ``complete``
    — journals a relay-due marker on the campaign scope, so a driving agent
    that reads the outcome from a background log and moves on without
    relaying it gets stopped once by the relay-audit hook (the exact conduct
    strike of run #10: two exit-1 iterations were never surfaced). Marking is
    fail-open — a marker write error never fails the iteration.

    DETACH-BY-CONTRACT (design §3; run-#10 F-K): the detach seat wraps OUTSIDE
    ``_campaign_run_impl`` AND the relay-due seam below, so with ``detach`` ON
    (default) the PARENT returns a handle immediately (no relay-due yet — nothing
    terminal), and the detached CHILD re-enters this SAME body with
    ``detach=False``, runs the impl, arms the relay-due marker on its terminal,
    and records that terminal for the parent's idempotent replay. So a detached
    iteration still arms relay-due exactly once, on its real outcome.
    """
    if spec.detach:
        # The journal-poll key is spec.aggregate.run_id (what _block_spec_run_id
        # digs out — a campaign spec has no submit.submit.run_id), so the lease,
        # the handle, and the terminal record all agree on one run_id.
        key_run_id = spec.aggregate.run_id
        replay = _replay_campaign_terminal(experiment_dir, key_run_id)
        if replay is not None:
            # Re-stamp the LIVE env on a replayed terminal: env-vs-record drift
            # (B15) is about what is exported NOW, not what was exported when the
            # worker recorded its outcome — the current environment is the one
            # that would reroute a re-invoke.
            replay.active_env_overrides = active_env_overrides()
            return replay

        from hpc_agent._kernel.lifecycle.detached import launch_submit_block_detached

        launch = launch_submit_block_detached(
            verb="campaign-run",
            experiment_dir=str(experiment_dir),
            spec=_detached_campaign_spec_dict(spec),
        )
        handle = _detached_campaign_result(
            run_id=launch.run_id, pid=launch.pid, log_path=launch.log_path
        )
        handle.active_env_overrides = active_env_overrides()
        return handle

    result = _campaign_run_impl(experiment_dir, spec=spec)
    # Env-echo disclosure (B15): every campaign brief carries the live HPC_*
    # overrides, echoed the way doctor does — one seat here covers every
    # stage_reached the impl returns (pure disclosure, never judged).
    result.active_env_overrides = active_env_overrides()
    try:
        if result.stage_reached in _RELAY_DUE_STAGES:
            from hpc_agent.state.notebook_audit import record_scope_relay_due

            record_scope_relay_due(
                experiment_dir,
                scope_kind="campaign",
                scope_id=str(spec.campaign_id or ""),
                record_kind="campaign-run",
                key_tokens=[result.stage_reached, str(result.run_id or spec.campaign_id)],
            )
    except Exception:  # noqa: BLE001 — the gate must never fail the work it guards
        pass
    # Record the genuine terminal (keyed by spec.aggregate.run_id, the lease key)
    # so a re-invoke after the detached worker exits REPLAYS this outcome instead
    # of re-submitting a fresh array. Runs on the synchronous path — which is what
    # the detached child executes.
    _record_campaign_terminal(experiment_dir, key_run_id=spec.aggregate.run_id, result=result)
    return result


#: Terminal stages that arm a relay-due marker — every outcome a human must
#: see. Deliberately the FULL terminal set (failures + complete): a campaign
#: iteration's outcome is always load-bearing; the in-flight stages are not.
_RELAY_DUE_STAGES = frozenset(
    {
        "submit_failed",
        "run_failed",
        "run_abandoned",
        "run_timeout",
        "aggregate_failed",
        "complete",
    }
)


def _campaign_run_impl(experiment_dir: Path, *, spec: CampaignRunSpec) -> CampaignRunResult:
    cid = spec.campaign_id

    # 1. Submit spine. A gate failure (canary / post-qsub health) stops before
    #    we ever monitor — the array never landed clean, so there is nothing to
    #    watch. A `deduped` outcome still proceeds: the run already exists/live,
    #    so we monitor the existing run.
    sp = submit_pipeline(experiment_dir, spec=spec.submit)
    # `parents_not_ready` is the DAG readiness refusal: the submit did NOT run
    # (no scheduler job ids). It must stop here like the gate failures — falling
    # through would monitor a never-submitted run_id and raise an uncaught
    # PreconditionFailed/JournalCorrupt out of the status stage instead of
    # returning a clean `submit_failed` needs-decision result.
    if sp.stage_reached in {
        "canary_failed",
        "verify_submitted_failed",
        "parents_not_ready",
    }:
        return CampaignRunResult(
            stage_reached="submit_failed",
            needs_decision=True,
            reason=(
                f"submit spine stopped at {sp.stage_reached!r}: {sp.reason} "
                "Do NOT monitor — fix the dispatch and re-invoke this iteration."
            ),
            campaign_id=cid,
            run_id=sp.run_id,
            job_ids=list(sp.job_ids),
        )

    # Thread the run we ACTUALLY submitted (sp.run_id) into the monitor + aggregate
    # specs. The per-step loop passes "the returned run_id" to monitor-flow /
    # aggregate-flow; doing the same here makes the composite robust instead of
    # trusting the caller to have pre-aligned all three sub-specs' run_id — a
    # mismatch would otherwise monitor / aggregate the WRONG run silently. On a
    # `deduped` submit, sp.run_id is the already-live run, which is what we want.
    run_id = sp.run_id
    status_spec = spec.status.model_copy(
        update={"monitor": spec.status.monitor.model_copy(update={"run_id": run_id})}
    )
    aggregate_spec = spec.aggregate.model_copy(update={"run_id": run_id})

    # 2. Monitor spine. Only a `complete` lifecycle proceeds to aggregate;
    #    failed / abandoned / timeout each stop with needs_decision=True (we
    #    cannot aggregate a run that did not complete). timeout is the budget
    #    case: the jobs are still live, so the driver re-invokes to keep
    #    watching — surfaced as run_failed would be wrong, so it gets its own
    #    clean non-aggregated outcome under the abandoned/failed family with a
    #    re-invoke reason.
    st = status_pipeline(experiment_dir, spec=status_spec)
    if st.stage_reached == "failed":
        return CampaignRunResult(
            stage_reached="run_failed",
            needs_decision=True,
            reason=(
                f"run reached a terminal failed state: {st.reason} "
                "Classify the failed tasks (recoverable vs not) and decide "
                "resubmit-failed / `reconcile --run-id <id> --scheduler <backend>` "
                "before re-invoking."
            ),
            campaign_id=cid,
            run_id=st.run_id,
            job_ids=list(sp.job_ids),
            lifecycle_state=st.lifecycle_state,
        )
    if st.stage_reached == "abandoned":
        return CampaignRunResult(
            stage_reached="run_abandoned",
            needs_decision=True,
            reason=(
                f"recorded jobs are no longer known to the scheduler: {st.reason} "
                "Run `reconcile --run-id <id> --scheduler <backend>` to confirm "
                "before re-submitting this iteration."
            ),
            campaign_id=cid,
            run_id=st.run_id,
            job_ids=list(sp.job_ids),
            lifecycle_state=st.lifecycle_state,
        )
    if st.stage_reached == "timeout":
        # Budget elapsed with the cluster jobs still live — we can't aggregate
        # yet, but nothing failed. Its own `run_timeout` stage (NOT run_failed)
        # keeps the distinction honest; needs_decision=True so the driver
        # re-invokes to keep watching.
        return CampaignRunResult(
            stage_reached="run_timeout",
            needs_decision=True,
            reason=(
                "wall-clock budget elapsed with the cluster jobs still live; the "
                "run did not reach complete, so this iteration cannot aggregate "
                "yet. Re-invoke to keep watching (nothing failed — budget only)."
            ),
            campaign_id=cid,
            run_id=st.run_id,
            job_ids=list(sp.job_ids),
            lifecycle_state=st.lifecycle_state,
        )

    # st.stage_reached == "complete" — the only lifecycle that aggregates.

    # 3. Aggregate spine. A raised error (combiner/outputs/ssh) OR a partial
    #    success (non-null escalation_reason: some wave did not combine) is an
    #    aggregate_failed escalation — the caller inspects failed_waves and
    #    decides whether the partial is acceptable. A clean result is the
    #    iteration's terminal `complete`.
    try:
        agg = aggregate_flow(experiment_dir, spec=aggregate_spec)
    except errors.HpcError as exc:
        return CampaignRunResult(
            stage_reached="aggregate_failed",
            needs_decision=True,
            reason=(
                f"aggregate spine errored ({type(exc).__name__}: {exc}). The run "
                "completed but its results did not aggregate; inspect and re-invoke "
                "the aggregate step."
            ),
            campaign_id=cid,
            run_id=st.run_id,
            job_ids=list(sp.job_ids),
            lifecycle_state=st.lifecycle_state,
        )

    if agg.escalation_reason is not None:
        return CampaignRunResult(
            stage_reached="aggregate_failed",
            needs_decision=True,
            reason=(
                f"aggregate spine returned a partial result "
                f"(escalation_reason={agg.escalation_reason!r}); some waves did not "
                "combine. Inspect failed_waves and decide whether the partial "
                "aggregate is acceptable."
            ),
            campaign_id=cid,
            run_id=st.run_id,
            job_ids=list(sp.job_ids),
            lifecycle_state=st.lifecycle_state,
            aggregate_result=agg.to_envelope_data(),
        )

    return CampaignRunResult(
        stage_reached="complete",
        needs_decision=False,
        reason=(
            "iteration spine complete: submitted, run reached complete, results "
            "aggregated cleanly. Hand back to the driver for advance/converge."
        ),
        campaign_id=cid,
        run_id=st.run_id,
        job_ids=list(sp.job_ids),
        lifecycle_state=st.lifecycle_state,
        aggregate_result=agg.to_envelope_data(),
    )
