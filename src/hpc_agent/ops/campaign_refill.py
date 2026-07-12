"""``campaign-refill``: the RFC #362 refill ACTOR, re-homed onto block-drive.

``campaign-advance`` (``meta/campaign/atoms/advance.py::_refill``) is the pure
AUTHORITY: each tick it decides whether a greenlit async campaign has a free
pool slot with budget headroom (``decision=="refill"``, carrying
``refill_count``). It never submits. ``campaign-refill`` is the side-effecting
arm that consumes that decision and tops the pool back up — the refill arm the
deleted ``deterministic_resolver`` used to carry, now a first-class primitive
sitting on the same block-drive spine as ``campaign-run``.

Per tick (``campaign_refill``):

1. Refuse an un-greenlit campaign — greenlight is the STANDING CONSENT for
   autonomous refill (human-amplification §4). Iterations carry no
   per-iteration human boundary; the guard FIRES on ``manifest.greenlit`` unset.
2. Call ``campaign-advance`` authoritatively (``advance.py::campaign_advance``,
   the SAME call ``load_context._async_should_refill`` routes on, so routing
   target == refill target). ``decision != "refill"`` → typed ``no_refill_needed``
   no-op carrying the decision. Else ``n = refill_count``.
3. Reconstruct the iteration submit context ONCE from the newest campaign
   sidecar (``_build_iteration_resolve_spec`` — no driver memory; every byte read
   from disk), then for each of ``n`` slots, SEQUENTIALLY:
   ``resolve-submit-inputs`` (``ops/resolve_submit_inputs.py::resolve_submit_inputs``)
   → wrap ``resolved`` output into a ``campaign-run`` spec → ``campaign_run(detach=True)``.

STRICTLY SEQUENTIAL, sidecar-between-slots (RFC E4 — LOAD-BEARING): the async
scaffold (``execution/mapreduce/templates/scaffolds/optuna_async_strategy.py::_submitted_count``)
indexes its proposals by the campaign sidecar count and CACHES per index. Two
slots that ``ask()`` at the same ``_submitted_count`` return the SAME cached
proposal → same cmd_sha → the second resolve stops at ``prior_run_found``.
``resolve-submit-inputs`` writes the sidecar on its ``resolved`` path, bumping
``_submitted_count`` by one — so slot *i* MUST fully complete (through the
sidecar write) before slot *i+1* starts. Do NOT batch-build all K specs; do NOT
parallelize slots.

Resolve+submit-per-slot atomically (RFC E5): ``campaign_run`` is called
IMMEDIATELY after each slot's ``resolve-submit-inputs``, inside the same loop
iteration, to minimize the crash window between the sidecar write and the
detached child spawn. A crash there leaves an ORPHAN: a cluster sidecar
(``<exp>/.hpc/runs/<run_id>.json``) with no journal ``RunRecord`` yet — the
detached child writes that record via ``ops/submit/runner.py::submit_and_record``
(``upsert_run``), AFTER child startup, so the sidecar write does NOT raise
``in_flight``. Two facts bound the residue, and NEITHER is ``in_flight``:

* No DOUBLE-SUBMIT (the real safety property): the async scaffold indexes its
  proposal by the campaign SIDECAR count
  (``optuna_async_strategy.py::_submitted_count`` == ``len(prior_records(...))``)
  and caches per index — the orphan's sidecar consumes its index, so the
  replacement slot asks the NEXT trial (a genuinely distinct run), never a
  re-qsub of the orphan.
* refill_count SHRINKS next tick ONLY through the BUDGET arm: the orphan sidecar
  raises ``campaign-budget``'s sidecar-counted ``spent_jobs``, so
  ``remaining_max_jobs`` drops. This self-correction is CAP-DEPENDENT — with no
  ``max_jobs`` budget the orphan does NOT shrink ``pool_room`` (``K − in_flight``
  over the JOURNAL, which the orphan never touched); the slot is then the §10-E5
  "stranded trial" ``load-context`` flags and ``doctor`` reconciles, not a pool
  that silently self-heals over ``in_flight``.

One-step-per-tick re-entry (``infra/block_chain.py::SUCCESSORS``): every
``campaign-refill`` stage maps to a chain END; the next cron/loop tick re-enters
via ``campaign-watch`` → ``watching_refill`` → ``campaign-refill``.

I/O contracts:

* Input: ``schemas/campaign_refill.input.json`` (from ``CampaignRefillSpec``).
* Output: ``schemas/campaign_refill.output.json`` (from ``CampaignRefillResult``).
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.campaign_refill import (
    BlockedSlot,
    CampaignRefillResult,
    CampaignRefillSpec,
    SubmittedIteration,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.env_flags import active_env_overrides

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from hpc_agent._wire.workflows.campaign_run import CampaignRunSpec
    from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec

__all__ = ["campaign_refill"]

_CAMPAIGN_ID_ENV = "HPC_CAMPAIGN_ID"


@contextlib.contextmanager
def _exported_campaign_id(campaign_id: str) -> Iterator[None]:
    """Export ``HPC_CAMPAIGN_ID`` for this tick's resolve / compute-run-id calls.

    Per-slot trial distinctness is LOAD-BEARING and driven by the async scaffold's
    ``_submitted_count()`` == ``len(prior_records(exp, HPC_CAMPAIGN_ID))``
    (``execution/mapreduce/templates/scaffolds/optuna_async_strategy.py``). That
    count reads the campaign id from ``os.environ[HPC_CAMPAIGN_ID]`` at
    task-module materialization time — ``compute-run-id`` re-execs ``tasks.py``
    FRESH each call (``hpc_agent.load_tasks_module``), so the module-top
    ``_CID = os.environ.get(...)`` is re-read live. If the var is UNSET — and
    ``campaign-refill`` is a self-contained agent-facing / MCP primitive reachable
    WITHOUT the ambient export a cron/driver would set (the load-context delegate
    prompt hands an agent the bare ``--spec {"campaign_id": ...}`` invocation) —
    every slot reads ``_submitted_count()==0`` → the SAME cached proposal →
    identical ``cmd_sha`` / ``run_id`` → the pool collapses to ONE real iteration.
    The id is known (``spec.campaign_id``), so export it around the resolve loop
    rather than trust the caller's shell, then RESTORE the prior value so the
    result's ``active_env_overrides`` discloses the ambient env, not this
    transient set.
    """
    prior = os.environ.get(_CAMPAIGN_ID_ENV)
    os.environ[_CAMPAIGN_ID_ENV] = campaign_id
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(_CAMPAIGN_ID_ENV, None)
        else:
            os.environ[_CAMPAIGN_ID_ENV] = prior

# resolve-submit-inputs placeholders for the run identity: compute-run-id inside
# resolve OVERRIDES both from the freshly-materialized task list, so these only
# need to satisfy the wire regex (RunIdStrict / ^[0-9a-f]{8,64}$). Mirrors
# ops/scaffold_spec.py's _PH_RUN_ID / _PH_CMD_SHA.
_PH_RUN_ID = "PLACEHOLDER-run-id"
_PH_CMD_SHA = "0" * 8


def _build_iteration_resolve_spec(
    experiment_dir: Path, campaign_id: str
) -> ResolveSubmitInputsSpec:
    """Reconstruct the next iteration's ``resolve-submit-inputs`` spec from disk.

    No driver memory (NON-NEGOTIABLE): every field is read from the campaign's
    NEWEST run — its journal RunRecord (transport: ssh_target / backend) + its
    sidecar (the v2 config snapshot: the REAL per-task executor, result-dir
    template, resources, env) — plus ``clusters.yaml`` for the live ssh_target.
    The prior iteration already submitted, so it is the guaranteed-good template
    for the next; ``run_name`` (the campaign's stable ``profile``) is shared,
    while ``compute-run-id`` inside resolve derives a DISTINCT cmd_sha per slot.

    The ``executor`` is read DIRECTLY from the prior sidecar — NOT a placeholder:
    ``resolve-submit-inputs`` only overrides it when ``interview.json`` exists
    (``resolve_submit_inputs.py::_materialized_executor_cmd``), so for a bare
    ``@register_run`` campaign the prior sidecar's executor is the only reliable
    source. ``total_tasks`` / ``task_count`` come from ``compute-run-id``'s
    ``total`` (RFC §3.3) so they can never disagree with resolve's own
    ``tasks.total()`` cross-check (``resolve_submit_inputs.py`` step 2b).
    """
    from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
    from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
    from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec
    from hpc_agent.incorporation.build.compute_run_id import compute_run_id
    from hpc_agent.infra.clusters import (
        ClusterConfig,
        load_clusters_config,
        resolve_ssh_target,
    )
    from hpc_agent.state.index import find_runs_by_campaign
    from hpc_agent.state.runs import read_run_sidecar

    records = find_runs_by_campaign(experiment_dir, campaign_id)
    if not records:
        # advance decided refill, so there is history to reconstruct from; an
        # empty set means the journal disagrees with advance — a loud refusal,
        # never a silent no-op that would submit a spec built from placeholders.
        raise errors.SpecInvalid(
            f"campaign {campaign_id!r} advance decided refill but has no prior run "
            "to reconstruct the next iteration's submit context from; a first "
            "iteration is submitted by the greenlight/submit flow, not by refill."
        )
    prior = records[-1]  # find_runs_by_campaign is oldest-first → newest last.
    sidecar = read_run_sidecar(experiment_dir, prior.run_id)

    profile = sidecar.get("profile") or prior.profile
    cluster = sidecar.get("cluster") or prior.cluster
    remote_path = sidecar.get("remote_path") or prior.remote_path
    executor = sidecar.get("executor")
    if not executor:
        raise errors.SpecInvalid(
            f"prior run {prior.run_id!r} sidecar carries no ``executor`` — cannot "
            "reconstruct the next iteration's per-task command for refill."
        )

    ssh_target = resolve_ssh_target(prior)

    # backend: honour the prior record's provenance; fall back to the cluster's
    # scheduler family from clusters.yaml when the record predates the field.
    backend = prior.backend or ""
    if not backend:
        try:
            clusters = load_clusters_config()
            cfg = clusters.get(cluster)
            if cfg is not None:
                backend = str(ClusterConfig.model_validate(cfg).scheduler)
        except Exception:  # noqa: BLE001 — best-effort; a bad yaml surfaces at resolve/submit.
            backend = ""
    if not backend:
        raise errors.SpecInvalid(
            f"cannot resolve a scheduler backend for campaign {campaign_id!r} "
            f"(prior run {prior.run_id!r} records none and clusters.yaml#{cluster} "
            "has no scheduler); refill needs it to build the submit spec."
        )

    # total: compute-run-id's materialized count is authoritative and idempotent
    # with the compute-run-id resolve runs internally (same _submitted_count →
    # same cached proposal), so it can never disagree with resolve's cross-check.
    total = int(compute_run_id(experiment_dir, run_name=profile)["total"])
    result_dir_template = sidecar.get("result_dir_template")

    submit = BuildSubmitSpecInput(
        profile=profile,
        cluster=cluster,
        ssh_target=ssh_target,
        remote_path=remote_path,
        run_id=_PH_RUN_ID,
        cmd_sha=_PH_CMD_SHA,
        total_tasks=total,
        backend=backend,
        result_dir_template=result_dir_template,
        campaign_id=campaign_id,
        # conda_source / conda_env / modules are left None: build-submit-spec
        # (run inside resolve-submit-inputs) BACKFILLS the COHERENT conda pair
        # from clusters.yaml#<cluster> when all three are absent
        # (incorporation/build/submit_spec.py::_resolve_activation, the same
        # source ops/scaffold_spec.py::_build_submit_block draws from). The prior
        # sidecar stores conda only inside its ``env`` dict, never as top-level
        # conda_source/conda_env, so reading those keys here would silently be None.
        runtime=sidecar.get("runtime"),
    )
    sidecar_input = WriteRunSidecarInput(
        run_id=_PH_RUN_ID,
        cmd_sha=_PH_CMD_SHA,
        executor=executor,
        result_dir_template=result_dir_template,
        task_count=total,
        cluster=cluster,
        profile=profile,
        remote_path=remote_path,
        campaign_id=campaign_id,
        resources=sidecar.get("resources"),
        env=sidecar.get("env"),
        runtime=sidecar.get("runtime"),
        aggregate_defaults=sidecar.get("aggregate_defaults"),
    )
    return ResolveSubmitInputsSpec(run_name=profile, submit=submit, sidecar=sidecar_input)


def _wrap_campaign_run_spec(
    campaign_id: str, run_id: str, submit_spec: dict[str, Any]
) -> CampaignRunSpec:
    """Nest a resolved submit-flow spec into a detached ``CampaignRunSpec``.

    ``rr.submit_spec`` is a submit-FLOW dict; ``CampaignRunSpec.submit`` is a
    ``SubmitPipelineSpec`` → ``SubmitAndVerifySpec`` → submit-flow, so it nests
    three deep (verified against ``ops/scaffold_spec.py::_scaffold_campaign_run``).
    ``aggregate.run_id`` is LOAD-BEARING — ``campaign_run``'s detached
    lease/poll/terminal key is ``spec.aggregate.run_id``
    (``ops/campaign_run.py::campaign_run``), so it MUST equal the run resolve
    computed. Monitor/aggregate fill their other fields from schema defaults.
    """
    from hpc_agent._wire.workflows.campaign_run import CampaignRunSpec

    return CampaignRunSpec.model_validate(
        {
            "submit": {"submit": {"submit": submit_spec}},
            "status": {"monitor": {"run_id": run_id}},
            "aggregate": {"run_id": run_id},
            "campaign_id": campaign_id,
            "detach": True,
        }
    )


@primitive(
    name="campaign-refill",
    verb="workflow",
    composes=["campaign-advance", "resolve-submit-inputs", "campaign-run"],
    side_effects=[
        SideEffect("scheduler-submit", "<cluster> (per refilled slot)"),
        SideEffect(
            "writes-campaign-state",
            "<experiment_dir>/.hpc/runs/<run_id>.json (per refilled slot)",
        ),
    ],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable, errors.ClusterUnknown],
    idempotent=True,
    idempotency_key="spec.campaign_id",
    cli=CliShape(
        help=(
            "RFC #362 refill actor: top a greenlit async campaign's pool back up "
            "this tick. Calls campaign-advance authoritatively; if it decides "
            "refill, resolves + submits refill_count iterations SEQUENTIALLY "
            "(each a detached campaign-run). Refuses an un-greenlit campaign "
            "(greenlight is the standing consent). Stages: refilled / "
            "no_refill_needed / refill_blocked. No new state file, no cursor — "
            "partial ticks self-correct via next tick's shrunk refill_count."
        ),
        spec_arg=True,
        spec_model=CampaignRefillSpec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="campaign_refill", output="campaign_refill"),
    ),
    agent_facing=True,
)
def campaign_refill(experiment_dir: Path, *, spec: CampaignRefillSpec) -> CampaignRefillResult:
    """Consume this tick's ``campaign-advance`` refill decision; spawn the slots.

    Returns one :class:`CampaignRefillResult`. ``needs_decision`` is set only on
    ``refill_blocked`` (a slot hit a live-prior / scaffold escalation). The
    ``refilled`` and ``no_refill_needed`` terminals end the chain; the next tick
    re-enters via ``campaign-watch`` (one-step-per-tick).

    NO driver memory, NO new state file, NO cursor: the whole decision is
    recomputed from journal state via ``campaign-advance`` each tick. Per-slot
    distinctness (no double-submit) is guaranteed by the async scaffold's
    sidecar-indexed proposal count — NOT by ``in_flight``, which a freshly
    written sidecar does not raise (the detached child writes the journal record
    later). A crash mid-tick leaves an orphan sidecar whose budget cost shrinks
    the next tick's ``refill_count`` only when a ``max_jobs`` cap is set;
    otherwise it is a stranded trial ``load-context`` / ``doctor`` reconcile (RFC
    E5). See the module docstring for the full mechanism.
    """
    from hpc_agent.meta.campaign.atoms.advance import campaign_advance
    from hpc_agent.meta.campaign.manifest import read_manifest
    from hpc_agent.ops.campaign_run import campaign_run
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    cid = spec.campaign_id

    # 1. Standing-consent guard — FIRES on an un-greenlit manifest (tested with a
    #    non-greenlit manifest). Greenlight is the one human boundary of an async
    #    campaign; refills carry none (human-amplification §4). An absent manifest
    #    is a loud SpecInvalid, mirroring campaign-greenlight (blocks.py).
    manifest = read_manifest(experiment_dir, campaign_id=cid)
    if manifest is None:
        raise errors.SpecInvalid(
            f"campaign {cid!r} has no manifest; write the spec (campaign-init) "
            "and greenlight it before autonomous refill."
        )
    if not manifest.get("greenlit"):
        raise errors.SpecInvalid(
            f"campaign {cid!r} is not greenlit; greenlight is the standing consent "
            "for autonomous refill (no per-iteration human boundary)."
        )

    # 2. Authoritative advance — the SAME call load_context._async_should_refill
    #    routes on (advance.py:110), so the routing target == the refill target.
    adv = campaign_advance(experiment_dir=experiment_dir, campaign_id=cid)
    decision = str(adv.get("decision", ""))
    if decision != "refill":
        return CampaignRefillResult(
            stage_reached="no_refill_needed",
            needs_decision=False,
            reason=(
                f"campaign {cid!r} advance decided {decision!r} (not refill): "
                f"{adv.get('reason')}. Nothing to submit this tick."
            ),
            campaign_id=cid,
            decision=decision,
            refill_count=0,
            next_block=None,
            active_env_overrides=active_env_overrides(),
        )

    n = int(adv.get("refill_count") or 0)

    # 3. Reconstruct the iteration context ONCE (disk-only), then resolve+submit
    #    each slot SEQUENTIALLY. A slot's resolve writes its sidecar (advancing
    #    the async scaffold's _submitted_count) BEFORE the next slot resolves
    #    (RFC E4), and campaign_run is spawned IMMEDIATELY after each resolve to
    #    bound the crash window (RFC E5).
    submitted: list[SubmittedIteration] = []
    blocked: list[BlockedSlot] = []
    stage = "refilled"

    # Export HPC_CAMPAIGN_ID around BOTH the disk reconstruction (its internal
    # compute-run-id) and every slot's resolve, so the async scaffold's
    # sidecar-indexed _submitted_count sees the right campaign and each slot asks a
    # DISTINCT trial — the per-slot distinctness invariant, made self-contained
    # rather than dependent on the caller's ambient shell (_exported_campaign_id).
    with _exported_campaign_id(cid):
        resolve_spec = _build_iteration_resolve_spec(experiment_dir, cid)
        for _ in range(n):
            rr = resolve_submit_inputs(experiment_dir, spec=resolve_spec)
            if rr.stage_reached != "resolved":
                # A live prior / scaffold interview is a genuine escalation — stop
                # refilling (continuing would ask more trials against an unresolved
                # slot) and hand it back for a human decision.
                blocked.append(
                    BlockedSlot(run_id=rr.run_id, stage=rr.stage_reached, reason=rr.reason)
                )
                stage = "refill_blocked"
                break
            # resolve-submit-inputs sets run_id/submit_spec on its ``resolved``
            # path; a ``resolved`` stage with either absent is a contract
            # violation, not a silent None to thread into a detached submit —
            # fail loudly.
            if rr.run_id is None or rr.submit_spec is None:
                raise errors.SpecInvalid(
                    f"campaign {cid!r} resolve-submit-inputs reported ``resolved`` but "
                    "carries no run_id / submit_spec; cannot build the refill iteration."
                )
            run_id = rr.run_id
            crspec = _wrap_campaign_run_spec(cid, run_id, rr.submit_spec)
            res = campaign_run(experiment_dir, spec=crspec)
            submitted.append(
                SubmittedIteration(
                    run_id=run_id,
                    detached_pid=res.detached_pid,
                    stage_reached=res.stage_reached,
                )
            )

    if stage == "refill_blocked":
        reason = (
            f"campaign {cid!r} refill blocked mid-tick after {len(submitted)} "
            f"submit(s): a slot stopped at {blocked[-1].stage!r} — a human must "
            "resolve it (resume-vs-fresh, or run the scaffold interview)."
        )
        needs_decision = True
    else:
        reason = (
            f"campaign {cid!r} refilled {len(submitted)} slot(s) this tick "
            f"(refill_count={n}); each iteration runs as a detached campaign-run. "
            "Next tick re-enters via campaign-watch."
        )
        needs_decision = False

    return CampaignRefillResult(
        stage_reached=stage,
        needs_decision=needs_decision,
        reason=reason,
        campaign_id=cid,
        decision="refill",
        refill_count=n,
        submitted=submitted,
        blocked=blocked,
        next_block=None,
        active_env_overrides=active_env_overrides(),
    )
