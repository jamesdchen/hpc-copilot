"""Canary RunRecord terminal transition.

A verified (or failed) canary must NOT linger ``in_flight`` in its RunRecord:
``verify-canary`` polls it to terminal on the cluster but is side-effect-free and
never closes the local record, so the §5 watchdog / ``doctor`` would false-flag a
green canary as a stalled driver and draft a spurious re-arm. ``submit_and_verify``
owns the canary lifecycle, so it closes the record once the verdict is known.

Surfaced by the first real Hoffman2 proving run (doctor flagged two verified
canaries as stalled).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest import mock

import pytest

import hpc_agent.ops.submit_and_verify as sav
from hpc_agent.state import run_record
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_MAIN = "rX"
_CANARY = "rX-canary"


@pytest.fixture(autouse=True)
def _single_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests assert the canary RunRecord terminal transition, not the
    determinism-fingerprint DOUBLE canary. Without the opt-out the verified-canary
    test would fire a real second canary; pin single-canary so it stays focused."""
    monkeypatch.setenv("HPC_NO_DOUBLE_CANARY", "1")


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _status(experiment: Path, run_id: str) -> str:
    rec = load_run(experiment, run_id)
    assert rec is not None, f"no run record for {run_id!r}"
    return rec.status


def _seed_canary(experiment: Path, status: str = "in_flight") -> None:
    upsert_run(
        experiment,
        RunRecord(
            run_id=_CANARY,
            profile="p",
            cluster="c",
            ssh_target="u@h",
            remote_path="/r",
            job_name="j",
            job_ids=["100"],
            total_tasks=1,
            submitted_at="2026-07-03T12:00:00+00:00",
            experiment_dir=str(experiment),
            status=status,
        ),
    )


# ── the helper directly ──────────────────────────────────────────────────────


def test_mark_canary_terminal_transitions_record(journal_home, experiment: Path) -> None:
    _seed_canary(experiment)
    assert _status(experiment, _CANARY) == "in_flight"
    sav._mark_canary_terminal(experiment, _CANARY, status="complete")
    assert _status(experiment, _CANARY) == "complete"


def test_mark_canary_terminal_benign_when_no_record(journal_home, experiment: Path) -> None:
    # deduped / cache-hit canary: no fresh record on disk. Must swallow the
    # FileNotFoundError, never raise (a bookkeeping stamp can't fail the gate).
    sav._mark_canary_terminal(experiment, "no-such-canary", status="complete")


def test_mark_canary_terminal_noop_on_none(journal_home, experiment: Path) -> None:
    sav._mark_canary_terminal(experiment, None, status="complete")


# ── wired through submit_and_verify ──────────────────────────────────────────


def _canary_submit(**over: object) -> SimpleNamespace:
    base: dict[str, object] = dict(
        run_id=_MAIN,
        total_tasks=4,
        deduped=False,
        canary_run_id=_CANARY,
        canary_job_ids=["100"],
        job_ids=[],
    )
    base.update(over)
    return SimpleNamespace(**base)


def _spec():
    from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifySpec
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

    flow = SubmitFlowSpec(
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/r",
        job_name="j",
        run_id=_MAIN,
        total_tasks=4,
        backend="sge",
        script="run.sh",
        job_env={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
        result_dir_template="results/{run_id}/task_{task_id}",
        canary=True,
    )
    return SubmitAndVerifySpec(submit=flow)


def test_verified_canary_marked_complete(journal_home, experiment: Path) -> None:
    _seed_canary(experiment)
    with (
        mock.patch.object(sav, "submit_flow", return_value=_canary_submit()),
        mock.patch.object(
            sav,
            "verify_canary",
            return_value={"ok": True, "failure_kind": None, "details": "ok", "stderr_tail": ""},
        ),
    ):
        res = sav.submit_and_verify(experiment, spec=_spec(), stop_after_canary=True)
    assert res.verified is True
    assert _status(experiment, _CANARY) == "complete"


def test_failed_canary_marked_failed(journal_home, experiment: Path) -> None:
    _seed_canary(experiment)
    with (
        mock.patch.object(sav, "submit_flow", return_value=_canary_submit()),
        mock.patch.object(
            sav,
            "verify_canary",
            return_value={
                "ok": False,
                "failure_kind": "nonzero_exit",
                "details": "boom",
                "stderr_tail": "err",
            },
        ),
    ):
        res = sav.submit_and_verify(experiment, spec=_spec(), stop_after_canary=True)
    assert res.verified is False
    assert _status(experiment, _CANARY) == "failed"
