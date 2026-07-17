"""Submit-once runner atom — the locked compare-and-mint, mint ordering, promote,
and the ``submitting`` -> ``_RECONCILE`` front-door routing (U3-b, premortem
Δ1/Δ4/§3.3).

Pins, red-then-green against the pre-U3 tree:

* the Δ1 concurrency fire: two genuinely concurrent same-run_id mints serialize —
  exactly one mints a ``submitting`` record, the other is routed to reconcile and
  refuses (no second dispatch, no duplicate array);
* mint-before-dispatch ordering: a mint that is never promoted (a kill in the
  dispatch window) leaves a durable ``submitting`` record with EMPTY ``job_ids``
  — a state reconcile can own — never "no record at all";
* the ruled ``attempt`` allocation (``max(record.attempt, jobmap.attempt)+1``);
* ``_resolve_layer1`` routes an existing ``submitting`` record to ``_RECONCILE``
  (not the leaky ``_DEDUP``), and ``submit_and_record`` refuses it loudly.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.ops.submit.runner import (
    _DEDUP,
    _PROCEED,
    _RECONCILE,
    _resolve_layer1,
    allocate_attempt,
    mint_submitting_record,
    promote_submitting_record,
    submit_and_record,
)
from hpc_agent.state import run_record
from hpc_agent.state.index import find_submitting_runs
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path


def _record(
    *,
    status: str,
    run_id: str = "run-abc12345",
    attempt: int = 0,
    job_ids: list[str] | None = None,
    cluster: str = "hoffman2",
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="slurm",
        cluster=cluster,
        ssh_target="user@host",
        remote_path="/home/u/demo",
        job_name="job",
        job_ids=job_ids if job_ids is not None else [],
        total_tasks=10,
        submitted_at="2026-01-01T00:00:00Z",
        experiment_dir="/e",
        status=status,
        attempt=attempt,
    )


def _mint(
    exp: Path,
    run_id: str,
    *,
    cluster: str = "hoffman2",
    total_tasks: int = 10,
    jobmap_attempt: int = 0,
) -> tuple[RunRecord, bool]:
    return mint_submitting_record(
        exp,
        run_id=run_id,
        profile="slurm",
        cluster=cluster,
        ssh_target="user@host",
        remote_path="/home/u/demo",
        job_name="job",
        total_tasks=total_tasks,
        jobmap_attempt=jobmap_attempt,
    )


# ── allocate_attempt (the single ruled allocation path, Δ1/A4) ────────────────


def test_allocate_attempt_first_submit_is_zero() -> None:
    assert allocate_attempt(None) == 0


def test_allocate_attempt_redo_is_prior_plus_one() -> None:
    assert allocate_attempt(_record(status="failed", attempt=0)) == 1
    assert allocate_attempt(_record(status="failed", attempt=3)) == 4


def test_allocate_attempt_folds_in_jobmap_attempt() -> None:
    # A jobmap that raced AHEAD of the journal still forces a strictly-newer
    # attempt (the recovery path passes the marker's attempt).
    assert allocate_attempt(_record(status="failed", attempt=1), jobmap_attempt=5) == 6


# ── _resolve_layer1 submitting -> _RECONCILE (front-door routing) ─────────────


def test_resolve_layer1_submitting_routes_to_reconcile() -> None:
    d = _resolve_layer1(
        _record(status="submitting"),
        invalidate_on_code_change=False,
        current_executor=None,
        current_tasks_py_sha=None,
        current_cluster="hoffman2",
    )
    assert d.action == _RECONCILE
    assert d.reason == "submitting_route_to_reconcile"


def test_resolve_layer1_submitting_not_dedup_or_proceed() -> None:
    # The leaky pre-U3 path treated any non-complete, non-terminal record as
    # _DEDUP; a submitting record must NOT dedup (nor proceed to a blind redo).
    d = _resolve_layer1(
        _record(status="submitting"),
        invalidate_on_code_change=False,
        current_executor=None,
        current_tasks_py_sha=None,
        current_cluster="hoffman2",
    )
    assert d.action not in (_DEDUP, _PROCEED)


def test_submit_and_record_refuses_submitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    exp = tmp_path / "exp"
    exp.mkdir()
    upsert_run(exp, _record(status="submitting"))
    from hpc_agent._wire.actions.submit import SubmitSpec

    spec = SubmitSpec(
        profile="slurm",
        cluster="hoffman2",
        ssh_target="user@host",
        remote_path="/home/u/demo",
        job_name="job",
        run_id="run-abc12345",
        job_ids=["12345"],
        total_tasks=10,
    )
    with pytest.raises(errors.SpecInvalid) as ei:
        submit_and_record(exp, spec=spec)
    assert "submitting" in str(ei.value)
    assert "reconcile" in str(ei.value).lower()


# ── mint-before-dispatch ordering + promote (§3.3) ────────────────────────────


def test_mint_lands_submitting_with_empty_job_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    exp = tmp_path / "exp"
    exp.mkdir()
    rec, minted = _mint(exp, "run-mint")
    assert minted is True
    assert rec.status == "submitting"
    assert rec.job_ids == []
    assert rec.attempt == 0


def test_mint_without_promote_leaves_submitting_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill in the dispatch window (mint, then die before promote) leaves a
    durable ``submitting`` record with empty ``job_ids`` — never no record."""
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    exp = tmp_path / "exp"
    exp.mkdir()
    _mint(exp, "run-orphan")
    # ... process dies here, promote never runs ...
    on_disk = load_run(exp, "run-orphan")
    assert on_disk is not None
    assert on_disk.status == "submitting"
    assert on_disk.job_ids == []
    # surfaced to reconcile-recovery, NOT the monitor live set
    assert [r.run_id for r in find_submitting_runs(exp)] == ["run-orphan"]


def test_mint_stamps_initial_watchdog_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    exp = tmp_path / "exp"
    exp.mkdir()
    _mint(exp, "run-wd")
    rec = load_run(exp, "run-wd")
    # A driver that dies in the dispatch window lapses this deadline -> doctor.
    assert rec is not None and rec.next_tick_due is not None


def test_promote_transitions_to_in_flight_with_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    exp = tmp_path / "exp"
    exp.mkdir()
    _mint(exp, "run-p")
    rec = promote_submitting_record(exp, "run-p", ["98765"])
    assert rec.status == "in_flight"
    assert rec.job_ids == ["98765"]
    # and it is now in the monitor live set, out of the submitting set
    assert find_submitting_runs(exp) == []


def test_mint_over_in_flight_dedups_does_not_remint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    exp = tmp_path / "exp"
    exp.mkdir()
    upsert_run(exp, _record(status="in_flight", run_id="run-live", job_ids=["1"]))
    rec, minted = _mint(exp, "run-live")
    assert minted is False
    assert rec.status == "in_flight"


def test_mint_over_submitting_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    exp = tmp_path / "exp"
    exp.mkdir()
    _mint(exp, "run-dup")
    with pytest.raises(errors.SpecInvalid):
        _mint(exp, "run-dup")


def test_mint_over_resubmittable_terminal_bumps_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    exp = tmp_path / "exp"
    exp.mkdir()
    upsert_run(exp, _record(status="failed", run_id="run-redo", attempt=0))
    rec, minted = _mint(exp, "run-redo")
    assert minted is True
    assert rec.status == "submitting"
    assert rec.attempt == 1  # max(0, 0) + 1 — a stale attempt-0 marker can't adopt


# ── Δ1: the atomic compare-and-mint concurrency fire ──────────────────────────


def test_concurrent_same_run_id_mints_serialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two genuinely concurrent same-run_id submits: exactly ONE mints a
    ``submitting`` record; the other, acquiring the run lock only after the
    first's write is durable, reads the ``submitting`` record and refuses
    (routed to reconcile). Never two dispatches, never a contended attempt."""
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    exp = tmp_path / "exp"
    exp.mkdir()
    # Prime the namespace so both threads race only on the run lock, not on
    # journal-dir creation.
    from hpc_agent.state.run_record import journal_dir

    journal_dir(exp)

    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def worker(name: str) -> None:
        barrier.wait()
        try:
            _rec, minted = _mint(exp, "race")
            outcomes[name] = "minted" if minted else "dedup"
        except errors.SpecInvalid:
            outcomes[name] = "refused"

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes.values()) == ["minted", "refused"]
    # Exactly one durable submitting record — no duplicate.
    subs = find_submitting_runs(exp)
    assert len(subs) == 1
    assert subs[0].run_id == "race"
    assert subs[0].job_ids == []
