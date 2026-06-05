"""#276 Bug 2 regression — the cluster status-poll path inherits the Windows
named-pipe ``getsockname`` auto-retry.

``infra.cluster_status.ssh_status_report`` shells through
``infra.remote.ssh_run``, and ``ssh_run`` itself is wrapped in
``run_with_named_pipe_retry`` (commit ``d6095f0``). So the wrapper *does* reach
the status path — contrary to the original #276 Bug 2 premise that it didn't —
and a transient ``getsockname failed: Not a socket`` on a status poll
auto-recovers on one retry instead of surfacing a terminal ``status reporter
failed`` that the monitor records as an ``abandoned`` journal corpse (the
upstream of #276 Bug 1).

The seam patched here is ``infra.remote._capture_via_select`` — the one capture
point ``ssh_run`` funnels through (and the documented point tests fake remote
output) — so the real ``run_with_named_pipe_retry`` recovery runs end to end.
"""

from __future__ import annotations

import subprocess

import pytest

from hpc_agent.errors import RemoteCommandFailed
from hpc_agent.infra import cluster_status, remote, ssh_options


@pytest.fixture(autouse=True)
def _reset_named_pipe_verdict():
    """The runtime verdict is a module-level mutable; reset it around each test
    so a retry that marks it broken doesn't leak into the next test."""
    ssh_options.reset_named_pipe_runtime_verdict()
    yield
    ssh_options.reset_named_pipe_runtime_verdict()


def _report(monkeypatch, attempts):
    """Drive ssh_status_report with *attempts* (an iterable of CompletedProcess)
    returned in order from the capture seam; return (parsed_report, call_argvs)."""
    outcomes = iter(attempts)
    calls: list[list[str]] = []

    def _fake_capture(argv, *, timeout):  # matches remote._capture_via_select
        calls.append(argv)
        return next(outcomes)

    monkeypatch.setenv("HPC_SSH_NO_BACKOFF", "1")  # isolate the named-pipe retry
    monkeypatch.setattr(remote, "_capture_via_select", _fake_capture)
    report = cluster_status.ssh_status_report(
        ssh_target="u@host",
        remote_path="/scratch/exp",
        run_id="run1",
        job_ids=["13554560"],
        job_name="jn",
    )
    return report, calls


def test_status_poll_recovers_from_getsockname(monkeypatch):
    # First attempt hits the Windows named-pipe bind failure; the wrapper marks
    # the verdict broken, rebuilds argv with multiplexing demoted, and retries
    # once — the second attempt succeeds. The status path recovers; no corpse.
    bad = subprocess.CompletedProcess(
        ["ssh"], 255, stdout="", stderr="getsockname failed: Not a socket\n"
    )
    good = subprocess.CompletedProcess(["ssh"], 0, stdout='{"summary": {}, "tasks": []}', stderr="")
    report, calls = _report(monkeypatch, [bad, good])

    assert len(calls) == 2  # failed once, retried, succeeded
    assert report == {"summary": {}, "tasks": []}
    assert ssh_options._named_pipe_runtime_broken()  # the flake demoted multiplexing


def test_status_poll_surfaces_non_getsockname_failure(monkeypatch):
    # A genuine non-named-pipe failure is NOT our marker: no retry, and the
    # terminal ``status reporter failed`` surfaces as before (so real cluster
    # problems are not silently swallowed by the recovery path).
    bad = subprocess.CompletedProcess(
        ["ssh"], 1, stdout="", stderr="qstat: some cluster-side error"
    )
    with pytest.raises(RemoteCommandFailed, match="status reporter failed"):
        _report(monkeypatch, [bad])
    assert not ssh_options._named_pipe_runtime_broken()
