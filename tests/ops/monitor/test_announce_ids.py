"""``read_announced_task_ids`` — the id-carrying sibling of the counts census (Δ1).

Where ``read_announcements`` COUNTS the per-task terminal markers, a remainder
migration needs the SET of done ids to compute ``undone = range(total) − done``.
Same ACK discipline: an absent announce dir / dropped ack is "no per-task census"
(``present=False``, empty set), NEVER "all undone"; an ssh transport failure
raises.
"""

from __future__ import annotations

import subprocess

import pytest

from hpc_agent import errors
from hpc_agent.ops.monitor import announce


def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_ids_read_parses_the_done_set(monkeypatch: pytest.MonkeyPatch) -> None:
    out = "__HPC_ANNOUNCE_IDS_ACK__\ntask_3.complete\ntask_7.complete\n"
    captured: dict[str, str] = {}

    def _fake_ssh(cmd: str, *, ssh_target: str, **_kw) -> subprocess.CompletedProcess:
        captured["cmd"] = cmd
        return _proc(out)

    monkeypatch.setattr(announce.remote, "ssh_run", _fake_ssh)
    res = announce.read_announced_task_ids(ssh_target="u@h", remote_path="/remote/exp", run_id="r1")
    assert res.present is True
    assert res.done_ids == frozenset({3, 7})
    # ONE exec, pointed at the per-run announce dir, pure-ls (no cat), listing
    # names not counting them.
    assert "/remote/exp/.hpc/announce/r1" in captured["cmd"]
    assert "cat" not in captured["cmd"]
    assert "task_*.complete" in captured["cmd"] and "wc -l" not in captured["cmd"]


def test_ids_read_ignores_failed_and_stray_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    # COMPLETE only: a terminal FAILURE still needs a re-run, so it is NOT done for
    # a remainder migration; a stray non-conforming name never parses to a bogus id.
    out = (
        "__HPC_ANNOUNCE_IDS_ACK__\n"
        "task_0.complete\n"
        "task_5.failed\n"
        "task_.complete\n"
        "task_12.complete\n"
        "README\n"
    )
    monkeypatch.setattr(announce.remote, "ssh_run", lambda *a, **k: _proc(out))
    res = announce.read_announced_task_ids(ssh_target="u@h", remote_path="/r", run_id="r1")
    assert res.done_ids == frozenset({0, 12})


def test_no_ack_is_no_census_not_all_undone(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pre-announce run: cd into a nonexistent dir yields no ack. present=False +
    # empty set — the caller REFUSES, never reads absence as "all undone".
    monkeypatch.setattr(announce.remote, "ssh_run", lambda *a, **k: _proc(""))
    res = announce.read_announced_task_ids(ssh_target="u@h", remote_path="/r", run_id="r1")
    assert res.present is False
    assert res.done_ids == frozenset()


def test_present_but_empty_dir_is_distinct_from_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dispatcher started, no task complete yet: cd succeeds (ack echoed) but ls is
    # empty. present=True with an empty set — distinct from the absent-dir refusal.
    monkeypatch.setattr(
        announce.remote, "ssh_run", lambda *a, **k: _proc("__HPC_ANNOUNCE_IDS_ACK__\n")
    )
    res = announce.read_announced_task_ids(ssh_target="u@h", remote_path="/r", run_id="r1")
    assert res.present is True
    assert res.done_ids == frozenset()


def test_ssh_transport_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # rc 255 = ssh transport death, NOT "nothing done" — must raise so a blip is
    # never read as an empty done-set.
    monkeypatch.setattr(
        announce.remote,
        "ssh_run",
        lambda *a, **k: _proc("", returncode=255, stderr="conn refused"),
    )
    with pytest.raises(errors.RemoteCommandFailed):
        announce.read_announced_task_ids(ssh_target="u@h", remote_path="/r", run_id="r1")
