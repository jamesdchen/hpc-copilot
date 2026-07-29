"""``queue-dispatch`` — the run queue's ACTOR. It composes; it never submits.

Phase 2 of ``docs/plans/run-queue-placement-2026-07-28.md`` (§6 Phase 2,
resolved by §10). ``queue-advance`` is the pure placement AUTHORITY — it reads,
decides, and writes nothing (R3). This verb is the other half of that split,
copied from the proven ``campaign-advance`` / ``campaign-refill`` pair: it
consumes the decision, records the placement on the intake ledger, and starts
each item's NORMAL run lifecycle.

Four rulings shape the whole module, and every one of them is a decision NOT to
build something:

**D1 — it composes the shipped lifecycle.** There is no second submit path
here, no gate of its own and no consent machinery of its own. A dispatched item
is started exactly the way ``campaign-refill`` starts a refill slot today —
``campaign_run(spec, detach=True)`` over a resolved submit-flow spec — so the
greenlight / standing-consent gates bind at the cluster boundary where they
already bind, inside the machinery this verb kicks off. A dispatch-side gate
would be a SECOND place the same question is answered, and the two would drift.
Submit TIMING is likewise untouched: gates first, then submit, exactly as
today. Phase 2 changes WHO triggers dispatch, not WHEN a job enters the
scheduler relative to its gates.

**D1a — eager submit (§7 R3).** "Gates first, then submit" has no third
clause: once the gates clear there is NO capacity wait between placement and
start. A placed item is started in the same tick even when its cluster is
busy — the job enters the scheduler's own queue and sits ``pending`` there,
because that queue is the authoritative one (fairshare, backfill, state we
cannot predict from outside). Neither this verb nor ``queue-advance`` holds
anything on inferred headroom; a held item is held for a gate, an etiquette
cap, or a hard-constraint mismatch, and its closed ``reason_code`` says which.
A long ``pending`` is safe on two §10.S4 facts this seat RELIES on: the submit
deploys a content-addressed code tree
(``ops/submit_flow._deploy_code_tree`` → ``infra/code_tree.py``) that a later
push cannot mutate — the #F20 ``--inplace`` ban generalized from running to
pending jobs — and the tree GC never reaps a tree a non-terminal run
references (``code_tree.plan_tree_gc``: a ``submitting``/``in_flight`` record
pins its tree for exactly as long as its job can still be queued). The
pending→running→terminal lifecycle is covered by the detached child this verb
starts (``campaign_run(detach=True)`` re-enters the synchronous
submit→monitor→aggregate spine, ``ops/campaign_run.py``), and that child's
retirement wakes the next dispatch (``ops/queue/chain.py``).

**D2 — the claim is the shipped lease, not a new one** (§10.S2). Run ids are
COMPUTED (``incorporation/build/compute_run_id`` — ``"<run_name>-<cmd_sha[:8]>"``,
pure and deterministic), so two dispatchers racing one item derive the SAME id
and the detached single-lease guard
(``_kernel/lifecycle/detached.py::_guard_single_lease`` — flock, F43 host +
create_time) arbitrates between them. The loser gets ``DetachedLeaseHeld``,
which this verb catches and reports as ``claim_held``: a healthy peer doing the
work, not an error. Writing a second lease system would have given two claims
that disagree.

**D3 — the E4 sidecar-between-slots rule is a DURABLE per-cid lock.** The whole
per-item window runs inside
:func:`hpc_agent.state.queue_locks.campaign_dispatch_lock`, whose timeout is
caught and reported as ``claim_held`` too. It is re-entrant within a process on
purpose: §10.S3's refill slot holds it across ``resolve → enqueue → dispatch``
and the ``queue-dispatch`` call nested inside must hold it as well.

**D4 (amended) — the adopt key is the local runs store, not a job name.** The
plan's §10.S2.5 sketch has the scheduler job NAME carry the intake item id;
the shipped backend contract refuses that mechanism outright
(``infra/backends/_engine.py::build_correlation_flags`` — SGE caps names at 15
characters and ``job_name`` is consumed byte-for-byte by log-path derivation and
canary naming, "the whole reason OPEN-1(iii) was rejected"), and the repo's own
run ids already exceed that cap. No item id is needed on the scheduler at all:
because the run id is COMPUTED, the submit-once ``submitting`` RunRecord
(``ops/submit/runner.py::mint_submitting_record``) already IS the durable "this
item is inside its dispatch→id window" fact, and cluster-side identity rides the
correlation token ``"<run_id>#<attempt>"``. So the pre-submit check here is one
local ``load_run`` — the SAME predicate ``queue-status`` projects ``dispatched``
from (R7: two surfaces must not answer one question differently).

**D8 — no new intake state.** Intake stores ``{queued, placed}`` and nothing
wider; "dispatched" stays a PROJECTION over the run stores. The only ledger
write on this path is the placement transition, through the shipped
``append_intake_placement`` with a DERIVED append token, so a retried or raced
dispatch leaves ONE placement record rather than one per attempt.

**D9 — nothing is dropped.** Every item this call touched comes back in exactly
one of ``dispatched`` / ``refused`` / ``held``, each with a reason a human can
act on — including the successes, because an adopt that did not say why it
adopted is indistinguishable from a dispatcher that silently did nothing.

**D10 — the WRITE authority grooms; the read paths never do** (§7's
relaunch-cheapness invariant, §8 S12). This verb is the queue's only actor, so
it is where the intake ledger is compacted and unreferenced terminal RunRecords
are pruned (``ops/queue/maintenance.groom_queue_stores``), on the ticks that
actually wrote a placement. ``queue-status`` / ``queue-advance`` declare
``side_effects=[]`` and must stay that way: a query that groomed the store it
reports on is the F46 bug one layer up. The full argument for this seat — and
for why compaction must precede the prune — lives in that module's docstring.

What this verb deliberately does NOT do
---------------------------------------
It does not RESOLVE. ``resolve-submit-inputs`` consumes the optuna scaffold's
sidecar-indexed proposal exactly once (``ops/campaign_refill.py`` E4), so a
dispatcher that re-resolved an item its enqueuer had already resolved would
propose the NEXT trial and start a run whose id disagrees with the ledger's.
§10.S3 puts the resolve in the refill slot, before the enqueue, which is why
``QueueRunSpec`` carries ``run_id``/``cmd_sha`` at all. An item that reaches
this verb without a startable, already-resolved spec is REFUSED with a stated
reason — never resolved here, never guessed. That is also why
``resolve_blocked`` has no producer in this module (the ``courtesy_cap_reached``
precedent in ``queue-advance``): it is reserved for the seat that owns the
resolve.

I/O contracts:

* Input: ``schemas/queue_dispatch.input.json`` (from ``QueueDispatchSpec``).
* Output: ``schemas/queue_dispatch.output.json`` (from ``QueueDispatchResult``).
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.queries.queue_advance import QueueAdvanceSpec, QueueHoldback
from hpc_agent._wire.workflows.queue_dispatch import (
    DispatchedItem,
    DispatchRefusal,
    QueueDispatchRefusal,
    QueueDispatchResult,
    QueueDispatchSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.env_flags import active_env_overrides
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso
from hpc_agent.state.queue_intake import (
    STATE_PLACED,
    append_intake_placement,
    find_intake_item,
    item_cmd_sha,
    item_run_id,
    placement_request_id,
    read_intake_items,
)
from hpc_agent.state.queue_locks import campaign_dispatch_lock

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

    from hpc_agent._wire.workflows.campaign_run import CampaignRunResult

__all__ = ["queue_dispatch"]

_PRIMITIVE = "queue-dispatch"

_CAMPAIGN_ID_ENV = "HPC_CAMPAIGN_ID"

#: ``QueueAdvanceSpec.max_placements``' declared ceiling. When the caller NAMES
#: items (the §10.S3 refill path) the advance call must be asked for the whole
#: batch rather than for ``max_dispatches``: a named item is not necessarily the
#: first in arrival order, and bounding advance to one placement would report
#: the caller's own freshly-enqueued item as "batch limit reached". The narrowing
#: to ``item_ids`` and to ``max_dispatches`` happens here, after the decision.
_ADVANCE_MAX_PLACEMENTS = 50

#: Statuses whose existing RunRecord means the item is ALREADY dispatched, so
#: this call adopts instead of starting a second lifecycle (§10.S2.5).
#: ``submitting`` is in by construction and must be — it is the submit-once
#: pre-dispatch record, and reconcile-recovery (not a second dispatcher) owns
#: its transition out. The resubmittable terminals (``failed`` / ``abandoned``)
#: are deliberately ABSENT: a corpse is not a dispatch, and the submit-once
#: minter's own decision table mints a fresh attempt over one.
_ADOPTABLE_STATUSES: frozenset[str] = frozenset({"submitting", "in_flight", "complete"})

#: The lifecycle stage that means "the gated submit spine refused" — canary
#: gate, post-qsub verification, or DAG readiness (``ops/campaign_run.py``
#: ``_campaign_run_impl`` step 1). Mapped to ``gate_refused`` because the answer
#: came from the gate, and D1 forbids this verb from re-asking or overriding it.
_GATE_REFUSED_STAGE = "submit_failed"

#: The refusal causes a HUMAN must resolve, and the whole of that set. A
#: ``claim_held`` is deliberately absent: nobody has to decide anything, a peer
#: is already doing the work, and flagging it would train readers to ignore the
#: flag. ``resolve_blocked`` is listed for the seat that owns the resolve —
#: this verb never produces it (see the module docstring).
_NEEDS_DECISION_REFUSALS: frozenset[str] = frozenset({"resolve_blocked", "gate_refused"})

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+$")
_CMD_SHA_RE = re.compile(r"^[0-9a-f]{8,64}$")


def _text(value: Any) -> str | None:
    """A non-blank string, or ``None`` — the ledger is untyped by design."""
    return value.strip() or None if isinstance(value, str) else None


def _wire_run_id(value: Any) -> str | None:
    """*value* if it satisfies ``RunIdStrict``, else ``None``.

    An id the wire model would reject is reported ABSENT rather than coerced,
    the same rule ``queue-advance`` applies to the same field: a shape the
    schema refuses is not a weaker id, it is not an id.
    """
    text = _text(value)
    return text if text is not None and _RUN_ID_RE.match(text) else None


def _item_campaign_id(item: dict[str, Any] | None) -> str | None:
    """The campaign id an item's OWN resolved submit spec names, or ``None``.

    The fallback when the placement's ``campaign_id`` is absent, and it closes a
    real hole rather than tidying one. The placement's value is COMPOSED from the
    item's ``campaign_base`` + cluster (``compose_campaign_id``), so it is
    ``None`` for every campaign whose id is not literally ``<base>_<cluster>`` —
    a single-cluster campaign named before the multi-cluster convention. The
    resolved submit spec carries the concrete id verbatim
    (``incorporation/build/submit_spec.py`` emits ``campaign_id``), so the
    campaign is not actually unknown, it was only unrecoverable by composition.

    Three things ride on getting it right, and all three silently degraded
    without this: the per-cid E4 dispatch lock (a ``None`` id took
    ``nullcontext`` — no lock at all), the ledger's placement record (whose
    stored id is what the occupancy predicate keys the item's cid on), and
    ``CampaignRunSpec.campaign_id``, which the lifecycle turns into the
    relay-due scope — a blank scope arms the run-#10 omission gate on nothing.

    Charset-checked through ``CampaignId``'s pattern (identical to
    ``RunIdStrict``'s) rather than trusted: a hand-enqueued item's spec is
    opaque to ``queue-run`` by design, and this value reaches a lock FILENAME.
    """
    if not isinstance(item, dict):
        return None
    spec = item.get("spec")
    if not isinstance(spec, dict):
        return None
    text = _text(spec.get("campaign_id"))
    return text if text is not None and _RUN_ID_RE.match(text) else None


def _wire_cmd_sha(value: Any) -> str | None:
    """*value* if it satisfies ``CmdSha`` (lowercase hex, 8..64), else ``None``."""
    text = _text(value)
    return text if text is not None and _CMD_SHA_RE.match(text) else None


@contextlib.contextmanager
def _exported_campaign_id(campaign_id: str | None) -> Iterator[None]:
    """Export ``HPC_CAMPAIGN_ID`` around a ``compute-run-id`` recomputation.

    Duplicated from ``ops/campaign_refill.py::_exported_campaign_id`` rather
    than imported: that symbol is package-private to another subject
    (``scripts/lint_private_cross_package_imports.py``), and the fact it
    protects is the same one here. ``compute-run-id`` re-execs ``tasks.py``
    FRESH on every call, so the async scaffold's module-top
    ``_CID = os.environ.get(...)`` is re-read live; with the variable unset a
    campaign trial's proposal index reads 0 and the recomputed id would name a
    DIFFERENT trial than the item's. A blank campaign id is a no-op — an
    open-loop item has no campaign to scope the recomputation to.
    """
    if not campaign_id:
        yield
        return
    prior = os.environ.get(_CAMPAIGN_ID_ENV)
    os.environ[_CAMPAIGN_ID_ENV] = campaign_id
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(_CAMPAIGN_ID_ENV, None)
        else:
            os.environ[_CAMPAIGN_ID_ENV] = prior


@dataclass(frozen=True)
class _Target:
    """One item this call will try to actuate, with the placement behind it.

    Sourced either from ``queue-advance``'s fresh decision or — for an item the
    caller NAMED that has already left advance's scope — from the placement
    record the ledger already carries. Both are placements; only the authorship
    differs, and ``source`` keeps that visible in the disclosed reason.
    """

    item_id: str
    cluster: str
    campaign_id: str | None
    placement_reason: str
    source: Literal["advance", "ledger"]


def _refusal(
    target: _Target | str,
    code: QueueDispatchRefusal,
    reason: str,
    *,
    run_id: str | None = None,
    placed: bool = False,
    detail: dict[str, Any] | None = None,
) -> DispatchRefusal:
    """One refusal row. Every field the operator needs to act, none inferred."""
    if isinstance(target, str):
        return DispatchRefusal(
            item_id=target, reason_code=code, reason=reason, placed=placed, detail=detail or {}
        )
    return DispatchRefusal(
        item_id=target.item_id,
        run_id=_wire_run_id(run_id),
        cluster=target.cluster or None,
        campaign_id=target.campaign_id,
        reason_code=code,
        reason=reason,
        placed=placed,
        detail=detail or {},
    )


def _derive_run_id(
    experiment_dir: Path, item: dict[str, Any], target: _Target
) -> tuple[str | None, str]:
    """The item's COMPUTED run id and how it was derived — never a guess.

    Three sources, in falling order of authority:

    1. the ledger's own resolved identity (``item_run_id``), written by the
       caller that resolved before enqueueing (§10.S3). This is authoritative:
       it is what the occupancy predicate already counts the item's slot under,
       so dispatching a different id would double-count the slot;
    2. the ``run_id`` carried inside the item's own submit-flow spec, for a hand
       enqueue that arrived with a resolved spec but no identity fields;
    3. a RECOMPUTATION through ``compute-run-id`` from the item's ``run_name``
       — pure and deterministic (``"<run_name>-<cmd_sha[:8]>"``, no sidecar
       written), scoped by the campaign export above. Best-effort: a repo whose
       ``tasks.py`` cannot be materialized simply has no derivable id, which is
       a refusal, not a crash.

    Returns ``(None, why)`` when none of the three answers, so the caller can
    disclose WHICH way of asking failed rather than reporting a bare "no id".
    """
    ledger_id = _wire_run_id(item_run_id(item))
    if ledger_id is not None:
        return ledger_id, "the ledger's resolved identity"

    spec = item.get("spec")
    if isinstance(spec, dict):
        spec_id = _wire_run_id(spec.get("run_id"))
        if spec_id is not None:
            return spec_id, "the run_id carried on the item's own submit spec"

    run_name = _text(item.get("run_name"))
    if run_name is None:
        return None, (
            "the item carries neither a resolved run_id nor a run_name, and a run "
            "id is DERIVED from the run name — there is nothing to compute from"
        )
    from hpc_agent.incorporation.build.compute_run_id import compute_run_id

    try:
        with _exported_campaign_id(target.campaign_id):
            computed = compute_run_id(experiment_dir, run_name=run_name)
    except Exception as exc:  # noqa: BLE001 — an unmaterializable repo is a refusal, not a crash
        return None, (
            f"recomputing the run id from run_name {run_name!r} failed "
            f"({type(exc).__name__}: {exc})"
        )
    recomputed = _wire_run_id(computed.get("run_id"))
    if recomputed is None:
        return None, f"compute-run-id returned no usable run_id for run_name {run_name!r}"
    return recomputed, f"recomputed by compute-run-id from run_name {run_name!r}"


def _adopted_status(experiment_dir: Path, run_id: str) -> str | None:
    """The existing run's status when it means "already dispatched", else ``None``.

    ONE local read of the runs store — the same join ``queue-status`` projects
    ``dispatched`` from (``ops/queue/status.py``), so the two surfaces cannot
    answer "has this item been dispatched?" differently (R7). Never a scheduler
    probe: the submit-once organ already stamps the durable fact locally, and
    the cluster-side recovery for an ambiguous ``submitting`` belongs to
    reconcile, not to a second dispatcher.
    """
    from hpc_agent.state.journal import load_run

    record = load_run(experiment_dir, run_id)
    if record is None:
        return None
    status = _text(getattr(record, "status", None))
    return status if status in _ADOPTABLE_STATUSES else None


def _cluster_transport_error(cluster: str) -> str | None:
    """Why *cluster* cannot be actuated right now, or ``None`` when it can.

    Placement discloses an ``ssh_target`` for the human and marks it
    stale-by-design; the actor always re-resolves at USE time (run12 finding
    23). So this re-asks ``clusters.yaml`` — the live config, through the real
    loader — whether the placed key still exists and still yields a derivable
    ``user@host``. It does NOT rewrite the item's submit spec: that spec's
    target was itself produced by the pool-aware
    :func:`hpc_agent.infra.clusters.resolve_ssh_target` at resolve time, and
    overwriting it here with the config default would silently undo a login-pool
    failover. The check is a REFUSAL gate, not a patch.
    """
    from hpc_agent.infra.clusters import ClusterConfig, load_clusters_config

    try:
        clusters = load_clusters_config()
    except Exception as exc:  # noqa: BLE001 — an unreadable config is a disclosed refusal
        return f"clusters.yaml failed to load ({type(exc).__name__}: {exc})"
    if not isinstance(clusters, dict) or cluster not in clusters:
        return (
            f"cluster {cluster!r} is absent from the active clusters.yaml — it was "
            "a live key when placement chose it and is not one now"
        )
    try:
        target = ClusterConfig.model_validate(clusters[cluster]).ssh_target
    except Exception as exc:  # noqa: BLE001 — an invalid entry is a disclosed refusal
        return f"clusters.yaml#{cluster} does not validate ({type(exc).__name__}: {exc})"
    if not _text(target):
        return f"clusters.yaml#{cluster} yields no derivable ssh target (user/host)"
    return None


def _campaign_run_spec(*, campaign_id: str | None, run_id: str, submit_spec: dict[str, Any]) -> Any:
    """Nest a resolved submit-flow spec into a DETACHED ``CampaignRunSpec``.

    Byte-for-byte the shape ``ops/campaign_refill.py::_wrap_campaign_run_spec``
    builds, and for the same reason: ``rr.submit_spec`` is a submit-FLOW dict
    while ``CampaignRunSpec.submit`` is a ``SubmitPipelineSpec`` →
    ``SubmitAndVerifySpec`` → submit-flow, so it nests three deep.
    ``aggregate.run_id`` is LOAD-BEARING — it is the key
    ``_kernel/lifecycle/detached.py::_block_spec_run_id`` digs out, hence the
    key of the lease this dispatch claims (D2), of the journal poll, and of the
    recorded terminal. It MUST equal the run id derived above or the claim would
    guard a different run than the one that starts.
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


def _started_row(
    target: _Target,
    *,
    run_id: str,
    cmd_sha: str | None,
    placed: bool,
    result: CampaignRunResult,
) -> DispatchedItem:
    """The dispatched row for a lifecycle this call actually started."""
    return DispatchedItem(
        item_id=target.item_id,
        run_id=run_id,
        cmd_sha=_wire_cmd_sha(cmd_sha),
        cluster=target.cluster,
        campaign_id=target.campaign_id,
        outcome="started",
        placed=placed,
        detached_pid=result.detached_pid,
        stage_reached=result.stage_reached,
        reason=(
            f"started run {run_id} on {target.cluster} — the item's normal gated "
            f"lifecycle (campaign-run, detached; stage {result.stage_reached!r}). "
            "The greenlight / standing-consent gates bind inside that machinery, "
            "at the cluster boundary, exactly as they do for every other submit."
        ),
    )


def _dispatch_one(
    experiment_dir: Path,
    target: _Target,
    items: list[dict[str, Any]],
) -> DispatchedItem | DispatchRefusal:
    """Actuate ONE placed item, or refuse it with a stated cause (R4/D9).

    The order is the whole safety argument, so it is fixed:

    1. derive the run id — an item with no derivable id can be neither claimed
       nor adopted, so it is refused before anything is written;
    2. ADOPT check against the local runs store — an item whose run already
       exists is a SUCCESS reported as such, and touching it again would be the
       double-submit the submit-once organ exists to prevent;
    3. re-resolve the placed cluster and check the item's own spec agrees with
       it — a spec pointing somewhere else would submit to a cluster the
       occupancy arithmetic is not counting;
    4. take the per-cid dispatch lock (D3) — the durable form of E4;
    5. append the placement transition BEFORE starting (durable-first, the same
       ordering §10.S3 uses for enqueue-before-submit): a crash after the start
       and before the append would leave a live run whose item still reads
       ``queued``, and the next advance would place it a second time;
    6. start the lifecycle under the shipped detached lease, which IS the claim.
    """
    item = find_intake_item(items, target.item_id)
    if item is None:
        return _refusal(
            target,
            "item_unresolved",
            f"item {target.item_id!r} is not on the intake ledger; there is nothing to start.",
        )

    # 1. Identity.
    run_id, how = _derive_run_id(experiment_dir, item, target)
    if run_id is None:
        return _refusal(
            target,
            "item_unresolved",
            (
                f"item {target.item_id!r} has no derivable run id: {how}. A run id is "
                "COMPUTED, never minted (§10.S2), so it cannot be invented here — "
                "resolve the item (campaign-refill resolves before enqueueing) and "
                "dispatch again."
            ),
            detail={"derivation": how},
        )
    cmd_sha = item_cmd_sha(item)

    # 2. Adopt. Never a resubmit: the submit-once ``submitting`` record IS the
    #    in-flight dispatch, and reconcile-recovery owns its transition out.
    adopted = _adopted_status(experiment_dir, run_id)
    if adopted is not None:
        placed_record = append_intake_placement(
            experiment_dir,
            item_id=target.item_id,
            request_id=placement_request_id(target.item_id),
            cluster=target.cluster,
            campaign_id=target.campaign_id or "",
            # NAMES the run, never its status. The ledger record is DURABLE and
            # nothing revises it, so a status word written here ("adopted an
            # existing submitting run") keeps asserting a point-in-time fact
            # long after the run failed, was resubmitted, or completed —
            # queue-status projects this reason verbatim. The run_id is the
            # stable half; whoever wants the status joins the run stores, which
            # is where D8 says lifecycle lives.
            reason=f"adopted run {run_id}, already dispatched: {target.placement_reason}",
            run_id=run_id,
        )
        return DispatchedItem(
            item_id=target.item_id,
            run_id=run_id,
            cmd_sha=_wire_cmd_sha(cmd_sha),
            cluster=target.cluster,
            campaign_id=target.campaign_id,
            outcome="adopted",
            adopted_status=adopted,
            placed=placed_record is None,
            reason=(
                f"adopted run {run_id} — a {adopted!r} RunRecord for this id already "
                f"exists ({how}), so this item is already dispatched and nothing was "
                "submitted. A second submit would duplicate the array the submit-once "
                "record is holding the window open for."
            ),
        )

    # 3. Transport, re-resolved at use time, and the spec's own agreement with it.
    transport_error = _cluster_transport_error(target.cluster)
    if transport_error is not None:
        return _refusal(
            target,
            "cluster_unresolvable",
            (
                f"item {target.item_id!r} is placed on {target.cluster!r} but that "
                f"placement cannot be actuated: {transport_error}. A placement's "
                "disclosed ssh target is stale by design and is re-resolved here, so "
                "this is the re-resolution failing, not a stale echo."
            ),
            run_id=run_id,
            detail={"cluster": target.cluster, "error": transport_error},
        )

    spec = item.get("spec")
    if not isinstance(spec, dict) or not spec:
        ref = _text(item.get("spec_ref"))
        by_ref = f" (it names {ref!r} by reference, which is not dereferenced here)" if ref else ""
        return _refusal(
            target,
            "item_unresolved",
            (
                f"item {target.item_id!r} carries no inline submit spec"
                + by_ref
                + ". queue-dispatch composes the shipped lifecycle and never resolves: "
                "resolving consumes the optuna sidecar index exactly once (E4), so the "
                "seat that resolves is the one that enqueues (§10.S3)."
            ),
            run_id=run_id,
            detail={"spec_ref": ref},
        )
    spec_run_id = _wire_run_id(spec.get("run_id"))
    if spec_run_id is not None and spec_run_id != run_id:
        return _refusal(
            target,
            "item_unresolved",
            (
                f"item {target.item_id!r} disagrees with itself: the ledger's identity is "
                f"{run_id!r} ({how}) and its submit spec names {spec_run_id!r}. Starting "
                "either would claim a lease the other half of the record does not point "
                "at — refusing instead of picking one."
            ),
            run_id=run_id,
            detail={"ledger_run_id": run_id, "spec_run_id": spec_run_id},
        )
    spec_cluster = _text(spec.get("cluster"))
    if spec_cluster is not None and spec_cluster != target.cluster:
        return _refusal(
            target,
            "cluster_unresolvable",
            (
                f"item {target.item_id!r} is placed on {target.cluster!r} but its submit "
                f"spec targets {spec_cluster!r}. Starting it would submit to a cluster the "
                "occupancy arithmetic is not counting this item against (R9), so the pool "
                "would over-fill silently."
            ),
            run_id=run_id,
            detail={"placed_cluster": target.cluster, "spec_cluster": spec_cluster},
        )

    try:
        crspec = _campaign_run_spec(campaign_id=target.campaign_id, run_id=run_id, submit_spec=spec)
    except ValidationError as exc:
        return _refusal(
            target,
            "item_unresolved",
            (
                f"item {target.item_id!r} carries a spec the run lifecycle refuses "
                f"({exc.error_count()} validation error(s)); it is not a resolved "
                "submit-flow spec. The enqueuing seat owns that invariant."
            ),
            run_id=run_id,
            detail={"errors": exc.errors(include_url=False)[:5]},
        )

    # 4-6. Lock, record, claim, start. Building the lock is INSIDE the try with
    # everything it guards: an open-coded acquire outside it would put the one
    # failure the lock exists to report (a peer inside the window) on the only
    # line that cannot be classified into a disclosed refusal.
    placed = False
    try:
        lock: AbstractContextManager[bool] = (
            campaign_dispatch_lock(experiment_dir, target.campaign_id)
            if target.campaign_id
            else contextlib.nullcontext(True)
        )
        with lock:
            placed = (
                append_intake_placement(
                    experiment_dir,
                    item_id=target.item_id,
                    request_id=placement_request_id(target.item_id),
                    cluster=target.cluster,
                    campaign_id=target.campaign_id or "",
                    reason=target.placement_reason,
                    run_id=run_id,
                )
                is None
            )
            result = _start_lifecycle(experiment_dir, crspec)
    except Exception as exc:
        refusal = _classify_start_failure(target, exc, run_id=run_id, placed=placed)
        if refusal is None:
            raise
        return refusal

    if result.stage_reached == _GATE_REFUSED_STAGE:
        return _refusal(
            target,
            "gate_refused",
            (
                f"the gated submit spine refused run {run_id}: {result.reason} "
                "queue-dispatch neither re-asks that question nor overrides the answer "
                "(D1) — the gate binds at the cluster boundary, where it always has."
            ),
            run_id=run_id,
            placed=placed,
            detail={"stage_reached": result.stage_reached},
        )
    if result.needs_decision:
        return _refusal(
            target,
            "lifecycle_failed",
            (
                f"the lifecycle for run {run_id} stopped at {result.stage_reached!r} "
                f"and needs a decision: {result.reason}"
            ),
            run_id=run_id,
            placed=placed,
            detail={"stage_reached": result.stage_reached},
        )
    if result.stage_reached != "detached":
        # A recorded terminal replayed (``campaign_run`` replays a finished
        # worker's outcome for the same cmd_sha). Nothing was submitted, so it
        # is an adopt — reported as one rather than as a start that never
        # happened.
        return DispatchedItem(
            item_id=target.item_id,
            run_id=run_id,
            cmd_sha=_wire_cmd_sha(cmd_sha),
            cluster=target.cluster,
            campaign_id=target.campaign_id,
            outcome="adopted",
            placed=placed,
            stage_reached=result.stage_reached,
            reason=(
                f"adopted run {run_id}: the lifecycle replayed a recorded "
                f"{result.stage_reached!r} terminal for this identity, so nothing was "
                f"submitted. {result.reason}"
            ),
        )
    return _started_row(target, run_id=run_id, cmd_sha=cmd_sha, placed=placed, result=result)


def _classify_start_failure(
    target: _Target, exc: BaseException, *, run_id: str, placed: bool
) -> DispatchRefusal | None:
    """Turn a start-time exception into a disclosed refusal, or ``None`` to re-raise.

    TWO distinct held-claim classes must both land on ``claim_held``, and only
    one of them is an :class:`errors.HpcError`:

    * :class:`errors.QueueDispatchLockHeld` — a peer is inside this campaign's
      resolve→start window (D3);
    * ``_kernel.lifecycle.detached.DetachedLeaseHeld`` — a live worker already
      owns this run id's lease (D2). It is ``DriveModeError(ValueError)``, so a
      bare ``except errors.HpcError`` would let it escape as an envelope error.
      ``campaign-refill`` does not catch it today; this verb must, because a
      claim held by a healthy peer is a normal fleet outcome, not a failure
      (D9).

    Everything else that is an ``HpcError`` is a genuine ``lifecycle_failed``.
    Anything that is neither is re-raised untouched: swallowing an unclassified
    exception here would convert a real bug into a per-item note nobody reads.
    """
    from hpc_agent._kernel.lifecycle.detached import DetachedLeaseHeld

    if isinstance(exc, errors.QueueDispatchLockHeld):
        return _refusal(
            target,
            "claim_held",
            (
                f"another process is inside campaign {target.campaign_id!r}'s "
                f"resolve→start window, so item {target.item_id!r} was not started: {exc}"
            ),
            run_id=run_id,
            placed=placed,
            detail={"campaign_id": target.campaign_id},
        )
    if isinstance(exc, DetachedLeaseHeld):
        return _refusal(
            target,
            "claim_held",
            (
                f"run {run_id} is already claimed by a live detached worker, so item "
                f"{target.item_id!r} was not started a second time: {exc} This is the "
                "computed-run-id claim doing its job (§10.S2) — poll the journal."
            ),
            run_id=run_id,
            placed=placed,
            detail={"claim": "detached-lease", "run_id": run_id},
        )
    if isinstance(exc, errors.HpcError):
        return _refusal(
            target,
            "lifecycle_failed",
            (
                f"starting item {target.item_id!r} as run {run_id} raised "
                f"{type(exc).__name__}: {exc}"
            ),
            run_id=run_id,
            placed=placed,
            detail={"error_code": getattr(exc, "error_code", "internal")},
        )
    return None


def _start_lifecycle(experiment_dir: Path, crspec: Any) -> CampaignRunResult:
    """Start the item's normal run lifecycle — the ONE actuation seat.

    Imported lazily and called exactly the way ``campaign-refill`` calls it
    today, so there is one submit path in the tree and this verb is a caller of
    it rather than a second implementation of it (D1).
    """
    from hpc_agent.ops.campaign_run import campaign_run

    return campaign_run(experiment_dir, spec=crspec)


def _ledger_target(item: dict[str, Any]) -> _Target | None:
    """A ``_Target`` for an item that is ALREADY placed on the ledger.

    ``queue-advance`` reads only ``queued`` items, so a dispatch that crashed
    between appending the placement and starting the lifecycle leaves an item no
    later advance call will ever hand back. Naming it in ``item_ids`` is the
    recovery: the placement it already carries IS the decision, so this call
    re-actuates it rather than asking for a second, contradictory one. Returns
    ``None`` when the record carries no cluster to act on.
    """
    cluster = _text(item.get("cluster"))
    if cluster is None:
        return None
    return _Target(
        item_id=str(item.get("item_id")),
        cluster=cluster,
        campaign_id=_text(item.get("campaign_id")) or _item_campaign_id(item),
        placement_reason=_text(item.get("placement_reason"))
        or _text(item.get("reason"))
        or "the placement already recorded on the intake ledger",
        source="ledger",
    )


def _brief(result: QueueDispatchResult) -> str:
    """The deterministic one-paragraph disclosure, computed and never authored."""
    if result.stage_reached == "nothing_to_dispatch":
        return (
            f"Run queue: nothing to dispatch (computed {result.computed_at}). "
            f"{result.placements_considered} placement(s) considered, "
            f"{sum(result.held_counts.values())} held."
        )
    lines = [
        f"Run queue: {len(result.dispatched)} dispatched, {len(result.refused)} refused, "
        f"{sum(result.held_counts.values())} held of {result.placements_considered} "
        f"placement(s) considered (computed {result.computed_at})."
    ]
    if result.batch_allowed_by:
        # §3: the one-per-y throttle was lifted, and the y (or its absence) has
        # to see on what basis — the tier/enumeration is disclosed in the same
        # breath as the counts, never inferred from a batch that just happened.
        lines.append(
            f"batch: max_dispatches > 1 allowed by {result.batch_allowed_by}; every "
            "dispatched item still meets its own cluster-boundary gates (D1)."
        )
    lines += [
        f"{row.outcome}: {row.item_id} -> {row.cluster} as {row.run_id} — {row.reason}"
        for row in result.dispatched
    ]
    lines += [
        f"refused: {row.item_id} ({row.reason_code}) — {row.reason}" for row in result.refused
    ]
    if result.held_counts:
        counts = ", ".join(f"{result.held_counts[c]} {c}" for c in sorted(result.held_counts))
        lines.append(f"held by queue-advance: {counts}.")
    groomed = result.maintenance
    if groomed.get("dropped_items") or groomed.get("pruned_runs"):
        lines.append(
            f"maintenance: compacted {groomed.get('dropped_items', 0)} settled item(s) "
            f"({groomed.get('dropped_records', 0)} ledger record(s)) and pruned "
            f"{groomed.get('pruned_runs', 0)} unreferenced terminal run record(s)."
        )
    if groomed.get("raced_items"):
        lines.append(
            f"maintenance declined {len(groomed['raced_items'])} item(s) a concurrent tick "
            f"touched between the census and the ledger lock: "
            f"{', '.join(groomed['raced_items'])} (they stay on the ledger; the next tick "
            "judges them afresh)."
        )
    if groomed.get("error"):
        lines.append(f"maintenance did not complete: {groomed['error']} (dispatch is unaffected).")
    return "\n".join(lines)


@primitive(
    name=_PRIMITIVE,
    verb="workflow",
    composes=["queue-advance", "campaign-run"],
    side_effects=[
        SideEffect(
            "file_write",
            "<experiment>/.hpc/queue/intake.jsonl (one placement record per item; "
            "compacted of settled items after a dispatching tick)",
        ),
        SideEffect("scheduler-submit", "<cluster> (per dispatched item, via campaign-run)"),
        SideEffect(
            "writes-journal",
            "prunes unreferenced terminal RunRecords after a dispatching tick (D10)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    # Two calls are the same call when they dispatch the same item, and what
    # makes the retry SAFE is that the run id is computed rather than minted:
    # the second derivation lands on the same id, so the shipped detached lease
    # refuses it and the submit-once RunRecord makes it an adopt (§10.S2).
    idempotency_key="item.run_id",
    cli=CliShape(
        help=(
            "The run queue's ACTOR: consume queue-advance's placement decisions "
            "and start each placed item's normal run lifecycle. It COMPOSES and "
            "never submits on its own - no second submit path, no gate of its "
            "own, no consent machinery of its own; the greenlight / standing "
            "consent bind at the cluster boundary inside the machinery this "
            "starts. Per item: derive the COMPUTED run id, adopt an existing "
            "submitting/in_flight/complete run instead of resubmitting, take "
            "the durable per-campaign dispatch lock, record the placement on "
            "the intake ledger, then start a detached campaign-run whose "
            "single-lease guard IS the claim - so two dispatchers racing one "
            "item derive the same id and the loser reports 'claim_held' rather "
            "than duplicating the array. Adds NO intake state: 'dispatched' "
            "stays a projection queue-status computes over the run stores. "
            "Nothing is dropped - every item comes back dispatched, refused "
            "with a closed reason_code, or held with queue-advance's own reason."
        ),
        spec_arg=True,
        # All-optional spec (the queue-advance precedent): a bare
        # `hpc-agent queue-dispatch` dispatches the next placed item.
        spec_required=False,
        experiment_dir_arg=True,
        spec_model=QueueDispatchSpec,
        requires_ssh=True,
        schema_ref=SchemaRef(input="queue_dispatch", output="queue_dispatch"),
    ),
    agent_facing=True,
)
def queue_dispatch(
    *, experiment_dir: Path, spec: QueueDispatchSpec | None = None
) -> QueueDispatchResult:
    """Start the placed items' lifecycles; report every item, started or not.

    Placement stays ``queue-advance``'s (R3): this call asks it for the
    decision and never overrides one. ``spec.item_ids`` NARROWS what is
    actuated — it cannot place anything, so an item named there that advance
    HELD comes back held, with advance's own reason. That narrowing is why the
    advance call is made for the whole batch rather than for
    ``max_dispatches``: a named item is not necessarily first in arrival order,
    and bounding the authority to one placement would report the caller's own
    freshly-enqueued item as "batch limit reached".

    ``needs_decision`` is set only for the refusals a HUMAN must resolve — a
    refused gate or a blocked resolve. A held claim is explicitly not one:
    nobody has to decide anything, a peer is already doing the work.

    Raises :class:`errors.SpecInvalid` only for a malformed ``now``; every
    per-item failure is DATA (R4), because a fleet in which one item's cluster
    left ``clusters.yaml`` must still dispatch the other four.
    """
    from hpc_agent.ops.queue.advance import queue_advance

    spec = spec or QueueDispatchSpec()
    now = (spec.now or "").strip() or utcnow_iso()
    if parse_iso_utc_or_none(now) is None:
        raise errors.SpecInvalid(f"queue-dispatch: now override {spec.now!r} is not ISO-8601 UTC")

    exp = Path(experiment_dir)
    narrowed = spec.item_ids is not None
    wanted = set(spec.item_ids or ())

    # The spec validator already refused a silently-raised batch; what remains
    # is to DISCLOSE which affordance carried it (R4 applies to successes too).
    # The declared tier outranks the enumeration when both are present — it is
    # the stronger claim, and the one the wake edge makes.
    batch_allowed_by: Literal["unattended_tier", "item_ids"] | None = None
    if spec.max_dispatches > 1:
        batch_allowed_by = "unattended_tier" if spec.tier == "unattended" else "item_ids"

    adv = queue_advance(
        experiment_dir=exp,
        spec=QueueAdvanceSpec(
            campaign_base=spec.campaign_base,
            # See the docstring: the authority decides over the whole batch and
            # the caller's bound is applied here, after the decision.
            max_placements=_ADVANCE_MAX_PLACEMENTS if narrowed else spec.max_dispatches,
            clusters=spec.clusters,
            now=now,
        ),
    )

    placements = list(adv.placements)
    held: list[QueueHoldback] = list(adv.held)
    if narrowed:
        placements = [p for p in placements if p.item_id in wanted]
        # Only the NAMED items' holdbacks are relayed: a caller dispatching one
        # item should not be handed the whole queue's holdbacks as if they were
        # this call's outcome.
        held = [h for h in held if h.item_id in wanted]

    items = read_intake_items(exp)
    targets: list[_Target] = [
        _Target(
            item_id=p.item_id,
            cluster=p.cluster,
            # The placement's id is COMPOSED and therefore absent for an
            # uncomposable cid; the item's own resolved spec still names it
            # (:func:`_item_campaign_id`), and the E4 lock / placement record /
            # relay-due scope all key off this one value.
            campaign_id=p.campaign_id or _item_campaign_id(find_intake_item(items, p.item_id)),
            placement_reason=p.reason,
            source="advance",
        )
        for p in placements
    ]
    refused: list[DispatchRefusal] = []

    if narrowed:
        # R4: an item the caller NAMED that advance returned neither a placement
        # nor a holdback for is not silently dropped. The common case is an item
        # already placed (advance reads only queued items) — a dispatch that
        # crashed after the placement append, which naming it here recovers.
        seen = {t.item_id for t in targets} | {h.item_id for h in held}
        for item_id in spec.item_ids or ():
            if item_id in seen:
                continue
            item = find_intake_item(items, item_id)
            if item is None:
                refused.append(
                    _refusal(
                        item_id,
                        "item_unresolved",
                        f"item {item_id!r} is not on the intake ledger at {exp}.",
                    )
                )
                continue
            if item.get("state") != STATE_PLACED:
                refused.append(
                    _refusal(
                        item_id,
                        "item_unresolved",
                        (
                            f"item {item_id!r} is state {item.get('state')!r} and "
                            "queue-advance returned neither a placement nor a holdback "
                            "for it this tick; there is no decision to act on."
                        ),
                    )
                )
                continue
            recovered = _ledger_target(item)
            if recovered is None:
                refused.append(
                    _refusal(
                        item_id,
                        "cluster_unresolvable",
                        (
                            f"item {item_id!r} reads 'placed' but its placement record "
                            "names no cluster, so there is nowhere to start it."
                        ),
                    )
                )
                continue
            targets.append(recovered)

    dispatched: list[DispatchedItem] = []
    for target in targets[: spec.max_dispatches]:
        outcome = _dispatch_one(exp, target, items)
        if isinstance(outcome, DispatchedItem):
            dispatched.append(outcome)
        else:
            refused.append(outcome)

    refused_counts: dict[str, int] = {}
    for row in refused:
        refused_counts[row.reason_code] = refused_counts.get(row.reason_code, 0) + 1
    held_counts: dict[str, int] = {}
    for hold in held:
        held_counts[hold.reason_code] = held_counts.get(hold.reason_code, 0) + 1

    if dispatched:
        stage: Literal["dispatched", "nothing_to_dispatch", "dispatch_refused"] = "dispatched"
        reason = (
            f"{len(dispatched)} item(s) entered their lifecycle "
            f"({sum(1 for d in dispatched if d.outcome == 'adopted')} adopted); "
            f"{len(refused)} refused, {len(held)} held by queue-advance."
        )
    elif refused:
        stage = "dispatch_refused"
        reason = (
            f"no item started: {len(refused)} placed item(s) were refused "
            f"({', '.join(f'{refused_counts[c]} {c}' for c in sorted(refused_counts))}). "
            "Each keeps its cause below; nothing was dropped."
        )
    else:
        stage = "nothing_to_dispatch"
        reason = (
            f"nothing to dispatch: queue-advance decided {adv.decision!r} with "
            f"{len(placements)} placement(s) in scope and {len(held)} holdback(s)."
        )

    # A held claim is NOT a decision anyone owes: the peer is doing the work.
    needs_decision = any(row.reason_code in _NEEDS_DECISION_REFUSALS for row in refused)

    # D10: groom ONLY on a tick that wrote a placement. A tick that dispatched
    # nothing is exactly the pass §7's relaunch-cheapness invariant is about, and
    # charging it an O(history) sweep would break the invariant this grooming
    # exists to hold.
    maintenance: dict[str, Any] = {}
    if dispatched:
        # Every item THIS call reports on is exempt: adopting onto an already-
        # ``complete`` run retires the item the instant its placement lands, and
        # compacting it here would erase — in the same tick — the ledger row this
        # result calls ``placed``. "Never destroy the thing you're operating on."
        # The import sits INSIDE the never-raises envelope: a broken maintenance
        # module must degrade to disclosed data like every other janitor failure,
        # not fail a dispatch that already submitted.
        try:
            from hpc_agent.ops.queue.maintenance import groom_queue_stores

            maintenance = groom_queue_stores(
                exp,
                exclude_item_ids=[row.item_id for row in dispatched]
                + [row.item_id for row in refused],
            )
        except Exception as exc:  # noqa: BLE001 — the items already started
            maintenance = {"error": f"{type(exc).__name__}: {exc}"}

    result = QueueDispatchResult(
        computed_at=now,
        stage_reached=stage,
        needs_decision=needs_decision,
        reason=reason,
        dispatched=dispatched,
        refused=refused,
        held=held,
        refused_counts=refused_counts,
        held_counts=held_counts,
        batch_allowed_by=batch_allowed_by,
        placements_considered=len(adv.placements),
        occupancy=dict(adv.occupancy),
        maintenance=maintenance,
        active_env_overrides=active_env_overrides(),
    )
    result.brief = _brief(result)
    return result
