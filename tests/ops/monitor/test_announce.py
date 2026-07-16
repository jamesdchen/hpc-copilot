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
    # P3 run-terminal WAKE marker: the standalone dispatcher writer and the
    # control-plane waiter/reader must agree on the ONE filename (doctrine row 12,
    # the exit_no_output_constant lockstep pattern).
    assert announce.ANNOUNCE_RUN_TERMINAL == dispatch._ANNOUNCE_RUN_TERMINAL


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


# ── F4: per-host batched census (fleet-of-3 = 1 exec/tick) ────────────────────


def test_batch_census_is_one_exec_for_a_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fleet of 3 runs on one login node censuses in ONE ssh exec (F4 fold),
    each run's counts distributed back with the per-run reader's shape."""
    out = (
        "__HPC_ANNOUNCE_BATCH_ACK__\n"
        "run=r1 present=1 complete=4 failed=0\n"
        "run=r2 present=1 complete=1 failed=1\n"
        # r3 dir doesn't exist yet → no present row → reported present:0 below.
    )
    dials: list[str] = []

    def _fake_ssh(cmd: str, *, ssh_target: str, **_kw) -> subprocess.CompletedProcess:
        dials.append(cmd)
        return _proc(out)

    monkeypatch.setattr(announce.remote, "ssh_run", _fake_ssh)
    res = announce.read_announcements_batch(
        ssh_target="u@h",
        remote_path="/remote/exp",
        run_task_counts={"r1": 4, "r2": 4, "r3": 4},
    )
    assert len(dials) == 1  # ONE exec for the whole fleet, not 3
    assert res["r1"] == {"present": 1, "announced": 4, "complete": 4, "failed": 0, "missing": 0}
    assert res["r2"] == {"present": 1, "announced": 2, "complete": 1, "failed": 1, "missing": 2}
    # A run with no announce dir yet reads present:0 individually — the fleet
    # census still acked, only r3 falls through per-run.
    assert res["r3"] == {"present": 0, "announced": 0, "complete": 0, "failed": 0, "missing": 4}


def test_batch_census_severed_read_degrades_all_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A severed batch read (ack dropped, rc 0) degrades EVERY run to not-present —
    never a spurious zero count on truncated bytes."""
    monkeypatch.setattr(announce.remote, "ssh_run", lambda *a, **k: _proc("run=r1 present=1"))
    res = announce.read_announcements_batch(
        ssh_target="u@h", remote_path="/remote", run_task_counts={"r1": 4, "r2": 4}
    )
    assert res["r1"]["present"] == 0 and res["r2"]["present"] == 0


# ── P3: per-host remote bounded-wait waiter (wake-is-a-hint) ──────────────────


def _wait_proc(woke: bool, acked: bool = True, returncode: int = 0) -> subprocess.CompletedProcess:
    lines = []
    if woke:
        lines.append("__HPC_ANNOUNCE_WOKE__")
    if acked:
        lines.append("__HPC_ANNOUNCE_WAIT_ACK__")
    return _proc("\n".join(lines) + "\n", returncode=returncode)


def test_waiter_wakes_in_one_dial_with_zero_intervening_census_reads() -> None:
    """Acceptance #1: a marker change wakes the waiter in ONE remote dial — the
    poll moved REMOTE-side, so there are ZERO intervening client census reads
    during the wait (the whole wait is one ssh exec, not N client polls)."""
    dials: list[str] = []

    def _fake_ssh(cmd: str, *, ssh_target: str, **_kw) -> subprocess.CompletedProcess:
        dials.append(cmd)
        return _wait_proc(woke=True)

    res = announce.wait_for_announce_change(
        ssh_target="u@h",
        remote_path="/remote/exp",
        run_ids=["r1"],
        deadline_seconds=60,
        _ssh_run=_fake_ssh,
    )
    assert res["woke"] is True and res["acked"] is True
    assert len(dials) == 1  # ONE remote dial for the whole wait
    # The remote body is a sh poll loop (C1: no inotify) watching the run dir and
    # short-circuiting on the run-terminal wake marker.
    cmd = dials[0]
    assert "/remote/exp/.hpc/announce/r1" in cmd
    assert ".run_terminal" in cmd
    assert "sleep" in cmd and "inotifywait" not in cmd


def test_waiter_multiplexes_a_fleet_in_one_dial() -> None:
    """C4: ONE waiter per host multiplexes all runs — a fleet of 3 is watched in a
    single remote dial (not one waiter per run)."""
    dials: list[str] = []

    def _fake_ssh(cmd: str, *, ssh_target: str, **_kw) -> subprocess.CompletedProcess:
        dials.append(cmd)
        return _wait_proc(woke=True)

    announce.wait_for_announce_change(
        ssh_target="u@h",
        remote_path="/remote",
        run_ids=["r1", "r2", "r3"],
        deadline_seconds=30,
        _ssh_run=_fake_ssh,
    )
    assert len(dials) == 1
    for rid in ("r1", "r2", "r3"):
        assert f"/remote/.hpc/announce/{rid}" in dials[0]


def test_waiter_severed_wait_is_not_a_wake() -> None:
    """C2/severed-vs-empty: a wait whose ack was dropped (severed link) is NOT a
    wake — the caller must re-census, never trust the truncated bytes."""
    res = announce.wait_for_announce_change(
        ssh_target="u@h",
        remote_path="/remote",
        run_ids=["r1"],
        deadline_seconds=10,
        _ssh_run=lambda *a, **k: _wait_proc(woke=True, acked=False, returncode=255),
    )
    assert res["acked"] is False
    assert res["woke"] is False  # a WOKE line without an ack does not count


def test_waiter_deadline_elapsed_returns_not_woke_but_acked() -> None:
    """A clean wait that reached its deadline with no change acks but does not
    wake — the caller re-censuses on the same (unchanged) state and backs off."""
    res = announce.wait_for_announce_change(
        ssh_target="u@h",
        remote_path="/remote",
        run_ids=["r1"],
        deadline_seconds=5,
        _ssh_run=lambda *a, **k: _wait_proc(woke=False, acked=True),
    )
    assert res["acked"] is True and res["woke"] is False


def test_waiter_stamps_watchdog_before_the_blocked_wait() -> None:
    """Doctrine row 13: the blocked wait routes its liveness stamp through the
    caller's closure over the ONE stamp_watchdog_tick definition, called BEFORE
    the single blocking dial so a poller that dies mid-wait lapses its deadline."""
    order: list[str] = []

    def _stamp() -> None:
        order.append("stamp")

    def _fake_ssh(cmd: str, *, ssh_target: str, **_kw) -> subprocess.CompletedProcess:
        order.append("dial")
        return _wait_proc(woke=True)

    announce.wait_for_announce_change(
        ssh_target="u@h",
        remote_path="/remote",
        run_ids=["r1"],
        deadline_seconds=10,
        stamp=_stamp,
        _ssh_run=_fake_ssh,
    )
    assert order == ["stamp", "dial"]  # stamped DURING (before) the blocked wait


def test_waiter_wake_is_a_hint_never_a_settle() -> None:
    """Doctrine row 11 (fire test): the waiter returns a WAKE signal only — a dict
    with no lifecycle verdict. A forged/premature run-terminal marker wakes the
    loop, but the return carries no settle; the control plane must re-read the
    per-task markers (the truth) to decide lifecycle."""
    res = announce.wait_for_announce_change(
        ssh_target="u@h",
        remote_path="/remote",
        run_ids=["r1"],
        deadline_seconds=10,
        _ssh_run=lambda *a, **k: _wait_proc(woke=True),
    )
    # The result is a hint — it names no terminal state / lifecycle verdict.
    assert set(res.keys()) == {"woke", "acked", "waited"}
    assert "complete" not in res and "failed" not in res and "lifecycle_state" not in res
