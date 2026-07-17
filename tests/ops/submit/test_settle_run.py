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
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path, harvest_receipt_exists
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


# ── the receipt-gated harvest: transition OR terminal-with-no-receipt backstop ──


def _write_receipt(tmp_path: Path, run_id: str = _RUN_ID) -> None:
    """Lay down a durable harvest receipt (mirrors ``harvest_on_terminal``'s last
    step) so the journal-evidence backstop reads the harvest as already performed."""
    append_jsonl_line(
        harvest_marker_path(tmp_path, run_id),
        {"run_id": run_id, "terminal_cause": "complete", "harvest_ok": True},
    )


def test_already_terminal_with_receipt_does_not_reharvest(tmp_path: Path, monkeypatch: Any) -> None:
    """A re-settle of an already-``complete`` run whose harvest receipt is already on
    the ledger records the sign-off but does NOT re-fire the harvest — no transition,
    receipt present, so the backstop is satisfied (idempotent no-op)."""
    _setup(tmp_path, monkeypatch, status="complete")
    _write_receipt(tmp_path)  # the guaranteed harvest already ran for this run.
    with mock.patch(_HARVEST_SEAM) as harvest:
        res = settle_run(
            tmp_path,
            spec=SettleRunInput(run_id=_RUN_ID, status="complete", evidence="already done"),
        )
    harvest.assert_not_called()
    assert res.harvested is False
    assert res.stage_reached == "already_terminal"


# ── the journal-evidence backstop (the sibling of reconcile's _harvest_if_owed) ─


def test_terminal_with_no_receipt_backstops_harvest_exactly_once(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """THE GAP: a run already ``complete`` (mark landed) but with NO harvest receipt
    (a session-death between ``mark_run`` and the harvest dropped it) MUST re-fire the
    guaranteed harvest on a directed re-settle — NOT solely on an in-process transition
    — and a SECOND re-settle (receipt now present) must NOT re-pull. Idempotent both
    ways, keyed off durable journal evidence, exactly like reconcile's ``_harvest_if_owed``.
    """
    _setup(tmp_path, monkeypatch, status="complete")
    assert not harvest_receipt_exists(tmp_path, _RUN_ID)

    def _fake_aggregate(_exp: Path, _rid: str) -> Any:
        raise RuntimeError("no cluster in this test")  # harvest_on_terminal never raises

    # First re-settle: no transition (already complete) but NO receipt → the backstop
    # fires the guaranteed harvest exactly once and it writes its durable receipt.
    res1 = settle_run(
        tmp_path,
        spec=SettleRunInput(run_id=_RUN_ID, status="complete", evidence="proven on disk"),
        _aggregate=_fake_aggregate,
        _sweep=lambda _rid: {},
    )
    assert res1.harvested is True, "terminal-with-no-receipt must re-fire the harvest"
    assert res1.stage_reached == "harvest_backstopped"
    assert res1.harvest["run_id"] == _RUN_ID
    assert res1.harvest["terminal_cause"] == "complete"
    assert harvest_receipt_exists(tmp_path, _RUN_ID)

    # Second re-settle: still no transition, but the receipt now exists → no re-pull.
    with mock.patch(_HARVEST_SEAM) as harvest:
        res2 = settle_run(
            tmp_path,
            spec=SettleRunInput(run_id=_RUN_ID, status="complete", evidence="proven on disk"),
        )
    harvest.assert_not_called()
    assert res2.harvested is False
    assert res2.stage_reached == "already_terminal"


def test_transition_settle_then_resettle_is_idempotent(tmp_path: Path, monkeypatch: Any) -> None:
    """The normal path is unchanged and self-consistent: an ``in_flight`` → ``complete``
    directed settle harvests once (writing its receipt), and a re-settle of the now
    already-``complete`` run does NOT re-fire (receipt present)."""
    _setup(tmp_path, monkeypatch)

    def _fake_aggregate(_exp: Path, _rid: str) -> Any:
        raise RuntimeError("no cluster in this test")

    res1 = settle_run(
        tmp_path,
        spec=SettleRunInput(run_id=_RUN_ID, status="complete", evidence="reporter RC=0"),
        _aggregate=_fake_aggregate,
        _sweep=lambda _rid: {},
    )
    assert res1.harvested is True
    assert res1.stage_reached == "settled"
    assert harvest_receipt_exists(tmp_path, _RUN_ID)

    with mock.patch(_HARVEST_SEAM) as harvest:
        res2 = settle_run(
            tmp_path,
            spec=SettleRunInput(run_id=_RUN_ID, status="complete", evidence="reporter RC=0"),
        )
    harvest.assert_not_called()
    assert res2.harvested is False
    assert res2.stage_reached == "already_terminal"


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
