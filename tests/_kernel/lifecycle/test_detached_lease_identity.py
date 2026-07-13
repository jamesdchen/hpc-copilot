"""Lease host + start-time identity — the F43 fire paths.

A detached-worker lease historically stamped a BARE pid, so on an NFS-shared
journal home (``$HOME`` shared across login nodes) the pid-only liveness probe
(:func:`hpc_agent.infra.proc.pid_alive`, LOCAL-only) misjudged two ways:

(a) a live worker on ANOTHER login node reads as a dead LOCAL pid → the lease is
    reclaimed → a duplicate concurrent worker for the same run; and
(b) after a reboot the recorded pid is reused by a STRANGER → reads as a live
    holder → a permanent ``DetachedLeaseHeld`` wedge whose error text falsely
    promised self-heal.

The fix stamps ``host`` + ``create_time`` and teaches ``_guard_single_lease`` to
refuse a cross-host lease it cannot verify and to reclaim a same-host pid whose
start-time no longer matches (reuse). These pin the guard's fire path.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from hpc_agent._kernel.lifecycle import detached

if TYPE_CHECKING:
    from pathlib import Path


def _write_lease(detached_dir: Path, block: str, run_id: str, **fields: object) -> Path:
    detached_dir.mkdir(parents=True, exist_ok=True)
    lease_path = detached_dir / f"{block}-{run_id}.lease.json"
    lease_path.write_text(
        json.dumps({"run_id": run_id, "block": block, **fields}), encoding="utf-8"
    )
    return lease_path


def test_cross_host_lease_is_refused_not_reclaimed(tmp_path: Path, monkeypatch) -> None:
    """F43 (a): a lease stamped on a DIFFERENT host is refused, NOT reclaimed —
    even though the LOCAL pid probe says dead (which pre-fix would reclaim and
    spawn a duplicate racing a possibly-live remote worker)."""
    d = tmp_path / "_detached"
    monkeypatch.setattr(detached, "_current_host", lambda: "login1")
    monkeypatch.setattr(detached, "_pid_alive", lambda _pid: False)  # dead LOCALLY
    _write_lease(d, "submit-s2", "runA", pid=999, host="login2")

    with pytest.raises(detached.DetachedLeaseHeld, match="DIFFERENT host"):
        detached._guard_single_lease(d, "submit-s2", "runA")


def test_pid_reuse_after_reboot_is_reclaimed(tmp_path: Path, monkeypatch) -> None:
    """F43 (b): same host, pid ALIVE, but its start-time differs from the lease's
    — a reboot reused the pid for a stranger. The lease is stale/reclaimable, so
    the guard returns the path (relaunch heals) rather than wedging forever."""
    d = tmp_path / "_detached"
    monkeypatch.setattr(detached, "_current_host", lambda: "login1")
    monkeypatch.setattr(detached, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(detached, "_process_create_time", lambda _pid: 5000.0)  # stranger
    _write_lease(d, "submit-s2", "runB", pid=999, host="login1", create_time=1000.0)

    lease_path = detached._guard_single_lease(d, "submit-s2", "runB")
    assert lease_path.name == "submit-s2-runB.lease.json"  # reclaimed, no raise


def test_same_host_matching_create_time_still_refused(tmp_path: Path, monkeypatch) -> None:
    """The original holder is genuinely alive (host + start-time both match): the
    guard must STILL refuse the second launch — the proving-run-#2 protection is
    intact, not weakened by the new checks."""
    d = tmp_path / "_detached"
    monkeypatch.setattr(detached, "_current_host", lambda: "login1")
    monkeypatch.setattr(detached, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(detached, "_process_create_time", lambda _pid: 1000.0)  # matches
    _write_lease(d, "submit-s2", "runC", pid=999, host="login1", create_time=1000.0)

    with pytest.raises(detached.DetachedLeaseHeld, match="already owns"):
        detached._guard_single_lease(d, "submit-s2", "runC")


def test_legacy_lease_without_identity_falls_back_to_pid_liveness(
    tmp_path: Path, monkeypatch
) -> None:
    """Back-compat: a pre-F43 lease (no host / create_time) with a live pid is
    still refused exactly as before — the new fields are additive."""
    d = tmp_path / "_detached"
    monkeypatch.setattr(detached, "_current_host", lambda: "login1")
    monkeypatch.setattr(detached, "_pid_alive", lambda _pid: True)
    _write_lease(d, "submit-s2", "runD", pid=999)

    with pytest.raises(detached.DetachedLeaseHeld, match="already owns"):
        detached._guard_single_lease(d, "submit-s2", "runD")


def test_dead_same_host_pid_still_reclaims(tmp_path: Path, monkeypatch) -> None:
    """A crashed same-host worker (dead pid) remains reclaimable — a crash must
    never permanently block relaunch."""
    d = tmp_path / "_detached"
    monkeypatch.setattr(detached, "_current_host", lambda: "login1")
    monkeypatch.setattr(detached, "_pid_alive", lambda _pid: False)
    _write_lease(d, "submit-s2", "runE", pid=999, host="login1", create_time=1000.0)

    lease_path = detached._guard_single_lease(d, "submit-s2", "runE")
    assert lease_path.name == "submit-s2-runE.lease.json"


def test_process_create_time_reads_current_process_and_rejects_nonpositive() -> None:
    ct = detached._process_create_time(os.getpid())
    assert isinstance(ct, float) and ct > 0
    assert detached._process_create_time(-1) is None


def test_spawn_detached_stamps_host_into_lease(tmp_path: Path, monkeypatch) -> None:
    """The stamp side: a launched lease now records the host so a reader on
    another node can tell it is not locally verifiable."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setattr(detached, "_current_host", lambda: "login-stamp")

    class _FakePopen:
        def __init__(self, argv, **kw):
            self.pid = 4242

    monkeypatch.setattr(detached.subprocess, "Popen", lambda argv, **kw: _FakePopen(argv, **kw))
    monkeypatch.setattr(detached, "_pid_alive", lambda _pid: True)

    detached.launch_submit_block_detached(
        verb="submit-s2",
        experiment_dir=str(tmp_path / "exp"),
        spec={"submit": {"submit": {"run_id": "runF"}}, "detach": False},
        hpc_agent_bin="hpc-agent-stub",
    )
    lease = json.loads(
        (tmp_path / "journal" / "_detached" / "submit-s2-runF.lease.json").read_text(
            encoding="utf-8"
        )
    )
    assert lease["host"] == "login-stamp"
    assert lease["pid"] == 4242
