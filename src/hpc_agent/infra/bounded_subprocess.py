"""Bounded subprocess execution that kills the whole PROCESS TREE on timeout.

Plain ``subprocess.run(argv, timeout=T)`` kills only the IMMEDIATE child when
the deadline fires, then blocks in its post-timeout ``communicate()`` reading
stdout until EOF. When that child spawned a grandchild — the composite-preflight
verbs shell ``hpc-agent <verb>`` which in turn spawns ``ssh`` — the grandchild
inherits the stdout pipe's write end and keeps it open after its parent is
killed, so the drain hangs for the grandchild's FULL lifetime and the timeout is
defeated entirely.

Observed live 2026-07-05: a Hoffman2 ``submit-s1`` preflight probe left two
``ssh.exe`` orphaned ~17 min, wedging the block. The outer 60s
``submit_preflight`` timeout killed the ``hpc-agent preflight`` child but not its
``ssh`` grandchild, and on Windows ``TerminateProcess`` does not cascade to
grandchildren (on POSIX a daemonized grandchild likewise escapes a bare child
kill), so the surviving ``ssh`` held the pipe past the deadline.

:func:`run_capture_bounded` closes the gap: it launches the child in its own
session/group (POSIX ``start_new_session``) and on timeout kills the ENTIRE tree
— POSIX ``os.killpg`` on the child's group, Windows ``taskkill /T`` walking the
PID tree — before draining, so no grandchild can hold the pipe past the
deadline. The signature mirrors ``subprocess.run(capture_output=True,
text=True, encoding="utf-8", timeout=...)``: it returns a ``CompletedProcess``
and raises ``subprocess.TimeoutExpired``, so existing call sites branch
identically (their ``except subprocess.TimeoutExpired`` / ``except OSError``
arms keep working unchanged).

Safety note on the kill: on POSIX ``start_new_session=True`` is REQUIRED, not
cosmetic — without it the child shares the caller's process group and
``os.killpg`` would signal the caller too. On Windows ``taskkill /F /T /PID`` is
scoped to the child's PID subtree, so it can never reach the caller.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Sequence

__all__ = ["run_capture_bounded"]

# Bound on the post-kill drain so a still-stuck grandchild cannot re-hang the
# caller here (belt-and-suspenders: the tree kill should have reaped it).
_DRAIN_TIMEOUT_SEC = 10.0


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill *proc* and every descendant. Best-effort; never raises."""
    if sys.platform == "win32":
        # ``/T`` kills the process AND its child tree (walked by PPID); ``/F``
        # forces. Scoped to proc.pid's subtree, so it can never reach the
        # caller. Bounded so a wedged taskkill can't itself hang us.
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=_DRAIN_TIMEOUT_SEC,
            )
        with contextlib.suppress(OSError):
            proc.kill()  # ensure the direct child is gone even if taskkill missed it
    else:
        # start_new_session made the child a session/group leader; signal the
        # whole group so a grandchild that escaped the direct child dies too.
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(OSError):
            proc.kill()


def run_capture_bounded(
    argv: Sequence[str], *, timeout_sec: float
) -> subprocess.CompletedProcess[str]:
    """Run *argv*, capturing stdout/stderr, bounded by *timeout_sec*.

    Behaves like ``subprocess.run(argv, capture_output=True, text=True,
    encoding="utf-8", timeout=timeout_sec)`` EXCEPT that on timeout it kills the
    whole process tree (not just the immediate child) before draining, so a
    grandchild holding the stdout pipe cannot outlive the deadline.

    Returns a ``subprocess.CompletedProcess`` on completion; raises
    ``subprocess.TimeoutExpired`` (after reaping the tree) on timeout. Spawn
    failures propagate as ``OSError`` from ``Popen`` — same as
    ``subprocess.run``.
    """
    popen_kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
    }
    if sys.platform != "win32":
        # Own session/group so the timeout can killpg the whole tree safely.
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(argv, **popen_kwargs)  # type: ignore[call-overload]
    try:
        out, err = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        # Drain so the pipes close and the child is reaped; bounded so a
        # still-stuck grandchild can't re-hang us on this second wait.
        try:
            out, err = proc.communicate(timeout=_DRAIN_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        raise subprocess.TimeoutExpired(
            cmd=list(argv), timeout=timeout_sec, output=out, stderr=err
        ) from None
    return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)
