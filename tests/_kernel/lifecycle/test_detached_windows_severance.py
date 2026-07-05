"""Windows detach-severance regression tests (proving-run-#3 finding h).

The detached worker is an MCP-grandchild: Claude-Code-like hosts run the MCP
server inside a Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, so a
worker spawned with only ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``
inherits the job and dies when the session's job handle closes (measured
empirically 2026-07-04, Windows 11 — see the ``_detach_popen_kwargs``
docstring). The fix: ``CREATE_BREAKAWAY_FROM_JOB`` in the detach flags, with a
one-shot fallback retry without it when the job denies breakaway
(``ERROR_ACCESS_DENIED``), so a hardened host degrades to the pre-fix contract
instead of refusing the launch.

These tests pin the flag composition and the fallback, mocking ``Popen`` so
they are deterministic; the flag-composition test is Windows-only because the
``CREATE_*`` constants exist only there.
"""

from __future__ import annotations

import subprocess
import sys
from unittest import mock

import pytest

win32_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only detach flags")


@win32_only
def test_detach_flags_include_job_breakaway():
    """The Windows detach flags must carry CREATE_BREAKAWAY_FROM_JOB — without
    it the worker stays in the host's kill-on-close Job Object and dies with
    the session, which is the exact severance the detach contract forbids."""
    from hpc_agent._kernel.lifecycle.detached import _detach_popen_kwargs

    kwargs = _detach_popen_kwargs()
    flags = kwargs["creationflags"]
    assert flags & subprocess.DETACHED_PROCESS
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert flags & subprocess.CREATE_BREAKAWAY_FROM_JOB


@win32_only
def test_breakaway_denied_falls_back_without_the_flag():
    """A job without BREAKAWAY_OK may refuse the spawn with ERROR_ACCESS_DENIED
    (winerror 5); the launch must retry once WITHOUT the breakaway bit and
    succeed — never refuse the detach outright."""
    from hpc_agent._kernel.lifecycle import detached

    calls: list[int] = []
    sentinel = object()

    def _popen(argv, **kwargs):
        flags = kwargs.get("creationflags", 0)
        calls.append(flags)
        if flags & subprocess.CREATE_BREAKAWAY_FROM_JOB:
            err = OSError("denied")
            err.winerror = 5
            raise err
        return sentinel

    with mock.patch.object(detached.subprocess, "Popen", side_effect=_popen):
        proc = detached._popen_detached(["dummy"])

    assert proc is sentinel
    assert len(calls) == 2
    assert calls[0] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert not (calls[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB)
    # The retry keeps the rest of the detach contract intact.
    assert calls[1] & subprocess.DETACHED_PROCESS
    assert calls[1] & subprocess.CREATE_NEW_PROCESS_GROUP


@win32_only
def test_non_breakaway_oserror_propagates():
    """Only ERROR_ACCESS_DENIED triggers the fallback; any other spawn failure
    (missing binary, bad cwd, ...) must propagate unchanged, not be retried."""
    from hpc_agent._kernel.lifecycle import detached

    calls: list[int] = []

    def _popen(argv, **kwargs):
        calls.append(kwargs.get("creationflags", 0))
        err = OSError("something else")
        err.winerror = 2  # ERROR_FILE_NOT_FOUND
        raise err

    with (
        mock.patch.object(detached.subprocess, "Popen", side_effect=_popen),
        pytest.raises(OSError) as excinfo,
    ):
        detached._popen_detached(["dummy"])

    assert excinfo.value.winerror == 2
    assert len(calls) == 1  # no retry


def test_posix_detach_uses_new_session():
    """On POSIX the detach contract is start_new_session; pinned so the win32
    branch work can never regress the POSIX shape."""
    from hpc_agent._kernel.lifecycle import detached

    with mock.patch.object(detached.sys, "platform", "linux"):
        assert detached._detach_popen_kwargs() == {"start_new_session": True}
