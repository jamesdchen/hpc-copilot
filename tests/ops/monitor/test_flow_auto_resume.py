"""Monitor-hook tests for the #299 auto-resume auto-fire.

At ``_is_terminal``'s ``FAILED`` seam the poll loop now consults the
auto-resume composite when the run opted in (``auto_resume_on_kill``).
These tests patch ``maybe_auto_resume`` (the composite itself is covered
in ``tests/ops/recover/test_auto_resume_flow.py``) and assert the loop's
control flow:

* opt-in OFF → unchanged behavior: surface ``FAILED``.
* opt-in ON + composite says "resume" → keep polling (no terminal
  ``FAILED`` return), the resumed run can then reach ``COMPLETE``.
* opt-in ON + composite says "escalate" → surface ``FAILED`` carrying the
  escalation reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent._kernel.contract.vocabulary import LifecycleState
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.ops import auto_resume_flow as auto_resume_module
from hpc_agent.ops import monitor_flow as monitor_flow_module
from hpc_agent.ops.auto_resume_flow import AutoResumeOutcome
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.state import run_record
from hpc_agent.state.journal import update_run_status, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260606-130000-ccc"


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed_record(experiment_dir: Path, **overrides: Any) -> RunRecord:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@host",
        "remote_path": "/remote",
        "job_name": "myjob",
        "job_ids": ["9001"],
        "total_tasks": 4,
        "submitted_at": "2026-06-06T13:00:00+00:00",
        "experiment_dir": str(experiment_dir),
        "script": ".hpc/templates/cpu_array.sh",
        "backend": "slurm",
        "job_env": {"EXECUTOR": "x"},
        "auto_resume_on_kill": True,
        "max_auto_resumes": 2,
    }
    base.update(overrides)
    rec = RunRecord(**base)
    upsert_run(experiment_dir, rec)
    return rec


def _status_record_status(experiment: Path, snapshots: list[dict[str, int]]):
    """Build a record_status stub that walks *snapshots*, persisting each as
    ``last_status`` so the returned record keeps every other field."""
    idx = {"n": 0}

    def _fake(experiment_dir: Path, run_id: str, **kwargs: Any) -> RunRecord:
        snap = snapshots[min(idx["n"], len(snapshots) - 1)]
        idx["n"] += 1
        return update_run_status(experiment_dir, run_id, last_status=dict(snap))

    return _fake


_FAILED_STATUS = {"complete": 2, "running": 0, "pending": 0, "failed": 2}
_COMPLETE_STATUS = {"complete": 4, "running": 0, "pending": 0, "failed": 0}


def test_opt_out_surfaces_failed_unchanged(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_record(experiment, auto_resume_on_kill=False)
    monkeypatch.setattr(
        monitor_flow_module, "record_status", _status_record_status(experiment, [_FAILED_STATUS])
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)

    # The composite must NOT even be consulted when the run opted out.
    def _boom(*a: Any, **k: Any) -> AutoResumeOutcome:  # pragma: no cover - must not run
        raise AssertionError("maybe_auto_resume called for an opt-out run")

    monkeypatch.setattr(auto_resume_module, "maybe_auto_resume", _boom)

    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=5,
        wall_clock_budget_seconds=10_000,
        auto_combine_waves=False,
    )
    result = monitor_flow(experiment, spec=spec, _sleep=lambda s: None, _now=lambda: 0.0)
    assert result.lifecycle_state == LifecycleState.FAILED
    assert result.escalation_reason == "failed_tasks_no_auto_recover_in_mvp"


def test_resume_verdict_keeps_polling_to_complete(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opt-in ON: FAILED → composite resumes → loop keeps polling → COMPLETE."""
    _seed_record(experiment)
    # poll 1: FAILED (triggers auto-resume); poll 2: COMPLETE (resumed work done).
    monkeypatch.setattr(
        monitor_flow_module,
        "record_status",
        _status_record_status(experiment, [_FAILED_STATUS, _COMPLETE_STATUS]),
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)

    calls: list[str] = []

    def _fake_resume(
        experiment_dir: Path, run_id: str, *, record: Any = None, **kwargs: Any
    ) -> AutoResumeOutcome:
        calls.append(run_id)
        # Simulate a real resume: extend job_ids + bump the cap counter so the
        # monitor's reload picks up live state.
        update_run_status(
            experiment_dir,
            run_id,
            job_ids=[*record.job_ids, "9100"],
            auto_resume_count=record.auto_resume_count + 1,
        )
        return AutoResumeOutcome(
            "resume",
            "preempted tasks present and under resume cap",
            task_ids=(0, 1),
            resubmitted=True,
            new_job_ids=["9100"],
            auto_resume_count=1,
        )

    monkeypatch.setattr(auto_resume_module, "maybe_auto_resume", _fake_resume)

    sleeps: list[float] = []
    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=5,
        wall_clock_budget_seconds=10_000,
        auto_combine_waves=False,
    )
    result = monitor_flow(
        experiment, spec=spec, _sleep=lambda s: sleeps.append(s), _now=lambda: 0.0
    )

    # The run did NOT surface FAILED — it auto-resumed and then completed.
    assert result.lifecycle_state == LifecycleState.COMPLETE
    # The composite was consulted exactly once (the single FAILED tick).
    assert calls == [_RUN_ID]
    # We slept once (between the resume continue and the completing poll).
    # The spec's 5s is lifted to the connection-pacing floor (#3, default
    # 10s) — the floored value is what the loop actually sleeps.
    assert sleeps == [monitor_flow_module._floor_poll_interval(5)]


def test_escalate_verdict_surfaces_failed_with_reason(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_record(experiment, max_auto_resumes=2, auto_resume_count=2)
    monkeypatch.setattr(
        monitor_flow_module, "record_status", _status_record_status(experiment, [_FAILED_STATUS])
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)

    def _fake_resume(*a: Any, **k: Any) -> AutoResumeOutcome:
        return AutoResumeOutcome("escalate", "auto-resume cap reached (2/2)", task_ids=(0,))

    monkeypatch.setattr(auto_resume_module, "maybe_auto_resume", _fake_resume)

    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=5,
        wall_clock_budget_seconds=10_000,
        auto_combine_waves=False,
    )
    result = monitor_flow(experiment, spec=spec, _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.FAILED
    # The escalation reason replaces the generic MVP reason.
    assert result.escalation_reason == "auto-resume cap reached (2/2)"
