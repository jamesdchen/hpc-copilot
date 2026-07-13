"""The client half of the §5 hybrid monitor at the ``record_status`` /
``ssh_status_report`` seam: the SAME status ssh call stamps ``.hpc_last_read``
and surfaces ``.hpc_watcher_ALARM`` — zero extra round-trip.

``remote.ssh_run`` is monkeypatched so no cluster is touched; the focus is (a)
the composed command carries the last_read touch + alarm read, and (b) the
alarm text is surfaced under ``watcher_alarm`` in the returned status.
"""

from __future__ import annotations

import subprocess

import pytest

from hpc_agent.infra import cluster_status
from hpc_agent.infra import remote as remote_mod


def _fake_reporter_stdout() -> str:
    return (
        '{"summary": {"complete": 1, "running": 0, "pending": 0, "failed": 0, '
        '"unknown": 0}, "tasks": {}, "rollup": {}, "errors": []}'
    )


def _ack(rc: int = 0) -> str:
    """The positive-evidence ack line the reporter echoes LAST (after any trailer)."""
    return f"{cluster_status._STATUS_ACK_PREFIX}{rc}\n"


def test_status_command_stamps_last_read_and_reads_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def _fake(cmd: str, *, ssh_target: str, **_kw: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        # Reporter JSON, then the sentinel, then the ALARM contents.
        stdout = (
            _fake_reporter_stdout()
            + "\n"
            + cluster_status._WATCHER_ALARM_SENTINEL
            + "\n"
            + "client has not read status since 2026-07-03T00:00:00+00:00 (4000 s ago)\n"
            + _ack()  # positive-evidence ack, echoed AFTER the alarm trailer
        )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(remote_mod, "ssh_run", _fake)

    report = cluster_status.ssh_status_report(
        ssh_target="u@h",
        remote_path="/remote/proj",
        run_id="r1",
        job_ids=["100"],
        job_name="jobx",
        watcher_run_dir="/remote/proj",
    )

    cmd = captured["cmd"]
    # Zero extra round-trip: the touch + alarm read ride the reporter command,
    # and the reporter's exit code is preserved so a missing-ALARM cat can't fail it.
    assert "touch /remote/proj/.hpc_last_read" in cmd
    assert "cat /remote/proj/.hpc_watcher_ALARM" in cmd
    assert "exit $__hpc_rc" in cmd
    # The alarm text is surfaced; JSON parsed cleanly despite the trailer.
    assert report["summary"]["complete"] == 1
    assert report["watcher_alarm"] is not None
    assert "has not read status" in report["watcher_alarm"]


def test_status_no_alarm_surfaces_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(cmd: str, *, ssh_target: str, **_kw: object) -> subprocess.CompletedProcess[str]:
        stdout = (
            _fake_reporter_stdout() + "\n" + cluster_status._WATCHER_ALARM_SENTINEL + "\n" + _ack()
        )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(remote_mod, "ssh_run", _fake)

    report = cluster_status.ssh_status_report(
        ssh_target="u@h",
        remote_path="/remote/proj",
        run_id="r1",
        job_ids=["100"],
        job_name="jobx",
        watcher_run_dir="/remote/proj",
    )
    assert report["watcher_alarm"] is None


def test_status_without_watcher_dir_carries_ack_but_no_watcher_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that omits watcher_run_dir gets NO watcher probe (no last_read
    touch / alarm cat) and no watcher_alarm key — but STILL carries the
    positive-evidence ack + rc-preserving exit (the non-monitor callers:
    aggregate-flow, failures-atom, canary)."""
    captured: dict[str, str] = {}

    def _fake(cmd: str, *, ssh_target: str, **_kw: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=_fake_reporter_stdout() + "\n" + _ack(), stderr=""
        )

    monkeypatch.setattr(remote_mod, "ssh_run", _fake)

    report = cluster_status.ssh_status_report(
        ssh_target="u@h",
        remote_path="/remote/proj",
        run_id="r1",
        job_ids=["100"],
        job_name="jobx",
    )
    cmd = captured["cmd"]
    assert "hpc_last_read" not in cmd
    assert cluster_status._WATCHER_ALARM_SENTINEL not in cmd
    # the ack + rc-preserving exit ride every reporter read, watcher or not.
    assert cluster_status._STATUS_ACK_PREFIX in cmd
    assert "exit $__hpc_rc" in cmd
    assert "watcher_alarm" not in report
