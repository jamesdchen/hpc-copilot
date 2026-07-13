"""Tests for ``settle-run`` (run-12 finding 25, the human-directed settle).

``settle-run`` takes DIRECTED terminal evidence and runs the SAME machinery the
probe path runs: journal the evidence as a sign-off, ``mark_run`` to the terminal
status, and the transition-gated ``harvest_on_terminal``. These assert:

* the FIRES path (finding 25's exact requirement) — a directed settle of an
  in_flight run TRANSITIONS the journal status via ``mark_run`` AND fires
  ``harvest_on_terminal`` with the settled cause, and journals the sign-off with
  directed provenance;
* the transition gate — an idempotent re-settle of an already-terminal run does
  NOT re-fire the harvest;
* the load-bearing guards — a missing run, a NON-terminal status, and EMPTY
  evidence are each refused loudly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.settle_run import SettleRunInput
from hpc_agent.ops.settle_run import settle_run

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "exp-abcd1234"
# settle_run imports harvest_on_terminal lazily via ``from … import``, so the
# patch seam is the source module (the lazy import re-binds from it at call time).
_HARVEST_SEAM = "hpc_agent.ops.monitor.harvest_guard.harvest_on_terminal"


def _setup(tmp_path: Path, monkeypatch: Any, *, status: str = "in_flight") -> Path:
    """Lay down a journal RunRecord at *status*."""
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    upsert_run(
        tmp_path,
        RunRecord(
            run_id=_RUN_ID,
            profile="exp",
            cluster="hoffman2",
            ssh_target="me@hoffman2.idre.ucla.edu",
            remote_path="/scratch/me/exp",
            job_name="exp",
            job_ids=["42"],
            total_tasks=2700,
            submitted_at="2026-07-11T00:00:00+00:00",
            experiment_dir=str(tmp_path),
            status=status,
            backend="sge",
        ),
    )
    return tmp_path


# ── the FIRES path (finding 25's exact requirement) ───────────────────────────


def test_directed_settle_marks_terminal_and_harvests(tmp_path: Path, monkeypatch: Any) -> None:
    """A directed settle of an in_flight run: mark_run transitions the status AND
    harvest_on_terminal fires with the settled cause (finding 25 pins BOTH ran),
    and the sign-off is journaled with directed provenance + typed counts."""
    from hpc_agent.state.decision_journal import read_decisions
    from hpc_agent.state.journal import load_run

    _setup(tmp_path, monkeypatch)

    harvest_marker = {"harvested_at": "2026-07-11T01:00:00+00:00", "harvest_ok": True}
    with mock.patch(_HARVEST_SEAM, return_value=harvest_marker) as harvest:
        res = settle_run(
            tmp_path,
            spec=SettleRunInput(
                run_id=_RUN_ID,
                status="complete",
                evidence="foreground reporter RC=0 all-2700; result tree on disk",
                artifact_refs=["/scratch/me/exp/results"],
                task_counts={"complete": 2700, "failed": 0, "total": 2700},
            ),
        )

    # (c) harvest_on_terminal ACTUALLY RAN with the settled cause — the exact pin.
    harvest.assert_called_once()
    assert harvest.call_args.kwargs["terminal_cause"] == "complete"
    assert res.harvested is True
    assert res.harvest == harvest_marker

    # (b) mark_run ACTUALLY RAN — the journal status transitioned to terminal.
    rec = load_run(tmp_path, _RUN_ID)
    assert rec is not None
    assert rec.status == "complete"
    assert res.stage_reached == "settled"
    assert res.prior_status == "in_flight"
    # The typed counts the prose hand-edit lacked are recorded in last_status.
    assert rec.last_status["task_counts"] == {"complete": 2700, "failed": 0, "total": 2700}
    assert rec.last_status["verdict_source"] == "human_directed"

    # (a) the sign-off is journaled with directed provenance.
    decisions = read_decisions(tmp_path, "run", _RUN_ID)
    assert len(decisions) == 1
    assert decisions[0]["response"] == "y"
    assert decisions[0]["proposal"].startswith("foreground reporter RC=0")
    prov = decisions[0]["provenance"]
    assert prov["directed"] is True
    assert prov["kind"] == "human-directed-settle"
    assert res.decision_ts


def test_real_harvest_marker_is_written(tmp_path: Path, monkeypatch: Any) -> None:
    """End-to-end (no cluster): harvest_on_terminal runs for real via the injected
    aggregate seam and returns a marker — proving the SAME harvest machinery runs,
    not just that a mock was called."""
    _setup(tmp_path, monkeypatch)

    def _fake_aggregate(_exp: Path, _rid: str) -> Any:
        raise RuntimeError("no cluster in this test")  # harvest_on_terminal never raises

    res = settle_run(
        tmp_path,
        spec=SettleRunInput(run_id=_RUN_ID, status="complete", evidence="proven on disk"),
        _aggregate=_fake_aggregate,
        _sweep=lambda _rid: {},
    )
    assert res.harvested is True
    # harvest_on_terminal's marker shape (best-effort, loud) — it RAN.
    assert res.harvest["run_id"] == _RUN_ID
    assert res.harvest["terminal_cause"] == "complete"
    assert "harvested_at" in res.harvest


# ── the transition gate ───────────────────────────────────────────────────────


def test_already_terminal_resettle_does_not_reharvest(tmp_path: Path, monkeypatch: Any) -> None:
    """A re-settle of an already-``complete`` run records the sign-off but does NOT
    re-fire the harvest (the reconcile arm's transition gate)."""
    _setup(tmp_path, monkeypatch, status="complete")
    with mock.patch(_HARVEST_SEAM) as harvest:
        res = settle_run(
            tmp_path,
            spec=SettleRunInput(run_id=_RUN_ID, status="complete", evidence="already done"),
        )
    harvest.assert_not_called()
    assert res.harvested is False
    assert res.stage_reached == "already_terminal"


# ── the load-bearing guards ───────────────────────────────────────────────────


def test_missing_run_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    with pytest.raises(errors.SpecInvalid) as exc:
        settle_run(tmp_path, spec=SettleRunInput(run_id="nope", status="complete", evidence="x"))
    assert "no run record" in str(exc.value)


def test_non_terminal_status_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """The guard CAN fire: status is a free str at the wire, and a non-terminal one
    is refused by the verb (settle-run only sets a TERMINAL state)."""
    _setup(tmp_path, monkeypatch)
    with pytest.raises(errors.SpecInvalid) as exc:
        settle_run(tmp_path, spec=SettleRunInput(run_id=_RUN_ID, status="in_flight", evidence="x"))
    assert "not terminal" in str(exc.value)


def test_empty_evidence_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """An empty-evidence settle is the surgical status-flip this verb replaces."""
    _setup(tmp_path, monkeypatch)
    with pytest.raises(errors.SpecInvalid) as exc:
        settle_run(tmp_path, spec=SettleRunInput(run_id=_RUN_ID, status="complete", evidence="   "))
    assert "evidence is required" in str(exc.value)
