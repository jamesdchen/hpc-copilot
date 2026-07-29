"""``campaign-advance`` async-refill decision tests (#362, plan §1.2 + §10.S3).

The refill rule is a function over the SHARED occupancy predicate
(``state/queue_occupancy.occupied_slots``) and
``campaign_budget.remaining.max_jobs``, so most of these drive it over
**synthetic evidence** by monkeypatching the atoms it composes rather than
standing up real journal records. Pins:

* default-off is byte-identical (never refills; wait_in_flight intact);
* async-on + free slots + headroom → ``refill`` with the exact count;
* ``refill_count = max(0, min(K - occupied, remaining_max_jobs))``, incl.
  the unbounded (``remaining = None``) and budget-capped cases;
* a full pool falls back to ``wait_in_flight`` (never over-submits);
* over_budget / stop_converged still win over refill (stops outrank refill);
* a terminal stop pending WHILE runs are in flight drains them first
  (``wait_in_flight``), and only emits the stop once the pool is empty —
  the issue's "don't orphan jobs on a terminal stop" guard.

The D6 block at the bottom is the rewiring itself, and it uses the REAL
predicate over a real ledger/journal: an enqueued-but-undispatched item shrinks
``pool_room`` (the §10.S3 window closed — the whole reason this wiring exists),
a superseded run stops occupying, and the ``wait_in_flight`` / drain legs
deliberately do NOT follow the ledger.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from hpc_agent.meta.campaign.atoms.advance import campaign_advance

_CID = "tune_async"


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    return tmp_path


def _patch_evidence(
    monkeypatch: pytest.MonkeyPatch,
    *,
    in_flight: int,
    remaining_max_jobs: int | None,
    exhausted: bool = False,
    iterations: int = 3,
    occupied: int | None = None,
) -> None:
    """Inject synthetic ``campaign_status`` / ``campaign_budget`` / occupancy evidence.

    *occupied* defaults to *in_flight* — the pre-D6 identity, which is what these
    ladder tests mean by "n runs hold n slots". Tests that care about the
    predicate's own arithmetic seed a real ledger instead and leave it alone.
    """

    def fake_status(*, experiment_dir: Path, campaign_id: str) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
            "iterations": iterations,
            "in_flight": in_flight,
            "history": [],
            "run_ids": [],
        }

    def fake_budget(*, experiment_dir: Path, campaign_id: str, **_caps: Any) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
            "spent": {"jobs": iterations, "tasks": iterations},
            "budget": {},
            "remaining": {"max_jobs": remaining_max_jobs},
            "projected": {},
            "coverage": {},
            "exhausted": exhausted,
            "reason": "max_jobs (cap met)" if exhausted else "within_budget",
        }

    monkeypatch.setattr("hpc_agent.meta.campaign.atoms.status.campaign_status", fake_status)
    monkeypatch.setattr("hpc_agent.meta.campaign.atoms.budget.campaign_budget", fake_budget)
    monkeypatch.setattr(
        "hpc_agent.meta.campaign.atoms.advance.occupied_slots",
        lambda _exp, _cid: in_flight if occupied is None else occupied,
    )


# ─── default-off: byte-identical synchronous ladder ─────────────────────────


def test_default_off_continue_when_idle(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_evidence(monkeypatch, in_flight=0, remaining_max_jobs=10)
    out = campaign_advance(experiment_dir=experiment, campaign_id=_CID)
    assert out["decision"] == "continue"
    assert out["refill_count"] is None


def test_default_off_waits_in_flight(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With async off, in-flight runs → wait_in_flight, never refill."""
    _patch_evidence(monkeypatch, in_flight=2, remaining_max_jobs=10)
    out = campaign_advance(experiment_dir=experiment, campaign_id=_CID)
    assert out["decision"] == "wait_in_flight"
    assert out["refill_count"] is None


# ─── async on: the refill decision ──────────────────────────────────────────


def test_async_refills_with_headroom(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """async + in_flight < K + budget headroom → refill with exact count."""
    _patch_evidence(monkeypatch, in_flight=1, remaining_max_jobs=10)
    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_CID, async_refill=True, max_in_flight=4
    )
    assert out["decision"] == "refill"
    # min(K=4, remaining=10) - in_flight=1 = 3
    assert out["refill_count"] == 3


def test_async_refill_unbounded_budget(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """remaining_max_jobs=None (no jobs cap) → refill up to K - in_flight."""
    _patch_evidence(monkeypatch, in_flight=1, remaining_max_jobs=None)
    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_CID, async_refill=True, max_in_flight=4
    )
    assert out["decision"] == "refill"
    assert out["refill_count"] == 3


def test_async_refill_budget_capped(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tight jobs budget caps the refill at the affordable headroom."""
    _patch_evidence(monkeypatch, in_flight=1, remaining_max_jobs=3)
    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_CID, async_refill=True, max_in_flight=10
    )
    assert out["decision"] == "refill"
    # min(K - in_flight = 9, remaining = 3) = 3. ``remaining`` already excludes the
    # 1 in-flight job (it carries a sidecar counted in ``spent``), so it is NOT
    # reduced by in_flight a second time.
    assert out["refill_count"] == 3


def test_async_refill_budget_caps_at_remaining(
    experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When budget headroom is the binding cap, refill == remaining_max_jobs
    exactly (never the old ``remaining - in_flight`` under-count)."""
    _patch_evidence(monkeypatch, in_flight=2, remaining_max_jobs=1)
    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_CID, async_refill=True, max_in_flight=10
    )
    assert out["decision"] == "refill"
    # min(K - in_flight = 8, remaining = 1) = 1 (the old formula gave 10-2... then
    # -? = under-count; the binding budget cap is exactly the 1 affordable job).
    assert out["refill_count"] == 1


def test_async_pool_full_waits(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At/over the target pool, refill falls back to wait_in_flight (no over-submit)."""
    _patch_evidence(monkeypatch, in_flight=4, remaining_max_jobs=10)
    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_CID, async_refill=True, max_in_flight=4
    )
    assert out["decision"] == "wait_in_flight"
    assert out["refill_count"] is None


def test_async_default_k_when_unset(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """async on with no max_in_flight uses the framework default K (4)."""
    _patch_evidence(monkeypatch, in_flight=0, remaining_max_jobs=None)
    out = campaign_advance(experiment_dir=experiment, campaign_id=_CID, async_refill=True)
    assert out["decision"] == "refill"
    assert out["refill_count"] == 4


# ─── stops outrank refill ───────────────────────────────────────────────────


def test_over_budget_beats_refill(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A met budget cap halts even with free slots — over_budget wins."""
    _patch_evidence(monkeypatch, in_flight=1, remaining_max_jobs=0, exhausted=True)
    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_CID, async_refill=True, max_in_flight=4
    )
    assert out["decision"] == "stop_over_budget"
    assert out["needs_acknowledgement"] is True
    assert out["refill_count"] is None


def test_converged_drains_in_flight_before_stop(
    experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fired stop criterion halts refilling, but with runs STILL in flight the
    async ladder drains them first (wait_in_flight) rather than orphaning them —
    the issue's "don't orphan jobs on a terminal stop" guard."""
    _patch_evidence(monkeypatch, in_flight=1, remaining_max_jobs=10)

    def fake_converged(**_kw: Any) -> dict[str, Any]:
        return {"converged": True, "reason": "max_iters_reached(5)"}

    monkeypatch.setattr(
        "hpc_agent.meta.campaign.atoms.converged.campaign_converged", fake_converged
    )
    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_CID, async_refill=True, max_in_flight=4
    )
    # Drains, does NOT stop while a run is still in flight (no refill either).
    assert out["decision"] == "wait_in_flight"
    assert out["refill_count"] is None


def test_converged_stops_once_pool_drained(
    experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the pool has drained (in_flight == 0), the same fired stop criterion
    finally emits stop_converged — the terminal stop the drain deferred."""
    _patch_evidence(monkeypatch, in_flight=0, remaining_max_jobs=10)

    def fake_converged(**_kw: Any) -> dict[str, Any]:
        return {"converged": True, "reason": "max_iters_reached(5)"}

    monkeypatch.setattr(
        "hpc_agent.meta.campaign.atoms.converged.campaign_converged", fake_converged
    )
    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_CID, async_refill=True, max_in_flight=4
    )
    assert out["decision"] == "stop_converged"
    assert out["refill_count"] is None


# ─── D6: the pool arithmetic routes through the SHARED predicate (§10.S3) ────
#
# From here down the occupancy predicate is REAL. These are the tests the
# rewiring exists for: the ledger's queued items are exactly the fact no journal
# holds, and pool_room has to see them or every tick inside the
# enqueue→dispatch window re-proposes slots that are already spoken for.

_BASE = "tune"
_CLUSTER = "hoffman2"
_COMPOSED_CID = f"{_BASE}_{_CLUSTER}"


def _patch_evidence_without_occupancy(
    monkeypatch: pytest.MonkeyPatch, *, in_flight: int, converged: bool = False
) -> None:
    """``campaign_status`` / ``campaign_budget`` fakes with the REAL predicate.

    Deliberately does NOT patch ``occupied_slots``: the point of these tests is
    that the number comes off the ledger + journal, so faking it would test the
    fake.
    """

    def fake_status(*, experiment_dir: Path, campaign_id: str) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
            "iterations": 1,
            "in_flight": in_flight,
            "history": [],
            "run_ids": [],
        }

    def fake_budget(*, experiment_dir: Path, campaign_id: str, **_caps: Any) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
            "spent": {"jobs": 1, "tasks": 1},
            "budget": {},
            "remaining": {"max_jobs": None},
            "projected": {},
            "coverage": {},
            "exhausted": False,
            "reason": "within_budget",
        }

    monkeypatch.setattr("hpc_agent.meta.campaign.atoms.status.campaign_status", fake_status)
    monkeypatch.setattr("hpc_agent.meta.campaign.atoms.budget.campaign_budget", fake_budget)
    if converged:
        monkeypatch.setattr(
            "hpc_agent.meta.campaign.atoms.converged.campaign_converged",
            lambda **_kw: {"converged": True, "reason": "max_iters_reached(5)"},
        )


def _enqueue_item(experiment_dir: Path, *, item: str, run_id: str | None = None) -> None:
    """One QUEUED intake item that composes onto ``_COMPOSED_CID``.

    Written through the ledger's own appender (not a hand-rolled line) so the
    fold, the pinned keys and the ``<base>_<cluster>`` composition rule are the
    real ones — this is the shape ``campaign-refill`` now produces per slot.
    """
    from hpc_agent.state.queue_intake import append_intake_item

    record: dict[str, Any] = {
        "spec": {"profile": "ml"},
        "run_name": "ml",
        "campaign_base": _BASE,
        "cluster_pin": _CLUSTER,
    }
    if run_id is not None:
        record["run_id"] = run_id
        record["cmd_sha"] = "0" * 8
    append_intake_item(experiment_dir, record=record, request_id=item)


def _seed_run(
    experiment_dir: Path, *, run_id: str, status: str = "in_flight", superseded_by: str = ""
) -> None:
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

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
            campaign_id=_COMPOSED_CID,
            status=status,
            superseded_by=superseded_by,
        ),
    )


def test_queued_intake_item_shrinks_pool_room(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE §10.S3 test. An enqueued-but-undispatched item occupies a pool slot.

    The journal knows nothing about it (no RunRecord exists yet — the fake
    status reports in_flight=0), so before D6 the pool read as empty and this
    tick would have asked for K MORE slots on top of the one already committed.
    """
    _patch_evidence_without_occupancy(monkeypatch, in_flight=0)
    _enqueue_item(experiment, item="slot-a", run_id="ml-aaaa1111")

    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_COMPOSED_CID, async_refill=True, max_in_flight=4
    )

    assert out["decision"] == "refill"
    assert out["occupied"] == 1
    assert out["refill_count"] == 3  # K=4 minus the one committed slot
    assert out["status"]["in_flight"] == 0  # ...which the JOURNAL still cannot see


def test_queued_items_can_fill_the_pool_and_stop_the_refill(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard FIRES: a pool held full by queued items refuses to refill at all.

    The negative half of the test above — without it "shrinks pool_room" could
    be satisfied by an off-by-one that still over-submits at the boundary. The
    fallback is ``wait_in_flight`` rather than ``continue``: reporting "no
    in-flight runs, plan another iteration" while two committed slots sit on the
    ledger is exactly the over-submit this closes.
    """
    _patch_evidence_without_occupancy(monkeypatch, in_flight=0)
    _enqueue_item(experiment, item="slot-a", run_id="ml-aaaa1111")
    _enqueue_item(experiment, item="slot-b", run_id="ml-bbbb2222")

    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_COMPOSED_CID, async_refill=True, max_in_flight=2
    )

    assert out["decision"] == "wait_in_flight"
    assert out["refill_count"] is None
    assert out["occupied"] == 2


def test_item_and_the_run_it_became_are_one_slot(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handoff window does not double-count: same run_id → same slot key.

    A dispatched item is still on the ledger (intake never learns it was
    dispatched — that is a projection, D8), so the union has to collapse it onto
    the run it became. Without the resolved identity on the ledger row this
    would read 2 and the pool would under-fill by one for the whole window.
    """
    _patch_evidence_without_occupancy(monkeypatch, in_flight=1)
    _enqueue_item(experiment, item="slot-a", run_id="ml-aaaa1111")
    _seed_run(experiment, run_id="ml-aaaa1111")

    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_COMPOSED_CID, async_refill=True, max_in_flight=4
    )

    assert out["occupied"] == 1
    assert out["refill_count"] == 3


def test_superseded_run_stops_occupying(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D6's other direction: a superseded record holds a slot no live job uses.

    ``campaign-status.in_flight`` counts every non-terminal record, superseded or
    not. The shared predicate excludes it, so the pool gets the slot back.
    """
    _patch_evidence_without_occupancy(monkeypatch, in_flight=2)
    _seed_run(experiment, run_id="ml-live0001")
    _seed_run(experiment, run_id="ml-dead0002", superseded_by="ml-live0001")

    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_COMPOSED_CID, async_refill=True, max_in_flight=2
    )

    assert out["occupied"] == 1
    assert out["decision"] == "refill"
    assert out["refill_count"] == 1


def test_completed_iterations_do_not_wedge_the_pool_forever(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE D6 wedge test — the one that decides whether this rewiring is safe.

    Intake has no terminal state (D8) and nothing compacts the ledger, so a
    dispatched item stays on it forever. Route the pool arithmetic through a
    predicate that counts those items unconditionally and a HEALTHY campaign
    stops refilling permanently after exactly ``K`` iterations: the journal half
    correctly drops the ``complete`` records, the ledger half keeps their slots,
    ``pool_room = max(0, K - K) = 0``, and the ``occupied > 0`` fallback returns
    ``wait_in_flight`` — forever, over an EMPTY cluster, with the whole job
    budget unspent and ``needs_decision`` false so nothing ever surfaces it.
    Pre-D6 (``pool_room = K - in_flight``) this could not happen.

    Every other test in this block seeds only NON-terminal runs, which is exactly
    why none of them caught it.
    """
    _patch_evidence_without_occupancy(monkeypatch, in_flight=0)
    for i in range(2):
        run_id = f"ml-done000{i}"
        _enqueue_item(experiment, item=f"slot-{i}", run_id=run_id)
        _seed_run(experiment, run_id=run_id, status="complete")

    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_COMPOSED_CID, async_refill=True, max_in_flight=2
    )

    assert out["occupied"] == 0
    assert out["decision"] == "refill"
    assert out["refill_count"] == 2


def test_queued_item_does_not_make_a_converged_campaign_drain_forever(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legs that must NOT follow the ledger: drain-before-stop and wait_in_flight.

    Their question is "would stopping now orphan a cluster JOB?", and a queued
    ledger item is not a job. If ``_drain_before_stop`` counted it, a converged
    campaign would wait forever behind an item nothing is going to dispatch —
    so the stop must fire with the item still on the ledger.
    """
    _patch_evidence_without_occupancy(monkeypatch, in_flight=0, converged=True)
    _enqueue_item(experiment, item="slot-a", run_id="ml-aaaa1111")

    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_COMPOSED_CID, async_refill=True, max_in_flight=4
    )

    assert out["decision"] == "stop_converged"
    assert out["occupied"] == 1  # still counted — it just does not gate the stop


def test_unpinned_item_occupies_no_cluster_pool(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An item with a base but NO pin is committed to no cluster, so no cid counts it.

    R4 as arithmetic: guessing which cluster an undecided item will land on
    would be an undisclosed placement decision dressed up as a count.
    """
    from hpc_agent.state.queue_intake import append_intake_item

    _patch_evidence_without_occupancy(monkeypatch, in_flight=0)
    append_intake_item(
        experiment,
        record={"spec": {"profile": "ml"}, "campaign_base": _BASE, "cluster_pin": None},
        request_id="floating",
    )

    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_COMPOSED_CID, async_refill=True, max_in_flight=4
    )

    assert out["occupied"] == 0
    assert out["refill_count"] == 4


# ─── the wiring itself, pinned structurally (the caller-census pattern) ──────


def test_pool_arithmetic_imports_and_uses_the_shared_predicate() -> None:
    """``advance`` imports ``occupied_slots`` and ``_refill`` reads it.

    The cheap structural pin R9 asks for: a future edit that re-inlines an
    in-flight count into the pool arithmetic would pass every behavioural test
    above that patches the atoms, and would silently re-open §10.S3's window.
    Two claims, both AST-checked: the import exists (so the census question "who
    calls the shared predicate?" has an answer), and ``_refill`` consumes the
    evidence key it produces rather than ``status.in_flight``.
    """
    import hpc_agent.meta.campaign.atoms.advance as advance_mod

    tree = ast.parse(inspect.getsource(advance_mod))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "hpc_agent.state.queue_occupancy"
        for alias in node.names
    }
    assert "occupied_slots" in imported

    refill = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_refill"
    )
    keys = {
        node.slice.value
        for node in ast.walk(refill)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
    }
    assert "occupied" in keys, "_refill's pool arithmetic must read evidence['occupied']"


def test_refill_rationale_names_both_numbers(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: the decision states the occupancy it was taken against, and the
    in-flight count beside it — the two now differ, so a reason naming only one
    would leave the human unable to reconcile the arithmetic."""
    _patch_evidence_without_occupancy(monkeypatch, in_flight=0)
    _enqueue_item(experiment, item="slot-a", run_id="ml-aaaa1111")

    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_COMPOSED_CID, async_refill=True, max_in_flight=4
    )

    assert "1 pool slot(s) occupied" in out["reason"]
    assert "0 in flight" in out["reason"]
