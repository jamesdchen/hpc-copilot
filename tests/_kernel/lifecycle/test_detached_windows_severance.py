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

``TestDeliberateSeverance`` is the end-to-end PROOF (2026-07-05 finding j): the
flag tests above pin what we *ask* Popen for, but nobody had demonstrated the
a7eb6207 breakaway fix in the real incident topology — a parent inside a
kill-on-close Job Object WITHOUT ``SILENT_BREAKAWAY_OK`` (the hostile-host
case; explicit ``BREAKAWAY_OK`` granted, as the empirical measurement assumed)
spawning through the actual :func:`_spawn_detached`, then dying and letting the
job handle close. The proof includes a negative control: a sibling spawned with
only the PRE-fix flags from the same parent must die with the job — otherwise
worker survival would prove nothing about the job at all.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Deliberate severance proof (finding j) — the REAL incident topology
# ---------------------------------------------------------------------------

# The worker: heartbeats a counter to a file. Self-bounded (~60s) so a cleanup
# failure can never leave a permanent orphan.
_WORKER_SRC = """\
import sys, time
hb = sys.argv[1]
for i in range(1200):
    with open(hb, "w", encoding="utf-8") as f:
        f.write(str(i))
    time.sleep(0.05)
"""

# The intermediate parent (spawned by the test so its death is controllable):
# builds the hostile-host Job Object — KILL_ON_JOB_CLOSE + explicit
# BREAKAWAY_OK, deliberately WITHOUT SILENT_BREAKAWAY_OK — assigns ITSELF,
# spawns (a) a negative-control child with only the PRE-fix detach flags and
# (b) the real worker through the actual production _spawn_detached helper,
# writes both pids, then exits. Its death closes the job handle: the kernel
# destroys the job and KILL_ON_JOB_CLOSE terminates everything still inside.
_PARENT_SRC = """\
import ctypes, json, subprocess, sys
from ctypes import wintypes
from pathlib import Path

hb_path, control_hb_path, out_path, worker_script, work_dir = sys.argv[1:6]

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800  # explicit breakaway allowed; NO silent (0x1000)
JobObjectExtendedLimitInformation = 9


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
# Explicit signatures: HANDLEs are 64-bit; ctypes' default c_int restype
# truncates them (observed: ERROR_INVALID_HANDLE from a truncated job handle).
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetCurrentProcess.argtypes = []
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

job = kernel32.CreateJobObjectW(None, None)
if not job:
    sys.exit(f"CreateJobObjectW failed; GetLastError={ctypes.get_last_error()}")
info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
info.BasicLimitInformation.LimitFlags = (
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK
)
if not kernel32.SetInformationJobObject(
    job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
):
    sys.exit(f"SetInformationJobObject failed; GetLastError={ctypes.get_last_error()}")
if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
    sys.exit(
        "AssignProcessToJobObject failed (nested-job denial?); "
        f"GetLastError={ctypes.get_last_error()}"
    )

# (a) negative control: PRE-fix flags only — inherits the job, must die with it.
control = subprocess.Popen(
    [sys.executable, worker_script, control_hb_path],
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

# (b) the real thing: the actual production spawn helper (breakaway + lease).
from hpc_agent._kernel.lifecycle.detached import _spawn_detached

launch = _spawn_detached(
    run_id="severance-proof",
    block="severance",
    argv=[sys.executable, worker_script, hb_path],
    log_path=Path(work_dir) / "severance-worker.log",
    cwd=work_dir,
)

Path(out_path).write_text(
    json.dumps({"worker_pid": launch.pid, "control_pid": control.pid}),
    encoding="utf-8",
)
# Exit now. Process death closes this process's job handle; with no other
# handle the job is destroyed and KILL_ON_JOB_CLOSE fires for its members.
"""


def _read_heartbeat(path: Path) -> int:
    """Current heartbeat counter; -1 while absent/mid-write (tolerated race)."""
    try:
        return int(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return -1


def _wait_until(predicate, deadline_sec: float, interval: float = 0.1) -> bool:
    """Bounded poll — True when *predicate* fired before the deadline."""
    deadline = time.monotonic() + deadline_sec
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _terminate(pid: int) -> None:
    """Best-effort bounded cleanup: TerminateProcess via os.kill on win32."""
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)


@win32_only
def test_worker_survives_job_handle_close_through_dying_parent(tmp_path):
    """Finding j, proven end-to-end: a worker launched by the REAL
    ``_spawn_detached`` from inside a kill-on-close Job Object (explicit
    BREAKAWAY_OK, no SILENT_BREAKAWAY_OK) keeps heartbeating after its parent
    dies and the job handle closes — while a sibling spawned with only the
    pre-fix flags is killed with the job (the negative control that proves
    the topology is genuinely hostile, not that the job never engaged)."""
    from hpc_agent._kernel.lifecycle.detached import _pid_alive

    worker_script = tmp_path / "worker.py"
    worker_script.write_text(_WORKER_SRC, encoding="utf-8")
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(_PARENT_SRC, encoding="utf-8")
    hb_path = tmp_path / "worker.hb"
    control_hb_path = tmp_path / "control.hb"
    out_path = tmp_path / "pids.json"

    worker_pid = control_pid = -1
    try:
        # The parent's death is the controllable severance event: run it as a
        # subprocess and let its exit BE the parent dying. It is spawned with
        # CREATE_BREAKAWAY_FROM_JOB so it starts OUTSIDE any ambient job the
        # test process sits in (an already-jobbed parent can be refused the
        # self-assignment into the freshly built hostile job — observed here:
        # AssignProcessToJobObject fails under the harness's session job).
        try:
            cp = subprocess.run(
                [
                    sys.executable,
                    str(parent_script),
                    str(hb_path),
                    str(control_hb_path),
                    str(out_path),
                    str(worker_script),
                    str(tmp_path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB,
            )
        except OSError as exc:
            if getattr(exc, "winerror", None) == 5:  # ERROR_ACCESS_DENIED
                pytest.skip(
                    "ambient job denies breakaway; cannot construct the "
                    "severance topology on this host"
                )
            raise
        assert cp.returncode == 0, f"parent failed: stdout={cp.stdout!r} stderr={cp.stderr!r}"
        pids = json.loads(out_path.read_text(encoding="utf-8"))
        worker_pid, control_pid = pids["worker_pid"], pids["control_pid"]

        # Negative control: the in-job sibling dies when the job handle closes.
        assert _wait_until(lambda: not _pid_alive(control_pid), 15.0), (
            "the pre-fix-flags control child survived the job-handle close — the "
            "Job Object never engaged, so this run proves nothing about severance"
        )

        # The proof: the broken-away worker keeps heartbeating AFTER the parent
        # is dead and the job is gone.
        assert _pid_alive(worker_pid), "worker died with the job — breakaway did not hold"
        first = _read_heartbeat(hb_path)
        assert _wait_until(lambda: _read_heartbeat(hb_path) > max(first, 0), 15.0), (
            f"worker heartbeat stalled at {first} after the parent died — the "
            "detached worker did not survive the session's job-handle close"
        )
        assert _pid_alive(worker_pid)
    finally:
        # Bounded cleanup; the workers also self-terminate after ~60s.
        for pid in (worker_pid, control_pid):
            if pid > 0:
                _terminate(pid)
