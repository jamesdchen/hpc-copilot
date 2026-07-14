"""Monitor-hook tests for the #240 resolve-and-recover live wiring.

At ``_is_terminal``'s ``FAILED`` seam the poll loop now also consults the
resolve-and-recover composite (the #234 deterministic resolver's live
wiring, stacked on the #315 / ``next-issues-5matvo`` composite). It runs
alongside the #299 auto-resume hook: auto-resume owns ``preempted``
clusters; resolve-and-recover deliberately SKIPS preempted, so the two
partition a FAILED tick without double-handling.

These tests patch ``maybe_resolve_and_recover`` (the composite itself is
covered in ``tests/ops/recover/test_resolve_and_recover_flow.py``) and
assert the loop's control flow:

* the composite IS consulted on a FAILED tick,
* a run WITHOUT ``auto_recover_on_failure`` is side-effect-free — the
  composite returns ``verdict_only`` clusters, no resubmit / no park, and
  the run still surfaces FAILED (behavior-neutral wiring),
* the outcome is surfaced into the monitor tick's ``actions`` log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent._kernel.contract.vocabulary import LifecycleState
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.ops import monitor_flow as monitor_flow_module
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.ops.resolve_and_recover_flow import (
    ClusterOutcome,
    ResolveAndRecoverOutcome,
)
from hpc_agent.state.journal import update_run_status, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260606-140000-ddd"


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
        "submitted_at": "2026-06-06T14:00:00+00:00",
        "experiment_dir": str(experiment_dir),
        "script": ".hpc/templates/cpu_array.sh",
        "backend": "slurm",
        "job_env": {"EXECUTOR": "x"},
        # auto-resume OFF so the auto-resume hook is skipped entirely and we
        # isolate the resolve-and-recover wiring.
        "auto_resume_on_kill": False,
    }
    base.update(overrides)
    rec = RunRecord(**base)
    upsert_run(experiment_dir, rec)
    return rec


def _status_record_status(experiment: Path, snapshots: list[dict[str, int]]):
    idx = {"n": 0}

    def _fake(experiment_dir: Path, run_id: str, **kwargs: Any) -> RunRecord:
        snap = snapshots[min(idx["n"], len(snapshots) - 1)]
        idx["n"] += 1
        return update_run_status(experiment_dir, run_id, last_status=dict(snap))

    return _fake


_FAILED_STATUS = {"complete": 2, "running": 0, "pending": 0, "failed": 2}
_COMPLETE_STATUS = {"complete": 4, "running": 0, "pending": 0, "failed": 0}


def _read_last_tick(experiment_dir: Path, run_id: str) -> dict[str, Any]:
    import json

    from hpc_agent.state.run_record import runs_dir

    path = runs_dir(experiment_dir) / f"{run_id}.monitor.jsonl"
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    return json.loads(lines[-1])


def test_resolve_and_recover_consulted_optout_is_side_effect_free(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opt-in OFF: the composite IS consulted, returns verdict-only clusters
    (no resubmit / no park), the run still surfaces FAILED, and the outcome
    is surfaced into the tick ``actions`` log."""
    _seed_record(experiment, auto_recover_on_failure=False)
    monkeypatch.setattr(
        monitor_flow_module, "record_status", _status_record_status(experiment, [_FAILED_STATUS])
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)

    calls: list[tuple[str, bool]] = []

    def _fake_recover(
        experiment_dir: Path, run_id: str, *, record: Any = None, **kwargs: Any
    ) -> ResolveAndRecoverOutcome:
        # Capture the run's opt-in so the test proves the live record (opt OFF)
        # flows through; the real composite would take no side effect for it.
        calls.append((run_id, bool(record.auto_recover_on_failure)))
        return ResolveAndRecoverOutcome(
            run_id,
            clusters=(
                ClusterOutcome(
                    fingerprint="fp1",
                    error_class="cuda_oom",
                    task_ids=(2, 3),
                    disposition="verdict_only",
                    decided_by="code",
                    reason="auto_recover_on_failure not enabled",
                    overrides={"mem_gb": 32},
                ),
            ),
            auto_recover_count=0,
        )

    monkeypatch.setattr(monitor_flow_module, "maybe_resolve_and_recover", _fake_recover)

    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=5,
        wall_clock_budget_seconds=10_000,
        auto_combine_waves=False,
    )
    result = monitor_flow(experiment, spec=spec, _sleep=lambda s: None, _now=lambda: 0.0)

    # (a) The composite was consulted during the FAILED tick — with the live
    #     opt-OFF record.
    assert calls == [(_RUN_ID, False)]
    # (b) Side-effect-free: the run still surfaces FAILED (no resubmit kept it
    #     alive, no state mutation revived it). The opt-out wiring is neutral.
    assert result.lifecycle_state == LifecycleState.FAILED
    # auto_recover_count stayed at its seeded 0 — no park / resubmit bumped it.
    refreshed = monitor_flow_module.load_run(experiment, _RUN_ID)
    assert refreshed is not None
    assert int(refreshed.auto_recover_count) == 0
    # (c) The outcome is surfaced into the monitor tick's actions log.
    tick = _read_last_tick(experiment, _RUN_ID)
    recover_actions = [a for a in tick.get("actions", []) if a.get("kind") == "resolve_and_recover"]
    assert len(recover_actions) == 1
    assert recover_actions[0]["clusters"][0]["disposition"] == "verdict_only"
    assert recover_actions[0]["clusters"][0]["error_class"] == "cuda_oom"


def test_resolve_and_recover_resubmit_keeps_polling_to_complete(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opt-in ON + code verdict under cap: the composite resubmits, the loop
    keeps polling (no FAILED return) and the resumed work reaches COMPLETE —
    mirroring the auto-resume "resume" branch."""
    _seed_record(experiment, auto_recover_on_failure=True, max_auto_recovers=2)
    monkeypatch.setattr(
        monitor_flow_module,
        "record_status",
        _status_record_status(experiment, [_FAILED_STATUS, _COMPLETE_STATUS]),
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)

    def _fake_recover(
        experiment_dir: Path, run_id: str, *, record: Any = None, **kwargs: Any
    ) -> ResolveAndRecoverOutcome:
        # Simulate a real auto-recover resubmit: extend job_ids + bump counter.
        update_run_status(
            experiment_dir,
            run_id,
            job_ids=[*record.job_ids, "9200"],
            auto_recover_count=record.auto_recover_count + 1,
        )
        return ResolveAndRecoverOutcome(
            run_id,
            clusters=(
                ClusterOutcome(
                    fingerprint="fp1",
                    error_class="cuda_oom",
                    task_ids=(2, 3),
                    disposition="resubmitted",
                    decided_by="code",
                    reason="cuda_oom: auto-recovered with refined overrides",
                    overrides={"mem_gb": 64},
                    new_job_ids=["9200"],
                ),
            ),
            auto_recover_count=1,
        )

    monkeypatch.setattr(monitor_flow_module, "maybe_resolve_and_recover", _fake_recover)

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

    assert result.lifecycle_state == LifecycleState.COMPLETE
    # The spec's 5s is lifted to the connection-pacing floor (#3, default
    # 10s) — assert against the floored value, not the raw request.
    assert sleeps == [monitor_flow_module._floor_poll_interval(5)]


def test_resolve_and_recover_held_surfaces_failed_with_reason(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opt-in ON + judgement verdict: the composite parks (held). The run
    surfaces FAILED enriched with the held reason via escalation-as-data."""
    _seed_record(experiment, auto_recover_on_failure=True)
    monkeypatch.setattr(
        monitor_flow_module, "record_status", _status_record_status(experiment, [_FAILED_STATUS])
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)

    def _fake_recover(*a: Any, **k: Any) -> ResolveAndRecoverOutcome:
        return ResolveAndRecoverOutcome(
            _RUN_ID,
            clusters=(
                ClusterOutcome(
                    fingerprint="fp9",
                    error_class="unknown_error",
                    task_ids=(0,),
                    disposition="held",
                    decided_by="judgement",
                    reason="no deterministic fix; escalated for judgement",
                ),
            ),
        )

    monkeypatch.setattr(monitor_flow_module, "maybe_resolve_and_recover", _fake_recover)

    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=5,
        wall_clock_budget_seconds=10_000,
        auto_combine_waves=False,
    )
    result = monitor_flow(experiment, spec=spec, _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.FAILED
    assert result.escalation_reason is not None
    assert "auto_recover_held" in result.escalation_reason
    assert "unknown_error" in result.escalation_reason
