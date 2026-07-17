"""``wait-detached`` — the harness-notification bridge (proving-run-3 finding b).

Deterministic: the lease dir is a tmp path (monkeypatched homedir) and pid
liveness is a scripted stub — no real processes, no sleeps beyond tiny
intervals.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent._kernel.lifecycle import detached as detached_mod
from hpc_agent._wire.queries.wait_detached import WaitDetachedInput
from hpc_agent.ops.monitor.wait_detached import wait_detached

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def homedir(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    (tmp_path / "_detached").mkdir()
    return tmp_path


def _write_lease(homedir: Path, *, block: str, run_id: str, pid: int) -> None:
    lease = {
        "run_id": run_id,
        "block": block,
        "pid": pid,
        "log_path": str(homedir / "_detached" / f"{block}-{run_id}.log"),
        "argv": ["x"],
    }
    (homedir / "_detached" / f"{block}-{run_id}.lease.json").write_text(
        json.dumps(lease), encoding="utf-8"
    )


def test_no_live_worker_returns_immediately(homedir) -> None:
    out = wait_detached(spec=WaitDetachedInput(run_id="run-abc"))
    assert out.outcome == "no_live_worker"
    assert out.waited_sec == 0.0


def test_dead_pid_lease_is_no_live_worker(homedir, monkeypatch) -> None:
    _write_lease(homedir, block="submit-s2", run_id="run-abc", pid=4242)
    monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: False)
    out = wait_detached(spec=WaitDetachedInput(run_id="run-abc"))
    assert out.outcome == "no_live_worker"


def test_worker_exit_is_observed(homedir, monkeypatch) -> None:
    """Alive for two probes, then dead → worker_exited with the lease's block."""
    _write_lease(homedir, block="submit-s2", run_id="run-abc", pid=4242)
    probes = iter([True, True, False])
    monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: next(probes, False))
    out = wait_detached(
        spec=WaitDetachedInput(run_id="run-abc", timeout_sec=30, poll_interval_sec=0.01)
    )
    assert out.outcome == "worker_exited"
    assert out.block == "submit-s2"
    assert out.pid == 4242
    assert out.log_path and out.log_path.endswith("submit-s2-run-abc.log")


def test_timeout_when_worker_outlives_budget(homedir, monkeypatch) -> None:
    _write_lease(homedir, block="submit-s3", run_id="run-abc", pid=77)
    monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: True)
    out = wait_detached(
        spec=WaitDetachedInput(run_id="run-abc", timeout_sec=0.05, poll_interval_sec=0.01)
    )
    assert out.outcome == "timeout"
    assert out.pid == 77
    assert out.waited_sec >= 0.05


def test_block_filter_selects_the_named_worker(homedir, monkeypatch) -> None:
    _write_lease(homedir, block="submit-s2", run_id="run-abc", pid=1)
    _write_lease(homedir, block="submit-s3", run_id="run-abc", pid=2)
    monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: pid == 2)
    out = wait_detached(
        spec=WaitDetachedInput(
            run_id="run-abc", block="submit-s3", timeout_sec=0.05, poll_interval_sec=0.01
        )
    )
    assert out.outcome == "timeout"  # pid 2 stays alive; the filter found it
    assert out.block == "submit-s3"


def test_corrupt_lease_is_skipped_not_fatal(homedir, monkeypatch) -> None:
    (homedir / "_detached" / "submit-s2-run-abc.lease.json").write_text(
        "{not json", encoding="utf-8"
    )
    out = wait_detached(spec=WaitDetachedInput(run_id="run-abc"))
    assert out.outcome == "no_live_worker"


# ── L2 wake payload: the worker parks itself; wait-detached hands back the brief ──


def _write_lease_with_ed(
    homedir: Path, experiment_dir: Path, *, block: str, run_id: str, pid: int
) -> None:
    lease = {
        "run_id": run_id,
        "block": block,
        "pid": pid,
        "log_path": str(homedir / "_detached" / f"{block}-{run_id}.log"),
        "experiment_dir": str(experiment_dir),
        "argv": ["x"],
    }
    (homedir / "_detached" / f"{block}-{run_id}.lease.json").write_text(
        json.dumps(lease), encoding="utf-8"
    )


def _record_worker_terminal(experiment_dir: Path, *, run_id: str, block: str) -> None:
    from hpc_agent.state.block_terminal import record_terminal

    record_terminal(
        experiment_dir,
        run_id=run_id,
        block=block,
        cmd_sha="",
        result_dump={
            "block": "s2",
            "stage_reached": "canary_verified",
            "needs_decision": True,
            "relay": "canary green, est. 4 core-hours",
            "run_id": run_id,
            "brief": {"run_id": run_id, "verified": True, "est_core_hours": 4},
            "next_block": {"verb": "submit-s3", "why": "launch main", "spec_hint": {}},
        },
    )


def test_worker_exited_hands_back_brief_relay_next_verb(homedir, tmp_path, monkeypatch) -> None:
    experiment_dir = tmp_path / "exp"
    experiment_dir.mkdir()
    _record_worker_terminal(experiment_dir, run_id="run-abc", block="submit-s2")
    _write_lease_with_ed(homedir, experiment_dir, block="submit-s2", run_id="run-abc", pid=4242)
    probes = iter([True, False])
    monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: next(probes, False))
    out = wait_detached(
        spec=WaitDetachedInput(run_id="run-abc", timeout_sec=30, poll_interval_sec=0.01)
    )
    assert out.outcome == "worker_exited"
    assert out.relay == "canary green, est. 4 core-hours"
    assert out.next_verb == "submit-s3"
    assert out.brief is not None and out.brief["verified"] is True


def test_no_live_worker_still_reads_the_exited_workers_terminal(
    homedir, tmp_path, monkeypatch
) -> None:
    """The worker already exited (dead-pid lease) — its recorded terminal is still
    the wake payload, so an agent that arrives late is not stranded."""
    experiment_dir = tmp_path / "exp"
    experiment_dir.mkdir()
    _record_worker_terminal(experiment_dir, run_id="run-abc", block="submit-s2")
    _write_lease_with_ed(homedir, experiment_dir, block="submit-s2", run_id="run-abc", pid=4242)
    monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: False)
    out = wait_detached(spec=WaitDetachedInput(run_id="run-abc"))
    assert out.outcome == "no_live_worker"
    assert out.next_verb == "submit-s3"
    assert out.brief is not None and out.brief["est_core_hours"] == 4


def test_timeout_carries_no_wake_payload(homedir, tmp_path, monkeypatch) -> None:
    """A worker still alive at the budget recorded no terminal → payload stays None."""
    experiment_dir = tmp_path / "exp"
    experiment_dir.mkdir()
    _write_lease_with_ed(homedir, experiment_dir, block="submit-s3", run_id="run-abc", pid=77)
    monkeypatch.setattr(detached_mod, "pid_alive", lambda pid: True)
    out = wait_detached(
        spec=WaitDetachedInput(run_id="run-abc", timeout_sec=0.05, poll_interval_sec=0.01)
    )
    assert out.outcome == "timeout"
    assert out.brief is None and out.relay is None and out.next_verb is None
