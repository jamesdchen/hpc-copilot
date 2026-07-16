"""Poll-loop fault tolerance for ``monitor_flow`` (SSH#1).

A single transient poll fault — a reporter rc!=0 (``RemoteCommandFailed``) or a
``TimeoutError`` (an ``OSError`` subclass) after the backoff window — must NOT
abort a healthy multi-hour poll. The per-tick ``record_status`` call is wrapped
so the loop swallows the blip, notes it on a tick, and continues to the next
poll; only a poller that keeps failing PAST the wall-clock budget terminates —
to ``TIMEOUT`` with the guaranteed harvest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._kernel.contract.vocabulary import LifecycleState
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.ops import monitor_flow as monitor_flow_module
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.state.journal import update_run_status, upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260606-130000-ccc"
_COMPLETE_STATUS = {"complete": 4, "running": 0, "pending": 0, "failed": 0}


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
    }
    base.update(overrides)
    rec = RunRecord(**base)
    upsert_run(experiment_dir, rec)
    return rec


def _record_status_sequence(experiment: Path, seq: list[Any]):
    """A record_status stub that walks *seq*: a dict is persisted as
    ``last_status`` and returned; an Exception is raised (a transient fault).
    The last item repeats once the sequence is exhausted."""
    idx = {"n": 0}

    def _fake(experiment_dir: Path, run_id: str, **kwargs: Any) -> RunRecord:
        item = seq[min(idx["n"], len(seq) - 1)]
        idx["n"] += 1
        if isinstance(item, BaseException):
            raise item
        return update_run_status(experiment_dir, run_id, last_status=dict(item))

    return _fake


def test_record_status_activation_seeded_from_record_cluster(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run #7 sibling of the b1b05f7d verify_canary fix, on the MONITOR caller.

    The MAIN-array monitor's per-run sidecar carries neither ``env`` nor
    ``cluster``, so ``record_status`` would derive ``""`` activation → the
    reporter runs bare login-node python → ``import hpc_agent`` fails → rc=127
    every poll, which the monitor rides as "transient" for the whole budget
    while a finished array sits unread. The journal record's cluster is seeded
    into the sidecar so the reporter activates conda.
    """
    from hpc_agent.ops.monitor import status as status_mod
    from hpc_agent.state.runs import write_run_sidecar

    _seed_record(experiment, cluster="hoffman2", backend="sge")
    # The BARE sidecar shape written live: no cluster, no env.
    write_run_sidecar(
        experiment,
        run_id=_RUN_ID,
        cmd_sha="",
        hpc_agent_version="",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{task_id}",
        task_count=4,
        tasks_py_sha="",
    )
    # Hermetic clusters.yaml (the c158f797 lesson): CI's packaged placeholder has
    # a conda_source but no conda_envs, so assert against a written fixture.
    clusters = experiment / "clusters_fixture.yaml"
    clusters.write_text(
        "hoffman2:\n  host: h.example\n  user: u\n  scratch: /s\n  scheduler: sge\n"
        "  conda_source: /apps/conda/etc/profile.d/conda.sh\n  conda_envs: [hpc-pi]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(clusters))

    captured: dict[str, object] = {}

    def _fake_report(**kwargs: Any) -> dict[str, Any]:
        captured["remote_activation"] = kwargs.get("remote_activation")
        return {"summary": {"complete": 4, "running": 0, "pending": 0, "failed": 0}}

    monkeypatch.setattr(status_mod, "_ssh_status_report", _fake_report)

    status_mod.record_status(
        experiment,
        _RUN_ID,
        ssh_target="user@host",
        remote_path="/remote",
        job_ids=["9001"],
        job_name="myjob",
    )

    # Cluster-derived activation, NOT the bare-python "" fallthrough (→ rc=127).
    assert captured["remote_activation"]
    assert "conda activate hpc-pi" in str(captured["remote_activation"])


def test_deterministic_env_failure_escalates_to_reporter_unreachable(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run #7: a reporter rc=127 (broken login-node env) fails EVERY poll the
    same way and never heals by waiting, so the monitor escalates FAST to a loud
    reporter-unreachable TIMEOUT after _DETERMINISTIC_ENV_POLLS_TO_FAIL — instead
    of riding the whole budget as "transient" (the S3 watch rode 28+ ticks of
    rc=127 while a finished 20-task array sat unread)."""
    _seed_record(experiment)
    rc127 = errors.RemoteCommandFailed("status reporter failed (rc=127)", returncode=127)
    monkeypatch.setattr(
        monitor_flow_module,
        "record_status",
        _record_status_sequence(experiment, [rc127]),  # every poll fails rc=127
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    monkeypatch.setattr(monitor_flow_module, "harvest_on_terminal", lambda *a, **k: None)

    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=5,
        wall_clock_budget_seconds=10_000,  # huge — the ONLY exit is the env escalation
        auto_combine_waves=False,
    )
    # _now pinned to 0 → never over budget; escalation is the sole terminator.
    result = monitor_flow(experiment, spec=spec, _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.TIMEOUT
    assert result.escalation_reason and "UNREACHABLE" in result.escalation_reason
    # Escalated at the threshold, NOT after the whole budget of polls.
    assert result.ticks == monitor_flow_module._DETERMINISTIC_ENV_POLLS_TO_FAIL


@pytest.mark.parametrize(
    "transient",
    [errors.RemoteCommandFailed("reporter rc=1"), TimeoutError("ssh timed out")],
    ids=["remote_command_failed", "timeout_error"],
)
def test_single_transient_does_not_abort_poll(
    journal_home: Path,
    experiment: Path,
    monkeypatch: pytest.MonkeyPatch,
    transient: BaseException,
) -> None:
    """A single transient poll fault is swallowed; the loop continues to the
    next poll and reaches COMPLETE (and still harvests)."""
    _seed_record(experiment)
    monkeypatch.setattr(
        monitor_flow_module,
        "record_status",
        _record_status_sequence(experiment, [transient, _COMPLETE_STATUS]),
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    harvested: list[str] = []
    monkeypatch.setattr(
        monitor_flow_module,
        "harvest_on_terminal",
        lambda *a, **k: harvested.append(k.get("terminal_cause", "?")),
    )

    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=5,
        wall_clock_budget_seconds=10_000,
        auto_combine_waves=False,
    )
    result = monitor_flow(experiment, spec=spec, _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert harvested == ["complete"]


def test_ssh_circuit_open_is_transient_survives_to_complete(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding #15: a transient network blip that opens the per-host SSH circuit
    breaker raises ``SshCircuitOpen`` — an ``HpcError``, NOT a
    ``RemoteCommandFailed`` / ``OSError`` — so the original poll-tolerance clause
    let it propagate and kill the detached multi-hour watch (run #7: a 3×60s
    hoffman2 latency spike). It must be classed transient: the loop waits out the
    breaker cooldown (≪ the watch budget) and rides on, so a blip that clears
    reaches COMPLETE with the guaranteed harvest."""
    _seed_record(experiment)
    breaker = errors.SshCircuitOpen("circuit open for host — cooldown until ...")
    breaker.host = "host"
    breaker.deadline = 0.0  # already past → _circuit_wait_sec returns the slack floor
    monkeypatch.setattr(
        monitor_flow_module,
        "record_status",
        _record_status_sequence(experiment, [breaker, _COMPLETE_STATUS]),
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    harvested: list[str] = []
    monkeypatch.setattr(
        monitor_flow_module,
        "harvest_on_terminal",
        lambda *a, **k: harvested.append(k.get("terminal_cause", "?")),
    )

    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=5,
        wall_clock_budget_seconds=10_000,
        auto_combine_waves=False,
    )
    result = monitor_flow(experiment, spec=spec, _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert harvested == ["complete"]


def test_degraded_preamble_breaker_surfaces_host_retarget_on_the_tick(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run-13 finding 10: when the per-host breaker is livelocking on a degraded
    module/conda preamble (a cheap probe keeps closing it), the monitor's
    transient-fault tick must carry the classification + the host-retarget/
    settle-run remedy — not just ride the breaker forever."""
    import json
    import time as _time

    from hpc_agent.infra.ssh_circuit import circuit_state_path
    from hpc_agent.ops.monitor.tick_log import _tick_log_path

    _seed_record(experiment, ssh_target="user@dead.host")
    # Seed a breaker doc for dead.host showing the degradation signal. The
    # breaker (like production) is keyed on the wall clock, and the monitor
    # reads the advice with the real time.time(), so the incident window must be
    # anchored to real time — not the monitor's pinned virtual _now.
    now = _time.time()
    path = circuit_state_path("dead.host")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "host": "dead.host",
                "state": "open",
                "consecutive_failures": 4,
                "cooldown_sec": 300.0,
                "opened_at": now,
                "probe_claimed_at": None,
                "last_failure": {
                    "at": now,
                    "detail": "ssh to user@dead.host timed out after 60s: "
                    "cd /x && module load conda && source /apps/conda/conda.sh && python run.py",
                },
                "reopen_cycles": 2,
                "incident_started_at": now,
            }
        ),
        encoding="utf-8",
    )
    breaker = errors.SshCircuitOpen("circuit open for dead.host")
    breaker.host = "dead.host"
    breaker.deadline = 0.0  # already past → the loop rides on
    monkeypatch.setattr(
        monitor_flow_module,
        "record_status",
        _record_status_sequence(experiment, [breaker, _COMPLETE_STATUS]),
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    monkeypatch.setattr(monitor_flow_module, "harvest_on_terminal", lambda *a, **k: None)

    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=5,
        wall_clock_budget_seconds=10_000,
        auto_combine_waves=False,
    )
    result = monitor_flow(experiment, spec=spec, _sleep=lambda s: None, _now=lambda: 0.0)
    assert result.lifecycle_state == LifecycleState.COMPLETE

    ticks = [
        json.loads(line)
        for line in _tick_log_path(experiment, _RUN_ID).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    degradation_notes = [
        a["degradation"]
        for t in ticks
        for a in t.get("actions", [])
        if isinstance(a, dict) and a.get("degradation")
    ]
    assert degradation_notes, "the degraded-preamble breaker tick carried no degradation note"
    note = degradation_notes[0]
    assert "conda activation" in note
    assert "2 cycles" in note
    assert "host-retarget" in note or "settle-run" in note


def test_nonconsecutive_env_blips_reset_and_do_not_escalate(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F27: a successful poll RESETS the deterministic-env failure streak, so
    NON-consecutive rc-127 blips across a long watch can no longer accumulate to
    the threshold and kill a healthy monitor. Three rc-127 faults interleaved
    with clean polls (poll 1 fails, 2 ok, 3 fails, 4 ok, 5 fails, 6 complete)
    must NOT reach ``_DETERMINISTIC_ENV_POLLS_TO_FAIL`` consecutive — the run
    reaches COMPLETE, not a spurious reporter-UNREACHABLE TIMEOUT."""
    _seed_record(experiment)
    rc127 = errors.RemoteCommandFailed("status reporter failed (rc=127)", returncode=127)
    in_flight = {"complete": 0, "running": 4, "pending": 0, "failed": 0}
    seq: list[Any] = [rc127, in_flight, rc127, in_flight, rc127, _COMPLETE_STATUS]
    monkeypatch.setattr(
        monitor_flow_module,
        "record_status",
        _record_status_sequence(experiment, seq),
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    harvested: list[str] = []
    monkeypatch.setattr(
        monitor_flow_module,
        "harvest_on_terminal",
        lambda *a, **k: harvested.append(k.get("terminal_cause", "?")),
    )

    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=5,
        wall_clock_budget_seconds=10_000,
        auto_combine_waves=False,
    )
    # _now pinned to 0 → never over budget; only an (erroneous) env escalation or
    # the real COMPLETE can terminate.
    result = monitor_flow(experiment, spec=spec, _sleep=lambda s: None, _now=lambda: 0.0)

    assert result.lifecycle_state == LifecycleState.COMPLETE
    assert result.escalation_reason is None  # never escalated reporter-unreachable
    assert harvested == ["complete"]


def test_repeated_transient_terminates_to_timeout(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poller that keeps failing does NOT spin forever — it stays bounded by
    the wall-clock budget and terminates to TIMEOUT with a guaranteed harvest."""
    _seed_record(experiment)
    monkeypatch.setattr(
        monitor_flow_module,
        "record_status",
        _record_status_sequence(experiment, [errors.RemoteCommandFailed("always down")]),
    )
    monkeypatch.setattr(monitor_flow_module, "_ingest_runtime_at_terminal", lambda *a, **k: 0)
    harvested: list[str] = []
    monkeypatch.setattr(
        monitor_flow_module,
        "harvest_on_terminal",
        lambda *a, **k: harvested.append(k.get("terminal_cause", "?")),
    )

    # Virtual clock: _now reads it, _sleep advances it, so repeated failed polls
    # cross the budget in finite iterations.
    clock = {"t": 0.0}
    spec = MonitorFlowSpec(
        run_id=_RUN_ID,
        poll_interval_seconds=5,
        wall_clock_budget_seconds=10,
        auto_combine_waves=False,
    )
    result = monitor_flow(
        experiment,
        spec=spec,
        _sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        _now=lambda: clock["t"],
    )

    assert result.lifecycle_state == LifecycleState.TIMEOUT
    assert harvested == ["cap-overrun"]
