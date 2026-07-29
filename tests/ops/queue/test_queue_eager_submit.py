"""Eager submit (§7 R3) — the scheduler IS the capacity queue; we never meter it.

``docs/plans/run-queue-placement-2026-07-28.md`` §7 R3: once an item is
gate-cleared and placed it goes INTO the scheduler queue and sits ``pending``
there — the ledger never holds work back on inferred scheduler headroom, and
``no_capacity`` does not exist as a ledger state. These tests pin the
load-bearing behaviors with the case that fails without each:

* **a busy cluster still gets the dispatch, in the same tick** — occupancy far
  past the only cap-shaped config field (``constraints.max_concurrent_jobs``,
  which S11 proved is per-submission-plan wave grouping, not a cross-run cap)
  neither holds placement nor defers the start: the actuation seat is called
  in the same ``queue-dispatch`` call that consumed the decision;
* **an unattended batch submits eagerly too** — the wake tier's batch starts
  N lifecycles against a busy cluster in one tick, no deferral between them;
* **the hold vocabularies are CLOSED and capacity-free** — pinned exact, so a
  ``no_capacity`` reason code cannot reappear without failing a named test.
  ``courtesy_cap_reached`` is RESERVED in the placement vocabulary (etiquette
  policy, the connection-storm lineage — a courtesy hold names courtesy,
  never capacity) and stays producer-less until a real account-level cap key
  exists;
* **a hard-constraint hold is unchanged and names the constraint** — the item
  that fits nowhere is held ``no_cluster_matches_constraints`` with the
  configured ceiling in the verdict, and nothing in the disclosure claims a
  capacity inference;
* **the actor has no deferral seat** — pinned by source inspection: dispatch
  never reads the occupancy predicate, so it structurally cannot invent a
  capacity wait between placement and submit.

Fixtures mirror ``test_queue_dispatch.py``: the one actuation seat
(``campaign_run``) is faked at its source module and everything else is real —
a real intake ledger, a real ``queue-advance``, a real journal under
``tmp_path``. Nothing here opens a socket or touches a cluster.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, get_args
from unittest import mock

import pytest

import hpc_agent.ops.queue.dispatch as dispatch_mod
from hpc_agent._wire.queries.queue_advance import QueueAdvanceSpec, QueueHoldReason
from hpc_agent._wire.workflows.campaign_run import CampaignRunResult
from hpc_agent._wire.workflows.queue_dispatch import (
    QueueDispatchRefusal,
    QueueDispatchSpec,
)
from hpc_agent.ops.queue.advance import queue_advance
from hpc_agent.ops.queue.dispatch import queue_dispatch
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.queue_intake import append_intake_item, read_intake_items
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-07-29T12:00:00+00:00"

#: The one actuation seat the queue composes (D1), patched at its source module.
_RUN = "hpc_agent.ops.campaign_run.campaign_run"

#: alpha DECLARES ``constraints.max_concurrent_jobs: 2`` on purpose: it is the
#: only cap-shaped field in clusters.yaml, and eager submit's negative case is
#: that being far past it changes nothing (S11: the field means per-submission
#: wave grouping; reading it as a cross-run cap would invent a second meaning).
_CLUSTERS = """
alpha:
  scheduler: slurm
  host: alpha.edu
  user: me
  max_walltime_sec: 86400
  constraints:
    max_concurrent_jobs: 2
beta:
  scheduler: slurm
  host: beta.edu
  user: me
  max_walltime_sec: 86400
"""


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def _submit_spec(run_id: str) -> dict[str, Any]:
    return {
        "profile": "ml",
        "cluster": "alpha",
        "ssh_target": "me@alpha.edu",
        "remote_path": "/scratch/ml",
        "job_name": "ml_array",
        "run_id": run_id,
        "total_tasks": 1,
        "backend": "slurm",
        "script": ".hpc/templates/cpu_array.sh",
        "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
    }


def _enqueue(exp: Path, item_id: str, *, run_id: str, cmd_sha: str, **extra: Any) -> None:
    """One RESOLVED item, the §10.S3 refill shape: identity + inline submit spec."""
    record: dict[str, Any] = {
        "run_name": "ml",
        "run_id": run_id,
        "cmd_sha": cmd_sha,
        "cluster_pin": "alpha",
        "campaign_base": "study",
        "spec": _submit_spec(run_id),
    }
    record.update(extra)
    append_intake_item(exp, record=record, request_id=item_id)


def _busy(exp: Path, n: int = 3, campaign_id: str = "study_alpha") -> None:
    """*n* live runs on alpha — occupancy past the declared wave-grouping cap."""
    for i in range(n):
        upsert_run(
            exp,
            RunRecord(
                run_id=f"busy-{i}",
                profile="ml",
                cluster="alpha",
                ssh_target="me@alpha.edu",
                remote_path="/scratch/ml",
                job_name="ml_array",
                job_ids=[str(100 + i)],
                total_tasks=1,
                submitted_at="2026-07-29T00:00:00+00:00",
                experiment_dir=str(exp),
                status="in_flight",
                campaign_id=campaign_id,
            ),
        )


def _detached_for(experiment_dir: Path, *, spec: Any) -> CampaignRunResult:
    return CampaignRunResult(
        stage_reached="detached",
        needs_decision=False,
        reason="detached",
        run_id=spec.aggregate.run_id,
        started=True,
        watch="journal",
        detached_pid=4242,
    )


def _states(exp: Path) -> dict[str, str]:
    return {str(i.get("item_id")): str(i.get("state")) for i in read_intake_items(exp)}


# ── a busy cluster still gets the placement AND the start, same tick ────────


def test_a_busy_cluster_still_gets_the_dispatch_in_the_same_tick(tmp_path: Path) -> None:
    """§7 R3's core: busy-but-placeable means SUBMIT, never a capacity hold.

    alpha carries three live runs — past its ``max_concurrent_jobs: 2`` — and
    the gate-cleared item is started anyway, in the same ``queue-dispatch``
    tick that consumed the decision. The job's pending wait happens in the
    scheduler's queue, on its own content-pinned §10.S4 tree, not on our
    ledger. The disclosure stays honest: the busy-ness is PUBLISHED (occupancy
    is the least-loaded/etiquette input), but no reason string converts it
    into a hold or cites the wave-grouping field as a cap.
    """
    exp = _exp(tmp_path)
    _busy(exp, 3)
    _enqueue(exp, "item-1", run_id="ml-aaaa1111", cmd_sha="aaaa1111")

    with mock.patch(_RUN, side_effect=_detached_for) as m_run:
        result = queue_dispatch(experiment_dir=exp, spec=QueueDispatchSpec(now=_NOW))

    assert result.stage_reached == "dispatched"
    assert [row.outcome for row in result.dispatched] == ["started"]
    assert m_run.call_count == 1  # the SAME tick submitted — no deferral seat
    assert result.refused == []
    assert result.held == []
    # 3 live runs + the pinned queued item itself (its cid is fixed at enqueue).
    assert result.occupancy["study_alpha"] == 4
    assert _states(exp) == {"item-1": "placed"}
    blob = result.model_dump_json().lower()
    assert "no_capacity" not in blob
    assert "max_concurrent_jobs" not in blob
    assert "capacity" not in blob


def test_an_unattended_batch_submits_eagerly_against_a_busy_cluster(tmp_path: Path) -> None:
    """The wake tier's batch is N eager SUBMISSIONS, not N queue positions.

    Three gate-cleared items, a cluster already at 3 live runs: the declared
    unattended tier starts all three lifecycles in one tick. Under §7 R3 the
    batch bound (``max_dispatches`` / the wake edge's constant) is a
    submissions-per-tick storm bound — nothing between placement and start
    consults the cluster's business.
    """
    exp = _exp(tmp_path)
    _busy(exp, 3)
    _enqueue(exp, "item-1", run_id="ml-aaaa1111", cmd_sha="aaaa1111")
    _enqueue(exp, "item-2", run_id="ml-bbbb2222", cmd_sha="bbbb2222")
    _enqueue(exp, "item-3", run_id="ml-cccc3333", cmd_sha="cccc3333")

    with mock.patch(_RUN, side_effect=_detached_for) as m_run:
        result = queue_dispatch(
            experiment_dir=exp,
            spec=QueueDispatchSpec(now=_NOW, tier="unattended", max_dispatches=3),
        )

    assert result.stage_reached == "dispatched"
    assert m_run.call_count == 3
    assert sorted(row.run_id for row in result.dispatched) == [
        "ml-aaaa1111",
        "ml-bbbb2222",
        "ml-cccc3333",
    ]
    assert result.held == []
    assert _states(exp) == {"item-1": "placed", "item-2": "placed", "item-3": "placed"}


# ── the vocabularies are closed and capacity-free ────────────────────────────


def test_the_hold_vocabularies_are_exactly_the_closed_sets_and_capacity_free() -> None:
    """Pin the vocabularies EXACT, so a capacity code cannot reappear silently.

    Every ``QueueHoldback``/``DispatchRefusal`` validates its ``reason_code``
    against these Literals, so this one assertion is the enforcement seat: a
    producer emitting ``no_capacity`` fails pydantic validation, and WIDENING
    the vocabulary must edit this test by name. ``courtesy_cap_reached`` is
    the reserved etiquette code — a courtesy hold names courtesy, never
    capacity — and ``batch_limit_reached`` is a caller bound, the pre-gate
    one-per-y throttle, not a headroom guess.
    """
    assert set(get_args(QueueHoldReason)) == {
        "no_clusters_configured",
        "cluster_pin_unknown",
        "no_cluster_matches_constraints",
        "courtesy_cap_reached",
        "item_unresolved",
        "batch_limit_reached",
    }
    assert set(get_args(QueueDispatchRefusal)) == {
        "claim_held",
        "item_unresolved",
        "resolve_blocked",
        "cluster_unresolvable",
        "gate_refused",
        "lifecycle_failed",
    }
    every_code = set(get_args(QueueHoldReason)) | set(get_args(QueueDispatchRefusal))
    assert not any("capacity" in code for code in every_code)


def test_a_constraint_hold_names_the_constraint_never_capacity(
    tmp_path: Path,
) -> None:
    """The one legitimate non-etiquette hold is a HARD mismatch, said as such.

    An item asking more walltime than any cluster's declared ceiling is held
    ``no_cluster_matches_constraints`` with the configured field in the
    verdict — the busy-ness of the cluster plays no part and the word
    'capacity' appears nowhere in the disclosure.
    """
    exp = _exp(tmp_path)
    _busy(exp, 3)
    _enqueue(
        exp,
        "item-1",
        run_id="ml-aaaa1111",
        cmd_sha="aaaa1111",
        cluster_pin=None,
        resources={"walltime_sec": 999999},
    )

    result = queue_advance(experiment_dir=exp, spec=QueueAdvanceSpec(now=_NOW))

    assert result.decision == "hold"
    (hold,) = result.held
    assert hold.reason_code == "no_cluster_matches_constraints"
    assert all(not v.eligible for v in hold.considered)
    assert any("max_walltime_sec" in v.reason for v in hold.considered)
    blob = result.model_dump_json().lower()
    assert "capacity" not in blob
    assert "no_capacity" not in blob


# ── the actor structurally cannot defer on occupancy ─────────────────────────


def test_the_actor_has_no_deferral_seat_between_placement_and_start() -> None:
    """Source-pinned (the ``occupied_slots`` inspection precedent): dispatch
    never reads the occupancy predicate, so a capacity wait between consuming
    the decision and starting the lifecycle has no seat to grow from. The
    occupancy it PUBLISHES is echoed verbatim off ``queue-advance``'s result —
    disclosure, not a gate."""
    source = inspect.getsource(dispatch_mod)
    assert "occupied_slots" not in source
    assert "no_capacity" not in source
    # The only actuation seat is the composed lifecycle, started directly.
    assert "campaign_run(experiment_dir, spec=crspec)" in source
