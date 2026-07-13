"""Tests for the status-reporter robustness fixes (BUG 2 + the env-python pin).

1. The reporter SSH command pins the interpreter to the activated env's python
   (``$CONDA_PREFIX/bin/python``), so a cluster ``module load python/X`` reload
   can't hijack a bare ``python`` and run the reporter under the wrong interpreter.
2. On ``rc != 0`` the caller surfaces the reporter's STRUCTURED stdout error
   (``errors[].code`` / ``detail``) instead of only the stderr — which on an Lmod
   cluster is benign ``module load`` reload noise that masks the real cause.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from hpc_agent.errors import RemoteCommandFailed
from hpc_agent.infra import cluster_status
from hpc_agent.infra.cluster_status import _reporter_error_from_stdout, ssh_status_report

_OK_ENVELOPE = json.dumps({"summary": {}, "tasks": {}, "rollup": {}, "errors": []})


def _ack(rc: int = 0) -> str:
    """The positive-evidence ack line a complete reporter read carries."""
    return f"\n{cluster_status._STATUS_ACK_PREFIX}{rc}"


def _ok_stdout(rc: int = 0) -> str:
    """A successful reporter read: JSON envelope + the sentinel-ack (run to completion)."""
    return _OK_ENVELOPE + _ack(rc)


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _run(**over):
    kw = dict(ssh_target="h", remote_path="/p", run_id="r", job_ids=["1"], job_name="j")
    kw.update(over)
    return ssh_status_report(**kw)


def test_reporter_command_pins_env_python(monkeypatch):
    captured: dict[str, str] = {}

    def fake_ssh_run(cmd, *, ssh_target):
        captured["cmd"] = cmd
        return _proc(0, stdout=_ok_stdout())

    monkeypatch.setattr(cluster_status.remote, "ssh_run", fake_ssh_run)
    _run()
    cmd = captured["cmd"]
    pinned = (
        "${CONDA_PREFIX:+$CONDA_PREFIX/bin/}python -m hpc_agent.execution.mapreduce.reduce.status"
    )
    assert pinned in cmd
    # the bare, hijackable ``python -m hpc_agent`` form must NOT be emitted.
    assert " python -m hpc_agent" not in cmd


def test_reporter_command_guards_module_absence_as_127(monkeypatch):
    """Run #7 live: with an empty activation, ``python -m hpc_agent...`` on a
    bare login node exits **1** ("No module named hpc_agent"), which the canary
    poll loop classifies "transient" and rides the full wait budget. The
    command must probe the import first and ``exit 127`` so module-absence
    lands in the deterministic-env class (rc 126/127) that escalates fast."""
    captured: dict[str, str] = {}

    def fake_ssh_run(cmd, *, ssh_target):
        captured["cmd"] = cmd
        return _proc(0, stdout=_ok_stdout())

    monkeypatch.setattr(cluster_status.remote, "ssh_run", fake_ssh_run)
    _run()
    cmd = captured["cmd"]
    guard = "-c 'import hpc_agent' 2>/dev/null || exit 127; "
    assert guard in cmd, f"import guard missing from reporter cmd: {cmd!r}"
    # Guard runs BEFORE the reporter module in the same shell.
    assert cmd.index(guard) < cmd.index("-m hpc_agent.execution.mapreduce.reduce.status")


def test_rc_nonzero_surfaces_structured_stdout_over_lmod_noise(monkeypatch):
    err_doc = json.dumps(
        {"errors": [{"code": "tasks_py_import_error", "detail": ".hpc/tasks.py: boom"}]}
    )
    lmod_noise = "reloaded with a version change: python/3.11 => python/3.13"

    monkeypatch.setattr(
        cluster_status.remote, "ssh_run", lambda cmd, *, ssh_target: _proc(2, err_doc, lmod_noise)
    )
    with pytest.raises(RemoteCommandFailed) as ei:
        _run()
    msg = str(ei.value)
    assert "tasks_py_import_error" in msg
    assert "tasks.py: boom" in msg  # the real, actionable cause is the headline
    assert "rc=2" in msg


def test_rc_nonzero_falls_back_to_stderr_when_stdout_not_structured(monkeypatch):
    monkeypatch.setattr(
        cluster_status.remote,
        "ssh_run",
        lambda cmd, *, ssh_target: _proc(255, stdout="", stderr="ssh: connect: timed out"),
    )
    with pytest.raises(RemoteCommandFailed) as ei:
        _run()
    assert "ssh: connect: timed out" in str(ei.value)


def test_reporter_error_from_stdout_parsing():
    assert _reporter_error_from_stdout('{"errors":[{"code":"x","detail":"d"}]}') == "x: d"
    assert _reporter_error_from_stdout('{"errors":[{"code":"x"}]}') == "x"
    assert _reporter_error_from_stdout('{"errors":[]}') is None
    assert _reporter_error_from_stdout('{"summary":{}}') is None
    assert _reporter_error_from_stdout("not json at all") is None


# --- sentinel-ack positive-evidence transport (run-12 finding 24) ------------


def test_reporter_command_appends_ack_sentinel(monkeypatch):
    """The reporter command carries the affirmative ack echo as its LAST step,
    inside the shell so a mid-stream truncation loses it (positive evidence)."""
    captured: dict[str, str] = {}

    def fake_ssh_run(cmd, *, ssh_target):
        captured["cmd"] = cmd
        return _proc(0, stdout=_ok_stdout())

    monkeypatch.setattr(cluster_status.remote, "ssh_run", fake_ssh_run)
    _run()
    cmd = captured["cmd"]
    assert f'echo "{cluster_status._STATUS_ACK_PREFIX}$__hpc_rc"' in cmd
    # reporter rc captured before the ack, and re-surfaced as the ssh rc.
    assert "__hpc_rc=$?" in cmd
    assert cmd.rstrip().endswith("exit $__hpc_rc")
    # the ack echo runs AFTER the reporter module.
    assert cmd.index("-m hpc_agent.execution.mapreduce.reduce.status") < cmd.index(
        cluster_status._STATUS_ACK_PREFIX
    )


def test_ack_present_rc0_parses_as_today(monkeypatch):
    """ack present + rc 0 → the JSON reporter envelope is parsed normally."""
    monkeypatch.setattr(
        cluster_status.remote, "ssh_run", lambda cmd, *, ssh_target: _proc(0, stdout=_ok_stdout())
    )
    report = _run()
    assert report == {"summary": {}, "tasks": {}, "rollup": {}, "errors": []}


def test_severed_stream_rc0_no_ack_raises_transient(monkeypatch):
    """A severed / truncated channel delivers a clean rc-0 read with NO ack: the
    reader must REFUSE to parse-and-trust it and raise (UNKNOWN), never read the
    truncated stream as 'the reporter emitted nothing' (finding 24)."""
    # A partial JSON body that happens to parse would be the dangerous case; even
    # a fully-valid-looking envelope with no ack is untrustworthy.
    monkeypatch.setattr(
        cluster_status.remote,
        "ssh_run",
        lambda cmd, *, ssh_target: _proc(0, stdout=_OK_ENVELOPE),  # NO ack appended
    )
    with pytest.raises(RemoteCommandFailed) as ei:
        _run()
    msg = str(ei.value)
    assert "channel severed" in msg or "truncated" in msg
    assert "__HPC_STATUS_ACK__" in msg


def test_empty_rc0_no_ack_raises_transient(monkeypatch):
    """The exact finding-24 victim: an empty rc-0 read (NAT idle-drop / idle
    reaper) is a severed channel, not a reporter that emitted nothing."""
    monkeypatch.setattr(
        cluster_status.remote, "ssh_run", lambda cmd, *, ssh_target: _proc(0, stdout="")
    )
    with pytest.raises(RemoteCommandFailed, match="channel severed|truncated"):
        _run()


def test_ack_present_rc_nonzero_surfaces_real_rc(monkeypatch):
    """ack present + rc nonzero → the real returncode surfaces (not masked by the
    ack); the ack line is stripped so the structured error still parses."""
    err_doc = json.dumps({"errors": [{"code": "boom", "detail": "d"}]})
    monkeypatch.setattr(
        cluster_status.remote,
        "ssh_run",
        lambda cmd, *, ssh_target: _proc(2, stdout=err_doc + _ack(2)),
    )
    with pytest.raises(RemoteCommandFailed) as ei:
        _run()
    assert ei.value.returncode == 2
    assert "boom: d" in str(ei.value)


def test_remote_deadline_rc124_no_ack_is_transient(monkeypatch):
    """An expired remote deadline (``timeout`` fires → rc 124, bash killed before
    the ack echo) surfaces the real rc 124 — classified transient downstream,
    never a broken-env or success signal."""
    monkeypatch.setattr(
        cluster_status.remote,
        "ssh_run",
        lambda cmd, *, ssh_target: _proc(124, stdout="", stderr=""),
    )
    with pytest.raises(RemoteCommandFailed) as ei:
        _run()
    assert ei.value.returncode == 124
