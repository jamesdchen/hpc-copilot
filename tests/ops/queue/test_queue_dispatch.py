"""``queue-dispatch`` — the actor's rules, each with the case that makes it fire.

``queue-dispatch`` composes: it consumes ``queue-advance``'s decision and starts
the item's normal lifecycle through the SAME ``campaign_run(detach=True)`` seat
``campaign-refill`` uses. So these tests fake that one seat — at its source
module, ``hpc_agent.ops.campaign_run.campaign_run``, the same monkeypatch point
``tests/meta/test_campaign_refill.py`` uses, because both actors import it
lazily inside the body — and everything else is real: a real intake ledger, a
real ``queue-advance``, a real journal under ``tmp_path``, a real
``clusters.yaml``. Nothing here opens a socket or touches a cluster.

What is PINNED, one negative case per binding rule:

* **D2 / D4 (adopt, never resubmit)** — a ``submitting`` / ``in_flight`` /
  ``complete`` RunRecord for the computed id makes the item an ADOPT and the
  submit seat is never called; the negative twin is a resubmittable terminal
  (``failed``), which is NOT adopted, proving the guard discriminates rather
  than always firing.
* **D2 (the claim is the shipped lease)** — a ``DetachedLeaseHeld`` from the
  detached launch becomes ``claim_held`` DATA, not an envelope error. It is
  ``DriveModeError(ValueError)``, so a bare ``except HpcError`` would have let
  it escape; that is exactly the case asserted.
* **D3 (the durable per-cid lock)** — a ``QueueDispatchLockHeld`` from the lock
  also becomes ``claim_held``, and neither claim sets ``needs_decision``.
* **D8 (no new intake state)** — after every path the ledger holds only
  ``{queued, placed}``, and a retried dispatch leaves ONE placement record.
* **D9 (nothing dropped, nothing guessed)** — an item with no derivable run id,
  one whose spec disagrees with its ledger identity, one whose spec targets
  another cluster, one that names its work by reference, and one named by a
  caller that advance never returned all come back as refusals with a closed
  ``reason_code`` — never started, never guessed at.
* **R3 (advance stays the authority)** — ``item_ids`` NARROWS and never places:
  a named item advance HELD is relayed held, with advance's own reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._kernel.lifecycle.detached import DetachedLeaseHeld
from hpc_agent._wire.workflows.campaign_run import CampaignRunResult
from hpc_agent._wire.workflows.queue_dispatch import QueueDispatchSpec
from hpc_agent.ops.queue.dispatch import queue_dispatch
from hpc_agent.state import queue_locks
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.queue_intake import append_intake_item, read_intake_items
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-07-29T12:00:00+00:00"

#: The submit seat this actor composes, patched at its SOURCE module (both
#: campaign-refill and queue-dispatch import it lazily inside the body).
_RUN = "hpc_agent.ops.campaign_run.campaign_run"

_CLUSTERS = """
alpha:
  scheduler: slurm
  host: alpha.edu
  user: me
  max_walltime_sec: 86400
beta:
  scheduler: slurm
  host: beta.edu
  user: me
  max_walltime_sec: 86400
"""


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the journal tree so RunRecords land under tmp, not the real home."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


@pytest.fixture(autouse=True)
def _clusters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "clusters.yaml"
    path.write_text(_CLUSTERS, encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(path))


def _exp(tmp_path: Path) -> Path:
    exp = tmp_path / "exp"
    exp.mkdir(exist_ok=True)
    return exp


def _submit_spec(run_id: str, *, cluster: str = "alpha") -> dict[str, Any]:
    """A minimal VALID resolved submit-flow spec (what resolve's submit_spec carries)."""
    return {
        "profile": "ml",
        "cluster": cluster,
        "ssh_target": f"me@{cluster}.edu",
        "remote_path": "/scratch/ml",
        "job_name": "ml_array",
        "run_id": run_id,
        "total_tasks": 1,
        "backend": "slurm",
        "script": ".hpc/templates/cpu_array.sh",
        "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
    }


def _enqueue(exp: Path, item_id: str, **record: Any) -> None:
    """One RESOLVED item, the §10.S3 refill shape: identity + an inline submit spec."""
    record.setdefault("run_name", "ml")
    record.setdefault("run_id", "ml-aaaa1111")
    record.setdefault("cmd_sha", "aaaa1111")
    record.setdefault("cluster_pin", "alpha")
    record.setdefault("campaign_base", "study")
    run_id = record.get("run_id")
    if "spec" not in record and "spec_ref" not in record:
        record["spec"] = _submit_spec(str(run_id or "ml-aaaa1111"))
    append_intake_item(exp, record=record, request_id=item_id)


def _seed_run(exp: Path, run_id: str, *, status: str, campaign_id: str = "study_alpha") -> None:
    upsert_run(
        exp,
        RunRecord(
            run_id=run_id,
            profile="ml",
            cluster="alpha",
            ssh_target="me@alpha.edu",
            remote_path="/scratch/ml",
            job_name="ml_array",
            job_ids=[] if status == "submitting" else ["1"],
            total_tasks=1,
            submitted_at="2026-07-29T00:00:00+00:00",
            experiment_dir=str(exp),
            status=status,
            campaign_id=campaign_id,
        ),
    )


def _detached(run_id: str = "ml-aaaa1111", pid: int = 4242) -> CampaignRunResult:
    return CampaignRunResult(
        stage_reached="detached",
        needs_decision=False,
        reason="detached",
        run_id=run_id,
        started=True,
        watch="journal",
        detached_pid=pid,
    )


def _spec(**kw: Any) -> QueueDispatchSpec:
    kw.setdefault("now", _NOW)
    return QueueDispatchSpec(**kw)


def _states(exp: Path) -> set[str]:
    return {str(item.get("state")) for item in read_intake_items(exp)}


def _item(exp: Path, item_id: str) -> dict[str, Any]:
    return next(i for i in read_intake_items(exp) if i.get("item_id") == item_id)


# ── boundary / empty ─────────────────────────────────────────────────────────


def test_bad_now_override_is_refused_at_the_boundary(tmp_path: Path) -> None:
    """A primitive owns its invariants: an unparseable instant is a loud refusal."""
    with pytest.raises(errors.SpecInvalid):
        queue_dispatch(experiment_dir=_exp(tmp_path), spec=QueueDispatchSpec(now="yesterday"))


def test_empty_queue_is_nothing_to_dispatch_not_a_refusal(tmp_path: Path) -> None:
    """'nothing to do' and 'work I could not start' are opposite situations."""
    with mock.patch(_RUN) as m_run:
        res = queue_dispatch(experiment_dir=_exp(tmp_path), spec=_spec())

    assert res.stage_reached == "nothing_to_dispatch"
    assert res.dispatched == []
    assert res.refused == []
    assert res.needs_decision is False
    assert res.brief
    m_run.assert_not_called()


# ── happy path ───────────────────────────────────────────────────────────────


def test_dispatches_a_placed_item_through_the_shipped_lifecycle(tmp_path: Path) -> None:
    """D1: the item is started by the SAME detached campaign-run seat refill uses,
    with aggregate.run_id == the item's computed id (the lease/poll/terminal key)."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")

    with mock.patch(_RUN, return_value=_detached()) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    assert res.stage_reached == "dispatched"
    assert res.needs_decision is False
    assert res.refused == []
    assert res.placements_considered == 1
    (row,) = res.dispatched
    assert (row.item_id, row.outcome, row.cluster) == ("item-1", "started", "alpha")
    assert row.run_id == "ml-aaaa1111"
    assert row.cmd_sha == "aaaa1111"
    assert row.campaign_id == "study_alpha"
    assert row.detached_pid == 4242
    assert row.stage_reached == "detached"
    assert row.placed is True
    assert row.reason  # D9: successes disclose why too
    assert res.brief

    # The composed spec: detached, and every run_id seat aligned on the computed id.
    crspec = m_run.call_args.kwargs["spec"]
    assert crspec.detach is True
    assert crspec.campaign_id == "study_alpha"
    assert crspec.aggregate.run_id == "ml-aaaa1111"
    assert crspec.status.monitor.run_id == "ml-aaaa1111"
    assert crspec.submit.submit.submit.run_id == "ml-aaaa1111"


def test_dispatch_records_the_placement_and_adds_no_intake_state(tmp_path: Path) -> None:
    """D8: the ONLY ledger write is the placement transition, and the vocabulary
    stays {queued, placed} — 'dispatched' is a projection, never a stored state."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")

    with mock.patch(_RUN, return_value=_detached()):
        queue_dispatch(experiment_dir=exp, spec=_spec())

    assert _states(exp) == {"placed"}
    item = _item(exp, "item-1")
    assert item["cluster"] == "alpha"
    assert item["campaign_id"] == "study_alpha"
    assert item["run_id"] == "ml-aaaa1111"
    assert item["reason"]  # R4: the placement travels with its disclosed why


def test_open_loop_item_dispatches_without_a_campaign_lock(tmp_path: Path) -> None:
    """An item with no campaign base occupies no campaign pool slot, so there is no
    per-cid window to serialize — the lock is not taken and the dispatch proceeds."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", campaign_base=None)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the per-cid lock must not be taken for an open-loop item")

    with (
        mock.patch(_RUN, return_value=_detached()),
        mock.patch("hpc_agent.ops.queue.dispatch.campaign_dispatch_lock", _boom),
    ):
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    (row,) = res.dispatched
    assert row.campaign_id is None
    assert row.outcome == "started"


# ── D2 / D4: adopt, never resubmit ───────────────────────────────────────────


@pytest.mark.parametrize("status", ["submitting", "in_flight", "complete"])
def test_existing_run_is_adopted_and_nothing_is_submitted(tmp_path: Path, status: str) -> None:
    """§10.S2.5: a RunRecord for the computed id IS the dispatch. Adopting is a
    SUCCESS reported as one — a silent skip would look like a crashed dispatcher."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")
    _seed_run(exp, "ml-aaaa1111", status=status)

    with mock.patch(_RUN) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    assert res.stage_reached == "dispatched"
    (row,) = res.dispatched
    assert row.outcome == "adopted"
    assert row.adopted_status == status
    assert row.run_id == "ml-aaaa1111"
    assert row.reason
    m_run.assert_not_called()  # nothing submitted a second time
    # The adopt still hands the item off the queue, with its reason on the ledger.
    assert _states(exp) == {"placed"}
    assert "adopted" in _item(exp, "item-1")["reason"]


@pytest.mark.parametrize("status", ["failed", "abandoned"])
def test_resubmittable_terminal_is_not_adopted(tmp_path: Path, status: str) -> None:
    """The negative twin of the adopt guard: a corpse is not a dispatch. Without
    this case the guard could be 'always adopt when a record exists' and pass."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")
    _seed_run(exp, "ml-aaaa1111", status=status)

    with mock.patch(_RUN, return_value=_detached()) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    (row,) = res.dispatched
    assert row.outcome == "started"
    assert row.adopted_status is None
    m_run.assert_called_once()


def test_replayed_terminal_from_the_lifecycle_is_reported_as_an_adopt(tmp_path: Path) -> None:
    """campaign-run replays a finished worker's recorded terminal for the same
    cmd_sha. Nothing was submitted, so it is an adopt — not a start that never
    happened."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")
    replay = CampaignRunResult(
        stage_reached="complete",
        needs_decision=False,
        reason="iteration spine complete",
        run_id="ml-aaaa1111",
    )

    with mock.patch(_RUN, return_value=replay):
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    (row,) = res.dispatched
    assert row.outcome == "adopted"
    assert row.stage_reached == "complete"


# ── D2 / D3: the two held-claim classes ──────────────────────────────────────


def test_detached_lease_held_is_claim_held_data_not_an_envelope_error(tmp_path: Path) -> None:
    """DetachedLeaseHeld is DriveModeError(ValueError), NOT an HpcError — a bare
    `except errors.HpcError` would let it escape. A claim held by a live peer is
    a healthy fleet, so it comes back as a refusal ROW, and never as
    needs_decision (nobody has anything to decide)."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")
    held = DetachedLeaseHeld("a live detached worker (pid 991) already owns the lease")

    with mock.patch(_RUN, side_effect=held):
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    assert res.stage_reached == "dispatch_refused"
    assert res.needs_decision is False
    (row,) = res.refused
    assert row.reason_code == "claim_held"
    assert "pid 991" in row.reason  # the peer is NAMED, so the operator can look
    assert row.run_id == "ml-aaaa1111"
    assert row.placed is True  # durable-first: the placement landed before the start
    assert res.refused_counts == {"claim_held": 1}


def test_dispatch_lock_held_is_claim_held(tmp_path: Path) -> None:
    """D3: the per-cid E4 lock's timeout is the OTHER held-claim class, and it maps
    to the same disclosed outcome. Patched at the seam dispatch imported it under,
    because a genuine contended acquire would block for the lock's full timeout."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")

    def _held(*_a: Any, **_k: Any) -> None:
        raise errors.QueueDispatchLockHeld("another process is inside the window (pid 77)")

    with (
        mock.patch("hpc_agent.ops.queue.dispatch.campaign_dispatch_lock", _held),
        mock.patch(_RUN) as m_run,
    ):
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    assert res.needs_decision is False
    (row,) = res.refused
    assert row.reason_code == "claim_held"
    assert row.placed is False  # the lock is taken BEFORE the ledger write
    assert "pid 77" in row.reason
    m_run.assert_not_called()
    assert _states(exp) == {"queued"}  # still advance's to re-decide


# ── lifecycle outcomes ───────────────────────────────────────────────────────


def test_gate_refusal_from_the_submit_spine_is_gate_refused(tmp_path: Path) -> None:
    """D1: dispatch neither re-asks the gate's question nor overrides its answer —
    it relays it. A refused gate IS a human decision, so needs_decision is set."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")
    refused = CampaignRunResult(
        stage_reached="submit_failed",
        needs_decision=True,
        reason="submit spine stopped at 'canary_failed': the canary task exited 1.",
        run_id="ml-aaaa1111",
    )

    with mock.patch(_RUN, return_value=refused):
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    assert res.stage_reached == "dispatch_refused"
    assert res.needs_decision is True
    (row,) = res.refused
    assert row.reason_code == "gate_refused"
    assert "canary_failed" in row.reason


def test_lifecycle_error_is_lifecycle_failed_not_a_crash(tmp_path: Path) -> None:
    """A fleet in which one item's transport is down must still dispatch the rest,
    so a lifecycle HpcError is per-item DATA rather than an envelope error."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")

    with mock.patch(_RUN, side_effect=errors.SshUnreachable("login node down")):
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    (row,) = res.refused
    assert row.reason_code == "lifecycle_failed"
    assert "login node down" in row.reason
    assert res.needs_decision is False


def test_unclassified_exception_is_re_raised_not_swallowed(tmp_path: Path) -> None:
    """The negative case for the catch: only the classified refusals become data.
    Swallowing an unclassified exception would turn a real bug into a note."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")

    with (
        mock.patch(_RUN, side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        queue_dispatch(experiment_dir=exp, spec=_spec())


# ── D9: refusals for items that cannot be actuated ───────────────────────────


def test_item_with_no_derivable_run_id_is_refused_never_guessed(tmp_path: Path) -> None:
    """§10.S2: a run id is COMPUTED, never minted. An item carrying neither a
    resolved identity, nor a spec run_id, nor a run_name to recompute from has no
    id — so it is refused and left QUEUED for advance to re-decide."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", run_name=None, run_id=None, cmd_sha=None, spec={"entry": "tasks.py"})

    with mock.patch(_RUN) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    assert res.stage_reached == "dispatch_refused"
    (row,) = res.refused
    assert row.reason_code == "item_unresolved"
    assert row.run_id is None
    assert row.placed is False
    m_run.assert_not_called()
    assert _states(exp) == {"queued"}


def test_spec_ref_item_is_refused_rather_than_dereferenced(tmp_path: Path) -> None:
    """queue-dispatch resolves nothing and dereferences nothing: the seat that
    resolves is the seat that enqueues (§10.S3, E4)."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", spec=None, spec_ref="specs/trial.json")

    with mock.patch(_RUN) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    (row,) = res.refused
    assert row.reason_code == "item_unresolved"
    assert row.run_id == "ml-aaaa1111"  # the identity is known; the WORK is not
    assert row.detail["spec_ref"] == "specs/trial.json"
    m_run.assert_not_called()


def test_item_disagreeing_with_its_own_spec_identity_is_refused(tmp_path: Path) -> None:
    """The ledger says one run id and the spec says another: starting either would
    claim a lease the other half of the record does not point at."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", spec=_submit_spec("ml-bbbb2222"))

    with mock.patch(_RUN) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    (row,) = res.refused
    assert row.reason_code == "item_unresolved"
    assert row.detail == {"ledger_run_id": "ml-aaaa1111", "spec_run_id": "ml-bbbb2222"}
    m_run.assert_not_called()


def test_spec_targeting_another_cluster_than_the_placement_is_refused(tmp_path: Path) -> None:
    """R9: submitting to a cluster the occupancy arithmetic is not counting this
    item against would over-fill the pool silently."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", spec=_submit_spec("ml-aaaa1111", cluster="beta"))

    with mock.patch(_RUN) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    (row,) = res.refused
    assert row.reason_code == "cluster_unresolvable"
    assert row.detail == {"placed_cluster": "alpha", "spec_cluster": "beta"}
    m_run.assert_not_called()


def test_unstartable_spec_is_refused_with_the_validation_detail(tmp_path: Path) -> None:
    """An inline spec the run lifecycle refuses is the enqueuer's invariant to own;
    dispatch names it rather than trying to repair it."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", spec={"profile": "ml", "run_id": "ml-aaaa1111", "cluster": "alpha"})

    with mock.patch(_RUN) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    (row,) = res.refused
    assert row.reason_code == "item_unresolved"
    assert row.detail["errors"]
    m_run.assert_not_called()


def test_placed_item_whose_cluster_left_clusters_yaml_is_refused(tmp_path: Path) -> None:
    """The actor ALWAYS re-resolves the placed cluster at use time (a placement's
    disclosed ssh target is stale by design). Reached through the recovery path
    below, since advance never places onto a key clusters.yaml does not have."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")
    from hpc_agent.state.queue_intake import append_intake_placement, placement_request_id

    append_intake_placement(
        exp,
        item_id="item-1",
        request_id=placement_request_id("item-1"),
        cluster="gamma",  # retired from clusters.yaml since the placement
        campaign_id="study_gamma",
        reason="placed before the config edit",
        run_id="ml-aaaa1111",
    )

    with mock.patch(_RUN) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec(item_ids=["item-1"]))

    (row,) = res.refused
    assert row.reason_code == "cluster_unresolvable"
    assert "gamma" in row.reason
    m_run.assert_not_called()


def test_named_item_absent_from_the_ledger_is_refused_not_dropped(tmp_path: Path) -> None:
    """R4: an item the caller named and advance never returned still comes back."""
    with mock.patch(_RUN) as m_run:
        res = queue_dispatch(experiment_dir=_exp(tmp_path), spec=_spec(item_ids=["ghost"]))

    (row,) = res.refused
    assert (row.item_id, row.reason_code) == ("ghost", "item_unresolved")
    m_run.assert_not_called()


# ── R3 / narrowing: item_ids selects, it never places ────────────────────────


def test_item_ids_narrows_to_the_named_item(tmp_path: Path) -> None:
    """The refill path's field: a slot enqueues one item and dispatches THAT one,
    rather than trusting arrival order to hand it back."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")
    _enqueue(exp, "item-2", run_id="ml-bbbb2222", cmd_sha="bbbb2222")

    with mock.patch(_RUN, return_value=_detached()) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec(item_ids=["item-2"]))

    assert [row.item_id for row in res.dispatched] == ["item-2"]
    assert m_run.call_count == 1
    assert _item(exp, "item-1")["state"] == "queued"  # untouched


def test_a_named_item_advance_held_is_relayed_held_never_placed(tmp_path: Path) -> None:
    """R3/R5: queue-advance stays the only authority. Naming an item cannot place
    it — an unplaceable pin comes back HELD, with advance's own reason."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", cluster_pin=None, campaign_base=None)

    with (
        mock.patch(_RUN) as m_run,
        # No cluster is configured, so advance can place nothing at all.
        mock.patch("hpc_agent.ops.queue.advance._load_clusters", return_value=({}, None)),
    ):
        res = queue_dispatch(experiment_dir=exp, spec=_spec(item_ids=["item-1"]))

    assert res.dispatched == []
    assert res.refused == []
    (hold,) = res.held
    assert hold.item_id == "item-1"
    assert hold.reason_code == "no_clusters_configured"
    assert res.held_counts == {"no_clusters_configured": 1}
    assert res.stage_reached == "nothing_to_dispatch"
    m_run.assert_not_called()


def test_max_dispatches_bounds_what_is_started(tmp_path: Path) -> None:
    """A bound on ACTUATION, applied after the authority decided — one signature,
    one start; the rest stay queued for the next tick."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")
    _enqueue(exp, "item-2", run_id="ml-bbbb2222", cmd_sha="bbbb2222")

    with mock.patch(_RUN, return_value=_detached()) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec(max_dispatches=1))

    assert len(res.dispatched) == 1
    assert m_run.call_count == 1


# ── D8: replay safety of the placement transition ────────────────────────────


def test_re_dispatching_a_placed_item_writes_one_placement_record(tmp_path: Path) -> None:
    """The placement's append token is DERIVED from the item id, so a retried or
    raced dispatch dedups against the existing transition instead of tearing the
    item with one placement line per attempt. The second call reaches the item
    through the ledger (advance reads only queued items) and ADOPTS the run the
    first call started."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")

    with mock.patch(_RUN, return_value=_detached()):
        first = queue_dispatch(experiment_dir=exp, spec=_spec())
    assert first.dispatched[0].placed is True

    # The first dispatch's run now exists — the second call must adopt it.
    _seed_run(exp, "ml-aaaa1111", status="submitting")
    with mock.patch(_RUN) as m_run:
        second = queue_dispatch(experiment_dir=exp, spec=_spec(item_ids=["item-1"]))

    (row,) = second.dispatched
    assert row.outcome == "adopted"
    assert row.placed is False  # the transition was already on the ledger
    m_run.assert_not_called()

    from hpc_agent.state.queue_intake import read_intake_records

    placements = [r for r in read_intake_records(exp) if r.get("kind") == "placement"]
    assert len(placements) == 1
    assert _states(exp) == {"placed"}


# ── campaign attribution: the placement's id is COMPOSED, the spec's is not ──


def test_uncomposed_campaign_id_is_recovered_from_the_items_own_submit_spec(
    tmp_path: Path,
) -> None:
    """A campaign whose id is not ``<base>_<cluster>`` must not lose its campaign.

    ``_Target.campaign_id`` comes from the placement, which composes it from the
    item's ``campaign_base`` + cluster — so it is ``None`` for every campaign
    named before the multi-cluster convention (``mytune`` on ``alpha``). The
    campaign is not actually unknown: the RESOLVED submit spec names it
    verbatim. Three things silently degraded when it was dropped, and all three
    are asserted here — the per-cid E4 lock (a ``None`` id took
    ``nullcontext``, i.e. NO lock), the ledger's placement record (whose stored
    id is what the occupancy predicate keys the item's cid on), and
    ``CampaignRunSpec.campaign_id``, which the lifecycle turns into the
    relay-due scope: a blank scope arms the run-#10 omission gate on nothing.
    """
    exp = _exp(tmp_path)
    spec = _submit_spec("ml-aaaa1111")
    spec["campaign_id"] = "mytune"
    _enqueue(exp, "item-1", campaign_base=None, spec=spec)

    locked: list[str] = []
    real_lock = queue_locks.campaign_dispatch_lock

    def _spy(experiment_dir: Path, campaign_id: str, **kw: Any) -> Any:
        locked.append(campaign_id)
        return real_lock(experiment_dir, campaign_id, **kw)

    with (
        mock.patch(_RUN, return_value=_detached()) as m_run,
        mock.patch("hpc_agent.ops.queue.dispatch.campaign_dispatch_lock", _spy),
    ):
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    (row,) = res.dispatched
    assert row.campaign_id == "mytune"
    assert locked == ["mytune"]
    assert m_run.call_args.kwargs["spec"].campaign_id == "mytune"
    assert _item(exp, "item-1")["campaign_id"] == "mytune"


def test_a_junk_campaign_id_on_the_spec_is_reported_absent_not_propagated(
    tmp_path: Path,
) -> None:
    """A hand-enqueued item's spec is OPAQUE to ``queue-run`` by design, so the
    recovered campaign id is charset-checked rather than trusted.

    The guard FIRES on the refusal row, not on the happy path: ``DispatchRefusal
    .campaign_id`` is ``CampaignId | None``, so an unchecked value would raise a
    pydantic ``ValidationError`` out of ``_refusal`` — turning the item's
    disclosed refusal into an envelope error that takes the whole call down and
    drops every other item with it (R4). The same value would also have reached
    a lock FILENAME.
    """
    exp = _exp(tmp_path)
    spec = _submit_spec("ml-aaaa1111")
    spec["campaign_id"] = "../escape"
    _enqueue(exp, "item-1", campaign_base=None, spec=spec)

    with mock.patch(_RUN, return_value=_detached()) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    # The lifecycle spec refuses the id too, so this item cannot start — the
    # point is that it comes back as DATA with a stated cause.
    (row,) = res.refused
    assert row.reason_code == "item_unresolved"
    assert row.campaign_id is None
    m_run.assert_not_called()


def test_the_adopt_placement_record_names_the_run_not_its_status(tmp_path: Path) -> None:
    """D8: the DURABLE ledger reason must not pin a point-in-time run status.

    Nothing revises a placement record, and ``queue-status`` projects its reason
    verbatim — so "adopted an existing submitting run" keeps asserting a status
    long after the run failed and was resubmitted. The run_id is the stable half;
    the status stays where D8 puts lifecycle, in the run stores. This call's OWN
    report may still name the status: that one is computed fresh each time.
    """
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1")
    _seed_run(exp, "ml-aaaa1111", status="submitting")

    with mock.patch(_RUN):
        res = queue_dispatch(experiment_dir=exp, spec=_spec())

    ledger_reason = _item(exp, "item-1")["reason"]
    assert "ml-aaaa1111" in ledger_reason
    assert "submitting" not in ledger_reason
    assert res.dispatched[0].adopted_status == "submitting"
