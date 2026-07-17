"""L2 (Row 21): the detached worker parks ITSELF at its decision terminal.

Two seats, one definition (the scope_gate/notebook_gate blessed pattern): the ONE
definition of "write the §5 pending-decision marker at a parked boundary" is
``block_drive._park``. Seat 1 is the driver tick; seat 2 is the detached worker's
own exit (``submit_blocks._worker_exit_park``). Before L2 a run detached at
S2/S3/S4 needed an EXTRA driver tick to notice the recorded terminal and park;
now the worker leaves the marker itself.

  * import-location contract: seat 2 ROUTES THROUGH ``_park`` and never re-inlines
    ``mark_pending_decision`` (the fork this pin catches).
  * behavior: a detached worker's terminal leaves BOTH the pending-decision marker
    (no driver tick) AND the block-terminal record (the wake payload).
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from hpc_agent._wire.workflows.submit_blocks import SubmitBlockResult

if TYPE_CHECKING:
    from pathlib import Path


# ── import-location contract (one definition, two seats) ────────────────────────


def test_park_has_exactly_one_definition_in_block_drive() -> None:
    from hpc_agent._kernel.lifecycle import block_drive

    assert hasattr(block_drive, "_park"), "the ONE marker-writing definition lives here"


def test_worker_exit_park_routes_through_the_one_definition() -> None:
    from hpc_agent.ops import submit_blocks

    src = inspect.getsource(submit_blocks._worker_exit_park)
    assert "_park" in src, "seat 2 must route through block_drive._park"
    assert "mark_pending_decision" not in src, (
        "seat 2 must NOT re-inline the marker write — it routes through _park"
    )


# ── behavior: the worker parks itself + records its terminal ────────────────────


def _mk_run_record(experiment_dir: Path, run_id: str) -> None:
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        experiment_dir,
        RunRecord(
            run_id=run_id,
            profile="prof",
            cluster="hoffman2",
            ssh_target="u@h",
            remote_path="/scratch/r",
            job_name="job",
            job_ids=["1"],
            total_tasks=10,
            submitted_at="2026-07-16T00:00:00+00:00",
            experiment_dir=str(experiment_dir),
            status="in_flight",
        ),
    )


def _terminal_result(run_id: str) -> SubmitBlockResult:
    return SubmitBlockResult(
        block="s2",
        stage_reached="canary_verified",
        needs_decision=True,
        reason="canary green",
        run_id=run_id,
        brief={"run_id": run_id, "verified": True},
        next_block={"verb": "submit-s3", "why": "launch main", "spec_hint": {}},
    )


def test_detached_worker_parks_itself_at_its_terminal(tmp_path: Path, monkeypatch) -> None:
    from hpc_agent.ops import submit_blocks
    from hpc_agent.state.block_terminal import read_terminal_with_fallback
    from hpc_agent.state.journal import read_pending_decision

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("HPC_DETACHED_BLOCK", "submit-s2")
    monkeypatch.setenv("HPC_DETACHED_RUN_ID", "run-parkme")
    experiment_dir = tmp_path / "exp"
    experiment_dir.mkdir()
    _mk_run_record(experiment_dir, "run-parkme")

    submit_blocks._persist_brief(experiment_dir, _terminal_result("run-parkme"), input_spec={})

    # The §5 pending-decision marker is present — the doctor reads "parked", not
    # "stalled", and no driver tick was needed to write it.
    marker = read_pending_decision("run-parkme", experiment_dir=experiment_dir)
    assert marker, "the worker must leave a pending-decision marker at its terminal"
    assert marker["block"] == "submit-s2"
    assert marker["resume_cursor"]["next_verb"] == "submit-s3"
    # The terminal record is the durable wake payload wait-detached hands back.
    rec = read_terminal_with_fallback(experiment_dir, "run-parkme", "submit-s2")
    assert rec is not None and rec["result"]["brief"]["verified"] is True


def test_non_worker_call_does_not_park(tmp_path: Path, monkeypatch) -> None:
    """A direct (non-detached) call must NOT park — HPC_DETACHED_BLOCK unset gates
    it (the driver tick owns the park in that path)."""
    from hpc_agent.ops import submit_blocks
    from hpc_agent.state.journal import read_pending_decision

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("HPC_DETACHED_BLOCK", raising=False)
    experiment_dir = tmp_path / "exp"
    experiment_dir.mkdir()
    _mk_run_record(experiment_dir, "run-parkme")

    submit_blocks._persist_brief(experiment_dir, _terminal_result("run-parkme"), input_spec={})

    assert read_pending_decision("run-parkme", experiment_dir=experiment_dir) == {}
