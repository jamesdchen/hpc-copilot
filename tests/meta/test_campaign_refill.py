"""Tests for the ``campaign-refill`` actor (RFC #362 + run-queue plan §10.S3).

``campaign-refill`` consumes ``campaign-advance``'s per-tick refill decision and,
for each requested slot, resolves ONE iteration, ENQUEUES it on the intake
ledger, and hands that item to ``queue-dispatch``. Phase 2 retired the direct
``campaign_run(detach=True)`` call: after D5 the dispatcher is the only submitter
on this path, and the ledger is what makes an interrupted tick visible.

The composed seams are mocked at their SOURCE modules (the actor imports them
lazily inside the body) so every control-flow branch is exercised with NO
cluster and NO SSH — with ONE deliberate exception: ``queue-run`` is REAL
throughout. The enqueue is the whole point of the rewiring, so faking it would
test the fake; it is ungated, local, and writes exactly one jsonl line, so the
tests read the ledger the way ``campaign-advance`` and ``queue-status`` will.

Every branch runs with the async opt-in ON (default-off never reaches this
actor; see test_watch_refill_stage).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
from hpc_agent._wire.queries.queue_advance import QueueHoldback
from hpc_agent._wire.workflows.campaign_refill import CampaignRefillSpec
from hpc_agent._wire.workflows.queue_dispatch import (
    DispatchedItem,
    DispatchRefusal,
    QueueDispatchResult,
)
from hpc_agent._wire.workflows.resolve_submit_inputs import (
    ResolveSubmitInputsResult,
    ResolveSubmitInputsSpec,
)
from hpc_agent.meta.campaign.manifest import write_manifest
from hpc_agent.ops.campaign_refill import campaign_refill
from hpc_agent.state.queue_intake import read_intake_items

if TYPE_CHECKING:
    from pathlib import Path

_ADV = "hpc_agent.meta.campaign.atoms.advance.campaign_advance"
_RESOLVE = "hpc_agent.ops.resolve_submit_inputs.resolve_submit_inputs"
_DISPATCH = "hpc_agent.ops.queue.dispatch.queue_dispatch"
_RUN = "hpc_agent.ops.campaign_run.campaign_run"
_BUILD = "hpc_agent.ops.campaign_refill._build_iteration_resolve_spec"
_LOCK = "hpc_agent.state.queue_locks.campaign_dispatch_lock"

# A campaign id COMPOSED the multi-cluster way (docs/design/campaign-multi-cluster.md
# §2): ``<base>_<clusterkey>``. Refill recovers the base by removing the KNOWN
# cluster suffix and re-composing to check it — never by splitting on '_'.
_BASE = "tune"
_CLUSTER = "hoffman2"
_CID = f"{_BASE}_{_CLUSTER}"

_CLUSTERS_YAML = """\
hoffman2:
  host: hoffman2.example.edu
  user: someone
  scheduler: sge
  scratch: /u/scratch/s/someone
"""


# ── fixtures / helpers ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clusters_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """queue-run validates a cluster PIN against the live config (R5), so every
    test needs one — never the developer's own."""
    path = tmp_path / "clusters.yaml"
    path.write_text(_CLUSTERS_YAML, encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(path))
    return path


def _greenlit_async_manifest(experiment_dir: Path, *, campaign_id: str = _CID, k: int = 3) -> None:
    write_manifest(
        experiment_dir,
        campaign_id=campaign_id,
        goal="tune",
        async_refill=True,
        max_in_flight=k,
        greenlit=True,
        greenlit_at="2026-07-12T00:00:00Z",
    )


def _resolve_spec(run_name: str = "ml") -> ResolveSubmitInputsSpec:
    """A REAL resolve spec — refill reads ``submit.cluster`` and ``run_name`` off it."""
    return ResolveSubmitInputsSpec(
        run_name=run_name,
        submit=BuildSubmitSpecInput(
            profile=run_name,
            cluster=_CLUSTER,
            ssh_target="someone@hoffman2.example.edu",
            remote_path="/u/scratch/ml",
            run_id="PLACEHOLDER-run-id",
            cmd_sha="0" * 8,
            total_tasks=1,
            backend="sge",
            result_dir_template="results/{run_id}/{task_id}",
        ),
        sidecar=WriteRunSidecarInput(
            run_id="PLACEHOLDER-run-id",
            cmd_sha="0" * 8,
            executor="python train.py --seed $SEED",
            result_dir_template="results/{run_id}/{task_id}",
            task_count=1,
        ),
    )


def _submit_flow_dict(run_id: str) -> dict[str, Any]:
    """A minimal VALID submit-flow spec dict (what resolve's submit_spec carries)."""
    return {
        "profile": "ml",
        "cluster": _CLUSTER,
        "ssh_target": "u@h",
        "remote_path": "/scratch/ml",
        "job_name": "ml_array",
        "run_id": run_id,
        "total_tasks": 1,
        "backend": "sge",
        "script": ".hpc/templates/cpu_array.sh",
        "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
    }


def _resolved(run_id: str, cmd_sha: str = "a" * 8) -> ResolveSubmitInputsResult:
    return ResolveSubmitInputsResult(
        stage_reached="resolved",
        needs_decision=False,
        reason="inputs resolved",
        run_id=run_id,
        cmd_sha=cmd_sha,
        submit_spec=_submit_flow_dict(run_id),
    )


def _blocked(run_id: str, stage: str = "prior_run_found") -> ResolveSubmitInputsResult:
    return ResolveSubmitInputsResult(
        stage_reached=stage,
        needs_decision=True,
        reason=f"a live prior run ({run_id}) matches this cmd_sha",
        run_id=run_id,
        cmd_sha="0" * 8,
        prior_run_id=run_id,
        prior_status="in_flight",
    )


def _dispatched(run_id: str, pid: int = 1000, item_id: str = "") -> QueueDispatchResult:
    return QueueDispatchResult(
        computed_at="2026-07-29T00:00:00Z",
        stage_reached="dispatched",
        reason="started",
        dispatched=[
            DispatchedItem(
                item_id=item_id or f"{_CID}.{run_id}",
                run_id=run_id,
                cluster=_CLUSTER,
                campaign_id=_CID,
                outcome="started",
                placed=True,
                detached_pid=pid,
                stage_reached="detached",
                reason="started the gated lifecycle",
            )
        ],
    )


def _refused(run_id: str, code: str, *, item_id: str = "") -> QueueDispatchResult:
    return QueueDispatchResult(
        computed_at="2026-07-29T00:00:00Z",
        stage_reached="dispatch_refused",
        needs_decision=code != "claim_held",
        reason="refused",
        refused=[
            DispatchRefusal(
                item_id=item_id or f"{_CID}.{run_id}",
                run_id=run_id,
                cluster=_CLUSTER,
                campaign_id=_CID,
                reason_code=code,  # type: ignore[arg-type]
                reason=f"refused because {code}",
                placed=True,
            )
        ],
    )


def _dispatch_se(*run_ids: str) -> Any:
    """A queue-dispatch side effect that answers with each run_id in turn."""
    seen: list[str] = []

    def _se(*, experiment_dir: Path, spec: Any) -> QueueDispatchResult:
        run_id = run_ids[len(seen)]
        seen.append(run_id)
        return _dispatched(run_id, pid=1000 + len(seen) - 1, item_id=spec.item_ids[0])

    return _se


def _items(experiment_dir: Path) -> list[dict[str, Any]]:
    return read_intake_items(experiment_dir)


# ── guard: un-greenlit refusal ────────────────────────────────────────────────


def test_refuses_ungreenlit(tmp_path: Path) -> None:
    """An async campaign that is NOT greenlit refuses — the standing-consent guard
    FIRES (greenlight is the one boundary; refills carry none)."""
    write_manifest(tmp_path, campaign_id=_CID, async_refill=True, max_in_flight=3)

    with pytest.raises(errors.SpecInvalid, match="not greenlit"):
        campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))


def test_refuses_absent_manifest(tmp_path: Path) -> None:
    """No manifest at all is a loud SpecInvalid, never a silent no-op."""
    with pytest.raises(errors.SpecInvalid, match="no manifest"):
        campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id="ghost"))


# ── guard: prior sidecar missing a required reconstruction field ───────────────

_FIND_RUNS = "hpc_agent.state.index.find_runs_by_campaign"
_READ_SIDECAR = "hpc_agent.state.runs.read_run_sidecar"


@pytest.mark.parametrize(
    ("missing", "match"),
    [("executor", "no ``executor``"), ("result_dir_template", "no ``result_dir_template``")],
)
def test_refuses_prior_sidecar_missing_required_field(
    tmp_path: Path, missing: str, match: str
) -> None:
    """``_build_iteration_resolve_spec`` reconstructs the next iteration purely
    from the prior run's sidecar; a sidecar lacking a required field is a loud
    SpecInvalid, never a spec silently built from a placeholder/None."""
    from hpc_agent.ops.campaign_refill import _build_iteration_resolve_spec

    sidecar = {
        "profile": "ml",
        "cluster": _CLUSTER,
        "remote_path": "/scratch/ml",
        "executor": "python train.py --seed $SEED",
        "result_dir_template": "results/{run_id}/{task_id}",
    }
    del sidecar[missing]
    prior = mock.Mock(run_id="ml-aaaa1111", profile="ml", cluster=_CLUSTER)

    with (
        mock.patch(_FIND_RUNS, return_value=[prior]),
        mock.patch(_READ_SIDECAR, return_value=sidecar),
        pytest.raises(errors.SpecInvalid, match=match),
    ):
        _build_iteration_resolve_spec(tmp_path, _CID)


# ── no-op: advance did not decide refill ──────────────────────────────────────


@pytest.mark.parametrize("decision", ["wait_in_flight", "continue", "stop_converged"])
def test_no_refill_when_advance_not_refill(tmp_path: Path, decision: str) -> None:
    """When advance decides anything other than ``refill`` the actor is a typed
    no-op carrying the decision; it never resolves, enqueues or dispatches."""
    _greenlit_async_manifest(tmp_path)

    with (
        mock.patch(_ADV, return_value={"decision": decision, "reason": "r", "refill_count": None}),
        mock.patch(_RESOLVE) as m_resolve,
        mock.patch(_DISPATCH) as m_dispatch,
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert res.stage_reached == "no_refill_needed"
    assert res.needs_decision is False
    assert res.decision == decision
    assert res.refill_count == 0
    assert res.submitted == []
    m_resolve.assert_not_called()
    m_dispatch.assert_not_called()
    assert _items(tmp_path) == []  # nothing on the ledger either


# ── refill: N slots, sequential, enqueued then dispatched ─────────────────────


def test_refills_n_slots_through_the_ledger(tmp_path: Path) -> None:
    """advance decides refill_count=3 → 3 ledger items carrying their RESOLVED
    identity, each dispatched by item_id through the one dispatcher."""
    _greenlit_async_manifest(tmp_path)
    run_ids = ["ml-aaaa1111", "ml-bbbb2222", "ml-cccc3333"]

    with (
        mock.patch(
            _ADV,
            return_value={"decision": "refill", "reason": "free slots", "refill_count": 3},
        ),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(
            _RESOLVE, side_effect=[_resolved(r, cmd_sha=f"{i}" * 8) for i, r in enumerate(run_ids)]
        ) as m_resolve,
        mock.patch(_DISPATCH, side_effect=_dispatch_se(*run_ids)) as m_dispatch,
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert res.stage_reached == "refilled"
    assert res.needs_decision is False
    assert res.refill_count == 3
    assert [s.run_id for s in res.submitted] == run_ids
    assert [s.detached_pid for s in res.submitted] == [1000, 1001, 1002]
    assert all(s.stage_reached == "detached" for s in res.submitted)
    assert m_resolve.call_count == 3

    # The ledger holds one item per slot, each with the resolved identity, the
    # cid's cluster as its PIN and the campaign BASE (so the shared occupancy
    # predicate composes it back onto this campaign's pool).
    items = _items(tmp_path)
    assert [i["item_id"] for i in items] == [f"{_CID}.{r}" for r in run_ids]
    assert [i["run_id"] for i in items] == run_ids
    assert [i["cmd_sha"] for i in items] == ["0" * 8, "1" * 8, "2" * 8]
    assert {i["cluster_pin"] for i in items} == {_CLUSTER}
    assert {i["campaign_base"] for i in items} == {_BASE}
    assert {i["state"] for i in items} == {"queued"}
    # The item carries the RESOLVED submit spec — a dispatcher that had to
    # resolve again would consume the next optuna proposal index (E4).
    assert items[0]["spec"]["run_id"] == run_ids[0]

    # Each dispatch names exactly the item that slot enqueued.
    assert [c.kwargs["spec"].item_ids for c in m_dispatch.call_args_list] == [
        [f"{_CID}.{r}"] for r in run_ids
    ]
    assert {c.kwargs["spec"].campaign_base for c in m_dispatch.call_args_list} == {_BASE}


def test_the_direct_campaign_run_submit_is_retired(tmp_path: Path) -> None:
    """D5: refill no longer submits. The old ``campaign_run(detach=True)`` call —
    a submit with NO intake record — must not fire; the dispatcher is the only
    submitter on this path, which is what makes the ledger authoritative."""
    _greenlit_async_manifest(tmp_path)

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 1}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=[_resolved("ml-aaaa1111")]),
        mock.patch(_DISPATCH, side_effect=_dispatch_se("ml-aaaa1111")),
        mock.patch(_RUN) as m_run,
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert res.stage_reached == "refilled"
    m_run.assert_not_called()


def test_resolve_enqueue_dispatch_interleave_per_slot(tmp_path: Path) -> None:
    """RFC E4/E5 + D5: each slot completes resolve → enqueue → dispatch before the
    next slot resolves. Never batch-resolve-then-dispatch (two slots at the same
    sidecar index ask the SAME cached trial), and never dispatch before the
    enqueue (a crash between them would hide the slot from the pool)."""
    _greenlit_async_manifest(tmp_path)
    run_ids = ["ml-aaaa1111", "ml-bbbb2222"]
    order: list[str] = []

    def _resolve_se(*_a: Any, **_k: Any) -> ResolveSubmitInputsResult:
        r = run_ids[len([o for o in order if o == "resolve"])]
        order.append("resolve")
        return _resolved(r)

    from hpc_agent.ops.queue.run import queue_run as real_queue_run

    def _enqueue_se(**kwargs: Any) -> Any:
        order.append("enqueue")
        return real_queue_run(**kwargs)

    def _dispatch_only(*, experiment_dir: Path, spec: Any) -> QueueDispatchResult:
        order.append("dispatch")
        return _dispatched(run_ids[len([o for o in order if o == "dispatch"]) - 1])

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 2}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=_resolve_se),
        mock.patch("hpc_agent.ops.queue.run.queue_run", side_effect=_enqueue_se),
        mock.patch(_DISPATCH, side_effect=_dispatch_only),
    ):
        campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert order == ["resolve", "enqueue", "dispatch", "resolve", "enqueue", "dispatch"]


def test_slot_holds_the_durable_dispatch_lock_across_the_window(tmp_path: Path) -> None:
    """D3/E4: the per-campaign lock is taken ONCE per slot and spans the whole
    resolve → enqueue → dispatch window — not any one call inside it. The sidecar
    write lands near the END of resolve, so a lock narrowed to the dispatch would
    leave exactly the gap the rule exists to close."""
    _greenlit_async_manifest(tmp_path)
    events: list[str] = []
    import contextlib

    from hpc_agent.state.queue_locks import campaign_dispatch_lock as real_lock

    @contextlib.contextmanager
    def _traced(experiment_dir: Path, campaign_id: str, **kw: Any) -> Any:
        events.append(f"lock:{campaign_id}")
        with real_lock(experiment_dir, campaign_id, **kw) as held:
            yield held
        events.append("unlock")

    def _resolve_se(*_a: Any, **_k: Any) -> ResolveSubmitInputsResult:
        events.append("resolve")
        return _resolved("ml-aaaa1111")

    def _dispatch_only(*, experiment_dir: Path, spec: Any) -> QueueDispatchResult:
        events.append("dispatch")
        return _dispatched("ml-aaaa1111")

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 1}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=_resolve_se),
        mock.patch(_DISPATCH, side_effect=_dispatch_only),
        mock.patch(_LOCK, _traced),
    ):
        campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert events == [f"lock:{_CID}", "resolve", "dispatch", "unlock"]


def test_claim_held_is_a_disclosed_stop_not_a_crash(tmp_path: Path) -> None:
    """D9: a peer inside this campaign's window stops the tick with a stated
    reason and NO needs_decision — nobody has to decide anything, the other
    process is already doing the work. Nothing is resolved or enqueued."""
    _greenlit_async_manifest(tmp_path)

    import contextlib

    @contextlib.contextmanager
    def _held(*_a: Any, **_k: Any) -> Any:
        # Faithful to the real lock: the refusal lands on __enter__, at the
        # bounded acquire, not when the context manager is constructed.
        raise errors.QueueDispatchLockHeld("another process holds camp's window (pid 4242)")
        yield True  # pragma: no cover — unreachable, keeps this a generator CM

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 2}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE) as m_resolve,
        mock.patch(_DISPATCH) as m_dispatch,
        mock.patch(_LOCK, _held),
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert res.stage_reached == "refill_blocked"
    assert res.needs_decision is False  # a healthy peer, not an escalation
    assert [b.stage for b in res.blocked] == ["claim_held"]
    assert "pid 4242" in res.blocked[0].reason
    m_resolve.assert_not_called()
    m_dispatch.assert_not_called()
    assert _items(tmp_path) == []


# ── per-slot distinctness: HPC_CAMPAIGN_ID is exported around the resolve loop ─


def test_exports_campaign_id_env_during_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The async scaffold's per-slot distinctness reads the campaign id from
    ``HPC_CAMPAIGN_ID`` at materialization time. The actor must EXPORT it from
    ``spec.campaign_id`` (not trust the ambient shell), so ``_submitted_count``
    advances per slot and each asks a distinct trial. Capture it inside the
    resolve call; assert it is the campaign id and is RESTORED (here: popped) after."""
    monkeypatch.delenv("HPC_CAMPAIGN_ID", raising=False)
    _greenlit_async_manifest(tmp_path)
    seen: list[str | None] = []

    def _resolve_se(*_a: Any, **_k: Any) -> ResolveSubmitInputsResult:
        import os

        seen.append(os.environ.get("HPC_CAMPAIGN_ID"))
        return _resolved(f"ml-{len(seen):04d}0000")

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 2}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=_resolve_se),
        mock.patch(_DISPATCH, side_effect=_dispatch_se("ml-00010000", "ml-00020000")),
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert res.stage_reached == "refilled"
    assert seen == [_CID, _CID]  # exported for every slot's resolve
    import os

    assert "HPC_CAMPAIGN_ID" not in os.environ  # restored (was unset) after the tick


def test_restores_preexisting_campaign_id_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing ambient ``HPC_CAMPAIGN_ID`` is overridden to ``spec.campaign_id``
    for the resolve (correctness: refill this campaign, not whatever the shell drove)
    and RESTORED to its prior value afterward."""
    monkeypatch.setenv("HPC_CAMPAIGN_ID", "other-campaign")
    _greenlit_async_manifest(tmp_path)
    seen: list[str | None] = []

    def _resolve_se(*_a: Any, **_k: Any) -> ResolveSubmitInputsResult:
        import os

        seen.append(os.environ.get("HPC_CAMPAIGN_ID"))
        return _resolved("ml-aaaa1111")

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 1}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=_resolve_se),
        mock.patch(_DISPATCH, side_effect=_dispatch_se("ml-aaaa1111")),
    ):
        campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    import os

    assert seen == [_CID]
    assert os.environ["HPC_CAMPAIGN_ID"] == "other-campaign"  # ambient value restored


# ── blocked mid-loop ──────────────────────────────────────────────────────────


def test_slot_blocked_mid_loop(tmp_path: Path) -> None:
    """A slot that resolves to prior_run_found stops the loop: stage
    refill_blocked, needs_decision, blocked populated, only the prior slots
    dispatched — and NO ledger item for the blocked slot (it never resolved,
    so there is no identity to enqueue)."""
    _greenlit_async_manifest(tmp_path)

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 3}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(
            _RESOLVE,
            side_effect=[_resolved("ml-aaaa1111"), _blocked("ml-bbbb2222")],
        ) as m_resolve,
        mock.patch(_DISPATCH, side_effect=_dispatch_se("ml-aaaa1111")) as m_dispatch,
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert res.stage_reached == "refill_blocked"
    assert res.needs_decision is True
    assert len(res.submitted) == 1
    assert res.submitted[0].run_id == "ml-aaaa1111"
    assert len(res.blocked) == 1
    assert res.blocked[0].stage == "prior_run_found"
    assert res.blocked[0].run_id == "ml-bbbb2222"
    assert m_resolve.call_count == 2
    assert m_dispatch.call_count == 1
    assert [i["item_id"] for i in _items(tmp_path)] == [f"{_CID}.ml-aaaa1111"]


@pytest.mark.parametrize(
    ("code", "needs_decision"),
    [
        ("gate_refused", True),
        ("resolve_blocked", True),
        ("lifecycle_failed", True),
        ("claim_held", False),
    ],
)
def test_dispatch_refusal_is_disclosed_and_stops_the_tick(
    tmp_path: Path, code: str, needs_decision: bool
) -> None:
    """D9: a refused dispatch is DATA, not an exception. The reason_code becomes
    the blocked slot's stage and the dispatcher's own sentence is relayed; only
    the codes a human must act on set needs_decision. The item stays on the
    ledger — a placed-but-unstarted slot is exactly what queue-status must show."""
    _greenlit_async_manifest(tmp_path)

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 2}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=[_resolved("ml-aaaa1111")]),
        mock.patch(_DISPATCH, return_value=_refused("ml-aaaa1111", code)),
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert res.stage_reached == "refill_blocked"
    assert res.needs_decision is needs_decision
    assert [b.stage for b in res.blocked] == [code]
    assert res.blocked[0].reason == f"refused because {code}"
    assert res.blocked[0].run_id == "ml-aaaa1111"
    assert [i["item_id"] for i in _items(tmp_path)] == [f"{_CID}.ml-aaaa1111"]


def test_advance_holdback_is_relayed_verbatim(tmp_path: Path) -> None:
    """An item ``queue-advance`` HELD (it never placed it) is relayed with the
    AUTHORITY's own reason. Restating it here would put the actor's words in
    front of a decision the actor did not make (R4)."""
    _greenlit_async_manifest(tmp_path)
    held = QueueDispatchResult(
        computed_at="2026-07-29T00:00:00Z",
        stage_reached="nothing_to_dispatch",
        reason="nothing placed",
        held=[
            QueueHoldback(
                item_id=f"{_CID}.ml-aaaa1111",
                reason_code="cluster_pin_unknown",
                reason="the item pins cluster 'hoffman2', which the active clusters.yaml ...",
            )
        ],
    )

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 1}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=[_resolved("ml-aaaa1111")]),
        mock.patch(_DISPATCH, return_value=held),
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert res.stage_reached == "refill_blocked"
    assert res.blocked[0].stage == "cluster_pin_unknown"
    assert res.blocked[0].reason.startswith("the item pins cluster")


def test_empty_dispatch_result_still_states_a_reason(tmp_path: Path) -> None:
    """R4 cuts both ways: a dispatcher that returned nothing at all — no
    dispatch, no refusal, no holdback — must not be reported as a silent
    success, and the actor must not invent a cause it was not given."""
    _greenlit_async_manifest(tmp_path)
    empty = QueueDispatchResult(
        computed_at="2026-07-29T00:00:00Z",
        stage_reached="nothing_to_dispatch",
        reason="queue-advance placed nothing this call",
    )

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 1}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=[_resolved("ml-aaaa1111")]),
        mock.patch(_DISPATCH, return_value=empty),
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert res.stage_reached == "refill_blocked"
    assert res.blocked[0].stage == "nothing_to_dispatch"
    assert res.blocked[0].reason == "queue-advance placed nothing this call"


def test_resolved_without_identity_is_a_loud_contract_violation(tmp_path: Path) -> None:
    """A ``resolved`` stage missing run_id / cmd_sha / submit_spec cannot be
    enqueued with the identity §10.S3 requires — and an item with half an
    identity is worse than none, because occupancy would join on the missing
    half. Loud, never a None threaded onto the ledger."""
    _greenlit_async_manifest(tmp_path)
    half = ResolveSubmitInputsResult(
        stage_reached="resolved",
        needs_decision=False,
        reason="inputs resolved",
        run_id="ml-aaaa1111",
        cmd_sha=None,
        submit_spec=_submit_flow_dict("ml-aaaa1111"),
    )

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 1}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=[half]),
        mock.patch(_DISPATCH) as m_dispatch,
        pytest.raises(errors.SpecInvalid, match="no run_id / cmd_sha / submit_spec"),
    ):
        campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    m_dispatch.assert_not_called()
    assert _items(tmp_path) == []


# ── the deterministic slot token: replays dedup, distinct trials do not ───────


def test_replayed_slot_does_not_double_enqueue(tmp_path: Path) -> None:
    """The slot token is derived from the RESOLVED trial identity, so a tick that
    re-resolves the SAME trial (a replayed workflow turn, a retried tick whose
    sidecar write never landed) appends NO second line and holds no second pool
    slot. A random token would have made the same trial two committed slots."""
    _greenlit_async_manifest(tmp_path)

    for _ in range(2):
        with (
            mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 1}),
            mock.patch(_BUILD, return_value=_resolve_spec()),
            mock.patch(_RESOLVE, side_effect=[_resolved("ml-aaaa1111")]),
            mock.patch(_DISPATCH, side_effect=_dispatch_se("ml-aaaa1111")),
        ):
            res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))
        assert res.stage_reached == "refilled"

    items = _items(tmp_path)
    assert len(items) == 1
    assert items[0]["item_id"] == f"{_CID}.ml-aaaa1111"
    assert items[0]["record_count"] == 1  # ONE ledger record, not two
    # The replay is disclosed rather than passed off as a fresh enqueue.
    assert "replayed an existing ledger item" in res.reason


def test_distinct_trials_get_distinct_items(tmp_path: Path) -> None:
    """The negative half: two DIFFERENT resolved trials must not collapse. The
    token dedups on identity, and a distinct cmd_sha is a distinct run id."""
    _greenlit_async_manifest(tmp_path)
    run_ids = ["ml-aaaa1111", "ml-bbbb2222"]

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 2}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=[_resolved(r) for r in run_ids]),
        mock.patch(_DISPATCH, side_effect=_dispatch_se(*run_ids)),
    ):
        campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert len(_items(tmp_path)) == 2


def test_uncomposed_campaign_id_enqueues_without_a_base_and_says_so(tmp_path: Path) -> None:
    """A campaign id that is NOT ``<base>_<clusterkey>`` yields no base — refill
    refuses to invent one (that would count the slot against a campaign that does
    not exist) and DISCLOSES that the §10.S3 window stays open for it."""
    _greenlit_async_manifest(tmp_path, campaign_id="legacy")

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 1}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=[_resolved("ml-aaaa1111")]),
        mock.patch(
            _DISPATCH,
            return_value=_dispatched("ml-aaaa1111", item_id="legacy.ml-aaaa1111"),
        ),
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id="legacy"))

    assert res.stage_reached == "refilled"
    items = _items(tmp_path)
    assert items[0]["campaign_base"] is None
    assert items[0]["cluster_pin"] == _CLUSTER  # the pin is still recorded
    assert "not composed as '<base>_hoffman2'" in res.reason


def test_campaign_base_is_recovered_not_split() -> None:
    """``_campaign_base_for`` removes a KNOWN cluster suffix and re-composes to
    check it — it never splits on '_'. A base containing underscores (the
    documented hazard) must survive intact, and a cid that does not end in this
    cluster's key must yield None rather than a plausible-looking guess."""
    from hpc_agent.ops.campaign_refill import _campaign_base_for

    assert _campaign_base_for("rv_sweep_stage2_hoffman2", "hoffman2") == "rv_sweep_stage2"
    assert _campaign_base_for("tune_carc", "hoffman2") is None  # different cluster
    assert _campaign_base_for("hoffman2", "hoffman2") is None  # no base left over
    assert _campaign_base_for("tune", "") is None  # no cluster key at all


def test_slot_refuses_a_campaign_id_that_cannot_key_its_lock(tmp_path: Path) -> None:
    """The guard FIRES before anything is resolved: a campaign id carrying a path
    separator cannot name the durable per-campaign lock file, and a lock on the
    wrong path is not a lock. Typed refusal, not a raw ValueError from the
    filesystem layer."""
    from hpc_agent.ops.campaign_refill import _refill_slot

    with pytest.raises(errors.SpecInvalid, match="cannot key its dispatch lock"):
        _refill_slot(
            tmp_path,
            cid="tune/hoffman2",
            cluster=_CLUSTER,
            campaign_base=_BASE,
            resolve_spec=_resolve_spec(),
        )


def test_slot_token_refuses_an_unusable_campaign_id() -> None:
    """The guard FIRES: a campaign id outside the ledger's charset cannot name an
    item, and falling back to a random token would silently re-open the
    double-enqueue the deterministic derivation exists to prevent."""
    from hpc_agent.ops.campaign_refill import _slot_request_id

    assert _slot_request_id("tune_hoffman2", "ml-aaaa1111") == "tune_hoffman2.ml-aaaa1111"
    with pytest.raises(errors.SpecInvalid, match="not a legal queue item id"):
        _slot_request_id("tune/hoffman2", "ml-aaaa1111")


# ── crash-mid-tick self-correction (real advance over a synthetic journal) ─────


def _seed_iteration(experiment_dir: Path, *, run_id: str, campaign_id: str, status: str) -> None:
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha="0" * 12,
        hpc_agent_version="0.0.0+test",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python train.py --seed $SEED",
        result_dir_template="results/{run_id}/{task_id}",
        task_count=1,
        tasks_py_sha="0" * 12,
        campaign_id=campaign_id,
        profile="ml",
        cluster=_CLUSTER,
        remote_path="/u/scratch/exp",
    )
    upsert_run(
        experiment_dir,
        RunRecord(
            run_id=run_id,
            profile="ml",
            cluster=_CLUSTER,
            ssh_target="user@host",
            remote_path="/scratch/exp",
            job_name="ml",
            job_ids=["1"],
            total_tasks=1,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(experiment_dir.resolve()),
            campaign_id=campaign_id,
            status=status,
        ),
    )


def test_crash_mid_tick_self_corrects_via_shrunk_refill_count(
    journal_home: Path, tmp_path: Path
) -> None:
    """Simulate a prior partial tick that submitted 2 of 3 slots (2 iterations now
    in flight). Re-ticking with REAL campaign-advance recomputes refill_count from
    the journal + ledger: K=3 pool with 2 occupied → refill_count SHRINKS to 1, so
    exactly one more slot runs. No cursor, no new state file."""
    _greenlit_async_manifest(tmp_path, k=3)
    _seed_iteration(tmp_path, run_id="ml-slot0", campaign_id=_CID, status="in_flight")
    _seed_iteration(tmp_path, run_id="ml-slot1", campaign_id=_CID, status="in_flight")

    with (
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=[_resolved("ml-slot2")]) as m_resolve,
        mock.patch(_DISPATCH, side_effect=_dispatch_se("ml-slot2")) as m_dispatch,
    ):
        # NOTE: campaign_advance is REAL here — it reads the 2 in-flight records.
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert res.stage_reached == "refilled"
    assert res.decision == "refill"
    assert res.refill_count == 1  # 3 - 2 occupied (had shrunk from a full-pool 3).
    assert len(res.submitted) == 1
    assert m_resolve.call_count == 1
    assert m_dispatch.call_count == 1


def test_crash_between_enqueue_and_dispatch_leaves_a_visible_queued_slot(
    journal_home: Path, tmp_path: Path
) -> None:
    """THE §10.S3 window, closed. The dispatch of slot 1 dies hard (a killed
    process, not a refusal), so the item is on the ledger with NO run anywhere.

    Before D5/D6 that slot was invisible: no journal record, no ``in_flight``, so
    the next tick asked for the FULL pool again and re-proposed a slot that was
    already committed. Now the queued item occupies a pool slot, the next REAL
    advance asks for one fewer, and the orphan is visible on the ledger with its
    state — nothing is dropped (R4)."""
    _greenlit_async_manifest(tmp_path, k=2)

    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 2}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=[_resolved("ml-aaaa1111")]),
        mock.patch(_DISPATCH, side_effect=RuntimeError("dispatcher killed mid-spawn")),
        pytest.raises(RuntimeError, match="killed mid-spawn"),
    ):
        campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    # The residue is DURABLE and visible, not an invisible orphan.
    items = _items(tmp_path)
    assert [(i["item_id"], i["state"]) for i in items] == [
        (f"{_CID}.ml-aaaa1111", "queued"),
    ]

    # ...and the next tick's REAL advance counts it: K=2 minus the one committed
    # slot leaves room for exactly ONE more, not two.
    from hpc_agent.meta.campaign.atoms.advance import campaign_advance

    adv = campaign_advance(experiment_dir=tmp_path, campaign_id=_CID)
    assert adv["decision"] == "refill"
    assert adv["occupied"] == 1
    assert adv["refill_count"] == 1
    assert adv["status"]["in_flight"] == 0  # the journal still knows nothing


def test_full_pool_waits_not_refills(journal_home: Path, tmp_path: Path) -> None:
    """Contrast to the shrink test: when the pool is already full (K=2, 2 in
    flight) REAL advance decides wait_in_flight, so the actor is a no-op — this is
    the terminal of the self-correction (the last partial slot never over-submits)."""
    _greenlit_async_manifest(tmp_path, k=2)
    _seed_iteration(tmp_path, run_id="ml-slot0", campaign_id=_CID, status="in_flight")
    _seed_iteration(tmp_path, run_id="ml-slot1", campaign_id=_CID, status="in_flight")

    with (
        mock.patch(_RESOLVE) as m_resolve,
        mock.patch(_DISPATCH) as m_dispatch,
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert res.stage_reached == "no_refill_needed"
    assert res.decision == "wait_in_flight"
    m_resolve.assert_not_called()
    m_dispatch.assert_not_called()


def test_queued_items_alone_can_hold_the_pool_full(journal_home: Path, tmp_path: Path) -> None:
    """The guard FIRES from the LEDGER side: two queued-but-undispatched slots
    fill a K=2 pool, so REAL advance says wait_in_flight and refill resolves
    nothing — the over-submit §10.S3 named, refused."""
    _greenlit_async_manifest(tmp_path, k=2)
    with (
        mock.patch(_ADV, return_value={"decision": "refill", "reason": "r", "refill_count": 2}),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=[_resolved("ml-aaaa1111"), _resolved("ml-bbbb2222")]),
        mock.patch(_DISPATCH, side_effect=_dispatch_se("ml-aaaa1111", "ml-bbbb2222")),
    ):
        campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))
    assert len(_items(tmp_path)) == 2

    with (
        mock.patch(_RESOLVE) as m_resolve,
        mock.patch(_DISPATCH) as m_dispatch,
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert res.stage_reached == "no_refill_needed"
    assert res.decision == "wait_in_flight"
    m_resolve.assert_not_called()
    m_dispatch.assert_not_called()


# ── §10.S3 / D9: the recovery leg and the in-lock pool bound ──────────────────


def _strand(experiment_dir: Path, item_id: str, run_id: str) -> None:
    """A PLACED item with no RunRecord — the residue of a dispatch that never
    started (a crash after the placement append, a gate refusal, a cluster that
    left clusters.yaml). ``queue-advance`` reads only ``queued`` items, so
    nothing re-offers it, and it occupies a pool slot until it runs."""
    from hpc_agent.state.queue_intake import (
        append_intake_item,
        append_intake_placement,
        placement_request_id,
    )

    append_intake_item(
        experiment_dir,
        record={
            "spec": _submit_flow_dict(run_id),
            "run_name": "ml",
            "run_id": run_id,
            "cmd_sha": "a" * 8,
            "cluster_pin": _CLUSTER,
            "campaign_base": _BASE,
        },
        request_id=item_id,
    )
    append_intake_placement(
        experiment_dir,
        item_id=item_id,
        request_id=placement_request_id(item_id),
        cluster=_CLUSTER,
        campaign_id=_CID,
        reason="placed by an earlier tick",
        run_id=run_id,
    )


def test_a_stranded_placement_is_re_actuated_before_anything_new_is_resolved(
    tmp_path: Path,
) -> None:
    """The recovery leg. A placed-but-unstarted item is committed work that NO
    automatic path would ever offer again, and it holds a pool slot until it runs
    — so K of them is a permanent ``wait_in_flight`` over an empty cluster, and
    the CLI help's "partial ticks self-correct via next tick's shrunk
    refill_count" is false for exactly this residue.

    It is re-offered BEFORE the refill decision and INDEPENDENTLY of it, because
    the stranded items ARE the occupancy: asking advance for pool room first
    returns ``wait_in_flight`` and the residue could never be cleared. Re-using
    it is also strictly cheaper than replacing it — its optuna proposal index is
    already spent, and dispatching it is the only way that index becomes a run.
    """
    _greenlit_async_manifest(tmp_path)
    _strand(tmp_path, "stranded-1", "ml-dddd4444")

    with (
        mock.patch(
            _ADV, return_value={"decision": "wait_in_flight", "reason": "pool full"}
        ) as m_adv,
        mock.patch(_DISPATCH, side_effect=_dispatch_se("ml-dddd4444")) as m_dispatch,
        mock.patch(_RESOLVE) as m_resolve,
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    # Recovery ran even though advance said "no refill" — and before it was asked.
    assert m_dispatch.call_args.kwargs["spec"].item_ids == ["stranded-1"]
    assert [row.run_id for row in res.submitted] == ["ml-dddd4444"]
    assert res.stage_reached == "refilled"
    assert "RECOVERY" in res.reason
    # No NEW trial was resolved: the stranded item already paid for its index.
    m_resolve.assert_not_called()
    assert m_adv.called


def test_a_stranded_placement_that_still_refuses_is_escalated_not_hidden(
    tmp_path: Path,
) -> None:
    """A placement that cannot start is exactly the fact the silent wedge hid.
    It sets the tick's terminal even when nothing else went wrong."""
    _greenlit_async_manifest(tmp_path)
    _strand(tmp_path, "stranded-1", "ml-dddd4444")

    with (
        mock.patch(_ADV, return_value={"decision": "wait_in_flight", "reason": "pool full"}),
        mock.patch(
            _DISPATCH, return_value=_refused("ml-dddd4444", "gate_refused", item_id="stranded-1")
        ),
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert res.stage_reached == "refill_blocked"
    assert res.needs_decision is True
    assert [row.stage for row in res.blocked] == ["gate_refused"]
    assert "still refused" in res.reason


def test_a_stranded_placement_held_by_a_peer_wakes_nobody(tmp_path: Path) -> None:
    """``claim_held`` on the recovery leg is a healthy peer, not a decision."""
    _greenlit_async_manifest(tmp_path)
    _strand(tmp_path, "stranded-1", "ml-dddd4444")

    with (
        mock.patch(_ADV, return_value={"decision": "wait_in_flight", "reason": "pool full"}),
        mock.patch(
            _DISPATCH, return_value=_refused("ml-dddd4444", "claim_held", item_id="stranded-1")
        ),
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert res.stage_reached == "refill_blocked"
    assert res.needs_decision is False


def test_an_item_whose_run_already_started_is_not_re_offered(tmp_path: Path) -> None:
    """The negative twin: STRANDED means placed with NO RunRecord. A live or
    complete run is already the dispatch, and a failed one is a campaign
    failure-handling question — not a slot to silently resubmit."""
    _greenlit_async_manifest(tmp_path)
    _strand(tmp_path, "started-1", "ml-eeee5555")
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        tmp_path,
        RunRecord(
            run_id="ml-eeee5555",
            profile="ml",
            cluster=_CLUSTER,
            ssh_target="me@h",
            remote_path="/u/scratch/ml",
            job_name="ml",
            job_ids=["1"],
            total_tasks=1,
            submitted_at="2026-07-29T00:00:00+00:00",
            experiment_dir=str(tmp_path),
            campaign_id=_CID,
            status="in_flight",
        ),
    )

    with (
        mock.patch(_ADV, return_value={"decision": "wait_in_flight", "reason": "pool full"}),
        mock.patch(_DISPATCH) as m_dispatch,
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    m_dispatch.assert_not_called()
    assert res.stage_reached == "no_refill_needed"
    assert "RECOVERY" not in res.reason


def test_each_slot_re_asks_the_authority_inside_the_lock(tmp_path: Path) -> None:
    """The over-fill bound. ``refill_count`` is computed BEFORE any lock exists, so
    two overlapping passes both read occupied=0 and both plan a FULL pool — and
    the per-cid lock is what makes that real rather than theoretical: serialized
    resolves consume DISTINCT optuna indices, so every downstream defense
    (distinct cmd_sha → run_id → item id → lease → submit-once record) passes by
    construction and 2K arrays land on a pool sized for K. Only re-reading the
    count under the lock bounds it, and only the authority may own that
    arithmetic (R9)."""
    _greenlit_async_manifest(tmp_path)
    # Tick opens at refill_count=3; a peer fills the pool after the first slot.
    answers = [
        {"decision": "refill", "reason": "r", "refill_count": 3},  # the opening call
        {"decision": "refill", "reason": "r", "refill_count": 3},  # slot 1's re-ask
        {"decision": "wait_in_flight", "reason": "async pool full: 3 occupied"},
    ]

    with (
        mock.patch(_ADV, side_effect=answers),
        mock.patch(_BUILD, return_value=_resolve_spec()),
        mock.patch(_RESOLVE, side_effect=[_resolved("ml-aaaa1111")]) as m_resolve,
        mock.patch(_DISPATCH, side_effect=_dispatch_se("ml-aaaa1111")) as m_dispatch,
    ):
        res = campaign_refill(tmp_path, spec=CampaignRefillSpec(campaign_id=_CID))

    assert m_resolve.call_count == 1  # slot 2 stopped before spending an index
    assert m_dispatch.call_count == 1
    assert res.stage_reached == "refill_blocked"
    assert res.needs_decision is False  # a peer did the work; nobody must decide
    assert [row.stage for row in res.blocked] == ["pool_full"]
