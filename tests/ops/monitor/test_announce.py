"""Client-side reader for the crash-only per-task announcement markers.

Crash-only-monitoring Phase 1: the cluster-side dispatcher writes one
filename-state-encoded marker per task (``task_<id>.complete`` /
``task_<id>.failed``); ``read_announcements`` counts them per-state in ONE
bounded ssh exec and reports ``{announced, complete, failed, missing}`` vs the
run's task_count.
"""

from __future__ import annotations

import subprocess

import pytest

from hpc_agent import errors
from hpc_agent.execution.mapreduce import dispatch
from hpc_agent.ops.monitor import announce


def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_vocabulary_lockstep_with_standalone_dispatcher() -> None:
    # The standalone dispatcher ships without hpc_agent on the path, so the
    # marker vocabulary is duplicated. Pin the two copies equal.
    assert announce.ANNOUNCE_STATE_COMPLETE == dispatch._ANNOUNCE_STATE_COMPLETE
    assert announce.ANNOUNCE_STATE_FAILED == dispatch._ANNOUNCE_STATE_FAILED
    expected_subpath = f".hpc/{dispatch._ANNOUNCE_DIRNAME}"
    assert expected_subpath == announce.ANNOUNCE_SUBPATH


def test_full_announcement_counts_complete_and_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    out = "__HPC_ANNOUNCE_ACK__\ncomplete=8\nfailed=2\n"
    captured: dict[str, str] = {}

    def _fake_ssh(cmd: str, *, ssh_target: str, **_kw) -> subprocess.CompletedProcess:
        captured["cmd"] = cmd
        captured["ssh_target"] = ssh_target
        return _proc(out)

    monkeypatch.setattr(announce.remote, "ssh_run", _fake_ssh)
    res = announce.read_announcements(
        ssh_target="u@h", remote_path="/remote/exp", run_id="r1", task_count=10
    )
    assert res == {"present": 1, "announced": 10, "complete": 8, "failed": 2, "missing": 0}
    # ONE exec, pointed at the per-run announce dir, pure-ls (no cat).
    assert "/remote/exp/.hpc/announce/r1" in captured["cmd"]
    assert "cat" not in captured["cmd"]
    assert "task_*.complete" in captured["cmd"] and "task_*.failed" in captured["cmd"]


def test_partial_announcement_reports_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    out = "__HPC_ANNOUNCE_ACK__\ncomplete=3\nfailed=0\n"
    monkeypatch.setattr(announce.remote, "ssh_run", lambda *a, **k: _proc(out))
    res = announce.read_announcements(
        ssh_target="u@h", remote_path="/remote", run_id="r1", task_count=10
    )
    assert res == {"present": 1, "announced": 3, "complete": 3, "failed": 0, "missing": 7}


def test_no_ack_reads_as_no_announcements(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pre-announce run: cd into a nonexistent dir yields no ack. Must read as
    # zero announcements (the capability signal the caller falls through on),
    # never a spurious count.
    monkeypatch.setattr(announce.remote, "ssh_run", lambda *a, **k: _proc(""))
    res = announce.read_announcements(
        ssh_target="u@h", remote_path="/remote", run_id="r1", task_count=5
    )
    assert res == {"present": 0, "announced": 0, "complete": 0, "failed": 0, "missing": 5}


def test_ssh_transport_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # rc 255 = ssh transport death, NOT "nothing announced" — must raise so the
    # caller doesn't read a blip as an empty announce set.
    monkeypatch.setattr(
        announce.remote, "ssh_run", lambda *a, **k: _proc("", returncode=255, stderr="conn refused")
    )
    with pytest.raises(errors.RemoteCommandFailed):
        announce.read_announcements(
            ssh_target="u@h", remote_path="/remote", run_id="r1", task_count=5
        )
