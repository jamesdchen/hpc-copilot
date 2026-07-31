"""The S3 watch leg when the DEPLOYED combiner is absent (U5 / F8).

U5's guard made ``combine_waves`` RAISE ``errors.CombinerMissing`` where it
previously returned an ordinary non-zero combine. That changes mid-flight watch
behaviour, so the behaviour was CHOSEN rather than inherited, and this module
is where the choice is pinned.

Three options were available inside the poll loop:

1. **let it propagate** — the watch dies at the first wave. Rejected: the array
   is still running, and killing the watch throws away the monitoring, the
   harvest and the tick ledger for a fault that ``redeploy-runtime`` repairs
   *while the array runs*.
2. **degrade through ``combiner_max_retries``** like any combine failure.
   Rejected: ``CombinerMissing.retry_safe`` is False by construction — N more
   ticks against an unchanged cause is precisely the waste the 2026-07-30
   evening consisted of.
3. **give up on combining immediately, keep watching, escalate with the
   remediation** — chosen, and pinned below.

The consequence that matters for the recovery path: the waves are NOT recorded
in ``failed_waves``. A wave that never ran is not a wave that failed, and the
``combiner_failed`` menu it would route to says "resubmit the offending tasks"
— which repairs nothing when the artifact is the thing that is missing.

Harness mirrors ``test_flow_wave_prefetch.py`` (the same census-leg fixtures
the wave bookkeeping rides).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._kernel.contract.vocabulary import LifecycleState
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.ops import monitor_flow as monitor_flow_module
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.recovery.registry import remediation_for
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260730-200000-miss"


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed_record(experiment_dir: Path) -> RunRecord:
    rec = RunRecord(
        run_id=_RUN_ID,
        profile="p",
        cluster="c",
        ssh_target="user@host",
        remote_path="/remote",
        job_name="myjob",
        job_ids=["9001"],
        total_tasks=4,
        submitted_at="2026-07-30T18:00:00+00:00",
        experiment_dir=str(experiment_dir),
        backend="sge",
        auto_resume_on_kill=False,
    )
    upsert_run(experiment_dir, rec)
    return rec


def _write_wave_sidecar(experiment_dir: Path) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=_RUN_ID,
        cmd_sha="0" * 64,
        hpc_agent_version="0.11.4",
        submitted_at="2026-07-30T18:00:00Z",
        executor="python3 run.py",
        result_dir_template="results/{task_id}",
        task_count=4,
        tasks_py_sha="1" * 64,
        wave_map={"0": [0, 1], "1": [2, 3]},
    )


def _present(complete: int, failed: int = 0, total: int = 4) -> dict[str, int]:
    announced = complete + failed
    return {
        "present": 1,
        "announced": announced,
        "complete": complete,
        "failed": failed,
        "missing": max(0, total - announced),
    }


def _stub_census(monkeypatch: pytest.MonkeyPatch, *census_seq: dict[str, int]) -> None:
    idx = {"n": 0}

    def _fake(*, ssh_target: str, remote_path: str, run_id: str, task_count: int) -> dict[str, int]:
        item = census_seq[min(idx["n"], len(census_seq) - 1)]
        idx["n"] += 1
        return dict(item)

    monkeypatch.setattr(monitor_flow_module, "read_announcements", _fake)


def _stub_complete_ids(monkeypatch: pytest.MonkeyPatch, *id_seq: set[int]) -> None:
    idx = {"n": 0}

    def _fake(record: Any, run_id: str) -> set[int]:
        item = id_seq[min(idx["n"], len(id_seq) - 1)]
        idx["n"] += 1
        return set(item)

    monkeypatch.setattr(monitor_flow_module, "_census_complete_task_ids", _fake)


def _raising_combine(monkeypatch: pytest.MonkeyPatch) -> list[list[int]]:
    """``combine_waves`` that always reports the artifact absent."""
    calls: list[list[int]] = []

    def _fake(experiment_dir: Path, run_id: str, *, waves: list[int], **_kw: Any) -> dict[str, Any]:
        calls.append(list(waves))
        raise errors.CombinerMissing(
            "the deployed combiner .hpc/_hpc_combiner.py is absent at user@host:/remote",
            remediation=remediation_for("combiner_missing"),
        )

    monkeypatch.setattr(monitor_flow_module, "combine_waves", _fake)
    return calls


def _harvest_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        monitor_flow_module,
        "harvest_on_terminal",
        lambda *a, **k: calls.append(k.get("terminal_cause", "?")),
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    return calls


def _spec(**overrides: Any) -> MonitorFlowSpec:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "poll_interval_seconds": 5,  # the spec's floor
        "wall_clock_budget_seconds": 10_000,
        "auto_combine_waves": True,
        "combiner_max_retries": 3,
    }
    base.update(overrides)
    return MonitorFlowSpec(**base)


def _actions(experiment_dir: Path, kind: str) -> list[dict[str, Any]]:
    from hpc_agent.ops.monitor.tick_log import _tick_log_path

    rows: list[dict[str, Any]] = []
    log = _tick_log_path(experiment_dir, _RUN_ID)
    if not log.is_file():
        return rows
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for action in json.loads(line).get("actions") or []:
            if isinstance(action, dict) and action.get("kind") == kind:
                rows.append(action)
    return rows


def test_the_watch_survives_and_escalates_with_the_redeploy_command(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Option 3, end to end: the run still reaches COMPLETE and harvests, and
    the human gets the redeploy command on ``escalation_reason``."""
    _seed_record(experiment)
    _write_wave_sidecar(experiment)
    _stub_census(monkeypatch, _present(4))
    _stub_complete_ids(monkeypatch, {0, 1, 2, 3})
    _raising_combine(monkeypatch)
    harvests = _harvest_recorder(monkeypatch)

    result = monitor_flow(experiment, spec=_spec())

    assert result.lifecycle_state == LifecycleState.COMPLETE, "the watch must not be aborted"
    assert harvests, "the harvest must still run — the results are on the cluster"
    assert result.escalation_reason is not None
    assert result.escalation_reason.startswith("combiner_missing:")
    assert "hpc-agent redeploy-runtime" in result.escalation_reason


def test_a_wave_that_never_ran_is_not_reported_as_a_failed_wave(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``failed_waves`` drives the ``combiner_failed`` menu — resubmit the
    tasks — which is the wrong repair for a missing artifact."""
    _seed_record(experiment)
    _write_wave_sidecar(experiment)
    _stub_census(monkeypatch, _present(4))
    _stub_complete_ids(monkeypatch, {0, 1, 2, 3})
    _raising_combine(monkeypatch)
    _harvest_recorder(monkeypatch)

    result = monitor_flow(experiment, spec=_spec())

    assert result.failed_waves == []
    assert result.combined_waves == []


def test_the_dropout_is_disclosed_on_the_tick_log_with_its_remediation(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick ledger is the durable record of what the watch saw."""
    _seed_record(experiment)
    _write_wave_sidecar(experiment)
    _stub_census(monkeypatch, _present(4))
    _stub_complete_ids(monkeypatch, {0, 1, 2, 3})
    _raising_combine(monkeypatch)
    _harvest_recorder(monkeypatch)

    monitor_flow(experiment, spec=_spec())

    rows = _actions(experiment, "combiner_missing")
    assert rows, "the dropout must be disclosed on the tick log"
    assert sorted(rows[0]["waves"]) == [0, 1]
    assert "hpc-agent redeploy-runtime" in rows[0]["remediation"]
    # And NOT as an ordinary per-wave combine failure.
    assert _actions(experiment, "combine_wave_failed") == []


def test_it_gives_up_immediately_instead_of_burning_the_retry_budget(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``retry_safe=False``: one exec, not ``combiner_max_retries`` + 1.

    Two ticks are driven (the second finds the same complete waves) to prove
    the give-up sentinel keeps holding — a later tick must not re-attempt a
    wave whose artifact is still missing.
    """
    _seed_record(experiment)
    _write_wave_sidecar(experiment)
    _stub_census(monkeypatch, _present(2), _present(4))
    _stub_complete_ids(monkeypatch, {0, 1}, {0, 1, 2, 3})
    calls = _raising_combine(monkeypatch)
    _harvest_recorder(monkeypatch)

    monitor_flow(experiment, spec=_spec(combiner_max_retries=3))

    # Wave 0/1 is attempted exactly once. Whatever later ticks do, they never
    # re-attempt a wave already past the give-up sentinel.
    assert calls, "the first burst must be attempted"
    attempted_first_burst = [c for c in calls if 0 in c]
    assert len(attempted_first_burst) == 1, (
        f"wave 0 was re-attempted {len(attempted_first_burst)} times against an "
        "unchanged cause; combiner_max_retries must not apply to a deploy dropout"
    )


def test_a_healthy_combine_is_completely_unaffected(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new arm is inert when the artifact is present."""
    _seed_record(experiment)
    _write_wave_sidecar(experiment)
    _stub_census(monkeypatch, _present(4))
    _stub_complete_ids(monkeypatch, {0, 1, 2, 3})
    monkeypatch.setattr(
        monitor_flow_module,
        "combine_waves",
        lambda _d, _r, *, waves, **_kw: {w: (True, "", "") for w in waves},
    )
    _harvest_recorder(monkeypatch)

    result = monitor_flow(experiment, spec=_spec())

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert result.escalation_reason is None
    assert sorted(result.combined_waves) == [0, 1]
    assert result.failed_waves == []
