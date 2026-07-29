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
2. RE-ACTUATE this campaign's STRANDED placements (``_recover_stranded``) —
   items it placed and never started, which ``queue-advance`` never re-offers
   (it reads only ``queued``) and which occupy pool slots until they run. Before
   and independently of the decision below, because those items ARE the
   occupancy: asking for pool room first returns ``wait_in_flight`` and the
   residue could never be cleared.
3. Call ``campaign-advance`` authoritatively (``advance.py::campaign_advance``,
   the SAME call ``load_context._async_should_refill`` routes on, so routing
   target == refill target). ``decision != "refill"`` → typed ``no_refill_needed``
   no-op carrying the decision. Else ``n = refill_count``.
4. Reconstruct the iteration submit context ONCE from the newest campaign
   sidecar (``_build_iteration_resolve_spec`` — no driver memory; every byte read
   from disk), then for each of ``n`` slots, SEQUENTIALLY, under the campaign's
   durable dispatch lock (``state/queue_locks.campaign_dispatch_lock``): re-ask
   the authority for pool room (the count in step 3 was taken before any lock
   existed, so two overlapping passes would each submit a full pool) →
   ``resolve-submit-inputs`` (``ops/resolve_submit_inputs.py::resolve_submit_inputs``)
   → ``queue-run`` (the item lands on the intake ledger carrying the resolved
   identity) → ``queue-dispatch`` of THAT item.

LEDGER PRODUCER, not submitter (run-queue plan §10.S3 / D5). Phase 2 retires the
direct ``campaign_run(detach=True)`` call this file used to make. A refill slot
now ENQUEUES its resolved trial and hands it to the ONE dispatcher; after this
change ``queue-dispatch`` is the only submitter on the refill path, which is what
makes "who submitted this?" answerable from the ledger instead of from whichever
process happened to be running a loop. The ordering — resolve, THEN enqueue,
THEN dispatch — is the whole point: resolving consumes the optuna sidecar index
exactly once (E4 below), and enqueueing BEFORE the spawn is what closes S3's
window, because ``campaign-advance``'s pool arithmetic counts queued intake
(``state/queue_occupancy.occupied_slots``, D6). A crash between the enqueue and
the dispatch therefore leaves a *visible queued item* that shrinks the next
tick's ``refill_count`` — the residue is on the ledger with a reason (R4/D9)
rather than invisible to the pool.

STRICTLY SEQUENTIAL, sidecar-between-slots (RFC E4 — LOAD-BEARING): the async
scaffold (``execution/mapreduce/templates/scaffolds/optuna_async_strategy.py::_submitted_count``)
indexes its proposals by the campaign sidecar count and CACHES per index. Two
slots that ``ask()`` at the same ``_submitted_count`` return the SAME cached
proposal → same cmd_sha → the second resolve stops at ``prior_run_found``.
``resolve-submit-inputs`` writes the sidecar on its ``resolved`` path, bumping
``_submitted_count`` by one — so slot *i* MUST fully complete (through the
sidecar write) before slot *i+1* starts. Do NOT batch-build all K specs; do NOT
parallelize slots. Until Phase 2 that rule was an in-process ``for``-loop
convention, true only because exactly one refill loop ran at a time; the queue
gives the ledger a second submitter path any session may fire, so each slot now
holds the DURABLE per-campaign lock (§10.S2.4 / D3) across its whole
resolve → sidecar → dispatch window. The lock is re-entrant within one process,
so the nested ``queue-dispatch`` call takes it too and does not self-deadlock.

Resolve+enqueue+dispatch-per-slot atomically (RFC E5): the enqueue and the
dispatch happen IMMEDIATELY after each slot's ``resolve-submit-inputs``, inside
the same loop iteration and the same lock, to minimize the crash window between
the sidecar write and the started lifecycle. Three facts bound the residue of a
crash inside that window, and none of them is ``in_flight``:

* No DOUBLE-SUBMIT (the real safety property): the async scaffold indexes its
  proposal by the campaign SIDECAR count
  (``optuna_async_strategy.py::_submitted_count`` == ``len(prior_records(...))``)
  and caches per index — the orphan's sidecar consumes its index, so the
  replacement slot asks the NEXT trial (a genuinely distinct run), never a
  re-qsub of the orphan.
* A crash AFTER the enqueue shrinks ``pool_room`` next tick, unconditionally
  (§10.S3 / D6): the queued item occupies a pool slot through the shared
  occupancy predicate, so ``campaign-advance`` asks for one fewer slot and
  ``queue-status`` shows the un-dispatched item with its reason. This is the
  window that used to be invisible — ``K − in_flight`` over the JOURNAL, which
  an enqueued-but-undispatched slot never touched.
* A crash BEFORE the enqueue (between the sidecar write and the ledger append)
  still self-corrects only through the BUDGET arm: the orphan sidecar raises
  ``campaign-budget``'s sidecar-counted ``spent_jobs``, so ``remaining_max_jobs``
  drops. That arm is CAP-DEPENDENT, so with no ``max_jobs`` budget the slot is
  the §10-E5 "stranded trial" ``load-context`` flags and ``doctor`` reconciles.
  The enqueue is placed as early as the resolved identity allows precisely to
  make this the narrow case rather than the normal one.

One-step-per-tick re-entry (``infra/block_chain.py::SUCCESSORS``): every
``campaign-refill`` stage maps to a chain END; the next cron/loop tick re-enters
via ``campaign-watch`` → ``watching_refill`` → ``campaign-refill``.

I/O contracts:

* Input: ``schemas/campaign_refill.input.json`` (from ``CampaignRefillSpec``).
* Output: ``schemas/campaign_refill.output.json`` (from ``CampaignRefillResult``).
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
from typing import TYPE_CHECKING, Literal

from pydantic import TypeAdapter

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire._shared import CampaignId, QueueItemId
from hpc_agent._wire.workflows.campaign_refill import (
    BlockedSlot,
    CampaignRefillResult,
    CampaignRefillSpec,
    SubmittedIteration,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.env_flags import active_env_overrides
from hpc_agent.state.queue_occupancy import compose_campaign_id

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from hpc_agent._wire.workflows.queue_dispatch import QueueDispatchResult
    from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec

__all__ = ["campaign_refill"]

_CAMPAIGN_ID_ENV = "HPC_CAMPAIGN_ID"

# Wire aliases as validators (the ``queue-advance`` pattern). A campaign id
# reaches this file as a bare ``str`` (``CampaignRefillSpec.campaign_id`` is
# ``min_length=1`` and nothing more), while the ledger's own fields are
# charset-constrained. Validating THROUGH the alias rather than restating its
# regex keeps the two from drifting: a value that would not survive the intake
# schema is refused here, where the operator can read why, instead of surfacing
# as a pydantic ValidationError from inside a composed verb.
_CAMPAIGN_ID: TypeAdapter[str] = TypeAdapter(CampaignId)
_QUEUE_ITEM_ID: TypeAdapter[str] = TypeAdapter(QueueItemId)

#: The slot stage for "the authority no longer has room" — the in-lock re-ask
#: (:func:`_refill_slot`). Not a dispatch refusal code and deliberately not one
#: of ``campaign-advance``'s decisions either: what happened is specific to this
#: tick, namely that the count it planned against was consumed by someone else.
_STAGE_POOL_FULL = "pool_full"

#: Slot stages that are NOT a human decision point (D9): a claim held by a peer
#: means another process is already doing this work, and a pool a peer filled
#: means the work is done. The slot still stops and is still disclosed — it is
#: simply not something to wake anyone for.
_NO_DECISION_STAGES = frozenset({"claim_held", _STAGE_POOL_FULL})


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
    result_dir_template = sidecar.get("result_dir_template")
    if not result_dir_template:
        raise errors.SpecInvalid(
            f"prior run {prior.run_id!r} sidecar carries no ``result_dir_template`` "
            "— cannot reconstruct the next iteration's per-task result dirs for refill."
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


def _campaign_base_for(campaign_id: str, cluster: str) -> str | None:
    """The logical campaign BASE behind *campaign_id*, or ``None`` when it has none.

    A concrete campaign id is COMPOSED as ``<base>_<clusterkey>``
    (``docs/design/campaign-multi-cluster.md`` §2,
    ``state/queue_occupancy.compose_campaign_id``), and that module is emphatic
    that the composition is never to be PARSED: bases routinely contain
    underscores, so splitting on ``_`` invents a base that composes back to a
    different id. This is not that split. The cluster key is KNOWN here (it came
    off the prior iteration's own sidecar), so the suffix is removed exactly and
    then the result is CHECKED by re-composing it — the composition rule stays
    the authority, and a base is returned only when it provably round-trips.

    ``None`` when the id does not end in this cluster's key (a single-cluster
    campaign named before the multi-cluster convention) or when the recovered
    base would not satisfy the ledger's ``CampaignId`` charset. That is a real
    loss and is disclosed by the caller rather than papered over: an item with
    no base composes no campaign id, so the shared occupancy predicate cannot
    count it against this campaign's pool and §10.S3's window stays open for
    that campaign. Guessing a base instead would be worse — it would count the
    slot against a campaign that does not exist.
    """
    suffix = f"_{cluster}"
    if not cluster or not campaign_id.endswith(suffix):
        return None
    base = campaign_id[: -len(suffix)]
    if not base or compose_campaign_id(base, cluster) != campaign_id:
        return None
    try:
        return _CAMPAIGN_ID.validate_python(base)
    except Exception:  # noqa: BLE001 — an unusable base is an absent base, not a crash
        return None


def _slot_request_id(campaign_id: str, run_id: str) -> str:
    """The deterministic intake token for one refill slot: ``<cid>.<run_id>``.

    ``request_id`` is the ledger's dedup key (``queue-run`` passes it straight
    through as ``append_jsonl_line``'s ``dedup_key``) AND the item's identity, so
    what it is derived from decides what "the same slot" means. It is derived
    from the RESOLVED trial identity because that is the fact a replay actually
    shares: two enqueues of the same campaign's same resolved trial are the same
    slot and must collapse to one ledger item, while the next trial — a distinct
    ``cmd_sha``, hence a distinct ``run_id`` — is a genuinely new one. A random
    token would make a retried slot a second item holding a second pool slot for
    a run that can only exist once.

    Raises :class:`~hpc_agent.errors.SpecInvalid` when the composed token would
    not satisfy the ledger's ``QueueItemId`` charset. Loud rather than
    fallback-to-uuid: a campaign whose id cannot name an item is a naming
    problem to fix, and a silent random token would quietly re-open the dedup
    the deterministic derivation exists to provide.
    """
    token = f"{campaign_id}.{run_id}"
    try:
        return _QUEUE_ITEM_ID.validate_python(token)
    except Exception as exc:
        raise errors.SpecInvalid(
            f"campaign {campaign_id!r} + run {run_id!r} compose the intake token "
            f"{token!r}, which is not a legal queue item id (allowed: letters, "
            "digits, '.', '_', '-'). The token is the ledger's dedup key, so it "
            "cannot be replaced by a random one without re-opening the "
            "double-enqueue it prevents."
        ) from exc


def _stranded_items(experiment_dir: Path, cid: str) -> list[str]:
    """Item ids this campaign PLACED and never started — oldest first.

    An item leaves ``queue-advance``'s scope the moment its placement is
    appended (advance reads only ``queued`` items, ``ops/queue/advance.py``), so
    a slot that placed an item and then failed to start it — a crash between the
    two, a gate refusal, an unresolvable cluster, a claim held by a peer that
    later died — leaves an item NO automatic path would ever offer again. It is
    not free to leave there: it is committed work, so
    ``state/queue_occupancy`` counts it against the pool for as long as it
    exists (correctly — nothing else knows the slot is spoken for), and intake
    has no terminal state to retire it with (D8). Four such items on a ``K=4``
    campaign is a permanent ``wait_in_flight`` over an empty cluster, and the
    CLI help's promise that "partial ticks self-correct via next tick's shrunk
    refill_count" is false for exactly this residue.

    So refill — the seat that produced them — re-offers them, and it does so
    BEFORE and INDEPENDENTLY of the refill decision (see :func:`campaign_refill`
    step 2b): asking for pool room first would deadlock, because the stranded
    items ARE the occupancy.

    STRANDED means placed with no RunRecord at all. An item whose run exists in
    any state is not re-actuated here: a live or complete run is already the
    dispatch (``queue-dispatch`` adopts it), and a FAILED one is a campaign
    failure-handling question, not a slot to silently resubmit — and it no
    longer wedges the pool either, because a retired run retires its item's slot
    (``state/queue_occupancy.run_occupies``).
    """
    from hpc_agent.state.index import find_runs_by_campaign
    from hpc_agent.state.queue_intake import STATE_PLACED, item_run_id, read_intake_items
    from hpc_agent.state.queue_occupancy import intake_item_campaign_id

    # The campaign's own records, read through the F46-guarded index rather than
    # per-item ``load_run`` — a per-item journal probe would mint the journal
    # namespace for an experiment that has none, from what is meant to be a read.
    started = {record.run_id for record in find_runs_by_campaign(experiment_dir, cid)}
    out: list[str] = []
    for item in read_intake_items(experiment_dir):
        if item.get("state") != STATE_PLACED:
            continue
        if intake_item_campaign_id(item) != cid:
            continue
        item_id = item.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            continue
        if item_run_id(item) in started:
            continue
        out.append(item_id)
    return out


def _recover_stranded(
    experiment_dir: Path, cid: str
) -> tuple[list[SubmittedIteration], list[BlockedSlot]]:
    """Re-actuate this campaign's stranded placements; report every one (D9).

    One ``queue-dispatch`` call naming the stranded items. NO resolve happens on
    this path and no ledger item is minted: the trial was already resolved and
    already paid for its optuna proposal index, so re-offering it is strictly
    cheaper than the fresh slot it would otherwise be replaced by — and it is
    the only way the index that was consumed ever becomes a run.

    ``queue-dispatch`` takes the per-cid E4 lock itself, per item, so this needs
    no lock of its own; it is deliberately called OUTSIDE one so the recovery of
    a campaign whose lock a peer holds reports ``claim_held`` for the items
    rather than stalling the whole tick at the acquire.

    Refusals come back as blocked slots with the dispatcher's own ``reason_code``
    and reason — never re-worded, and never swallowed: a placement that cannot
    be started is exactly the fact a human needs, and it is the fact the old
    silent wedge hid.
    """
    from hpc_agent._wire.workflows.queue_dispatch import QueueDispatchSpec
    from hpc_agent.ops.queue.dispatch import queue_dispatch

    stranded = _stranded_items(experiment_dir, cid)
    if not stranded:
        return [], []
    result = queue_dispatch(
        experiment_dir=experiment_dir,
        spec=QueueDispatchSpec(item_ids=stranded, max_dispatches=len(stranded)),
    )
    submitted = [
        SubmittedIteration(
            run_id=row.run_id,
            detached_pid=row.detached_pid,
            stage_reached=row.stage_reached or row.outcome,
        )
        for row in result.dispatched
    ]
    blocked = [
        BlockedSlot(run_id=row.run_id, stage=row.reason_code, reason=row.reason)
        for row in result.refused
    ]
    return submitted, blocked


def _recovery_note(submitted: list[SubmittedIteration], blocked: list[BlockedSlot]) -> str:
    """The recovery sentence appended to every tick's reason, or ``""``.

    Recovery is invisible in the slot counts — it starts runs without spending a
    refill slot — so a tick that quietly re-actuated three stranded placements
    would otherwise read exactly like one that did nothing. R4: say what
    happened, including on the successes.
    """
    if not submitted and not blocked:
        return ""
    parts: list[str] = []
    if submitted:
        parts.append(f"re-actuated {len(submitted)} stranded placement(s)")
    if blocked:
        codes = ", ".join(sorted({row.stage for row in blocked}))
        parts.append(f"{len(blocked)} stranded placement(s) still refused ({codes})")
    return (
        f" RECOVERY: {'; '.join(parts)} — item(s) this campaign had PLACED and never "
        "started, which queue-advance never re-offers (it reads only queued items) "
        "and which occupy pool slots until they run."
    )


def _recovery_terminal(blocked: list[BlockedSlot]) -> tuple[str | None, bool]:
    """``(stage override, needs_decision)`` for the recovery leg's own blockers.

    A stranded placement that still cannot start is the escalation the silent
    wedge used to hide, so it sets the tick's terminal even when the fresh slots
    all succeeded — reporting ``refilled`` while a placed item sits unstartable
    is how the campaign quietly ran out of pool in the first place. ``claim_held``
    and a peer-filled pool stay decision-free (:data:`_NO_DECISION_STAGES`).
    """
    if not blocked:
        return None, False
    return "refill_blocked", any(row.stage not in _NO_DECISION_STAGES for row in blocked)


@dataclasses.dataclass(frozen=True)
class _SlotOutcome:
    """What one refill slot produced — exactly one of *submitted* / *blocked*.

    A private carrier rather than a tuple so the "exactly one" invariant is
    readable at both ends, and so the replay signal rides along without widening
    either wire model. R4/D9 applies to every field of it: a slot always leaves
    behind either a dispatched iteration or a stated reason it did not.
    """

    submitted: SubmittedIteration | None = None
    blocked: BlockedSlot | None = None
    replayed: bool = False


def _refill_slot(
    experiment_dir: Path,
    *,
    cid: str,
    cluster: str,
    campaign_base: str | None,
    resolve_spec: ResolveSubmitInputsSpec,
) -> _SlotOutcome:
    """One slot: resolve → enqueue → dispatch, inside ONE per-campaign lock.

    The lock (``state/queue_locks.campaign_dispatch_lock``, §10.S2.4 / D3) spans
    the WHOLE window rather than any one call, because the sidecar write that
    consumes the optuna proposal index lands near the END of
    ``resolve-submit-inputs``: a holder that released after resolving and
    re-acquired to dispatch would leave exactly the gap E4 exists to close. It
    is re-entrant within this process, so the nested ``queue-dispatch`` call
    takes it again for free; it is exclusive across processes, so a second
    dispatcher is refused rather than allowed to propose the same trial.

    The enqueue comes BEFORE the dispatch and is durable (D5). That ordering is
    the fix for §10.S3: ``campaign-advance`` counts queued intake, so a crash
    here costs the pool one slot next tick instead of silently re-proposing the
    same one. The item carries the RESOLVED identity (``run_id`` + ``cmd_sha``)
    because the occupancy predicate only collapses an item and the run it becomes
    onto one slot when the item knows its run id — without it the handoff window
    would count one committed slot twice.

    Every failure mode returns a ``blocked`` outcome with a disclosed reason
    rather than raising: a claim held by a peer, an item queue-advance held back,
    a gate refusal and a lifecycle failure are all normal outcomes of a healthy
    fleet (R4). The two exceptions are contract violations — a ``resolved``
    stage missing its identity, and a campaign id that cannot name a ledger item
    — which stay loud because no later reader could make sense of them.
    """
    from hpc_agent._wire.actions.queue_run import QueueRunSpec
    from hpc_agent._wire.workflows.queue_dispatch import QueueDispatchSpec
    from hpc_agent.errors import QueueDispatchLockHeld
    from hpc_agent.meta.campaign.atoms.advance import campaign_advance
    from hpc_agent.ops.queue.dispatch import queue_dispatch
    from hpc_agent.ops.queue.run import queue_run
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs
    from hpc_agent.state.queue_locks import campaign_dispatch_lock, dispatch_lock_path

    # Check the lock's own naming rule FIRST and separately. The path is a pure
    # computation, so asking for it costs nothing and converts the one
    # ``ValueError`` this window can raise for a structural reason (a campaign id
    # that cannot be a filename) into a typed refusal — without wrapping the
    # whole window in an ``except ValueError``, which would also swallow every
    # pydantic ``ValidationError`` raised inside it.
    try:
        dispatch_lock_path(experiment_dir, cid)
    except ValueError as exc:
        raise errors.SpecInvalid(f"campaign {cid!r} cannot key its dispatch lock: {exc}") from exc

    try:
        with campaign_dispatch_lock(experiment_dir, cid):
            # RE-ASK THE AUTHORITY, INSIDE THE LOCK. ``refill_count`` was computed
            # by the tick's opening ``campaign-advance``, before any lock existed,
            # so two overlapping passes (§8 S8 calls overlapping passes the
            # design's own steady state, and Phase 2 gives any session a second
            # submitter path) both read occupied=0 and both plan a FULL pool. The
            # per-cid lock serializes them, which is what makes the over-fill
            # real rather than theoretical: serialized resolves consume DISTINCT
            # optuna indices, so every downstream defense — distinct cmd_sha,
            # distinct run_id, distinct item id, distinct lease, distinct
            # submit-once record — passes by construction and 2K arrays land on a
            # pool sized for K. Nothing but re-reading the count under the lock
            # bounds it, and the authority is the only thing allowed to own that
            # arithmetic (R9: one predicate, one seat).
            adv = campaign_advance(experiment_dir=experiment_dir, campaign_id=cid)
            if str(adv.get("decision", "")) != "refill" or int(adv.get("refill_count") or 0) < 1:
                return _SlotOutcome(
                    blocked=BlockedSlot(
                        run_id=None,
                        stage=_STAGE_POOL_FULL,
                        reason=(
                            f"campaign {cid!r} has no pool room left this tick — "
                            f"advance now decides {adv.get('decision')!r}: "
                            f"{adv.get('reason')}. A peer refill pass filled the "
                            "slots this tick planned for; stopping rather than "
                            "over-submitting a pool sized for K."
                        ),
                    )
                )
            rr = resolve_submit_inputs(experiment_dir, spec=resolve_spec)
            if rr.stage_reached != "resolved":
                # A live prior / scaffold interview is a genuine escalation — the
                # caller stops refilling (continuing would ask more trials against
                # an unresolved slot) and hands it back for a human decision.
                return _SlotOutcome(
                    blocked=BlockedSlot(run_id=rr.run_id, stage=rr.stage_reached, reason=rr.reason)
                )
            # resolve-submit-inputs sets run_id/cmd_sha/submit_spec on its
            # ``resolved`` path; a ``resolved`` stage missing any of them is a
            # contract violation, not a None to thread onto the ledger — the
            # identity is what a dispatcher claims and what occupancy joins on.
            if rr.run_id is None or rr.cmd_sha is None or rr.submit_spec is None:
                raise errors.SpecInvalid(
                    f"campaign {cid!r} resolve-submit-inputs reported ``resolved`` but "
                    "carries no run_id / cmd_sha / submit_spec; cannot enqueue the "
                    "refill iteration with the resolved identity §10.S3 requires."
                )
            run_id = rr.run_id

            # DURABLE FIRST (D5). queue-run is ungated and writes one ledger line
            # under the ledger's own flock; a replay of this exact resolved trial
            # returns the ORIGINAL item rather than minting a second slot.
            enqueued = queue_run(
                experiment_dir=experiment_dir,
                spec=QueueRunSpec(
                    request_id=_slot_request_id(cid, run_id),
                    # The RESOLVED submit-flow spec, verbatim — the item carries
                    # everything the dispatcher needs to start it and nothing it
                    # would have to re-derive. Enqueueing the *resolve* spec
                    # instead would force the dispatcher to resolve a second
                    # time, which would consume the next optuna proposal index
                    # and compute a DIFFERENT run id than the one on the ledger
                    # (E4). The seat that resolves is the seat that enqueues.
                    spec=rr.submit_spec,
                    run_name=resolve_spec.run_name,
                    run_id=run_id,
                    cmd_sha=rr.cmd_sha,
                    cluster=cluster,
                    campaign_base=campaign_base,
                ),
            )

            # ONE dispatcher (D5). Named by item_id so this slot starts THIS
            # item; the decision itself still belongs to queue-advance, which
            # queue-dispatch calls — refill never places anything.
            dispatched = queue_dispatch(
                experiment_dir=experiment_dir,
                spec=QueueDispatchSpec(
                    item_ids=[enqueued.item_id],
                    campaign_base=campaign_base,
                    max_dispatches=1,
                ),
            )
            return _dispatch_outcome(dispatched, run_id=run_id, replayed=enqueued.replayed)
    except QueueDispatchLockHeld as exc:
        # A healthy peer, not a failure: another process is inside this
        # campaign's resolve->start window. Nothing was resolved or enqueued.
        return _SlotOutcome(blocked=BlockedSlot(run_id=None, stage="claim_held", reason=str(exc)))


def _dispatch_outcome(
    dispatched: QueueDispatchResult, *, run_id: str, replayed: bool
) -> _SlotOutcome:
    """Fold one ``queue-dispatch`` result into this slot's outcome.

    The dispatcher's three lists are three different answers and are kept
    distinct here rather than collapsed into "did it work": ``refused`` is the
    ACTOR saying it chose a cluster and could not start; ``held`` is the
    AUTHORITY saying it never placed the item at all, relayed verbatim so the
    human reads the reason that was actually decided rather than a restatement
    of it. An empty result is the fourth case and is reported as the
    dispatcher's own terminal — R4 cuts both ways, so the slot never invents a
    cause it was not given.
    """
    if dispatched.dispatched:
        started = dispatched.dispatched[0]
        return _SlotOutcome(
            submitted=SubmittedIteration(
                run_id=started.run_id,
                detached_pid=started.detached_pid,
                # The composed verb's own stage when it has one; otherwise the
                # outcome that stands in for it ("adopted" — the run already
                # existed and this slot took it rather than submitting twice).
                stage_reached=started.stage_reached or started.outcome,
            ),
            replayed=replayed,
        )
    if dispatched.refused:
        refusal = dispatched.refused[0]
        return _SlotOutcome(
            blocked=BlockedSlot(
                run_id=refusal.run_id or run_id,
                stage=refusal.reason_code,
                reason=refusal.reason,
            ),
            replayed=replayed,
        )
    if dispatched.held:
        holdback = dispatched.held[0]
        return _SlotOutcome(
            blocked=BlockedSlot(run_id=run_id, stage=holdback.reason_code, reason=holdback.reason),
            replayed=replayed,
        )
    return _SlotOutcome(
        blocked=BlockedSlot(
            run_id=run_id, stage=dispatched.stage_reached, reason=dispatched.reason
        ),
        replayed=replayed,
    )


@primitive(
    name="campaign-refill",
    verb="workflow",
    composes=["campaign-advance", "resolve-submit-inputs", "queue-run", "queue-dispatch"],
    side_effects=[
        SideEffect("scheduler-submit", "<cluster> (per refilled slot, via queue-dispatch)"),
        SideEffect(
            "writes-campaign-state",
            "<experiment_dir>/.hpc/runs/<run_id>.json (per refilled slot)",
        ),
        SideEffect("file_write", "<experiment_dir>/.hpc/queue/intake.jsonl (per refilled slot)"),
    ],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable, errors.ClusterUnknown],
    idempotent=True,
    idempotency_key="spec.campaign_id",
    cli=CliShape(
        help=(
            "RFC #362 refill actor: top a greenlit async campaign's pool back up "
            "this tick. Calls campaign-advance authoritatively; if it decides "
            "refill, resolves refill_count iterations SEQUENTIALLY and, per slot, "
            "ENQUEUES the resolved trial on the intake ledger (queue-run) and "
            "hands it to the one dispatcher (queue-dispatch) — durable-first, so "
            "a crash between the two leaves a visible queued item that shrinks "
            "the next tick's pool room instead of an invisible orphan. Refuses "
            "an un-greenlit campaign (greenlight is the standing consent). "
            "Re-actuates this campaign's STRANDED placements first - items it "
            "placed and never started, which queue-advance never re-offers and "
            "which hold pool slots until they run. Stages: refilled / "
            "no_refill_needed / refill_blocked. No new state file, no cursor - "
            "partial ticks self-correct via the recovery leg plus next tick's "
            "shrunk refill_count."
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
    recomputed from journal + ledger state via ``campaign-advance`` each tick.
    Per-slot distinctness (no double-submit) is guaranteed by the async
    scaffold's sidecar-indexed proposal count — NOT by ``in_flight``, which a
    freshly written sidecar does not raise (the detached child writes the journal
    record later). What Phase 2 adds is that each slot's resolved trial is on the
    intake ledger BEFORE anything is started (§10.S3 / D5), so a crash mid-tick
    leaves a queued item the next tick's ``campaign-advance`` counts against the
    pool (D6) and ``queue-status`` shows — not an orphan visible only through a
    ``max_jobs`` budget arm. See the module docstring for the full mechanism.

    This verb no longer submits anything itself: it produces ledger items and
    ``queue-dispatch`` starts them (D5). It therefore also gains no gate of its
    own — the greenlight check below is refill's standing consent, and the
    cluster-boundary gates bind inside the lifecycle exactly where they bind for
    every other submit (D1).
    """
    from hpc_agent.meta.campaign.atoms.advance import campaign_advance
    from hpc_agent.meta.campaign.manifest import read_manifest

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

    # 2. RECOVERY, BEFORE the decision (§10.S3 / D9). This campaign's placed-but-
    #    unstarted items are committed pool slots that no automatic path would
    #    ever re-offer (queue-advance reads only ``queued``), and they occupy
    #    until they run. That ordering is not a preference: the stranded items
    #    ARE the occupancy, so asking advance for pool room first would return
    #    ``wait_in_flight`` and the residue could never be cleared — a campaign
    #    with K stranded items would sit forever over an empty cluster. Re-using
    #    a stranded item is also strictly cheaper than replacing it: its optuna
    #    proposal index is already spent, and dispatching it is the only way that
    #    index ever becomes a run.
    submitted: list[SubmittedIteration] = []
    blocked: list[BlockedSlot] = []
    replayed_slots = 0
    stage: Literal["refilled", "no_refill_needed", "refill_blocked"] = "refilled"
    recovered, recovery_blocked = _recover_stranded(experiment_dir, cid)
    submitted.extend(recovered)
    blocked.extend(recovery_blocked)

    # 3. Authoritative advance — the SAME call load_context._async_should_refill
    #    routes on (advance.py:110), so the routing target == the refill target.
    #    Asked AFTER the recovery so its occupancy reflects it.
    adv = campaign_advance(experiment_dir=experiment_dir, campaign_id=cid)
    decision = str(adv.get("decision", ""))
    if decision != "refill":
        recovery_stage, recovery_decision = _recovery_terminal(recovery_blocked)
        early: Literal["refilled", "no_refill_needed", "refill_blocked"] = (
            "refill_blocked"
            if recovery_stage
            else ("refilled" if submitted else "no_refill_needed")
        )
        return CampaignRefillResult(
            stage_reached=early,
            needs_decision=recovery_decision,
            reason=(
                f"campaign {cid!r} advance decided {decision!r} (not refill): "
                f"{adv.get('reason')}. Nothing new to submit this tick."
                + _recovery_note(recovered, recovery_blocked)
            ),
            campaign_id=cid,
            decision=decision,
            refill_count=0,
            submitted=submitted,
            blocked=blocked,
            next_block=None,
            active_env_overrides=active_env_overrides(),
        )

    n = int(adv.get("refill_count") or 0)

    # 4. Reconstruct the iteration context ONCE (disk-only), then run each slot
    #    SEQUENTIALLY: resolve (writes the sidecar, advancing the async scaffold's
    #    _submitted_count) → enqueue → dispatch, all inside ONE per-campaign lock
    #    so the window is durable across processes and not merely a loop
    #    convention (RFC E4 / §10.S2.4), and with no gap between the sidecar and
    #    the ledger item that a crash could hide the slot in (RFC E5 / §10.S3).

    # Export HPC_CAMPAIGN_ID around BOTH the disk reconstruction (its internal
    # compute-run-id) and every slot's resolve, so the async scaffold's
    # sidecar-indexed _submitted_count sees the right campaign and each slot asks a
    # DISTINCT trial — the per-slot distinctness invariant, made self-contained
    # rather than dependent on the caller's ambient shell (_exported_campaign_id).
    with _exported_campaign_id(cid):
        resolve_spec = _build_iteration_resolve_spec(experiment_dir, cid)
        # The cluster comes off the prior iteration's own sidecar, never from
        # splitting the campaign id (compose_campaign_id: composed, never
        # parsed). It is the item's PIN — R5 makes a pin supreme, which is
        # exactly right here: a campaign id is per-cluster by construction, so
        # placement has nothing left to choose.
        cluster = resolve_spec.submit.cluster
        campaign_base = _campaign_base_for(cid, cluster)
        for _ in range(n):
            slot = _refill_slot(
                experiment_dir,
                cid=cid,
                cluster=cluster,
                campaign_base=campaign_base,
                resolve_spec=resolve_spec,
            )
            if slot.replayed:
                replayed_slots += 1
            if slot.submitted is not None:
                submitted.append(slot.submitted)
                continue
            # Every non-dispatch outcome is DISCLOSED and stops the tick: whether
            # the resolver escalated, a peer holds the claim, or the gated
            # lifecycle refused, asking for another trial against an unfinished
            # slot would spend a proposal index on a pool nobody is draining (R4).
            assert slot.blocked is not None  # a slot yields exactly one of the two
            blocked.append(slot.blocked)
            stage = "refill_blocked"
            break

    if stage == "refill_blocked":
        last = blocked[-1]
        needs_decision = last.stage not in _NO_DECISION_STAGES
        tail = (
            "a human must resolve it (resume-vs-fresh, run the scaffold "
            "interview, or clear whatever the gate named)"
            if needs_decision
            else "another process is doing this work (a held claim, or a pool a peer pass filled)"
        )
        reason = (
            f"campaign {cid!r} refill stopped mid-tick after {len(submitted)} "
            f"dispatch(es): a slot stopped at {last.stage!r} — {tail}."
        )
    else:
        reason = (
            f"campaign {cid!r} refilled {len(submitted)} slot(s) this tick "
            f"(refill_count={n}); each slot was enqueued on the intake ledger and "
            "started through queue-dispatch. Next tick re-enters via campaign-watch."
        )
        needs_decision = False
    # The recovery leg's own blockers set the terminal independently of the slot
    # loop: a stranded placement that still cannot start is exactly the fact the
    # silent wedge used to hide, and reporting ``refilled`` over it would hide it
    # again one layer up.
    recovery_stage, recovery_decision = _recovery_terminal(recovery_blocked)
    if recovery_stage is not None:
        stage = "refill_blocked"
        needs_decision = needs_decision or recovery_decision
    reason += _recovery_note(recovered, recovery_blocked)
    if replayed_slots:
        reason += (
            f" {replayed_slots} slot(s) replayed an existing ledger item (the "
            "resolved trial was already enqueued); the dispatcher adopted rather "
            "than double-enqueued."
        )
    if campaign_base is None:
        reason += (
            f" NOTE: campaign id {cid!r} is not composed as '<base>_{cluster}', so "
            "its ledger items carry no campaign base and the shared occupancy "
            "predicate cannot count them against this campaign's pool — the "
            "enqueue->dispatch window stays open for this campaign (§10.S3)."
        )

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
