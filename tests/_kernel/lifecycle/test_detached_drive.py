"""Deterministic detached drive mode (#connection-storm-4).

The default drive stays the ``claude -p --bare`` worker; ``--detached`` /
``HPC_AGENT_DRIVE=detached`` opts into running the lifecycle composite in a
DETACHED CLI subprocess (no LLM in the connection loop) and polling the journal
for the outcome. These tests pin:

* the journal-read poll helper is cluster-free and terminal-aware;
* the status-pipeline spec is built deterministically from the run fields;
* the CLI ``run`` path launches the detached runner (mocked Popen) and emits a
  ``mode=detached`` envelope with the run_id to poll;
* the mode is refused for unsupported shapes and falls through to the default
  worker when not selected.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

# ─── journal-read poll helper ──────────────────────────────────────────────


def _seed_record(experiment_dir: Path, run_id: str, status: str) -> None:
    """Write a minimal journal RunRecord with *status* via the real writer."""
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        experiment_dir,
        RunRecord(
            run_id=run_id,
            profile="p",
            cluster="c",
            ssh_target="user@host",
            remote_path="/remote",
            job_name="j",
            job_ids=["100"],
            total_tasks=4,
            submitted_at="2026-06-24T00:00:00Z",
            experiment_dir=str(experiment_dir),
            status=status,
        ),
    )


@pytest.fixture
def _journal(tmp_path, monkeypatch):
    """Redirect the journal home into tmp so reads/writes are hermetic."""
    home = tmp_path / "journal"
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(home))
    return tmp_path / "exp"


def test_read_run_status_missing_record_is_not_found(_journal):
    from hpc_agent.state.journal_poll import read_run_status

    snap = read_run_status(_journal, "ml-deadbeef")
    assert snap.found is False
    assert snap.status is None
    assert snap.terminal is False


def test_read_run_status_in_flight_is_not_terminal(_journal):
    from hpc_agent.state.journal_poll import read_run_status

    _seed_record(_journal, "ml-deadbeef", "in_flight")
    snap = read_run_status(_journal, "ml-deadbeef")
    assert snap.found is True
    assert snap.status == "in_flight"
    assert snap.terminal is False


@pytest.mark.parametrize("status", ["complete", "failed", "abandoned"])
def test_read_run_status_terminal_states(_journal, status):
    from hpc_agent.state.journal_poll import read_run_status

    _seed_record(_journal, "ml-deadbeef", status)
    snap = read_run_status(_journal, "ml-deadbeef")
    assert snap.terminal is True
    assert snap.status == status


def test_poll_until_terminal_returns_when_runner_writes_terminal(_journal):
    """The detached runner flips the journal to terminal mid-poll; the poller,
    driven by an injected clock, returns the terminal snapshot without sleeping
    on real time. The run STARTS in_flight and the runner finishes it on tick 2.
    """
    from hpc_agent.state.journal_poll import poll_until_terminal

    _seed_record(_journal, "ml-run", "in_flight")
    slept: list[float] = []
    ticks = {"n": 0}

    def fake_sleep(_secs):
        slept.append(_secs)
        ticks["n"] += 1
        if ticks["n"] == 2:  # the detached runner reaches terminal here
            _seed_record(_journal, "ml-run", "complete")

    snap = poll_until_terminal(
        _journal,
        "ml-run",
        poll_interval_seconds=30,
        timeout_seconds=10_000,
        sleep=fake_sleep,
        now=lambda: 0.0,  # never hits the deadline; terminal exits the loop
    )
    assert snap.terminal is True
    assert snap.status == "complete"
    assert slept == [30, 30]  # two sleeps before the terminal read on tick 3


def test_poll_until_terminal_gives_up_at_local_timeout(_journal):
    """A run that never finishes: the poller returns the (non-terminal) last
    snapshot once the LOCAL budget elapses — it never blocks forever, and the
    caller decides whether to re-arm the runner."""
    from hpc_agent.state.journal_poll import poll_until_terminal

    _seed_record(_journal, "ml-stuck", "in_flight")
    clock = {"t": 0.0}

    def fake_now():
        return clock["t"]

    def fake_sleep(secs):
        clock["t"] += secs

    snap = poll_until_terminal(
        _journal,
        "ml-stuck",
        poll_interval_seconds=30,
        timeout_seconds=90,
        sleep=fake_sleep,
        now=fake_now,
    )
    assert snap.terminal is False
    assert snap.status == "in_flight"


# ─── status-pipeline spec builder ──────────────────────────────────────────


def test_build_status_pipeline_spec_minimal():
    from hpc_agent._kernel.lifecycle.detached import build_status_pipeline_spec

    spec = build_status_pipeline_spec({"run_id": "ml-abcd1234", "blocking": True})
    assert spec == {"monitor": {"run_id": "ml-abcd1234"}}


def test_build_status_pipeline_spec_passes_through_monitor_fields():
    from hpc_agent._kernel.lifecycle.detached import build_status_pipeline_spec

    spec = build_status_pipeline_spec(
        {
            "run_id": "ml-abcd1234",
            "blocking": True,
            "poll_interval_seconds": 120,
            "wall_clock_budget_seconds": 7200,
            "file_glob": "metrics_*.json",
            "ignored_extra": "dropped",
        }
    )
    assert spec["monitor"]["run_id"] == "ml-abcd1234"
    assert spec["monitor"]["poll_interval_seconds"] == 120
    assert spec["monitor"]["wall_clock_budget_seconds"] == 7200
    assert spec["monitor"]["file_glob"] == "metrics_*.json"
    assert "ignored_extra" not in spec["monitor"]


def test_build_status_pipeline_spec_requires_run_id():
    from hpc_agent._kernel.lifecycle.detached import (
        DriveModeError,
        build_status_pipeline_spec,
    )

    with pytest.raises(DriveModeError, match="run_id"):
        build_status_pipeline_spec({"blocking": True})


def test_spec_round_trips_through_status_pipeline_model():
    """The dict the detached runner writes must validate as a real
    StatusPipelineSpec — otherwise the launched `hpc-agent status-pipeline`
    would reject its own spec at intake."""
    from hpc_agent._kernel.lifecycle.detached import build_status_pipeline_spec
    from hpc_agent._wire.workflows.status_pipeline import StatusPipelineSpec

    spec = build_status_pipeline_spec(
        {"run_id": "ml-abcd1234", "blocking": True, "poll_interval_seconds": 90}
    )
    model = StatusPipelineSpec.model_validate(spec)
    assert model.monitor.run_id == "ml-abcd1234"
    assert model.monitor.poll_interval_seconds == 90


# ─── support predicate ─────────────────────────────────────────────────────


def test_detached_supported_only_for_blocking_status():
    from hpc_agent._kernel.lifecycle.detached import detached_drive_supported

    assert detached_drive_supported("status", {"blocking": True, "run_id": "x"}) is True
    # snapshot status has no loop to drive
    assert detached_drive_supported("status", {"blocking": False, "run_id": "x"}) is False
    assert detached_drive_supported("status", {"run_id": "x"}) is False
    # other workflows keep the default worker
    assert detached_drive_supported("submit", {"blocking": True}) is False
    assert detached_drive_supported("aggregate", {}) is False


# ─── launch (mocked Popen) ─────────────────────────────────────────────────


class _FakePopen:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242


def test_launch_detached_writes_spec_and_detaches(_journal, monkeypatch):
    from hpc_agent._kernel.lifecycle import detached

    captured = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakePopen(argv, **kwargs)

    monkeypatch.setattr(detached.subprocess, "Popen", _fake_popen)

    launch = detached.launch_status_pipeline_detached(
        experiment_dir=str(_journal),
        fields={"run_id": "ml-launch1", "blocking": True},
        hpc_agent_bin="hpc-agent-stub",
    )

    assert launch.run_id == "ml-launch1"
    assert launch.pid == 4242
    # The launched argv runs the DETERMINISTIC composite, not a `claude -p` worker.
    assert captured["argv"][0] == "hpc-agent-stub"
    assert captured["argv"][1] == "status-pipeline"
    assert "--spec" in captured["argv"]
    assert "claude" not in " ".join(captured["argv"])
    # Detach flags present (platform-specific).
    kw = captured["kwargs"]
    assert ("start_new_session" in kw) or ("creationflags" in kw)
    # stdin is closed so a poll can never block on a tty.
    assert kw["stdin"] == detached.subprocess.DEVNULL
    # The spec file the runner reads validates as a real StatusPipelineSpec.
    spec_idx = captured["argv"].index("--spec") + 1
    spec_path = Path(captured["argv"][spec_idx])
    written = json.loads(spec_path.read_text(encoding="utf-8"))
    assert written == {"monitor": {"run_id": "ml-launch1"}}


# ─── CLI run wiring ────────────────────────────────────────────────────────


def _run_args(tmp_path, *, workflow="status", fields="{}", detached=False, inline=False):
    return argparse.Namespace(
        workflow=workflow,
        experiment_dir=tmp_path,
        fields_json=fields,
        detached=detached,
        inline=inline,
    )


def _envelope(capsys):
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])


def test_cli_detached_flag_launches_runner_and_emits_run_id(_journal, monkeypatch, capsys):
    from hpc_agent._kernel.lifecycle import detached
    from hpc_agent.cli import spawn

    monkeypatch.delenv("HPC_AGENT_DRIVE", raising=False)
    monkeypatch.setattr(detached.subprocess, "Popen", lambda argv, **kw: _FakePopen(argv, **kw))

    # The spawn path must NOT run — detached spawns no LLM.
    monkeypatch.setattr(
        "hpc_agent._kernel.lifecycle.run.run_workflow",
        lambda **_: (_ for _ in ()).throw(AssertionError("no worker on detached path")),
    )

    rc = spawn.cmd_run(
        _run_args(
            _journal, fields=json.dumps({"run_id": "ml-cli1", "blocking": True}), detached=True
        )
    )
    env = _envelope(capsys)
    assert rc == 0
    assert env["ok"] is True
    assert env["data"]["mode"] == "detached"
    assert env["data"]["run_id"] == "ml-cli1"
    assert "poll" in env["data"]["instructions"].lower()


def test_cli_detached_env_selects_mode(_journal, monkeypatch, capsys):
    from hpc_agent._kernel.lifecycle import detached
    from hpc_agent.cli import spawn

    monkeypatch.setenv("HPC_AGENT_DRIVE", "detached")
    monkeypatch.setattr(detached.subprocess, "Popen", lambda argv, **kw: _FakePopen(argv, **kw))

    rc = spawn.cmd_run(
        _run_args(_journal, fields=json.dumps({"run_id": "ml-cli2", "blocking": True}))
    )
    env = _envelope(capsys)
    assert rc == 0
    assert env["data"]["mode"] == "detached"
    assert env["data"]["run_id"] == "ml-cli2"


def test_cli_detached_refused_for_unsupported_workflow(_journal, monkeypatch, capsys):
    from hpc_agent.cli import spawn

    monkeypatch.delenv("HPC_AGENT_DRIVE", raising=False)
    rc = spawn.cmd_run(
        _run_args(_journal, workflow="submit", fields=json.dumps({"blocking": True}), detached=True)
    )
    env = _envelope(capsys)
    assert rc == 1
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
    assert "status" in env["message"]


def test_cli_detached_refused_for_snapshot_status(_journal, monkeypatch, capsys):
    from hpc_agent.cli import spawn

    monkeypatch.delenv("HPC_AGENT_DRIVE", raising=False)
    rc = spawn.cmd_run(
        _run_args(_journal, fields=json.dumps({"run_id": "x", "blocking": False}), detached=True)
    )
    env = _envelope(capsys)
    assert rc == 1
    assert env["error_code"] == "spec_invalid"


# ─── submit-block detached launch (design §3 detach-by-contract) ────────────


def _block_spec(run_id="ml-blk1", *, detach=False):
    """A submit-s2-shaped dict spec (submit.submit.run_id is the poll key)."""
    return {"submit": {"submit": {"run_id": run_id}}, "detach": detach}


def test_launch_submit_block_detached_writes_spec_and_detaches(_journal, monkeypatch):
    from hpc_agent._kernel.lifecycle import detached

    captured = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakePopen(argv, **kwargs)

    monkeypatch.setattr(detached.subprocess, "Popen", _fake_popen)

    launch = detached.launch_submit_block_detached(
        verb="submit-s2",
        experiment_dir=str(_journal),
        spec=_block_spec("ml-blk1", detach=False),
        hpc_agent_bin="hpc-agent-stub",
    )

    assert launch.run_id == "ml-blk1"
    assert launch.pid == 4242
    # The child runs the SAME verb (its detach forced off), NOT a claude worker.
    assert captured["argv"][0] == "hpc-agent-stub"
    assert captured["argv"][1] == "submit-s2"
    assert "--spec" in captured["argv"]
    assert "claude" not in " ".join(captured["argv"])
    kw = captured["kwargs"]
    assert ("start_new_session" in kw) or ("creationflags" in kw)
    assert kw["stdin"] == detached.subprocess.DEVNULL
    # The written spec has detach=False so the child never re-detaches.
    spec_idx = captured["argv"].index("--spec") + 1
    written = json.loads(Path(captured["argv"][spec_idx]).read_text(encoding="utf-8"))
    assert written["detach"] is False


def test_launch_submit_block_refuses_unsupported_verb(_journal):
    from hpc_agent._kernel.lifecycle.detached import DriveModeError, launch_submit_block_detached

    with pytest.raises(DriveModeError, match="only supported"):
        launch_submit_block_detached(
            verb="submit-s1", experiment_dir=str(_journal), spec=_block_spec()
        )


def test_launch_submit_block_refuses_truthy_detach(_journal):
    """A spec still carrying detach=True would fork forever — refuse it."""
    from hpc_agent._kernel.lifecycle.detached import DriveModeError, launch_submit_block_detached

    with pytest.raises(DriveModeError, match="detach=False"):
        launch_submit_block_detached(
            verb="submit-s2", experiment_dir=str(_journal), spec=_block_spec(detach=True)
        )


def test_launch_submit_block_requires_run_id(_journal):
    from hpc_agent._kernel.lifecycle.detached import DriveModeError, launch_submit_block_detached

    with pytest.raises(DriveModeError, match="run_id"):
        launch_submit_block_detached(
            verb="submit-s2", experiment_dir=str(_journal), spec={"submit": {"submit": {}}}
        )


def test_cli_default_still_spawns_worker(_journal, monkeypatch, capsys):
    """No flag, no env → the default `claude -p` worker path, unchanged."""
    import types

    from hpc_agent.cli import spawn

    monkeypatch.delenv("HPC_AGENT_DRIVE", raising=False)
    report = types.SimpleNamespace(model_dump=lambda: {"result": "ok"})
    monkeypatch.setattr(
        "hpc_agent._kernel.lifecycle.run.run_workflow", lambda **_: (report, 0, None)
    )
    rc = spawn.cmd_run(_run_args(_journal, fields=json.dumps({"run_id": "x", "blocking": True})))
    env = _envelope(capsys)
    assert rc == 0
    assert env["data"]["mode"] == "spawn"
