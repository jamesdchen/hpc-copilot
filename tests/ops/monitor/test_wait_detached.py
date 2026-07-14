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
