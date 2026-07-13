"""Regression tests for :func:`hpc_agent.infra.bounded_subprocess.run_capture_bounded`.

The load-bearing case is the grandchild orphan: a spawned child that itself
spawns a grandchild inheriting the stdout pipe. Plain ``subprocess.run`` kills
only the child on timeout, then blocks in its post-timeout drain until the
grandchild closes the pipe — the exact wedge that left two ``ssh`` orphaned for
~17 min during a live Hoffman2 ``submit-s1`` (2026-07-05). ``run_capture_bounded``
must kill the whole process tree so the call returns at ~the deadline, not the
grandchild's lifetime, and no descendant survives.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from hpc_agent.infra.bounded_subprocess import run_capture_bounded

# A grandchild sleep far longer than any test deadline, so a pipe-hang
# regression (drain waits for the grandchild) is unmistakably slower than a
# clean tree-kill.
_GRANDCHILD_SLEEP_SEC = 60
# Ceiling for the tree-kill path: a ~1s deadline + kill + bounded drain. Well
# below _GRANDCHILD_SLEEP_SEC so the assertion separates fixed from regressed
# without being flaky on a slow (esp. Windows) CI runner.
_TREE_KILL_CEILING_SEC = 15.0

# Parent that spawns a grandchild inheriting its stdout (our capture pipe), then
# both sleep past the deadline. The parent prints the grandchild PID first so a
# test can prove the whole tree — not just the direct child — was reaped.
_SLEEP_CMD = f"import time; time.sleep({_GRANDCHILD_SLEEP_SEC})"
_PARENT_SPAWNS_GRANDCHILD = (
    "import subprocess, sys, time; "
    f"gc = subprocess.Popen([sys.executable, '-c', {_SLEEP_CMD!r}]); "
    "print(gc.pid, flush=True); "
    f"time.sleep({_GRANDCHILD_SLEEP_SEC})"
)


def test_returns_completed_process_on_success() -> None:
    proc = run_capture_bounded(
        [sys.executable, "-c", "import sys; print('hello'); sys.stderr.write('werr')"],
        timeout_sec=30,
    )
    assert proc.returncode == 0
    assert "hello" in proc.stdout
    assert "werr" in proc.stderr


def test_nonzero_exit_is_reported() -> None:
    proc = run_capture_bounded([sys.executable, "-c", "import sys; sys.exit(3)"], timeout_sec=30)
    assert proc.returncode == 3


def test_plain_timeout_raises_without_hanging() -> None:
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_capture_bounded(
            [sys.executable, "-c", f"import time; time.sleep({_GRANDCHILD_SLEEP_SEC})"],
            timeout_sec=1,
        )
    assert time.monotonic() - started < _TREE_KILL_CEILING_SEC


def test_grandchild_pipe_does_not_hang_the_drain() -> None:
    """The orphan regression: a grandchild inheriting the stdout pipe must not
    make the post-timeout drain block for the grandchild's whole lifetime.

    With plain ``subprocess.run`` this call blocks ~``_GRANDCHILD_SLEEP_SEC``;
    with the tree-kill it returns in a few seconds.
    """
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_capture_bounded([sys.executable, "-c", _PARENT_SPAWNS_GRANDCHILD], timeout_sec=1)
    elapsed = time.monotonic() - started
    assert elapsed < _TREE_KILL_CEILING_SEC, (
        f"run_capture_bounded blocked {elapsed:.1f}s — the grandchild's pipe was "
        "not released, i.e. the process tree was not killed on timeout"
    )


def test_forwards_stdin_pipe() -> None:
    """``stdin=`` is forwarded to the child — the ``tar c | ssh tar x`` push
    pattern, where the local tar Popen's read end becomes ssh's stdin."""
    producer = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('piped-payload')"],
        stdout=subprocess.PIPE,
    )
    try:
        assert producer.stdout is not None
        proc = run_capture_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
            timeout_sec=30,
            stdin=producer.stdout,
        )
    finally:
        if producer.stdout is not None:
            producer.stdout.close()
        producer.wait(timeout=5)
    assert proc.returncode == 0
    assert "piped-payload" in proc.stdout


def test_default_stdin_is_isolated_devnull() -> None:
    """With no explicit ``stdin=``, the child gets DEVNULL, never the parent's
    stdin — under ``mcp-serve`` the inherited stdin IS the live JSON-RPC pipe,
    and a child that reads it steals protocol bytes or blocks forever (run-12
    finding 4, the ``git_output`` server wedge).

    Fire path: the runner is exercised in a RE-EXEC'd parent whose stdin
    carries pending bytes (pytest's own fd 0 is already null-like, so calling
    the runner in-process could never catch an inheritance regression). The
    grandchild must read 0 bytes (DEVNULL EOF), not the parent's payload."""
    inner = (
        "from hpc_agent.infra.bounded_subprocess import run_capture_bounded; "
        "import sys; "
        "p = run_capture_bounded([sys.executable, '-c', "
        "'import sys; print(len(sys.stdin.buffer.read()))'], timeout_sec=30); "
        "print('CHILD_READ=' + p.stdout.strip())"
    )
    outer = subprocess.run(
        [sys.executable, "-c", inner],
        input="PROTOCOL-BYTES-THAT-MUST-NOT-LEAK",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert outer.returncode == 0, outer.stderr
    assert "CHILD_READ=0" in outer.stdout


def test_forwards_env() -> None:
    """``env=`` is forwarded to the child — rsync's ``RSYNC_RSH`` ssh-binary pin."""
    child_env = {**os.environ, "HPC_BOUNDED_TEST": "sentinel-value"}
    proc = run_capture_bounded(
        [sys.executable, "-c", "import os; print(os.environ.get('HPC_BOUNDED_TEST', 'MISSING'))"],
        timeout_sec=30,
        env=child_env,
    )
    assert proc.returncode == 0
    assert "sentinel-value" in proc.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pid-liveness probe (os.kill(pid, 0))")
def test_grandchild_pid_is_reaped_not_orphaned() -> None:
    """Deterministic proof (Linux CI) that the tree-kill reaches the grandchild.

    The parent prints its grandchild's PID before sleeping; ``run_capture_bounded``
    drains that into the raised ``TimeoutExpired.output``. After the timeout the
    grandchild's process group was SIGKILL'd, so the PID must go away.
    """
    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        run_capture_bounded([sys.executable, "-c", _PARENT_SPAWNS_GRANDCHILD], timeout_sec=1)
    out = (exc_info.value.output or "").strip()
    assert out, "grandchild PID was not captured — cannot verify the reap"
    gc_pid = int(out.split()[0])

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(gc_pid, 0)
        except ProcessLookupError:
            return  # reaped — the tree kill reached the grandchild
        except PermissionError:
            return  # pid recycled to another owner; ours is gone
        time.sleep(0.05)
    pytest.fail(f"grandchild {gc_pid} still alive after timeout — tree not killed")
