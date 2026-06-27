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
        return _proc(0, stdout=_OK_ENVELOPE)

    monkeypatch.setattr(cluster_status.remote, "ssh_run", fake_ssh_run)
    _run()
    cmd = captured["cmd"]
    pinned = (
        "${CONDA_PREFIX:+$CONDA_PREFIX/bin/}python -m hpc_agent.execution.mapreduce.reduce.status"
    )
    assert pinned in cmd
    # the bare, hijackable ``python -m hpc_agent`` form must NOT be emitted.
    assert " python -m hpc_agent" not in cmd


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
