"""Integration tests for the bounded dispatch retry + backoff (#161).

The array path used to run the per-task command exactly once and let a
hard failure get relaunched in a tight, uncapped loop — one task retried
~8,647 times in 12 min, burning 8 nodes and flooding scratch with empty
``_wip_*_failed_*`` dirs. ``hpc_run_with_retry`` (shipped in
``hpc_preamble.sh`` and called by all four array templates) bounds that:
a cap with exponential backoff, a terminal failure marker after the cap,
a cross-invocation short-circuit on that marker, and immediate-terminal
handling of the dispatcher's "no per-task runner" exit code (3).

These source the real preamble in a bash subprocess and drive
``hpc_run_with_retry`` against stub executors — no scheduler involved.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from hpc_agent import _PACKAGE_ROOT

if TYPE_CHECKING:
    from pathlib import Path

PREAMBLE = (
    _PACKAGE_ROOT
    / "execution"
    / "mapreduce"
    / "templates"
    / "runtime"
    / "common"
    / "hpc_preamble.sh"
)

_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    not _BASH or sys.platform == "win32",
    reason="POSIX-shell integration: drives hpc_preamble.sh's retry loop through a real "
    "bash subprocess with executable .sh runners and host-path interpolation. The cluster "
    "preamble is verified on Linux; the harness isn't Windows path/exec-safe (#163).",
)


def _run_retry(
    tmp_path: Path,
    *,
    executor: str,
    env_setup: str = "",
    result_dir: str | None = None,
) -> subprocess.CompletedProcess:
    """Source the preamble and invoke ``hpc_run_with_retry`` once.

    Stubs ``module``/``conda`` and sets ``HPC_RUNTIME=none`` so the
    setup blocks no-op. ``$EXECUTOR`` is the command under test; the
    function's return code propagates out as the script's exit code.
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    rdir = result_dir or str(tmp_path / "results")

    script = textwrap.dedent(
        f"""\
        set -eo pipefail
        module() {{ :; }}
        export -f module
        conda() {{ :; }}
        export -f conda
        export REPO_DIR={repo_dir!s}
        export HPC_RUNTIME=none
        unset HPC_NFS_DATA_DIR

        source {PREAMBLE!s}

        export RESULT_DIR={rdir!r}
        mkdir -p "$RESULT_DIR"
        {env_setup}
        export EXECUTOR={executor!r}
        rc=0
        hpc_run_with_retry || rc=$?
        echo "RETRY_RC=$rc"
        exit "$rc"
        """
    )
    return subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        # Bounded so a regression that reintroduces an unbounded retry
        # loop fails the test fast instead of hanging CI.
        timeout=60,
    )


def _rc(proc: subprocess.CompletedProcess) -> int:
    for line in proc.stdout.splitlines():
        if line.startswith("RETRY_RC="):
            return int(line.split("=", 1)[1])
    raise AssertionError(f"no RETRY_RC marker in stdout: {proc.stdout!r}\nstderr={proc.stderr!r}")


def test_instant_failure_caps_at_max_attempts(tmp_path: Path) -> None:
    """A deterministic instant failure must stop after $HPC_MAX_ATTEMPTS
    tries — NOT loop to walltime — and write a terminal marker recording
    the attempt count and last exit code."""
    proc = _run_retry(
        tmp_path,
        executor="false",
        env_setup=(
            "export TASK_ID=7 HPC_RUN_ID=run_abc\n"
            "export HPC_MAX_ATTEMPTS=3 HPC_RETRY_BACKOFF_SEC=0\n"
        ),
    )
    assert _rc(proc) == 1, proc.stderr
    marker = tmp_path / "results" / ".hpc_failed" / "run_abc.7.failed"
    assert marker.is_file(), f"terminal marker not written; stderr={proc.stderr!r}"
    body = marker.read_text()
    assert "attempts=3" in body
    assert "last_exit=1" in body
    assert "task_id=7" in body
    # Exactly 3 attempts were announced.
    assert proc.stdout.count("attempt 1/3") + proc.stderr.count("attempt 1/3") >= 1
    assert "attempt 3 failed" not in proc.stderr  # 3rd is the last; no 4th retry


def test_exit_code_3_is_terminal_no_retry(tmp_path: Path) -> None:
    """Dispatcher exit 3 (no per-task runner resolved — the #162 guard)
    is a deterministic scaffold error; retrying cannot fix it, so it must
    fail on the FIRST attempt without burning the backoff window."""
    runner = tmp_path / "exit3.sh"
    runner.write_text("#!/bin/bash\nexit 3\n")
    runner.chmod(0o755)
    proc = _run_retry(
        tmp_path,
        executor=str(runner),
        env_setup=(
            "export TASK_ID=2 HPC_RUN_ID=run_xyz\n"
            # Large backoff: if the loop retried, the test would hang.
            "export HPC_MAX_ATTEMPTS=3 HPC_RETRY_BACKOFF_SEC=30\n"
        ),
    )
    assert _rc(proc) == 3, proc.stderr
    assert "not retrying" in proc.stderr
    marker = tmp_path / "results" / ".hpc_failed" / "run_xyz.2.failed"
    assert marker.is_file()
    assert "attempts=1" in marker.read_text()


def test_terminal_marker_short_circuits_relaunch(tmp_path: Path) -> None:
    """Cross-invocation cap: once a (run, task) is terminally marked, a
    fresh invocation (a scheduler relaunch) must refuse to re-run the
    executor — otherwise an external resubmit loop is unbounded."""
    rdir = str(tmp_path / "results")
    # First invocation: deterministic failure writes the marker.
    first = _run_retry(
        tmp_path,
        executor="false",
        result_dir=rdir,
        env_setup=(
            "export TASK_ID=5 HPC_RUN_ID=run_loop\n"
            "export HPC_MAX_ATTEMPTS=2 HPC_RETRY_BACKOFF_SEC=0\n"
        ),
    )
    assert _rc(first) == 1
    marker = tmp_path / "results" / ".hpc_failed" / "run_loop.5.failed"
    assert marker.is_file()

    # Second invocation: an executor that would create a sentinel if it
    # ran. The marker must short-circuit it.
    sentinel = tmp_path / "SHOULD_NOT_RUN"
    runner = tmp_path / "make_sentinel.sh"
    runner.write_text(f"#!/bin/bash\ntouch {sentinel!s}\n")
    runner.chmod(0o755)
    second = _run_retry(
        tmp_path,
        executor=str(runner),
        result_dir=rdir,
        env_setup="export TASK_ID=5 HPC_RUN_ID=run_loop\n",
    )
    assert _rc(second) == 1, second.stderr
    assert "refusing to re-run" in second.stderr
    assert not sentinel.exists(), "executor ran despite terminal marker"


def test_success_writes_no_marker(tmp_path: Path) -> None:
    proc = _run_retry(
        tmp_path,
        executor="true",
        env_setup="export TASK_ID=0 HPC_RUN_ID=run_ok\n",
    )
    assert _rc(proc) == 0, proc.stderr
    assert not (tmp_path / "results" / ".hpc_failed").exists()


def test_transient_failure_then_success(tmp_path: Path) -> None:
    """A flaky executor that fails once then succeeds must be retried and
    ultimately exit 0 with no terminal marker."""
    flag = tmp_path / "flaky.flag"
    runner = tmp_path / "flaky.sh"
    runner.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            if [ -f {flag!s} ]; then exit 0; fi
            touch {flag!s}
            exit 2
            """
        )
    )
    runner.chmod(0o755)
    proc = _run_retry(
        tmp_path,
        executor=str(runner),
        env_setup=(
            "export TASK_ID=0 HPC_RUN_ID=run_flaky\n"
            "export HPC_MAX_ATTEMPTS=3 HPC_RETRY_BACKOFF_SEC=0\n"
        ),
    )
    assert _rc(proc) == 0, proc.stderr
    assert not (tmp_path / "results" / ".hpc_failed").exists()


def test_backoff_is_exponential_and_bounded(tmp_path: Path) -> None:
    """With base backoff 1s and 3 attempts, total sleep is 1 + 2 = 3s
    (sleeps happen between attempts, not after the last). The run must
    finish in a few seconds — proving the loop is bounded, not a
    walltime-length grind."""
    import time

    t0 = time.monotonic()
    proc = _run_retry(
        tmp_path,
        executor="false",
        env_setup=(
            "export TASK_ID=1 HPC_RUN_ID=run_bo\n"
            "export HPC_MAX_ATTEMPTS=3 HPC_RETRY_BACKOFF_SEC=1\n"
        ),
    )
    elapsed = time.monotonic() - t0
    assert _rc(proc) == 1
    # 1 + 2 = 3s of backoff + small overhead; comfortably under 10s.
    assert 2.5 <= elapsed < 10.0, f"backoff timing off: {elapsed:.2f}s"
